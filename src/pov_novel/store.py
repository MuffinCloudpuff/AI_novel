import hashlib
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

from .models import Memory, StoryConfig


def digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def write_bytes(path: Path, value: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("wb", dir=path.parent,
                            suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_text(path: Path, text: str):
    # Preserve literal line endings; Windows text-mode writes would double CRLF.
    write_bytes(path, text.encode('utf-8'))


def write_json(path, value):
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(value, dict) and 'schema_version' in value:
        version = value['schema_version']
        if not isinstance(version, int) or isinstance(version, bool) or version not in (1, 2):
            raise ValueError('不支持的数据 schema_version；原文件已保留')
    return value


class ProjectBusy(RuntimeError):
    pass


class FileStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self._lock_owner = None

    def path(self, *parts):
        path = self.root.joinpath(*parts).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("路径超出小说项目目录")
        return path

    @contextmanager
    def lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        # OS locks release on process death; the persistent file isn't a stale lock.
        stream = self.path(".project.lock").open("a+b")
        acquired = False
        try:
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                raise ProjectBusy("同一个小说项目已有写作任务运行") from exc
            from .transactions import recover_transactions
            recover_transactions(self.root)
            self._lock_owner = threading.get_ident()
            yield
        finally:
            if acquired:
                self._lock_owner = None
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()

    def initialize(self, config: StoryConfig):
        with self.lock():
            from .story import write_story
            if self.path("project.json").exists() or self.path("story.json").exists():
                if self.config() != config:
                    raise ValueError("项目已存在且设定不同，请使用新目录；已有项目请直接编辑独立设定文件")
                return
            write_story(self.root, config)

    def config(self):
        from .story import load_story
        if self._lock_owner == threading.get_ident():
            return load_story(self.root)
        with self.lock():
            return load_story(self.root)

    def migrate_config(self):
        """Keep existing scene hashes and memory while splitting legacy config."""
        from .story import write_story
        with self.lock():
            legacy = self.path("story.json")
            if not legacy.exists():
                self.config()  # Validate an already migrated project.
                return
            config = StoryConfig.model_validate(read_json(legacy))
            if self.path("project.json").exists():
                if self.config() != config:
                    raise ValueError("独立设定文件与旧 story.json 不一致，未覆盖任何文件")
            else:
                write_story(self.root, config)
            backup = self.path(".runtime", "legacy-story.json")
            if backup.exists() and read_json(backup) != config.model_dump():
                raise ValueError("旧设定备份已存在且内容不同")
            write_json(backup, config.model_dump())
            legacy.unlink()

    def accepted(self):
        if self._lock_owner != threading.get_ident():
            with self.lock():
                return self.accepted()
        paths = self.path("scenes").glob("*/accepted.json")
        return sorted((item for path in paths if (item := read_json(path)).get('artifact', {}).get('status', 'final') == 'final'),
                      key=lambda item: item["sequence"])

    def memory(self, character_id):
        # Accepted scene manifests are authoritative, even if materialization failed.
        for item in reversed(self.accepted()):
            if character_id in item["memories"]:
                return Memory.model_validate(item["memories"][character_id])
        card = next(card for card in self.config().characters if card.id == character_id)
        return Memory(recent=card.initial_memories, current_goal=card.current_goal)

    def observations(self, character_id):
        return [event for item in self.accepted() for event in item["events"]
                if character_id in event["visible_to"]]

    def materialize(self):
        """Rebuild derived files from committed manifests, without model calls."""
        if self._lock_owner != threading.get_ident():
            with self.lock():
                return self.materialize()
        scenes = self.accepted()
        if not scenes and self.path('.runtime', 'import', 'checkpoint.json').exists():
            raise ValueError('导入尚未完成，请先继续 import-novel；未覆盖已有状态')
        from .story_state import read_author_material
        if scenes and not any('author_material' in item for item in scenes) and not self.path('imports', 'foundation', 'author_material.json').exists():
            write_json(self.path('imports', 'foundation', 'author_material.json'),
                       {'schema_version': 2, 'materials': read_author_material(self)})
        prose = []
        for item in scenes:
            scene_id = item["scene_id"]
            write_text(self.path("manuscript", f"{scene_id}.md"), item["prose"] + "\n")
            write_json(self.path("world", "events", f"{scene_id}.json"), item["events"])
            for cid, memory in item["memories"].items():
                write_json(self.path("characters", cid, "memory.json"), memory)
            for turn in item["turns"]:
                cid = turn["character_id"]
                write_json(self.path("characters", cid, "private_logs", turn["task_id"] + ".json"), turn)
            prose.append(item["prose"])
            if item.get('artifact'):
                write_json(self.path('scenes', scene_id, 'artifacts', 'final.json'),
                           {'schema_version': 2, 'artifact': item['artifact'], 'accepted': 'accepted.json'})
            for stage, artifact in item.get('stage_artifacts', {}).items():
                write_json(self.path('scenes', scene_id, 'artifacts', 'stages', stage + '.json'),
                           {'schema_version': 2, 'artifact': artifact})
        write_text(self.path("manuscript", "book.md"), "\n\n".join(prose) + "\n")
        from .story_state import rebuild_state
        from .retrieval import StoryIndex
        state = rebuild_state(self)
        write_json(self.path('world', 'story_state.json'), state)
        write_json(self.path('world', 'chapter_summaries.json'), {'schema_version': 2, 'items': state['summaries']})
        # Root author files are now live projections, retaining imported facts.
        write_json(self.path('hooks.json'), {'schema_version': 2, 'items': list(state['hooks'].values())})
        write_json(self.path('timeline.json'), {'schema_version': 2, 'items': state['timeline'] + [fact for fact in state['history'] if fact['kind'] == 'time'], 'scenes': [
            {'scene_id': item['scene_id'], 'chapter': item['chapter'], 'text': item['summary'], 'event_ids': item['event_ids']}
            for item in state['summaries']]})
        write_json(self.path('relationships.json'), {'schema_version': 2, 'items': state['relationships'] + [
            fact for fact in state['facts'].values() if fact['kind'] == 'relationship']})
        write_json(self.path('world.json'), {'schema_version': 2, 'items': state['world'] + [
            fact for fact in state['facts'].values() if fact['kind'] in ('world', 'location', 'item', 'time')]})
        StoryIndex(self).rebuild(state['summaries'])

    def story_state(self):
        from .story_state import rebuild_state
        if self._lock_owner == threading.get_ident():
            return rebuild_state(self)
        with self.lock():
            return rebuild_state(self)

    def commit_files(self, files):
        from .transactions import atomic_file_set, json_files
        atomic_file_set(self.root, json_files(files))

    def status(self, scene_id, **fields):
        write_json(self.path("scenes", scene_id, "status.json"), fields)
