# Scheduled Browser Trust Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow a scheduled task to gain permanent browser approval after its first successful confirmation, so later runs of that same cron task can open/reuse the browser without stopping for another approval card.

**Architecture:** Add durable task-level browser trust to the existing scheduler SQLite metadata, and thread scheduled-task identity through a small execution context so `browser_open` can distinguish scheduler-triggered runs from normal chat runs. Keep interactive chat approvals unchanged, add a revoke path under `/cron`, and clean up trust rows when a scheduled task is deleted.

**Tech Stack:** FastAPI, SQLite, APScheduler, Python `contextvars`, httpx, lark-oapi, Python `unittest`

---

## File Structure

### Existing files to modify

- `scheduler/store.py`
  Add the durable `schedule_browser_trust` table plus helper functions for create/read/revoke/delete, and delete trust when a scheduled task is removed.
- `scheduler/runner.py`
  Set scheduler execution context before handing a prompt to the agent and clear it afterwards.
- `agent/tools_browser.py`
  Change `browser_open` so scheduler runs can skip approval when the current `task_id` is trusted, and persist trust after the first approved scheduled run.
- `feishu/client.py`
  Extend the browser approval card API so scheduled-task approvals can include a durable-trust notice without changing normal chat copy.
- `app.py`
  Extend `/cron` command handling with `/cron browser revoke <task_id>`.
- `tests/test_browser_tools.py`
  Cover trusted vs untrusted scheduler browser flows.
- `tests/test_feishu_client.py`
  Cover the approval-card copy change for scheduled tasks.

### New files to create

- `agent/run_context.py`
  Small context-local helper for `source="chat" | "scheduler"` and `task_id`.
- `tests/test_scheduler_store.py`
  Covers trust table CRUD and cleanup on delete.
- `tests/test_scheduler_runner.py`
  Covers scheduler-run context setup/teardown around `agent_runner.handle_user_message`.
- `tests/test_cron_commands.py`
  Covers `/cron browser revoke <task_id>` behavior.

## Task 1: Add Durable Scheduled Browser Trust Storage

**Files:**
- Modify: `scheduler/store.py`
- Test: `tests/test_scheduler_store.py`

- [ ] **Step 1: Write the failing storage tests**

```python
def test_browser_trust_round_trip(self) -> None:
    task = store.add_task("ou_user", "scratch", "0 * * * *", "run task", "note")

    self.assertFalse(store.is_browser_trusted(task.task_id, "ou_user"))

    store.approve_browser_trust(task.task_id, "ou_user")

    self.assertTrue(store.is_browser_trusted(task.task_id, "ou_user"))


def test_revoke_browser_trust_is_owner_scoped(self) -> None:
    task = store.add_task("ou_user", "scratch", "0 * * * *", "run task", "note")
    store.approve_browser_trust(task.task_id, "ou_user")

    self.assertFalse(store.revoke_browser_trust(task.task_id, "ou_other"))
    self.assertTrue(store.is_browser_trusted(task.task_id, "ou_user"))
    self.assertTrue(store.revoke_browser_trust(task.task_id, "ou_user"))
    self.assertFalse(store.is_browser_trusted(task.task_id, "ou_user"))


def test_delete_task_removes_browser_trust(self) -> None:
    task = store.add_task("ou_user", "scratch", "0 * * * *", "run task", "note")
    store.approve_browser_trust(task.task_id, "ou_user")

    self.assertTrue(store.delete_task(task.task_id, "ou_user"))
    self.assertFalse(store.is_browser_trusted(task.task_id, "ou_user"))
```

- [ ] **Step 2: Run the new scheduler-store tests to verify they fail first**

Run: `python -m unittest tests.test_scheduler_store -v`  
Expected: FAIL because the trust table and helper methods do not exist yet.

- [ ] **Step 3: Implement the trust schema and helpers in `scheduler/store.py`**

Add schema:

```sql
CREATE TABLE IF NOT EXISTS schedule_browser_trust (
    task_id TEXT PRIMARY KEY,
    open_id TEXT NOT NULL,
    approved_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Add helpers:

```python
def is_browser_trusted(task_id: str, open_id: str) -> bool: ...
def approve_browser_trust(task_id: str, open_id: str) -> None: ...
def revoke_browser_trust(task_id: str, open_id: str) -> bool: ...
def delete_browser_trust(task_id: str, open_id: str) -> None: ...
```

Update:

```python
def delete_task(task_id: str, open_id: str) -> bool:
    ...
    if deleted:
        delete_browser_trust(task_id, open_id)
