# Agent platform API

Run control, background jobs, research projects, the MCP server and the A2A endpoint (features 13, 15, 16, 17
of `docs/plans/agentic-system.md`). All routes use the same bearer authentication as `/api/chat`
(`PHRONESIS_API_TOKENS`, `PHRONESIS_JWT_SECRET`, or `PHRONESIS_AUTH_MODE=disabled` for local development) and
are scoped to the authenticated user: another user's run, job, project or task id answers exactly like an
unknown one (404, or JSON-RPC `-32001` for A2A).

The examples use `TOKEN` for a bearer token and `http://localhost:8000` for the backend.

## SSE events added by agentic runs

Agentic frameworks (`agentic-plan-execute`, `multi-agent-supervisor`, `multi-agent-swarm`,
`multi-agent-debate`) add two event types to the chat stream (`POST /api/chat/stream`) and to job event logs.
All other event types (`init`, `thought`, `reasoning_delta`, `content_delta`, `content_reset`, `done`,
`error`) are unchanged.

| Event | When | Shape |
|---|---|---|
| `run_control` | first event of an agentic run | `{"type":"run_control","run_id":"run_…","framework":"agentic-plan-execute","interactive":true}` |
| `approval_request` | a phase waits for the researcher | `{"type":"approval_request","run_id","approval_id":"apr_…","kind":"plan"\|"dag","payload":{…}}` |
| `job` | job status change (job logs only) | `{"type":"job","job_id","status","run_id"?,"resumed"?}` |

A plan approval payload:

```json
{
  "sources": [
    {"key": "synapta", "label": "Target-disease associations and pathways (OpenTargets, KEGG)", "run": true, "reason": ""},
    {"key": "websurfer", "label": "Web search", "run": true, "reason": ""},
    {"key": "vivarix", "label": "Kinetic model simulation (Vivarix, COPASI)", "run": false, "reason": "no simulation requested"}
  ],
  "expensive": ["parameter scan"],
  "question": "..."
}
```

Plan approval runs on interactive runs when `agent_options.require_plan_approval` is set or expensive work is
routed (simulation, parameter scan, single-cell analysis, calibration, docking, sandboxed analysis). A
non-interactive run with `require_plan_approval` is approved automatically and recorded as `auto`. With no
answer within `PHRONESIS_APPROVAL_TIMEOUT_S` (default 300 s) the plan runs as proposed (`timeout`).

A clarifying question ends the turn instead: the response is the question with numbered options,
`task_status.task_status` is `needs_input`, and the artifact `clarification` is
`{"question","options","reason","signals"}`. The next message in the thread is treated as the answer ("2" or
the option text): it is merged with the original question, and the agent never asks twice in a row.

## Run control: `/api/agent/runs/{run_id}`

| Method | Path | Body | Result |
|---|---|---|---|
| GET | `/api/agent/runs/{run_id}` | | `{"run_id","interactive","cancelled","pending":[{approval_id,kind,payload,created}],"decisions":[…],"queued_steering"}` |
| POST | `/api/agent/runs/{run_id}/approve` | `{"approval_id","approved",payload?,message?}` | `{"success":true,…}`; 409 if the approval is no longer pending |
| POST | `/api/agent/runs/{run_id}/steer` | `{"message"}` (at most 2000 characters) | `{"success":true,"queued_steering":n}` |
| POST | `/api/agent/runs/{run_id}/cancel` | `{"reason"?}` | `{"success":true,"cancelled":true}` |
| GET | `/api/agent/reliability` | | tool reliability snapshot (circuit breakers, retries, cache) when available |

Editing the plan can only switch sources off. Send the payload back with `"run": false` on the rows to drop
(or `{"sources": {"websurfer": false}}`). Rejecting the plan (`"approved": false`) keeps only the literature
search. The artifact `plan_approval` records `{"approval_id","status":"approved|edited|rejected|auto|timeout",
"sources","switched_off"}`.

```bash
# approve the plan but switch the web search off
curl -X POST http://localhost:8000/api/agent/runs/run_0123456789abcdef/approve \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"approval_id":"apr_0123456789","approved":true,"payload":{"sources":{"websurfer":false}}}'

# add an instruction while the run is in progress (read before synthesis and after each draft)
curl -X POST http://localhost:8000/api/agent/runs/run_0123456789abcdef/steer \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"message":"Focus on pancreatic ductal adenocarcinoma"}'
```

Run controls live in the server process; they disappear when the run ends.

## Background jobs: `/api/jobs`

A job runs one question without a client attached. The event log is durable (SQLite), ordered by `seq`,
and can be replayed and followed.

