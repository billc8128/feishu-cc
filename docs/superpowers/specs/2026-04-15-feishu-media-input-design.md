# Feishu Media Input Design

Date: 2026-04-15

## Goal

Support image and video messages sent directly in Feishu conversations.

The user experience target is:

- The user can send text, images, videos, or mixed text+media to the bot.
- The bot should treat media as part of the current turn instead of requiring a follow-up command.
- Media files should be saved into the current project sandbox so later tool use can reference them.
- The existing project, session, and command model should stay intact.

## Non-Goals

- No `/model` switching in this phase.
- No redesign of session memory architecture.
- No group chat support.
- No rich card UI for media analysis results.
- No promise that video is always passed as a native multimodal block to the agent SDK.

## Constraints

- The current Feishu ingress only accepts text and drops non-text messages.
- The current agent path only sends `client.query(text)`.
- Session continuity must remain stable.
- A single Zhipu key should be sufficient.
- Media understanding should use `GLM-5V-Turbo`.

## Recommended Architecture

Use a split pipeline:

1. Feishu ingress accepts text, image, and file messages.
2. Media resources are downloaded into the current project sandbox.
3. A media analysis layer calls `GLM-5V-Turbo`.
4. The resulting media summary is folded into the current user turn.
5. The existing agent session handles the final response and tool use.

This keeps the current `claude-agent-sdk` session/tool flow for the main agent while avoiding a hard dependency on its native multimodal format for video.

## Data Model

### Parsed Message

Replace the current text-only parsed event with a richer structure:

```python
ParsedMessageEvent(
    event_id: str,
    sender_open_id: str,
    chat_id: str,
    chat_type: str,
    message_id: str,
    text: str,
    attachments: list[IncomingAttachment],
)
```

### Incoming Attachment

```python
IncomingAttachment(
    kind: Literal["image", "video", "file"],
    file_key: str,
    message_resource_type: str,
    file_name: str | None,
    file_type: str | None,
)
```

### Stored Attachment

```python
MediaAttachment(
    kind: Literal["image", "video"],
    original_name: str,
    local_path: str,
    mime_type: str | None,
    size_bytes: int | None,
)
```

### Analysis Result

```python
MediaAnalysis(
    kind: Literal["image", "video"],
    summary: str,
    ocr_text: str,
    visual_elements: list[str],
    actions_or_mechanics: list[str],
    suggested_intent: str,
    fallback_used: bool = False,
)
```

## Request Flow

### 1. Feishu Event Parsing

`feishu/events.py` should:

- continue parsing text messages
- add parsing for `image`
- add parsing for `file`
- classify common video file types under `file`
- keep non-supported message types ignored

The parser should no longer reject all non-text events.

### 2. Media Download

`feishu/client.py` should add a message resource download helper built on `GetMessageResource`.

The download target should be:

```text
/data/sandbox/users/<open_id>/<project>/inbox/<timestamp>-<safe-filename>
```

Rules:

- sanitize file names
- never allow path traversal
- preserve extension where possible
- keep downloads inside the user sandbox

### 3. Media Analysis

Add `media/analyze.py`.

Responsibilities:

- analyze images with `GLM-5V-Turbo`
- analyze videos with `GLM-5V-Turbo`
- if direct video understanding fails, fall back to frame extraction and image analysis
- return a normalized `MediaAnalysis`

The analysis prompt should be task-oriented and optimized for downstream agent use, not for direct user display.

### 4. Prompt Assembly

Add a formatter that turns the incoming turn into a stable prompt envelope.

Template:

```text
用户发送了一条飞书消息。

附带文字:
<可能为空>

附件 1:
- 类型: image|video
- 文件路径: <sandbox path>
- 原始文件名: <name>
- 媒体分析摘要: <summary>
- OCR: <ocr_text>
- 关键元素/动作: <visual_elements / actions_or_mechanics>

请结合当前对话上下文决定如何回复。
如果上下文足够，直接继续任务。
如果上下文不足，再向用户追问。
```

The main agent should still receive a single textual turn in phase 1.

## Error Handling

### Download Failure

- Send a direct user-facing error.
- Do not enter the agent flow.

### Analysis Failure

- Do not abort the turn.
- Keep the downloaded file.
- Pass a degraded prompt to the agent including:
  - local file path
  - media type
  - an explicit note that analysis failed

### Video Failure

- First attempt direct `GLM-5V-Turbo` video understanding.
- On failure, try frame extraction.
- If frame extraction also fails, continue with file-path-only degraded prompt.

### Unsupported Type

- Save if feasible.
- Tell the user the file was saved but not analyzed automatically.
- Let the agent decide whether to handle it via tools.

## Session Behavior

Media support must not change the existing session ownership model.

- Existing `(open_id, project)` session behavior stays in place.
- Media input becomes just another turn in the same conversation.
- The current session resume fix must remain untouched.

## Scope for Phase 1

- private chat only
- at most 4 images or 1 video per incoming message
- common image types only
- common video types only
- no model switching
- no custom media result cards

## File/Module Plan

### Update

- `feishu/events.py`
- `feishu/client.py`
- `app.py`
- `agent/runner.py`

### Add

- `media/__init__.py`
- `media/ingest.py`
- `media/analyze.py`
- `media/prompting.py`
- tests for message parsing, download handling, prompt assembly, and degraded analysis flow

## Testing Strategy

Minimum tests:

1. Text-only messages remain unchanged.
2. Image messages parse successfully.
3. Video file messages parse successfully.
4. Media downloads are sandboxed and sanitized.
5. Image analysis success produces a valid prompt envelope.
6. Video analysis failure falls back without crashing the turn.
7. Analysis persistence errors do not break agent response delivery.
8. Session flow is unchanged by media turns.

## Risks

### SDK Multimodal Mismatch

The current agent SDK path may not reliably support native video blocks across providers.

Mitigation:

- keep phase 1 prompt-based integration
- isolate media understanding behind `media/analyze.py`

### Large Media

Videos may exceed request or processing limits.

Mitigation:

- explicit size checks
- graceful fallback
- preserve files in sandbox even when analysis fails

### Tooling Dependencies

Frame extraction may require `ffmpeg`.

Mitigation:

- make it a fallback, not the primary path
- degrade to file-path-only if unavailable

## Recommendation

Implement phase 1 exactly as above:

- `GLM-5V-Turbo` for media understanding
- sandbox download first
- prompt augmentation into the current agent session
- no model switching

This gives the user the desired Feishu-native image/video experience while minimizing risk to the existing session and agent architecture.
