import json
import tempfile
import unittest
from pathlib import Path

from pov_novel.context import build_character_context
from pov_novel.gateway import ModelError
from pov_novel.mock import MockGateway
from pov_novel.models import Claim, Event, Issue, Memory, Plan, Review, SceneSpec, StoryConfig, Task
from pov_novel.store import FileStore, ProjectBusy, read_json
from pov_novel.story import load_story
from pov_novel.workflow import NovelWorkflow

ROOT = Path(__file__).resolve().parents[1]


class FailReview(MockGateway):
    identity = "review-failure-test"

    def __init__(self, always=False):
        super().__init__()
        self.always = always
        self.style_calls = 0

    def generate(self, role, instruction, payload, schema):
        if role == "writer" and payload.get("review_feedback"):
            result = super().generate(role, instruction, payload, schema)
            # Simulate a writer actually changing its wording after feedback.
            return result.model_copy(update={"prose": result.prose.replace("莫非他在心虚。", "他是不是在心虚？")})
        if role == "style":
            self.style_calls += 1
            if self.always or self.style_calls == 1:
                self.calls.append((role, payload))
                return Review(score=4, summary="文风漂移", issues=[Issue(
                    severity="major", location="末段", description="语气过于直白",
                    evidence=payload["draft"][-30:], suggestion="恢复克制和短句")])
        return super().generate(role, instruction, payload, schema)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = FileStore(Path(self.temp.name) / "book")
        self.story = load_story(ROOT / "examples/story")
        self.scene = SceneSpec.model_validate(read_json(ROOT / "examples/scene.json"))
        self.store.initialize(self.story)

    def tearDown(self):
        self.temp.cleanup()

    def test_character_isolation_and_single_pov(self):
        gateway = MockGateway()
        result = NovelWorkflow(self.store, gateway).run(self.scene)
        self.assertEqual(result["phase"], "completed")
        actor_inputs = {p["character"]["id"]: p for role, p in gateway.calls if role == "character"}
        a_input = json.dumps(actor_inputs["linmo"], ensure_ascii=False)
        self.assertNotIn("中毒", a_input)
        self.assertNotIn("解药", a_input)
        self.assertNotIn("outline", actor_inputs["linmo"])
        self.assertNotIn("scene_goal", actor_inputs["linmo"])
        b_input = json.dumps(actor_inputs["chuyao"], ensure_ascii=False)
        self.assertIn("中毒", b_input)
        self.assertNotIn("林墨一直怀疑", b_input)
        writer_payload = next(p for role, p in gateway.calls if role == "writer")
        self.assertNotIn("中毒", json.dumps(writer_payload, ensure_ascii=False))
        self.assertNotIn("解药", result["prose"])
        self.assertIn("心虚", result["prose"])
        b_logs = list(self.store.path("characters", "chuyao", "private_logs").glob("*.json"))
        self.assertEqual(len(b_logs), 1)
        self.assertIn("中毒", read_json(b_logs[0])["result"]["private_text"])
        self.assertTrue(any(c.kind == "belief" and "心虚" in c.text for c in self.store.memory("linmo").recent))

    def test_changing_hidden_secret_does_not_change_other_character_input(self):
        def run(secret, directory):
            story = self.story.model_copy(deep=True)
            story.characters[1].known_secrets = [secret]
            scene = self.scene.model_copy(deep=True)
            scene.plan = Plan(tasks=[Task(character_id="linmo", after_event_id="ch01_seen03")])
            store = FileStore(Path(self.temp.name) / directory)
            store.initialize(story)
            gateway = MockGateway()
            NovelWorkflow(store, gateway).run(scene)
            return next(p for role, p in gateway.calls if role == "character")
        self.assertEqual(run("秘密甲：昨夜中毒", "first"), run("秘密乙：藏有账本", "second"))

    def test_future_event_is_not_sent_to_earlier_actor(self):
        scene = self.scene.model_copy(deep=True)
        scene.events.append(Event(id="future_event", text="楚瑶随后公开自己的真实病情", visible_to=["linmo", "chuyao"]))
        scene.plan = Plan(tasks=[Task(character_id="linmo", after_event_id="ch01_seen03")])
        gateway = MockGateway()
        NovelWorkflow(self.store, gateway).run(scene)
        payload = next(p for role, p in gateway.calls if role == "character")
        self.assertNotIn("future_event", json.dumps(payload))
        self.assertIn("future_event", [c.evidence_ids[0] for c in self.store.memory("linmo").recent])

    def test_no_delegation_branch(self):
        scene = self.scene.model_copy(update={"plan": Plan(tasks=[])})
        gateway = MockGateway()
        result = NovelWorkflow(self.store, gateway).run(scene)
        self.assertEqual(result["phase"], "completed")
        self.assertFalse(any(role == "character" for role, _ in gateway.calls))
        self.assertEqual(read_json(self.store.path("scenes", scene.id, "accepted.json"))["turns"], [])

    def test_null_task_and_no_psychology(self):
        scene = self.scene.model_copy(update={"plan": Plan(tasks=[
            Task(character_id=None, after_event_id="ch01_seen02"),
            Task(character_id="linmo", after_event_id="ch01_seen03", need_psychology=False)])})
        result = NovelWorkflow(self.store, MockGateway()).run(scene)
        self.assertEqual(result["phase"], "completed")
        turns = read_json(self.store.path("scenes", scene.id, "accepted.json"))["turns"]
        self.assertEqual(turns[0]["result"]["private_text"], "")

    def test_style_failure_rewrites_main_prose_without_regenerating_actors(self):
        gateway = FailReview()
        result = NovelWorkflow(self.store, gateway).run(self.scene)
        self.assertEqual(result["phase"], "completed")
        self.assertEqual(sum(role == "writer" for role, _ in gateway.calls), 2)
        self.assertEqual(sum(role == "character" for role, _ in gateway.calls), 2)
        self.assertTrue(self.store.path("scenes", self.scene.id, "reviews", "round1.json").exists())

    def test_retries_are_bounded_and_pause_is_persistent(self):
        gateway = FailReview(always=True)
        result = NovelWorkflow(self.store, gateway, max_rewrites=1).run(self.scene)
        self.assertEqual(result["phase"], "awaiting_author")
        self.assertFalse(self.store.path("scenes", self.scene.id, "accepted.json").exists())
        self.assertFalse(self.store.path("manuscript", "book.md").exists())
        self.assertEqual(sum(role == "writer" for role, _ in gateway.calls), 2)
        second_gateway = FailReview(always=True)
        resumed = NovelWorkflow(self.store, second_gateway, max_rewrites=1).run(self.scene)
        self.assertEqual(resumed["phase"], "awaiting_author")
        self.assertEqual(second_gateway.calls, [])
        cancelled = NovelWorkflow(self.store, second_gateway, max_rewrites=1).run(self.scene, action="cancel")
        self.assertEqual(cancelled["phase"], "cancelled")
        self.assertFalse(self.store.path("scenes", self.scene.id, "accepted.json").exists())

    def test_accepted_rerun_is_idempotent(self):
        gateway = MockGateway()
        NovelWorkflow(self.store, gateway).run(self.scene)
        original = self.store.path("manuscript", "book.md").read_bytes()
        gateway2 = MockGateway()
        rerun = NovelWorkflow(self.store, gateway2).run(self.scene)
        self.assertTrue(rerun["cached"])
        self.assertEqual(gateway2.calls, [])
        self.assertEqual(original, self.store.path("manuscript", "book.md").read_bytes())
        self.assertEqual(self.store.memory("linmo").version, 1)

    def test_recover_after_commit_before_materialization(self):
        original = self.store.materialize
        self.store.materialize = lambda: (_ for _ in ()).throw(OSError("simulated disk failure"))
        with self.assertRaises(OSError):
            NovelWorkflow(self.store, MockGateway()).run(self.scene)
        self.assertTrue(self.store.path("scenes", self.scene.id, "accepted.json").exists())
        self.store.materialize = original
        gateway = MockGateway()
        result = NovelWorkflow(self.store, gateway).run(self.scene)
        self.assertTrue(result["cached"])
        self.assertEqual(gateway.calls, [])
        self.assertTrue(self.store.path("manuscript", "book.md").exists())

    def test_restart_after_model_failure_uses_checkpoint_and_cached_calls(self):
        class FailOnce(MockGateway):
            def generate(self, role, instruction, payload, schema):
                if role == "writer":
                    raise ModelError("temporary failure")
                return super().generate(role, instruction, payload, schema)
        with self.assertRaises(ModelError):
            NovelWorkflow(self.store, FailOnce()).run(self.scene)
        gateway = MockGateway()
        result = NovelWorkflow(self.store, gateway).run(self.scene)
        self.assertEqual(result["phase"], "completed")
        self.assertFalse(any(role == "character" for role, _ in gateway.calls))

    def test_changed_scene_cannot_overwrite_accepted_scene(self):
        NovelWorkflow(self.store, MockGateway()).run(self.scene)
        changed = self.scene.model_copy(update={"goal": "不同的场景目标"})
        with self.assertRaises(ValueError):
            NovelWorkflow(self.store, MockGateway()).run(changed)

    def test_memory_compaction_preserves_beliefs_and_sources(self):
        claims = [Claim(text=f"怀疑对方隐瞒经历{i}", kind="belief", evidence_ids=[f"seen{i}"])
                  for i in range(20)]
        workflow = NovelWorkflow(self.store, MockGateway(), recent_limit=4)
        compressed = workflow.compress(Memory(recent=claims))
        self.assertEqual(len(compressed.recent), 2)
        self.assertEqual(len(compressed.summary), 18)
        self.assertTrue(all(c.kind == "belief" for c in compressed.summary))
        self.assertEqual(compressed.summary[0].evidence_ids, ["seen0"])
        self.assertEqual(compressed.version, 0)

    def test_context_over_budget_fails_without_silently_deleting_memories(self):
        memory = Memory(recent=[Claim(text="记忆" * 1000, kind="belief", evidence_ids=["profile"])])
        with self.assertRaises(ValueError):
            build_character_context(self.story.characters[0], memory, self.scene,
                                    Task(character_id="linmo", after_event_id="ch01_seen03"), [], 2000)
        self.assertEqual(len(memory.recent), 1)

    def test_project_lock_prevents_concurrent_writers(self):
        with self.store.lock():
            with self.assertRaises(ProjectBusy):
                with FileStore(self.store.root).lock():
                    self.fail("second writer acquired lock")

    def test_path_traversal_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.path("..", "outside.json")
        with self.assertRaises(ValueError):
            SceneSpec.model_validate({**self.scene.model_dump(), "id": "../outside"})

    def test_review_payload_contains_outline_style_and_explicit_intent(self):
        gateway = MockGateway()
        NovelWorkflow(self.store, gateway).run(self.scene)
        payload = next(p for role, p in gateway.calls if role == "style")
        self.assertEqual(payload["outline"], self.story.outline)
        self.assertEqual(payload["style_sample"], self.story.style_sample)
        self.assertEqual(payload["intentional_choices"], self.scene.intentional_choices)

    def test_new_scene_sees_own_history_and_not_other_private_logs(self):
        NovelWorkflow(self.store, MockGateway()).run(self.scene)
        scene = self.scene.model_copy(deep=True)
        scene.id = "chapter01_scene02"
        for event in scene.events:
            event.id = event.id.replace("ch01", "ch02")
        gateway = MockGateway()
        NovelWorkflow(self.store, gateway).run(scene)
        a = next(p for role, p in gateway.calls if role == "character" and p["character"]["id"] == "linmo")
        self.assertEqual(a["memory"]["version"], 1)
        self.assertTrue(any("心虚" in c["text"] for c in a["memory"]["recent"]))
        self.assertNotIn("中毒", json.dumps(a, ensure_ascii=False))
        self.assertEqual(self.store.memory("linmo").version, 2)


if __name__ == "__main__":
    unittest.main()
