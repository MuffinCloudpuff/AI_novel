"""SQLite FTS5 projection; JSON manifests remain authoritative."""
import json
import re
import sqlite3
from contextlib import closing
from pydantic import Field
from .models import Record


class RetrievalSelection(Record):
    ids: list[str] = Field(default_factory=list, max_length=12)


def terms(text, *, unique=True):
    result = re.findall(r'[a-zA-Z0-9_]+', text.lower())
    for segment in re.findall(r'[\u3400-\u9fff]+', text):
        result += [segment[i:i+2] for i in range(len(segment)-1)] or [segment]
    return list(dict.fromkeys(result)) if unique else result


class StoryIndex:
    def __init__(self, store):
        self.store = store
        self.path = store.path('.runtime', 'retrieval.sqlite')

    def rebuild(self, summaries):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE VIRTUAL TABLE IF NOT EXISTS summaries USING fts5(id UNINDEXED, title, body, record UNINDEXED)')
            db.execute('DELETE FROM summaries')
            for item in summaries:
                searchable = item.get('summary', '') + '\n' + '\n'.join(item.get('new_information', [])) + '\n' + item.get('prose', '')
                db.execute('INSERT INTO summaries VALUES (?,?,?,?)', (item['scene_id'],
                    ' '.join(terms(item.get('title', ''), unique=False)), ' '.join(terms(searchable, unique=False)), json.dumps(item, ensure_ascii=False)))

    def search(self, query, *, limit=12, pov=None):
        query_terms = terms(query)
        if not query_terms or not self.path.exists():
            return []
        # Fixed identifiers and parameterized query; punctuation never becomes FTS syntax.
        expression = ' OR '.join('"' + term.replace('"', '""') + '"' for term in query_terms[:80])
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute('SELECT record FROM summaries WHERE summaries MATCH ? ORDER BY bm25(summaries,0,5,1,0),id LIMIT ?',
                              (expression, min(50, max(1, limit)))).fetchall()
        records = [json.loads(row[0]) for row in rows]
        if pov is not None:
            records = [pov_record(item, pov) for item in records]
            records = [item for item in records if item['events']]
        return records

    def select(self, query, gateway, *, limit=6, pov=None):
        candidates = self.search(query, limit=12, pov=pov)
        if not candidates:
            return []
        # Selector sees the same filtered candidates as the consumer.
        packet = {'query': query, 'candidates': [{k: v for k, v in item.items() if k != 'prose'} for item in candidates],
                  'max_results': limit}
        selected = gateway.generate('retrieval', '选择与当前任务直接相关的候选 scene_id。ids 只能来自候选列表，不生成新 ID。', packet, RetrievalSelection)
        known = {item['scene_id']: item for item in candidates}
        if len(selected.ids) != len(set(selected.ids)) or not set(selected.ids).issubset(known) or len(selected.ids) > limit:
            raise ValueError('检索选择器引用候选集之外或重复的 ID')
        return [known[sid] for sid in selected.ids]


def pov_record(item, pov):
    events = [event for event in item.get('events', []) if pov in event.get('visible_to', [])]
    return {'scene_id': item['scene_id'], 'chapter': item['chapter'], 'title': item['scene_id'],
            'summary': '\n'.join(e['text'] for e in events), 'events': events, 'author_only': False}