| Method | Path | Notes |
|---|---|---|
| POST | `/api/jobs` | body = the `/api/chat` request fields → `201 {"job_id","run_id","status":"queued","thread_id"}`; 429 over the per-user limit |
| GET | `/api/jobs` | `?status=&thread_id=&limit=` → `{"jobs":[…]}` (no results in the list) |
| GET | `/api/jobs/{id}` | the job with `result` (the chat response object), `pending_approvals`, `watchers`, `last_seq` |
| GET | `/api/jobs/{id}/events` | SSE: replay `?after=N` then follow; `Last-Event-ID` works; `?stream=false` returns JSON |
| POST | `/api/jobs/{id}/cancel` | queued: cancelled at once; running: cancelled at the next checkpoint |
| POST | `/api/jobs/{id}/resume` | interrupted, failed or cancelled jobs run again (new `run_id`; tool calls hit the result cache) |
| DELETE | `/api/jobs/{id}` | cancels if needed, then deletes the job and its log |

Statuses: `queued`, `running`, `waiting_approval`, `succeeded`, `failed`, `cancelled`, `interrupted`. Jobs
left running when the server stops are marked `interrupted` at the next start. Each SSE frame is
`id: <seq>` plus `data: <event JSON with "seq">`. A `: ping` comment is sent while idle. The stream ends after
the current attempt's `done` or `error` event. While a client is attached the run is interactive, so plan
approvals wait for an answer through `/api/agent/runs/{run_id}/approve`. When the last watcher leaves, a
pending approval is approved automatically.

```bash
curl -X POST http://localhost:8000/api/jobs -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"message":"What diseases is KRAS associated with?","framework":"agentic-plan-execute","agent_options":{"require_plan_approval":true}}'

# reattach after event 42
curl -N "http://localhost:8000/api/jobs/job_0123456789abcdef/events?after=42" -H "Authorization: Bearer $TOKEN"
```

A runtime `api_key` sent with a job is kept in memory only, never written to the job store. After a
restart, a resumed job runs with the server's keys.

## Research projects: `/api/projects`

| Method | Path | Body / query |
|---|---|---|
| POST | `/api/projects` | `{"name","description"}` → `201` project (`id` = `prj_…`) |
| GET | `/api/projects` | list with fact counts |
| GET / PATCH / DELETE | `/api/projects/{id}` | PATCH `{"name"?,"description"?}`; DELETE removes the project and its facts |
| GET | `/api/projects/{id}/memory` | `?query=` ranked recall (what the agent would get), else newest first; `include_invalid=true` shows history |
| PATCH | `/api/projects/{id}/memory/{fact_id}` | `{"invalidate":true,"reason"}` or an edit `{"object"?,"value"?,"confidence"?,"verified"?,"tags"?}` |
| DELETE | `/api/projects/{id}/memory/{fact_id}` | hard delete |

Send `project_id` with a chat or job request to use the project's memory. A fact is
`{"id":"fct_…","subject","relation","object","value","valid_from","invalid_at","recorded_at","source":{"thread_id","run_id","tool"},"confidence","verified","tags"}`.
A contradiction (a new classification, mechanism or association score) invalidates the old fact and does not
delete it. Editing an object or value creates a new version of the fact. Facts from turns that failed
verification are tagged `unverified`. The artifact `project_memory` is
`{"project_id","recalled":[…],"written","invalidated"}`.

## MCP server: `POST /mcp`

JSON-RPC 2.0 over Streamable HTTP. Responses are always JSON; `GET /mcp` returns 405. Supported protocol
versions are `2026-07-28`, `2025-11-25`, `2025-06-18` and `2025-03-26`. `initialize` echoes a supported
client version and otherwise answers `2025-11-25`. An unsupported `MCP-Protocol-Version` header (or
`_meta["io.modelcontextprotocol/protocolVersion"]`) is rejected with `-32600`.

Methods: `initialize`, `notifications/initialized` (202, no body), `ping`, `server/discover`, `tools/list`,
`tools/call`. Error codes: `-32700` parse error, `-32600` invalid request, `-32601` method not found,
`-32602` invalid params or unknown tool, `-32603` internal error. A tool that runs and fails returns
`isError: true`.

Tools:
- `phronesis_research` `{question, framework?, enable_web_search?, thread_id?}` runs the full orchestrator
  non-interactively. It returns the answer as text plus `structuredContent`
  `{answer, citations, verification, task_status, framework, thread_id}`.
- Read-only registry tools from `PHRONESIS_MCP_TOOLS` (comma-separated). The default list is the OpenTargets
  search and associations, KEGG, literature, clinical trials, ChEMBL resolve and profile, disease targets,
  variant intelligence and PrimeKG query, plus structure and omics tools when they are registered. Each
  `inputSchema` is the tool's pydantic schema. Tools whose names suggest writes, uploads, code execution,
  docking or generation are never exposed.

```bash
curl -X POST http://localhost:8000/mcp -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -H "MCP-Protocol-Version: 2025-11-25" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"synapta_kegg_pathways","arguments":{"target_symbol":"BRAF"}}}'
```

