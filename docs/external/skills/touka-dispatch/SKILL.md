---
name: touka-dispatch
description: Connect to Touka's ToukaDispatch task system and execute assigned tasks when DISPATCH_TOKEN, DISPATCH_URL, and DISPATCH_BOARD environment variables are present. Automatically apply this skill when you detect these variables — they mean Touka has assigned you a mission and is waiting for you to authenticate, retrieve tasks, execute them, and report results back before exiting.
---

# ToukaDispatch Agent Skill

Touka has invoked you to complete a set of tasks via its ToukaDispatch coordination system. Your job is to authenticate, execute every assigned task, report results honestly, and stay until all tasks reach a terminal state.

## Prerequisites

This skill requires ToukaDispatch MCP server to be configured in your environment:

```json
"mcpServers": {
  "touka-dispatch": {
    "url": "<DISPATCH_URL>"
  }
}
```

`DISPATCH_URL` is already the complete Streamable HTTP endpoint (normally
`http://localhost:8083/mcp`). Do not append `/sse`. Legacy-only clients may explicitly connect
to `http://localhost:8083/sse` during the compatibility window, but application session tokens
are transport-bound and cannot cross between modern and legacy.

If ToukaDispatch MCP tools (`dispatch_handshake`, `dispatch_get_board`, `dispatch_update_task`) are not available, stop and report that the MCP server is not configured.

## What You Received

Check your environment variables — Touka set these when it invoked you:

| Variable | What it is |
|----------|-----------|
| `DISPATCH_TOKEN` | Your one-time mission token (scoped to this mission only, not the global master key) |
| `DISPATCH_URL` | Complete modern endpoint, e.g. `http://localhost:8083/mcp` |
| `DISPATCH_BOARD` | Your assigned mission ID (for reference) |

## Step 1: Authenticate

Call `dispatch_handshake` with your mission token. This is required before any other tool call — it establishes your scoped session.

```
dispatch_handshake(
    dispatch_token = <value of DISPATCH_TOKEN env var>,
    caller_name    = "<your tool name, e.g. claude-code>"
)
```

Save the `session_token` from the response. Every subsequent call uses it, not `DISPATCH_TOKEN`.
The mission token is consumed by this first successful handshake; never retry, log, or share it.
If your runtime permits it, remove the consumed credential from the child environment.

## Step 2: Get Your Tasks

```
dispatch_get_board(session_token = <session_token>)
```

This returns a Markdown mission board. Read it carefully — task `dependencies` define execution order. Do not start a task until all its dependencies are `DONE`.

Task statuses:
- `⬜ TODO` — not started
- `⏳ DOING` — in progress
- `✅ DONE` — completed (terminal)
- `FAILED` — failed (terminal)
- `BLOCKED` — blocked (terminal)

## Step 3: Execute Tasks

Work through each `TODO` task in dependency order.

**Mark as in-progress before starting** (Touka monitors this to detect stalled work):

```
dispatch_update_task(session_token, task_id, status="DOING")
```

**Report success with a concrete artifact reference:**

```
dispatch_update_task(
    session_token = <session_token>,
    task_id       = "<task id>",
    status        = "DONE",
    artifact_ref  = "<path, URL, or description of your output>"
)
```

**Report failure honestly with the specific reason:**

```
dispatch_update_task(
    session_token  = <session_token>,
    task_id        = "<task id>",
    status         = "FAILED",
    blocker_detail = "<specific reason — file not found / permission denied / etc.>"
)
```

A clear `FAILED` with a concrete `blocker_detail` is far more useful than a fabricated `DONE`. Touka can retry or reassign a failed task; it cannot debug a silent fake success.

**For multiple outputs, use `artifact_refs` (list) instead of `artifact_ref`:**

```
dispatch_update_task(
    session_token = <session_token>,
    task_id       = "<task id>",
    status        = "DONE",
    artifact_refs = ["path/to/file1.py", "path/to/file2.md"]
)
```

## Step 4: Stay Until Done

Do not exit until every task has reached a terminal state (`DONE`, `FAILED`, or `BLOCKED`). Touka is blocking on a callback from each terminal update — an early exit leaves tasks in `DOING` and stalls the entire pipeline indefinitely.

After all tasks are terminal, exit cleanly.

## Rules

**Do not call `dispatch_initialize_mission`** — you are not authorized to create missions. Your mission already exists; your only job is to execute it.

**Do not share or log `DISPATCH_TOKEN`** — treat it as a one-time password. It expires when this mission completes.

**Do not move session tokens between transports** — a token created on `/mcp` is invalid on
legacy `/sse`, and vice versa. Dispatch restart or mission rotation also invalidates the
ephemeral authorization.

**Respect dependencies** — check the `dependencies` field on each task before starting it. Starting a dependent task before its prerequisite finishes produces incorrect results.

**Do not re-open terminal states** — `DONE`, `FAILED`, and `BLOCKED` are final. Do not attempt to update a task that is already in a terminal state.

## artifact_ref Format

Use whichever is most specific and verifiable:

| Type | Example |
|------|---------|
| Relative path | `src/output/result.py` |
| Absolute path | `/app/data/reports/summary.md` |
| URL | `https://github.com/user/repo/pull/42` |
| Description | `"Refactored authentication module in auth_service.py lines 45–112"` |
