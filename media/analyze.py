from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MediaAnalysis:
    kind: str
    summary: str
    ocr_text: str
    visual_elements: list[str]
    actions_or_mechanics: list[str]
    suggested_intent: str
    fallback_used: bool = False
