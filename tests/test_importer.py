import hashlib
import tempfile
import unittest
from pathlib import Path

from pov_novel.chapter_splitter import ordinal_number, split_chapters
from pov_novel.gateway import ModelError
from pov_novel.importer import (ExtractedCharacter, NovelImporter, SourceAnalysis,
                                SupportedFact, SupportedMemory, source_chunks)
from pov_novel.mock import MockGateway
from pov_novel.models import Event, SceneSpec
from pov_novel.store import FileStore, read_json, write_json
from pov_novel.workflow import NovelWorkflow

ROOT = Path(__file__).resolve().parents[1]


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.store = FileStore(self.base / 'project')
        self.source = self.base / 'novel.txt'
        self.source.write_bytes((ROOT / 'examples/existing_novel.txt').read_bytes())

    def tearDown(self):
        self.temp.cleanup()

    def test_import_preserves_prose_and_separates_characters(self):
        gateway = MockGateway()
        output = NovelImporter(self.store, gateway).run(self.source)
        self.assertEqual(output['phase'], 'completed')
        self.assertTrue(output['mock'])
        self.assertEqual(output['chapter_count'], 2)
        config = self.store.config()
        cards = {card.name: card for card in config.characters}
        self.assertEqual(len(cards), 2)
        self.assertFalse(cards['林墨'].known_secrets)
        self.assertTrue(any('中毒' in secret for secret in cards['楚瑶'].known_secrets))
        memories = self.store.memory(cards['林墨'].id)
        self.assertTrue(any(claim.kind == 'belief' and '背叛' in claim.text for claim in memories.recent))
        self.assertFalse(any('中毒' in claim.text for claim in memories.recent))
        manifest = read_json(self.store.path('scenes', 'imported_source', 'accepted.json'))
        self.assertEqual(manifest['prose'], self.source.read_text(encoding='utf-8'))
        self.assertEqual(manifest['events'], [])
        self.assertEqual(self.store.path('.runtime', 'import', 'originals', '0001.txt').read_bytes(), self.source.read_bytes())
        self.assertFalse(self.store.path('story.json').exists())
        self.assertEqual(config.style_sample, '\n\n'.join(c.content[:1200] for c in split_chapters(manifest['prose'])))

    def test_repeated_import_does_not_call_models_or_duplicate_prose(self):
        NovelImporter(self.store, MockGateway()).run(self.source)
        before = self.store.path('manuscript', 'book.md').read_bytes()
        gateway = MockGateway()
        result = NovelImporter(self.store, gateway).run(self.source)
        self.assertTrue(result['cached'])
        self.assertEqual(gateway.calls, [])
        self.assertEqual(before, self.store.path('manuscript', 'book.md').read_bytes())
        self.assertEqual(len(self.store.accepted()), 1)

    def test_interrupted_import_resumes_only_remaining_chunks(self):
        class FailSecond(MockGateway):
            def generate(self, role, instruction, payload, schema):
                if role == 'importer' and payload['chapter'] == 2:
                    raise ModelError('simulated interruption')
                return super().generate(role, instruction, payload, schema)
        with self.assertRaises(ModelError):
            NovelImporter(self.store, FailSecond()).run(self.source)
        self.assertFalse(self.store.path('project.json').exists())
        self.assertFalse(self.store.accepted())
        gateway = MockGateway()
        NovelImporter(self.store, gateway).run(self.source)
        self.assertEqual([payload['chapter'] for role, payload in gateway.calls if role == 'importer'], [2])

    def test_materialization_failure_recovers_from_committed_import(self):
        original = self.store.materialize
        self.store.materialize = lambda: (_ for _ in ()).throw(OSError('disk failure'))
        with self.assertRaises(OSError):
            NovelImporter(self.store, MockGateway()).run(self.source)
        self.assertTrue(self.store.path('scenes', 'imported_source', 'accepted.json').exists())
        self.store.materialize = original
        gateway = MockGateway()
        self.assertTrue(NovelImporter(self.store, gateway).run(self.source)['cached'])
        self.assertEqual(gateway.calls, [])
        self.assertTrue(self.store.path('manuscript', 'book.md').exists())
        self.assertEqual(read_json(self.store.path('scenes', 'imported_source', 'status.json'))['phase'], 'completed')

    def test_invalid_quote_is_retried_and_not_committed(self):
        class InventQuote(MockGateway):
            def generate(self, role, instruction, payload, schema):
                if role == 'importer':
                    self.calls.append((role, payload))
                    return SourceAnalysis(outline='摘要', style='文风', characters=[ExtractedCharacter(
                        name='林墨', personality=SupportedFact(text='性格', quote='原文并不存在这句话'))])
                return super().generate(role, instruction, payload, schema)
        gateway = InventQuote()
        with self.assertRaises(ValueError):
            NovelImporter(self.store, gateway).run(self.source)
        self.assertEqual(len(gateway.calls), 3)
        self.assertFalse(self.store.accepted())
        self.assertFalse(self.store.path('project.json').exists())

    def test_continuation_uses_imported_private_memories(self):
        NovelImporter(self.store, MockGateway()).run(self.source)
        cards = {card.name: card for card in self.store.config().characters}
        ids = [cards['林墨'].id, cards['楚瑶'].id]
        scene = SceneSpec(id='next_scene', pov_character_id=ids[0], present_characters=ids,
                          setting='次日的药铺门外', goal='继续试探，暂时不揭露病情',
                          events=[Event(id='next_seen', text='林墨与楚瑶在药铺门外碰面。', visible_to=ids)])
        gateway = MockGateway()
        NovelWorkflow(self.store, gateway).run(scene)
        actors = {p['character']['id']: p for role, p in gateway.calls if role == 'character'}
        self.assertIn('背叛', str(actors[ids[0]]['memory']))
        self.assertNotIn('中毒', str(actors[ids[0]]))
        self.assertIn('中毒', str(actors[ids[1]]))
        self.assertEqual(len(self.store.accepted()), 2)
        self.assertTrue(self.store.path('manuscript', 'book.md').read_text(encoding='utf-8').startswith(self.source.read_text(encoding='utf-8')))

    def test_existing_project_and_changed_source_are_not_overwritten(self):
        NovelImporter(self.store, MockGateway()).run(self.source)
        original = self.store.path('outline.json').read_bytes()
        self.source.write_text('第一章\n林墨看见一扇门。', encoding='utf-8')
        with self.assertRaises(ValueError):
            NovelImporter(self.store, MockGateway()).run(self.source)
        self.assertEqual(original, self.store.path('outline.json').read_bytes())
        other = FileStore(self.base / 'existing')
        other.initialize(self.store.config())
        with self.assertRaises(ValueError):
            NovelImporter(other, MockGateway()).run(self.source)

    def test_directory_order_gaps_and_complete_source_coverage(self):
        directory = self.base / 'chapters'
        directory.mkdir()
        (directory / '10.txt').write_text('第十章 末尾\n林墨看见雪。', encoding='utf-8')
        (directory / '2.txt').write_text('第二章 早些\n楚瑶看见雨。', encoding='utf-8')
        gateway = MockGateway()
        report = NovelImporter(self.store, gateway).run(directory)
        self.assertEqual([item['name'] for item in report['source_files']], ['2.txt', '10.txt'])
        self.assertEqual(report['detected_gaps'], [{'after': 2, 'before': 10}])
        self.assertTrue(report['continuation_point']['requires_review'])
        self.assertEqual(report['continuation_point']['next_number_hint'], 11)
        payloads = [p for role, p in gateway.calls if role == 'importer']
        self.assertIn('雨', payloads[0]['source_text'])
        self.assertIn('雪', payloads[1]['source_text'])

    def test_large_chapter_reads_tail_and_merges_alias_identity(self):
        class Capture(MockGateway):
            def generate(self, role, instruction, payload, schema):
                if role != 'importer':
                    return super().generate(role, instruction, payload, schema)
                self.calls.append((role, payload))
                text = payload['source_text']
                quote = text[:30]
                name, aliases = ('林墨', ['小林']) if payload['offset'] == 0 else ('小林', ['林墨'])
                return SourceAnalysis(outline=payload['previous_outline'] + ('末尾真相' if '末尾真相' in text else '已读；'),
                    style='克制短句', characters=[ExtractedCharacter(name=name, aliases=aliases,
                    personality=SupportedFact(text='谨慎', quote=quote))])
        text = '第一章 长章\n林墨看见雨。小林只是林墨的别名。\n' + '小林看见雨。\n' * 300 + '末尾真相：小林终于抵达药铺。'
        self.source.write_bytes(text.encode('utf-8'))
        gateway = Capture()
        NovelImporter(self.store, gateway, chunk_size=500).run(self.source)
        self.assertEqual(''.join(p['source_text'] for role, p in gateway.calls if role == 'importer'), text)
        self.assertIn('末尾真相', self.store.config().outline)
        self.assertEqual(len(self.store.config().characters), 1)
        self.assertEqual(self.store.config().characters[0].name, '林墨')

    def test_supported_global_facts_and_uncertainties_keep_source_pointers(self):
        class Facts(MockGateway):
            def generate(self, role, instruction, payload, schema):
                result = super().generate(role, instruction, payload, schema)
                if role == 'importer':
                    fact = SupportedFact(text='作者层面的待核对事实', quote=payload['source_text'][:20])
                    return result.model_copy(update={'world_rules': [fact], 'relationships': [fact],
                        'timeline': [fact], 'unresolved_threads': [fact], 'contradictions': ['存在矛盾，不能强行确定']})
                return result
        report = NovelImporter(self.store, Facts()).run(self.source)
        for filename in ('world.json', 'relationships.json', 'timeline.json', 'hooks.json'):
            items = read_json(self.store.path(filename))['items']
            self.assertEqual(len(items), 2)
            self.assertEqual(items[0]['source_id'], 'import0001_part0001')
        self.assertEqual(report['contradictions'][0]['source_id'], 'import0001_part0001')

    def test_empty_chapter_and_source_project_overlap_are_rejected(self):
        self.source.write_text('第一章\n第二章\n林墨看见雨。', encoding='utf-8')
        with self.assertRaises(ValueError):
            NovelImporter(self.store, MockGateway()).run(self.source)
        with self.assertRaises(ValueError):
            NovelImporter(self.store, MockGateway()).run(self.base)

    def test_legacy_chinese_encoding_is_explicit_and_preserved(self):
        text = '第一章\n林墨看见楚瑶离开。'
        self.source.write_bytes(text.encode('gb18030'))
        with self.assertRaises(ValueError):
            NovelImporter(self.store, MockGateway()).run(self.source)
        NovelImporter(self.store, MockGateway()).run(self.source, encoding='gb18030')
        manifest = read_json(self.store.path('scenes', 'imported_source', 'accepted.json'))
        self.assertEqual(manifest['prose'], text)
        self.assertEqual(self.store.path('.runtime', 'import', 'originals', '0001.txt').read_bytes(), self.source.read_bytes())

    def test_upstream_guidance_and_snapshot_integrity(self):
        folder = ROOT / 'src/pov_novel/vendor/inkos'
        manifest = read_json(folder / 'provenance.json')
        for item in manifest['files']:
            self.assertEqual(hashlib.sha256((folder / item['local']).read_bytes()).hexdigest(), item['sha256'])
        gateway = MockGateway()
        captured = []
        original = gateway.generate
        def generate(role, instruction, payload, schema):
            if role == 'importer':
                captured.append(instruction)
            return original(role, instruction, payload, schema)
        gateway.generate = generate
        NovelImporter(self.store, gateway).run(self.source)
        self.assertIn((folder / 'story-import.md').read_text(encoding='utf-8'), captured[0])
        self.assertIn((folder / 'reconstruction-rubric.md').read_text(encoding='utf-8'), captured[0])


