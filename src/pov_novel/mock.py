"""Deterministic, offline demo backend. Not a substitute for literary evaluation."""
from .models import CharacterResult, Claim, CompressedMemory, Draft, Plan, Review, Task


class MockGateway:
    identity = "offline-demo-v1"

    def __init__(self):
        self.calls = []

    def generate(self, role, instruction, payload, schema):
        self.calls.append((role, payload))
        if role == "importer":
            # Explicitly synthetic: characters are only detected for our bundled sample.
            from .importer import SourceAnalysis, ExtractedCharacter, SupportedFact, SupportedMemory
            text = payload["source_text"]
            characters = []
            for name in ("林墨", "楚瑶"):
                if name not in text:
                    continue
                lines = [line for line in text.splitlines() if name in line]
                secret_lines = [line for line in lines if "中毒" in line]
                own_lines = [line for line in lines if line.startswith(name + "看见")]
                belief_lines = [line for line in lines if line.startswith(name + "怀疑")]
                secrets = [SupportedFact(text=line, quote=line) for line in secret_lines] if name == "楚瑶" else []
                memories = [SupportedMemory(text=line, quote=line, kind="observation") for line in own_lines]
                memories += [SupportedMemory(text=line, quote=line, kind="belief") for line in belief_lines]
                characters.append(ExtractedCharacter(name=name, secrets=secrets, memories=memories,
                    personality=SupportedFact(text="离线演示：性格待真实模型提取", quote=lines[0])))
            return SourceAnalysis(outline="离线演示：只用于验证导入流程，请用真实模型重新提取。",
                                  style="离线演示：文风待真实模型提取。", characters=characters,
                                  uncertainties=["当前输出是离线演示，未进行真正的小说分析。"])
        if role == "director":
            scene = payload["scene"]
            return Plan(tasks=[Task(character_id=cid, after_event_id=scene["events"][-1]["id"])
                               for cid in reversed(scene["present_characters"])])
        if role == "character":
            card = payload["character"]
            observations = payload["observations"]
            evidence = [observations[-1]["id"]] if observations else ["profile"]
            if card["known_secrets"]:
                thought = f"{card['known_secrets'][0]} 不能让眼前的人知道。先把话说稳。"
                action = f"{card['name']}把杯子放回桌上，手缩进袖口。"
                dialogue = "“不过是替人送点东西。”"
                claim = Claim(text="对方可能在试探自己，暂时不能透露秘密", kind="belief", evidence_ids=evidence)
            else:
                thought = "他把手藏了起来。只问了一句，便这样紧张？莫非他在心虚。"
                action = f"{card['name']}没有接话，目光停在对方的袖口。"
                dialogue = ""
                claim = Claim(text="怀疑对方因隐瞒事情而心虚，尚未证实", kind="belief", evidence_ids=evidence, confidence="high")
            return CharacterResult(private_text=thought if payload["need_psychology"] else "",
                                   public_action=action, public_dialogue=dialogue,
                                   memories=[claim], emotion="戒备", current_goal=card["current_goal"])
        if role == "writer":
            paragraphs = [e["text"] for e in payload["visible_events"]]
            paragraphs.extend(payload["pov_psychology"])
            return Draft(prose="\n\n".join(paragraphs))
        if role in ("style", "outline", "editor"):
            common = {'issues': [], 'summary': f"离线演示：{role} 接口通过。此评分不是模型的文学评判。"}
            if schema is Review:
                return Review(score=9, **common)
            if role == 'style':
                return schema(consistency=9, genre=9, voice=9, **common)
            if role == 'outline':
                return schema(status='PASS', **common)
            return schema(before_quality=9 if payload.get('previous_draft') else None, after_quality=9, **common)
        if role == 'state':
            from .story_state import RuntimeDelta
            return RuntimeDelta(summary='离线演示摘要；请使用真实模型总结已经发生的事件。')
        if role == 'retrieval':
            return schema(ids=[item['scene_id'] for item in payload['candidates'][:payload['max_results']]])
        if role == "context":
            from .semantic_context import CompiledHistory
            # Synthetic context, only for exercising budget/trace contracts offline.
            content = "离线摘要；请使用真实模型编译历史资料。"
            return CompiledHistory(content=content, source_ids=payload["source_ids"])
        if role == "memory":
            # Preserve claims and their epistemic type exactly in demo mode.
            unique = []
            retained = []
            for index, raw in enumerate(payload["memories"]):
                claim = Claim.model_validate(raw)
                if claim not in unique:
                    unique.append(claim)
                    if 'memory_ids' in payload and len(retained) < 30:
                        retained.append(payload['memory_ids'][index])
            if schema.__name__ == 'MemoryCompilation':
                return schema(retained_ids=retained)
            return CompressedMemory(summary=unique[:30])
        raise ValueError(f"未知演示角色 {role}")
