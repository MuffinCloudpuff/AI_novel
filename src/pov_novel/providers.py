"""Declared aliases; explicit NOVEL_* settings always take precedence."""
import json
import os
import subprocess
import time
from .store import digest

PROVIDERS = {
    'deepseek': {'base_url': 'https://api.deepseek.com', 'model': 'deepseek-flash', 'key_env': 'DEEPSEEK_API_KEY'},
    'custom': {'base_url': 'http://127.0.0.1:8000/v1', 'model': '', 'key_env': 'NOVEL_API_KEY'},
}


def provider_defaults():
    alias = os.environ.get('NOVEL_PROVIDER')
    if not alias:
        return {}
    if alias not in PROVIDERS:
        raise ValueError('未知 NOVEL_PROVIDER：' + alias)
    return PROVIDERS[alias]


class CliGateway:
    """Opt-in CLI adapter: literal argv JSON, stdin prompt, no shell expansion."""
    def __init__(self):
        from .gateway import ModelError
        try:
            self.argv = json.loads(os.environ.get('NOVEL_CLI_COMMAND', '[]'))
        except ValueError:
            raise ModelError('NOVEL_CLI_COMMAND 必须是 JSON argv 数组') from None
        if not isinstance(self.argv, list) or not self.argv or any(not isinstance(x, str) or not x for x in self.argv):
            raise ModelError('NOVEL_CLI_COMMAND 必须是非空字符串 argv 数组')
        self.timeout = float(os.environ.get('NOVEL_API_TIMEOUT', '180'))
        self.models = {'cli': os.environ.get('NOVEL_MODEL', 'cli-configured')}
        self.identity = digest({'backend': 'cli', 'argv': self.argv, 'model': self.models, 'timeout': self.timeout})
        self.resume_identity = {'backend': 'cli', 'identity': self.identity}

    def generate(self, role, instruction, payload, schema):
        from .gateway import ModelError, StructuredOutputError
        from pydantic import ValidationError
        text = ('你是非交互式文本生成引擎，只返回指定 JSON，不调用工具、不读写文件、不询问确认。\n'
                + instruction + '\nJSON Schema:\n' + json.dumps(schema.model_json_schema(), ensure_ascii=False)
                + '\n输入数据:\n' + json.dumps(payload, ensure_ascii=False))
        for attempt in range(3):
            try:
                environment = dict(os.environ)
                environment.setdefault('PYTHONIOENCODING', 'utf-8')
                result = subprocess.run(self.argv, input=text, text=True, encoding='utf-8',
                    capture_output=True, timeout=self.timeout, shell=False, env=environment)
                if result.returncode:
                    raise OSError('CLI nonzero exit')
            except (OSError, subprocess.TimeoutExpired):
                if attempt < 2:
                    time.sleep(attempt + 1)
                    continue
                raise ModelError('模型 CLI 执行失败或超时；未提交部分输出') from None
            try:
                return schema.model_validate_json(result.stdout.strip())
            except ValidationError:
                raise StructuredOutputError('模型 CLI 未返回规定 JSON', raw_output=result.stdout) from None


def make_gateway(*, mock=False, backend=None):
    if mock:
        from .mock import MockGateway
        return MockGateway()
    selected = backend or os.environ.get('NOVEL_BACKEND', 'api')
    if selected == 'cli':
        return CliGateway()
    if selected != 'api':
        raise ValueError('NOVEL_BACKEND 必须是 api 或 cli')
    from .gateway import OpenAICompatibleGateway
    return OpenAICompatibleGateway()
