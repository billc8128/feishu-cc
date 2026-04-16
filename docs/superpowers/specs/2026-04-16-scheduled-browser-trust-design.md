# Scheduled Browser Trust Design

**Date:** 2026-04-16

**Goal:** Allow a scheduled task to permanently reuse browser access after a single approval, so recurring jobs can run unattended without waiting for a fresh browser confirmation every time.

## Scope

This design adds:

- persistent browser trust at the scheduled-task level
- one-time browser approval for each scheduled task
- automatic browser reuse for later runs of the same task
- a revoke path so a previously trusted task can be forced back to manual approval
- cleanup of task trust when a scheduled task is deleted

This design does not add:

- global browser trust for all scheduled tasks owned by a user
- global browser trust for all interactive chat tasks
- bypassing human takeover when the site requires login, captcha, or 2FA
- durable approval for non-scheduled browser usage

## Problem

The current behavior treats every `browser_open` call the same:

- the agent requests browser use
- the bot sends an approval card
- execution waits for `/browser yes` or `/browser no`

That works for interactive chat, but it breaks unattended scheduler workflows. A recurring task that needs a logged-in browser will repeatedly stop on approval if the user is not watching Feishu at the moment it fires.

The result is that browser-backed cron jobs are not truly automatable.

## Recommended Approach

Three approaches were considered:

1. Trust each scheduled task permanently after its first approval.
2. Trust all scheduled tasks for a user after one approval.
3. Keep prompting, but add a time-limited approval window such as 24 hours or 7 days.

Recommended: **Approach 1**.

Why:

- it directly solves the unattended execution problem
- it keeps the trust scope narrow
- it avoids granting browser autonomy to new tasks that the user has never reviewed
- it preserves the stricter approval model for normal interactive chat

## User-Facing Behavior

### First Browser Use For A Scheduled Task

When a scheduled task first reaches `browser_open`:

- the bot still sends the normal browser approval card
- the copy should make it explicit that approval is durable for this scheduled task
- if the user approves, the task continues normally
- the system stores a browser trust record for that specific `task_id`

Suggested approval wording:

- "允许后，此定时任务后续将自动使用浏览器，不再重复询问。"

### Later Runs Of The Same Task

When the same `task_id` runs again and reaches `browser_open`:

- the approval step is skipped
- the browser session is created or reused immediately
- the user still receives the live viewer/takeover link
- the task continues unattended unless a site-specific human step is required

### Interactive Chat

Normal chat-triggered browser usage remains unchanged:

- every ad hoc `browser_open` still requires per-request approval
- no trust record is created for ordinary chat conversations

### Revocation

Because the trust is permanent, the user must be able to revoke it.

Recommended command:

- `/cron browser revoke <task_id>`

Behavior:

- removes the stored trust for that scheduled task
- the next browser use for that task goes back to manual approval

## Data Model

Add a new metadata table in the existing scheduler SQLite database:

```sql
CREATE TABLE IF NOT EXISTS schedule_browser_trust (
    task_id TEXT PRIMARY KEY,
    open_id TEXT NOT NULL,
    approved_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Properties:

- trust is keyed by `task_id`
- `open_id` is stored for ownership checks and safer deletion/revocation
- the table is durable across service restarts

Required store helpers:

- `is_browser_trusted(task_id: str, open_id: str) -> bool`
- `approve_browser_trust(task_id: str, open_id: str) -> None`
- `revoke_browser_trust(task_id: str, open_id: str) -> bool`
- `delete_browser_trust(task_id: str, open_id: str) -> None`

## Execution Context

The main implementation challenge is that `browser_open` currently only knows the user `open_id`. It does not know whether the current run came from:

- a scheduled task
- or a normal chat request

To solve this, add a small execution context that is available while a single task is running.

Recommended shape:

- a context-local value such as `task_context`
- fields:
  - `source = "scheduler" | "chat"`
  - `task_id: str | None`

Flow:

- scheduler runner sets `source="scheduler"` and `task_id=<scheduled task id>` before handing the prompt to the agent
- ordinary chat handling sets `source="chat"` with no task id
- browser tools read the current context before deciding whether approval is required

This keeps the browser logic simple and avoids threading `task_id` through every MCP tool argument.

## Browser Approval Logic

### Existing Behavior

Current `browser_open` logic is:

1. check whether a session already exists
2. if not, start an approval request
3. wait for approval
4. call browser service `ensure session`

### New Behavior

Update that decision tree as follows:

1. check whether a session already exists
2. if not, inspect the current execution context
3. if the source is `scheduler` and `task_id` is present:
   - check `schedule_browser_trust`
   - if trusted, skip approval
   - if not trusted, run approval
4. if approval is granted for a scheduled task:
   - persist browser trust for that `task_id`
5. call browser service `ensure session`

Behavioral rules:

- trust only suppresses the approval prompt
- trust does not bypass queueing
- trust does not bypass human takeover when a website truly needs the user
- trust does not grant browser access to other tasks

## Command Changes

### `/cron` Commands

Add a revoke subcommand:

- `/cron browser revoke <task_id>`

Response examples:

- success: `🧹 已撤销定时任务 #<task_id> 的浏览器自动授权。`
- not found: `未找到任务 #<task_id> 的浏览器授权。`

Optional but recommended later:

- show browser trust state in `/cron list`

This is not required for the first implementation.

### `/browser` Commands

No new `/browser` command is required for the approval path itself.

The existing approval card and `/browser yes|no` fallback remain valid for the first approval of a scheduled task.

## Cleanup Rules

### Scheduled Task Deletion

When a user deletes a scheduled task:

- remove the scheduler job as today
- remove the corresponding `schedule_browser_trust` row if present

This prevents orphaned durable trust records from accumulating after a task is gone.

### Ownership Enforcement

Every trust mutation must verify ownership using both:

- `task_id`
- `open_id`

This prevents one user from revoking or relying on trust created for another user.

## Error Handling

### Approval Timeout On First Use

If the first browser approval for a scheduled task times out:

- do not create trust
- fail that run normally
- next run will ask again

### Browser Service Errors

If browser startup, queueing, or automation fails:

- trust remains intact
- later runs still skip approval
- operational failure is treated separately from trust state

### Missing Task Context

If browser tools are invoked in scheduler mode but no `task_id` is available:

- fall back to the current per-request approval behavior
- log a warning so the bug is visible

This avoids silent over-trust due to missing context.

## Testing Strategy

Cover the behavior at three levels.

### Scheduler Store

- creates and reads browser trust records
- revokes trust records
- deletes trust records when a task is deleted
- enforces `task_id + open_id` ownership

### Browser Tool Logic

- scheduled run without trust requests approval
- approval success persists trust
- scheduled run with trust skips approval
- normal chat run still requests approval
- approval timeout does not create trust

### Command Handling

- `/cron browser revoke <task_id>` removes trust and returns correct user-facing text
- deleting a scheduled task also removes its browser trust

## Rollout Notes

This change is safe to roll out incrementally because:

- existing tasks without trust continue to behave exactly as before
- trust is only created after an explicit human approval
- revocation gives the user a clear escape hatch if a task becomes unsafe or obsolete

The expected user experience after rollout is:

1. first scheduled browser run asks once
2. user approves
3. later runs of that task no longer wait for approval
4. user can revoke if needed
