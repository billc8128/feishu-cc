from __future__ import annotations

from media.analyze import MediaAnalysis
from media.ingest import MediaAttachment


def build_media_turn_prompt(
    *,
    text: str,
    attachments: list[MediaAttachment],
    analyses: list[MediaAnalysis | None],
) -> str:
    lines = [
        "用户发送了一条飞书消息。",
        "",
        "附带文字:",
        text or "(空)",
        "",
    ]

    for index, attachment in enumerate(attachments, start=1):
        analysis = analyses[index - 1] if index - 1 < len(analyses) else None
        lines.extend(
            [
                f"附件 {index}:",
                f"- 类型: {attachment.kind}",
                f"- 文件路径: {attachment.local_path}",
                f"- 原始文件名: {attachment.original_name}",
            ]
        )
        if analysis is None:
            lines.append(
                "- 媒体分析: 媒体分析失败；不要直接读取图片/视频作为模型输入。"
                "请基于用户文字继续，必要时让用户补充描述或配置可用的多模态分析接口。"
            )
        else:
            lines.extend(
                [
                    f"- 媒体分析摘要: {analysis.summary}",
                    f"- OCR: {analysis.ocr_text or '(无)'}",
                    f"- 关键元素/动作: {_join_items(analysis.visual_elements, analysis.actions_or_mechanics)}",
                ]
            )
        lines.append("")

    lines.extend(
        [
            "请结合当前对话上下文决定如何回复。",
            "如果上下文足够，直接继续任务。",
            "如果上下文不足，再向用户追问。",
        ]
    )
    return "\n".join(lines)


def _join_items(visual_elements: list[str], actions_or_mechanics: list[str]) -> str:
    items = [*visual_elements, *actions_or_mechanics]
    if not items:
        return "(无)"
    return " / ".join(items)
