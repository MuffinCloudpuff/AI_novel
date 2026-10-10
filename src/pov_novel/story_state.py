"""Author state, grounded deltas and rebuildable projections; never actor input."""
import re
from typing import Literal
from pydantic import Field

from .models import Identifier, Record, SourceRef
from .store import digest, read_json, write_json


class HookCandidate(Record):
    text: str = Field(min_length=1, max_length=2000)
    expected_payoff: int | None = Field(default=None, ge=1)
    source_refs: list[SourceRef] = Field(min_length=1, max_length=10)


class HookOperation(Record):
    op: Literal['upsert', 'mention', 'resolve', 'defer']
    id: Identifier
    text: str = ''
    expected_payoff: int | None = Field(default=None, ge=1)
    source_refs: list[SourceRef] = Field(min_length=1, max_length=10)


class StateFact(Record):
    subject: str = Field(min_length=1, max_length=160)
    predicate: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=2000)
    kind: Literal['relationship', 'location', 'item', 'world', 'time']
    # "current" replaces the previous current triple; past facts stay in history.
    source_refs: list[SourceRef] = Field(min_length=1, max_length=10)


class RuntimeDelta(Record):
    summary: str = Field(min_length=1, max_length=4000)
    new_information: list[str] = Field(default_factory=list, max_length=30)
    unresolved_questions: list[str] = Field(default_factory=list, max_length=30)
    new_hook_candidates: list[HookCandidate] = Field(default_factory=list, max_length=20)
    hook_ops: list[HookOperation] = Field(default_factory=list, max_length=30)
    facts: list[StateFact] = Field(default_factory=list, max_length=30)


class Dismissal(Record):
    reason: str = Field(min_length=1, max_length=4000)


class ProjectSettings(Record):
    schema_version: Literal[2] = 2
    dismissals: dict[str, Dismissal] = Field(default_factory=dict)
    skills: dict[str, bool] = Field(default_factory=dict)
    genre: str = Field(default='', max_length=120)
    absent_after: int = Field(default=10, ge=1)


STATE_INSTRUCTION = '''结算已经接受审核的正文，不能追加剧情。summary 是本场景摘要；新信息和未解问题保留不确定性。
仅对当前 draft/events 中实际发生、可逐字举证的变化提交状态增量。
每个 fact/hook 引用 sources 中的 source_id 和连续逐字 quote。不把人物猜测、谎言或心理当成世界事实。
新伏笔仅 new_hook_candidates，ID 由宿主生成。已有伏笔用 hook_ops，禁止自造 ID。
upsert 修改已有伏笔说明；mention 推进；resolve 明确回收；defer 仅作者明确推迟时使用。
facts 用 subject/predicate/value 记录当前关系、位置、物品、时间或世界约束，保留旧时态历史。
没有明确变更就返回空列表，不能为填字段编造。'''


def empty_state():
    return {'schema_version': 2, 'chapter': 0, 'hooks': {}, 'facts': {}, 'history': [],
            'timeline': [], 'relationships': [], 'world': [], 'appearances': {}, 'summaries': []}


def read_author_material(store):
    for item in store.accepted():
        if 'author_material' in item:
            return item['author_material']
    foundation = store.path('imports', 'foundation', 'author_material.json')
    if foundation.exists():
        return read_json(foundation)['materials']
    result = {}
    for name in ('world', 'relationships', 'timeline', 'hooks'):
        path = store.path(name + '.json')
        result[name] = read_json(path) if path.exists() else {'items': []}
    return result


