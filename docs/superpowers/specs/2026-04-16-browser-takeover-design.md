# Browser Takeover Design

**Date:** 2026-04-16

**Goal:** Upgrade the existing spectator-only browser session into an explicit takeover flow where the user can pause agent automation, interact with the live browser, and hand control back to the agent.

## Scope

This design adds:

- a control model for each active browser session: `agent` or `human`
- a lightweight wrapper page around noVNC with explicit `Take Over` and `Resume Agent` actions
- interactive noVNC access only while the session is in `human` control
- browser-service APIs for takeover and resume
- agent/browser-tool pause semantics so the agent stops cleanly while the user is driving
- bot notifications that explain when the browser is paused for the user and when the agent has resumed

This design does not add:

- multiple simultaneous human viewers
- partial shared control where both the user and the agent can act at once
- automatic resume timers
- recording, replay, or audit video for browser sessions
- durable takeover state across browser-service restarts

## Why This Change Exists

The current implementation exposes a live browser viewer URL, but it always opens noVNC in `view_only=1` mode. That is enough for observation, but not for the main value of a browser agent:

- the agent should automate normal browser actions
- the user should step in only when a human is required
- the user should then be able to return control to the agent without starting over

The design goal is to add that human handoff without introducing control races between the user and the agent.

## Recommended Approach

Three possible approaches were considered:

1. Opening the viewer immediately transfers control to the user.
2. The viewer starts in spectator mode and the user explicitly presses `Take Over`.
3. Takeover is triggered from Feishu commands rather than from the viewer page.

Recommended: **Approach 2**.

Why:

- it preserves passive viewing without interrupting the agent
- it makes the handoff explicit, so accidental page opens do not pause work
- it keeps the main control surface next to the live browser rather than split between Feishu and the viewer

## Control Model

Each active session gains a control field:

- `controller = "agent"` means browser automation is allowed and the viewer is read-only
- `controller = "human"` means browser automation is paused and the viewer is interactive

Rules:

- only one controller is active at a time
- switching to `human` does not roll back actions already completed by the agent
- switching back to `agent` does not replay previous steps; it only allows subsequent steps to continue
- takeover affects only future browser actions; if a browser step is already in flight, it may finish before the pause takes effect

## Viewer Experience

The current `/view/{viewer_token}` redirect becomes a small HTML wrapper page instead of a direct redirect to `vnc_lite.html`.

The wrapper page contains:

- a status bar showing:
  - session state
  - current controller: `Agent` or `You`
- the embedded noVNC frame
- a `Take Over` button
- a `Resume Agent` button

Default behavior:

- opening the page does not pause the agent
- the page initially embeds noVNC in read-only mode
- the `Take Over` button is enabled only while `controller = "agent"`
- the `Resume Agent` button is enabled only while `controller = "human"`

Takeover behavior:

1. User opens the viewer page.
2. User clicks `Take Over`.
3. Browser service sets `controller = "human"`.
4. The wrapper page reconnects noVNC without `view_only=1`.
5. All subsequent agent browser actions are rejected with a takeover pause error.

Resume behavior:

1. User clicks `Resume Agent`.
2. Browser service sets `controller = "agent"`.
3. The wrapper page reconnects noVNC in read-only mode.
4. Agent browser actions are allowed again.

## Browser Service Changes

### Session State

`SessionRecord` should gain:

- `controller: str`
- `paused_reason: str`
- `last_control_change_at: float`

Expected values:

- `controller` is one of `agent` or `human`
- `paused_reason` is empty during normal operation and set to `takeover` while the human controls the browser

### New HTTP API

Add:

- `POST /v1/sessions/{open_id}/takeover`
- `POST /v1/sessions/{open_id}/resume`

Update:

- `GET /v1/sessions/{open_id}` should also return:
  - `controller`
  - `paused_reason`
  - `last_control_change_at`

Behavior:

- `takeover` requires an active session owned by that `open_id`
- `resume` requires an active session currently controlled by `human`
- both endpoints are idempotent enough for UI retries:
  - taking over an already-human session returns success with unchanged state
  - resuming an already-agent session returns success with unchanged state

### Viewer Token Validation

The existing viewer token remains the gate for the wrapper page and VNC WebSocket access.

Server-side rules:

- token must match the active session
- token must not be expired
- interactive or read-only VNC mode is decided on the server from `controller`, not trusted from a client query string alone

This means the wrapper page may request a mode change, but the server remains the source of truth for whether the VNC path is interactive.

## Agent And Tooling Changes

All browser actions must check control state before dispatching to Playwright:

- `navigate`
- `click`
- `type`
- `wait`
- `snapshot`

If `controller = "human"`, those actions should fail fast with a structured error, for example:

- `error_code = "BROWSER_PAUSED_FOR_TAKEOVER"`

The bot/agent layer should interpret this as a user-wait state, not an infrastructure failure.

Expected behavior:

- the agent stops browser progression
- the bot sends a short user-facing message such as:
  - "浏览器已交给你。处理完后点击页面里的 Resume Agent，我再继续。"
- after resume, the next browser action proceeds normally

No new Feishu command is required for takeover or resume. Existing commands remain:

- `/browser yes`
- `/browser no`
- `/browser status`
- `/browser close`

The viewer page is the only takeover control surface in this version.

## Failure Handling

### Session Ends During Viewing

If the wrapper page is open after the session has already closed or expired:

- show a clear terminal state message
- disable both control buttons
- do not keep reconnecting noVNC forever

### User Takes Over While Agent Is Mid-Step

If the user clicks `Take Over` while one browser action is already executing:

- let the in-flight step finish
- apply the pause to the next browser action

This avoids trying to cancel arbitrary Playwright operations mid-call.

### User Never Resumes

The existing session cleanup still applies:

- idle timeout
- max session TTL

If the user holds control too long:

- close the session
- notify the bot layer so the user is not left waiting silently

### Invalid Mode Transitions

Examples:

- taking over a queued session
- resuming a closed session
- resuming when no session exists

These should return clean 4xx errors with actionable messages, not generic 500s.

## Testing Strategy

Focus on deterministic behavior in unit tests.

### Browser Service

- takeover toggles `controller` from `agent` to `human`
- resume toggles `controller` from `human` to `agent`
- repeated takeover/resume requests are idempotent
- browser actions fail with the takeover pause error while `controller = "human"`
- session serialization includes the new control fields

### App Layer

- `/view/{viewer_token}` returns the wrapper page instead of a direct redirect
- wrapper page bootstrap data reflects the current controller
- VNC mode returned to the page matches server-side control state

### Bot / Agent Layer

- paused browser action errors are surfaced as "waiting for human" rather than generic browser failures
- the agent can continue after resume without reopening the session

Avoid trying to fully simulate interactive VNC in unit tests. The critical logic is the state machine and API behavior.

## Rollout Notes

This is an additive change on top of the existing browser worker architecture:

- queueing stays global and single-worker
- persistent Chromium profiles stay unchanged
- the main migration risk is the viewer flow, because `/view/{viewer_token}` stops being a plain redirect and becomes an app page

That risk is acceptable because it localizes the feature to the browser service and avoids reworking Feishu command handling.
