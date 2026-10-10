import json
import tempfile
import unittest
from pathlib import Path

from pov_novel.importer import (EvidenceValidationError, NovelImporter, SourceAnalysis,
                                split_import_text, verify_analysis)
from pov_novel.mock import MockGateway
from pov_novel.models import CharacterCard, Memory
from pov_novel.store import FileStore, read_json


class IdentityImportTests(unittest.TestCase):
    def registry(self):
        return {'char_0001': {'card': CharacterCard(id='char_0001', name='林墨', personality='谨慎').model_dump(),
            'aliases': ['林先生'], 'memory': Memory().model_dump()}}

    def test_registered_alias_need_not_be_repeated_by_model(self):
        analysis = SourceAnalysis(outline='摘要', style='文风', characters=[{
            'name': '林墨', 'voice': {'text': '轻声', 'quote': '林先生轻声说话。'}}])
        verify_analysis(analysis, '林先生轻声说话。', self.registry())

    def test_absent_identity_still_rejected_by_verifier(self):
        analysis = SourceAnalysis(outline='摘要', style='文风', characters=[{
            'name': '林墨', 'current_goal': {'text': '指向空地', 'quote': '他指向空地。'}}])
        with self.assertRaises(EvidenceValidationError) as caught:
            verify_analysis(analysis, '他指向空地。', self.registry())
        self.assertEqual(caught.exception.issues[0]['type'], 'missing_name_evidence')

    def test_ambiguous_alias_cannot_select_a_character(self):
        known = self.registry()
        known['char_0002'] = {'card': {'name': '林海'}, 'aliases': ['林先生']}
        analysis = SourceAnalysis(outline='摘要', style='文风', characters=[{'name': '林先生'}])
        with self.assertRaises(EvidenceValidationError) as caught:
            verify_analysis(analysis, '林先生轻声说话。', known)
        self.assertEqual(caught.exception.issues[0]['type'], 'ambiguous_identity')

    def test_missing_identity_is_deferred_without_losing_grounded_updates(self):
        text = '林先生看着她指向空地。'
        original_uncertainties = [f'原有待确认项{i}' for i in range(20)]
        analysis = SourceAnalysis(outline='更新后的摘要', style='文风', characters=[
            {'name': '秦明珰', 'memories': [{'text': '指向空地', 'quote': text, 'kind': 'observation'}]},
            {'name': '林墨', 'current_goal': {'text': '注视空地', 'quote': text}}],
            timeline=[{'text': '有人指向空地', 'quote': text}], uncertainties=original_uncertainties)
        class Uncorrected(MockGateway):
            def generate(self, role, instruction, payload, schema):
                self.calls.append((role, payload))
                return analysis.model_copy(deep=True)
        with tempfile.TemporaryDirectory() as folder:
            store = FileStore(Path(folder)/'book')
            gateway = Uncorrected()
            importer = NovelImporter(store, gateway)
            state = {'characters': self.registry(), 'outline': '此前摘要', 'style': '文风', 'next_part': 11}
            before = json.dumps(state, ensure_ascii=False, sort_keys=True)
            part = {'source_id': 'part_s3', 'chapter': 10, 'title': '尾段', 'offset': 100, 'source_text': text}
            result = importer.extract_part(part, state)
            self.assertEqual(len(gateway.calls), 3)
            self.assertIn('characters 可以为 []', gateway.calls[1][1]['validation_feedback'])
            self.assertEqual([actor.name for actor in result.characters], ['林墨'])
            self.assertEqual(result.timeline, analysis.timeline)
            self.assertEqual(len(result.uncertainties), 20)
            self.assertTrue(all(old in '\n'.join(result.uncertainties) for old in original_uncertainties))
            self.assertIn('秦明珰', result.uncertainties[-1])
            self.assertEqual(json.dumps(state, ensure_ascii=False, sort_keys=True), before)
            note = read_json(store.path('.runtime/import/identity_uncertainties/part_s3.json'))
            self.assertEqual(note['candidates'], [analysis.characters[0].model_dump()])
            importer.merge(state, result, part['source_id'])
            self.assertEqual(set(state['characters']), {'char_0001'})
            self.assertEqual(state['characters']['char_0001']['card']['current_goal'], '注视空地')
            self.assertFalse(state['characters']['char_0001']['memory']['recent'])

    def test_fabricated_quote_cannot_be_deferred_as_identity_uncertainty(self):
        class Fabricated(MockGateway):
            def generate(self, role, instruction, payload, schema):
                self.calls.append((role, payload))
                return SourceAnalysis(outline='摘要', style='文风', characters=[{
                    'name': '林墨', 'current_goal': {'text': '指向空地', 'quote': '根本不存在的引文'}}])
        with tempfile.TemporaryDirectory() as folder:
            store = FileStore(Path(folder)/'book')
            importer = NovelImporter(store, Fabricated())
            with self.assertRaises(EvidenceValidationError) as caught:
                importer.extract_part({'source_id': 'tail', 'source_text': '他指向空地。'},
                    {'characters': self.registry(), 'outline': '摘要', 'style': '文风'})
            self.assertEqual(caught.exception.issues[0]['type'], 'quote_not_in_current_source')
            self.assertFalse(store.path('.runtime/import/identity_uncertainties/tail.json').exists())

    def test_split_preserves_bytes_and_balances_tiny_tail(self):
        text = ('林墨看着雨幕。\r\n' * 120) + '她指了指身边的空地。\r\n'
        budget = len(text.encode('utf-8')) // 2
        chunks = split_import_text(text, budget)
        self.assertEqual(''.join(chunks).encode('utf-8'), text.encode('utf-8'))
        self.assertTrue(all(len(chunk.encode('utf-8')) <= budget for chunk in chunks))
        self.assertGreater(len(chunks[-1]), 100)


if __name__ == '__main__':
    unittest.main()
