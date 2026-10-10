import tempfile
import unittest
from pathlib import Path

from pov_novel.mock import MockGateway
from pov_novel.models import SceneSpec
from pov_novel.store import FileStore, read_json, write_json
from pov_novel.story import load_story
from pov_novel.workflow import NovelWorkflow

ROOT = Path(__file__).resolve().parents[1]


class StoryFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = FileStore(Path(self.temp.name) / "book")
        self.story = load_story(ROOT / "examples/story")

    def tearDown(self):
        self.temp.cleanup()

    def test_initialize_separate_files_and_index(self):
        self.store.initialize(self.story)
        self.assertFalse(self.store.path("story.json").exists())
        self.assertEqual(read_json(self.store.path("outline.json")), {"outline": self.story.outline})
        self.assertEqual(read_json(self.store.path("style.json")), {"style": self.story.style})
        self.assertEqual(read_json(self.store.path("reference.json")), {"style_sample": self.story.style_sample})
        self.assertEqual(read_json(self.store.path("project.json"))["characters"], ["linmo", "chuyao"])
        self.assertEqual(load_story(self.store.path("project.json")), self.story)

    def test_independent_edits_reach_workflow_without_shared_secrets(self):
        self.store.initialize(self.story)
        write_json(self.store.path("outline.json"), {"outline": "新的总纲"})
        write_json(self.store.path("style.json"), {"style": "新的文风"})
        write_json(self.store.path("reference.json"), {"style_sample": "新的参考正文"})
        card = self.story.characters[1].model_copy(update={"known_secrets": ["独立卡片中的秘密"]})
        write_json(self.store.path("characters", "chuyao", "card.json"), card.model_dump())
        gateway = MockGateway()
        scene = SceneSpec.model_validate(read_json(ROOT / "examples/scene.json"))
        NovelWorkflow(self.store, gateway).run(scene)
        audit = next(p for role, p in gateway.calls if role == "style")
        self.assertEqual((audit["outline"], audit["style"], audit["style_sample"]),
                         ("新的总纲", "新的文风", "新的参考正文"))
        actors = {p["character"]["id"]: p for role, p in gateway.calls if role == "character"}
        self.assertEqual(actors["chuyao"]["character"]["known_secrets"], ["独立卡片中的秘密"])
        self.assertNotIn("独立卡片中的秘密", str(actors["linmo"]))

    def test_migration_keeps_accepted_scene_hash_and_memory(self):
        write_json(self.store.path("story.json"), self.story.model_dump())
        scene = SceneSpec.model_validate(read_json(ROOT / "examples/scene.json"))
        NovelWorkflow(self.store, MockGateway()).run(scene)
        original = self.store.path("manuscript", "book.md").read_bytes()
        memory = self.store.memory("linmo")
        self.store.migrate_config()
        self.store.migrate_config()
        self.assertFalse(self.store.path("story.json").exists())
        self.assertEqual(read_json(self.store.path(".runtime", "legacy-story.json")), self.story.model_dump())
        self.assertEqual(self.store.config(), self.story)
        gateway = MockGateway()
        self.assertTrue(NovelWorkflow(self.store, gateway).run(scene)["cached"])
        self.assertEqual(gateway.calls, [])
        self.assertEqual(self.store.memory("linmo"), memory)
        self.assertEqual(self.store.path("manuscript", "book.md").read_bytes(), original)

    def test_conflicting_migration_does_not_overwrite_edits(self):
        self.store.initialize(self.story)
        write_json(self.store.path("story.json"), self.story.model_dump())
        write_json(self.store.path("style.json"), {"style": "作者的新文风"})
        with self.assertRaises(ValueError):
            self.store.migrate_config()
        self.assertEqual(read_json(self.store.path("style.json"))["style"], "作者的新文风")
        self.assertTrue(self.store.path("story.json").exists())

    def test_card_id_mismatch_is_rejected(self):
        self.store.initialize(self.story)
        write_json(self.store.path("characters", "linmo", "card.json"), self.story.characters[1].model_dump())
        with self.assertRaises(ValueError):
            self.store.config()

    def test_index_cannot_reference_outside_project(self):
        self.store.initialize(self.story)
        write_json(self.store.path("project.json"), {"title": self.story.title, "characters": ["../outside"]})
        with self.assertRaises(ValueError):
            self.store.config()


if __name__ == "__main__":
    unittest.main()
