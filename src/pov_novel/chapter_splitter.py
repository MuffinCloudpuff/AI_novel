"""Python port of InkOS chapter-splitter.ts (AGPL-3.0); see vendor/inkos.

Local additions preserve preambles and exact source slices for import provenance.
"""
import re
from dataclasses import dataclass

HEADING = re.compile(
    r'^#{0,6}\s*(?:第([零〇○Ｏ０一二三四五六七八九十百千万\d]+)(章|回)(?:[:：]|\s+)?\s*(.*)'
    r'|Chapter\s+(\d+|[IVXLCDM]+)[.:]?\s*(.*))$', re.I)


@dataclass
class SourceChapter:
    title: str
    content: str
    raw: str
    number: int | None = None
    preface: bool = False


def ordinal_number(value, english=False):
    if value.isdecimal():
        return int(value)
    if english:
        digits = [dict(I=1, V=5, X=10, L=50, C=100, D=500, M=1000)[c] for c in value.upper()]
        return sum(-digit if i + 1 < len(digits) and digit < digits[i + 1] else digit
                   for i, digit in enumerate(digits))
    digits = dict(zip('零〇○Ｏ０一二三四五六七八九', [0, 0, 0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9]))
    units = {'十': 10, '百': 100, '千': 1000, '万': 10000}
    total = section = number = 0
    for char in value:
        unit = units.get(char)
        if unit is None:
            number = number * 10 + (digits[char] if char in digits else int(char))
        elif unit == 10000:
            total += (section + number or 1) * unit
            section = number = 0
        else:
            section += (number or 1) * unit
            number = 0
    return total + section + number


def read_heading(line):
    match = HEADING.fullmatch(line.strip())
    if match is None:
        return None
    value, unit, title, en_value, en_title = match.groups()
    number = ordinal_number(value or en_value, english=value is None)
    title = (title if value is not None else en_title).strip()
    fallback = f'第{number}{unit}' if value is not None else f'Chapter {number}'
    return number, title or fallback


def split_chapters(text, fallback='正文'):
    lines = text.splitlines(keepends=True)
    heads = []
    for i, line in enumerate(lines):
        heading = read_heading(line)
        if heading is None:
            continue
        number, title = heading
        # Adjacent aliases describe one chapter; distinct empty chapters remain.
        if heads and heads[-1][1] == number and not ''.join(lines[heads[-1][0] + 1:i]).strip():
            heads[-1][3] = i
            continue
        heads.append([i, number, title, i])
    if not heads:
        return [SourceChapter(fallback, text.strip(), text)] if text.strip() else []
    result = []
    prefix = ''.join(lines[:heads[0][0]])
    if prefix.strip():
        result.append(SourceChapter('前置正文', prefix.strip(), prefix, preface=True))
    for i, (start, number, title, content_start) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(lines)
        result.append(SourceChapter(title, ''.join(lines[content_start + 1:end]).strip(),
                                    ''.join(lines[start:end]), number))
    return result


def natural_key(name):
    return tuple((1, int(part)) if part.isdecimal() else (0, part.casefold())
                 for part in re.split(r'(\d+)', name))
