# Feishu Media Input Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add image and video input support for Feishu private chats by downloading media into the current project sandbox, analyzing it with `GLM-5V-Turbo`, and folding the result into the existing agent turn without changing session ownership.

**Architecture:** Extend the current text-only Feishu ingress into a richer parsed message model with attachments, then add a separate media pipeline that downloads inbound resources into `inbox/`, analyzes them through a dedicated VLM helper, and converts the result into a stable text envelope for the existing `ClaudeSDKClient` flow. Keep the main agent/session path text-based in phase 1 so current resume, tool, and project behavior stays intact while media understanding remains isolated behind focused modules.

**Tech Stack:** Python 3.12, FastAPI, `lark-oapi`, `httpx`, `claude-agent-sdk`, `unittest`, Railway Docker deploy, `ffmpeg` for video fallback.

---

## File Structure

### Existing files to modify

- `config.py`
  Add only the media-analysis configuration needed for phase 1. Keep it minimal: VLM model name, optional VLM endpoint override if required by Zhipu’s VLM API, and media limits if a constant genuinely improves clarity.

- `feishu/events.py`
  Replace the text-only parsed event model with a richer message model that can represent text plus attachments. Parsing remains defensive and still ignores unsupported event types.

- `feishu/client.py`
  Add inbound message-resource download support on top of the existing outbound upload/send wrapper. Keep protocol-specific code here instead of leaking Feishu SDK calls into business logic.

- `app.py`
  Keep webhook routing unchanged, but pass the richer parsed event into dispatch and then into the agent layer.

- `agent/runner.py`
  Keep session pooling/resume intact. Add a media-aware entrypoint that accepts text plus attachments, invokes the media pipeline, and still sends one textual turn to the SDK.

- `Dockerfile`
  Install `ffmpeg` so video fallback is real, not hypothetical.

### New files to create

- `media/__init__.py`
  Minimal exports only.

- `media/ingest.py`
  Translate `IncomingAttachment` items into sandboxed local files under `<project>/inbox/`, sanitize names, classify kinds, and return normalized `MediaAttachment` objects.

- `media/analyze.py`
  Encapsulate `GLM-5V-Turbo` calls plus video fallback behavior. This module owns the provider-specific request/response mapping and must return a normalized `MediaAnalysis`.

- `media/prompting.py`
  Turn user text plus analyzed attachments into the stable textual prompt envelope consumed by `agent.runner`.

### Test files to create

- `tests/test_feishu_events.py`
  Parser coverage for text, image, video-file, mixed-content metadata, and unsupported message types.

- `tests/test_feishu_client.py`
  Inbound message-resource download helper tests with Feishu SDK calls mocked.

- `tests/test_media_ingest.py`
  Sandbox path, filename sanitization, and saved-file metadata tests.

- `tests/test_media_prompting.py`
  Prompt envelope formatting tests for text-only, pure-media, mixed-media, and degraded-analysis cases.

- `tests/test_media_analyze.py`
  VLM response normalization and video fallback decision tests with all network/process calls mocked.

- `tests/test_media_flow.py`
  End-to-end integration coverage from parsed attachments to the text passed into `client.query`, while protecting current session behavior.

## Implementation Notes

- Keep phase 1 scoped to `p2p` chats only, matching current behavior.
- Accept up to `4` images or `1` video per inbound message. Reject or ignore anything above this limit early with a clear user-facing message.
- Save inbound files under `project_root / "inbox"`.
- Never rely on provider-native multimodal session support in `claude-agent-sdk` for phase 1. The agent still receives text.
- If media analysis fails, do not fail the turn. Degrade to “file saved + analysis failed” and continue.
- Use `subprocess.run([...], check=True/False)` or equivalent argument lists for `ffmpeg`; do not shell out with a string.
- Do not alter the existing `(open_id, project)` session ownership model.

### Task 1: Expand Feishu Event Parsing

**Files:**
- Modify: `feishu/events.py`
- Test: `tests/test_feishu_events.py`

- [ ] **Step 1: Write the failing parser tests**

