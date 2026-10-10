"""Assemble prompts without a key; provisional actor output is clearly labeled."""
import tempfile
from pathlib import Path
from .mock import MockGateway
from .store import FileStore, read_json, write_json


class PromptRecorder(MockGateway):
    identity = 'prompt-preview-v2'

    def __init__(self):
        super().__init__()
        self.prompts = []

    def generate(self, role, instruction, payload, schema):
        self.prompts.append({'role': role, 'instruction': instruction, 'payload': payload,
                             'json_schema': schema.model_json_schema()})
        return super().generate(role, instruction, payload, schema)


def preview_scene(project, scene, output, **runtime):
    from .workflow import NovelWorkflow
    source = FileStore(project)
    with source.lock():
        config = source.config()
        accepted = source.accepted()
        extra = {name: read_json(source.path(name)) for name in ('world.json', 'relationships.json', 'timeline.json', 'hooks.json', 'settings.json') if source.path(name).exists()}
    with tempfile.TemporaryDirectory(prefix='novel-prompts-') as folder:
        target = FileStore(folder)
        target.initialize(config)
        for item in accepted:
            write_json(target.path('scenes', item['scene_id'], 'accepted.json'), item)
        for name, value in extra.items():
            write_json(target.path(name), value)
        # Only static skill definitions/references are copied.
        from .skills import discover
        for skill in discover(source).values():
            for entry in skill['provenance']:
                original = Path(entry['path'])
                base = original.parent if original.name == 'SKILL.md' else next(Path(x['path']).parent for x in skill['provenance'] if Path(x['path']).name == 'SKILL.md')
                destination = target.path('skills', skill['id'], str(original.relative_to(base)))
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(original.read_bytes())
        recorder = PromptRecorder()
        result = NovelWorkflow(target, recorder, **runtime).run(scene)
        report = {'schema_version': 2, 'mode': 'dry_run', 'api_calls': 0,
                  'note': '下游提示使用离线演示的人物回应；提供 --draft-file 可预览真实人工正文的审核提示。原项目不提交新场景。',
                  'phase': result['phase'], 'prompts': recorder.prompts}
    write_json(Path(output), report)
    return {'mode': 'dry_run', 'api_calls': 0, 'prompt_file': str(Path(output).resolve())}


def preview_import(project, source, output, *, chunk_size=6000, context_budget=60000, encoding='utf-8-sig'):
    from .importer import NovelImporter, read_sources
    _, chapters = read_sources(source, encoding)
    recorder = PromptRecorder()
    with tempfile.TemporaryDirectory(prefix='novel-import-prompts-') as folder:
        importer = NovelImporter(FileStore(folder), recorder, chunk_size=chunk_size, context_budget=context_budget)
        parts = importer.make_parts(chapters)
        importer.extract_part(parts[0], {'next_part': 0, 'characters': {}, 'outline': '', 'style': ''})
    write_json(Path(output), {'schema_version': 2, 'mode': 'dry_run', 'api_calls': 0,
        'note': '预览第一正文片段的实际提取提示，后续上下文依赖前序真实提取结果。', 'total_parts': len(parts), 'prompts': recorder.prompts})
    return {'mode': 'dry_run', 'api_calls': 0, 'total_parts': len(parts), 'prompt_file': str(Path(output).resolve())}
