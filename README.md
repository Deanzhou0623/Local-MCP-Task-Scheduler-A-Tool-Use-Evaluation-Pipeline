# Local MCP Task Scheduler + Tool-Use Evals Pipeline

A local real MCP task scheduler used as a controlled environment for studying LLM tool use. The project starts with a working MCP server that can create, list, inspect, cancel, and execute scheduled jobs, then grows toward production scheduling and evals.

This project has two goals:

1. Build a working real MCP scheduler that can be tested with MCP Inspector and Claude Desktop.
2. Add production scheduler concepts from the design notes, including job run history, recurring jobs, durable execution, and external API actions.
3. Use the scheduler as the environment for a first LLM tool-use evals pipeline.

Example user request:

```txt
Every morning at 8am, summarize financial news for me.
```

Future recurring-job tool call shape:

```json
{
  "tool": "task.create",
  "args": {
    "type": "recurring",
    "schedule": "0 8 * * *",
    "timezone": "America/Vancouver",
    "action": "summarize_financial_news"
  }
}
```

## Status

This repository is currently in the project-planning / MVP-build stage. The current implementation direction is real MCP first: build a local MCP scheduler, verify it with MCP Inspector and Claude Desktop, then add external API integrations, production hardening, and the evals pipeline.

## MVP Scope

The first real MCP version should support:

- A real MCP server using the Python MCP SDK
- SQLite job persistence
- A single `Job` table
- Time bucket indexing for due-job lookup
- Watcher loop that scans for due jobs
- In-memory queue that simulates a production queue
- Worker loop that executes queued jobs
- MCP tools for create, list, status, and cancel
- MCP Inspector validation
- Claude Desktop testing

Deferred until later features:

- FastAPI / REST API
- Recurring jobs
- Cron parsing
- Timezone-heavy scheduling
- Separate `JobRun` history table
- Retry policy, DLQ, heartbeat, and durable queues
- External API actions
- PostgreSQL persistence
- Hosted deployment
- Evals pipeline

## Architecture

```txt
Natural-language scheduling request
        |
        v
MCP client, such as MCP Inspector or Claude Desktop
        |
        v
Real MCP server
        |
        v
Tool handlers
        |
        v
SQLite job store
        |
        v
Watcher -> in-memory queue -> worker
        |
        v
Job status and result
```

## Core MCP Tools

The first MCP server exposes these tools:

```txt
task.create@v1
task.list@v1
task.get@v1
task.modify@v1
task.delete@v1
```

The LLM or eval harness must provide strict scheduler arguments. For local-time
schedules, resolve relative time and timezone before calling `task.create@v1`:

```json
{
  "user_id": "user_123",
  "action": "summarize_financial_news",
  "job_params": {
    "type": "recurring",
    "schedule": "0 8 * * *",
    "timezone": "America/Vancouver"
  }
}
```

Each tool should include:

- Clear name
- Clear description
- Strict JSON schema
- Required fields
- Enum values
- Structured validation errors

