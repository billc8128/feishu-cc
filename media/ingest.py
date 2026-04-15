from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from feishu.events import IncomingAttachment


@dataclass
class MediaAttachment:
    kind: str
    original_name: str
    local_path: Path
    mime_type: str | None = None
    size_bytes: int | None = None


def sanitize_filename(name: str | None, default_stem: str = "attachment") -> str:
    candidate = Path(name or "").name
    stem = Path(candidate).stem or default_stem
    suffix = Path(candidate).suffix

    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._")
    if not safe_stem:
        safe_stem = default_stem

    safe_suffix = re.sub(r"[^A-Za-z0-9.]+", "", suffix.lower())
    return f"{safe_stem}{safe_suffix}"


async def ingest_attachments(
    *,
    feishu,
    project_root: Path,
    message_id: str,
    attachments: list[IncomingAttachment],
) -> list[MediaAttachment]:
    inbox_dir = project_root / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)

    stored: list[MediaAttachment] = []
    for index, attachment in enumerate(attachments, start=1):
        original_name = attachment.file_name or f"{attachment.kind}-{index}"
        safe_name = sanitize_filename(original_name, f"{attachment.kind}-{index}")
        destination = inbox_dir / f"{_timestamp_prefix()}-{safe_name}"
        saved = await feishu.download_message_resource(
            message_id=message_id,
            file_key=attachment.file_key,
            resource_type=attachment.message_resource_type,
            destination=destination,
        )
        if not saved:
            continue

        stored.append(
            MediaAttachment(
                kind=attachment.kind,
                original_name=original_name,
                local_path=saved,
                mime_type=attachment.file_type,
                size_bytes=saved.stat().st_size,
            )
        )
    return stored


def _timestamp_prefix() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S%f")
