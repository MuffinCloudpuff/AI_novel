import json
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from pov_novel.gateway import ModelError, OpenAICompatibleGateway, OutputLimitError
from pov_novel.importer import NovelImporter
from pov_novel.mock import MockGateway
from pov_novel.models import Claim, CompressedMemory, Draft, Memory
from pov_novel.semantic_context import (CompiledHistory, ContextFragment, ContextManager,
    ProtectedContextOverflow, compact_private_memory, compile_context, split_text_by_estimated_tokens)
from pov_novel.store import FileStore, read_json, write_json


class SourceEditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source = self.base / 'novel.txt'
        self.store = FileStore(self.base / 'book')
        self.text = '第一章 起点\n林墨看见雨。\n第二章 相遇\n楚瑶看见林墨。\n第三章 终点\n林墨看见楚瑶离开。'
        self.source.write_bytes(self.text.encode('utf-8'))

    def tearDown(self):
        self.temp.cleanup()

    def test_remove_unread_tail_keeps_completed_prefix(self):
        first = NovelImporter(self.store, MockGateway()).run(self.source, max_parts=1)
        self.assertEqual(first['completed_parts'], 1)
        old = self.store.path('.runtime', 'import', 'checkpoint.json').read_bytes()
        shortened = self.text[:self.text.index('第三章')]
        self.source.write_bytes(shortened.encode('utf-8'))
        gateway = MockGateway()
        result = NovelImporter(self.store, gateway).run(self.source)
        self.assertEqual(result['chapter_count'], 2)
        self.assertEqual([p['chapter'] for role, p in gateway.calls if role == 'importer'], [2])
        manifest = read_json(self.store.path('scenes', 'imported_source', 'accepted.json'))
        self.assertEqual(manifest['prose'], self.source.read_bytes().decode('utf-8'))
        archived = list(self.store.path('.runtime', 'import', 'binding_history').glob('*.json'))
        self.assertEqual(read_json(archived[0])['reused_parts'], 1)
        self.assertEqual(read_json(archived[0])['checkpoint'], json.loads(old))
        self.assertTrue(list(self.store.path('.runtime', 'import', 'originals_history').glob('*.txt')))
        self.assertFalse(self.store.path('imports', 'chapters', '0003.md').exists())
        self.assertTrue(list(self.store.path('.runtime', 'import', 'chapters_history').glob('*/*.md')))

    def test_change_read_chapter_rewinds_only_affected_suffix(self):
        NovelImporter(self.store, MockGateway()).run(self.source, max_parts=2)
        self.source.write_bytes(self.text.replace('楚瑶看见林墨。', '楚瑶看见一扇门。').encode('utf-8'))
        gateway = MockGateway()
        NovelImporter(self.store, gateway).run(self.source)
        self.assertEqual([p['chapter'] for role, p in gateway.calls if role == 'importer'], [2, 3])
        traces = read_json(self.store.path('scenes', 'imported_source', 'accepted.json'))
        self.assertNotIn('楚瑶看见林墨。', str(traces['memories']))
        self.assertIn('楚瑶看见一扇门。', str(traces['memories']))

    def test_change_first_chapter_resets_stale_character_memory(self):
        NovelImporter(self.store, MockGateway()).run(self.source, max_parts=2)
        self.source.write_bytes(self.text.replace('林墨看见雨。', '林墨看见雪。').encode('utf-8'))
        gateway = MockGateway()
        NovelImporter(self.store, gateway).run(self.source)
        self.assertIn(1, [p['chapter'] for role, p in gateway.calls if role == 'importer'])
        self.assertEqual(read_json(self.store.path('.runtime', 'import', 'checkpoint.json'))['next_part'], 3)
        self.assertNotIn('林墨看见雨。', str(self.store.memory('char_0001').model_dump()))

    def test_legacy_checkpoint_migrates_after_source_shortening(self):
        NovelImporter(self.store, MockGateway()).run(self.source, max_parts=1)
        runtime = self.store.path('.runtime', 'import')
        write_json(runtime / 'binding.json', {'request_hash': 'legacy'})
        (runtime / 'plan.json').unlink()
        for path in (runtime / 'checkpoints').glob('*.json'):
            path.unlink()
        self.source.write_bytes(self.text[:self.text.index('第三章')].encode('utf-8'))
        gateway = MockGateway()
        NovelImporter(self.store, gateway).run(self.source)
        self.assertEqual([p['chapter'] for role, p in gateway.calls if role == 'importer'], [2])
        self.assertEqual(read_json(runtime / 'binding.json')['version'], 2)

    def test_runtime_budget_change_resumes_pending_work(self):
        NovelImporter(self.store, MockGateway()).run(self.source, max_parts=1)
        gateway = MockGateway()
        result = NovelImporter(self.store, gateway, chunk_size=500, context_budget=40000).run(self.source)
        self.assertEqual(result['phase'], 'completed')
        self.assertEqual([p['chapter'] for role, p in gateway.calls if role == 'importer'], [2, 3])

    def test_interrupted_source_migration_recovers_from_receipt(self):
        import pov_novel.importer as importer_module
        NovelImporter(self.store, MockGateway()).run(self.source, max_parts=2)
        self.source.write_bytes(self.text.replace('楚瑶看见林墨。', '楚瑶看见雪。').encode('utf-8'))
        original_write = importer_module.write_json
        def interrupted_write(path, value):
            if Path(path).name == 'checkpoint.json':
                raise OSError('interrupted checkpoint migration')
            return original_write(path, value)
        with patch('pov_novel.importer.write_json', side_effect=interrupted_write):
            with self.assertRaises(OSError):
                NovelImporter(self.store, MockGateway()).run(self.source)
        self.assertTrue(self.store.path('.runtime', 'import', 'pending_reset.json').exists())
        gateway = MockGateway()
        NovelImporter(self.store, gateway).run(self.source)
        self.assertEqual([p['chapter'] for role, p in gateway.calls if role == 'importer'], [2, 3])
        self.assertFalse(self.store.path('.runtime', 'import', 'pending_reset.json').exists())

    def test_character_uncertainties_are_preserved_in_report(self):
        class Uncertain(MockGateway):
            def generate(self, role, instruction, payload, schema):
                result = super().generate(role, instruction, payload, schema)
                if role == 'importer':
                    result.characters[0].uncertainties = ['尚不能确认真实动机']
                return result
        report = NovelImporter(self.store, Uncertain()).run(self.source)
        self.assertTrue(any('尚不能确认真实动机' in item['text'] for item in report['uncertainties']))

    def test_output_overflow_splits_losslessly_and_restarts(self):
        class Limited(MockGateway):
            def generate(self, role, instruction, payload, schema):
                if role == 'importer' and len(payload['source_text']) > 800:
                    raise OutputLimitError('limit')
                return super().generate(role, instruction, payload, schema)
        text = '第一章 长章\n' + '林墨站在窗边。\n' * 300
        self.source.write_bytes(text.encode('utf-8'))
        gateway = Limited()
        first = NovelImporter(self.store, gateway).run(self.source, max_parts=1)
        self.assertEqual(first['completed_parts'], 1)
        gateway = Limited()
        result = NovelImporter(self.store, gateway).run(self.source)
        plan = read_json(self.store.path('.runtime', 'import', 'plan.json'))
        self.assertEqual(''.join(p['source_text'] for p in plan), text)
        self.assertGreater(result['part_count'], 1)
        self.assertTrue(all(len(p['source_text']) <= 800 for p in plan))


