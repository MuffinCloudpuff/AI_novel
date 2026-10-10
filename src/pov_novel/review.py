from pathlib import Path

from .models import Review, StyleReview, ContinuityReview, EditorReview, SourceRef

VENDOR = Path(__file__).parent / "vendor" / "novel_os"
ROLES = {"style": "style_curator.md", "outline": "continuity_guardian.md", "editor": "editor.md"}
SCHEMAS = {'style': StyleReview, 'outline': ContinuityReview, 'editor': EditorReview}


def review_instruction(role):
    # Keep the upstream methodology intact on disk, but replace its Markdown
    # output contract with our validated JSON interface. Never mutate upstream.
    original = (VENDOR / ROLES[role]).read_text(encoding="utf-8")
    methodology = original.split("## Response Format", 1)[0]
    focus = {
        "style": "重点检查目标文风、参考正文、叙述距离、句式节奏、用词和角色口吻的一致性。",
        "outline": "重点检查是否偏离全书总纲、当前场景目标、情节红线和前文已确定的事件。",
        "editor": "以 developmental 模式检查结构、节奏和场景推进，提出建议，不代写正文。",
    }[role]
    return methodology + "\n\n本项目审核适配契约（覆盖上游输出格式与编辑动作）：\n" + focus + "\n" + (
        "只评审，返回角色对应 JSON：style 保留 consistency/genre/voice 三个0到10分；"
        "outline 返回 PASS/WARNING/FAIL；editor 返回 before_quality/after_quality，"
        "首次评审 before_quality=null，重写后评价实际 previous_draft 与 draft，不评价不存在的假想改稿。"
        "issues 每项包含 severity、location、description、source_refs、suggestion、category、entity_id。"
        "sources 是具名来源目录：键为 source_id，path 指向输入 JSON 中对应字符串（用点分隔对象键或数组下标）。"
        "source_refs 每项必须包含该目录真实 source_id 和 path 对应文本内连续逐字 quote。"
        "category/entity_id 为稳定 ASCII 键，不能把措辞或章节号编进 category。不得使用自由文本 evidence。"
        "deterministic_findings 是程序检查结果；dismissals 是作者持久决定，已豁免事项不要再次阻塞。"
        "不输出改写后的正文或上游 STATE_UPDATE 块。不要把英文写作范式机械套到中文。"
        "总纲允许有意的信息差、误解、谎言和不可靠判断；这些不等于剧情错误。"
        "intentional_choices 是作者明确的创作意图。审稿建议不得要求角色知道其他人的心理。"
        "检查当前 POV；后台私密心理不是已对读者揭示的事实。不要要求当前场景"
        "提前完成后续章节的伏笔回收。修改建议只引用已提供的正文或允许叙述的素材，"
        "不要把 world/private 的隐秘事实写进建议。"
    )


def passed(reviews, threshold):
    for report in reviews.values():
        if any(issue.severity in ('critical', 'major') for issue in report.issues):
            return False
        if isinstance(report, StyleReview) and min(report.consistency, report.genre, report.voice) < threshold:
            return False
        if isinstance(report, ContinuityReview) and report.status == 'FAIL':
            return False
        if isinstance(report, EditorReview) and report.after_quality < threshold:
            return False
        if isinstance(report, Review) and report.score < threshold:
            return False
    return True


def verify_refs(refs, sources):
    for ref in refs:
        ref = ref.model_dump() if hasattr(ref, 'model_dump') else ref
        if ref['source_id'] not in sources:
            raise ValueError('审核/状态引用未知来源：' + ref['source_id'])
        if not ref['quote'] or ref['quote'] not in sources[ref['source_id']]['text']:
            raise ValueError('审核/状态引用不是指定来源的连续逐字原文')


def normalize_review(role, report, sources):
    """Legacy saved/test reports are upgraded and still pass the evidence gate."""
    if isinstance(report, Review):
        issues = []
        for issue in report.issues:
            data = issue.model_dump(exclude={'evidence'})
            if not data['source_refs']:
                matches = [sid for sid, source in sources.items() if issue.evidence and issue.evidence in source['text']]
                if not matches:
                    raise ValueError('旧审核证据无法对应本次来源')
                data['source_refs'] = [SourceRef(source_id=matches[0], quote=issue.evidence).model_dump()]
            issues.append(data)
        common = {'issues': issues, 'summary': report.summary}
        if role == 'style':
            report = StyleReview(**common, consistency=report.score, genre=report.score, voice=report.score)
        elif role == 'outline':
            report = ContinuityReview(**common, status='PASS' if report.score >= 7 and not issues else 'FAIL' if report.score < 7 else 'WARNING')
        else:
            report = EditorReview(**common, before_quality=None, after_quality=report.score)
    for issue in report.issues:
        verify_refs(issue.source_refs, sources)
    return report


def writer_feedback(reviews, sources):
    """Never return author-only findings/suggestions to the POV composer."""
    result = {}
    for role, report in reviews.items():
        issues = [issue.model_dump() for issue in report.issues if all(sources[r.source_id]['writer_visible'] for r in issue.source_refs)]
        # Free-text descriptions/suggestions could import hidden author knowledge.
        # Writer receives only safe exact quotes and structural locations.
        result[role] = {'issues': [{'severity': i['severity'], 'location': i['location'],
                                   'source_refs': i['source_refs']} for i in issues]}
    return result
