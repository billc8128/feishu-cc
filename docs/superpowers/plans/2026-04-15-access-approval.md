# Approval-Based Access Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the static private-chat whitelist with an approval-based access flow so users can self-apply and a single admin can approve them without redeploying.

**Architecture:** Keep access control outside the Claude agent path by handling approval commands directly in the webhook dispatcher. Store user access state and pending requests in SQLite, seed admins from environment variables, and make the event gate consult that store before allowing normal commands through.

**Tech Stack:** Python, FastAPI, SQLite, unittest, unittest.mock

---

### Task 1: Add access storage

**Files:**
- Create: `auth/store.py`
- Test: `tests/test_auth_store.py`

- [ ] Write failing tests for admin seeding, pending request creation, and approval transitions.
- [ ] Run `python -m unittest tests.test_auth_store -v` and confirm the new tests fail because the module does not exist yet.
- [ ] Implement a focused SQLite-backed access store with schema init, admin bootstrap, request lifecycle, and lookup helpers.
- [ ] Run `python -m unittest tests.test_auth_store -v` and confirm the tests pass.

### Task 2: Route approval commands outside the agent

**Files:**
- Modify: `app.py`
- Test: `tests/test_access_flow.py`

- [ ] Write failing tests covering `/apply`, `/status`, `/approve`, and rejection of normal commands for unapproved users.
- [ ] Run `python -m unittest tests.test_access_flow -v` and confirm the tests fail for the expected missing behavior.
- [ ] Implement dispatcher command handling for access commands and admin notifications without touching agent sessions.
- [ ] Run `python -m unittest tests.test_access_flow -v` and confirm the tests pass.

### Task 3: Swap whitelist checks for approval checks

**Files:**
- Modify: `config.py`
- Modify: `feishu/events.py`
- Modify: `tests/test_feishu_events.py`

- [ ] Add failing tests for allowed commands by access state and admin bypass behavior.
- [ ] Run `python -m unittest tests.test_feishu_events -v` and confirm the tests fail before the production changes.
- [ ] Implement admin env parsing plus event gating that allows approval commands for unapproved users and normal traffic only for approved/admin users.
- [ ] Run `python -m unittest tests.test_feishu_events -v` and confirm the tests pass.

### Task 4: Update user-facing docs and final verification

**Files:**
- Modify: `README.md`

- [ ] Update setup and operator docs to describe the approval workflow and admin environment variable.
- [ ] Run `python -m unittest tests.test_auth_store tests.test_access_flow tests.test_feishu_events -v`.
- [ ] If the targeted suite is green, summarize the new workflow and any remaining gaps.
