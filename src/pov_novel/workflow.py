"""Real LangGraph workflow: director -> actor turns -> prose -> review -> commit."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .context import (build_character_context, merge_memories,
                      validate_character_result, validate_plan)
from .gateway import CachedGateway, Gateway, StructuredOutputError
from .semantic_context import ContextManager, compact_private_memory, semantic_input_budget
from .models import (CharacterResult, Claim, Draft, Event, Memory,
                     Plan, Review, SceneSpec)
from .review import SCHEMAS, normalize_review, passed, review_instruction, verify_refs, writer_feedback
from .store import FileStore, digest, read_json, write_json
from .story_state import (RuntimeDelta, STATE_INSTRUCTION, apply_delta, deterministic_findings,
                         drop_exempt, read_author_material, scene_summary, settings)
from .retrieval import StoryIndex
from .skills import active_skills, augment

PROMPTS = Path(__file__).parent / "prompts"


def prompt(name):
    return (PROMPTS / (name + ".md")).read_text(encoding="utf-8")


class State(TypedDict, total=False):
    scene: dict
    request_hash: str
    base_sequence: int
    plan: dict
    task_index: int
    exposed_index: int
    events: list[dict]
    turns: list[dict]
    memories: dict[str, dict]
    actor_attempt: int
    actor_valid: bool
    actor_feedback: str
    retry_batch: int
    draft: str
    reviews: dict[str, dict]
    review_round: int
    review_passed: bool
    manual_accept: bool
    pause_reason: str
    phase: str
    state_delta: dict
    summary: dict
    findings: list[dict]
    telemetry: dict
    safe_feedback: dict
    previous_draft: str
    artifacts: dict


class NovelWorkflow:
    def __init__(self, store: FileStore, gateway: Gateway, *, max_rewrites=2,
                 threshold=7.0, context_budget=24000, recent_limit=24, draft_file=None, skills=()):
        if max_rewrites < 0 or not 0 <= threshold <= 10 or context_budget < 2000 or recent_limit < 2:
            raise ValueError("重试次数、分数阈值或记忆预算无效")
        self.store = store
        self.config = store.config()
        self.cards = {card.id: card for card in self.config.characters}
        self.gateway = CachedGateway(gateway, store.path(".runtime", "model_cache"))
        self.max_rewrites = max_rewrites
        self.threshold = threshold
        self.context_budget = context_budget
        self.recent_limit = recent_limit
        self.manual_draft = Path(draft_file).read_text(encoding='utf-8-sig') if draft_file else None
        if self.manual_draft is not None and not self.manual_draft.strip():
            raise ValueError('人工草稿不能为空')
        self.skill_switches = skills
        self.skill_identity = {role: [{'id': skill['id'], 'hashes': [item['sha256'] for item in skill['provenance']]}
            for skill in active_skills(store, role, skills)] for role in ('director', 'writer', 'style', 'outline', 'editor', 'state')}
        available = semantic_input_budget(getattr(gateway, 'context_window', 131072),
                                          getattr(gateway, 'output_ceiling', 24000), overhead=8192)
        self.context_manager = ContextManager(self.gateway, available,
                                             store.path('.runtime', 'context_traces'))

    def instruction(self, role, base, scene_id):
        instruction, provenance = augment(self.store, role, base, self.skill_switches)
        if provenance:
            write_json(self.store.path('scenes', scene_id, 'skills', role + '.json'), {'schema_version': 2, 'skills': provenance})
        return instruction

    def artifact(self, role, status, reviewers=()):
        backend = self.gateway.gateway
        model = None if role == 'author' or role.startswith('host_') else getattr(backend, 'models', {}).get(role, backend.identity)
        return {'status': status, 'produced_by': {'role': role, 'model': model},
                'reviewed_by': [{'role': reviewer, 'model': None if reviewer.startswith('host_') else getattr(backend, 'models', {}).get(reviewer, backend.identity)} for reviewer in reviewers],
                'reviewed_at': datetime.now(timezone.utc).isoformat() if reviewers else None}

    def chapter(self, scene):
        return scene.chapter or self.store.story_state()['chapter'] + 1

    def retrieve(self, scene, *, pov=None):
        index = StoryIndex(self.store)
        # Rebuilding uses no model; failures never erase canonical manifests.
        index.rebuild(self.store.story_state()['summaries'])
        return index.select(scene.goal + '\n' + scene.setting, self.gateway, pov=pov)

    def state_context(self):
        state = self.store.story_state()
        # Full prose/temporal history are retrieved, not reread on every call.
        return {key: state[key] for key in ('chapter', 'hooks', 'facts', 'world', 'relationships', 'timeline', 'appearances')}

    def progress(self, state, phase):
        self.store.status(state["scene"]["id"], phase=phase,
                          task_index=state.get("task_index", 0),
                          review_round=state.get("review_round", 0),
                          request_hash=state["request_hash"])

    def compress(self, memory: Memory):
        return compact_private_memory(memory, self.gateway, budget=self.context_budget,
                                      recent_limit=self.recent_limit)

    def planning_packet(self, scene):
        protected = {"outline": self.config.outline, "scene": scene.model_dump(),
                     "characters": [{"id": card.id, "name": card.name,
                                     "known_secrets": card.known_secrets,
                                     "current_goal": card.current_goal} for card in self.cards.values()]}
        history = {"character_profiles": {card.id: {"personality": card.personality, "voice": card.voice}
                                          for card in self.cards.values()},
                   'story_state': self.state_context(),
                   'retrieved_scenes': self.retrieve(scene)}
        return self.context_manager.packet(protected, history, recipe="director",
            intent="安排本场景的人物任务，保持人物身份、性格和当前动机")

    def plan_node(self, state: State):
        self.progress(state, "planning")
        scene = SceneSpec.model_validate(state["scene"])
        if scene.chapter is not None and scene.chapter < self.store.story_state()['chapter']:
            raise ValueError('不能在已归档章节之前追加场景；修改旧章请创建独立项目')
        plan = scene.plan if scene.plan is not None else self.gateway.generate(
            "director", self.instruction('director', prompt("director"), scene.id), self.planning_packet(scene), Plan)
        validate_plan(plan, scene, set(self.cards))
        existing_ids = {event["id"] for entry in self.store.accepted() for event in entry["events"]}
        if existing_ids.intersection(event.id for event in scene.events):
            raise ValueError("事件 ID 已用于先前场景，请使用全书唯一 ID")
        write_json(self.store.path("scenes", scene.id, "plan.json"), plan.model_dump())
        plan_artifact = {**self.artifact('author' if scene.plan is not None else 'director', 'proposed', ['host_plan_gate']), 'source': 'plan.json'}
        return {"plan": plan.model_dump(), "task_index": 0, "exposed_index": -1,
                "events": [], "turns": [], "memories": {
                    cid: self.store.memory(cid).model_dump() for cid in scene.present_characters},
                "actor_attempt": 0, "actor_feedback": "", "review_round": 0,
                "manual_accept": False, "retry_batch": 0, "phase": "dispatching", 'artifacts': {'plan': plan_artifact}}

    def dispatch_node(self, state: State):
        scene = SceneSpec.model_validate(state["scene"])
        tasks = Plan.model_validate(state["plan"]).tasks
        self.progress(state, "dispatching")
        if state["task_index"] >= len(tasks):
            return {"phase": "composing"}
        task = tasks[state["task_index"]]
        stop = next(i for i, e in enumerate(scene.events) if e.id == task.after_event_id)
        events = state["events"] + [e.model_dump() for e in scene.events[state["exposed_index"] + 1:stop + 1]]
        return {"events": events, "exposed_index": stop, "actor_attempt": 0,
                "actor_feedback": "", "phase": "acting" if task.character_id else "skipping"}

    def actor_node(self, state: State):
        self.progress(state, "generating_character")
        scene = SceneSpec.model_validate(state["scene"])
        task = Plan.model_validate(state["plan"]).tasks[state["task_index"]]
        cid = task.character_id
        task_id = f"{scene.id}-turn{state['task_index'] + 1:02d}"
        memories = dict(state["memories"])
        previous_memory = Memory.model_validate(memories[cid])
        memory = self.compress(previous_memory)
        memories[cid] = memory.model_dump()
        context = build_character_context(self.cards[cid], memory, scene, task,
                                          [Event.model_validate(e) for e in state["events"]],
                                          self.context_budget)
        payload = context.model_dump()
        payload["validation_feedback"] = state.get("actor_feedback", "")
        payload["generation_attempt"] = state["actor_attempt"] + 1
        payload["retry_batch"] = state["retry_batch"]
        # The cache and author trace are private project artifacts, never passed
        # to another character's model call.
        write_json(self.store.path("scenes", scene.id, "tasks", task_id + "-input.json"), payload)
        result = self.gateway.generate("character", prompt("character"), payload, CharacterResult)
        attempt = state["actor_attempt"] + 1
        write_json(self.store.path("scenes", scene.id, "tasks", f"{task_id}-attempt{attempt}.json"), result.model_dump())
        try:
            validate_character_result(result, context)
            if not task.need_psychology and result.private_text:
                raise ValueError("此任务不请求心理正文")
        except ValueError:
            return {"actor_attempt": attempt, "actor_valid": False,
                    "actor_feedback": "请只引用本次输入中已有的来源 ID；推测用 belief，遵守 need_psychology。",
                    "memories": memories, "pause_reason": "character_contract"}
        observations = [Claim(text=e.text, kind="observation", evidence_ids=[e.id], confidence="high")
                        for e in context.observations]
        memory = merge_memories(memory, observations + result.memories, result.emotion, result.current_goal)
        memories[cid] = memory.model_dump()
        public_text = "\n".join(filter(None, [result.public_action, result.public_dialogue]))
        events = list(state["events"])
        if public_text:
            events.append(Event(id=f"r-{digest(scene.id)[:16]}-{state['task_index'] + 1}",
                                text=public_text, visible_to=scene.present_characters).model_dump())
        write_json(self.store.path("characters", cid, "drafts",
                                   f"{task_id}-attempt{attempt}.json"), result.model_dump())
        turn = {"task_id": task_id, "character_id": cid,
                "after_event_id": task.after_event_id, "need_psychology": task.need_psychology,
                "result": result.model_dump()}
        artifacts = dict(state.get('artifacts', {}))
        artifacts[task_id] = {**self.artifact('character', 'proposed', ['host_character_gate']),
                              'source': f'tasks/{task_id}-attempt{attempt}.json'}
        if memory.summary != previous_memory.summary:
            artifacts[task_id + '-memory'] = {**self.artifact('memory', 'proposed', ['host_memory_gate']),
                                             'source': f'tasks/{task_id}-input.json#/memory'}
        return {"turns": state["turns"] + [turn], "events": events,
                "memories": memories, "actor_valid": True, "actor_attempt": attempt, 'artifacts': artifacts}

    def advance_node(self, state: State):
        return {"task_index": state["task_index"] + 1}

    def writing_packet(self, state: State):
        scene = SceneSpec.model_validate(state["scene"])
        # No pending world event is visible to earlier actor tasks. Remaining
        # events become available only to the final POV composition here.
        events = state["events"] + [e.model_dump() for e in scene.events[state["exposed_index"] + 1:]]
        visible = [{"id": e["id"], "text": e["text"]} for e in events
                   if scene.pov_character_id in e["visible_to"]]
        pov_thoughts = [turn["result"]["private_text"] for turn in state["turns"]
                       if turn["character_id"] == scene.pov_character_id and turn["need_psychology"]]
        # Author directions are not character context. Avoid copying clauses
        # naming another character's secrets into the POV composer's prompt.
        private_terms = set()
        for cid, card in self.cards.items():
            if cid != scene.pov_character_id:
                for secret in card.known_secrets:
                    for word in re.findall(r'[\u3400-\u9fff]+|[A-Za-z0-9_]+', secret):
                        private_terms.update(word[i:i+2] for i in range(len(word)-1))
        clauses = re.split(r'(?<=[。；;\n])', self.config.outline)
        narrative_outline = ''.join(clause for clause in clauses if not any(term in clause for term in private_terms))
        return {"pov": self.cards[scene.pov_character_id].name, "setting": scene.setting,
                'outline': narrative_outline, 'scene_goal': scene.goal,
                'intentional_choices': scene.intentional_choices,
                'outline_scope': '当前可叙述的总纲片段；完整作者总纲由规划和审核使用，不能推知未揭示事实。',
                "style": self.config.style, "style_sample": self.config.style_sample,
                "visible_events": visible, "pov_psychology": pov_thoughts,
                'pov_memory': state['memories'].get(scene.pov_character_id, {})}

    def compose_node(self, state: State):
        self.progress(state, "writing")
        packet = self.writing_packet(state)
        packet["retry_batch"] = state["retry_batch"]
        if state.get("draft") and state.get("reviews"):
            packet.update(draft=state["draft"], review_feedback=state.get('safe_feedback', {}),
                          revision_round=state["review_round"])
        scene = SceneSpec.model_validate(state['scene'])
        packet = self.context_manager.packet(packet, {'previous_scenes': self.retrieve(scene, pov=scene.pov_character_id)}, recipe="writer")
        instruction = self.instruction('writer', prompt('writer'), scene.id)
        draft = Draft(prose=self.manual_draft) if self.manual_draft is not None and state['review_round'] == 0 else self.gateway.generate_prose(instruction, packet)
        metadata = self.artifact('author' if self.manual_draft is not None and state['review_round'] == 0 else 'writer', 'proposed')
        write_json(self.store.path("scenes", state["scene"]["id"], "drafts",
                                   f"round{state['review_round']}.json"), {**draft.model_dump(), 'schema_version': 2, 'artifact': metadata})
        return {"draft": draft.prose, 'previous_draft': state.get('draft', ''), "phase": "auditing",
                'artifacts': {**state.get('artifacts', {}), 'draft': {**metadata, 'source': f'drafts/round{state["review_round"]}.json'}}}

    def audit_node(self, state: State):
        self.progress(state, "reviewing_style_and_outline")
        scene = SceneSpec.model_validate(state["scene"])
        events = state['events'] + [e.model_dump() for e in scene.events[state['exposed_index'] + 1:]]
        options = settings(self.store)
        findings, telemetry = deterministic_findings(self.store.story_state(), scene, Plan.model_validate(state['plan']),
            events, state['turns'], self.cards, state['draft'], absent_after=options.get('absent_after', 10))
        findings = drop_exempt(findings, options)
        packet = {"outline": self.config.outline, "scene_goal": scene.goal,
                  "style": self.config.style, "style_sample": self.config.style_sample,
                  "draft": state["draft"], "intentional_choices": scene.intentional_choices,
                  "pov_material": self.writing_packet(state),
                  'previous_draft': state.get('previous_draft') or None,
                  'deterministic_findings': findings, 'telemetry': telemetry, 'dismissals': options['dismissals'],
                  "world_events": events}
        packet = self.context_manager.packet(packet, {'previous_scenes': self.retrieve(scene),
            'story_state': self.state_context()}, recipe="review",
            intent="检查当前正文与前文的因果、人物状态和时间是否一致，保留来源")
        sources = self.review_sources(packet)
        packet['sources'] = {sid: {'path': source['path'], 'writer_visible': source['writer_visible']} for sid, source in sources.items()}
        if len(json.dumps(packet, ensure_ascii=False).encode('utf-8')) > self.context_manager.budget:
            raise ValueError('带来源目录的审核上下文超过预算；请减小场景或提高上下文窗口')
        reviews = {}
        waived_roles = []
        for role in ('style', 'outline', 'editor'):
            instruction = self.instruction(role, review_instruction(role), scene.id)
            if role == 'editor':
                instruction += ('\n这是已重写正文的评审。previous_draft 是实际旧稿，draft 是实际新稿。before_quality 必须是旧稿0到10分，不能返回 null。'
                    if packet.get('previous_draft') else '\n这是初稿评审，没有上一版。before_quality 必须为 null。')
            for attempt in range(3):
                raw = None
                try:
                    raw = self.gateway.generate(role, instruction, {**packet, 'evidence_attempt': attempt}, SCHEMAS[role])
                    report = normalize_review(role, raw, sources)
                    if role == 'editor' and bool(packet.get('previous_draft')) != (report.before_quality is not None):
                        raise ValueError('Editor before_quality 必须对应实际上一版正文；首次审核应为 null')
                    # Hosts, not prompts, honor the author's persistent decisions.
                    had_issues = bool(report.issues)
                    report.issues = [issue for issue in report.issues if issue.category + ':' + issue.entity_id not in options['dismissals']]
                    if had_issues and not report.issues:
                        waived_roles.append(role)
                    reviews[role] = report
                    break
                except (ValueError, StructuredOutputError) as exc:
                    write_json(self.store.path('scenes', scene.id, 'review_failures', f'{role}-round{state["review_round"]}-attempt{attempt}.json'),
                               {'schema_version': 2, 'error_type': type(exc).__name__, 'raw_output': getattr(exc, 'raw_output', None),
                                'report': raw.model_dump() if 'raw' in locals() and hasattr(raw, 'model_dump') else None})
                    if attempt == 2:
                        raise
                    instruction += '\n上次宿主校验失败：' + str(exc)[:400] + '\n严格按指定来源逐字引用，纠正格式；没有可靠问题时 issues=[]。'
        serialized = {role: report.model_dump() for role, report in reviews.items()}
        write_json(self.store.path("scenes", scene.id, "reviews", f"round{state['review_round']}.json"), serialized)
        artifacts = {key: {**value, 'status': 'in_review'} for key, value in state.get('artifacts', {}).items()}
        for role in reviews:
            artifacts['review-' + role] = {**self.artifact(role, 'in_review', ['host_quote_gate']),
                'source': f'reviews/round{state["review_round"]}.json#/{role}'}
        draft_producer = artifacts.get('draft', {}).get('produced_by', {'role': 'writer', 'model': None})
        artifacts['draft'] = {**self.artifact('writer', 'in_review', reviews), 'produced_by': draft_producer,
                              'source': f'drafts/round{state["review_round"]}.json'}
        write_json(self.store.path('scenes', scene.id, 'artifacts', f'round{state["review_round"]}.json'),
                   {'schema_version': 2, 'artifact': artifacts['draft'],
                    'findings': findings, 'telemetry': telemetry, 'reviews': serialized, 'waived_roles': waived_roles})
        return {"reviews": serialized, "review_passed": passed({role: report for role, report in reviews.items() if role not in waived_roles}, self.threshold) and not any(f['hard'] for f in findings),
                'safe_feedback': writer_feedback(reviews, sources), 'findings': findings, 'telemetry': telemetry,
                'artifacts': artifacts,
                "pause_reason": "style_or_outline", "phase": "audited"}

    @staticmethod
    def review_sources(packet):
        sources = {}
        def add(sid, text, path, visible=False):
            if isinstance(text, str) and text:
                sources[sid] = {'text': text, 'path': path, 'writer_visible': visible}
        for key in ('draft', 'previous_draft', 'outline', 'scene_goal', 'style', 'style_sample'):
            add(key, packet.get(key), key, key in ('draft', 'previous_draft', 'scene_goal', 'style', 'style_sample'))
        visible_ids = {e['id'] for e in packet['pov_material']['visible_events']}
        for index, event in enumerate(packet.get('world_events', [])):
            add('event-' + event['id'], event['text'], f'world_events.{index}.text', event['id'] in visible_ids)
        if 'compiled_history' in packet:
            add('compiled_history', packet['compiled_history']['content'], 'compiled_history.content')
        for index, item in enumerate(packet.get('previous_scenes', [])):
            add('history-' + item['scene_id'], item.get('prose') or item['summary'], f'previous_scenes.{index}.' + ('prose' if item.get('prose') else 'summary'))
        if 'story_state' in packet:
            current = packet['story_state']
            for hid, hook in current['hooks'].items():
                add('hook-' + hid, hook['text'], f'story_state.hooks.{hid}.text')
            for fid, fact in current['facts'].items():
                add('fact-' + fid, fact['value'], f'story_state.facts.{fid}.value')
            for category in ('world', 'relationships', 'timeline'):
                for index, fact in enumerate(current[category]):
                    add(f'{category}-{index}', fact.get('text'), f'story_state.{category}.{index}.text')
        return sources

    def settle_node(self, state):
        scene = SceneSpec.model_validate(state['scene'])
        events = state['events'] + [e.model_dump() for e in scene.events[state['exposed_index']+1:]]
        current = self.store.story_state()
        sources = {'draft': {'text': state['draft'], 'writer_visible': False}}
        sources.update({'event-' + e['id']: {'text': e['text'], 'writer_visible': False} for e in events})
        payload = {'sources': sources, 'known_hooks': current['hooks'], 'current_facts': current['facts'],
                   'chapter': self.chapter(scene), 'scene_id': scene.id}
        payload = self.context_manager.packet({'sources': sources, 'chapter': self.chapter(scene), 'scene_id': scene.id},
            {'known_hooks': current['hooks'], 'current_facts': current['facts']}, recipe='state')
        instruction = self.instruction('state', STATE_INSTRUCTION, scene.id)
        for attempt in range(3):
            try:
                delta = self.gateway.generate('state', instruction, {**payload, 'validation_attempt': attempt}, RuntimeDelta)
                for item in [*delta.new_hook_candidates, *delta.hook_ops, *delta.facts]:
                    verify_refs(item.source_refs, sources)
                apply_delta(json.loads(json.dumps(current)), delta.model_dump(), self.chapter(scene), scene.id)
                break
            except (ValueError, StructuredOutputError):
                if attempt == 2:
                    raise
                instruction += '\n上次状态或引用校验失败：不使用未知伏笔 ID，不改写 quote，不把推测升级为事实。'
        summary = scene_summary(scene.model_dump(), events, state['draft'], self.chapter(scene), delta.model_dump())
        write_json(self.store.path('scenes', scene.id, 'state_delta.json'), {'schema_version': 2, 'delta': delta.model_dump(), 'summary': summary})
        artifacts = {**state.get('artifacts', {}), 'state_delta': {**self.artifact('state', 'in_review', ['host_quote_gate', 'host_hook_gate']), 'source': 'state_delta.json'}}
        return {'state_delta': delta.model_dump(), 'summary': summary, 'phase': 'settled', 'artifacts': artifacts}

    def rewrite_node(self, state: State):
        return {"review_round": state["review_round"] + 1}

    def pause_node(self, state: State):
        self.progress(state, "awaiting_author")
        decision = interrupt({"scene_id": state["scene"]["id"], "reason": state["pause_reason"],
                              "reviews": state.get("reviews", {}),
                              "actions": ["retry", "cancel"]})
        if not isinstance(decision, dict) or decision.get("action") not in ("retry", "cancel"):
            raise ValueError("恢复操作必须是 retry 或 cancel")
        if decision["action"] == "cancel":
            self.progress(state, "cancelled")
            return {"phase": "cancelled"}
        return {"phase": "retry_actor" if state["pause_reason"] == "character_contract" else "retry_writer",
                "actor_attempt": 0, "review_round": 0, "retry_batch": state["retry_batch"] + 1}

    def archive_node(self, state: State):
        self.progress(state, "archiving")
        scene = SceneSpec.model_validate(state["scene"])
        accepted_path = self.store.path("scenes", scene.id, "accepted.json")
        if not accepted_path.exists():
            events = state["events"] + [e.model_dump() for e in scene.events[state["exposed_index"] + 1:]]
            memories = {}
            artifacts = dict(state.get('artifacts', {}))
            for cid in scene.present_characters:
                memory = Memory.model_validate(state["memories"][cid])
                previous_summary = list(memory.summary)
                observations = [Claim(text=e["text"], kind="observation", evidence_ids=[e["id"]], confidence="high")
                                for e in events if cid in e["visible_to"]]
                memory = self.compress(merge_memories(memory, observations))
                memory.version += 1
                memories[cid] = memory.model_dump()
                artifacts['memory-' + cid] = {**self.artifact('memory' if memory.summary != previous_summary else 'host_memory_update', 'in_review', ['host_memory_gate']),
                    'source': f'../../characters/{cid}/memory.json'}
            entries = self.store.accepted()
            sequence = max((entry["sequence"] for entry in entries), default=0) + 1
            manifest = {'schema_version': 2, 'summary': state['summary'], 'state_delta': state['state_delta'],
                        'artifact': self.artifact('writer', 'final', state['reviews']),
                        'stage_artifacts': {key: {**value, 'status': 'final'} for key, value in artifacts.items()},
                        'findings': state.get('findings', []), 'telemetry': state.get('telemetry', {}),
                        "scene_id": scene.id, "request_hash": state["request_hash"],
                        "sequence": sequence, "scene": scene.model_dump(), "prose": state["draft"],
                        "events": events, "turns": state["turns"], "memories": memories,
                        "reviews": state["reviews"], "plan": state["plan"]}
            if 'draft' in manifest['stage_artifacts']:
                manifest['artifact']['produced_by'] = manifest['stage_artifacts']['draft']['produced_by']
            if not any('author_material' in item for item in entries):
                manifest['author_material'] = read_author_material(self.store)
            # This single atomic file is the commit point. All other outputs
            # can be reconstructed after a crash without appending duplicates.
            write_json(accepted_path, manifest)
        self.store.materialize()
        self.progress(state, "completed")
        return {"phase": "completed"}

    def graph(self, checkpointer):
        graph = StateGraph(State)
        for name, method in [("plan", self.plan_node), ("dispatch", self.dispatch_node),
                             ("actor", self.actor_node), ("advance", self.advance_node),
                             ("compose", self.compose_node), ("audit", self.audit_node),
                             ("rewrite", self.rewrite_node), ("author", self.pause_node),
                             ('settle', self.settle_node), ("archive", self.archive_node)]:
            graph.add_node(name, method)
        graph.add_edge(START, "plan")
        graph.add_edge("plan", "dispatch")
        graph.add_conditional_edges("dispatch", lambda s: s["phase"],
                                    {"acting": "actor", "skipping": "advance", "composing": "compose"})
        graph.add_conditional_edges("actor", lambda s: "ok" if s["actor_valid"] else
                                    "retry" if s["actor_attempt"] <= self.max_rewrites else "pause",
                                    {"ok": "advance", "retry": "actor", "pause": "author"})
        graph.add_edge("advance", "dispatch")
        graph.add_edge("compose", "audit")
        graph.add_conditional_edges("audit", lambda s: "ok" if s["review_passed"] else
                                    "retry" if s["review_round"] < self.max_rewrites else "pause",
                                    {"ok": "settle", "retry": "rewrite", "pause": "author"})
        graph.add_edge('settle', 'archive')
        graph.add_edge("rewrite", "compose")
        graph.add_conditional_edges("author", lambda s: s["phase"],
                                    {"cancelled": END, "retry_actor": "actor", "retry_writer": "compose"})
        graph.add_edge("archive", END)
        return graph.compile(checkpointer=checkpointer)

    def run(self, scene: SceneSpec, action=None):
        with self.store.lock():
            # New optional defaults do not invalidate pre-upgrade scene identities.
            scene_data = scene.model_dump()
            for key in ('chapter', 'min_characters', 'max_characters'):
                if scene_data[key] is None:
                    scene_data.pop(key)
            request_hash = digest({"scene": scene_data, "story": self.config.model_dump()})
            runtime_hash = digest({"gateway": self.gateway.identity, "threshold": self.threshold,
                                   "max_rewrites": self.max_rewrites, "context_budget": self.context_budget,
                                   "recent_limit": self.recent_limit, 'settings': {k: v for k, v in settings(self.store).items() if k != 'dismissals'},
                                   'skills': self.skill_identity, 'manual_draft': digest(self.manual_draft) if self.manual_draft is not None else None,
                                   'contracts': 2})
            scene_dir = self.store.path("scenes", scene.id)
            accepted_path = scene_dir / "accepted.json"
            if accepted_path.exists():
                item = read_json(accepted_path)
                if item["request_hash"] != request_hash:
                    raise ValueError("已归档场景内容不同；修改请创建新的场景 ID")
                self.store.materialize()
                self.store.status(scene.id, phase="completed", request_hash=request_hash)
                return {"scene_id": scene.id, "phase": "completed", "prose": item["prose"],
                        "reviews": item["reviews"], "cached": True}
            entries = self.store.accepted()
            base = max((item["sequence"] for item in entries), default=0)
            binding_path = scene_dir / "binding.json"
            binding = {'schema_version': 2, "request_hash": request_hash, "runtime_hash": runtime_hash, "base_sequence": base}
            if binding_path.exists() and read_json(binding_path) != binding:
                raise ValueError("未完成场景的输入、运行模式或前文发生变化，请使用新场景 ID")
            write_json(binding_path, binding)
            write_json(scene_dir / "request.json", scene.model_dump())
            self.store.path(".runtime").mkdir(parents=True, exist_ok=True)
            with SqliteSaver.from_conn_string(str(self.store.path(".runtime", "checkpoints.sqlite"))) as saver:
                graph = self.graph(saver)
                config = {"configurable": {"thread_id": scene.id}, "recursion_limit": 160}
                snapshot = graph.get_state(config)
                if action:
                    if not snapshot.interrupts:
                        raise ValueError("场景没有等待作者处理的审核任务")
                    input_value = Command(resume={"action": action})
                elif snapshot.values:
                    if snapshot.interrupts:
                        return self.result(scene.id, snapshot.values, snapshot.interrupts)
                    input_value = None
                else:
                    input_value = {"scene": scene.model_dump(), "request_hash": request_hash,
                                   "base_sequence": base}
                try:
                    output = graph.invoke(input_value, config=config)
                except Exception as exc:
                    self.store.status(scene.id, phase="error", error_type=type(exc).__name__, request_hash=request_hash)
                    raise
                snapshot = graph.get_state(config)
                return self.result(scene.id, output, snapshot.interrupts)

    @staticmethod
    def result(scene_id, output, interrupts=()):
        return {"scene_id": scene_id, "phase": "awaiting_author" if interrupts else output.get("phase", "error"),
                "prose": output.get("draft", ""), "reviews": output.get("reviews", {}),
                "interrupts": [item.value for item in interrupts], "cached": False}
