# ToukaDispatch External AI Guide

This document is for **external AI tools** (e.g., Claude Code CLI, other AI agents) that have been invoked by Touka to execute tasks via ToukaDispatch.

---

## What You Received

When Touka invoked you, the following environment variables were set:

| Variable | Description |
|----------|-------------|
| `DISPATCH_TOKEN` | Your one-time mission token (scoped to this task only) |
| `DISPATCH_URL` | Complete modern MCP endpoint (e.g., `http://localhost:8083/mcp`) |
| `DISPATCH_BOARD` | Your assigned mission board ID (for reference) |

**Important**: `DISPATCH_TOKEN` here is a per-mission token, not the global master key. It is consumed by the first successful handshake. Do not log, retry, or share it after authentication.

---

## Required Steps (follow in order)

### Step 1: Authenticate

Connect to ToukaDispatch using your MCP client (Streamable HTTP):

```
Server URL: <DISPATCH_URL>
```

`DISPATCH_URL` already includes `/mcp`; do not append `/sse` or another path. A legacy-only
client may explicitly use `http://localhost:8083/sse` during the compatibility window, but a
session token obtained on one transport cannot be used on the other.

Then call:

```
dispatch_handshake(
    dispatch_token = <value of DISPATCH_TOKEN env var>,
    caller_name    = "<your tool name, e.g. claude-code>",
    caller_version = "<optional version string>"
)
```

**Response**: `✅ Handshake successful. Session token: <session_token>`

Save the `session_token` — you will use it for all subsequent calls. Every tool request is
authenticated again, so reconnecting the MCP transport does not remove this requirement.

---

### Step 2: View Your Tasks

```
dispatch_get_board(session_token = <session_token>)
```

This returns a Markdown-formatted mission board showing all tasks with their current status:
- `⬜ TODO` — not started
- `⏳ DOING` — in progress
- `✅ DONE` — completed
- `FAILED` / `BLOCKED` — terminal states

---

### Step 3: Execute Tasks

For each `TODO` task, in order (respecting `dependencies`):

**Mark as in-progress:**
```
dispatch_update_task(
    session_token = <session_token>,
    task_id       = "<task id>",
    status        = "DOING"
)
```

**Complete the task, then report:**
```
dispatch_update_task(
    session_token = <session_token>,
    task_id       = "<task id>",
    status        = "DONE",
    artifact_ref  = "<path, URL, or description of your output>"
)
```

**If the task fails:**
```
dispatch_update_task(
    session_token    = <session_token>,
    task_id          = "<task id>",
    status           = "FAILED",
    blocker_detail   = "<reason for failure>"
)
```

**Multiple outputs:**
```
dispatch_update_task(
    session_token  = <session_token>,
    task_id        = "<task id>",
    status         = "DONE",
    artifact_refs  = ["path/to/file1.py", "path/to/file2.md"]
)
```

---

### Step 4: Finish

**Do not exit until all tasks have been updated to a terminal state** (`DONE`, `FAILED`, or `BLOCKED`).

Once all tasks are complete, your session ends naturally. Touka will be notified automatically via callback.

---

## Constraints

- Your `DISPATCH_TOKEN` is scoped to this mission only — you cannot access other mission boards
- Your mission token is single-use; a second handshake with it is rejected
- Session TTL: 3600 seconds from handshake
- A session is bound to the transport used during handshake and becomes invalid after Dispatch restarts or the mission generation changes
- Do not call `dispatch_initialize_mission` — only Touka's internal agents may create boards
- Do not attempt to use the global `DISPATCH_TOKEN` (you do not have it; what you received is the mission token)

---

## artifact_ref Format

`artifact_ref` and `artifact_refs` accept any of:
- Relative file path: `src/output/result.py`
- Absolute path: `/app/data/reports/summary.md`
- URL: `https://github.com/user/repo/pull/42`
- Plain description: `"Refactored authentication module in auth_service.py"`

---

## Example Session (pseudocode)

```python
# 1. Authenticate
result = dispatch_handshake(
    dispatch_token=os.environ["DISPATCH_TOKEN"],
    caller_name="claude-code"
)
session_token = result.split("Session token:")[-1].strip()
# Remove the consumed mission credential from the child environment if your runtime permits it.
del os.environ["DISPATCH_TOKEN"]

# 2. Get tasks
board = dispatch_get_board(session_token)
# Parse tasks from board output...

# 3. Execute each task
for task in todo_tasks:
    dispatch_update_task(session_token, task.id, "DOING")
    output = execute(task.description)
    dispatch_update_task(session_token, task.id, "DONE", artifact_ref=output)

# 4. Done — exit cleanly
```