class ContextCompilerTests(unittest.TestCase):
    def test_unicode_split_is_lossless_and_bounded(self):
        text = '甲乙😀' * 100 + '\r\n' + '单行。\n' * 20
        chunks = split_text_by_estimated_tokens(text, 79)
        self.assertEqual(''.join(chunks), text)
        self.assertTrue(all(0 < len(c.encode('utf-8')) <= 79 for c in chunks))

    def test_protected_content_never_enters_compression(self):
        captured = []
        def compiler(history, budget, intent):
            captured.extend(history)
            return CompiledHistory(content='旧人物状态', source_ids=['old'])
        current = ContextFragment('now', 'now', '原文必须完整保留', 'protected', 100)
        old = ContextFragment('old', 'old', '历史' * 500)
        output, trace = compile_context([old, current], 500, compiler)
        self.assertEqual(captured, [old])
        self.assertEqual(output[0], current)
        self.assertTrue(trace['compression_triggered'])
        self.assertEqual(trace['protected_source_ids'], ['now'])

    def test_protected_overflow_does_not_call_compiler(self):
        with self.assertRaises(ProtectedContextOverflow):
            compile_context([ContextFragment('now', 'now', '甲' * 500, 'protected')], 300,
                            lambda *args: self.fail('protected data compressed'))

    def test_unknown_source_is_rejected(self):
        with self.assertRaisesRegex(ValueError, '未知来源'):
            compile_context([ContextFragment('old', 'old', '甲' * 500)], 400,
                            lambda *args: CompiledHistory(content='短摘要', source_ids=['invented']))

    def test_recompression_cannot_introduce_new_sources(self):
        class Rewriter(MockGateway):
            def generate(self, role, instruction, payload, schema):
                if 'recompression_attempt' in payload:
                    return CompiledHistory(content='短摘要', source_ids=['invented'])
                return CompiledHistory(content='历史' * 1000, source_ids=['old'])
        manager = ContextManager(Rewriter(), 10000)
        with self.assertRaisesRegex(ValueError, '未知来源'):
            manager.compile_history([ContextFragment('old', 'old', '旧剧情')], 500, '保留剧情')

    def test_every_compilation_pass_respects_input_budget(self):
        class Capturing(MockGateway):
            def generate(self, role, instruction, payload, schema):
                self.calls.append((role, payload))
                content = '甲' * (100 if 'recompression_attempt' in payload else 400)
                return CompiledHistory(content=content, source_ids=['old'])
        gateway = Capturing()
        manager = ContextManager(gateway, 3000)
        output = manager.compile_history([ContextFragment('old', 'old', '旧剧情' * 800)], 800, '保留主线')
        self.assertLessEqual(len(output.content.encode('utf-8')), 800)
        self.assertTrue(any('recompression_attempt' in p for _, p in gateway.calls))
        self.assertTrue(all(len(p['source_text'].encode('utf-8')) <= 2000 for _, p in gateway.calls))

    def test_packet_preserves_source_and_records_compilation(self):
        gateway = MockGateway()
        with tempfile.TemporaryDirectory() as temp:
            manager = ContextManager(gateway, 3000, temp)
            current = {'source_text': '当前证据原句', 'source_id': 'part2'}
            result = manager.packet(current, {'previous_outline': '历史' * 1000}, recipe='test')
            self.assertEqual(result['source_text'], current['source_text'])
            self.assertIn('compiled_history', result)
            payload = [p for role, p in gateway.calls if role == 'context'][0]
            self.assertNotIn('当前证据原句', payload['source_text'])
            self.assertTrue(read_json(next(Path(temp).glob('*.json')))['compression_triggered'])

    def test_private_memory_compaction_rejects_belief_upgrade(self):
        class Upgrade(MockGateway):
            def generate(self, role, instruction, payload, schema):
                self.calls.append((role, payload))
                return CompressedMemory(summary=[Claim(text='他可能撒谎', kind='observation', evidence_ids=['own'])])
        memory = Memory(recent=[Claim(text='他可能撒谎', kind='belief', evidence_ids=['own']) for _ in range(8)])
        gateway = Upgrade()
        with self.assertRaises(ValueError):
            compact_private_memory(memory, gateway, recent_limit=4)
        self.assertEqual(len(gateway.calls), 3)
        self.assertTrue(all(c.kind == 'belief' for c in memory.recent))