class ChapterSplitterTests(unittest.TestCase):
    def test_heading_languages_ordinals_preamble_and_aliases(self):
        self.assertEqual(ordinal_number('一○○'), 100)
        self.assertEqual(ordinal_number('一万零三十'), 10030)
        self.assertEqual(ordinal_number('IX', english=True), 9)
        text = '前言不能丢。\n\n第一回：桃园\n人物相见。\n第100回\n终章。\n'
        chapters = split_chapters(text)
        self.assertEqual([c.title for c in chapters], ['前置正文', '桃园', '第100回'])
        self.assertTrue(chapters[0].preface)
        self.assertEqual(''.join(c.raw for c in chapters), text)
        aliases = split_chapters('Chapter I.\n# Chapter 1: Opening\nMara waited.\nCHAPTER II.\nShe left.')
        self.assertEqual(len(aliases), 2)
        self.assertEqual(aliases[0].content, 'Mara waited.')
        self.assertEqual(aliases[1].title, 'Chapter 2')

    def test_chunking_keeps_every_character(self):
        text = ('长行' * 700) + '\n\n' + ('短句。\n' * 700)
        parts = list(source_chunks(text, 500))
        self.assertEqual(''.join(part for _, part in parts), text)
        self.assertTrue(all(0 < len(part) <= 500 for _, part in parts))


if __name__ == '__main__':
    unittest.main()
