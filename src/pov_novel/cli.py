import argparse
import json
import sys
from pathlib import Path

from .gateway import ModelError, OpenAICompatibleGateway, load_env
from .mock import MockGateway
from .models import SceneSpec
from .story import load_story
from .store import FileStore, read_json
from .workflow import NovelWorkflow
from .providers import make_gateway

ROOT = Path(__file__).resolve().parents[2]


def make_workflow(project, args):
    gateway = make_gateway(mock=args.mock, backend=getattr(args, 'backend', None))
    return NovelWorkflow(FileStore(project), gateway, max_rewrites=args.max_rewrites,
                         threshold=args.threshold, context_budget=args.context_budget,
                         recent_limit=args.recent_limit, draft_file=getattr(args, 'draft_file', None),
                         skills=getattr(args, 'skill', []))


def add_runtime(parser, *, preview=False):
    parser.add_argument("--mock", action="store_true", help="离线演示，不调用模型 API")
    parser.add_argument("--max-rewrites", type=int, default=2)
    parser.add_argument("--threshold", type=float, default=7.0)
    parser.add_argument("--context-budget", type=int, default=24000,
                        help="角色上下文保守 token 上限（按 UTF-8 字节估算）")
    parser.add_argument("--recent-limit", type=int, default=24)
    parser.add_argument('--backend', choices=['api', 'cli'])
    parser.add_argument('--draft-file', help='人工正文先经过同一套审核；不通过时仍按正常流程修订')
    parser.add_argument('--skill', action='append', default=[], help='@id 启用，-@id 禁用；禁用写 --skill=-@id')
    if preview:
        parser.add_argument('--dry-run', nargs='?', const='', help='离线落盘实际组装提示；可指定输出 JSON 路径')