Example `task.create@v1` schema:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["user_id", "action", "job_params"],
  "properties": {
    "user_id": {
      "type": "string",
      "description": "Owner boundary for the job."
    },
    "action": {
      "type": "string",
      "description": "Supported action name."
    },
    "job_params": {
      "type": "object",
      "additionalProperties": false,
      "required": ["type"],
      "properties": {
        "type": { "enum": ["immediate", "one_time", "recurring"] },
        "time": { "type": "string", "format": "date-time" },
        "schedule": { "type": "string" },
        "timezone": { "type": "string", "description": "IANA timezone." }
      }
    }
  }
}
```

Example structured validation error:

```json
{
  "error": {
    "code": "INVALID_SCHEDULED_AT",
    "message": "scheduled_at must be a valid ISO 8601 datetime.",
    "field": "scheduled_at",
    "retryable": true
  }
}
```

## Evals Pipeline

This project follows the basic eval loop from Anthropic's "Demystifying AI Evals for Agents":

```txt
define task dataset -> run model -> collect output and tool trace -> grade results -> analyze failures -> improve prompt/schema -> rerun evals
```

For this project:

```txt
scheduling task dataset -> model tool calls -> trace logger -> rule-based grader -> results.csv -> pandas analysis
```

Target model providers:

- OpenAI
- Anthropic
- Google Gemini

## Eval Dataset

Initial dataset path:

```txt
data/scheduling_tasks.json
```

Example dataset item:

```json
{
  "id": "one_time_review_pr",
  "input": "Schedule a task to review PR #123 tomorrow at 9am.",
  "expected_tool": "task.create",
  "expected_args": {
    "description": "review PR #123",
    "scheduled_at": "2026-06-09T09:00:00"
  },
  "grading_notes": {
    "requires_datetime": true,
    "expected_clarification": false
  }
}
```

## Metrics

Main metrics:

```txt
tool_selected_correct
schema_valid
required_fields_complete
job_type_correct
time_parse_correct
datetime_correct
schedule_correct
clarification_behavior_correct
structured_error_retry_success
trace_success
final_success
outcome_trace_mismatch
input_tokens
output_tokens
total_tokens
latency_seconds
estimated_cost
```

## Results

The eval runner should produce:

```txt
results/raw_outputs.jsonl
results/traces.jsonl
results/results.csv
```

The CSV should be easy to inspect with pandas and suitable for comparing model behavior across prompt/schema revisions.

## Tech Stack

```txt
MCP server: Python MCP SDK
Database: SQLite for MVP, PostgreSQL later, SQLAlchemy
Scheduler: watcher loop, in-memory queue, worker loop
MCP tool layer: JSON Schema tool definitions, structured tool results
Testing client: MCP Inspector, Claude Desktop
Evals later: Python, pandas, JSONL, CSV, Jupyter Notebook
Testing: pytest
DevOps later: Docker, Docker Compose, .env
```

## Planned Project Structure

```txt
chatGPT-task/
|-- docs/
|   |-- Q&A.md
|   |-- spec01_real_mcp_scheduler_core.md
|   |-- feature02_mcp_inspector_and_claude_desktop_testing.md
|   |-- feature03_tool_schema_and_error_contract.md
|   |-- feature04_integrate_external_apis.md
|   |-- feature05_job_execution_production_hardening.md
|   |-- feature06_job_run_history_and_recurring_jobs.md
|   |-- feature07_llm_tool_use_evals_pipeline.md
|   `-- feature08_results_analysis_and_iteration.md
|-- chatGPT-task/
|   |-- app/
|   |   |-- api/
|   |   |-- core/
|   |   |-- jobs/
|   |   |-- mcp/
|   |   `-- scheduler/
|   |-- tests/
|   |-- README.md
|   `-- requirements.txt
|-- PROMPT.md
`-- README.md
```

## Quickstart

These commands represent the intended local development flow for the real MCP scaffold:

```bash
cd chatGPT-task
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m app.mcp.server
```

Test with MCP Inspector:

```bash
npx @modelcontextprotocol/inspector python -m app.mcp.server
```

Run tests:

```bash
pytest
```

## Environment

The first MCP scaffold does not require provider API keys.

Later external API and eval features may need:

```txt
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GOOGLE_API_KEY=
```

Scheduler defaults:

```txt
DATABASE_URL=sqlite:///./chatgpt_task.db
```

## MVP Completion Criteria

The first real MCP MVP is complete when:

- `python -m app.mcp.server` starts a real MCP server.
- MCP Inspector can connect to the server.
- Claude Desktop can load the local MCP server as a testing client.
- `task.create@v1` creates a SQLite job.
- The watcher finds due jobs.
- The worker executes queued jobs and marks them completed.
- `task.list@v1` returns scheduled jobs.
- `task.get@v1` returns one job status.
- `task.modify@v1` modifies a scheduled job.
- `task.delete@v1` deletes a non-terminal job.
- Tool schemas and structured errors are documented.

## Roadmap

The roadmap is organized as independent specs under `docs/`:

Questions and design-review notes are recorded in [Q&A.md](docs/Q&A.md).

1. [Spec 01: Real MCP Scheduler Core](docs/spec01_real_mcp_scheduler_core.md)
   Build the local MCP server, SQLite job store, watcher, in-memory queue, worker, and create/view/modify/delete task tools.

2. [Feature 02: MCP Inspector And Claude Desktop Testing](docs/feature02_mcp_inspector_and_claude_desktop_testing.md)
   Verify the MCP server with MCP Inspector first, then connect Claude Desktop as the first natural-language testing client.

3. [Feature 03: Tool Schema And Error Contract](docs/feature03_tool_schema_and_error_contract.md)
   Tighten tool names, descriptions, JSON schemas, validation behavior, and structured errors.

4. [Feature 04: Integrate External APIs](docs/feature04_integrate_external_apis.md)
   Add real task actions that call external APIs, replacing placeholder execution behavior.

5. [Feature 05: Job Execution Production Hardening](docs/feature05_job_execution_production_hardening.md)
   Move from prototype watcher/worker behavior toward durable execution guarantees.

6. [Feature 06: Job Run History And Recurring Jobs](docs/feature06_job_run_history_and_recurring_jobs.md)
   Add JobRun history, recurring schedules, cron parsing, and timezone handling.

7. [Feature 07: LLM Tool Use Evals Pipeline](docs/feature07_llm_tool_use_evals_pipeline.md)
   Build the dataset, model runner, trace logger, grader, and CSV output for tool-use evals.

8. [Feature 08: Results Analysis And Iteration](docs/feature08_results_analysis_and_iteration.md)
   Analyze failures, compare models, improve prompts/schemas, and rerun evals.

## References

1. Anthropic Engineering Blog: [Demystifying AI Evals for Agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)

   Used for the eval loop:

   ```txt
   task dataset -> model run -> grader -> failure analysis -> improve -> rerun
   ```

2. Model Context Protocol: [Tools Documentation](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)

   Used for the real MCP tool layer:

   ```txt
   tool name
   tool description
   tool metadata
   tool list
   input schema
   structured tool result
   structured error
   ```

3. OpenAI API Docs: [Structured Outputs](https://platform.openai.com/docs/guides/structured-outputs) and [Function Calling](https://platform.openai.com/docs/guides/function-calling)

   Used for schema design and stable model output:

   ```txt
   strict JSON schema
   structured output
   function/tool calling
   schema design
   validation
   ```

4. OpenAI Cookbook: [Structured Outputs Intro](https://cookbook.openai.com/examples/structured_outputs_intro) and [Getting Started with OpenAI Evals](https://cookbook.openai.com/examples/evaluation/getting_started_with_openai_evals)

   Used for runnable examples of structured-output testing, grading, and iteration:

   ```txt
   runnable eval examples
   structured output checks
   grader logic
   result analysis
   iteration loop
   ```

## Notes

This project does not focus on complex multi-agent workflows. The first task dataset should be small, manually designed, and easy to inspect.

The value of this project is to build a clean first evals pipeline for studying LLM tool-use reliability in a constrained environment.
