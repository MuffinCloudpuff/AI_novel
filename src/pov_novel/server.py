"""Local, author-facing HTTP gateway; exposes only allowlisted project paths."""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .gateway import ModelError
from .models import SceneSpec, StoryConfig
from .store import FileStore, ProjectBusy, read_json


def project_path(root, project_id):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", project_id):
        raise ValueError("项目 ID 无效")
    target = (root / project_id).resolve()
    if not target.is_relative_to(root):
        raise ValueError("项目路径无效")
    return target


def make_handler(args):
    root = Path(args.projects_dir).resolve()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *values):
            # Request bodies, keys, and character histories are never logged.
            pass

        def send_json(self, code, value):
            raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def body(self):
            if getattr(self, '_body_loaded', False):
                return self._parsed_body
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 1024 * 1024:
                raise ValueError("请求体必须是1MB以内的 JSON")
            self._parsed_body = json.loads(self.rfile.read(size))
            self._body_loaded = True
            return self._parsed_body

        def handle_request(self):
            self._body_loaded = False
            # Consume the bounded JSON body before route validation, so closing
            # a rejected Windows connection does not reset an unread upload.
            if self.command == 'POST':
                self.body()
            parts = urlsplit(self.path).path.strip("/").split("/")
            if self.command == "GET" and parts == ["health"]:
                return self.send_json(200, {"status": "ok", "mode": "mock" if args.mock else "api"})
            if len(parts) < 2 or parts[0] != "projects":
                return self.send_json(404, {"error": "接口不存在"})
            project = project_path(root, parts[1])
            if self.command == 'GET' and not project.exists():
                raise FileNotFoundError(project)
            store = FileStore(project)
            if self.command == 'GET' and parts[2:] in (['state'], ['settings'], ['skills']):
                with store.lock():
                    if parts[2:] == ['state']:
                        value = store.story_state()
                    elif parts[2:] == ['settings']:
                        from .story_state import settings
                        value = settings(store)
                    else:
                        from .skills import discover
                        value = [{k: v for k, v in skill.items() if k != 'content'} for skill in discover(store).values()]
                return self.send_json(200, value)
            if self.command == 'POST' and parts[2:] == ['dismissals']:
                from .story_state import dismiss
                body = self.body()
                value = dismiss(store, body['key'], body.get('reason', ''), restore=body.get('restore', False))
                return self.send_json(200, value)
            if self.command == 'GET' and parts[2:] == ['search']:
                from .retrieval import StoryIndex
                query = parse_qs(urlsplit(self.path).query)
                with store.lock():
                    store.materialize()
                    value = StoryIndex(store).search(query.get('q', [''])[0], pov=query.get('pov', [None])[0])
                return self.send_json(200, value)
            if self.command == "POST" and len(parts) == 2:
                store.initialize(StoryConfig.model_validate(self.body()))
                return self.send_json(201, {"project_id": parts[1]})
            if self.command == "GET" and parts[2:] == ["manuscript"]:
                with store.lock():
                    prose = '\n\n'.join(item['prose'] for item in store.accepted()) + '\n'
                return self.send_json(200, {"prose": prose})
            if self.command == "GET" and len(parts) == 4 and parts[2] == "scenes":
                return self.send_json(200, read_json(store.path("scenes", parts[3], "status.json")))
            from .cli import make_workflow
            if self.command == "POST" and parts[2:] == ["scenes"]:
                result = make_workflow(project, args).run(SceneSpec.model_validate(self.body()))
                return self.send_json(202 if result["phase"] == "awaiting_author" else 200, result)
            if self.command == "POST" and len(parts) == 5 and parts[2] == "scenes" and parts[4] == "resume":
                body = self.body()
                if not isinstance(body, dict) or body.get("action") not in ("retry", "cancel"):
                    raise ValueError("action 必须是 retry 或 cancel")
                scene = SceneSpec.model_validate(read_json(store.path("scenes", parts[3], "request.json")))
                result = make_workflow(project, args).run(scene, action=body["action"])
                return self.send_json(202 if result["phase"] == "awaiting_author" else 200, result)
            return self.send_json(404, {"error": "接口不存在"})

        def dispatch(self):
            try:
                self.handle_request()
            except ProjectBusy as exc:
                self.send_json(409, {"error": str(exc)})
            except FileNotFoundError:
                self.send_json(404, {"error": "项目或场景不存在"})
            except (ValueError, TypeError, KeyError) as exc:
                self.send_json(400, {"error": str(exc)})
            except ModelError as exc:
                self.send_json(502, {"error": str(exc)})
            except Exception:
                self.send_json(500, {"error": "执行失败，请检查场景 status.json 并恢复运行"})

        do_GET = dispatch
        do_POST = dispatch

    return Handler


def serve(args):
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args))
    print(f"小说 API 网关：http://127.0.0.1:{server.server_port}（仅本机）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
