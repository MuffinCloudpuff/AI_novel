"""Read independent author files; assemble StoryConfig only in memory."""
from pathlib import Path

from pydantic import Field, model_validator

from .models import CharacterCard, Identifier, Record, StoryConfig, Text
from .store import read_json, write_json


class ProjectIndex(Record):
    schema_version: int = Field(default=2, ge=1, le=2)
    title: Text
    characters: list[Identifier] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_ids(self):
        if len(self.characters) != len(set(self.characters)):
            raise ValueError("项目索引中人物 ID 重复")
        return self


class OutlineFile(Record):
    outline: Text


class StyleFile(Record):
    style: Text


class ReferenceFile(Record):
    style_sample: str = Field(default="", max_length=16000)


def load_story(source):
    source = Path(source)
    if source.is_file() and source.name != "project.json":
        # Compatibility with the previous all-in-one format.
        return StoryConfig.model_validate(read_json(source))
    folder = source.parent if source.is_file() else source
    if not (folder / "project.json").exists():
        return StoryConfig.model_validate(read_json(folder / "story.json"))
    index = ProjectIndex.model_validate(read_json(folder / "project.json"))
    outline = OutlineFile.model_validate(read_json(folder / "outline.json"))
    style = StyleFile.model_validate(read_json(folder / "style.json"))
    reference = ReferenceFile.model_validate(read_json(folder / "reference.json"))
    cards = []
    for cid in index.characters:
        path = (folder / "characters" / cid / "card.json").resolve()
        if not path.is_relative_to(folder.resolve()):
            raise ValueError("人物卡路径超出设定目录")
        card = CharacterCard.model_validate(read_json(path))
        if card.id != cid:
            raise ValueError(f"人物卡 ID 与项目索引不一致：{cid}")
        cards.append(card)
    return StoryConfig(title=index.title, outline=outline.outline, style=style.style,
                       style_sample=reference.style_sample, characters=cards)


def story_files(config):
    files = {f'characters/{card.id}/card.json': card.model_dump() for card in config.characters}
    files.update({'outline.json': {'outline': config.outline}, 'style.json': {'style': config.style},
                  'reference.json': {'style_sample': config.style_sample},
                  'project.json': {'schema_version': 2, 'title': config.title,
                                   'characters': [card.id for card in config.characters]}})
    return files


def write_story(folder, config):
    folder = Path(folder).resolve()
    for card in config.characters:
        path = (folder / "characters" / card.id / "card.json").resolve()
        if not path.is_relative_to(folder):
            raise ValueError("人物卡路径超出设定目录")
    from .transactions import atomic_file_set, json_files
    atomic_file_set(folder, json_files(story_files(config)))
