# Spec 02: MCP Inspector And Claude Desktop Testing

## 1. Overview

Spec 02 verifies the Spec 01 scheduler with real MCP clients: MCP Inspector
first, then Claude Desktop.

The scheduler remains strict and dumb. It validates explicit input, routes tool
calls, stores jobs, and returns structured JSON. It does not infer timezone,
parse natural language, search the internet, fetch news, summarize content, or
decide what the user meant.

The MCP client, LLM, or eval harness converts user requests into strict tool
arguments before calling the scheduler.

Core flows:

1. Discover tools with `tools/list`.
2. Inspect tool names, descriptions, and JSON schemas.
3. Call task tools from MCP Inspector.
4. Connect Claude Desktop to the same local MCP server.
5. Verify create/list/get/modify/delete from Claude Desktop.
6. Verify strict errors for missing or invalid arguments.

## 2. Scope

Included in Spec 02:

- MCP Inspector connection.
- Claude Desktop connection.
- `tools/list` validation.
- Manual MCP create/list/get/modify/delete tests.
- Strict validation and structured error checks.
- Client-side instructions for tool use.
- Trace expectations for future evals.

Not included in Spec 02:

- Scheduler inference.
- Natural-language date parsing.
- Timezone or location inference.
- Internet search.
- Finance/news API calls.
- Article summarization.
- Email or notification delivery.
- Eval runner implementation.
- Production queue hardening.

Those belong to later specs.

## 3. Design Rules

- MCP Inspector is the raw protocol validation client.
- Claude Desktop is the natural-language testing client.
- The scheduler receives explicit MCP arguments only.
- Relative phrases such as "tomorrow at 8" are resolved before
  `task.create@v1`.
- City names such as "Vancouver" are resolved outside the scheduler.
- The scheduler validates `America/Vancouver`; it does not infer it.
- Unsupported action names return `UNSUPPORTED_ACTION`.
- Missing required scheduling fields return `VALIDATION_ERROR`.
- Tool results are structured JSON.
- Claude may summarize JSON, but must not invent fields or claim unsupported
  external work was completed.

## 4. MCP Client Flow

The intended MCP flow is:

1. Client calls `tools/list`.
2. Client/model reads tool names, descriptions, and JSON schemas.
3. Client/model resolves the user request into explicit arguments.
4. Client sends a tool call to the MCP server.
5. MCP server routes through the registry.
6. Scheduler validates arguments and returns structured JSON.
7. Client/model summarizes the JSON for the user.

For scheduling, the scheduler should receive concrete fields:

```txt
action
job_params.type
job_params.time
job_params.schedule
job_params.timezone
```

It should not receive unresolved scheduling phrases in place of these fields.

## 5. Tool Contract

The MCP server exposes the Spec 01 task tools:

| Tool | Purpose |
| --- | --- |
| `task.create@v1` | Create a job and first run. |
| `task.list@v1` | List jobs for a user. |
| `task.get@v1` | View one job and recent runs. |
| `task.modify@v1` | Modify an existing job. |
| `task.delete@v1` | Soft-delete a job and cancel pending runs. |

Tool names use `namespace.verb@version` so tool catalogs stay stable and eval
traces can compare behavior across versions.

Schema requirements:

- Required fields are declared.
- Enum values are visible where supported.
- Unknown fields are rejected where supported.
- Descriptions are short and action-oriented.
- Error responses identify the invalid field when possible.

## 6. Tool Selection Rules

| Tool | Use when | Do not use when |
| --- | --- | --- |
| `task.create@v1` | The request has explicit `action`, `job_params.type`, and required schedule fields. | The request still needs timezone, date, or action inference. |
| `task.list@v1` | The user wants a list of jobs. | The user asks for one known job. |
| `task.get@v1` | The user asks for one known job by ID. | The user needs broad job discovery. |
| `task.modify@v1` | The user wants to edit an existing scheduled job. | The job is deleted, completed, running, or failed. |
| `task.delete@v1` | The user wants to cancel/remove a scheduled job. | The user wants to edit job fields. |

The server still enforces correctness even if a model chooses the wrong tool.

## 7. Client System Instructions

Claude Desktop or the eval harness should use instructions equivalent to:

```txt
You are using a strict task scheduler MCP server.

Do not rely on the scheduler to infer missing details. Before calling
task.create@v1, resolve the user's request into explicit action,
job_params.type, job_params.time or job_params.schedule, and
job_params.timezone.

If required scheduling information is missing, ask one concise clarification
question.

Do not fetch, summarize, email, browse, or call external APIs through the
scheduler. In this spec, scheduled execution is placeholder behavior only.

After a tool call, summarize the returned JSON accurately for the user. Do not
invent fields or claim external work was completed.
```

These are client-side instructions. They do not add inference to the scheduler.

## 8. MCP Inspector Setup

Run from the package directory:

```bash
cd chatGPT-task
npx @modelcontextprotocol/inspector .venv/bin/python -m app.mcp.server
```

If dependencies are missing:

```bash
cd chatGPT-task
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Expected behavior:

- Server starts without stdout log noise corrupting MCP protocol messages.
- `tools/list` returns the five versioned scheduler tools.
- Tool schemas are visible.
- Tool calls return structured JSON.
- Invalid tool calls return structured errors.

## 9. Claude Desktop Setup

Claude Desktop launches the MCP server as a local stdio server.

macOS config path:

```txt
~/Library/Application Support/Claude/claude_desktop_config.json
```

Project-specific config:

```json
{
  "mcpServers": {
    "task-scheduler": {
      "command": "/Users/deanzhou/Desktop/projects/Local-MCP-Task-Scheduler-A-Tool-Use-Evaluation-Pipeline/chatGPT-task/.venv/bin/python",
      "args": ["-m", "app.mcp.server"],
      "cwd": "/Users/deanzhou/Desktop/projects/Local-MCP-Task-Scheduler-A-Tool-Use-Evaluation-Pipeline/chatGPT-task"
    }
  }
}
```

After editing the config, restart Claude Desktop.

Stdio rule:

- stdout is reserved for MCP protocol messages.
- diagnostics go to stderr.

## 10. Manual MCP Tests

### Tool Discovery

Call `tools/list`.

Expected tools:

```txt
task.create@v1
task.list@v1
task.get@v1
task.modify@v1
task.delete@v1
```

Pass criteria:

- All five tools are present.
- Tool descriptions are readable.
- Input schemas and required fields are visible.

### Create Immediate Job

Call `task.create@v1`:

```json
{
  "user_id": "demo-user",
  "action": "generate_report",
  "job_params": {
    "type": "immediate"
  }
}
```

Pass criteria:

- Response has `ok: true`.
- Response includes a job ID.

### Create One-Time Job

Call `task.create@v1`:

```json
{
  "user_id": "demo-user",
  "action": "summarize_financial_news",
  "job_params": {
    "type": "one_time",
    "time": "2026-06-10T08:00:00",
    "timezone": "America/Vancouver"
  }
}
```

Pass criteria:

- Response has `ok: true`.
- Stored job keeps explicit timezone.
- Scheduler does not fetch or summarize finance news.

### Reject Missing Timezone

Call `task.create@v1`:

```json
{
  "user_id": "demo-user",
  "action": "summarize_financial_news",
  "job_params": {
    "type": "one_time",
    "time": "2026-06-10T08:00:00"
  }
}
```

Pass criteria:

- Response has `ok: false`.
- Error code is `VALIDATION_ERROR`.
- Error field identifies `job_params.timezone`.

### List, Get, Modify, Delete

Required checks:

- `task.list@v1` returns jobs for `user_id`.
- `task.get@v1` returns one known `job_id`.
- `task.modify@v1` changes only allowed fields.
- `task.delete@v1` soft-deletes the job.
- Missing IDs return `NOT_FOUND`.
- Unknown patch fields are rejected.
- Repeated delete is idempotent.

## 11. Claude Desktop Trace Example

User says:

```txt
send me the finance news at 8:00 tomorrow
```

Claude Desktop must not expect the scheduler to browse the internet or infer
hidden context. Expected trace:

1. Claude resolves "tomorrow" using client context or asks for clarification.
2. Claude resolves the user's timezone or asks for it.
3. Claude maps the request to a supported scheduler action.
4. Claude calls `task.create@v1` with explicit arguments.
5. Scheduler validates and stores a placeholder job.
6. Scheduler returns structured JSON.
7. Claude summarizes the scheduled job.

If the current date is June 9, 2026 and timezone is `America/Vancouver`, the
tool call is:

```json
{
  "tool": "task.create@v1",
  "args": {
    "user_id": "user_123",
    "action": "summarize_financial_news",
    "job_params": {
      "type": "one_time",
      "time": "2026-06-10T08:00:00",
      "timezone": "America/Vancouver"
    }
  }
}
```

The scheduler stores a placeholder job. Real search, summarization, and delivery
belong to later specs.

## 12. Error Contract

All MCP tool failures use the Spec 01 error shape:

```json
{
  "ok": false,
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "One-time and recurring jobs require an explicit IANA timezone.",
    "field": "job_params.timezone",
    "expected": "IANA timezone"
  }
}
```

Required error codes:

- `VALIDATION_ERROR`
- `NOT_FOUND`
- `PERMISSION_DENIED`
- `CONFLICT`
- `UNSUPPORTED_ACTION`
- `INTERNAL_ERROR`

Errors should be fixable by a client/model and identify the invalid field when
possible.

## 13. Acceptance Criteria

Spec 02 is complete when:

- MCP Inspector connects to the local server.
- Claude Desktop connects to the local server.
- `tools/list` shows the five versioned task tools.
- MCP Inspector can call create/list/get/modify/delete.
- Claude Desktop can call create/list/get/modify/delete.
- Missing required fields return structured errors.
- Invalid enum values return structured errors.
- Unknown fields are rejected where schema enforcement supports it.
- Claude Desktop summarizes scheduler JSON without changing its meaning.
- The scheduler contains no LLM-style inference.

## 14. Tests

Spec 02 should be verified with:

- MCP Inspector tool discovery.
- MCP Inspector create/list/get/modify/delete calls.
- MCP Inspector invalid-input calls.
- Claude Desktop connection test.
- Claude Desktop create/list/get/modify/delete prompts.
- Claude Desktop missing-timezone prompt.
- Trace notes showing tool selection and arguments.