```

- [ ] **Step 4: Re-run the scheduler-store tests**

Run: `python -m unittest tests.test_scheduler_store -v`  
Expected: PASS with trust CRUD and delete cleanup covered.

- [ ] **Step 5: Commit the trust-storage change**

```bash
git add scheduler/store.py tests/test_scheduler_store.py
git commit -m "feat: add scheduled browser trust storage"
```

## Task 2: Add Scheduled Execution Context

**Files:**
- Create: `agent/run_context.py`
- Modify: `scheduler/runner.py`
- Test: `tests/test_scheduler_runner.py`

- [ ] **Step 1: Write the failing execution-context tests**

```python
def test_fire_task_sets_scheduler_context_for_agent_run(self) -> None:
    ...
    with patch("agent.runner.handle_user_message", new=AsyncMock(side_effect=_capture_context)):
        await scheduler_runner.fire_task(task.task_id)

    self.assertEqual(captured["source"], "scheduler")
    self.assertEqual(captured["task_id"], task.task_id)


def test_fire_task_restores_context_after_execution(self) -> None:
    ...
    await scheduler_runner.fire_task(task.task_id)
    self.assertEqual(run_context.get_current_task_context().source, "chat")
    self.assertIsNone(run_context.get_current_task_context().task_id)
```

- [ ] **Step 2: Run the scheduler-runner tests to verify the context is missing**

Run: `python -m unittest tests.test_scheduler_runner -v`  
Expected: FAIL because there is no run-context helper and scheduler runs do not mark themselves.

- [ ] **Step 3: Implement a small context-local helper and use it in `scheduler/runner.py`**

Create `agent/run_context.py`:

```python
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import dataclass

@dataclass(frozen=True)
class TaskContext:
    source: str = "chat"
    task_id: str | None = None

_task_context: ContextVar[TaskContext] = ContextVar("task_context", default=TaskContext())

def get_current_task_context() -> TaskContext:
    return _task_context.get()

@contextmanager
def use_task_context(*, source: str, task_id: str | None) -> Iterator[None]:
    token = _task_context.set(TaskContext(source=source, task_id=task_id))
    try:
        yield
    finally:
        _task_context.reset(token)
```

Wrap scheduler execution:

```python
with run_context.use_task_context(source="scheduler", task_id=task.task_id):
    await agent_runner.handle_user_message(task.open_id, task.prompt)
```

- [ ] **Step 4: Re-run the scheduler-runner tests**

Run: `python -m unittest tests.test_scheduler_runner -v`  
Expected: PASS with scheduler context visible during the run and restored afterwards.

- [ ] **Step 5: Commit the context change**

```bash
git add agent/run_context.py scheduler/runner.py tests/test_scheduler_runner.py
git commit -m "feat: add scheduler run context"
```

## Task 3: Make `browser_open` Persist And Reuse Scheduled Trust

**Files:**
- Modify: `agent/tools_browser.py`
- Modify: `feishu/client.py`
- Test: `tests/test_browser_tools.py`
- Test: `tests/test_feishu_client.py`

- [ ] **Step 1: Write the failing browser-tool and card-copy tests**

```python
def test_scheduled_browser_open_skips_approval_when_task_is_trusted(self) -> None:
    async def run_test() -> None:
        server = browser_tools.build_browser_mcp("ou_123")
        browser_open = server["tools"]["browser_open"]

        with patch("agent.tools_browser.run_context.get_current_task_context", return_value=TaskContext(source="scheduler", task_id="task_1")), \
             patch("scheduler.store.is_browser_trusted", return_value=True), \
             patch("agent.tools_browser.browser_client.get_session", new=AsyncMock(return_value=None)), \
             patch("agent.tools_browser.browser_client.ensure_session", new=AsyncMock(return_value={"state": "ready", "viewer_url": "https://viewer/session-1"})), \
             patch.object(browser_tools.feishu_client, "send_browser_approval_card", new=AsyncMock()) as send_card:
            await browser_open({"reason": "需要登录 Reddit"})

        send_card.assert_not_awaited()


def test_scheduled_browser_open_persists_trust_after_approval(self) -> None:
    async def run_test() -> None:
        ...
        approve_browser_trust.assert_called_once_with("task_1", "ou_123")


def test_send_browser_approval_card_can_include_persistent_trust_note(self) -> None:
    async def run_test() -> None:
        ...
        self.assertIn("后续将自动使用浏览器", card["elements"][0]["content"])
