# Browser Takeover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the existing spectator-only browser viewer into an explicit takeover flow where the user can pause agent automation, interact with the live browser, and return control to the agent.

**Architecture:** Extend the current single-worker browser session state machine with a `controller` field and explicit takeover/resume APIs. Replace the direct noVNC redirect with a small wrapper page that embeds noVNC, shows control state, and toggles between read-only and interactive modes. Propagate the paused-for-takeover state through the browser service client and browser MCP tools so the agent stops cleanly and can continue after resume.

**Tech Stack:** FastAPI, Playwright/Chromium, noVNC, httpx, Python `unittest`

---

## File Structure

### Existing files to modify

- `browser/service.py`
  Adds takeover state, serializes control metadata, and blocks browser actions while the human controls the session.
- `browser/app.py`
  Exposes takeover/resume endpoints and replaces the plain `/view/{viewer_token}` redirect with a wrapper page.
- `agent/browser_client.py`
  Adds takeover/resume/status client methods and preserves structured pause errors from the browser service.
- `agent/tools_browser.py`
  Updates user-facing browser messages and handles paused-for-takeover responses as a wait state instead of a generic failure.
- `tests/test_browser_service.py`
  Covers session controller transitions and browser action blocking while taken over.
- `tests/test_browser_tools.py`
  Covers paused-for-takeover handling and any changed `browser_open` messaging.
- `tests/test_browser_commands.py`
  Keeps existing `/browser` commands compatible and verifies status output if controller metadata is surfaced there.

### New files to create

- `browser/viewer_page.py`
  Renders the lightweight takeover wrapper HTML instead of embedding a large string in `browser/app.py`.
- `tests/test_browser_app.py`
  Covers the viewer page response and the new takeover/resume HTTP endpoints.

## Task 1: Extend The Session State Machine

**Files:**
- Modify: `browser/service.py`
- Test: `tests/test_browser_service.py`

- [ ] **Step 1: Write the failing session-control tests**

```python
def test_takeover_switches_controller_to_human(self) -> None:
    async def run_test() -> None:
        await self.manager.ensure_session("ou_a", public_base_url="https://browser.example.com")

        session = await self.manager.takeover("ou_a")

        self.assertEqual(session["controller"], "human")
        self.assertEqual(session["paused_reason"], "takeover")

    asyncio.run(run_test())


def test_resume_switches_controller_back_to_agent(self) -> None:
    async def run_test() -> None:
        await self.manager.ensure_session("ou_a", public_base_url="https://browser.example.com")
        await self.manager.takeover("ou_a")

        session = await self.manager.resume("ou_a")

        self.assertEqual(session["controller"], "agent")
        self.assertEqual(session["paused_reason"], "")

    asyncio.run(run_test())


def test_browser_actions_fail_while_human_controls_session(self) -> None:
    async def run_test() -> None:
        await self.manager.ensure_session("ou_a", public_base_url="https://browser.example.com")
        await self.manager.takeover("ou_a")

        with self.assertRaisesRegex(RuntimeError, "BROWSER_PAUSED_FOR_TAKEOVER"):
            await self.manager.navigate("ou_a", "https://example.com")

    asyncio.run(run_test())
```

- [ ] **Step 2: Run the browser-service tests to verify the new behavior fails first**

Run: `python -m unittest tests.test_browser_service -v`
Expected: FAIL because `takeover`, `resume`, controller serialization, and pause handling do not exist yet.

- [ ] **Step 3: Implement the minimal session-control model in `browser/service.py`**

Add:

```python
@dataclass
class SessionRecord:
    ...
    controller: str = "agent"
    paused_reason: str = ""
    last_control_change_at: float = 0.0
```

Add methods:

```python
async def takeover(self, open_id: str) -> Dict[str, Any]:
    ...

async def resume(self, open_id: str) -> Dict[str, Any]:
    ...
```

Update browser actions to call a shared helper:

```python
def _require_agent_control_locked(self, record: SessionRecord) -> None:
    if record.controller != "agent":
        raise RuntimeError("BROWSER_PAUSED_FOR_TAKEOVER")
```

- [ ] **Step 4: Re-run the browser-service tests**

Run: `python -m unittest tests.test_browser_service -v`
Expected: PASS with controller transitions and pause blocking covered.

- [ ] **Step 5: Commit the state-machine change**

```bash
git add browser/service.py tests/test_browser_service.py
git commit -m "feat: add browser takeover session state"
```

## Task 2: Add The Viewer Wrapper And Takeover APIs

**Files:**
- Modify: `browser/app.py`
- Create: `browser/viewer_page.py`
- Test: `tests/test_browser_app.py`

- [ ] **Step 1: Write the failing app tests**

```python
def test_view_page_returns_wrapper_html(self) -> None:
    response = self.client.get("/view/viewer-ou_a")
    self.assertEqual(response.status_code, 200)
    self.assertIn("Take Over", response.text)
    self.assertIn("Resume Agent", response.text)


def test_takeover_endpoint_switches_session_controller(self) -> None:
    response = self.client.post(
        "/v1/sessions/ou_a/takeover",
        headers={"Authorization": "Bearer browser-token"},
    )
    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.json()["controller"], "human")
```

- [ ] **Step 2: Run the new app test module to verify the feature is missing**

Run: `python -m unittest tests.test_browser_app -v`
Expected: FAIL because the module or endpoints do not exist yet.

- [ ] **Step 3: Create `browser/viewer_page.py` and wire it into `browser/app.py`**

Implementation notes:

- `browser/viewer_page.py` should render a small HTML page with:
  - status text
  - `Take Over` button
  - `Resume Agent` button
  - embedded noVNC iframe or bootstrap JS that points at `/novnc/vnc_lite.html`
- `browser/app.py` should:
  - return `HTMLResponse` from `/view/{viewer_token}`
  - add `POST /v1/sessions/{open_id}/takeover`
  - add `POST /v1/sessions/{open_id}/resume`
  - keep `GET /v1/sessions/{open_id}` returning the new controller fields

Minimal handler shape:

```python
@app.post("/v1/sessions/{open_id}/takeover", dependencies=[Depends(_require_auth)])
async def takeover_session(open_id: str) -> dict:
    return await manager.takeover(open_id)
```

- [ ] **Step 4: Re-run the app tests**

Run: `python -m unittest tests.test_browser_app -v`
Expected: PASS with wrapper HTML and takeover/resume endpoints working.

- [ ] **Step 5: Commit the viewer/API change**

```bash
git add browser/app.py browser/viewer_page.py tests/test_browser_app.py
git commit -m "feat: add browser takeover viewer"
```

## Task 3: Propagate Pause Semantics To The Bot And Agent Tools

**Files:**
- Modify: `agent/browser_client.py`
- Modify: `agent/tools_browser.py`
- Test: `tests/test_browser_tools.py`

- [ ] **Step 1: Write the failing tool-level tests**

```python
def test_browser_navigate_reports_human_takeover_pause(self) -> None:
    async def run_test() -> None:
        server = browser_tools.build_browser_mcp("ou_123")
        browser_navigate = server["tools"]["browser_navigate"]

        with patch(
            "agent.tools_browser.browser_client.navigate",
            new=AsyncMock(side_effect=RuntimeError("BROWSER_PAUSED_FOR_TAKEOVER")),
        ):
            result = await browser_navigate({"url": "https://example.com"})

        self.assertTrue(result["is_error"])
        self.assertIn("Resume Agent", result["content"][0]["text"])

    asyncio.run(run_test())
```

- [ ] **Step 2: Run the browser-tool tests first**

Run: `python -m unittest tests.test_browser_tools -v`
Expected: FAIL because takeover pause is still treated as a generic browser failure.

- [ ] **Step 3: Add structured client helpers and pause-aware tool messaging**

Implementation notes:

- `agent/browser_client.py` should add:
  - `takeover(open_id)`
  - `resume(open_id)`
- Do not add new Feishu commands for takeover/resume.
- In `agent/tools_browser.py`, add a small helper that detects `BROWSER_PAUSED_FOR_TAKEOVER` and returns a dedicated message, for example:

```python
def _takeover_pause_text() -> Dict[str, Any]:
    return _tool_text(
        "浏览器已交给你。处理完后点击浏览器页面里的 Resume Agent，我再继续。",
        is_error=True,
    )
```

- Keep `browser_open` messaging aligned with the new wrapper page wording:
  - "旁观/接管链接" instead of "旁观链接"

- [ ] **Step 4: Re-run the browser-tool tests**

Run: `python -m unittest tests.test_browser_tools -v`
Expected: PASS with pause-aware tool behavior.

- [ ] **Step 5: Commit the bot/agent propagation change**

```bash
git add agent/browser_client.py agent/tools_browser.py tests/test_browser_tools.py
git commit -m "feat: pause agent tools during browser takeover"
```

## Task 4: Keep Bot Commands And Status Output Consistent

**Files:**
- Modify: `app.py`
- Modify: `tests/test_browser_commands.py`

- [ ] **Step 1: Write or extend failing command tests**

```python
def test_browser_status_reports_controller_state(self) -> None:
    async def run_test() -> None:
        parsed = ParsedMessageEvent(...)

        with patch.object(app_module.feishu_client, "send_text", new=AsyncMock()) as send_text, patch(
            "agent.browser_client.browser_client.get_session",
            new=AsyncMock(return_value={"state": "active", "controller": "human"}),
        ):
            await app_module._dispatch(parsed)

        self.assertIn("human", send_text.await_args.args[1].lower())

    asyncio.run(run_test())
```

- [ ] **Step 2: Run the browser-command tests**

Run: `python -m unittest tests.test_browser_commands -v`
Expected: FAIL if status output does not surface controller information yet.

- [ ] **Step 3: Update `app.py` browser status text only where needed**

Implementation notes:

- keep the command surface unchanged:
  - `/browser yes`
  - `/browser no`
  - `/browser status`
  - `/browser close`
- if a session is active, status text should include:
  - state
  - controller
  - viewer URL if present

- [ ] **Step 4: Re-run the browser-command tests**

Run: `python -m unittest tests.test_browser_commands -v`
Expected: PASS.

- [ ] **Step 5: Commit the status-text update**

```bash
git add app.py tests/test_browser_commands.py
git commit -m "feat: show takeover state in browser status"
```

## Task 5: Full Verification And Deployment Prep

**Files:**
- Modify: `README.md` if the viewer wording or usage flow changed enough to confuse operators

- [ ] **Step 1: Run the focused browser-related test suite**

Run:

```bash
python -m unittest tests.test_browser_service -v
python -m unittest tests.test_browser_app -v
python -m unittest tests.test_browser_tools -v
python -m unittest tests.test_browser_commands -v
```

Expected: PASS.

- [ ] **Step 2: Run the broader regression suite that previously covered browser integration**

Run:

```bash
python -m unittest tests.test_browser_approval -v
python -m unittest tests.test_access_flow -v
python -m unittest tests.test_feishu_events -v
python -m py_compile app.py config.py agent/*.py browser/*.py feishu/*.py auth/*.py project/*.py scheduler/*.py media/*.py security/*.py
```

Expected: PASS.

- [ ] **Step 3: Update operator docs only if needed**

If changed, document that the viewer page now supports:

- passive observation by default
- explicit `Take Over`
- explicit `Resume Agent`

- [ ] **Step 4: Commit the verification/docs pass**

```bash
git add README.md tests/test_browser_app.py
git commit -m "docs: describe browser takeover flow"
```