Client configuration (Claude Desktop, Claude Code or any client that supports remote Streamable HTTP servers):

```json
{
  "mcpServers": {
    "phronesis": {
      "type": "http",
      "url": "http://localhost:8000/mcp",
      "headers": {"Authorization": "Bearer YOUR_PHRONESIS_TOKEN"}
    }
  }
}
```

Security, following Hou et al. 2025 (arXiv:2503.23278):
- The tool allowlist is fixed.
- No write tools are exposed.
- Tool descriptions are static.
- Bearer authentication is required.
- `Origin` is checked against `PHRONESIS_CORS_ORIGINS` plus `PHRONESIS_MCP_ALLOWED_ORIGINS` (DNS-rebinding
  protection).
- Each user can run at most `PHRONESIS_MCP_MAX_CONCURRENT` (default 2) `tools/call` requests at once.
- The server is stateless: no sessions and no sampling.

## A2A: `/.well-known/agent-card.json` and `POST /a2a`

The A2A v1.0.0 JSON-RPC binding (https://a2a-protocol.org/latest/specification/). The Agent Card is public.
It lists the skills `biomedical-research` and `model-simulation`, a bearer security scheme, and
`capabilities.streaming = false`.

Methods: `SendMessage`, `GetTask`, `ListTasks` and `CancelTask`. The pre-1.0 aliases `message/send`,
`tasks/get`, `tasks/list` and `tasks/cancel` are also accepted. Every message becomes a background job:
- The task `id` is the job id and `contextId` is the thread.
- `status.state` is one of `submitted`, `working`, `input-required`, `completed`, `failed` or `canceled`.
- A completed task carries the answer as a text artifact.
- `configuration.blocking: true` waits for the answer, up to `PHRONESIS_A2A_BLOCKING_TIMEOUT_S` (default
  300 s).

Errors: `-32001` task not found, `-32002` task not cancelable.

```bash
curl -X POST http://localhost:8000/a2a -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{
  "jsonrpc":"2.0","id":1,"method":"SendMessage",
  "params":{"message":{"role":"user","messageId":"m1","parts":[{"kind":"text","text":"What diseases is KRAS associated with?"}]},
            "configuration":{"blocking":true}}}'
```

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `PHRONESIS_APPROVAL_TIMEOUT_S` | 300 | how long a plan approval waits |
| `PHRONESIS_JOBS_DIR` | `$PHRONESIS_RUNS_DIR/jobs`, else `data/jobs` | job store |
| `PHRONESIS_JOB_WORKERS` | 2 | asyncio job workers |
| `PHRONESIS_MAX_JOBS_PER_USER` | 3 | queued + running jobs per user |
| `PHRONESIS_JOB_MAX_EVENTS` | 20000 | log cap (token deltas are dropped past it) |
| `PHRONESIS_JOB_CANCEL_GRACE_S` | 20 | after this long a cancelled agentic job's task is cancelled outright |
| `PHRONESIS_PROJECTS_DIR` | next to the clinical memory store | project store |
| `PHRONESIS_MCP_TOOLS` | built-in read-only list | MCP registry-tool allowlist |
| `PHRONESIS_MCP_MAX_CONCURRENT` | 2 | concurrent MCP `tools/call` per user |
| `PHRONESIS_MCP_ALLOWED_ORIGINS` | (none) | extra allowed `Origin` values for `/mcp` |
| `PHRONESIS_MCP_RESEARCH_TIMEOUT_S` | 900 | `phronesis_research` timeout |
| `PHRONESIS_A2A_BLOCKING_TIMEOUT_S` | 300 | blocking `SendMessage` wait |

Jobs and run controls assume one server process per jobs directory. Startup recovery marks every running job
interrupted.

## References

- CLAM (arXiv:2212.07769), Clarify When Necessary (arXiv:2311.09469), Aliannejadi et al. (arXiv:1907.06554):
  clarification.
- Magentic-UI (arXiv:2507.22358): co-planning and action guards.
- LangGraph persistence (https://docs.langchain.com/oss/python/langgraph/persistence): durable jobs. Living
  systematic reviews (Elliott et al. 2014, doi:10.1371/journal.pmed.1001603) motivate long-running research.
- Zep (arXiv:2501.13956), Mem0 (arXiv:2504.19413), HippoRAG (arXiv:2405.14831), Generative Agents
  (arXiv:2304.03442), MemGPT (arXiv:2310.08560), A-MEM (arXiv:2502.12110): project memory.
- MCP specification (https://modelcontextprotocol.io/specification/2025-11-25/,
  https://modelcontextprotocol.io/specification/2026-07-28/), A2A specification
  (https://a2a-protocol.org/latest/specification/), Hou et al. 2025 (arXiv:2503.23278): interoperability and
  security.
