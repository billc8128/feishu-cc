from __future__ import annotations

import base64
import json
import mimetypes
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from config import settings
from media.ingest import MediaAttachment


@dataclass
class MediaAnalysis:
    kind: str
    summary: str
    ocr_text: str
    visual_elements: list[str]
    actions_or_mechanics: list[str]
    suggested_intent: str
    fallback_used: bool = False


class MediaAnalyzer:
    async def analyze(
        self,
        attachment: MediaAttachment,
        user_text: str,
    ) -> MediaAnalysis:
        if attachment.kind == "video":
            return await self._analyze_video(attachment, user_text)

        payload = await self._call_glm_vision(
            media_kind="image",
            paths=[attachment.local_path],
            user_text=user_text,
        )
        return self._normalize_analysis(
            kind=attachment.kind,
            payload=payload,
            fallback_used=False,
        )

    async def _analyze_video(
        self,
        attachment: MediaAttachment,
        user_text: str,
    ) -> MediaAnalysis:
        try:
            payload = await self._call_glm_vision(
                media_kind="video",
                paths=[attachment.local_path],
                user_text=user_text,
            )
            return self._normalize_analysis(
                kind="video",
                payload=payload,
                fallback_used=False,
            )
        except Exception:
            frames = self._extract_video_frames(attachment.local_path)
            if not frames:
                raise
            payload = await self._call_glm_vision(
                media_kind="image",
                paths=frames,
                user_text=user_text,
            )
            return self._normalize_analysis(
                kind="video",
                payload=payload,
                fallback_used=True,
            )

    async def _call_glm_vision(
        self,
        *,
        media_kind: str,
        paths: list[Path],
        user_text: str,
    ) -> dict:
        content = [{"type": "text", "text": _analysis_prompt(user_text)}]
        for path in paths:
            url = _path_to_data_url(path)
            if media_kind == "video":
                # `video_url` is inferred from Z.AI's multimodal chat-completions
                # schema style; if the provider rejects it, callers fall back to
                # frame extraction.
                content.append({"type": "video_url", "video_url": {"url": url}})
            else:
                content.append({"type": "image_url", "image_url": {"url": url}})

        body = {
            "model": settings.glm_vision_model,
            "messages": [{"role": "user", "content": content}],
            "thinking": {"type": "disabled"},
            "temperature": 0.1,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {settings.anthropic_auth_token}",
            "Content-Type": "application/json",
        }
        timeout = max(int(settings.api_timeout_ms) / 1000, 30)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                settings.glm_vision_base_url,
                headers=headers,
                json=body,
            )
            response.raise_for_status()

        data = response.json()
        message = data["choices"][0]["message"]["content"]
        if isinstance(message, list):
            text_chunks: list[str] = []
            for chunk in message:
                if isinstance(chunk, dict) and chunk.get("type") == "text":
                    text_chunks.append(chunk.get("text", ""))
            message = "\n".join(text_chunks).strip()

        if isinstance(message, dict):
            return message

        try:
            return json.loads(message)
        except (TypeError, json.JSONDecodeError):
            return {"summary": str(message).strip()}

    def _extract_video_frames(self, video_path: Path) -> list[Path]:
        with tempfile.TemporaryDirectory(prefix="feishu-media-frames-") as tmp:
            output_pattern = str(Path(tmp) / "frame-%03d.png")
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(video_path),
                    "-vf",
                    "fps=1/3",
                    "-frames:v",
                    "3",
                    output_pattern,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "ffmpeg frame extraction failed")

            frame_paths = sorted(Path(tmp).glob("frame-*.png"))
            if not frame_paths:
                raise RuntimeError("ffmpeg produced no frames")

            persisted_frames: list[Path] = []
            for frame_path in frame_paths:
                target = video_path.parent / frame_path.name
                target.write_bytes(frame_path.read_bytes())
                persisted_frames.append(target)
            return persisted_frames

    def _normalize_analysis(
        self,
        *,
        kind: str,
        payload: dict,
        fallback_used: bool,
    ) -> MediaAnalysis:
        return MediaAnalysis(
            kind=kind,
            summary=str(payload.get("summary", "")).strip(),
            ocr_text=str(payload.get("ocr_text", "")).strip(),
            visual_elements=_ensure_str_list(payload.get("visual_elements")),
            actions_or_mechanics=_ensure_str_list(payload.get("actions_or_mechanics")),
            suggested_intent=str(payload.get("suggested_intent", "")).strip(),
            fallback_used=fallback_used,
        )


def _analysis_prompt(user_text: str) -> str:
    task_hint = user_text.strip() or "请分析这份媒体内容并总结对当前任务有用的信息。"
    return (
        "请分析用户发来的媒体内容，并返回严格 JSON。"
        'JSON 必须包含 keys: summary, ocr_text, visual_elements, '
        "actions_or_mechanics, suggested_intent。"
        "其中 visual_elements 和 actions_or_mechanics 必须是字符串数组。"
        f"用户附加说明: {task_hint}"
    )


def _path_to_data_url(path: Path) -> str:
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _ensure_str_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []
