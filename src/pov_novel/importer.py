"""InkOS import guidance adapted to isolated character cards and JSON files.

Original guidance and splitter: vendor/inkos, AGPL-3.0. The host checks exact
source quotes; semantic attribution is a model task and remains reviewable.
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from .chapter_splitter import natural_key, split_chapters
from .context import estimate_tokens
from .gateway import CachedGateway, ModelError, OutputLimitError, StructuredOutputError
from .models import CharacterCard, Claim, Memory, Record, StoryConfig, Text
from .store import FileStore, digest, read_json, write_bytes, write_json, write_text
from .story import write_story, story_files
from .semantic_context import (ContextManager, ProtectedContextOverflow, compact_private_memory,
                               semantic_input_budget, split_text_by_estimated_tokens)

Short = Annotated[str, Field(min_length=1, max_length=120)]


class SupportedFact(Record):
    text: Annotated[str, Field(min_length=1, max_length=1000)]
    quote: Annotated[str, Field(min_length=1, max_length=500)]
    confidence: Literal['low', 'medium', 'high'] | None = None


class SupportedMemory(SupportedFact):
    kind: Literal['observation', 'belief']
    confidence: Literal['low', 'medium', 'high'] = 'medium'


class ExtractedCharacter(Record):
    name: Short
    aliases: list[Short] = Field(default_factory=list, max_length=10)
    personality: SupportedFact | None = None
    voice: SupportedFact | None = None
    current_goal: SupportedFact | None = None
    secrets: list[SupportedFact] = Field(default_factory=list, max_length=10)
    memories: list[SupportedMemory] = Field(default_factory=list, max_length=20)
    uncertainties: list[Text] = Field(default_factory=list, max_length=20)


class SourceAnalysis(Record):
    outline: Text
    style: Text
    characters: list[ExtractedCharacter] = Field(default_factory=list, max_length=30)
    world_rules: list[SupportedFact] = Field(default_factory=list, max_length=20)
    relationships: list[SupportedFact] = Field(default_factory=list, max_length=20)
    timeline: list[SupportedFact] = Field(default_factory=list, max_length=20)
    unresolved_threads: list[SupportedFact] = Field(default_factory=list, max_length=20)
    uncertainties: list[str] = Field(default_factory=list, max_length=20)
    contradictions: list[str] = Field(default_factory=list, max_length=20)


class CharacterAliasCandidates(ExtractedCharacter):
    # Recovery parsing only. The public output schema remains unchanged.
    aliases: list[Short] = Field(default_factory=list, max_length=1000)


class SourceAliasCandidates(SourceAnalysis):
    characters: list[CharacterAliasCandidates] = Field(default_factory=list, max_length=30)


def import_instruction():
    here = Path(__file__).parent
    return '\n\n'.join((here / name).read_text(encoding='utf-8') for name in (
        'vendor/inkos/story-import.md', 'vendor/inkos/reconstruction-rubric.md', 'prompts/import.md'))


def read_sources(source, encoding='utf-8-sig'):
    source = Path(source).resolve()
    if not source.exists():
        raise ValueError('找不到小说正文文件或目录')
    files = sorted((p for p in source.iterdir() if p.is_file() and p.suffix.lower() in ('.txt', '.md')),
                   key=lambda p: natural_key(p.name)) if source.is_dir() else [source]
    if not files or any(p.suffix.lower() not in ('.txt', '.md') for p in files):
        raise ValueError('正文只支持 TXT / Markdown 文件或按章保存的目录')
    sources, chapters = [], []
    for file in files:
        raw = file.read_bytes()
        try:
            text = raw.decode(encoding)
        except (UnicodeError, LookupError) as exc:
            raise ValueError(f'无法按 {encoding} 解码 {file.name}，请指定正确的 --encoding') from exc
        found = split_chapters(text, fallback=file.stem)
        if not found or any(not chapter.content.strip() for chapter in found):
            raise ValueError(f'导入文件存在空正文或空章节：{file.name}')
        sources.append({'name': file.name, 'text': text, 'bytes': raw, 'checksum': hashlib.sha256(raw).hexdigest()})
        chapters.extend(found)
    return sources, chapters


def source_chunks(text, limit):
    # Preserve every character, splitting preferentially at a paragraph boundary.
    offset = 0
    while offset < len(text):
        stop = min(len(text), offset + limit)
        if stop < len(text):
            line_end = text.rfind('\n', offset + limit // 2, stop)
            if line_end >= 0:
                stop = line_end + 1
        yield offset, text[offset:stop]
        offset = stop



def quote_layout_index(text):
    """Ignore presentation whitespace, but retain separators inside ASCII words."""
    characters, positions, offset = [], [], 0
    while offset < len(text):
        char = text[offset]
        if char.isspace():
            end = offset + 1
            while end < len(text) and text[end].isspace():
                end += 1
            previous = text[offset - 1] if offset else ''
            following = text[end] if end < len(text) else ''
            if previous.isascii() and previous.isalnum() and following.isascii() and following.isalnum():
                characters.append(' ')
                positions.append(offset)
            offset = end
        else:
            characters.append(char)
            positions.append(offset)
            offset += 1
    return ''.join(characters), positions


def restore_quote_layout(analysis, source):
    """Recover an exact source slice for a layout-only quote difference.

    No interior punctuation, wording, omissions, or ASCII word boundaries are
    repaired. A paired quotation wrapper added around a verbatim excerpt can
    be omitted only when its entire interior matches one continuous source slice.
    The returned evidence still has to pass the normal exact-substring check.
    """
    normalized, positions = quote_layout_index(source)
    restored = analysis.model_copy(deep=True)
    facts = [fact for field in ('world_rules', 'relationships', 'timeline', 'unresolved_threads')
             for fact in getattr(restored, field)]
    for actor in restored.characters:
        facts.extend(fact for fact in (actor.personality, actor.voice, actor.current_goal) if fact)
        facts.extend(actor.secrets)
        facts.extend(actor.memories)
    repairs = []
    for fact in facts:
        if fact.quote in source:
            continue
        needle, _ = quote_layout_index(fact.quote)
        start = normalized.find(needle) if needle else -1
        if start < 0 and len(fact.quote) > 2:
            wrappers = {('“', '”'), ('‘', '’'), ('「', '」'), ('『', '』'), ('"', '"'), ("'", "'")}
            if (fact.quote[0], fact.quote[-1]) in wrappers:
                needle, _ = quote_layout_index(fact.quote[1:-1])
                start = normalized.find(needle) if needle else -1
        if start < 0:
            continue
        end = positions[start + len(needle) - 1] + 1
        exact = source[positions[start]:end]
        if len(exact) > 500:
            continue
        repairs.append({'before': fact.quote, 'after': exact,
                        'source_start': positions[start], 'source_end': end})
        fact.quote = exact
    return restored, repairs


class EvidenceValidationError(ValueError):
    def __init__(self, message, issues):
        self.issues = issues
        paths = ', '.join('.'.join(str(part) for part in issue['path']) for issue in issues[:20])
        super().__init__(message + ('：' + paths if paths else ''))


NON_IDENTIFYING_NAMES = frozenset({
    '他', '她', '它', '他们', '她们', '它们', '我', '你', '您', '你们', '我们', '咱', '咱们',
    '自己', '本人', '人家', '这人', '那人', '对方', '此人', '那位', '这位', '那个', '这个',
    '臣妾', '朕', '寡人', '本王', '小人', '奴婢', '奴才', '陛下', '殿下', '小姐', '公子',
    '大人', '姑娘', '夫人', '皇上', '圣上', '皇帝', '王爷', '将军', '丞相', '太后',
    '母后', '父皇', '皇子', '公主', '太子', '丫鬟', '侍女', '宫女', '下人', '仆人', '先生'})


def identity_names(name, aliases, *, legacy=False):
    return {value for value in [name, *aliases] if value and (legacy or value not in NON_IDENTIFYING_NAMES)}


def character_matches(character, known, *, legacy=False):
    if legacy or character.name not in NON_IDENTIFYING_NAMES:
        canonical = [cid for cid, entry in known.items() if entry['card']['name'] == character.name]
        if canonical:
            return canonical
    names = identity_names(character.name, character.aliases, legacy=legacy)
    return [cid for cid, entry in known.items()
            if names.intersection(identity_names(entry['card']['name'], entry['aliases'], legacy=legacy))]


def add_uncertainty(analysis, note):
    if len(analysis.uncertainties) < 20:
        analysis.uncertainties.append(note)
    else:
        analysis.uncertainties[-1] += '\n' + note


def verify_analysis_quotes(analysis, text):
    facts = [( [field, index, 'quote'], fact) for field in ('world_rules', 'relationships', 'timeline', 'unresolved_threads')
             for index, fact in enumerate(getattr(analysis, field))]
    for index, character in enumerate(analysis.characters):
        facts.extend((['characters', index, field, 'quote'], item) for field in ('personality', 'voice', 'current_goal')
                     if (item := getattr(character, field)) is not None)
        facts.extend((['characters', index, 'secrets', offset, 'quote'], item) for offset, item in enumerate(character.secrets))
        facts.extend((['characters', index, 'memories', offset, 'quote'], item) for offset, item in enumerate(character.memories))
    invalid = [{'path': path, 'type': 'quote_not_in_current_source'} for path, fact in facts if not fact.quote.strip() or fact.quote not in text]
    if invalid:
        raise EvidenceValidationError('提取证据不是当前片段中的连续原句', invalid)


def verify_analysis(analysis, text, known, *, identity_contract_version=2):
    # Quote validity is checked even for an update whose identity is uncertain.
    # Deferring attribution must never become a way to accept fabricated quotes.
    verify_analysis_quotes(analysis, text)
    missing = []
    for index, character in enumerate(analysis.characters):
        legacy = identity_contract_version < 2
        names = identity_names(character.name, character.aliases, legacy=legacy)
        matches = character_matches(character, known, legacy=legacy)
        if len(matches) > 1:
            missing.append({'path': ['characters', index, 'name'], 'type': 'ambiguous_identity'})
            continue
        if matches:
            # The model may return the canonical name without repeating an
            # already registered alias that is actually present in this source.
            entry = known[matches[0]]
            names.update(identity_names(entry['card']['name'], entry['aliases'], legacy=legacy))
        if not any(name in text for name in names):
            missing.append({'path': ['characters', index, 'name'], 'type': 'missing_name_evidence'})
    if missing:
        raise EvidenceValidationError('人物在当前片段中缺少可唯一归属的姓名或别称，不能凭空更新', missing)


def split_import_text(text, budget):
    """Keep lossless byte-bounded splitting without a tiny final fragment."""
    chunks = split_text_by_estimated_tokens(text, budget)
    if len(chunks) < 2 or len(chunks[-1]) >= min(500, len(text) // 4):
        return chunks
    tail = chunks[-2] + chunks[-1]
    total = len(tail.encode('utf-8'))
    candidates, size = [], 0
    for offset, char in enumerate(tail[:-1], 1):
        size += len(char.encode('utf-8'))
        if max(total - budget, total // 3) <= size <= min(budget, total * 2 // 3):
            candidates.append((offset, size, char == '\n'))
    if candidates:
        boundaries = [item for item in candidates if item[2]] or candidates
        stop = min(boundaries, key=lambda item: abs(item[1] * 2 - total))[0]
        chunks[-2:] = [tail[:stop], tail[stop:]]
    return chunks


class NovelImporter:
    def __init__(self, store: FileStore, gateway, *, chunk_size=6000, context_budget=60000):
        if context_budget < 2000:
            raise ValueError('context-budget 必须至少为 2000')
        if not 500 <= chunk_size <= 12000:
            raise ValueError('chunk-size 必须在 500 到 12000 字符之间')
        self.store = store
        self.gateway = CachedGateway(gateway, store.path('.runtime', 'import', 'model_cache'))
        self.mock = gateway.__class__.__module__ == 'pov_novel.mock'
        self.chunk_size = chunk_size
        self.context_budget = context_budget
        self.instruction = import_instruction()
        available = semantic_input_budget(getattr(gateway, 'context_window', 131072),
                                          getattr(gateway, 'output_ceiling', 24000), overhead=8192)
        self.context_manager = ContextManager(self.gateway, min(context_budget, available),
            store.path('.runtime', 'import', 'context_traces'))
        self.resume_identity = getattr(gateway, 'resume_identity', {'gateway': gateway.identity})

    def compact(self, memory):
        return compact_private_memory(memory, self.gateway, budget=self.context_manager.budget)

    def merge(self, state, analysis, source_id):
        registry = state['characters']
        for character in analysis.characters:
            names = identity_names(character.name, character.aliases)
            matches = character_matches(character, registry)
            if len(matches) > 1:
                raise ValueError('人物别称同时匹配多张卡片，请检查原文身份或提取结果')
            if matches:
                cid = matches[0]
            else:
                cid = f'char_{len(registry) + 1:04d}'
                card = CharacterCard(id=cid, name=character.name, personality='正文未明确，待作者补充')
                registry[cid] = {'card': card.model_dump(), 'aliases': [], 'memory': Memory().model_dump()}
            entry = registry[cid]
            allowed = set()
            for name in names - {entry['card']['name']}:
                owners = [other for other, value in registry.items() if other != cid
                          and name in identity_names(value['card']['name'], value['aliases'])]
                if owners:
                    add_uncertainty(analysis, f'别称身份待确认：{character.name} 的别称 {name} 另有卡片 {", ".join(owners)}；未跨卡登记或合并。')
                else:
                    allowed.add(name)
            entry['aliases'] = sorted(set(entry['aliases']) | allowed)
            for field in ('personality', 'voice', 'current_goal'):
                fact = getattr(character, field)
                if fact is not None:
                    entry['card'][field] = fact.text
            secrets = entry['card']['known_secrets']
            for fact in character.secrets:
                if fact.text not in secrets:
                    secrets.append(fact.text)
            memory = Memory.model_validate(entry['memory'])
            for item in character.memories:
                claim = Claim(text=item.text, kind=item.kind, evidence_ids=[source_id], confidence=item.confidence)
                if claim not in memory.recent and claim not in memory.summary:
                    memory.recent.append(claim)
            memory.current_goal = entry['card']['current_goal']
            entry['memory'] = self.compact(memory).model_dump()
        state['outline'], state['style'] = analysis.outline, analysis.style

    def make_parts(self, chapters, consumed=None, prefix=None):
        consumed, prefix = consumed or {}, prefix or []
        parts = list(prefix)
        existing_ids = {p['source_id'] for p in prefix}
        for index, chapter in enumerate(chapters, 1):
            start = consumed.get(index, 0)
            for part, (offset, text) in enumerate(source_chunks(chapter.raw[start:], self.chunk_size), 1):
                source_id = f'import{index:04d}_part{part:04d}'
                if source_id in existing_ids or start:
                    source_id = f'import{index:04d}_resume{len(parts) + 1:04d}'
                parts.append({'source_id': source_id, 'chapter': index, 'title': chapter.title,
                              'offset': start + offset, 'source_text': text})
        return parts

    def reconstruct_plan(self, chapters, checkpoint, runtime):
        count = checkpoint.get('next_part', 0)
        plan_file = runtime / 'plan.json'
        if plan_file.exists():
            old_parts = read_json(plan_file)
        else:
            traces = [read_json(path)['source'] for path in (runtime / 'traces').glob('*.json')]
            old_parts = sorted(traces, key=lambda p: (p['chapter'], p['offset']))
        if len(old_parts) < count:
            raise ValueError('已完成片段缺少来源记录，不能安全恢复；旧结果已保留')
        prefix, consumed = [], {}
        for part in old_parts[:count]:
            chapter_index, offset, text = part['chapter'], part['offset'], part['source_text']
            if chapter_index > len(chapters) or offset != consumed.get(chapter_index, 0):
                break
            current = chapters[chapter_index - 1]
            if current.raw[offset:offset + len(text)] != text or current.title != part['title']:
                break
            if any(consumed.get(i, 0) != len(chapters[i - 1].raw) for i in range(1, chapter_index)):
                break
            # Check that stored evidence still matches the exact source slice.
            trace = read_json(runtime / 'traces' / (part['source_id'] + '.json'))
            analysis = SourceAnalysis.model_validate(trace['analysis'])
            verify_analysis(analysis, text, checkpoint.get('characters', {}),
                            identity_contract_version=trace.get('identity_contract_version', 1))
            prefix.append(part)
            consumed[chapter_index] = offset + len(text)
        kept = len(prefix)
        if kept == count:
            state = checkpoint
        elif kept == 0:
            state = {'next_part': 0, 'characters': {}, 'outline': '', 'style': ''}
        else:
            saved = runtime / 'checkpoints' / (prefix[-1]['source_id'] + '.json')
            if saved.exists():
                state = read_json(saved)
            else:
                # Legacy imports only had one checkpoint. Replay preserved,
                # grounded facts; cached private compression calls are reusable.
                state = {'next_part': 0, 'characters': {}, 'outline': '', 'style': ''}
                for part in prefix:
                    analysis = SourceAnalysis.model_validate(read_json(runtime / 'traces' / (part['source_id'] + '.json'))['analysis'])
                    self.merge(state, analysis, part['source_id'])
                    state['next_part'] += 1
        return self.make_parts(chapters, consumed, prefix), state, count - kept

    def extract_part(self, part, state):
        identities = [{'id': cid, 'name': entry['card']['name'], 'aliases': [alias for alias in entry['aliases'] if alias not in NON_IDENTIFYING_NAMES]}
                      for cid, entry in state['characters'].items()]
        # Full identity catalog is protected. Profiles for currently mentioned
        # characters are compressible; irrelevant profiles remain on disk.
        profiles = {cid: {field: entry['card'][field] for field in ('personality', 'voice', 'current_goal')}
                    for cid, entry in state['characters'].items()
                    if any(name in part['source_text'] for name in identity_names(entry['card']['name'], entry['aliases']))}
        protected = {**part, 'known_characters': identities}
        history = {'previous_outline': state['outline'], 'previous_style': state['style'], 'prior_profiles': profiles}
        packet = self.context_manager.packet(protected, history, recipe='novel-import',
                                             intent='根据当前原文更新人物状态、已发生剧情与未解决信息差')
        instruction = self.instruction + '\n上游指南仅提供提取方法；本任务输出格式严格以最后附加的 JSON Schema 为准，不生成 Markdown、STATE_UPDATE 或额外字段。\n历史资料可能由 compiled_history 提供，其中的 source_ids 可追溯。只更新当前 source_text 有证据的人物，不重复枚举此前未出场角色。保持大纲简洁，避免逐句复述；每个人物只提取关键见闻和私密经历，证据原句尽量短。\naliases 只使用可区分人物的姓名、具体别称；他、她、我、臣妾等代词、自称，以及陛下、小姐等泛称不能作为全书唯一身份。优先沿用已知明确姓名，无法确定人物时记录 uncertainties，不猜测身份。'
        feedback, invalid_output = '', None
        for attempt in range(3):
            analysis = None
            payload = {**packet, 'validation_attempt': attempt, 'validation_feedback': feedback}
            if invalid_output is not None:
                payload['invalid_output'] = invalid_output
                payload['repair_mode'] = True
                if estimate_tokens(payload) > self.context_manager.budget:
                    # Retain complete failed output on disk. Source evidence
                    # takes priority over the optional repair example.
                    payload.pop('invalid_output')
                payload['validation_feedback'] += ('逐项修复前次格式错误；输出完整 JSON。'
                    'personality/voice/current_goal 必须为 {"text":"描述","quote":"当前原文逐字摘录"} 或 null，'
                    '不能直接复制 prior_profiles 的描述字符串。quote 不得编造。'
                    '无法找到证据时记录 uncertainties 并使用 null。confidence 只能为 low/medium/high 或 null。')
            try:
                try:
                    analysis = self.gateway.generate('importer', instruction, payload, SourceAnalysis)
                except StructuredOutputError as exc:
                    analysis = self.restore_registered_aliases(exc.raw_output, part, state)
                    if analysis is None:
                        raise
                analysis = self.normalize_aliases(analysis, part, state)
                analysis, repairs = restore_quote_layout(analysis, part['source_text'])
                verify_analysis(analysis, part['source_text'], state['characters'])
                if repairs:
                    write_json(self.store.path('.runtime', 'import', 'quote_repairs', part['source_id'] + '.json'),
                               {'source_id': part['source_id'], 'repairs': repairs})
                return analysis
            except (ValueError, StructuredOutputError) as exc:
                feedback = str(exc) + "；严格遵守字段类型、长度和列表条数上限。"
                if isinstance(exc, EvidenceValidationError) and all(issue['type'] == 'missing_name_evidence' for issue in exc.issues):
                    feedback += ('缺少姓名依据的人物必须从 characters 移除，将身份不确定性写入 uncertainties；'
                                 '若片段只有代词，characters 可以为 []。不得把历史中的姓名或泛称当作当前原文证据。')
                invalid_output = getattr(exc, 'raw_output', None)
                if invalid_output is None and analysis is not None:
                    invalid_output = analysis.model_dump()
                if invalid_output is not None:
                    write_json(self.store.path('.runtime', 'import', 'format_failures',
                        part['source_id'] + f'-attempt{attempt + 1}.json'),
                        {'source_id': part['source_id'], 'validation_error': str(exc),
                         'issues': getattr(exc, 'issues', []), 'output': invalid_output})
                if attempt == 2:
                    if (analysis is not None and isinstance(exc, EvidenceValidationError)
                        and exc.issues and all(issue['type'] in ('missing_name_evidence', 'ambiguous_identity') for issue in exc.issues)):
                        return self.defer_identity_updates(analysis, part, state, exc.issues, repairs)
                    raise

    def normalize_aliases(self, analysis, part, state):
        normalized = analysis.model_copy(deep=True)
        changes = []
        for actor in normalized.characters:
            removed, aliases = [], []
            matches = character_matches(actor, state['characters'])
            for alias in actor.aliases:
                if alias in NON_IDENTIFYING_NAMES:
                    removed.append({'alias': alias, 'reason': 'non_identifying_address'})
                    continue
                owners = [cid for cid, entry in state['characters'].items()
                          if alias in identity_names(entry['card']['name'], entry['aliases'])]
                if len(matches) == 1 and any(cid != matches[0] for cid in owners):
                    removed.append({'alias': alias, 'reason': 'other_character', 'owners': owners})
                    add_uncertainty(normalized, f'别称身份待确认：{actor.name} 的别称 {alias} 另有卡片；未跨卡登记或合并。')
                else:
                    aliases.append(alias)
            actor.aliases = list(dict.fromkeys(aliases))
            if removed:
                changes.append({'name': actor.name, 'removed': removed})
        if changes:
            write_json(self.store.path('.runtime', 'import', 'alias_normalizations', part['source_id'] + '.json'),
                       {'schema_version': 2, 'source_id': part['source_id'], 'changes': changes})
        return normalized

    def restore_registered_aliases(self, raw, part, state):
        """Emit bounded alias deltas instead of repeating the full registry."""
        try:
            candidate = (SourceAliasCandidates.model_validate_json(raw) if isinstance(raw, str)
                         else SourceAliasCandidates.model_validate(raw))
        except (ValueError, TypeError):
            return None
        repaired = candidate.model_dump()
        changes = []
        for index, actor in enumerate(candidate.characters):
            if len(actor.aliases) <= 10:
                continue
            names = identity_names(actor.name, actor.aliases)
            ids = character_matches(actor, state['characters'])
            matches = [state['characters'][cid] for cid in ids]
            # Never hide an identity conflict or silently drop new aliases.
            if len(matches) != 1:
                return None
            for name in names - {actor.name}:
                owners = [cid for cid, entry in state['characters'].items()
                          if name in identity_names(entry['card']['name'], entry['aliases'])]
                if any(cid != ids[0] for cid in owners):
                    return None
            registered = {matches[0]['card']['name'], *matches[0]['aliases']}
            aliases = list(dict.fromkeys(alias for alias in actor.aliases if alias not in registered and alias not in NON_IDENTIFYING_NAMES))
            if actor.name not in registered:
                binding = [alias for alias in actor.aliases if alias in registered]
                preferred = matches[0]['card']['name']
                aliases.insert(0, preferred if preferred in binding else binding[0])
            if len(aliases) > 10:
                return None
            repaired['characters'][index]['aliases'] = aliases
            changes.append({'name': actor.name, 'input_count': len(actor.aliases), 'delta_count': len(aliases),
                            'omitted_registered_aliases': [alias for alias in actor.aliases if alias not in aliases and alias in registered],
                            'ignored_non_identifying_aliases': [alias for alias in actor.aliases if alias in NON_IDENTIFYING_NAMES]})
        if not changes:
            return None
        normalized = SourceAnalysis.model_validate(repaired)
        write_json(self.store.path('.runtime', 'import', 'alias_budget_repairs', part['source_id'] + '.json'),
            {'schema_version': 2, 'source_id': part['source_id'], 'status': 'proposed',
             'reason': 'registered_aliases_are_not_new_updates', 'changes': changes,
             'original_candidates': candidate.model_dump()})
        return normalized

    def defer_identity_updates(self, analysis, part, state, issues, repairs):
        """Preserve unassignable candidates for the author, never as memories."""
        verify_analysis_quotes(analysis, part['source_text'])
        indices = {issue['path'][1] for issue in issues}
        deferred = [actor.model_dump() for index, actor in enumerate(analysis.characters) if index in indices]
        retained = analysis.model_copy(deep=True)
        retained.characters = [actor for index, actor in enumerate(retained.characters) if index not in indices]
        record = '.runtime/import/identity_uncertainties/' + part['source_id'] + '.json'
        note = ('人物身份待确认：' + '、'.join(actor['name'] for actor in deferred)
                + '。当前片段没有可唯一归属的姓名或已知别称，候选更新未写入人物卡或个人记忆；完整候选保存在 ' + record)
        add_uncertainty(retained, note)
        verify_analysis(retained, part['source_text'], state['characters'])
        write_json(self.store.path(record), {'schema_version': 2, 'source_id': part['source_id'],
            'source': part, 'reason': 'identity_uncertain', 'issues': issues, 'candidates': deferred, 'attempts': 3})
        if repairs:
            write_json(self.store.path('.runtime', 'import', 'quote_repairs', part['source_id'] + '.json'),
                       {'source_id': part['source_id'], 'repairs': repairs})
        return retained

    def run(self, source, *, title=None, encoding='utf-8-sig', progress=None, max_parts=None):
        if max_parts is not None and max_parts <= 0:
            raise ValueError('max-parts 必须为正数')
        source = Path(source).resolve()
        if source.is_relative_to(self.store.root) or (source.is_dir() and self.store.root.is_relative_to(source)):
            raise ValueError('正文来源目录与导入项目目录不能互相包含')
        sources, chapters = read_sources(source, encoding)
        title = title or source.stem
        if not title.strip() or len(title) > 16000:
            raise ValueError('小说标题为空或过长')
        semantics = {'title': title, 'encoding': encoding, 'schema': digest(SourceAnalysis.model_json_schema()),
                     'gateway': self.resume_identity}
        request_hash = digest({'files': [{'name': item['name'], 'checksum': item['checksum']} for item in sources],
                               'semantics': semantics})
        runtime = self.store.path('.runtime', 'import')
        binding_path, checkpoint_path, plan_path = (runtime / name for name in ('binding.json', 'checkpoint.json', 'plan.json'))
        accepted_path = self.store.path('scenes', 'imported_source', 'accepted.json')
        with self.store.lock():
            binding = read_json(binding_path) if binding_path.exists() else None
            if not binding and (self.store.path('project.json').exists() or self.store.path('story.json').exists() or self.store.accepted()):
                raise ValueError('小说项目已存在，导入需要新的项目目录，未覆盖原设定')
            if accepted_path.exists():
                report = read_json(self.store.path('import-report.json'))
                unchanged = report['source_files'] == [{'name': item['name'], 'sha256': item['checksum']} for item in sources]
                if not unchanged or report['title'] != title:
                    raise ValueError('已完成导入的原文改变，请使用新项目目录；原结果和后续正文已保留')
                self.store.materialize()
                self.store.status('imported_source', phase='completed', total_parts=report['part_count'])
                return {**report, 'cached': True}
            if self.store.accepted():
                raise ValueError('导入项目中出现其他已接受场景，不能覆盖')
            if binding and binding.get('version') == 2 and binding['semantics']['gateway'] != semantics['gateway']:
                raise ValueError('模型服务或模型名称改变；已保存进度，恢复原模型可继续')
            reset_path = runtime / 'pending_reset.json'
            if reset_path.exists():
                reset = read_json(reset_path)
                write_json(plan_path, reset['parts'])
                write_json(checkpoint_path, reset['state'])
                write_json(binding_path, reset['binding'])
                reset_path.unlink()
                binding = read_json(binding_path)
            state = read_json(checkpoint_path) if checkpoint_path.exists() else {
                'next_part': 0, 'characters': {}, 'outline': '', 'style': ''}
            changed = binding and binding.get('request_hash') != request_hash
            if changed or (binding and binding.get('version') != 2):
                parts, state, rewound = self.reconstruct_plan(chapters, state, runtime)
                archive = runtime / 'binding_history' / (digest(binding) + '.json')
                write_json(archive, {'binding': binding, 'checkpoint': read_json(checkpoint_path) if checkpoint_path.exists() else None,
                                     'reused_parts': state['next_part'], 'rewound_parts': rewound})
                new_binding = {'schema_version': 2, 'version': 2, 'request_hash': request_hash, 'semantics': semantics,
                               'chunk_size': self.chunk_size, 'context_budget': self.context_manager.budget}
                write_json(reset_path, {'parts': parts, 'state': state, 'binding': new_binding})
                write_json(plan_path, parts)
                write_json(checkpoint_path, state)
                write_json(binding_path, new_binding)
                reset_path.unlink()
            elif plan_path.exists():
                parts = read_json(plan_path)
            else:
                parts = self.make_parts(chapters)
                write_json(plan_path, parts)
            write_json(binding_path, {'schema_version': 2, 'version': 2, 'request_hash': request_hash, 'semantics': semantics,
                                      'chunk_size': self.chunk_size, 'context_budget': self.context_manager.budget})
            for index, item in enumerate(sources, 1):
                target = runtime / 'originals' / f'{index:04d}{Path(item["name"]).suffix.lower()}'
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() and target.read_bytes() != item['bytes']:
                    old = target.read_bytes()
                    archive = runtime / 'originals_history' / (hashlib.sha256(old).hexdigest() + target.suffix)
                    archive.parent.mkdir(parents=True, exist_ok=True)
                    write_bytes(archive, old)
                if not target.exists() or target.read_bytes() != item['bytes']:
                    write_bytes(target, item['bytes'])
            for index, chapter in enumerate(chapters, 1):
                write_text(self.store.path('imports', 'chapters', f'{index:04d}.md'), chapter.raw)
            for orphan in self.store.path('imports', 'chapters').glob('*.md'):
                if orphan.stem.isascii() and orphan.stem.isdigit() and int(orphan.stem) > len(chapters):
                    previous = orphan.read_bytes()
                    archive = runtime / 'chapters_history' / hashlib.sha256(previous).hexdigest() / orphan.name
                    archive.parent.mkdir(parents=True, exist_ok=True)
                    write_bytes(archive, previous)
                    orphan.unlink()
            batch_done = 0
            try:
                while state['next_part'] < len(parts):
                    index = state['next_part']
                    if max_parts is not None and batch_done >= max_parts:
                        self.store.status('imported_source', phase='importing', completed_parts=index, total_parts=len(parts))
                        return {'project': str(self.store.root), 'phase': 'in_progress', 'completed_parts': index,
                                'total_parts': len(parts), 'next_part': index + 1, 'cached': False}
                    if progress:
                        progress(index + 1, len(parts))
                    part = parts[index]
                    self.store.status('imported_source', phase='importing', part=index + 1, total_parts=len(parts))
                    try:
                        analysis = self.extract_part(part, state)
                    except (OutputLimitError, ProtectedContextOverflow, StructuredOutputError, EvidenceValidationError):
                        if len(part['source_text']) <= 500:
                            raise
                        text = part['source_text']
                        budget = max(1, len(text.encode('utf-8')) // 2)
                        children, consumed = [], 0
                        for child_index, chunk in enumerate(split_import_text(text, budget), 1):
                            children.append({**part, 'source_id': part['source_id'] + f'_s{child_index}',
                                             'offset': part['offset'] + consumed, 'source_text': chunk})
                            consumed += len(chunk)
                        parts[index:index + 1] = children
                        write_json(plan_path, parts)
                        continue
                    self.merge(state, analysis, part['source_id'])
                    state['schema_version'] = 2
                    state['next_part'] = index + 1
                    self.store.commit_files({
                        str((runtime / 'traces' / (part['source_id'] + '.json')).relative_to(self.store.root)):
                            {'schema_version': 2, 'identity_contract_version': 2, 'source': part, 'analysis': analysis.model_dump(), 'gateway': self.resume_identity},
                        str((runtime / 'checkpoints' / (part['source_id'] + '.json')).relative_to(self.store.root)): state,
                        str(checkpoint_path.relative_to(self.store.root)): state})
                    batch_done += 1
                if not state['characters']:
                    raise ValueError('没有提取到有依据的人物，请检查正文；mock 仅支持附带演示样本')
                return self.finish(state, sources, chapters, parts, request_hash, title)
            except (ValueError, OSError, ModelError) as exc:
                self.store.status('imported_source', phase='error', error_type=type(exc).__name__,
                                  completed_parts=read_json(checkpoint_path)['next_part'] if checkpoint_path.exists() else 0)
                raise

    def finish(self, state, sources, chapters, parts, request_hash, title):
        cards, memories = [], {}
        for cid, entry in state['characters'].items():
            memory = Memory.model_validate(entry['memory'])
            card = CharacterCard.model_validate({**entry['card'], 'initial_memories': [
                claim.model_dump() for claim in memory.summary + memory.recent]})
            cards.append(card)
            memories[cid] = memory.model_dump()
        bodies = [chapter.content for chapter in chapters if chapter.content.strip()]
        sample_indexes = sorted(set((0, len(bodies) // 2, len(bodies) - 1)))
        sample = '\n\n'.join(bodies[index][:1200] for index in sample_indexes)
        config = StoryConfig(title=title, outline=state['outline'], style=state['style'],
                             style_sample=sample, characters=cards)
        if self.store.path('project.json').exists() and self.store.config() != config:
            raise ValueError('未完成导入的设定已被手动修改，未覆盖修改，请使用新项目目录')
        files = story_files(config)
        traces = [read_json(self.store.path('.runtime', 'import', 'traces', part['source_id'] + '.json')) for part in parts]
        for field, name in [('world_rules', 'world.json'), ('relationships', 'relationships.json'),
                            ('timeline', 'timeline.json'), ('unresolved_threads', 'hooks.json')]:
            files[name] = {'schema_version': 2, 'items': [
                {**fact, 'source_id': trace['source']['source_id'],
                 **({'start_chapter': trace['source']['chapter'], 'last_advanced_chapter': trace['source']['chapter']}
                    if field == 'unresolved_threads' else {})}
                for trace in traces for fact in trace['analysis'][field]]}
        numbers = [chapter.number for chapter in chapters if chapter.number is not None]
        gaps = [{'after': first, 'before': last} for first, last in zip(numbers, numbers[1:]) if last > first + 1]
        order_issues = [{'previous': first, 'current': last} for first, last in zip(numbers, numbers[1:]) if last <= first]
        report = {'project': str(self.store.root), 'phase': 'completed', 'title': title, 'mock': self.mock,
                  'chapter_count': sum(not c.preface for c in chapters), 'preface_count': sum(c.preface for c in chapters),
                  'part_count': len(parts), 'characters': [
                      {'id': cid, 'name': entry['card']['name'], 'aliases': entry['aliases']} for cid, entry in state['characters'].items()],
                  'detected_gaps': gaps, 'chapter_order_issues': order_issues,
                  'continuation_point': {'after_source_segment': len(chapters),
                                         'next_number_hint': numbers[-1] + 1 if numbers and not order_issues else None,
                                         'requires_review': bool(gaps or order_issues)},
                  'uncertainties': [{'source_id': t['source']['source_id'], 'text': text}
                                    for t in traces for text in (t['analysis']['uncertainties'] + [f"{actor['name']}：{item}" for actor in t['analysis']['characters'] for item in actor.get('uncertainties', [])])],
                  'contradictions': [{'source_id': t['source']['source_id'], 'text': text}
                                    for t in traces for text in t['analysis']['contradictions']],
                  'source_files': [{'name': item['name'], 'sha256': item['checksum']} for item in sources]}
        report['continuation_point']['requires_review'] |= bool(report['uncertainties'] or report['contradictions'])
        files['import-report.json'] = {'schema_version': 2, **report}
        summaries = []
        for number, chapter in enumerate(chapters, 1):
            current = [trace for trace in traces if trace['source']['chapter'] == number]
            facts = [fact['text'] for trace in current for fact in trace['analysis']['timeline']]
            present = [cid for cid, entry in state['characters'].items()
                       if any(name in chapter.raw for name in [entry['card']['name'], *entry['aliases']])]
            summaries.append({'schema_version': 2, 'scene_id': f'imported_chapter_{number:04d}',
                'chapter': number, 'title': chapter.title, 'pov': None, 'present_characters': present,
                'event_ids': [], 'events': [], 'setting': '',
                'summary': '\n'.join(facts) if facts else chapter.content,
                'new_information': [], 'unresolved_questions': [fact['text'] for trace in current for fact in trace['analysis']['unresolved_threads']],
                'source_ids': [trace['source']['source_id'] for trace in current],
                'author_only': True, 'prose': chapter.raw})
        # The same atomic acceptance point as normal writing. No imported private
        # knowledge is published as a globally visible event.
        files['scenes/imported_source/accepted.json'] = {
            'schema_version': 2, 'chapter_summaries': summaries,
            'author_material': {name: files[name + '.json'] for name in ('world', 'relationships', 'timeline', 'hooks')},
            'artifact': {'status': 'final', 'produced_by': {'role': 'importer', 'model': self.resume_identity},
                         'reviewed_by': ['host_quote_gate'], 'reviewed_at': datetime.now(timezone.utc).isoformat()},
            'sequence': 1, 'scene_id': 'imported_source', 'request_hash': request_hash, 'provenance': 'imported',
            'prose': '\n\n'.join(item['text'] for item in sources), 'reviews': {}, 'events': [],
            'memories': memories, 'turns': []}
        self.store.commit_files(files)
        self.store.materialize()
        self.store.status('imported_source', phase='completed', total_parts=len(parts), request_hash=request_hash)
        return {**report, 'cached': False}
