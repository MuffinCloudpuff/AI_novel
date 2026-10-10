import json
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from pov_novel.gateway import CachedGateway, ModelError, OpenAICompatibleGateway
from pov_novel.mock import MockGateway
from pov_novel.models import Draft
from pov_novel.review import review_instruction
from pov_novel.store import read_json


class Response(BytesIO):
    pass


class GatewayTests(unittest.TestCase):
    def test_openai_compatible_transport_and_json_contract(self):
        response = {"choices": [{"finish_reason": "stop", "message": {"content": '{"prose":"测试正文"}'}}]}
        with patch.dict(os.environ, {"NOVEL_MODEL": "configured-model", "NOVEL_API_KEY": "test-key"}, clear=True):
            gateway = OpenAICompatibleGateway()
            with patch("pov_novel.gateway.urlopen", return_value=Response(json.dumps(response).encode())) as send:
                result = gateway.generate("writer", "system", {"test": 1}, Draft)
        self.assertEqual(result.prose, "测试正文")
        request = send.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["model"], "configured-model")
        self.assertEqual(len(body["messages"]), 2)
        self.assertNotIn("tools", body)
        self.assertNotIn("test-key", body["messages"][0]["content"])

    def test_importer_model_route_and_nested_json_contract(self):
        from pov_novel.importer import SourceAnalysis
        response = {"choices": [{"finish_reason": "stop", "message": {"content":
            json.dumps({"outline": "总纲", "style": "短句", "characters": [{"name": "林墨",
                        "personality": {"text": "谨慎", "quote": "林墨看见雨。"}}]})}}]}
        with patch.dict(os.environ, {"NOVEL_MODEL": "default", "NOVEL_IMPORTER_MODEL": "extract-model"}, clear=True):
            gateway = OpenAICompatibleGateway()
            with patch("pov_novel.gateway.urlopen", return_value=Response(json.dumps(response).encode())) as send:
                output = gateway.generate("importer", "提取设定", {"source_text": "林墨看见雨。"}, SourceAnalysis)
        self.assertEqual(output.characters[0].name, "林墨")
        self.assertEqual(json.loads(send.call_args.args[0].data)["model"], "extract-model")

    def test_malformed_model_output_is_not_accepted(self):
        response = {"choices": [{"message": {"content": '{"wrong":"value"}'}}]}
        with patch.dict(os.environ, {"NOVEL_MODEL": "configured-model"}, clear=True):
            gateway = OpenAICompatibleGateway()
            with patch("pov_novel.gateway.urlopen", return_value=Response(json.dumps(response).encode())):
                with self.assertRaises(ModelError):
                    gateway.generate("writer", "system", {}, Draft)

    def test_error_does_not_expose_provider_body_or_api_key(self):
        with patch.dict(os.environ, {"NOVEL_MODEL": "configured-model", "NOVEL_API_KEY": "secret-key"}, clear=True):
            gateway = OpenAICompatibleGateway()
            error = HTTPError(gateway.base_url, 401, "unauthorized", {}, BytesIO(b"secret-key"))
            with patch("pov_novel.gateway.urlopen", side_effect=error):
                with self.assertRaises(ModelError) as caught:
                    gateway.generate("writer", "system", {}, Draft)
        self.assertNotIn("secret-key", str(caught.exception))

    def test_insufficient_balance_is_actionable_without_retries_or_secret_leak(self):
        with patch.dict(os.environ, {'NOVEL_MODEL':'configured-model','NOVEL_API_KEY':'secret-key'}, clear=True):
            gateway = OpenAICompatibleGateway()
            error = HTTPError(gateway.base_url, 402, 'payment required', {}, BytesIO(b'secret-key'))
            with patch('pov_novel.gateway.urlopen', side_effect=error) as send:
                with self.assertRaises(ModelError) as caught:
                    gateway.generate('importer', 'system', {}, Draft)
        self.assertIn('HTTP 402', str(caught.exception))
        self.assertIn('余额', str(caught.exception))
        self.assertNotIn('端点和模型配置', str(caught.exception))
        self.assertNotIn('secret-key', str(caught.exception))
        self.assertEqual(send.call_count,1)

    def test_upstream_prompts_are_used_with_local_json_contract(self):
        for role in ("style", "outline", "editor"):
            instruction = review_instruction(role)
            self.assertGreater(len(instruction), 2000)
            self.assertIn("审核适配契约", instruction)
            self.assertNotIn("# OUTPUT CONTRACT", instruction)

    def test_vendor_snapshot_integrity(self):
        import hashlib
        vendor = Path(__file__).resolve().parents[1] / "src/pov_novel/vendor/novel_os"
        manifest = read_json(vendor / "provenance.json")
        self.assertEqual(len(manifest["commit"]), 40)
        for item in manifest["files"]:
            self.assertEqual(hashlib.sha256((vendor / item["local"]).read_bytes()).hexdigest(), item["sha256"])


if __name__ == "__main__":
    unittest.main()