def main():
    parser = argparse.ArgumentParser(description="主笔委派人物的单视角小说工作流")
    parser.add_argument("--env", default=".env")
    sub = parser.add_subparsers(dest="command", required=True)
    initialize = sub.add_parser("init", help="用角色卡、总纲和文风创建小说项目")
    initialize.add_argument("--project", required=True)
    initialize.add_argument("--story", required=True, help="独立设定目录（也兼容旧 story.json）")
    migrate = sub.add_parser("migrate", help="把已有项目的 story.json 拆成独立设定文件")
    migrate.add_argument("--project", required=True)
    importer = sub.add_parser("import-novel", help="从已有 TXT/Markdown 小说提取独立设定和人物记忆")
    importer.add_argument("--project", required=True)
    importer.add_argument("--source", required=True, help="TXT/MD 文件，或按章排列的文件目录")
    importer.add_argument("--title", help="小说名称，省略时使用源文件或目录名")
    importer.add_argument("--encoding", default="utf-8-sig", help="旧中文 TXT 可指定 gb18030")
    importer.add_argument("--chunk-size", type=int, default=6000)
    importer.add_argument("--context-budget", type=int, default=60000)
    importer.add_argument("--max-parts", type=int, help="本次最多提取几个新片段；再次运行可接着提取")
    importer.add_argument("--mock", action="store_true", help="离线演示，不能代替真实提取")
    importer.add_argument('--backend', choices=['api', 'cli'])
    importer.add_argument('--dry-run', nargs='?', const='')
    run = sub.add_parser("run", help="写一个场景，或从最近 checkpoint 恢复")
    run.add_argument("--project", required=True)
    run.add_argument("--scene", required=True)
    add_runtime(run, preview=True)
    resume = sub.add_parser("resume", help="审核耗尽重试后，重试或取消")
    resume.add_argument("--project", required=True)
    resume.add_argument("--scene-id", required=True)
    resume.add_argument("--action", choices=["retry", "cancel"], required=True)
    add_runtime(resume)
    demo = sub.add_parser("demo", help="初始化并运行两个人物的演示场景")
    demo.add_argument("--project", default="projects/demo")
    add_runtime(demo, preview=True)
    status = sub.add_parser("status")
    status.add_argument("--project", required=True)
    status.add_argument("--scene-id", required=True)
    serve = sub.add_parser("serve", help="启动本地 HTTP API 网关")
    serve.add_argument("--projects-dir", default="projects")
    serve.add_argument("--port", type=int, default=8765)
    add_runtime(serve)
    for name in ('state', 'reindex', 'skills'):
        command = sub.add_parser(name, help={'state': '查看跨章状态和伏笔', 'reindex': '从归档记录重建状态与检索', 'skills': '列出静态写作技能'}[name])
        command.add_argument('--project', required=True)
    search = sub.add_parser('search', help='检索前文（作者视图，或 --pov 限制为人物已见事件）')
    search.add_argument('--project', required=True)
    search.add_argument('--query', required=True)
    search.add_argument('--pov')
    waiver = sub.add_parser('dismiss', help='持久豁免 category:entity_id；--restore 取消豁免')
    waiver.add_argument('--project', required=True)
    waiver.add_argument('--key', required=True)
    waiver.add_argument('--reason', default='')
    waiver.add_argument('--restore', action='store_true')
    export = sub.add_parser('export', help='导出仅包含 final 归档的手稿；待审修订始终排除')
    export.add_argument('--project', required=True)
    export.add_argument('--output', required=True)
    export.add_argument('--reject-all', action='store_true', help='显式要求排除所有待定修订（默认行为）')
    args = parser.parse_args()
    load_env(Path(args.env))
    try:
        if args.command == "init":
            store = FileStore(args.project)
            store.initialize(load_story(args.story))
            output = {"project": str(store.root), "phase": "initialized"}
        elif args.command == "import-novel":
            from .importer import NovelImporter
            if args.dry_run is not None:
                from .dry_run import preview_import
                output = preview_import(args.project, args.source, args.dry_run or str(Path(args.project)/'.runtime/dry_run/import.json'),
                    chunk_size=args.chunk_size, context_budget=args.context_budget, encoding=args.encoding)
            else:
                gateway = make_gateway(mock=args.mock, backend=args.backend)
                output = NovelImporter(FileStore(args.project), gateway, chunk_size=args.chunk_size, context_budget=args.context_budget).run(
                    args.source, title=args.title, encoding=args.encoding, max_parts=args.max_parts,
                    progress=lambda current, total: print(f"提取正文片段 {current}/{total}", file=sys.stderr))
        elif args.command == "migrate":
            store = FileStore(args.project)
            store.migrate_config()
            output = {"project": str(store.root), "phase": "migrated"}
        elif args.command == "demo":
            store = FileStore(args.project)
            store.initialize(load_story(ROOT / "examples/story"))
            scene = SceneSpec.model_validate(read_json(ROOT / 'examples/scene.json'))
            if args.dry_run is not None:
                from .dry_run import preview_scene
                output = preview_scene(args.project, scene, args.dry_run or str(Path(args.project)/'.runtime/dry_run/scene.json'),
                    max_rewrites=args.max_rewrites, threshold=args.threshold, context_budget=args.context_budget,
                    recent_limit=args.recent_limit, draft_file=args.draft_file, skills=args.skill)
            else:
                output = make_workflow(args.project, args).run(scene)
        elif args.command == "run":
            scene = SceneSpec.model_validate(read_json(args.scene))
            if args.dry_run is not None:
                from .dry_run import preview_scene
                output = preview_scene(args.project, scene, args.dry_run or str(Path(args.project)/'.runtime/dry_run/scene.json'),
                    max_rewrites=args.max_rewrites, threshold=args.threshold, context_budget=args.context_budget,
                    recent_limit=args.recent_limit, draft_file=args.draft_file, skills=args.skill)
            else:
                output = make_workflow(args.project, args).run(scene)
        elif args.command == "resume":
            store = FileStore(args.project)
            scene = SceneSpec.model_validate(read_json(store.path("scenes", args.scene_id, "request.json")))
            output = make_workflow(args.project, args).run(scene, action=args.action)
        elif args.command == "status":
            store = FileStore(args.project)
            output = read_json(store.path("scenes", args.scene_id, "status.json"))
        elif args.command == 'dismiss':
            from .story_state import dismiss
            output = dismiss(FileStore(args.project), args.key, args.reason, restore=args.restore)
        elif args.command == 'export':
            from .store import write_text
            store = FileStore(args.project)
            with store.lock():
                entries = store.accepted()
                destination = Path(args.output).resolve()
                # An export cannot overwrite project truth or a source manuscript.
                if destination.is_relative_to(store.root) and not destination.is_relative_to(store.path('exports')):
                    raise ValueError('项目内导出请放在 exports/，避免覆盖归档和设定')
                write_text(destination, '\n\n'.join(item['prose'] for item in entries) + '\n')
            output = {'output': str(destination), 'final_scenes': len(entries), 'pending_revisions': 'excluded'}
        elif args.command in ('state', 'reindex', 'skills', 'search'):
            store = FileStore(args.project)
            with store.lock():
                if args.command == 'skills':
                    from .skills import discover
                    output = [{k: v for k, v in skill.items() if k != 'content'} for skill in discover(store).values()]
                elif args.command == 'state':
                    output = store.story_state()
                else:
                    store.materialize()
                    if args.command == 'reindex':
                        output = {'phase': 'rebuilt', 'project': str(store.root)}
                    else:
                        from .retrieval import StoryIndex
                        output = StoryIndex(store).search(args.query, pov=args.pov)
        else:
            from .server import serve
            serve(args)
            return
        print(json.dumps(output, ensure_ascii=False, indent=2))
    except (ValueError, OSError, ModelError, RuntimeError) as exc:
        parser.exit(1, f"错误：{exc}\n")


if __name__ == "__main__":
    main()
