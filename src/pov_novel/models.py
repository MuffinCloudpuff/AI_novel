from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")]
Text = Annotated[str, Field(min_length=1, max_length=16000)]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Claim(Record):
    text: Text
    kind: Literal["observation", "belief"]
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    confidence: Literal["low", "medium", "high"] = "medium"


class CharacterCard(Record):
    id: Identifier
    name: Text
    personality: Text
    voice: str = ""
    # Cumulative project data, not a single model response. Long novels can
    # establish more than 30 secrets; context/output budgets apply at use sites.
    known_secrets: list[str] = Field(default_factory=list)
    current_goal: str = ""
    initial_memories: list[Claim] = Field(default_factory=list, max_length=100)


class StoryConfig(Record):
    title: Text
    outline: Text
    style: Text
    style_sample: str = Field(default="", max_length=16000)
    # The book-wide catalog grows across chapters; scene/model limits are
    # enforced separately and must not truncate archived cast members.
    characters: list[CharacterCard] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_characters(self):
        ids = [card.id for card in self.characters]
        if len(ids) != len(set(ids)):
            raise ValueError("人物 ID 重复")
        return self


class Event(Record):
    id: Identifier
    text: Text
    visible_to: list[Identifier] = Field(default_factory=list, max_length=50)


class Task(Record):
    character_id: Identifier | None = None
    after_event_id: Identifier
    purpose: Literal["react", "speak", "decide"] = "react"
    need_psychology: bool = True


class Plan(Record):
    tasks: list[Task] = Field(default_factory=list, max_length=12)


class SceneSpec(Record):
    id: Identifier
    pov_character_id: Identifier
    setting: Text
    goal: Text
    present_characters: list[Identifier] = Field(min_length=1, max_length=30)
    events: list[Event] = Field(min_length=1, max_length=100)
    # An explicit plan bypasses the director's planning call. An empty plan
    # exercises the no-delegation branch without inventing character thoughts.
    plan: Plan | None = None
    intentional_choices: list[str] = Field(default_factory=list, max_length=30)
    chapter: int | None = Field(default=None, ge=1)
    min_characters: int | None = Field(default=None, ge=1)
    max_characters: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_scene(self):
        if self.min_characters and self.max_characters and self.min_characters > self.max_characters:
            raise ValueError("正文最少字数不能超过最多字数")
        ids = [event.id for event in self.events]
        if len(ids) != len(set(ids)):
            raise ValueError("事件 ID 重复")
        if len(self.present_characters) != len(set(self.present_characters)):
            raise ValueError("在场人物重复")
        if self.pov_character_id not in self.present_characters:
            raise ValueError("视角人物必须在当前场景")
        return self


class Memory(Record):
    schema_version: Literal[2] = 2
    version: int = 0
    # Old memory is compressed to structured claims, never an omniscient summary.
    summary: list[Claim] = Field(default_factory=list)
    recent: list[Claim] = Field(default_factory=list)
    emotion: str = ""
    current_goal: str = ""

    @model_validator(mode='before')
    @classmethod
    def upgrade_schema(cls, value):
        if isinstance(value, dict) and value.get('schema_version') == 1:
            return {**value, 'schema_version': 2}
        return value


class CharacterContext(Record):
    character: CharacterCard
    memory: Memory
    setting: str
    observations: list[Event]
    purpose: Literal["react", "speak", "decide"]
    need_psychology: bool


class CharacterResult(Record):
    private_text: str = Field(default="", max_length=6000)
    public_action: str = Field(default="", max_length=4000)
    public_dialogue: str = Field(default="", max_length=4000)
    memories: list[Claim] = Field(default_factory=list, max_length=15)
    emotion: str = Field(default="", max_length=500)
    current_goal: str = Field(default="", max_length=1000)


class Draft(Record):
    prose: str = Field(min_length=1)


class SourceRef(Record):
    source_id: str = Field(min_length=1, max_length=160)
    quote: str = Field(min_length=1, max_length=2000)


class Issue(Record):
    severity: Literal["minor", "major", "critical"]
    location: str
    description: Text
    # Legacy disk/mock compatibility only. Live review schemas require refs.
    evidence: str = ""
    source_refs: list[SourceRef] = Field(default_factory=list, max_length=10)
    category: str = Field(default="review", pattern=r"^[a-zA-Z0-9_-]+$")
    entity_id: str = Field(default="scene", pattern=r"^[a-zA-Z0-9_-]+$")
    suggestion: str


class Review(Record):
    score: float = Field(ge=0, le=10)
    issues: list[Issue] = Field(default_factory=list, max_length=30)
    summary: str


class GroundedIssue(Record):
    severity: Literal["minor", "major", "critical"]
    location: str
    description: Text
    source_refs: list[SourceRef] = Field(min_length=1, max_length=10)
    suggestion: str
    category: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    entity_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")


class StyleReview(Record):
    consistency: float = Field(ge=0, le=10)
    genre: float = Field(ge=0, le=10)
    voice: float = Field(ge=0, le=10)
    issues: list[GroundedIssue] = Field(default_factory=list, max_length=30)
    summary: str


class ContinuityReview(Record):
    status: Literal["PASS", "WARNING", "FAIL"]
    issues: list[GroundedIssue] = Field(default_factory=list, max_length=30)
    summary: str


class EditorReview(Record):
    before_quality: float | None = Field(ge=0, le=10)
    after_quality: float = Field(ge=0, le=10)
    issues: list[GroundedIssue] = Field(default_factory=list, max_length=30)
    summary: str


class CompressedMemory(Record):
    summary: list[Claim] = Field(max_length=30)


class BeliefSummary(Record):
    text: Text
    source_memory_ids: list[Identifier] = Field(min_length=1, max_length=30)
    confidence: Literal['low', 'medium', 'high'] = 'medium'


class MemoryCompilation(Record):
    retained_ids: list[Identifier] = Field(default_factory=list, max_length=30)
    belief_summaries: list[BeliefSummary] = Field(default_factory=list, max_length=30)

    @model_validator(mode='after')
    def bounded_summary(self):
        if len(self.retained_ids) + len(self.belief_summaries) > 30:
            raise ValueError('记忆摘要合计最多30项')
        return self
