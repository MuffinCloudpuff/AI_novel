"""Verify finished import mechanically; never print manuscript or credentials."""
import argparse
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from pov_novel.chapter_splitter import split_chapters
from pov_novel.importer import SourceAnalysis, verify_analysis
from pov_novel.store import FileStore, read_json

sys.stdout.reconfigure(encoding='utf-8')
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--partial', action='store_true', help='Verify only a saved incomplete prefix')
parser.add_argument('--project', default='projects/mybook', help='Imported novel project directory')
parser.add_argument('--source', required=True, help='Original single TXT or Markdown file')
parser.add_argument('--encoding', default='utf-8-sig', help='Source text encoding used during import')
args = parser.parse_args()
project = Path(__file__).resolve().parents[1]
book = Path(args.project).resolve()
source = Path(args.source).resolve()
store = FileStore(book)
with store.lock():
    runtime = book/'.runtime/import'
    state = read_json(runtime/'checkpoint.json')
    parts = read_json(runtime/'plan.json')
    status = read_json(book/'scenes/imported_source/status.json')
    if not args.partial:
        assert status['phase'] == 'completed', 'Import has not finished'
        assert state['next_part'] == len(parts), 'Incomplete checkpoint'
    else:
        assert 0 < state['next_part'] <= len(parts), 'Invalid saved prefix'
    original_bytes = source.read_bytes()
    original = original_bytes.decode(args.encoding)
    assert (runtime/'originals/0001.txt').read_bytes() == original_bytes, 'Original snapshot differs'
    chapters = split_chapters(original, fallback=source.stem)
    by_chapter = defaultdict(list)
    previous_characters = {}
    quotes_verified = 0
    identity_versions = defaultdict(int)
    for part in parts:
        by_chapter[part['chapter']].append(part)
    for part in parts[:state['next_part']]:
        trace = read_json(runtime/'traces'/(part['source_id']+'.json'))
        assert trace['source'] == part
        analysis = SourceAnalysis.model_validate(trace['analysis'])
        verify_analysis(analysis, part['source_text'], previous_characters,
                        identity_contract_version=trace.get('identity_contract_version', 1))
        identity_versions[str(trace.get('identity_contract_version', 1))] += 1
        saved = runtime/'checkpoints'/(part['source_id']+'.json')
        if saved.exists():
            previous_characters = read_json(saved)['characters']
        else:
            # Older prefixes may predate per-part checkpoint files. Rebuild
            # only the identity catalog; no model calls or subjective replay.
            for actor in analysis.characters:
                names = {actor.name, *actor.aliases}
                matches = [key for key, entry in previous_characters.items()
                           if names.intersection([entry['card']['name'], *entry['aliases']])]
                if matches:
                    entry = previous_characters[matches[0]]
                    entry['aliases'] = sorted(set(entry['aliases']) | names - {entry['card']['name']})
                else:
                    previous_characters[f'legacy_{len(previous_characters)}'] = {'card':{'name':actor.name}, 'aliases':actor.aliases}
        quotes_verified += 1
    for number, chapter in enumerate(chapters, 1):
        offset = 0
        for part in by_chapter[number]:
            assert part['offset'] == offset
            offset += len(part['source_text'])
        assert ''.join(part['source_text'] for part in by_chapter[number]) == chapter.raw
    if not args.partial:
        accepted = read_json(book/'scenes/imported_source/accepted.json')
        report = read_json(book/'import-report.json')
        config = store.config()
        assert accepted['prose'] == original, 'Canonical manuscript differs from source'
        assert len(config.characters) == len(state['characters'])
        assert store.path('manuscript/book.md').read_bytes().decode('utf-8') == original + '\n'
        assert len(store.story_state()['summaries']) == len(chapters)
    completed_source_ids = {p['source_id'] for p in parts[:state['next_part']]}
    for entry in state['characters'].values():
        for claim in entry['memory']['summary'] + entry['memory']['recent']:
            assert set(claim['evidence_ids']).issubset(completed_source_ids), 'Unknown memory source'
    result = {'schema_version':2, 'at':datetime.now(timezone.utc).isoformat(),
              'phase':status['phase'], 'verification_scope':'completed_prefix' if args.partial else 'full_import',
              'completed_parts':state['next_part'], 'total_parts':len(parts),
              'chapters':sum(not c.preface for c in chapters), 'preface_count':sum(c.preface for c in chapters),
              'characters':len(state['characters']), 'original_sha256':hashlib.sha256(original_bytes).hexdigest(),
              'original_snapshot_equals_source':True, 'lossless_full_source_plan_verified':True,
              'completed_source_traces_verified':quotes_verified, 'memory_sources_verified':True,
              'identity_contract_versions':dict(identity_versions),
              'source_percent_completed':round(100*sum(len(p['source_text']) for p in parts[:state['next_part']])/len(original), 2),
              'memory_budget_repairs':len(list((runtime/'model_cache/memory_budget_repairs').glob('*.json')))}
    if not args.partial:
        result.update(canonical_prose_equals_original=True, uncertainty_count=len(report['uncertainties']),
                      contradiction_count=len(report['contradictions']))
    destination = project/'.runtime'/('full-import-progress-verification.json' if args.partial else 'full-import-verification.json')
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
