import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pov_novel.models import (CharacterCard, ContinuityReview, EditorReview, Event,
    Claim, GroundedIssue, Memory, MemoryCompilation, Plan, SceneSpec, SourceRef, StoryConfig, StyleReview, Task)
from pov_novel.mock import MockGateway
from pov_novel.review import normalize_review, passed, writer_feedback
from pov_novel.retrieval import StoryIndex
from pov_novel.semantic_context import CompiledHistory, ContextFragment, compile_context
from pov_novel.semantic_context import compact_private_memory
from pov_novel.skills import active_skills, discover
from pov_novel.store import FileStore, read_json, write_bytes, write_json, write_text
from pov_novel.story_state import (RuntimeDelta, apply_delta, deterministic_findings,
    dismiss, drop_exempt, empty_state, narrative_length, settings)
from pov_novel.transactions import atomic_file_set, recover_transactions
from pov_novel.workflow import NovelWorkflow


def fixture():
    config = StoryConfig(title='试写', outline='林墨寻找失去的信。楚瑶隐瞒中毒。', style='克制、短句',
        characters=[CharacterCard(id='linmo', name='林墨', personality='谨慎'),
                    CharacterCard(id='chuyao', name='楚瑶', personality='警觉', known_secrets=['昨夜中毒'])])
    scene = SceneSpec(id='scene1', pov_character_id='linmo', present_characters=['linmo', 'chuyao'],
        setting='夜晚的客栈', goal='建立对遗失信件的疑问', plan=Plan(tasks=[]),
        events=[Event(id='seen1', text='林墨看见桌上有封信。', visible_to=['linmo']),
                Event(id='hidden1', text='楚瑶独自服下解药。', visible_to=['chuyao'])])
    return config, scene


class TransactionTests(unittest.TestCase):
    def test_literal_crlf_and_legacy_source_bytes_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'chapter.md'
            original = '第一章\r\n正文\r\n'
            write_text(path, original)
            self.assertEqual(path.read_bytes(), original.encode('utf-8'))
            raw = original.encode('gb18030')
            write_bytes(path, raw)
            self.assertEqual(path.read_bytes(), raw)

    def test_failed_commit_restores_old_files_and_removes_new_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'old.json').write_text('old', encoding='utf-8')
            def fail(index):
                if index == 1:
                    raise OSError('simulated failure')
            with self.assertRaises(OSError):
                atomic_file_set(root, {'old.json': 'new', 'created.json': 'new'}, fault=fail)
            self.assertEqual((root/'old.json').read_text(), 'old')
            self.assertFalse((root/'created.json').exists())

    def test_process_death_is_rolled_back_on_next_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            store = FileStore(folder)
            (store.root/'old.json').write_text('old')
            def die(index):
                raise KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt):
                atomic_file_set(store.root, {'old.json': 'new', 'next.json': 'next'}, fault=die)
            self.assertEqual((store.root/'old.json').read_text(), 'new')
            with store.lock():
                self.assertEqual((store.root/'old.json').read_text(), 'old')
            self.assertFalse((store.root/'next.json').exists())
            recover_transactions(store.root)  # recovery is idempotent

    def test_paths_cannot_escape_project(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                atomic_file_set(folder, {'../escape.json': 'no'})

    def test_future_versions_rejected_and_legacy_memory_upgraded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'future.json'
            write_json(path, {'schema_version': 50})
            with self.assertRaises(ValueError):
                read_json(path)
        self.assertEqual(Memory.model_validate({'schema_version': 1, 'version': 17}).schema_version, 2)
        self.assertEqual(Memory.model_validate({'version': 17}).version, 17)


class ImportEvidenceRecoveryTests(unittest.TestCase):
    def test_invalid_quote_reports_its_exact_field_without_echoing_prose(self):
        from pov_novel.importer import EvidenceValidationError, SourceAnalysis, verify_analysis
        analysis = SourceAnalysis(outline='摘要', style='文风', characters=[{
            'name': '林墨', 'personality': {'text': '谨慎', 'quote': '并不存在的秘密原句'}}])
        with self.assertRaises(EvidenceValidationError) as caught:
            verify_analysis(analysis, '林墨看见雨。', {})
        self.assertEqual(caught.exception.issues[0]['path'], ['characters', 0, 'personality', 'quote'])
        self.assertIn('characters.0.personality.quote', str(caught.exception))
        self.assertNotIn('秘密原句', str(caught.exception))

    def test_grounding_failure_splits_losslessly_after_bounded_repair(self):
        from pov_novel.importer import NovelImporter, SourceAnalysis
        class Limited(MockGateway):
            def generate(self, role, instruction, payload, schema):
                if role == 'importer' and len(payload['source_text']) > 700:
                    self.calls.append((role, payload))
                    return SourceAnalysis(outline='摘要', style='文风', characters=[{
                        'name': '林墨', 'personality': {'text': '谨慎', 'quote': '不存在的原句'}}])
                return super().generate(role, instruction, payload, schema)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root/'source.txt'
            prose = '第一章 雨幕\n林墨走过雨幕。\n' + '风在吹。\n' * 200
            source.write_bytes(prose.encode('utf-8'))
            store = FileStore(root/'book')
            gateway = Limited()
            result = NovelImporter(store, gateway).run(source)
            self.assertEqual(result['phase'], 'completed')
            parts = read_json(store.path('.runtime', 'import', 'plan.json'))
            self.assertEqual(''.join(part['source_text'] for part in parts), prose)
            self.assertTrue(all(len(part['source_text']) <= 700 for part in parts))
            self.assertEqual(sum(role == 'importer' and len(p['source_text']) > 700 for role, p in gateway.calls), 3)


class EvidenceTests(unittest.TestCase):
    def issue(self, source='draft', quote='原句'):
        return GroundedIssue(severity='major', location='末段', description='问题', suggestion='修订',
            category='style_drift', entity_id='voice', source_refs=[SourceRef(source_id=source, quote=quote)])

    def test_fabricated_and_wrong_source_quotes_are_rejected(self):
        sources = {'draft': {'text': '原句在这里。', 'writer_visible': True},
                   'outline': {'text': '作者秘密。', 'writer_visible': False}}
        for issue in (self.issue(quote='编造句子'), self.issue('not_found'), self.issue('outline', '原句')):
            with self.assertRaises(ValueError):
                normalize_review('style', StyleReview(consistency=9, genre=9, voice=9, issues=[issue], summary='审稿'), sources)

    def test_hidden_suggestions_are_not_copied_to_composer(self):
        issue = self.issue('outline', '作者秘密')
        report = StyleReview(consistency=4, genre=9, voice=9, issues=[issue], summary='作者秘密')
        feedback = writer_feedback({'style': report}, {'outline': {'text': '作者秘密', 'writer_visible': False}})
        self.assertNotIn('作者秘密', json.dumps(feedback, ensure_ascii=False))

    def test_role_specific_verdicts_and_all_style_dimensions_gate(self):
        self.assertFalse(passed({'style': StyleReview(consistency=9, genre=9, voice=6, summary='口吻')}, 7))
        self.assertFalse(passed({'outline': ContinuityReview(status='FAIL', summary='矛盾')}, 7))
        self.assertTrue(passed({'outline': ContinuityReview(status='WARNING', summary='提示')}, 7))
        self.assertTrue(passed({'editor': EditorReview(before_quality=5, after_quality=8, summary='改稿提升')}, 7))


class StateTests(unittest.TestCase):
    def test_memory_selection_copies_observations_and_forces_summaries_to_belief(self):
        observations = [Claim(text=f'亲眼看见第{i}封信。', kind='observation', evidence_ids=[f'seen{i}'], confidence='high') for i in range(17)]
        beliefs = [Claim(text=f'怀疑第{i}封信来自故人。', kind='belief', evidence_ids=[f'guess{i}']) for i in range(17)]
        memory = Memory(version=7, recent=observations+beliefs, emotion='忧虑', current_goal='寻找来处')
        class Selection(MockGateway):
            def generate(self, role, instruction, payload, schema):
                return MemoryCompilation(retained_ids=['memory-0'], belief_summaries=[{
                    'text': '可能是故人寄来的。', 'source_memory_ids': ['memory-17'], 'confidence': 'low'}])
        result = compact_private_memory(memory, Selection(), recent_limit=2)
        self.assertEqual(result.summary[0], observations[0])
        self.assertEqual(result.summary[1].kind, 'belief')
        self.assertEqual(result.summary[1].evidence_ids, ['guess0'])
        self.assertEqual(result.version, 7)
        self.assertEqual(result.emotion, '忧虑')
        self.assertEqual(result.current_goal, '寻找来处')

    def test_unknown_memory_selection_is_bounded_and_original_is_untouched(self):
        memory = Memory(recent=[Claim(text=f'看见事件{i}', kind='observation', evidence_ids=[f'e{i}']) for i in range(26)])
        original = memory.model_dump()
        class Rogue(MockGateway):
            def generate(self, role, instruction, payload, schema):
                self.calls.append((role, payload))
                return MemoryCompilation(retained_ids=['not_in_input'])
        gateway = Rogue()
        with self.assertRaises(ValueError):
            compact_private_memory(memory, gateway)
        self.assertEqual(len(gateway.calls), 3)
        self.assertEqual(memory.model_dump(), original)
        self.assertIn('不重复', gateway.calls[1][1]['validation_feedback'])

    def test_memory_format_failure_gets_bounded_feedback_repair(self):
        from pov_novel.gateway import StructuredOutputError
        memory = Memory(recent=[Claim(text=f'怀疑事件{i}', kind='belief', evidence_ids=[f'e{i}']) for i in range(26)])
        class Repair(MockGateway):
            def generate(self, role, instruction, payload, schema):
                self.calls.append((role, payload))
                if payload['validation_attempt'] == 0:
                    raise StructuredOutputError('retained_ids 格式错误', raw_output='private invalid text')
                return MemoryCompilation(retained_ids=['memory-0'])
        gateway = Repair()
        result = compact_private_memory(memory, gateway)
        self.assertEqual(result.summary[0], memory.recent[0])
        self.assertEqual(len(gateway.calls), 2)
        self.assertIn('retained_ids', gateway.calls[1][1]['validation_feedback'])
        self.assertNotIn('private invalid', gateway.calls[1][1]['validation_feedback'])

    def test_host_mints_hook_ids_and_unknown_resolve_fails(self):
        state = empty_state()
        delta = {'new_hook_candidates': [{'text': '信的来处', 'source_refs': [{'source_id': 'draft', 'quote': '信'}]}]}
        apply_delta(state, delta, 3, 'scene')
        hid = next(iter(state['hooks']))
        self.assertTrue(hid.startswith('hook-'))
        apply_delta(state, {'hook_ops': [{'op': 'mention', 'id': hid, 'source_refs': []}]}, 7, 'next')
        self.assertEqual(state['hooks'][hid]['last_advanced_chapter'], 7)
        apply_delta(state, {'hook_ops': [{'op': 'resolve', 'id': hid, 'source_refs': []}]}, 8, 'last')
        self.assertEqual(state['hooks'][hid]['status'], 'resolved')
        with self.assertRaises(ValueError):
            apply_delta(state, {'hook_ops': [{'op': 'resolve', 'id': 'unknown'}]}, 9, 'last')

    def test_temporal_facts_replace_current_but_keep_history(self):
        state = empty_state()
        fact = {'kind': 'location', 'subject': 'linmo', 'predicate': '在', 'value': '客栈', 'source_refs': []}
        apply_delta(state, {'facts': [fact]}, 1, 'one')
        apply_delta(state, {'facts': [{**fact, 'value': '渡口'}]}, 2, 'two')
        self.assertEqual(next(iter(state['facts'].values()))['value'], '渡口')
        self.assertEqual([f['value'] for f in state['history']], ['客栈', '渡口'])

    def test_findings_length_is_telemetry_until_author_sets_bounds(self):
        config, scene = fixture()
        cards = {c.id: c for c in config.characters}
        prose = '# 标题\n```text\n正文 两字\n```'
        self.assertEqual(narrative_length(prose), 4)
        findings, telemetry = deterministic_findings(empty_state(), scene, scene.plan,
            [e.model_dump() for e in scene.events], [], cards, prose)
        self.assertEqual(telemetry['characters'], 4)
        self.assertFalse(any(f['hard'] for f in findings))
        scene.min_characters = 10
        findings, _ = deterministic_findings(empty_state(), scene, scene.plan,
            [e.model_dump() for e in scene.events], [], cards, prose)
        self.assertEqual(findings[0]['key'], 'length_min:scene1')
        self.assertTrue(findings[0]['hard'])

    def test_overdue_absence_relationship_and_task_coverage(self):
        config, scene = fixture()
        state = empty_state()
        state.update(chapter=20, appearances={'missing': 2})
        state['hooks']['hook-one'] = {'id': 'hook-one', 'text': '旧信', 'status': 'open', 'expected_payoff': 10}
        state['history'] = [{'kind': 'relationship', 'subject': 'a', 'predicate': '盟友', 'value': value, 'chapter': 20} for value in ('是', '否')]
        findings, _ = deterministic_findings(state, scene, scene.plan, [], [], {c.id: c for c in config.characters}, '正文')
        self.assertEqual({f['category'] for f in findings}, {'overdue_hook', 'absent_character', 'relationship_conflict'})
        plan = Plan(tasks=[Task(character_id='linmo', after_event_id='not_executed')])
        findings, _ = deterministic_findings(empty_state(), scene, plan, [], [], {c.id: c for c in config.characters}, '正文')
        self.assertEqual(findings[0]['category'], 'task_coverage')
        self.assertTrue(findings[0]['hard'])

    def test_dismissals_survive_changed_wording_and_new_process(self):
        with tempfile.TemporaryDirectory() as folder:
            store = FileStore(folder)
            dismiss(store, 'overdue_hook:hook-one', '故意延迟到第三卷')
            changed = {'key': 'overdue_hook:hook-one', 'message': '措辞和章节都变化了'}
            self.assertEqual(drop_exempt([changed], settings(FileStore(folder))), [])
            dismiss(store, 'overdue_hook:hook-one', '', restore=True)
            self.assertEqual(drop_exempt([changed], settings(store)), [changed])

    def test_small_context_budget_uses_actual_wrapper(self):
        protected = ContextFragment('now', 'now', '当前内容', 'protected')
        old = ContextFragment('old', 'old', '过去' * 500)
        for budget in (400, 120):
            result, trace = compile_context([protected, old], budget,
                lambda *args: CompiledHistory(content='精简', source_ids=['old']))
            self.assertEqual(result[0], protected)
            self.assertLessEqual(trace['final_tokens'], budget)


class RetrievalSkillWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = FileStore(self.temp.name)
        self.config, self.scene = fixture()
        self.store.initialize(self.config)

    def tearDown(self):
        self.temp.cleanup()

    def test_fts_chinese_bigrams_and_pov_filter(self):
        index = StoryIndex(self.store)
        index.rebuild([{'scene_id': 'one', 'chapter': 1, 'title': '遗失信件', 'summary': '遗失信件在渡口',
            'prose': '作者知道解药', 'events': [e.model_dump() for e in self.scene.events]}])
        self.assertEqual(index.search('遗失信件')[0]['scene_id'], 'one')
        own = index.search('遗失信件', pov='linmo')[0]
        self.assertNotIn('解药', json.dumps(own, ensure_ascii=False))
        index.rebuild([])
        self.assertEqual(index.search('遗失信件'), [])

    def test_selector_cannot_invent_candidate_ids(self):
        index = StoryIndex(self.store)
        index.rebuild([{'scene_id': 'one', 'chapter': 1, 'title': '信件', 'summary': '信件', 'events': []}])
        class Rogue(MockGateway):
            def generate(self, role, instruction, payload, schema):
                return schema(ids=['not_candidate'])
        with self.assertRaises(ValueError):
            index.select('信件', Rogue())

    def test_skills_are_static_role_scoped_and_hash_references(self):
        skill = self.store.path('skills', 'mystery')
        skill.mkdir(parents=True)
        (skill/'SKILL.md').write_text('---\nid: mystery\nroles: [writer, style]\ngenres: [悬疑]\nreferences: [reference.md]\n---\n埋设合理线索。', encoding='utf-8')
        (skill/'reference.md').write_text('先展示可观察的线索。', encoding='utf-8')
        with patch.dict(os.environ, {'NOVEL_USER_SKILLS_DIR': str(self.store.path('none'))}):
            found = discover(self.store)
            self.assertEqual(len(found['mystery']['provenance']), 2)
            self.assertEqual(len(active_skills(self.store, 'writer', ['@mystery'])), 1)
            self.assertEqual(active_skills(self.store, 'state', ['@mystery']), [])
            self.assertEqual(active_skills(self.store, 'writer', ['-@mystery']), [])
            first = found['mystery']['provenance'][1]['sha256']
            (skill/'reference.md').write_text('更改参考。', encoding='utf-8')
            self.assertNotEqual(first, discover(self.store)['mystery']['provenance'][1]['sha256'])
            (skill/'SKILL.md').write_text('---\nreferences: [../outside.md]\n---\n正文', encoding='utf-8')
            with self.assertRaises(ValueError):
                discover(self.store)

    def test_writer_receives_goal_intent_outline_and_state_is_rebuildable(self):
        self.scene.intentional_choices = ['信件来源暂不揭示']
        gateway = MockGateway()
        result = NovelWorkflow(self.store, gateway).run(self.scene)
        self.assertEqual(result['phase'], 'completed')
        packet = next(p for role, p in gateway.calls if role == 'writer')
        self.assertEqual(packet['scene_goal'], self.scene.goal)
        self.assertEqual(packet['intentional_choices'], self.scene.intentional_choices)
        self.assertIn('寻找失去的信', packet['outline'])
        self.assertNotIn('中毒', json.dumps(packet, ensure_ascii=False))
        state = self.store.story_state()
        self.assertEqual(state['summaries'][0]['event_ids'], ['seen1', 'hidden1'])
        self.store.path('world', 'story_state.json').unlink()
        self.store.path('.runtime', 'retrieval.sqlite').unlink()
        self.store.materialize()
        self.assertEqual(self.store.story_state(), state)

    def test_grounded_delta_is_archived_and_root_hook_projection_updates(self):
        class Hooks(MockGateway):
            def generate(self, role, instruction, payload, schema):
                if role == 'state':
                    quote = '林墨看见桌上有封信。'
                    return RuntimeDelta(summary='发现信件', new_hook_candidates=[{'text': '信的来处',
                        'expected_payoff': 3, 'source_refs': [{'source_id': 'draft', 'quote': quote}]}])
                return super().generate(role, instruction, payload, schema)
        NovelWorkflow(self.store, Hooks()).run(self.scene)
        hook = read_json(self.store.path('hooks.json'))['items'][0]
        self.assertEqual(hook['status'], 'open')
        self.assertEqual(hook['start_chapter'], 1)
        self.assertIn('hook-', hook['id'])
        accepted = self.store.accepted()[0]
        self.assertEqual(accepted['artifact']['status'], 'final')
        self.assertEqual(len(accepted['artifact']['reviewed_by']), 3)

    def test_bad_review_is_bounded_and_never_accepted(self):
        class Rogue(MockGateway):
            def generate(self, role, instruction, payload, schema):
                if role == 'style':
                    self.calls.append((role, payload))
                    return StyleReview(consistency=3, genre=9, voice=9, summary='问题', issues=[{
                        'severity': 'major', 'location': '正文', 'description': '不存在的句子', 'suggestion': '修正',
                        'category': 'style', 'entity_id': 'voice', 'source_refs': [{'source_id': 'draft', 'quote': '根本没有写这句话'}]}])
                return super().generate(role, instruction, payload, schema)
        gateway = Rogue()
        with self.assertRaises(ValueError):
            NovelWorkflow(self.store, gateway).run(self.scene)
        self.assertEqual(sum(role == 'style' for role, _ in gateway.calls), 3)
        self.assertEqual(self.store.accepted(), [])

    def test_dismissal_allows_paused_scene_to_retry_without_identity_change(self):
        class Objection(MockGateway):
            def generate(self, role, instruction, payload, schema):
                if role == 'outline':
                    return ContinuityReview(status='FAIL', summary='作者有意的误解', issues=[{
                        'severity': 'major', 'location': '正文', 'description': '误解', 'suggestion': '消除',
                        'category': 'intentional_misunderstanding', 'entity_id': 'linmo',
                        'source_refs': [{'source_id': 'draft', 'quote': '林墨看见桌上有封信。'}]}])
                return super().generate(role, instruction, payload, schema)
        first = NovelWorkflow(self.store, Objection(), max_rewrites=0).run(self.scene)
        self.assertEqual(first['phase'], 'awaiting_author')
        dismiss(self.store, 'intentional_misunderstanding:linmo', '这是故意的')
        result = NovelWorkflow(self.store, Objection(), max_rewrites=0).run(self.scene, action='retry')
        self.assertEqual(result['phase'], 'completed')

    def test_proposed_manifests_never_enter_context_or_exports(self):
        write_json(self.store.path('scenes', 'pending', 'accepted.json'), {
            'sequence': 1, 'scene_id': 'pending', 'artifact': {'status': 'proposed'},
            'prose': '未通过的改稿', 'events': [], 'turns': [], 'memories': {}})
        self.assertEqual(self.store.accepted(), [])
        self.store.materialize()
        self.assertNotIn('未通过的改稿', self.store.path('manuscript', 'book.md').read_text(encoding='utf-8'))

    def test_dry_run_needs_no_key_and_leaves_project_uncommitted(self):
        from pov_novel.dry_run import preview_scene
        with patch.dict(os.environ, {}, clear=True):
            output = self.store.path('.runtime', 'preview.json')
            result = preview_scene(self.store.root, self.scene, output)
        self.assertEqual(result['api_calls'], 0)
        self.assertEqual(self.store.accepted(), [])
        self.assertTrue(any(p['role'] == 'style' for p in read_json(output)['prompts']))

    def test_manual_draft_uses_same_evidence_gate(self):
        draft = self.store.path('manual.md')
        draft.write_text('林墨看见桌上有封信。', encoding='utf-8')
        gateway = MockGateway()
        NovelWorkflow(self.store, gateway, draft_file=draft).run(self.scene)
        self.assertFalse(any(role == 'writer' for role, _ in gateway.calls))
        self.assertEqual(self.store.accepted()[0]['artifact']['produced_by']['role'], 'author')
        stages = self.store.accepted()[0]['stage_artifacts']
        self.assertEqual(stages['plan']['produced_by']['role'], 'author')
        self.assertEqual(stages['state_delta']['produced_by']['role'], 'state')
        self.assertTrue(all(artifact['status'] == 'final' for artifact in stages.values()))
        self.assertEqual(next(p for role, p in gateway.calls if role == 'style')['draft'], draft.read_text(encoding='utf-8'))

    def test_twenty_thousand_character_draft_can_complete_audit_and_state(self):
        draft = self.store.path('long.md')
        prose = '林墨看见桌上有封信。\n' * 2000
        draft.write_text(prose, encoding='utf-8')
        result = NovelWorkflow(self.store, MockGateway(), draft_file=draft).run(self.scene)
        self.assertEqual(result['phase'], 'completed')
        self.assertEqual(result['prose'], prose)
        self.assertEqual(self.store.accepted()[0]['prose'], prose)

    def test_unknown_state_hook_never_commits_a_reviewed_draft(self):
        class UnknownHook(MockGateway):
            def generate(self, role, instruction, payload, schema):
                if role == 'state':
                    self.calls.append((role, payload))
                    return RuntimeDelta(summary='发现信件', hook_ops=[{'op': 'resolve', 'id': 'unknown',
                        'source_refs': [{'source_id': 'draft', 'quote': '林墨看见桌上有封信。'}]}])
                return super().generate(role, instruction, payload, schema)
        gateway = UnknownHook()
        with self.assertRaisesRegex(ValueError, '未知 ID'):
            NovelWorkflow(self.store, gateway).run(self.scene)
        self.assertEqual(sum(role == 'state' for role, _ in gateway.calls), 3)
        self.assertEqual(self.store.accepted(), [])


if __name__ == '__main__':
    unittest.main()
