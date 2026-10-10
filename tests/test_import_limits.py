import json
import tempfile
import unittest
from pathlib import Path

from pov_novel.gateway import StructuredOutputError
from pov_novel.importer import EvidenceValidationError, NovelImporter, SourceAnalysis, verify_analysis, restore_quote_layout
from pov_novel.mock import MockGateway
from pov_novel.models import CharacterCard, Memory, StoryConfig
from pov_novel.store import FileStore, digest, read_json, write_json


class AliasBudgetTests(unittest.TestCase):
    def state(self):
        return {'characters': {'char_0001': {'card': CharacterCard(id='char_0001', name='林墨', personality='谨慎').model_dump(),
            'aliases': [f'小林{i}' for i in range(12)], 'memory': Memory().model_dump()}},
            'outline': '此前摘要', 'style': '文风', 'next_part': 150}

    def gateway(self, raw):
        class Oversized(MockGateway):
            def generate(self, role, instruction, payload, schema):
                self.calls.append((role, payload))
                raise StructuredOutputError('aliases: too_long', raw_output=json.dumps(raw, ensure_ascii=False))
        return Oversized()

    def test_registered_aliases_become_delta_without_changing_contract(self):
        state = self.state()
        aliases = list(state['characters']['char_0001']['aliases'])
        raw = {'outline':'更新摘要', 'style':'文风', 'characters':[{'name':'林墨', 'aliases':aliases,
               'current_goal': {'text':'看雨', 'quote':'林墨看雨。'}}]}
        signature = digest(SourceAnalysis.model_json_schema())
        with tempfile.TemporaryDirectory() as folder:
            store = FileStore(Path(folder)/'book')
            gateway = self.gateway(raw)
            importer = NovelImporter(store, gateway)
            result = importer.extract_part({'source_id':'tail','source_text':'林墨看雨。'}, state)
            self.assertEqual(len(gateway.calls), 1)
            self.assertEqual(result.characters[0].aliases, [])
            importer.merge(state, result, 'tail')
            self.assertEqual(set(state['characters']['char_0001']['aliases']), set(aliases))
            self.assertEqual(digest(SourceAnalysis.model_json_schema()), signature)
            self.assertEqual(read_json(store.path('.runtime/import/alias_budget_repairs/tail.json'))['changes'][0]['input_count'], 12)

    def test_new_name_keeps_identity_binding_when_registered_aliases_omitted(self):
        state = self.state()
        raw = {'outline':'摘要', 'style':'文风', 'characters':[{'name':'小林新称',
                'aliases':['林墨',*state['characters']['char_0001']['aliases']]}]}
        with tempfile.TemporaryDirectory() as folder:
            importer = NovelImporter(FileStore(Path(folder)/'book'), self.gateway(raw))
            result = importer.extract_part({'source_id':'tail','source_text':'小林新称看雨。'}, state)
            self.assertEqual(result.characters[0].aliases, ['林墨'])
            importer.merge(state, result, 'tail')
            self.assertEqual(len(state['characters']), 1)
            self.assertIn('小林新称', state['characters']['char_0001']['aliases'])

    def test_new_alias_overflow_is_not_silently_trimmed(self):
        raw = {'outline':'摘要', 'style':'文风', 'characters':[{'name':'林墨','aliases':[f'新称{i}' for i in range(12)]}]}
        with tempfile.TemporaryDirectory() as folder:
            gateway = self.gateway(raw)
            importer = NovelImporter(FileStore(Path(folder)/'book'), gateway)
            with self.assertRaises(StructuredOutputError):
                importer.extract_part({'source_id':'tail','source_text':'林墨看雨。'}, self.state())
            self.assertEqual(len(gateway.calls), 3)

    def test_alias_recovery_cannot_hide_bad_quote_or_cross_character_match(self):
        state = self.state()
        raw = {'outline':'摘要', 'style':'文风', 'characters':[{'name':'林墨',
               'aliases':state['characters']['char_0001']['aliases'],
               'current_goal':{'text':'编造事实','quote':'不存在的引文'}}]}
        with tempfile.TemporaryDirectory() as folder:
            importer = NovelImporter(FileStore(Path(folder)/'book'), self.gateway(raw))
            with self.assertRaises(ValueError):
                importer.extract_part({'source_id':'tail','source_text':'林墨看雨。'}, state)
            state['characters']['char_0002'] = {'card':{'name':'林海'},'aliases':['小林11']}
            self.assertIsNone(importer.restore_registered_aliases(raw, {'source_id':'tail'}, state))

    def test_same_batch_pronouns_never_merge_two_named_characters(self):
        state = self.state()
        state['characters']['char_0002'] = {'card':CharacterCard(id='char_0002', name='楚瑶', personality='冷静').model_dump(),
                                           'aliases':['楚姑娘'], 'memory':Memory().model_dump()}
        analysis = SourceAnalysis(outline='摘要', style='文风', characters=[
            {'name':'林墨','aliases':['他','她','林墨']},
            {'name':'楚瑶','aliases':['她','臣妾','楚瑶']}])
        with tempfile.TemporaryDirectory() as folder:
            importer = NovelImporter(FileStore(Path(folder)/'book'), MockGateway())
            verify_analysis(analysis, '林墨与楚瑶看雨。', state['characters'])
            importer.merge(state, analysis, 'part')
            self.assertEqual(len(state['characters']), 2)
            self.assertNotIn('她', state['characters']['char_0001']['aliases'])
            self.assertNotIn('她', state['characters']['char_0002']['aliases'])
            self.assertNotIn('臣妾', state['characters']['char_0002']['aliases'])

    def test_legacy_generic_alias_does_not_match_a_new_named_character(self):
        state = self.state()
        state['characters']['char_0001']['aliases'] += ['她','小姐']
        analysis = SourceAnalysis(outline='摘要', style='文风', characters=[{'name':'楚瑶','aliases':['她','小姐']}])
        with tempfile.TemporaryDirectory() as folder:
            importer = NovelImporter(FileStore(Path(folder)/'book'), MockGateway())
            importer.merge(state, analysis, 'part')
            self.assertEqual(len(state['characters']), 2)
            self.assertEqual(state['characters']['char_0002']['card']['name'], '楚瑶')

    def test_specific_cross_card_alias_is_deferred_while_canonical_identity_kept(self):
        state = self.state()
        state['characters']['char_0002'] = {'card':CharacterCard(id='char_0002', name='楚瑶', personality='冷静').model_dump(),
                                           'aliases':['楚姑娘'], 'memory':Memory().model_dump()}
        analysis = SourceAnalysis(outline='摘要', style='文风', characters=[{'name':'林墨','aliases':['楚瑶','他']}])
        with tempfile.TemporaryDirectory() as folder:
            importer = NovelImporter(FileStore(Path(folder)/'book'), MockGateway())
            normalized = importer.normalize_aliases(analysis, {'source_id':'part'}, state)
            self.assertEqual(normalized.characters[0].aliases, [])
            self.assertTrue(normalized.uncertainties)
            importer.merge(state, normalized, 'part')
            self.assertNotIn('楚瑶', state['characters']['char_0001']['aliases'])
            self.assertEqual(len(state['characters']), 2)

    def test_generic_word_alone_cannot_ground_new_updates_but_legacy_readable(self):
        state = self.state()
        analysis = SourceAnalysis(outline='摘要', style='文风', characters=[{'name':'林墨','aliases':['她']}])
        with self.assertRaises(EvidenceValidationError):
            verify_analysis(analysis, '她看雨。', state['characters'])
        verify_analysis(analysis, '她看雨。', state['characters'], identity_contract_version=1)

    def test_ambiguous_and_absent_identities_are_deferred_together(self):
        state = self.state()
        state['characters']['char_0001']['aliases'] += ['林先生']
        state['characters']['char_0002'] = {'card':CharacterCard(id='char_0002', name='林海', personality='沉稳').model_dump(),
                                           'aliases':['林先生'], 'memory':Memory().model_dump()}
        raw = SourceAnalysis(outline='摘要', style='文风', characters=[
            {'name':'林先生'}, {'name':'未出现人物'}, {'name':'楚瑶'}])
        class Ambiguous(MockGateway):
            def generate(self, role, instruction, payload, schema):
                self.calls.append((role,payload))
                return raw.model_copy(deep=True)
        with tempfile.TemporaryDirectory() as folder:
            importer = NovelImporter(FileStore(Path(folder)/'book'), Ambiguous())
            result = importer.extract_part({'source_id':'part','source_text':'林先生与楚瑶等人看雨。'}, state)
            self.assertEqual([actor.name for actor in result.characters], ['楚瑶'])
            pending = read_json(importer.store.path('.runtime/import/identity_uncertainties/part.json'))
            self.assertEqual({issue['type'] for issue in pending['issues']}, {'ambiguous_identity','missing_name_evidence'})

    def test_cumulative_secrets_survive_merge_and_disk_roundtrip(self):
        state = self.state()
        old = [f'已确立的私密事实{i}' for i in range(30)]
        state['characters']['char_0001']['card']['known_secrets'] = old.copy()
        analysis = SourceAnalysis(outline='摘要', style='文风', characters=[{
            'name':'林墨', 'secrets':[{'text':'新私密事实','quote':'林墨没有告诉别人。'},
                                  {'text':old[0],'quote':'林墨没有告诉别人。'}]}])
        signature = digest(SourceAnalysis.model_json_schema())
        with tempfile.TemporaryDirectory() as folder:
            store = FileStore(Path(folder)/'book')
            importer = NovelImporter(store, MockGateway())
            verify_analysis(analysis, '林墨没有告诉别人。', state['characters'])
            importer.merge(state, analysis, 'part')
            card = CharacterCard.model_validate(state['characters']['char_0001']['card'])
            self.assertEqual(card.known_secrets, old + ['新私密事实'])
            write_json(store.path('card.json'), card.model_dump())
            self.assertEqual(CharacterCard.model_validate(read_json(store.path('card.json'))), card)
            self.assertEqual(digest(SourceAnalysis.model_json_schema()), signature)

    def test_secret_archive_size_does_not_bypass_source_quote_gate(self):
        state = self.state()
        state['characters']['char_0001']['card']['known_secrets'] = [f'既有事实{i}' for i in range(60)]
        analysis = SourceAnalysis(outline='摘要', style='文风', characters=[{
            'name':'林墨', 'secrets':[{'text':'无根据的新秘密','quote':'原文不存在'}]}])
        with self.assertRaises(EvidenceValidationError):
            verify_analysis(analysis, '林墨看雨。', state['characters'])

    def test_quotation_wrapper_recovers_only_exact_continuous_source_excerpt(self):
        source = '林墨说：“楚瑶，我愿意把船交给你。”'
        analysis = SourceAnalysis(outline='摘要', style='文风', unresolved_threads=[{
            'text':'船的归属待决定', 'quote':'“我愿意把船交给你。”'}])
        restored, repairs = restore_quote_layout(analysis, source)
        self.assertEqual(restored.unresolved_threads[0].quote, '我愿意把船交给你。')
        verify_analysis(restored, source, {})
        self.assertEqual(source[repairs[0]['source_start']:repairs[0]['source_end']], restored.unresolved_threads[0].quote)
        self.assertEqual(analysis.unresolved_threads[0].quote, '“我愿意把船交给你。”')

    def test_quotation_wrapper_does_not_repair_wording_punctuation_or_omissions(self):
        source = '林墨说：“我愿意把船交给你，明天出发。”'
        for quote in ['“我同意把船交给你，明天出发。”', '“我愿意把船交给你。明天出发。”', '“我愿意……明天出发。”']:
            with self.subTest(quote=quote):
                analysis = SourceAnalysis(outline='摘要', style='文风', timeline=[{'text':'出发安排', 'quote':quote}])
                restored, repairs = restore_quote_layout(analysis, source)
                self.assertEqual(repairs, [])
                with self.assertRaises(EvidenceValidationError):
                    verify_analysis(restored, source, {})

    def test_quotation_wrapper_preserves_ascii_word_boundaries(self):
        source = '林墨说：“we are ready now.”'
        analysis = SourceAnalysis(outline='摘要', style='文风', timeline=[{'text':'准备完成', 'quote':'"are ready"'}])
        restored, repairs = restore_quote_layout(analysis, source)
        self.assertEqual(restored.timeline[0].quote, 'are ready')
        self.assertEqual(len(repairs), 1)
        analysis.timeline[0].quote = '"areready"'
        restored, repairs = restore_quote_layout(analysis, source)
        self.assertEqual(repairs, [])
        with self.assertRaises(EvidenceValidationError):
            verify_analysis(restored, source, {})

    def test_book_cast_survives_fifty_first_character_and_config_roundtrip(self):
        state = {'characters':{f'char_{index:04d}':{
            'card':CharacterCard(id=f'char_{index:04d}', name=f'人物{index}', personality='谨慎').model_dump(),
            'aliases':[], 'memory':Memory().model_dump()} for index in range(1,51)},
            'outline':'摘要', 'style':'文风'}
        analysis = SourceAnalysis(outline='新摘要', style='文风', characters=[{'name':'新来的人物'}])
        signature = digest(SourceAnalysis.model_json_schema())
        with tempfile.TemporaryDirectory() as folder:
            store = FileStore(Path(folder)/'book')
            verify_analysis(analysis, '新来的人物走进门。', state['characters'])
            NovelImporter(store, MockGateway()).merge(state, analysis, 'part')
            self.assertEqual(len(state['characters']), 51)
            self.assertEqual(state['characters']['char_0051']['card']['name'], '新来的人物')
            config = StoryConfig(title='长篇', outline=state['outline'], style=state['style'],
                characters=[CharacterCard.model_validate(e['card']) for e in state['characters'].values()])
            write_json(store.path('config.json'), config.model_dump())
            self.assertEqual(StoryConfig.model_validate(read_json(store.path('config.json'))), config)
            self.assertEqual(digest(SourceAnalysis.model_json_schema()), signature)

    def test_large_cast_still_requires_unique_character_ids(self):
        cards=[CharacterCard(id=f'char_{i:04d}',name=f'人物{i}',personality='谨慎') for i in range(65)]
        config=StoryConfig(title='长篇',outline='摘要',style='文风',characters=cards)
        self.assertEqual(len(config.characters),65)
        with self.assertRaises(ValueError):
            StoryConfig(title='长篇',outline='摘要',style='文风',characters=cards+[cards[0]])


if __name__ == '__main__':
    unittest.main()
