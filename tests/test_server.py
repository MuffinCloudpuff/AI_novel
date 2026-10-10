import json
import tempfile
import threading
import unittest
from argparse import Namespace
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import quote

from pov_novel.server import make_handler
from pov_novel.store import read_json
from pov_novel.story import load_story

ROOT = Path(__file__).resolve().parents[1]


class ServerTests(unittest.TestCase):
    def test_http_create_run_status_and_manuscript(self):
        with tempfile.TemporaryDirectory() as folder:
            args = Namespace(projects_dir=folder, mock=True, max_rewrites=2,
                             threshold=7.0, context_budget=24000, recent_limit=24)
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(args))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"

            def call(path, body=None):
                data = json.dumps(body).encode() if body is not None else None
                request = Request(base + path, data=data, headers={"Content-Type": "application/json"})
                with urlopen(request, timeout=30) as response:
                    return response.status, json.loads(response.read())

            try:
                self.assertEqual(call("/health")[1]["mode"], "mock")
                story = load_story(ROOT / "examples/story").model_dump()
                scene = read_json(ROOT / "examples/scene.json")
                self.assertEqual(call("/projects/testbook", story)[0], 201)
                result = call("/projects/testbook/scenes", scene)[1]
                self.assertEqual(result["phase"], "completed")
                self.assertEqual(call("/projects/testbook/scenes/" + scene["id"])[1]["phase"], "completed")
                prose = call("/projects/testbook/manuscript")[1]["prose"]
                self.assertIn("心虚", prose)
                self.assertNotIn("中毒", prose)
                self.assertTrue(call("/projects/testbook/scenes", scene)[1]["cached"])
                state = call('/projects/testbook/state')[1]
                self.assertEqual(state['chapter'], 1)
                self.assertEqual(state['summaries'][0]['scene_id'], scene['id'])
                own_search = call('/projects/testbook/search?q=' + quote('心虚') + '&pov=linmo')[1]
                self.assertEqual(len(own_search), 1)
                self.assertNotIn('中毒', json.dumps(own_search, ensure_ascii=False))
                self.assertEqual(call('/projects/testbook/skills')[1], [])
                self.assertEqual(call('/projects/testbook/settings')[1]['dismissals'], {})
                waiver = call('/projects/testbook/dismissals', {'key': 'overdue_hook:hook-test', 'reason': '有意延迟'})[1]
                self.assertEqual(waiver['dismissals']['overdue_hook:hook-test']['reason'], '有意延迟')
                restored = call('/projects/testbook/dismissals', {'key': 'overdue_hook:hook-test', 'restore': True})[1]
                self.assertEqual(restored['dismissals'], {})
                with self.assertRaises(HTTPError) as caught:
                    call("/projects/invalid%2Fproject", story)
                self.assertEqual(caught.exception.code, 400)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
