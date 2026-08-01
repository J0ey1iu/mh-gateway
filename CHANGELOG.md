# Change Log

## 0.1.1a3

- refactor(controller): `GoalController` / `TimerController` now live in
  `mh_gateway.services.controllers` (previously imported from
  `minimal-harness`) — the gateway implements the looping controllers
  itself against the SDK `Controller` protocol, validating that external
  apps can plug custom controllers into the layer.
- feat(controller): per-request `ChatRequest.controller` /
  `controller_config` routed through to the runtime Controller layer
  (default / goal / timer); `GET /api/v1/management/controllers` catalog
  endpoint; goal / timer registered with config-backed defaults;
  ControllerStart / Continue / End events serialised on the chat SSE
  stream.
- feat(controllers): goal/timer 第 2 轮起的系统自动 prompt 通过
  `Agent.run(user_message_meta={"source": "auto"})` 打标记持久化；
  message API 项带 `auto: true`，前端据此与真实用户输入区分渲染。
  需要 minimal-harness>=0.8.0a1。
- change(controllers): timer 模式不再调 judge LLM——时间未到的下一轮输入
  直接以"用户期望"视角逻辑拼接（期望至少投入的时长、已过时长、剩余时长、
  更好地完成用户指令），轮间零 LLM 等待。goal 模式保留 judge。

## 0.1.1a2

- feat(metrics): persistent metrics repository, middleware, and
  management API.
- feat(feedback): user feedback endpoints (`POST /api/v1/feedback`,
  management list / detail / export), built-in `submit_feedback_fn`
  tool, session replay highlight matching.

## 0.1.1a1

- chore: bump 0.1.0 → 0.1.1a1 (pre-release alignment).

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