```python
import unittest

from feishu.events import parse_message_event


class ParseMessageEventTests(unittest.TestCase):
    def test_parses_text_message_without_attachments(self) -> None:
        body = {
            "header": {"event_type": "im.message.receive_v1", "event_id": "evt-1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_123"}},
                "message": {
                    "chat_id": "oc_1",
                    "chat_type": "p2p",
                    "message_id": "om_1",
                    "message_type": "text",
                    "content": "{\"text\": \"你好\"}",
                },
            },
        }
        parsed = parse_message_event(body)
        self.assertEqual(parsed.text, "你好")
        self.assertEqual(parsed.attachments, [])

    def test_parses_image_message_as_attachment(self) -> None:
        body = {
            "header": {"event_type": "im.message.receive_v1", "event_id": "evt-2"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_123"}},
                "message": {
                    "chat_id": "oc_1",
                    "chat_type": "p2p",
                    "message_id": "om_2",
                    "message_type": "image",
                    "content": "{\"image_key\": \"img_key\"}",
                },
            },
        }
        parsed = parse_message_event(body)
        self.assertEqual(parsed.text, "")
        self.assertEqual(parsed.attachments[0].kind, "image")
        self.assertEqual(parsed.attachments[0].file_key, "img_key")

    def test_parses_video_file_message_as_video_attachment(self) -> None:
        body = {
            "header": {"event_type": "im.message.receive_v1", "event_id": "evt-3"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_123"}},
                "message": {
                    "chat_id": "oc_1",
                    "chat_type": "p2p",
                    "message_id": "om_3",
                    "message_type": "file",
                    "content": "{\"file_key\": \"file_key\", \"file_name\": \"clip.mp4\"}",
                },
            },
        }
        parsed = parse_message_event(body)
        self.assertEqual(parsed.attachments[0].kind, "video")
```

- [ ] **Step 2: Run the parser tests to verify they fail**

Run: `python -m unittest tests.test_feishu_events -v`
Expected: FAIL because `ParsedMessageEvent` has no `attachments` field and non-text messages currently return `None`.

- [ ] **Step 3: Implement the minimal parser changes**

```python
@dataclass
class IncomingAttachment:
    kind: str
    file_key: str
    message_resource_type: str
    file_name: str | None = None
    file_type: str | None = None


@dataclass
class ParsedMessageEvent:
    event_id: str
    sender_open_id: str
    chat_id: str
    chat_type: str
    message_id: str
    text: str
    attachments: list[IncomingAttachment]
```

Implement helpers in `feishu/events.py`:

- `_parse_text_content(message: dict) -> str`
- `_parse_image_attachment(message: dict) -> IncomingAttachment | None`
- `_parse_file_attachment(message: dict) -> IncomingAttachment | None`
- `_classify_file_kind(file_name: str | None, file_type: str | None) -> str`

Behavior:

- `text` messages return `attachments=[]`
- `image` messages return one `IncomingAttachment(kind="image", ...)`
- `file` messages classify common video extensions and known file types as `video`
- unsupported message types still return `None`

- [ ] **Step 4: Re-run the parser tests**

Run: `python -m unittest tests.test_feishu_events -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add feishu/events.py tests/test_feishu_events.py
git commit -m "feat: parse inbound feishu media messages"
```

### Task 2: Add Feishu Message Resource Download Support

**Files:**
- Modify: `feishu/client.py`
- Test: `tests/test_feishu_client.py`

- [ ] **Step 1: Write the failing download-helper tests**

```python
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from feishu.client import FeishuClient


class FeishuDownloadTests(unittest.TestCase):
    def test_download_message_resource_writes_bytes(self) -> None:
        async def run_test() -> None:
            client = FeishuClient()
            fake_api = AsyncMock()
            fake_api.aretrieve.return_value.success.return_value = True
            fake_api.aretrieve.return_value.file.read.return_value = b"image-bytes"
            client._client = type(
                "StubClient",
                (),
                {"im": type("StubIM", (), {"v1": type("StubV1", (), {"message_resource": fake_api})()})()},
            )()

            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "saved.png"
                saved = await client.download_message_resource(
                    message_id="om_1",
                    file_key="img_key",
                    resource_type="image",
                    destination=out,
                )
                self.assertEqual(saved, out)
                self.assertEqual(out.read_bytes(), b"image-bytes")

        asyncio.run(run_test())
```

- [ ] **Step 2: Run the download tests to verify they fail**

Run: `python -m unittest tests.test_feishu_client -v`
Expected: FAIL because `FeishuClient.download_message_resource` does not exist.

- [ ] **Step 3: Implement the minimal download helper**

Add a new async method in `feishu/client.py`:

```python
async def download_message_resource(
    self,
    *,
    message_id: str,
    file_key: str,
    resource_type: str,
    destination: Path,
) -> Optional[Path]:
```

Implementation requirements:

- create `destination.parent`
- call Feishu `message_resource` retrieval API using `message_id`, `file_key`, and `type`
- stream/write bytes to `destination`
- return `destination` on success, `None` on API failure
- log failures with `code` and `msg`

- [ ] **Step 4: Re-run the download tests**

Run: `python -m unittest tests.test_feishu_client -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add feishu/client.py tests/test_feishu_client.py
git commit -m "feat: download inbound feishu message resources"
```

### Task 3: Add Sandbox Media Ingest

**Files:**
- Create: `media/__init__.py`
- Create: `media/ingest.py`
- Test: `tests/test_media_ingest.py`

- [ ] **Step 1: Write the failing ingest tests**

```python
import asyncio
import tempfile
import unittest
from pathlib import Path

from feishu.events import IncomingAttachment
from media.ingest import sanitize_filename, ingest_attachments


class MediaIngestTests(unittest.TestCase):
    def test_sanitize_filename_preserves_extension(self) -> None:
        self.assertEqual(sanitize_filename("../../bad name!!.mp4"), "bad-name.mp4")

    def test_ingest_attachments_saves_into_project_inbox(self) -> None:
        class StubFeishuClient:
            async def download_message_resource(self, **kwargs):
                destination = kwargs["destination"]
                destination.write_bytes(b"video")
                return destination

        async def run_test() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                project_root = Path(tmp)
                attachments = [
                    IncomingAttachment(
                        kind="video",
                        file_key="file_key",
                        message_resource_type="file",
                        file_name="demo.mp4",
                        file_type="mp4",
                    )
                ]
                stored = await ingest_attachments(
                    feishu=StubFeishuClient(),
                    project_root=project_root,
                    message_id="om_1",
                    attachments=attachments,
                )
                self.assertEqual(stored[0].local_path.parent.name, "inbox")
                self.assertTrue(stored[0].local_path.exists())

        asyncio.run(run_test())
```

- [ ] **Step 2: Run the ingest tests to verify they fail**

Run: `python -m unittest tests.test_media_ingest -v`
Expected: FAIL because the `media` package does not exist.

- [ ] **Step 3: Implement the minimal ingest module**

Add in `media/ingest.py`:

```python
@dataclass
class MediaAttachment:
    kind: str
    original_name: str
    local_path: Path
    mime_type: str | None = None
    size_bytes: int | None = None


def sanitize_filename(name: str | None, default_stem: str) -> str:
    ...


async def ingest_attachments(
    *,
    feishu: FeishuClient,
    project_root: Path,
    message_id: str,
    attachments: list[IncomingAttachment],
) -> list[MediaAttachment]:
    ...
```

Rules:

- all files land under `project_root / "inbox"`
- generate unique filenames with a timestamp prefix
- preserve extension if present
- reject path traversal by using sanitized basename only
- return `MediaAttachment.local_path` as a `Path`

- [ ] **Step 4: Re-run the ingest tests**

Run: `python -m unittest tests.test_media_ingest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add media/__init__.py media/ingest.py tests/test_media_ingest.py
git commit -m "feat: ingest feishu media into project inbox"
```

### Task 4: Add Prompt Envelope Formatting

**Files:**
- Create: `media/prompting.py`
- Test: `tests/test_media_prompting.py`

- [ ] **Step 1: Write the failing prompt-format tests**

```python
import unittest
from pathlib import Path

from media.ingest import MediaAttachment
from media.prompting import build_media_turn_prompt
from media.analyze import MediaAnalysis


class MediaPromptingTests(unittest.TestCase):
    def test_build_media_turn_prompt_handles_pure_media_message(self) -> None:
        prompt = build_media_turn_prompt(
            text="",
            attachments=[
                MediaAttachment(
                    kind="image",
                    original_name="demo.png",
                    local_path=Path("/tmp/demo.png"),
                )
            ],
            analyses=[
                MediaAnalysis(
                    kind="image",
                    summary="界面截图，展示任务列表。",
                    ocr_text="Task list",
                    visual_elements=["列表", "按钮"],
                    actions_or_mechanics=["浏览任务"],
                    suggested_intent="让 agent 分析截图内容",
                    fallback_used=False,
                )
            ],
        )
        self.assertIn("用户发送了一条飞书消息", prompt)
        self.assertIn("/tmp/demo.png", prompt)
        self.assertIn("界面截图", prompt)
```

- [ ] **Step 2: Run the prompt-format tests to verify they fail**

Run: `python -m unittest tests.test_media_prompting -v`
Expected: FAIL because `media.prompting` does not exist.

- [ ] **Step 3: Implement the minimal prompt formatter**

