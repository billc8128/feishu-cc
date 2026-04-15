# Browser Service Design

**Date:** 2026-04-16

**Goal:** Add a Railway-friendly browser worker that the Feishu bot can invoke after user consent, with agent-led automation, a spectator link, single-worker queueing, and per-user persistent Chromium profiles.

## Scope

This first version adds:

- A separate `browser service` deployed from the same repository with its own Dockerfile.
- A single global browser worker with FIFO queueing across users.
- Persistent Chromium user data per Feishu `open_id` under `/data/browser-profiles/<open_id>`.
- Agent-triggered permission flow:
  - agent requests browser use
  - bot asks the user to confirm
  - after confirmation, the agent can create/reuse a browser session and continue automatically
- A spectator link that shows the live browser via noVNC.
- Minimal automation actions for the agent: create session, navigate, click, type, wait, inspect, close.

This version does not add:

- human takeover / keyboard-mouse control from the browser viewer
- multiple concurrent browser workers
- durable queue/session state across browser-service restarts
- advanced browser orchestration such as downloads, uploads, multi-tab workflows, or anti-bot stealth

## Architecture

### Bot Service

The existing FastAPI app remains the control plane. It gains:

- browser approval state for each user
- `/browser yes`, `/browser no`, `/browser status`, `/browser close` commands
- a browser MCP server exposed to the agent

The agent does not call browser APIs directly over HTTP. Instead it uses custom MCP tools bound to the current `open_id`, matching the existing `schedule` and `deliver` integration style.

### Browser Service

The new browser service is a separate FastAPI app under `browser/`.

Responsibilities:

- maintain one active session at a time
- queue later users in FIFO order
- launch and supervise the local display/browser stack
- expose a simple authenticated HTTP API for the bot service
- provide a spectator URL for the live browser

### Display And Automation Stack

The browser service runs:

- `Xvfb` for a virtual display
- `x11vnc` to mirror the display
- `noVNC/websockify` for browser-based viewing
- `Chromium` with a persistent `--user-data-dir`
- `Playwright` connected over Chrome DevTools Protocol for automation

The same Chromium instance is both:

- visible in the spectator viewer
- controlled by the automation layer

## Data Flow

1. User asks the bot to do something that needs a real logged-in browser.
2. Agent calls a browser permission tool.
3. Tool sends a Feishu confirmation message and waits for `/browser yes` or `/browser no`.
4. If approved, the tool calls browser service `ensure session`.
5. Browser service either:
   - returns an existing active session for the user
   - queues the user behind the current owner
   - or starts a new browser stack for that user
6. Once ready, the tool returns the live viewer URL to the agent and the bot notifies the user.
7. Agent continues browser automation through MCP tools backed by browser-service HTTP calls.

## Session Model

Each user may have at most one session record in one of these states:

- `queued`
- `starting`
- `ready`
- `active`
- `closed`
- `expired`

Rules:

- only one active session globally
- one `open_id` cannot hold multiple queued/active sessions
- repeat requests for the same user reuse the existing session state
- idle and max TTL limits reclaim the worker

The queue and active-session metadata are in memory for this first version. Browser login state survives restarts because Chromium profiles live on `/data`.

## Approval Model

Approval is explicit and per-request:

- agent asks once when browser access is needed
- user responds with `/browser yes` or `/browser no`
- approval wait has a timeout

The approval registry is in-memory in the bot process for the first version.

## API Surface

### Browser Service HTTP API

- `POST /v1/sessions/ensure`
- `GET /v1/sessions/{open_id}`
- `POST /v1/sessions/{open_id}/close`
- `POST /v1/sessions/{open_id}/navigate`
- `POST /v1/sessions/{open_id}/click`
- `POST /v1/sessions/{open_id}/type`
- `POST /v1/sessions/{open_id}/wait`
- `POST /v1/sessions/{open_id}/snapshot`

All requests use a shared bearer token between bot service and browser service.

### Agent MCP Tools

- `browser_request_permission`
- `browser_open`
- `browser_navigate`
- `browser_click`
- `browser_type`
- `browser_wait`
- `browser_snapshot`
- `browser_close`

`browser_open` internally ensures a session exists and returns the live viewer URL in its tool result text.

## Error Handling

- If approval is denied or times out, the tool returns an error result and the agent must continue without browser automation.
- If the browser service is unavailable, tools fail with actionable text and no retry storm.
- If the user is queued, the tool informs the user of queue position and waits until session readiness or timeout.
- If automation detects a missing session, it returns a clean error instructing the agent to reopen the browser session first.

## Testing Strategy

Focus on deterministic logic:

- approval registry behavior
- bot `/browser` command handling
- browser-service queue/session reuse/close logic
- browser MCP tool HTTP client behavior
- app dispatch integration for confirmation messages and close/status commands

Avoid trying to fully end-to-end test Chromium/noVNC locally in unit tests. Those pieces are integration/deployment concerns and should be verified with container build/runtime checks.
