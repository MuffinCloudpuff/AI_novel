import json
import os
import sys
import unittest
from unittest.mock import patch
from urllib.error import URLError

from pov_novel.gateway import ModelError, OpenAICompatibleGateway, OutputLimitError, merge_exact_continuation
from pov_novel.models import Draft
from pov_novel.providers import CliGateway


def choice(text, reason='stop'):
    return {'choices': [{'message': {'content': text}, 'finish_reason': reason}]}


class BackendTests(unittest.TestCase):
    def gateway(self, **extra):
        with patch.dict(os.environ, {'NOVEL_MODEL': 'test-model', 'NOVEL_MAX_OUTPUT_TOKENS': '64', **extra}, clear=True):
            return OpenAICompatibleGateway()

    def test_writer_continues_only_confirmed_length_and_merges_exact_overlap(self):
        gateway = self.gateway()
        with patch.object(gateway, 'request_json', side_effect=[choice('前段。停止处', 'length'), choice('停止处后段。')]) as send:
            draft = gateway.generate_prose('写作', {'scene': '场景'})
        self.assertEqual(draft.prose, '前段。停止处后段。')
        body = json.loads(send.call_args.args[0].data)
        self.assertEqual(body['messages'][-2]['role'], 'assistant')
        self.assertEqual(body['messages'][-2]['content'], '前段。停止处')
        self.assertNotIn('response_format', body)
        self.assertEqual(body['max_tokens'], 64)

    def test_network_failure_after_partial_is_not_success(self):
        gateway = self.gateway()
        with patch.object(gateway, 'request_json', side_effect=[choice('部分正文', 'length'), ModelError('网络失败')]):
            with self.assertRaises(ModelError):
                gateway.generate_prose('写作', {})

    def test_continuations_are_bounded_and_refusals_never_accepted(self):
        gateway = self.gateway(NOVEL_MAX_CONTINUATIONS='1')
        with patch.object(gateway, 'request_json', return_value=choice('部分', 'length')) as send:
            with self.assertRaises(OutputLimitError):
                gateway.generate_prose('写作', {})
        self.assertEqual(send.call_count, 2)
        with patch.object(gateway, 'request_json', return_value=choice('被过滤', 'content_filter')):
            with self.assertRaises(ModelError):
                gateway.generate_prose('写作', {})

    def test_large_prose_is_not_limited_to_sixteen_thousand_characters(self):
        self.assertEqual(len(Draft(prose='正文'*10000).prose), 20000)
        self.assertEqual(merge_exact_continuation('ab'*3000, 'ab'*3000), 'ab'*3952)

    def test_phase_temperatures_and_provider_alias_keep_explicit_overrides(self):
        with patch.dict(os.environ, {'NOVEL_PROVIDER': 'deepseek', 'DEEPSEEK_API_KEY': 'fake-key',
            'NOVEL_MODEL': 'author-selected', 'NOVEL_CREATIVE_TEMPERATURE': '0.7', 'NOVEL_STATE_TEMPERATURE': '0.3'}, clear=True):
            gateway = OpenAICompatibleGateway()
        self.assertEqual(gateway.base_url, 'https://api.deepseek.com')
        self.assertEqual(gateway.models['writer'], 'author-selected')
        self.assertEqual(gateway.temperature_for('writer'), '0.7')
        self.assertEqual(gateway.temperature_for('state'), '0.3')
        self.assertIsNone(gateway.temperature_for('style'))

    def test_cli_stdin_transport_is_literal_and_does_not_start_shell(self):
        argv = [sys.executable, '-c', 'import sys,json; data=sys.stdin.read(); assert "非交互式" in data; print(json.dumps({"prose":"正文"}))']
        with patch.dict(os.environ, {'NOVEL_CLI_COMMAND': json.dumps(argv)}, clear=True):
            result = CliGateway().generate('writer', '方法', {'text': '$(never_execute)'}, Draft)
        self.assertEqual(result.prose, '正文')

    def test_cli_timeout_retries_and_does_not_leak_stderr(self):
        import subprocess
        with patch.dict(os.environ, {'NOVEL_CLI_COMMAND': '["program"]'}, clear=True):
            gateway = CliGateway()
        with patch('pov_novel.providers.subprocess.run', side_effect=subprocess.TimeoutExpired('secret-command', 1, stderr='secret-key')) as run, patch('pov_novel.providers.time.sleep'):
            with self.assertRaises(ModelError) as caught:
                gateway.generate('writer', '方法', {}, Draft)
        self.assertEqual(run.call_count, 3)
        self.assertNotIn('secret', str(caught.exception))
        self.assertFalse(run.call_args.kwargs['shell'])

    def test_partial_prose_is_saved_privately_without_becoming_cached_success(self):
        import tempfile
        from pathlib import Path
        from pov_novel.gateway import CachedGateway
        gateway = self.gateway(NOVEL_MAX_CONTINUATIONS='0')
        with tempfile.TemporaryDirectory() as folder:
            cache = CachedGateway(gateway, Path(folder))
            with patch.object(gateway, 'request_json', return_value=choice('未完正文', 'length')):
                with self.assertRaises(OutputLimitError):
                    cache.generate_prose('写作', {})
            self.assertEqual(len(list(Path(folder).glob('*.partial.json'))), 1)
            with patch.object(gateway, 'request_json', return_value=choice('完整正文')) as send:
                result = cache.generate_prose('写作', {})
            self.assertEqual(result.prose, '完整正文')
            self.assertEqual(send.call_count, 1)

    def test_custom_validation_reason_is_available_for_model_repair(self):
        from pov_novel.gateway import StructuredOutputError
        from pov_novel.models import MemoryCompilation
        gateway = self.gateway()
        response = {'retained_ids': [f'memory-{i}' for i in range(25)],
                    'belief_summaries': [{'text': '可能如此', 'source_memory_ids': ['memory-0']} for _ in range(7)]}
        with patch.object(gateway, 'request_json', return_value=choice(json.dumps(response))):
            with self.assertRaises(StructuredOutputError) as caught:
                gateway.generate('memory', '记忆压缩', {}, MemoryCompilation)
        self.assertIn('合计最多30项', str(caught.exception))
        self.assertIn('30', caught.exception.issues[0]['message'])


if __name__ == '__main__':
    unittest.main()
