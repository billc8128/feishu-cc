# Browser Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Railway-deployable browser worker plus bot/agent integration for consent-gated browser automation with a spectator link and single-worker queueing.

**Architecture:** Keep the Feishu bot as the control plane and add a separate `browser/` FastAPI service as the execution plane. The bot exposes browser MCP tools to the agent, handles user approval in Feishu, and talks to the browser service over authenticated HTTP. The browser service owns the queue, browser processes, and per-user Chromium profiles.

**Tech Stack:** FastAPI, Playwright, Chromium, Xvfb, x11vnc, noVNC/websockify, unittest, httpx

---

### Task 1: Add browser-related configuration and approval registry

**Files:**
- Modify: `config.py`
- Create: `agent/browser_approval.py`
- Test: `tests/test_browser_approval.py`

- [ ] **Step 1: Write the failing test**

Create tests for:
- starting a pending approval request
- approving and denying a request
- timing out an expired request

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_browser_approval -v`
Expected: FAIL because the module does not exist yet.

- [ ] **Step 3: Write minimal implementation**

Add browser-related settings and an in-memory approval registry with async wait support and timeout cleanup.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_browser_approval -v`
Expected: PASS

### Task 2: Add browser service client and bot command handling

**Files:**
- Create: `agent/browser_client.py`
- Modify: `app.py`
- Test: `tests/test_browser_commands.py`

- [ ] **Step 1: Write the failing test**

Cover:
- `/browser yes` resolves approval
- `/browser no` denies approval
- `/browser status` queries the browser client
- `/browser close` closes the current browser session

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_browser_commands -v`
Expected: FAIL because command handlers do not exist yet.

- [ ] **Step 3: Write minimal implementation**

Add a lightweight async HTTP client for the browser service and route `/browser` commands inside `app._dispatch`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_browser_commands -v`
Expected: PASS

### Task 3: Add browser MCP tools for the agent

**Files:**
- Create: `agent/tools_browser.py`
- Modify: `agent/runner.py`
- Test: `tests/test_browser_tools.py`

- [ ] **Step 1: Write the failing test**

Cover:
- permission request sends a Feishu prompt and waits for approval
- browser open reuses/creates a session and returns viewer URL text
- browser actions call the browser client with the current user

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_browser_tools -v`
Expected: FAIL because the MCP server does not exist yet.

- [ ] **Step 3: Write minimal implementation**

Implement a browser MCP server mirroring the existing `schedule`/`deliver` pattern and register it in `agent/runner.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_browser_tools -v`
Expected: PASS

### Task 4: Implement browser service queue/session core

**Files:**
- Create: `browser/config.py`
- Create: `browser/state.py`
- Create: `browser/service.py`
- Create: `browser/app.py`
- Test: `tests/test_browser_service.py`

- [ ] **Step 1: Write the failing test**

Cover:
- ensuring a session starts immediately when idle
- ensuring a second user is queued
- ensuring the same user reuses the existing session
- closing the active session promotes the next queued user

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_browser_service -v`
Expected: FAIL because the browser service modules do not exist yet.

- [ ] **Step 3: Write minimal implementation**

Build the in-memory queue/session manager and expose it through FastAPI endpoints. Stub the actual process-launch calls behind a small driver interface so queue logic can be tested without launching Chromium.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_browser_service -v`
Expected: PASS

### Task 5: Add browser process driver and deployment assets

**Files:**
- Create: `browser/driver.py`
- Create: `browser/start.sh`
- Create: `browser/Dockerfile`
- Create: `browser/requirements.txt`
- Modify: `README.md`

- [ ] **Step 1: Write the failing test**

Add/adjust targeted tests for command generation or driver preconditions where feasible.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_browser_service -v`
Expected: FAIL for the new driver contract assertions.

- [ ] **Step 3: Write minimal implementation**

Add the real process launcher for Xvfb/x11vnc/noVNC/Chromium/Playwright plus browser-service Docker assets and setup notes.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_browser_service -v`
Expected: PASS

### Task 6: Run verification

**Files:**
- Modify: any files touched above as needed

- [ ] **Step 1: Run focused tests**

Run:
- `python -m unittest tests.test_browser_approval -v`
- `python -m unittest tests.test_browser_commands -v`
- `python -m unittest tests.test_browser_tools -v`
- `python -m unittest tests.test_browser_service -v`

Expected: all PASS

- [ ] **Step 2: Run broader regression tests**

Run:
- `python -m unittest tests.test_access_flow -v`
- `python -m unittest tests.test_feishu_events -v`

Expected: PASS

- [ ] **Step 3: Optional build verification**

Run:
- `python -m py_compile app.py agent/*.py browser/*.py`

Expected: no syntax errors
