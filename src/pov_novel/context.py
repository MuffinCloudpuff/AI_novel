import json

from .models import CharacterContext, CharacterResult, Claim, Event, Memory, SceneSpec, Task


def estimate_tokens(value):
    # Conservative upper bound: a tokenizer token consumes at least one UTF-8 byte.
    # This avoids a model-specific tokenizer dependency; actual usage is lower.
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def validate_plan(plan, scene: SceneSpec, known_characters):
    order = {event.id: index for index, event in enumerate(scene.events)}
    last = -1
    for task in plan.tasks:
        if task.character_id is not None and task.character_id not in scene.present_characters:
            raise ValueError("任务指向未在场的角色")
        if task.after_event_id not in order:
            raise ValueError("任务引用不存在的事件")
        if order[task.after_event_id] < last:
            raise ValueError("任务事件必须按时间顺序推进")
        last = order[task.after_event_id]
    for cid in scene.present_characters:
        if cid not in known_characters:
            raise ValueError(f"角色 {cid} 没有角色卡")
    for event in scene.events:
        if not set(event.visible_to).issubset(known_characters):
            raise ValueError("事件可见列表包含未知角色")


def build_character_context(card, memory: Memory, scene: SceneSpec, task: Task,
                            preceding_events: list[Event], budget: int):
    # Don't forward the task's director rationale, scene goal, outline, global
    # facts, other cards, other psychology, or any future scene events.
    visible = [Event(id=e.id, text=e.text, visible_to=[card.id])
               for e in preceding_events if card.id in e.visible_to]
    context = CharacterContext(character=card.model_copy(update={"initial_memories": []}), memory=memory.model_copy(deep=True),
                               setting=scene.setting, observations=visible,
                               purpose=task.purpose, need_psychology=task.need_psychology)
    if estimate_tokens(context.model_dump()) > budget:
        raise ValueError("角色上下文超出预算；压缩个人记忆或缩短场景，不会静默删掉经历")
    return context


def validate_character_result(result: CharacterResult, context: CharacterContext):
    allowed = {e.id for e in context.observations} | {"profile"}
    for claim in context.memory.summary + context.memory.recent + context.character.initial_memories:
        allowed.update(claim.evidence_ids)
    for claim in result.memories:
        if not claim.evidence_ids or not set(claim.evidence_ids).issubset(allowed):
            raise ValueError("角色记忆必须引用该角色已获得的信息来源")


def merge_memories(memory: Memory, claims: list[Claim], emotion="", goal=""):
    updated = memory.model_copy(deep=True)
    seen = {json.dumps(c.model_dump(), sort_keys=True, ensure_ascii=False)
            for c in updated.summary + updated.recent}
    for claim in claims:
        key = json.dumps(claim.model_dump(), sort_keys=True, ensure_ascii=False)
        if key not in seen:
            updated.recent.append(claim)
            seen.add(key)
    if emotion:
        updated.emotion = emotion
    if goal:
        updated.current_goal = goal
    return updated
