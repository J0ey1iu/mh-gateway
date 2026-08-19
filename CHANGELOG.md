# Change Log

## 0.1.2a7

- release: lockstep bump to match the 0.8.1a5/0.1.2a7/0.1.2a8 publish
  set — `minimal-harness>=0.8.1a5` constraint updated (mh-local now
  ships `mh-gateway>=0.1.2a7`).
- feat(announcements): draft/publish lifecycle, validity window, media
  carousel, i18n content, draft-only delete (mh-gateway #43).
- feat(announcements): announcement image upload, scene binding, and
  style config (mh-gateway #42).
- fix(metrics): record LLM call failures in the error rate
  (mh-gateway #44).

## 0.1.2a6

- fix(bash): background children no longer block command completion
  (mh-incubator #75) — completion is now keyed on shell exit instead of
  pipe EOF, so a background child (`Start-Process` / `sleep &`)
  inheriting stdout/stderr can no longer keep the pipes open and make a
  finished foreground command (e.g. `curl`) hit the no-output timeout
  and get its tree killed; the tree is only killed on a real timeout
  (normal completion leaves background processes running and reports
  `background_processes=true`), Windows drops the `curl` ->
  `Invoke-WebRequest` alias, and timed-out results now include
  truncated/total output byte counts.

## 0.1.2a5

- fix: bash tool timeout now kills the whole process tree
  (`timeout_ms` renamed to `timeout` in seconds, default 300; on
  timeout `taskkill /T /F` on Windows, `os.killpg` with
  `start_new_session` on POSIX) so grandchildren can no longer hold
  the pipes open and hang `process.wait()`; all cleanup awaits are
  bounded (2s caps).
- fix: remove the agent-level no-progress watchdog
  (`_await_run_no_stall`) — the lower layers own their timeouts (LLM
  stall detection + tool timeouts) and their errors already propagate
  to the agent; a coarse watchdog preempted graceful tool timeout
  recovery.
- fix: handoff (`_handoff_fn`) forwards reasoning chunks so the parent
  sees progress during sub-agent thinking (no more false idle kills).
- chore: delete the unused HTTP handoff/execute endpoint and
  regenerate the OpenAPI route baseline.
- deps: minimal-harness>=0.8.1a3 -> >=0.8.1a4 (chunk-level stream
  stall watchdog).

## 0.1.2a4

- fix: chat runs are no longer force-cancelled after a fixed 60s total
  duration — long reasoning calls were killed mid-stream with
  "LLM call interrupted" / "Agent completed before tool finished"
  (mh-incubator #68).  The run finalizer now watches the task's
  `progress` heartbeat and only cancels when the run stops producing
  events for `RUN_IDLE_TIMEOUT` (120s), so legitimate long runs keep
  the session lock until they finish.
- deps: minimal-harness>=0.8.1a2 -> >=0.8.1a3 (run task `progress`
  heartbeat contract).

## 0.1.2a3

- fix: `SimpleSession` now subclasses `ConversationMemory` and the
  `Session` protocol inherits `Memory`, so the session's memory surface
  can no longer drift from the minimal-harness contract (fixes the
  `get_replay_messages` AttributeError on long-running sessions,
  mh-incubator #58). Contract tests in `tests/test_session_contract.py`
  fail the suite if a future Memory protocol member goes missing again.

- raise the one-shot agent run endpoint's `max_iterations` from 10 to
  2000 to match the framework default (silent early stop on long runs).

- fix: judge failures and unparseable judge output no longer silently
  end goal-controller runs as DONE — they now surface as errors, so a
  long loop can no longer "mysteriously stop" with no signal (mh-incubator
  #58).

- feat: attachment upload with docx/pptx/md/txt/msg extraction
  (mh-incubator #36) — `AttachmentStore` protocol + `AttachmentRecord`
  (app-side implementations), `read_attachment` / `list_attachments`
  tools with metadata in `attachment_tools.json` (import-ready, zh/en
  i18n), stdlib-only extractors (docx/pptx via ZIP+XML, msg via OLE2
  reader), `POST/GET /api/v1/attachments` (size/type checks, UTF-8
  filenames); the chat request accepts attachments, validates
  ownership, binds them to the session and injects attachment tools
  only when attachments are present; `session_id` contextvar.

- feat: announcements bulletin (mh-incubator #57, feature 2) —
  `AnnouncementRecord` / `AnnouncementStats` / `AnnouncementStore`
  protocols (gateway owns the contract, storage lives in deployments);
  admin API (CRUD / repush / stats behind `manage:announcement:*`);
  user API (visible / read / consent with `Literal[agree, decline]`);
  paginated history `GET /announcements?page=&page_size=`.

- fix(metrics): derive the tool name from the per-call event, not
  shared state (mh-incubator #62) — `MetricsPersistenceMiddleware`
  kept `_tool_name` on the instance while the harness runs the turn's
  tool calls concurrently, so concurrent calls clobbered each other's
  name (records landed under the wrong name or as "unknown"). The name
  is now read from the `tool_call` passed to `on_tool_end` /
  `on_tool_error`.

- feat(chat): detach runs from the SSE connection (mh-incubator #63) —
  runs survive client disconnect; the cancel endpoint writes a shared
  cancel marker polled by a per-run watcher (works across multi-POD
  workers); the sessions list surfaces real running/idle status from
  the shared store; the finalizer persists and releases the session
  lock even after disconnect, with a timeout fallback so a stuck run
  can never hold the lock forever.

## 0.1.2a2

- feat: JSON metadata import/export for scene / agent / tool —
  management endpoints to dump and restore registry metadata as JSON
  (mh-incubator #54).
- feat: split the monolithic `local_file_operator` into standalone
  file tools (mh-incubator #55) — `read_file` (line-based pagination
  via offset/limit, default 2000-line truncation so large files no
  longer flood the context), `write_file` / `append_file`
  (path+content enforced by the schema) and `edit_file` (exact
  find/replace, old_string/new_string required); each tool description
  carries routing rules preferring the dedicated file tool over
  `bash`, and `bash` gains the reverse hint. Dropped
  list_dir/glob/search/file_ops operations are covered by `bash`.

## 0.1.2a1

- feat(metrics): full paginated rankings, daily trend, display-name
  enrichment — `MetricsRepository` protocol gains `query_ranking`
  (server-side pagination; external backends push down LIMIT/OFFSET)
  and `query_trend` (zero-filled daily series); the summary loses its
  unused top-N lists. New `GET /api/v1/management/metrics/rankings`
  and `/metrics/trend` endpoints; read-time display-name resolution
  from metadata (scenes / agents / tools, locale-aware, storage
  records untouched); extended scalars (error_rate, avg tokens/call,
  active users, sessions, tool call/error counts).
- fix: `discover_agents` only lists the current scenario's agents —
  agent discovery now filters by the active scenario and the caller's
  `use:agent` permissions, matching the endpoint behaviour
  (mh-incubator #47).
- chore: pin `minimal-harness>=0.8.1a1` (lockstep pre-release
  alignment with the 0.8.1a1 publish set).

## 0.1.1

- feat(controller): `GoalController` / `TimerController` now live in
  `mh_gateway.services.controllers` (previously imported from
  `minimal-harness`) — the gateway implements the looping controllers
  itself against the SDK `Controller` protocol. Per-request
  `ChatRequest.controller` / `controller_config` route through to the
  runtime Controller layer (default / goal / timer); `GET
  /api/v1/management/controllers` catalog endpoint; goal / timer
  registered with config-backed defaults; ControllerStart / Continue /
  End events serialised on the chat SSE stream. 第 2 轮起的系统自动
  prompt 通过 `Agent.run(user_message_meta={"source": "auto"})` 打标记
  持久化，message API 项带 `auto: true`。timer 模式不再调 judge LLM —
  时间未到的下一轮输入直接以"用户期望"视角逻辑拼接，轮间零 LLM 等待。
- feat(metrics): persistent metrics repository, middleware, and
  management API (`/api/v1/management/metrics`).
- feat(feedback): user feedback endpoints (`POST /api/v1/feedback`,
  management list / detail / export), built-in `submit_feedback_fn`
  tool, session replay highlight matching; user-facing PUT/DELETE
  feedback routes with `update_content`; `submit_feedback` enforces
  session ownership and auto-links `target_id`.
- fix(csv): feedback export prepends a UTF-8 BOM and serves
  `text/csv; charset=utf-8` so Excel opens Chinese comments correctly
  (mh-incubator #39).
- feat(events): forward `MessageEvent` to the SSE stream (was skipped)
  and surface `message_id` on `AgentEnd` / `LLMEnd` / `CompactionEnd`
  — clients get the canonical `msg-{seq}` id for every message, so
  feedback / references committed during streaming match the ids
  returned after a session reload (mh-incubator #30).
- fix(feedback): session feedback list returned empty on refresh —
  `page_size=0` meant `LIMIT 0` in both stores; stores now treat
  `page_size <= 0` as "all" and the session list fetches all rows
  before filtering (mh-incubator #30).
- fix: serialize streaming tool-call deltas as `{index, id, name,
  arguments}` dicts (were repr strings via `json.dumps(default=str)`)
  so the frontend can render tool-call chunks live (mh-incubator #28).
- fix: chunked file-write progress — write/append/edit emit bounded
  `Writing: N/M chars` events instead of one silent write
  (mh-incubator #28).
- fix: bounded bash streaming — rolling 64KB tail window + batch flush
  (every 50 lines / 100ms) + truncation markers; a 10k-line traversal
  now emits ~200 progress events instead of 10k, and the event loop is
  no longer starved by O(n²) full-buffer joins (mh-incubator #25).
- feat(context): propagate full request context to remote tool services
  via the request body (`context` field). New `build_tool_context()`
  assembles user identity (from the full `UserIdentity` now stored in a
  contextvar via `set_current_identity`), trace id, locale, scenario /
  agent names and correlation id; `_tool_binding` wires it as a
  `context_provider` on every `RemoteToolBinding`. Headers keep carrying
  credentials; structured context travels in the body.
- feat(portal): user-facing scenario list API with agents, heat, and
  homepage flag; docs/portal-adaptation-guide.md for gateway SDK
  consumers.
- feat(prompt): system prompt assembly adapters — `GatewayAdapters`
  gains optional `system_prompt_assembler` / `system_prompt_provider` /
  `user_preference_provider` fields (types from
  `minimal_harness.agent.runtime`); `create_runtime()` passes them
  through to `AgentRuntime`. Chat run context now carries `user_id` so
  preference providers can resolve per-user prompts. All fields
  optional — existing deployments need no change. See
  `docs/system-prompt-assembly-guide.md` for customer adaptation.
- feat: run the `bash` tool in the current scene's folder by default.
- feat(handoff): forward sub-agent lifecycle events as tool progress
  chunks so the frontend sees streaming sub-agent activity.
- fix: 计时模式描述改为最少工作时间语义.
- chore: lockstep pins — `minimal-harness>=0.8.0`,
  `mh-service-kit>=0.1.2` (pre-release alignment across a1–a11).
- docs: customer-adaptation-guide documents the message-id convention
  and event-ordering contract for session adapters.

## 0.1.0a9

- chore: bump `minimal-harness` pin from `==0.7.0a7` to `==0.7.0a8`
  and `mh-service-kit` pin from `==0.1.1a6` to `==0.1.1a7`
  for lockstep pre-release alignment.

## 0.1.0a8

- chore: lockstep pre-release bump with the SDK chain
  (`minimal-harness` 0.7.0a8, `mh-service-kit` 0.1.1a6).

## 0.1.0a7

- fix: declare `python-multipart>=0.0.20` as a direct
  dependency. The `/api/v1/management/tools/upload` and
  `/tools/upload-batch` routes use `UploadFile`, and FastAPI
  requires `python-multipart` for multipart parsing. Previously
  the package relied on `mh-orch-app` pulling it in
  transitively in the workspace, so `uv tool install mh-local`
  (and any other standalone consumer) crashed at import time
  with `RuntimeError: Form data requires "python-multipart" to
  be installed.`

## 0.1.0a6

- chore: lockstep pre-release bump with the SDK chain
  (`mh-gateway` 0.1.0a6, `mh-service-kit` 0.1.1a6,
  `minimal-harness` 0.7.0a7). No API changes.

## 0.1.0a5

- **BREAKING**: collapse 13 adapter protocols into 9 unified
  contracts; `create_app` now takes a single
  `AdapterLifespan` returning an immutable `GatewayAdapters`
  bundle.  The mutable `AppState` with 14 named hook slots is
  removed.  Removed symbols (no longer importable from
  `mh_gateway`): `AppState`, `UserAuthProvider`, `PermissionChecker`,
  `M2MAuthProvider`, `OutboundAuthProvider` (now takes an
  `OutboundRequestContext`), `RegistryProvider`, `MetadataManager`,
  `LLMProviderFactory`, `LLMProviderRegistry`, `LLMProviderStore`,
  `DatabaseProtocol`, `SessionStoreProtocol`, `EvalResultStorage`,
  `SecretResolver`, `LifespanHook`, `ExtraHeadersProvider`.
- New unified abstractions: `UserAuthenticator`,
  `AuthorizationProvider`, `M2MAuthenticator`, `OutboundAuthProvider`
  (with `OutboundRequestContext`), `MetadataRepository`,
  `LLMProviderService` (with `DefaultLLMProviderService`,
  `LLMProviderConfig`, `LLMConfigBackend`, `LLMHeaderResolver`,
  `LLMResolveSpec`), `SessionRepository` (with `healthcheck()`),
  `EvalResultRepository`, `ConfigProvider`.
- Public session DTOs (`Session`, `SessionSummary`,
  `SimpleSession`) are now re-exported from
  `mh_gateway.session`; the old `database._session` private
  module is removed.
- Runtime: LLM credentials are pre-loaded by
  `llm.build_resolver(LLMResolveSpec)`, eliminating per-call
  database reads on the agent hot path.
- Outbound auth: M2M identity headers and the gateway-managed
  `x-user-id` fallback are consolidated into a single
  `OutboundAuthProvider.get_headers(OutboundRequestContext)` call.
- `/ready` now delegates to `SessionRepository.healthcheck()`.
- fix(llm): `create_llm` and `build_resolver` are now properly
  `async`; the legacy `asyncio.run` workaround is removed.
- fix(chat): `serialize_harness_event` returns a `dict` payload
  (the legacy double-encoding path is removed).
- fix(chat): SSE events are emitted with the flat schema expected by
  the Vue/TypeScript frontend (`LLMChunk.{content, reasoning,
  tool_calls}`, `LLMEnd.{content, reasoning_content, tool_calls,
  usage, error}`, etc., no `type` discriminator wrapper).
- tests: OpenAPI route surface pinned to 37 paths in
  `tests/baseline_openapi.json`; SSE event schema locked in
  `tests/test_event_schema.py`.
- docs: `docs/adapter-migration-guide.md` for customers migrating
  from the 14-hook `AppState` assembly to `AdapterLifespan`.
- `orch-app` (`mh-orch-app`) and `mh-local` updated to construct a
  single `AdapterLifespan`; `mh-local` no longer ships the
  `_NullDatabase` shim.
- chore: bump `minimal-harness==0.7.0a6` and `mh-service-kit==0.1.1a5` for lockstep pre-release alignment

## 0.1.3a1

- chore: remove dead `ConfigMapping` class (was exported but never used by `ConfigManager.resolve()`)
- chore: remove unused `ToolProvider` Protocol from `adapters.py`
- feat: export `UserAuthProvider`, `PermissionChecker`, `UserIdentity`, `match_permission`, `MetadataManager`, `RegistryProvider`, `ToolGenerator`, `AgentGenerator`, `InMemoryManagementProvider`, `DefaultAuthProvider`, `DefaultM2MAuthProvider`, `DefaultOutboundAuthProvider` at top-level `mh_gateway` package for easier imports
- feat: `ConfigManager` env-var coercion now supports `int` / `bool` / `float` natively (in addition to `list[str]`)
- feat: warn on `LifespanHook` setting an unknown `AppState` attribute (catches typos like `management_providers`)
- refactor: deprecate `AppState.registry_provider` field — use `management_provider` instead (still functional, emits `DeprecationWarning` on set)
- docs: fix `MetadataManager` / `RegistryProvider` import path in customer/dev guides (was incorrectly pointing at `minimal_harness.adapters`)

## 0.1.2

- feat: add `resolve_m2m_identity` for user-aware M2M permission checks
- feat: support M2M auth fallback on chat and sessions APIs
- feat: handoff message persistence, triage multi-agent coordination, M2M auth fixes
- feat: add `stop_agent` test tool and wire `stop` signal through API
- feat(handoff): enrich SSE events with chunk-level detail and streaming LLM content
- feat: add `verify_agent_tool_ssl` config for remote SSL verification
- feat(monitoring): add metrics collector, access log middleware, structured audit logging
- feat: add AI agent generator with trial chat (symmetrical to tool generator)
- feat: filter `discover_agents` by scenario and user permissions
- feat: resolve localized `display_name` and pass `display_name_locale` on session create
- refactor: merge `enable_builtin_agents` into `dev_mode`, extract dev runtime tools
- refactor: `_DefaultM2MAuthProvider` to log-only mode, remove auth control
- refactor(logging): deprecate `create_app()` logger param, use root logger instead
- refactor(db): extract `SessionStore` as pluggable adapter, remove OpenGauss built-in
- refactor: migrate auth to numeric user IDs and extract database module
- revert: remove per-user counters — audit logs as source of truth
- fix: improve chat SSE error handling — surface to user, preserve partial content
- fix: wrap SSE `event_stream` generators with top-level try/except for exception logging
- fix: exclude calling agent from `discover_agents` results
- fix: pass `scenario_id` to `create_session` in handoff/execute
- fix(metrics): add missing fields to `live_snapshot`, skip OPTIONS in middleware
- docs: add `stop_agent` to built-in tools list and description
- docs: sync dev-guide, customer-adaptation-guide, and README with current codebase
- chore: remove unused backward-compat aliases
- chore: add static directory for frontend SPA

## 0.1.1

- feat: add monitoring infrastructure — metrics collector, access log middleware, structured audit logging
- feat: add per-user metrics counters with TTL eviction
- refactor: extract SessionStore as pluggable adapter protocol

## 0.1.0

- feat: initial orchestration gateway service
- feat: scenario loading, agent routing, SSE event streaming
- feat: LifespanHook adapter layer (UserAuthProvider, PermissionChecker, MetadataManager, etc.)
- feat: ConfigManager with env/ConfigCenter/SecretResolver resolution pipeline
- feat: per-request context API (get_current_user_id, get_current_locale, etc.)
- feat: built-in agents (triage, code-reviewer, writer) with dev mode
- feat: management CRUD API for agents/tools/scenarios
- feat: M2M authentication for agent/tool execution endpoints
- feat: AI tool generator (LLM-powered tool creation)
- feat: permission middleware for runtime tool call authorization
- feat: built-in session store with SQLite backend