def apply_delta(state, delta, chapter, scene_id):
    for candidate in delta.get('new_hook_candidates', []):
        hid = 'hook-' + digest({'scene': scene_id, 'text': candidate['text']})[:20]
        state['hooks'].setdefault(hid, {**candidate, 'id': hid, 'status': 'open',
            'start_chapter': chapter, 'last_advanced_chapter': chapter})
    for operation in delta.get('hook_ops', []):
        hid = operation['id']
        if hid not in state['hooks']:
            raise ValueError('伏笔增量引用未知 ID：' + hid)
        hook = state['hooks'][hid]
        op = operation['op']
        if op == 'upsert':
            if operation.get('text'):
                hook['text'] = operation['text']
        elif op == 'resolve':
            hook['status'] = 'resolved'
        elif op == 'defer':
            hook['status'] = 'deferred'
        elif op != 'mention':
            raise ValueError('未知伏笔操作')
        if operation.get('expected_payoff') is not None:
            hook['expected_payoff'] = operation['expected_payoff']
        hook['last_advanced_chapter'] = chapter
        hook['source_refs'] = operation.get('source_refs', [])
    for fact in delta.get('facts', []):
        key = digest({'kind': fact['kind'], 'subject': fact['subject'], 'predicate': fact['predicate']})
        current = {**fact, 'chapter': chapter, 'scene_id': scene_id}
        state['facts'][key] = current
        state['history'].append(current)


def rebuild_state(store):
    state = empty_state()
    materials = read_author_material(store)
    for name in ('world', 'timeline', 'relationships'):
        state[name] = materials[name].get('items', [])
    for fact in materials['hooks'].get('items', []):
        hid = fact.get('id') or 'hook-' + digest({'source': fact.get('source_id'), 'text': fact['text']})[:20]
        state['hooks'][hid] = {**fact, 'id': hid, 'status': fact.get('status', 'open'),
            'start_chapter': fact.get('start_chapter', 1),
            'last_advanced_chapter': fact.get('last_advanced_chapter', 1),
            'expected_payoff': fact.get('expected_payoff')}
    for accepted in store.accepted():
        summaries = accepted.get('chapter_summaries') or [accepted.get('summary')]
        if accepted.get('provenance') == 'imported' and not any(summaries):
            # Legacy import manifests still replay into per-chapter projections.
            from .chapter_splitter import split_chapters
            config = store.config()
            originals = sorted(store.path('imports', 'chapters').glob('*.md'))
            chapters = [chapter for path in originals for chapter in split_chapters(path.read_text(encoding='utf-8'), fallback=path.stem)] if originals else split_chapters(accepted['prose'], fallback='imported')
            summaries = [{'schema_version': 2, 'scene_id': f'imported_chapter_{index:04d}',
                'chapter': index, 'title': chapter.title, 'pov': None, 'summary': chapter.content,
                'present_characters': [card.id for card in config.characters if card.name in chapter.raw],
                'event_ids': [], 'events': [], 'new_information': [], 'unresolved_questions': [],
                'author_only': True, 'prose': chapter.raw} for index, chapter in enumerate(chapters, 1)]
        for summary in summaries:
            if summary:
                state['summaries'].append(summary)
                state['chapter'] = max(state['chapter'], summary['chapter'])
                for cid in summary.get('present_characters', []):
                    state['appearances'][cid] = summary['chapter']
        chapter = accepted.get('summary', {}).get('chapter', accepted['sequence'])
        state['chapter'] = max(state['chapter'], chapter)
        # Legacy accepted scenes receive deterministic structured summaries.
        if not any(summaries) and accepted.get('scene'):
            scene = accepted['scene']
            summary = scene_summary(scene, accepted['events'], accepted['prose'], chapter)
            state['summaries'].append(summary)
            for cid in scene['present_characters']:
                state['appearances'][cid] = chapter
        apply_delta(state, accepted.get('state_delta', {}), chapter, accepted['scene_id'])
    return state


def scene_summary(scene, events, prose, chapter, delta=None):
    delta = delta or {}
    return {'schema_version': 2, 'scene_id': scene['id'], 'chapter': chapter,
        'title': scene.get('goal', scene['id']), 'pov': scene.get('pov_character_id'),
        'present_characters': scene.get('present_characters', []), 'setting': scene.get('setting', ''),
        'event_ids': [event['id'] for event in events], 'events': events,
        'summary': delta.get('summary') or '\n'.join(event['text'] for event in events),
        'new_information': delta.get('new_information', []),
        'unresolved_questions': delta.get('unresolved_questions', []),
        # Import prose is author-only. POV retrieval uses separately filtered events.
        'author_only': True, 'prose': prose}


