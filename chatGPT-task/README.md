# ChatGPT Task — Real MCP Task Scheduler

A small, working MCP server that lets an MCP client (Claude Desktop, Claude
Code, or the MCP inspector) schedule and review tasks through standardized tool
calls. It is a ChatGPT Tasks-inspired scheduler built on the Python MCP SDK and
`FastMCP`.

## What it does

- Exposes MCP tools to create, list, check, and cancel scheduled tasks.
- Persists jobs to SQLite via SQLAlchemy 2.0.
- Runs a background **watcher** that scans for due jobs by hourly time bucket.
- Pushes due jobs into an in-memory queue consumed by a background **worker**
  that executes a placeholder action and records the result.

```txt
MCP Client
    |
    v
FastMCP Server
    |
    v
Tool handlers
    |
    v
SQLite Jobs DB
    ^
    |
Watcher scans due jobs -> in-memory queue -> Worker executes jobs
```

## Project structure

```txt
chatGPT-task/
|-- README.md
|-- requirements.txt
|-- .env.example
|-- app/
|   |-- __init__.py
|   |-- database.py      # engine, SessionLocal, Base, get_db
|   |-- models.py        # Job model + status constants
|   |-- scheduler.py     # time buckets, watcher_loop, worker_loop
|   `-- mcp_server.py    # FastMCP server, tool handlers, tools
`-- tests/
    |-- __init__.py
    |-- conftest.py
    |-- test_models.py
    |-- test_scheduler.py
    `-- test_tool_handlers.py
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m app.mcp_server
```

Copy `.env.example` to `.env` if you want to override defaults:

```bash
cp .env.example .env
```

## Run with the MCP inspector

```bash
npx @modelcontextprotocol/inspector python -m app.mcp_server
```

The inspector lets you list the registered tools and invoke them manually.

## Connect to Claude Desktop

Add an entry to your Claude Desktop MCP config (e.g.
`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "task-scheduler": {
      "command": "python",
      "args": ["-m", "app.mcp_server"],
      "cwd": "/absolute/path/to/chatGPT-task"
    }
  }
}
```

Restart Claude Desktop and the `task_*` tools will appear.

## Try the tools

The server registers four tools:

| Tool          | Description                                          |
| ------------- | ---------------------------------------------------- |
| `task_create` | Schedule a new task for future execution.            |
| `task_list`   | List all scheduled tasks.                            |
| `task_status` | Get the status of a scheduled task by `job_id`.      |
| `task_cancel` | Cancel a scheduled task that has not completed yet.  |

Example flow:

```txt
Schedule a task to review PR #123 tomorrow at 9am.
  -> task_create -> { "job_id": 1, "status": "pending", ... }

What's the status of that task?
  -> task_status -> { "job_id": 1, "status": "completed", "result": "Executed: ..." }
```

`task_create` takes a `description` and an ISO 8601 `scheduled_at`
(e.g. `2026-06-09T09:00:00`).

## Tests

```bash
pytest
```

If dependencies are not yet installed:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest
```

## Troubleshooting — MCP stdio and stdout

MCP stdio uses **stdout** for protocol messages. The server therefore never
calls `print()` to stdout — diagnostic messages are written to **stderr**. If
you add logging, keep it on stderr or write to a file. Stray stdout output will
corrupt the MCP protocol stream and break the connection.