```

- [ ] **Step 2: Run the browser-tool and Feishu-client tests to verify they fail first**

Run: `python -m unittest tests.test_browser_tools tests.test_feishu_client -v`  
Expected: FAIL because `browser_open` always requests approval and the card helper cannot render scheduled-trust copy.

- [ ] **Step 3: Implement the scheduled-trust decision path**

In `agent/tools_browser.py`:

```python
task_context = run_context.get_current_task_context()
is_scheduled_task = task_context.source == "scheduler" and bool(task_context.task_id)
trusted = is_scheduled_task and store.is_browser_trusted(task_context.task_id, open_id)
```

Behavior:

- if `trusted`, skip `browser_approval.start_request()` and go directly to `ensure_session`
- if untrusted scheduled task is approved, call:

```python
store.approve_browser_trust(task_context.task_id, open_id)
```

In `feishu/client.py`, extend:

```python
async def send_browser_approval_card(self, open_id: str, *, reason: str, trust_note: str = "") -> Optional[str]:
```

Then, for scheduled first-time approvals, pass:

```python
trust_note="允许后，此定时任务后续将自动使用浏览器，不再重复询问。"
```

- [ ] **Step 4: Re-run the browser-tool and Feishu-client tests**

Run: `python -m unittest tests.test_browser_tools tests.test_feishu_client -v`  
Expected: PASS with trusted scheduled tasks skipping approval and first-time approvals persisting trust.

- [ ] **Step 5: Commit the browser-trust logic**

```bash
git add agent/tools_browser.py feishu/client.py tests/test_browser_tools.py tests/test_feishu_client.py
git commit -m "feat: trust scheduled browser tasks after first approval"
```

## Task 4: Add `/cron browser revoke <task_id>`

**Files:**
- Modify: `app.py`
- Modify: `scheduler/store.py`
- Test: `tests/test_cron_commands.py`

- [ ] **Step 1: Write the failing cron-command tests**

```python
def test_cron_browser_revoke_removes_existing_trust(self) -> None:
    async def run_test() -> None:
        task = store.add_task("ou_user", "scratch", "0 * * * *", "run task", "note")
        store.approve_browser_trust(task.task_id, "ou_user")

        await app_module._handle_cron_command("ou_user", f"/cron browser revoke {task.task_id}")

        self.assertFalse(store.is_browser_trusted(task.task_id, "ou_user"))


def test_cron_browser_revoke_reports_missing_trust(self) -> None:
    async def run_test() -> None:
        ...
        self.assertIn("未找到", send_text.await_args.args[1])
```

- [ ] **Step 2: Run the cron-command tests to verify the subcommand does not exist yet**

Run: `python -m unittest tests.test_cron_commands -v`  
Expected: FAIL because `/cron browser revoke` is not handled.

- [ ] **Step 3: Implement the new `/cron browser revoke` branch**

Add handling in `app.py`:

```python
if sub == "browser":
    if len(parts) < 4 or parts[2].strip().lower() != "revoke":
        await feishu_client.send_text(open_id, "用法:/cron browser revoke <task_id>")
        return
    task_id = parts[3].strip()
    ok = scheduler_store.revoke_browser_trust(task_id, open_id)
    ...
```

User-facing responses:

- success: `🧹 已撤销定时任务 #<task_id> 的浏览器自动授权。`
- missing: `未找到任务 #<task_id> 的浏览器授权。`

- [ ] **Step 4: Re-run the cron-command tests**

Run: `python -m unittest tests.test_cron_commands -v`  
Expected: PASS with revoke behavior and user-facing messages covered.

- [ ] **Step 5: Commit the revoke command**

```bash
git add app.py tests/test_cron_commands.py
git commit -m "feat: add cron browser revoke command"
```

## Task 5: Run End-To-End Regression For Browser And Scheduler Paths

**Files:**
- Modify: none unless a regression is found
- Test: `tests/test_scheduler_store.py`
- Test: `tests/test_scheduler_runner.py`
- Test: `tests/test_cron_commands.py`
- Test: `tests/test_browser_tools.py`
- Test: `tests/test_browser_commands.py`
- Test: `tests/test_feishu_client.py`

- [ ] **Step 1: Run the focused regression suite**

Run:

```bash
python -m unittest \
  tests.test_scheduler_store \
  tests.test_scheduler_runner \
  tests.test_cron_commands \
  tests.test_browser_tools \
  tests.test_browser_commands \
  tests.test_feishu_client -v
```

Expected: PASS with no new failures in existing browser or command behavior.

- [ ] **Step 2: Run a syntax sanity check on touched modules**

Run:

```bash
python -m py_compile \
  app.py \
  agent/run_context.py \
  agent/tools_browser.py \
  feishu/client.py \
  scheduler/store.py \
  scheduler/runner.py
```

Expected: no output, exit code 0

- [ ] **Step 3: Review the user-facing copy**

Confirm:

- scheduled first-time approval explicitly says future runs will auto-use the browser
- `/cron browser revoke <task_id>` help text and error text are clear
- normal interactive browser flows still say `/browser yes|no`

- [ ] **Step 4: Commit any final regression fixes**

```bash
git add app.py agent/run_context.py agent/tools_browser.py feishu/client.py scheduler/store.py scheduler/runner.py tests/test_scheduler_store.py tests/test_scheduler_runner.py tests/test_cron_commands.py tests/test_browser_tools.py tests/test_browser_commands.py tests/test_feishu_client.py
git commit -m "test: finalize scheduled browser trust rollout"
```