def narrative_length(prose):
    lines = []
    for line in prose.splitlines():
        if line.lstrip().startswith('```'):
            continue
        if line.lstrip().startswith('#'):
            continue
        # Fences themselves and Markdown headings do not count; body does.
        lines.append(line)
    return len(re.sub(r'\s+', '', '\n'.join(lines)))


def deterministic_findings(state, scene, plan, events, turns, cards, prose, *, absent_after=10):
    chapter = scene.chapter or state['chapter'] + 1
    findings = []
    def add(category, entity, message, hard=False):
        findings.append({'key': category + ':' + entity, 'category': category,
                         'entity_id': entity, 'message': message, 'hard': hard})
    for hook in state['hooks'].values():
        due = hook.get('expected_payoff')
        if hook['status'] == 'open' and due is not None and chapter > due:
            add('overdue_hook', hook['id'], '伏笔超过作者预期回收章节：' + hook['text'])
    for cid, last in state['appearances'].items():
        if cid not in scene.present_characters and chapter - last >= absent_after:
            add('absent_character', cid, f'人物连续 {chapter-last} 章未出场')
    event_ids = {e['id'] for e in events}
    turn_pairs = {(t['character_id'], t['after_event_id']) for t in turns}
    for index, task in enumerate(plan.tasks):
        if task.after_event_id not in event_ids or (task.character_id and (task.character_id, task.after_event_id) not in turn_pairs):
            add('task_coverage', str(index), '人物任务或触发事件未执行', True)
    for cid, card in cards.items():
        if not card.name.strip() or not card.personality.strip():
            add('missing_card_field', cid, '人物卡缺少姓名或性格', True)
    # Imported natural-language relationship claims cannot be mechanically judged.
    triples = {}
    for fact in state.get('history', []):
        key = (fact['subject'], fact['predicate'], fact['chapter'])
        if fact['kind'] == 'relationship' and key in triples and triples[key] != fact['value']:
            add('relationship_conflict', digest(key)[:20], '同章关系三元组取值冲突')
        triples[key] = fact['value']
    count = narrative_length(prose)
    if scene.min_characters and count < scene.min_characters:
        add('length_min', scene.id, f'正文 {count} 字，低于明确下限 {scene.min_characters}', True)
    if scene.max_characters and count > scene.max_characters:
        add('length_max', scene.id, f'正文 {count} 字，超过明确上限 {scene.max_characters}', True)
    return findings, {'characters': count, 'chapter': chapter, 'task_count': len(plan.tasks), 'event_count': len(events)}


def settings(store):
    path = store.path('settings.json')
    value = read_json(path) if path.exists() else {}
    if value.get('schema_version') == 1:
        value = {**value, 'schema_version': 2}
    options = ProjectSettings.model_validate(value).model_dump()
    if any(not re.fullmatch(r'[a-zA-Z0-9_-]+:[a-zA-Z0-9_-]+', key) for key in options['dismissals']):
        raise ValueError('豁免设置中的键必须为 category:entity_id')
    if any(not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', key) for key in options['skills']):
        raise ValueError('技能设置中的 ID 无效')
    return options


def drop_exempt(findings, options):
    return [finding for finding in findings if finding['key'] not in options.get('dismissals', {})]


def dismiss(store, key, reason, *, restore=False):
    if not isinstance(key, str) or not isinstance(reason, str) or not isinstance(restore, bool):
        raise ValueError('豁免键和原因须为字符串，restore 须为布尔值')
    if not re.fullmatch(r'[a-zA-Z0-9_-]+:[a-zA-Z0-9_-]+', key) or (not restore and not reason.strip()):
        raise ValueError('豁免需要 category:entity_id 稳定键和原因')
    with store.lock():
        options = settings(store)
        if restore:
            options['dismissals'].pop(key, None)
        else:
            options['dismissals'][key] = {'reason': reason}
        write_json(store.path('settings.json'), options)
    return options