Create `media/prompting.py` with:

```python
def build_media_turn_prompt(
    *,
    text: str,
    attachments: list[MediaAttachment],
    analyses: list[MediaAnalysis | None],
) -> str:
    ...
```

Formatting requirements:

- always include the fixed intro line
- include “附带文字” even when empty
- enumerate attachments as `附件 1`, `附件 2`, ...
- when `analysis is None`, include a degraded note such as `媒体分析失败，请按本地文件继续处理`
- end with the same decision instruction block from the approved spec

- [ ] **Step 4: Re-run the prompt-format tests**

Run: `python -m unittest tests.test_media_prompting -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add media/prompting.py tests/test_media_prompting.py
git commit -m "feat: format media turns for agent input"
```

### Task 5: Add `GLM-5V-Turbo` Media Analysis With Video Fallback

**Files:**
- Modify: `config.py`
- Modify: `Dockerfile`
- Create: `media/analyze.py`
- Test: `tests/test_media_analyze.py`

- [ ] **Step 1: Write the failing media-analysis tests**

```python
import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from media.analyze import MediaAnalyzer
from media.ingest import MediaAttachment


class MediaAnalyzeTests(unittest.TestCase):
    def test_image_analysis_normalizes_vlm_response(self) -> None:
        async def run_test() -> None:
            analyzer = MediaAnalyzer()
            with patch.object(analyzer, "_call_glm_vision", new=AsyncMock(return_value={
                "summary": "像素风平台跳跃视频截图",
                "ocr_text": "",
                "visual_elements": ["角色", "平台"],
                "actions_or_mechanics": ["跳跃"],
                "suggested_intent": "分析游戏机制",
            })):
                result = await analyzer.analyze(
                    MediaAttachment(
                        kind="image",
                        original_name="frame.png",
                        local_path=Path("/tmp/frame.png"),
                    ),
                    user_text="帮我拆解玩法",
                )
                self.assertEqual(result.kind, "image")
                self.assertIn("平台", result.visual_elements)

    def test_video_analysis_falls_back_to_frames(self) -> None:
        async def run_test() -> None:
            analyzer = MediaAnalyzer()
            video = MediaAttachment(
                kind="video",
                original_name="clip.mp4",
                local_path=Path("/tmp/clip.mp4"),
            )
            with patch.object(analyzer, "_call_glm_vision", new=AsyncMock(side_effect=[RuntimeError("video failed"), {
                "summary": "抽帧后的总结",
                "ocr_text": "",
                "visual_elements": ["角色"],
                "actions_or_mechanics": ["移动"],
                "suggested_intent": "继续分析",
            }])), patch.object(analyzer, "_extract_video_frames", return_value=[Path("/tmp/frame-001.png")]):
                result = await analyzer.analyze(video, user_text="")
                self.assertTrue(result.fallback_used)

        asyncio.run(run_test())
```

- [ ] **Step 2: Run the media-analysis tests to verify they fail**

Run: `python -m unittest tests.test_media_analyze -v`
Expected: FAIL because `media.analyze` and `MediaAnalyzer` do not exist.

- [ ] **Step 3: Implement the minimal analyzer**

Create `media/analyze.py` with:

```python
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
    async def analyze(self, attachment: MediaAttachment, user_text: str) -> MediaAnalysis:
        ...
```

Implementation requirements:

- use `httpx.AsyncClient` for the direct `GLM-5V-Turbo` call
- keep request construction inside `_call_glm_vision(...)`
- normalize provider output into `MediaAnalysis`
- for videos:
  - try direct video analysis first
  - on failure, call `_extract_video_frames(...)`
  - analyze representative frames and merge into one `MediaAnalysis`
- if all analysis paths fail, raise an exception and let the caller degrade gracefully

Also update:

- `config.py` with the smallest viable VLM settings, for example:

```python
glm_vision_model: str = "glm-5v-turbo"
glm_vision_base_url: str = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
```

- `Dockerfile` to install `ffmpeg`

- [ ] **Step 4: Re-run the media-analysis tests**

Run: `python -m unittest tests.test_media_analyze -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add config.py Dockerfile media/analyze.py tests/test_media_analyze.py
git commit -m "feat: add glm-based media analysis"
```

### Task 6: Wire Media Through App Dispatch And Agent Runner

**Files:**
- Modify: `app.py`
- Modify: `agent/runner.py`
- Test: `tests/test_media_flow.py`

- [ ] **Step 1: Write the failing integration tests**

