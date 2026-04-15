from __future__ import annotations

import mimetypes
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
        saved, detected_mime_type = _finalize_downloaded_file(saved, attachment.kind)

        stored.append(
            MediaAttachment(
                kind=attachment.kind,
                original_name=original_name,
                local_path=saved,
                mime_type=_resolve_mime_type(saved, detected_mime_type, attachment.file_type),
                size_bytes=saved.stat().st_size,
            )
        )
    return stored


def _timestamp_prefix() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S%f")


def _finalize_downloaded_file(path: Path, kind: str) -> tuple[Path, str | None]:
    sniffed_suffix, sniffed_mime_type = _sniff_media_signature(path.read_bytes(), kind)
    if sniffed_suffix and not path.suffix:
        renamed = path.with_name(f"{path.name}{sniffed_suffix}")
        path.rename(renamed)
        path = renamed
    return path, sniffed_mime_type


def _resolve_mime_type(
    path: Path,
    sniffed_mime_type: str | None,
    file_type: str | None,
) -> str | None:
    if sniffed_mime_type:
        return sniffed_mime_type

    guessed = mimetypes.guess_type(path.name)[0]
    if guessed:
        return guessed

    if file_type:
        return mimetypes.guess_type(f"file.{file_type.lower()}")[0]
    return None


def _sniff_media_signature(data: bytes, kind: str) -> tuple[str | None, str | None]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif", "image/gif"
    if data.startswith(b"BM"):
        return ".bmp", "image/bmp"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp", "image/webp"

    if kind == "video" and len(data) >= 12 and data[4:8] == b"ftyp":
        return ".mp4", "video/mp4"
    if kind == "video" and data.startswith(b"\x1a\x45\xdf\xa3"):
        return ".webm", "video/webm"

    return None, None
