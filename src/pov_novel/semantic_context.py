"""InkOS context compiler and lossless token splitter ported to Python.

AGPL-3.0; original sources and hashes live in vendor/inkos. JSON packets replace
InkOS's Markdown context recipes; protected fragments are never sent to a model
for compression. Character memory uses a separate typed, private compiler.
"""
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field

from .context import estimate_tokens
from .gateway import StructuredOutputError
from .models import BeliefSummary, Claim, CompressedMemory, Identifier, MemoryCompilation, Record
from .store import digest, write_json


def text_tokens(text):
    # Conservative UTF-8 bound, intentionally independent of model tokenizers.
    return len(text.encode('utf-8'))


def semantic_input_budget(context_window, reserved_output_tokens, overhead=4096):
    return max(1, context_window - reserved_output_tokens - overhead)


def split_text_by_estimated_tokens(text, budget):
    if budget <= 0:
        raise ValueError('分块预算必须为正数')
    if text_tokens(text) <= budget:
        return [text]
    chunks, current = [], ''
    for line in text.splitlines(keepends=True):
        if text_tokens(line) > budget:
            if current:
                chunks.append(current)
                current = ''
            offset = 0
            while offset < len(line):
                lo, hi, stop = offset + 1, len(line), offset
                while lo <= hi:
                    mid = (lo + hi) // 2
                    if text_tokens(line[offset:mid]) <= budget:
                        stop, lo = mid, mid + 1
                    else:
                        hi = mid - 1
                if stop == offset:
                    raise ValueError('单个字符超过分块预算')
                chunks.append(line[offset:stop])
                offset = stop
            continue
        if current and text_tokens(current + line) > budget:
            chunks.append(current)
            current = ''
        current += line
    if current:
        chunks.append(current)
    return chunks


@dataclass(frozen=True)
class ContextFragment:
    id: str
    source: str
    content: str
    protection: str = 'compressible'
    priority: int = 0
    relevance: float = 0
    pointer: str = ''


class CompiledHistory(Record):
    content: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1, max_length=400)


class ProtectedContextOverflow(ValueError):
    pass


def render_fragment(fragment):
    return '\n'.join(part for part in (f'## {fragment.source}',
                     f'Source pointer: {fragment.pointer}' if fragment.pointer else '', fragment.content) if part)


def compile_context(fragments, budget, compiler, *, recipe='context', intent=''):
    if budget <= 0:
        raise ValueError('上下文预算必须为正数')
    ids = [item.id for item in fragments]
    if any(not item.strip() for item in ids) or len(ids) != len(set(ids)):
        raise ValueError('上下文来源 ID 为空或重复')
    if any(item.protection not in ('protected', 'compressible') for item in fragments):
        raise ValueError('未知上下文保护级别')
    rank = lambda f: (-f.priority, -f.relevance, f.id)
    protected = sorted((f for f in fragments if f.protection == 'protected'), key=rank)
    history = sorted((f for f in fragments if f.protection == 'compressible'), key=rank)
    size = lambda group: sum(text_tokens(render_fragment(f)) for f in group)
    protected_size, history_size = size(protected), size(history)
    if protected_size > budget:
        raise ProtectedContextOverflow(f'必须保留的当前内容超出预算：{protected_size}/{budget}，请减小正文片段')
    final, compiled_ids, triggered = protected + history, [], False
    if protected_size + history_size > budget:
        wrapper = ContextFragment(f'compiled-{recipe}', f'compiled:{recipe}', '',
                                  pointer=', '.join(f.id for f in history))
        available = budget - protected_size - text_tokens(render_fragment(wrapper)) - 1
        if available <= 0:
            raise ProtectedContextOverflow('没有足够空间保留历史摘要，请减小当前正文片段')
        output = compiler(history, available, intent)
        if not set(output.source_ids).issubset({f.id for f in history}):
            raise ValueError('上下文压缩新增了未知来源 ID')
        compiled_ids = list(dict.fromkeys(output.source_ids))
        final = protected + [ContextFragment(f'compiled-{recipe}', f'compiled:{recipe}',
                                            output.content, pointer=', '.join(compiled_ids))]
        triggered = True
    final_size = size(final)
    if final_size > budget:
        raise ValueError(f'上下文压缩后仍超过预算：{final_size}/{budget}')
    return final, {'recipe_id': recipe, 'budget_tokens': budget, 'protected_tokens': protected_size,
                   'compressible_tokens': history_size, 'final_tokens': final_size,
                   'protected_source_ids': [f.id for f in protected],
                   'compressible_source_ids': [f.id for f in history],
                   'compiled_source_ids': compiled_ids, 'compression_triggered': triggered}


CONTEXT_METHOD = '''只编译本次提供的低优先级上下文。围绕当前任务保留仍相关的精确实体 id、名称、未解状态、因果事实、约束和来源指针，省略无关历史。
这是 InkOS semantic-context-compiler 的 JSON 适配。content 返回带来源的紧凑摘要，source_ids 只能引用给出的来源 ID。
不增加事实，不修改实体身份，不把人物的 belief 变为客观事实。控制 UTF-8 字节数不超过 max_bytes，必要时重新压缩。'''


class ContextManager:
    def __init__(self, gateway, budget=48000, trace_dir=None):
        self.gateway, self.budget = gateway, budget
        self.trace_dir = Path(trace_dir) if trace_dir else None

    def compile_history(self, fragments, budget, intent):
        if any(f.protection != 'compressible' for f in fragments):
            raise ValueError('压缩器只能读取可压缩资料')
        raw = '\n\n'.join(f'Source id: {f.id}\n{render_fragment(f)}' for f in fragments)
        allowed = {f.id for f in fragments}
        source_ids = [f.id for f in fragments]
        input_budget = max(512, self.budget - min(4096, self.budget // 3))
        for pass_id in range(4):
            chunks = split_text_by_estimated_tokens(raw, input_budget)
            outputs = []
            for index, chunk in enumerate(chunks):
                target = max(1, budget // len(chunks) // (1 if pass_id == 0 else 2))
                characters = max(1, target // 4)
                payload = {'intent': intent, 'max_bytes': target, 'max_characters': characters,
                           'output_target': f'content 最多 {characters} 个字符（包括标点），输出极简摘要。来源 ID 单独放 source_ids。',
                           'source_ids': source_ids, 'chunk_index': index, 'source_text': chunk}
                if pass_id:
                    payload['recompression_attempt'] = pass_id - 1
                item = self.gateway.generate('context', CONTEXT_METHOD, payload, CompiledHistory)
                if not set(item.source_ids).issubset(allowed):
                    raise ValueError('压缩器新增未知来源')
                outputs.append(item)
            output = CompiledHistory(content='\n\n'.join(item.content for item in outputs),
                                     source_ids=list(dict.fromkeys(ref for item in outputs for ref in item.source_ids)))
            if text_tokens(output.content) <= budget:
                return output
            # Further passes obey the same input budget; never send the entire
            # accumulated intermediate history in one oversized model call.
            if pass_id and text_tokens(output.content) >= text_tokens(raw):
                raise ValueError('模型未缩短历史资料；原始记录已保留')
            raw, source_ids = output.content, output.source_ids
            allowed = set(source_ids)
        raise ValueError('模型未将历史资料压缩到预算内')

    def packet(self, protected, history, *, recipe, intent=''):
        if not history:
            if estimate_tokens(protected) > self.budget:
                raise ProtectedContextOverflow('当前任务的必要内容超过上下文预算')
            return protected
        fragments = [ContextFragment('current', 'current-task', json.dumps(protected, ensure_ascii=False),
                                     'protected', 100)]
        fragments += [ContextFragment(key, key, json.dumps(value, ensure_ascii=False), pointer=key)
                      for key, value in history.items() if value]
        # Reserve actual JSON envelope overhead, not an arbitrary minimum.
        envelope = {**protected, 'compiled_history': {'content': '', 'source_ids': [f.id for f in fragments[1:]]}}
        reserve = max(0, estimate_tokens(envelope) - estimate_tokens(protected))
        final, trace = compile_context(fragments, self.budget - reserve, self.compile_history,
                                       recipe=recipe, intent=intent)
        packet = {**protected, **history} if not trace['compression_triggered'] else {
            **protected, 'compiled_history': {'content': final[-1].content, 'source_ids': trace['compiled_source_ids']}}
        if estimate_tokens(packet) > self.budget:
            raise ValueError('压缩后的 JSON 上下文仍超过预算')
        if self.trace_dir:
            write_json(self.trace_dir / (digest({'recipe': recipe, 'protected': protected, 'history': history}) + '.json'), trace)
        return packet


class MemorySelectionCandidates(Record):
    """Private recovery parser; never replaces the model's output contract."""
    retained_ids: list[Identifier] = Field(default_factory=list, max_length=1000)
    belief_summaries: list[BeliefSummary] = Field(default_factory=list, max_length=1000)


def compact_private_memory(memory, gateway, *, budget=24000, recent_limit=24):
    keep = min(8, max(1, recent_limit // 2))
    if len(memory.recent) <= recent_limit and estimate_tokens(memory.model_dump()) < budget // 2:
        return memory
    older = memory.summary + memory.recent[:-keep]
    if not older:
        return memory
    instruction = (Path(__file__).parent / 'prompts/memory.md').read_text(encoding='utf-8')
    instruction += '''\n本次输出契约使用 MemoryCompilation：
retained_ids 选择 memory_ids 中的重要原条目，宿主将复制其完整文本、类型、证据和确信程度。
observation 只能选择原条目 ID，禁止输出或改写观察文本；belief 不得升级为 observation。
belief_summaries 仅用于合并主观判断，每项 text 是主观判断，source_memory_ids 只能引用本次 memory_ids。
优先保留仍相关的观察、未解问题、身份和因果，去掉重复或不相关旧条目；合计最多30项。
预计摘要 UTF-8 字节尽量不超过 max_summary_bytes，原始记录另行完整保留。'''
    # Group complete typed claims, never split JSON objects or mix characters.
    input_budget = budget - min(4096, budget // 4)
    groups, group = [], []
    for claim in older:
        candidate = group + [claim]
        if estimate_tokens({'memories': [c.model_dump() for c in candidate]}) > input_budget:
            if not group:
                raise ProtectedContextOverflow('单条个人记忆超过压缩输入预算')
            groups.append(group)
            group = [claim]
            if estimate_tokens({'memories': [claim.model_dump()]}) > input_budget:
                raise ProtectedContextOverflow('单条个人记忆超过压缩输入预算')
        else:
            group = candidate
    if group:
        groups.append(group)

    def compress(items, pass_id):
        allowed = {ref for c in items for ref in c.evidence_ids}
        observed = {}
        for claim in items:
            if claim.kind == 'observation':
                observed.setdefault(claim.text, set()).update(claim.evidence_ids)
        indexed = {f'memory-{index}': claim for index, claim in enumerate(items)}
        feedback = ''
        last_format_error = None
        semantic_failure = False
        for attempt in range(3):
            overflow = False
            try:
                output = gateway.generate('memory', instruction, {
                    'memories': [c.model_dump() for c in items], 'pass_id': pass_id,
                    'memory_ids': list(indexed), 'validation_feedback': feedback,
                    'validation_attempt': attempt, 'max_summary_bytes': max(512, budget // 3)}, MemoryCompilation)
            except StructuredOutputError as exc:
                last_format_error = exc
                if hasattr(gateway, 'directory'):
                    write_json(gateway.directory / 'memory_failures' / (digest({'items': [c.model_dump() for c in items], 'pass': pass_id})[:20] + f'-attempt{attempt}.json'),
                               {'error': str(exc), 'issues': exc.issues, 'raw_output': exc.raw_output})
                try:
                    candidates = (MemorySelectionCandidates.model_validate_json(exc.raw_output)
                        if isinstance(exc.raw_output, str) else MemorySelectionCandidates.model_validate(exc.raw_output))
                except (ValueError, TypeError):
                    feedback = str(exc)[:400]
                    continue
                if len(candidates.retained_ids) + len(candidates.belief_summaries) <= 30:
                    feedback = str(exc)[:400]
                    continue
                # Oversize selections are recoverable only after every candidate
                # passes the same identity/source/type checks as normal output.
                output, overflow = candidates, True
            if isinstance(output, (MemoryCompilation, MemorySelectionCandidates)):
                if len(output.retained_ids) != len(set(output.retained_ids)) or not set(output.retained_ids).issubset(indexed):
                    semantic_failure = True
                    feedback = 'retained_ids 必须是本次 memory_ids 中不重复的 ID。'
                    continue
                claims = [indexed[mid].model_copy(deep=True) for mid in output.retained_ids]
                invalid = False
                for belief in output.belief_summaries:
                    if not set(belief.source_memory_ids).issubset(indexed):
                        invalid = True
                        break
                    evidence = list(dict.fromkeys(ref for mid in belief.source_memory_ids for ref in indexed[mid].evidence_ids))
                    if len(evidence) > 20:
                        invalid = True
                        break
                    # Model never controls epistemic type or source IDs here.
                    claims.append(Claim(text=belief.text, kind='belief', evidence_ids=evidence, confidence=belief.confidence))
                if invalid:
                    semantic_failure = True
                    feedback = 'belief_summaries.source_memory_ids 只能引用本次 memory_ids；合并后证据最多20个，过多时请减少合并或保留原条目。'
                    continue
                if overflow:
                    if not all(c.evidence_ids and set(c.evidence_ids).issubset(allowed)
                        and (c.kind != 'observation' or (c.text in observed and set(c.evidence_ids).issubset(observed[c.text]))) for c in claims):
                        semantic_failure = True
                        feedback = '所有候选均须保留已知来源；观察只能复制原条目。'
                        continue
                    if hasattr(gateway, 'directory'):
                        write_json(gateway.directory / 'memory_budget_repairs' / (digest({'items': [c.model_dump() for c in items], 'pass': pass_id})[:20] + f'-attempt{attempt}.json'),
                            {'schema_version': 2, 'reason': 'summary_item_budget', 'candidates': output.model_dump(),
                             'input_memory_ids': list(indexed), 'input_memories': [c.model_dump() for c in items],
                             'candidate_count': len(claims), 'kept_count': 30,
                             'selection': 'retained_ids followed by belief_summaries, first 30; all candidates validated'})
                output = CompressedMemory(summary=claims[:30])
            if output.summary and all(c.evidence_ids and set(c.evidence_ids).issubset(allowed)
                   and (c.kind != 'observation' or (c.text in observed and set(c.evidence_ids).issubset(observed[c.text]))) for c in output.summary):
                return output.summary
            feedback = '摘要不能完全清空、增加未知来源、改写观察或升级推测。观察只能通过 retained_ids 复制；推测只能在 belief_summaries 中合并。'
            semantic_failure = True
        if last_format_error is not None and not semantic_failure:
            raise last_format_error
        raise ValueError('个人记忆压缩新增来源、改写见闻或把推测升级为事实')
    summaries = [c for index, items in enumerate(groups) for c in compress(items, index)]
    rounds = 0
    while len(summaries) > 30:
        reduced = []
        merge_groups, items = [], []
        for claim in summaries:
            candidate = items + [claim]
            if items and (len(candidate) > 60 or estimate_tokens({'memories': [c.model_dump() for c in candidate]}) > input_budget):
                merge_groups.append(items)
                items = []
            items.append(claim)
        if items:
            merge_groups.append(items)
        for offset, items in enumerate(merge_groups):
            reduced.extend(compress(items, f'merge{rounds}-{offset}'))
        if len(reduced) >= len(summaries):
            raise ValueError('模型未缩减个人记忆条目，已保留原始记录')
        summaries, rounds = reduced, rounds + 1
    return memory.model_copy(update={'summary': summaries, 'recent': memory.recent[-keep:]})