```python
import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from feishu.events import IncomingAttachment, ParsedMessageEvent


class MediaFlowTests(unittest.TestCase):
    def test_handle_user_message_builds_prompt_from_media(self) -> None:
        async def run_test() -> None:
            parsed = ParsedMessageEvent(
                event_id="evt-1",
                sender_open_id="ou_123",
                chat_id="oc_1",
                chat_type="p2p",
                message_id="om_1",
                text="帮我分析这个视频",
                attachments=[
                    IncomingAttachment(
                        kind="video",
                        file_key="file_key",
                        message_resource_type="file",
                        file_name="clip.mp4",
                        file_type="mp4",
                    )
                ],
            )
            with patch("agent.runner._get_or_create_client", new=AsyncMock()) as get_client, \
                 patch("agent.runner._run_query", new=AsyncMock()) as run_query:
                ...
                self.assertIn("媒体分析摘要", sent_prompt)

        asyncio.run(run_test())
```

- [ ] **Step 2: Run the integration tests to verify they fail**

Run: `python -m unittest tests.test_media_flow -v`
Expected: FAIL because the app/runner path only accepts a plain `text: str`.

- [ ] **Step 3: Implement the minimal integration**

In `agent/runner.py`:

- keep `handle_user_message(open_id: str, text: str)` as a compatibility wrapper for text-only callers
- add a richer entrypoint, for example:

```python
async def handle_incoming_message(
    open_id: str,
    *,
    text: str,
    message_id: str,
    attachments: list[IncomingAttachment],
) -> None:
    ...
```

Integration flow:

- resolve current `project` and `project_root`
- if no attachments, delegate to the existing text-only path
- otherwise:
  - call `ingest_attachments(...)`
  - call `MediaAnalyzer.analyze(...)` per attachment with per-attachment exception handling
  - build one textual prompt through `build_media_turn_prompt(...)`
  - send that text into `_run_query(...)`

In `app.py`:

- keep command handling on `parsed.text`
- route non-command messages with attachments into `agent_runner.handle_incoming_message(...)`
- allow pure-media messages through instead of dropping empty `text`

- [ ] **Step 4: Re-run the integration tests**

Run: `python -m unittest tests.test_media_flow -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app.py agent/runner.py tests/test_media_flow.py
git commit -m "feat: wire media messages into agent turns"
```

### Task 7: Verify No Regressions In Existing Session Behavior

**Files:**
- Test: `tests/test_session_resume.py`
- Test: `tests/test_project_state.py`
- Test: `tests/test_feishu_events.py`
- Test: `tests/test_feishu_client.py`
- Test: `tests/test_media_ingest.py`
- Test: `tests/test_media_prompting.py`
- Test: `tests/test_media_analyze.py`
- Test: `tests/test_media_flow.py`

- [ ] **Step 1: Run the full targeted suite**

Run:

```bash
python -m unittest \
  tests.test_project_state \
  tests.test_session_resume \
  tests.test_feishu_events \
  tests.test_feishu_client \
  tests.test_media_ingest \
  tests.test_media_prompting \
  tests.test_media_analyze \
  tests.test_media_flow \
  -v
```

Expected: PASS

- [ ] **Step 2: Run a syntax/compile sanity check**

Run:

```bash
python -m py_compile \
  app.py \
  config.py \
  feishu/events.py \
  feishu/client.py \
  agent/runner.py \
  media/__init__.py \
  media/ingest.py \
  media/analyze.py \
  media/prompting.py
```

Expected: no output

- [ ] **Step 3: Smoke-check the Docker image build prerequisites**

Run: `docker build -t feishu-cc-media-test .`
Expected: PASS, confirming `ffmpeg` packages resolve cleanly in the image

- [ ] **Step 4: Update any operator-facing docs only if code changed behavior**

If needed, update:

- `README.md` help text for supported inbound media types
- deployment notes for the new `ffmpeg` dependency
- env var docs if `glm_vision_base_url` or `glm_vision_model` were added

- [ ] **Step 5: Commit**

```bash
git add .
git commit -m "test: verify feishu media input rollout"
```

## Execution Guardrails

- Do not refactor session resume logic while implementing this feature.
- Do not add `/model` or provider switching in this plan.
- Do not route media directly into `ClaudeSDKClient` multimodal blocks in phase 1.
- Prefer small commits after every task; if a task grows, split it before coding.
- If Zhipu’s direct video API shape differs from the plan, adjust only inside `media/analyze.py` and keep the rest of the architecture unchanged.
