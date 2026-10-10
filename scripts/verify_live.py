"""Explicit, isolated live API verification. Does not alter the user's book."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from pov_novel.gateway import OpenAICompatibleGateway, load_env
from pov_novel.importer import NovelImporter
from pov_novel.models import Event, Plan, SceneSpec
from pov_novel.store import FileStore, read_json, write_json
from pov_novel.story import load_story
from pov_novel.workflow import NovelWorkflow


class ObservedGateway(OpenAICompatibleGateway):
    def __init__(self):
        super().__init__()
        self.calls = []

    def generate(self, role, instruction, payload, schema):
        print('live stage: ' + role, flush=True)
        result = super().generate(role, instruction, payload, schema)
        self.calls.append(role)
        return result

    def generate_prose(self, instruction, payload):
        print('live stage: writer', flush=True)
        result = super().generate_prose(instruction, payload)
        self.calls.append('writer')
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', required=True, help='显式启用真实 API，产生正常供应商调用费用')
    parser.add_argument('--root', default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--output', required=True)
    parser.add_argument('--with-follow-up', action='store_true', help='额外验证第二场景检索与跨场景状态')
    args = parser.parse_args()
    root = Path(args.root)
    output = Path(args.output)
    load_env(root/'.env')
    gateway = ObservedGateway()
    store = FileStore(output/'writing')
    store.initialize(load_story(root/'examples/story'))
    scene = SceneSpec.model_validate(read_json(root/'examples/scene.json'))
    result = NovelWorkflow(store, gateway, max_rewrites=1).run(scene)
    follow_up = None
    if args.with_follow_up:
        next_scene = scene.model_copy(deep=True)
        next_scene.id = 'live_followup'
        next_scene.goal = '延续客栈中的试探与疑问，人物暂时放下追问，不揭露秘密'
        next_scene.plan = Plan(tasks=[])
        next_scene.events = [Event(id='live_followup_seen', text='林墨把桌上的杯子推开，暂时没有再追问。', visible_to=scene.present_characters)]
        follow_up = NovelWorkflow(store, gateway, max_rewrites=1).run(next_scene)
    imported = NovelImporter(FileStore(output/'import'), gateway).run(root/'examples/existing_novel.txt')
    report = {'schema_version': 2, 'at': datetime.now(timezone.utc).isoformat(),
              'writing_phase': result['phase'], 'import_phase': imported['phase'],
              'calls': gateway.calls, 'models': gateway.models,
              'accepted_scenes': len(store.accepted()), 'prose_characters': len(result['prose']),
              'review_roles': list(result['reviews']), 'import_chapters': imported['chapter_count']}
    if follow_up:
        report['follow_up_phase'] = follow_up['phase']
    write_json(output/'verification.json', report)
    print(json.dumps(report, ensure_ascii=True, indent=2))
    if result['phase'] != 'completed' or imported['phase'] != 'completed' or (follow_up and follow_up['phase'] != 'completed'):
        raise SystemExit('Live check did not reach acceptance; inspect private review traces.')


if __name__ == '__main__':
    main()
