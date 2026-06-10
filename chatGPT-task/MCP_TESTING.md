# MCP Inspector & Claude Desktop Testing (Spec 02)

Verification checklist for the strict scheduler against real MCP clients. The
scheduler stays **strict and dumb**: it validates explicit arguments, routes
tool calls, stores jobs, and returns structured JSON. It does **not** infer
timezone, parse "tomorrow", search the internet, fetch/summarize news, or send
anything. The client (LLM / eval harness) resolves the request into explicit
arguments *before* calling `task.create@v1`.

The protocol-level equivalents of these checks are automated in
`tests/test_mcp_server.py` (run `pytest tests/test_mcp_server.py`). This doc
covers the manual Inspector and Claude Desktop steps that can't be scripted.

## 1. MCP Inspector

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

Expected:

- Server starts with no stdout noise corrupting the protocol (diagnostics go to stderr).
- `tools/list` returns exactly the five versioned tools below.
- Tool descriptions, input schemas, and required fields are visible.
- Tool calls return structured JSON; invalid calls return structured errors.

| Tool | Purpose |
| --- | --- |
| `task.create@v1` | Create a job and its first run. |
| `task.list@v1` | List jobs for a user. |
| `task.get@v1` | View one job and recent runs. |
| `task.modify@v1` | Modify an existing job. |
| `task.delete@v1` | Soft-delete a job and cancel pending runs. |

## 2. Claude Desktop

1. Copy `claude_desktop_config.example.json` into your Claude Desktop config:
   `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS).
   Adjust the absolute paths if your checkout differs.
2. Restart Claude Desktop.
3. The `task.*@v1` tools appear in the tool list.

Stdio rule: stdout is reserved for MCP protocol messages; diagnostics go to
stderr. The server never `print()`s to stdout.

## 3. Client system instructions

Give Claude Desktop (or the eval harness) instructions equivalent to:

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

These are client-side instructions; they add no inference to the scheduler.

## 4. Manual test cases

| Check | Call | Pass criteria |
| --- | --- | --- |
| Discovery | `tools/list` | Five tools above; schemas + required fields visible. |
| Create immediate | `task.create@v1` `{user_id, action:"generate_report", job_params:{type:"immediate"}}` | `ok:true`, has `job.job_id`. |
| Create one-time | `… job_params:{type:"one_time", time:"2026-06-10T08:00:00", timezone:"America/Vancouver"}` | `ok:true`, timezone preserved; no news fetched/summarized. |
| Reject missing tz | same as above but omit `timezone` | `ok:false`, `VALIDATION_ERROR`, field `job_params.timezone`. |
| List / Get | `task.list@v1`, `task.get@v1` | Jobs for `user_id`; one job by `job_id`. |
| Modify | `task.modify@v1` | Only allowed fields change; unknown fields rejected. |
| Delete | `task.delete@v1` | Soft-deletes; repeated delete is idempotent. |
| Missing ID | `task.get@v1` unknown id | `NOT_FOUND`. |

## 5. Trace example

User: *"send me the finance news at 8:00 tomorrow"*

The client must not expect the scheduler to browse or infer. Expected trace:

1. Resolve "tomorrow" from client context (or ask).
2. Resolve the user's timezone (or ask).
3. Map to a supported action.
4. Call `task.create@v1` with explicit arguments.
5. Scheduler validates and stores a placeholder job.
6. Scheduler returns structured JSON.
7. Client summarizes the scheduled job.

If today is 2026-06-09 and the timezone is `America/Vancouver`, the call is:

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

Real search, summarization, and delivery are later features. The scheduler only
stores a placeholder job.
