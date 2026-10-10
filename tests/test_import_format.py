import json
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError
from pov_novel.gateway import OpenAICompatibleGateway, StructuredOutputError
from pov_novel.importer import NovelImporter, SourceAnalysis, restore_quote_layout, verify_analysis
from pov_novel.mock import MockGateway
from pov_novel.store import FileStore, read_json


class ExtractionFormatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.store = FileStore(self.base / 'book')
        self.source = self.base / 'novel.txt'
        self.source.write_bytes('第一章 起点\n林墨谨慎地推开门。他说话很轻。'.encode('utf-8'))
        self.valid = {
            'outline': '林墨推开门。', 'style': '短句叙述',
            'characters': [{'name': '林墨',
                'personality': {'text': '谨慎', 'quote': '林墨谨慎地推开门。', 'confidence': 'high'},
                'voice': {'text': '说话很轻', 'quote': '他说话很轻。'}, 'current_goal': None}],
            'timeline': [{'text': '林墨推开门', 'quote': '林墨谨慎地推开门。', 'confidence': 'high'}]}

    def tearDown(self):
        self.temp.cleanup()

    def test_fact_confidence_is_preserved_without_losing_quotes(self):
        analysis = SourceAnalysis.model_validate(self.valid)
        verify_analysis(analysis, self.source.read_text(encoding='utf-8'), {})
        self.assertEqual(analysis.timeline[0].confidence, 'high')
        self.assertEqual(analysis.characters[0].personality.quote, '林墨谨慎地推开门。')

    def test_unquoted_description_still_cannot_be_accepted(self):
        invalid = json.loads(json.dumps(self.valid))
        invalid['characters'][0]['personality'] = '谨慎'
        with self.assertRaises(ValidationError):
            SourceAnalysis.model_validate(invalid)
        invalid['characters'][0]['personality'] = {'text': '谨慎'}
        with self.assertRaises(ValidationError):
            SourceAnalysis.model_validate(invalid)

    def test_unknown_fields_still_fail_instead_of_being_discarded(self):
        invalid = json.loads(json.dumps(self.valid))
        invalid['timeline'][0]['fabricated_extra'] = 'unknown data'
        with self.assertRaises(ValidationError):
            SourceAnalysis.model_validate(invalid)

    def test_gateway_preserves_invalid_output_privately_for_repair(self):
        invalid = json.loads(json.dumps(self.valid))
        invalid['characters'][0]['personality'] = 'PRIVATE_INVALID_DESCRIPTION'
        raw = json.dumps(invalid, ensure_ascii=False)
        response = {'choices': [{'finish_reason': 'stop', 'message': {'content': raw}}]}
        with patch.dict(os.environ, {'NOVEL_MODEL': 'test'}, clear=True):
            gateway = OpenAICompatibleGateway()
            with patch('pov_novel.gateway.urlopen', return_value=BytesIO(json.dumps(response).encode())):
                with self.assertRaises(StructuredOutputError) as caught:
                    gateway.generate('importer', 'test', {}, SourceAnalysis)
        self.assertEqual(caught.exception.raw_output, raw)
        self.assertEqual(caught.exception.issues[0]['path'], ['characters', 0, 'personality'])
        self.assertNotIn('PRIVATE_INVALID_DESCRIPTION', str(caught.exception))

    def test_invalid_output_is_repaired_with_original_evidence(self):
        valid = self.valid
        invalid = json.loads(json.dumps(valid))
        invalid['characters'][0]['personality'] = '谨慎'
        raw = json.dumps(invalid, ensure_ascii=False)
        class Repairing(MockGateway):
            def generate(self, role, instruction, payload, schema):
                if role != 'importer':
                    return super().generate(role, instruction, payload, schema)
                self.calls.append((role, payload))
                if payload['validation_attempt'] == 0:
                    raise StructuredOutputError('characters.0.personality: model_type', raw_output=raw)
                if payload.get('invalid_output') != raw or not payload.get('repair_mode'):
                    raise AssertionError('Previous invalid result not available for repair')
                return SourceAnalysis.model_validate(valid)
        gateway = Repairing()
        report = NovelImporter(self.store, gateway).run(self.source)
        self.assertEqual(report['phase'], 'completed')
        self.assertEqual(len([p for role, p in gateway.calls if role == 'importer']), 2)
        self.assertEqual(self.store.config().characters[0].personality, '谨慎')
        self.assertEqual(read_json(self.store.path('timeline.json'))['items'][0]['confidence'], 'high')
        failure = read_json(next(self.store.path('.runtime', 'import', 'format_failures').glob('*.json')))
        self.assertEqual(failure['output'], raw)

    def test_oversized_repair_output_does_not_displace_current_source(self):
        valid = self.valid
        class Oversized(MockGateway):
            def generate(self, role, instruction, payload, schema):
                if role != 'importer':
                    return super().generate(role, instruction, payload, schema)
                self.calls.append((role, payload))
                if payload['validation_attempt'] == 0:
                    raise StructuredOutputError('invalid', raw_output='甲' * 100000)
                if 'invalid_output' in payload or not payload.get('repair_mode'):
                    raise AssertionError('Unbounded repair input')
                if '林墨谨慎地推开门。' not in payload['source_text']:
                    raise AssertionError('Lost current evidence')
                return SourceAnalysis.model_validate(valid)
        self.assertEqual(NovelImporter(self.store, Oversized()).run(self.source)['phase'], 'completed')

    def test_unrepaired_output_is_bounded_and_not_committed(self):
        class Broken(MockGateway):
            def generate(self, role, instruction, payload, schema):
                self.calls.append((role, payload))
                raise StructuredOutputError('still invalid', raw_output='{"characters":["bad"]}')
        gateway = Broken()
        with self.assertRaises(StructuredOutputError):
            NovelImporter(self.store, gateway).run(self.source)
        self.assertEqual(len(gateway.calls), 3)
        self.assertFalse(self.store.accepted())
        self.assertFalse(self.store.path('project.json').exists())

    def test_joined_chinese_lines_recover_exact_original_slice(self):
        source = '林墨谨慎地推开门。\r\n　　他说话很轻。'
        valid = json.loads(json.dumps(self.valid))
        valid['characters'][0]['current_goal'] = {
            'text': '描述', 'quote': '林墨谨慎地推开门。他说话很轻。'}
        original = SourceAnalysis.model_validate(valid)
        restored, repairs = restore_quote_layout(original, source)
        self.assertEqual(restored.characters[0].current_goal.quote, source)
        self.assertEqual(original.characters[0].current_goal.quote, '林墨谨慎地推开门。他说话很轻。')
        self.assertEqual(len(repairs), 1)
        self.assertEqual(source[repairs[0]['source_start']:repairs[0]['source_end']], repairs[0]['after'])
        verify_analysis(restored, source, {})

    def test_quote_recovery_does_not_change_punctuation_or_omit_words(self):
        for quote in ('林墨推开门。', '林墨谨慎地推开门！'):
            valid = json.loads(json.dumps(self.valid))
            valid['characters'][0]['personality']['quote'] = quote
            restored, repairs = restore_quote_layout(SourceAnalysis.model_validate(valid), self.source.read_text(encoding='utf-8'))
            self.assertEqual(repairs, [])
            with self.assertRaises(ValueError):
                verify_analysis(restored, self.source.read_text(encoding='utf-8'), {})

    def test_quote_recovery_preserves_ascii_word_boundaries(self):
        analysis = SourceAnalysis(outline='Summary', style='Style', timeline=[{'text': 'Claim', 'quote': 'She cannot go.'}])
        restored, repairs = restore_quote_layout(analysis, 'She can not go.')
        self.assertEqual(repairs, [])
        with self.assertRaises(ValueError):
            verify_analysis(restored, 'She can not go.', {})

    def test_overlong_original_quote_is_not_silently_truncated(self):
        source = ('甲' + '\n' * 10) * 100
        analysis = SourceAnalysis(outline='摘要', style='文风', timeline=[{'text': '描述', 'quote': '甲' * 100}])
        restored, repairs = restore_quote_layout(analysis, source)
        self.assertEqual(repairs, [])
        with self.assertRaises(ValueError):
            verify_analysis(restored, source, {})


if __name__ == '__main__':
    unittest.main()