class OutputRecoveryTests(unittest.TestCase):
    def test_confirmed_truncation_retries_with_larger_limit(self):
        limited = {'choices': [{'finish_reason': 'length', 'message': {'content': '{"prose":"partial'}}]}
        complete = {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"prose":"完整"}'}}]}
        with patch.dict(os.environ, {'NOVEL_MODEL': 'test'}, clear=True):
            gateway = OpenAICompatibleGateway()
            with patch('pov_novel.gateway.urlopen', side_effect=[BytesIO(json.dumps(r).encode()) for r in [limited, complete]]) as send:
                output = gateway.generate('writer', 'test', {}, Draft)
        self.assertEqual(output.prose, '完整')
        self.assertEqual([json.loads(c.args[0].data)['max_tokens'] for c in send.call_args_list], [6000, 12000])

    def test_exhausted_truncation_is_explicit_and_bounded(self):
        limited = {'choices': [{'finish_reason': 'length', 'message': {'content': '{'}}]}
        with patch.dict(os.environ, {'NOVEL_MODEL': 'test'}, clear=True):
            gateway = OpenAICompatibleGateway()
            with patch('pov_novel.gateway.urlopen', side_effect=[BytesIO(json.dumps(limited).encode()) for _ in range(3)]) as send:
                with self.assertRaises(OutputLimitError):
                    gateway.generate('importer', 'test', {}, Draft)
        self.assertEqual([json.loads(c.args[0].data)['max_tokens'] for c in send.call_args_list], [6000, 12000, 24000])

    def test_resume_identity_survives_output_setting_changes(self):
        with patch.dict(os.environ, {'NOVEL_MODEL': 'test'}, clear=True):
            first = OpenAICompatibleGateway()
            os.environ['NOVEL_MAX_OUTPUT_TOKENS'] = '12000'
            second = OpenAICompatibleGateway()
        self.assertEqual(first.resume_identity, second.resume_identity)
        self.assertNotEqual(first.identity, second.identity)

    def test_official_deepseek_extraction_disables_thinking(self):
        response = {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"prose":"结果"}'}}]}
        with patch.dict(os.environ, {'NOVEL_MODEL': 'deepseek-flash', 'NOVEL_API_BASE_URL': 'https://api.deepseek.com'}, clear=True):
            gateway = OpenAICompatibleGateway()
            with patch('pov_novel.gateway.urlopen', return_value=BytesIO(json.dumps(response).encode())) as send:
                gateway.generate('importer', 'test', {}, Draft)
        self.assertEqual(json.loads(send.call_args.args[0].data)['thinking'], {'type': 'disabled'})
