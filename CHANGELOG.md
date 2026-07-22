# Change Log

## 0.2.0a1

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
- `orch-app` (`mh-orch-app`) and `mh-local` updated to construct a
  single `AdapterLifespan`; `mh-local` no longer ships the
  `_NullDatabase` shim.
- tests: OpenAPI route surface pinned to 37 paths in
  `tests/baseline_openapi.json`; both
  `mh_gateway`/`mh-orch-app`/`mh-local` ship updates to
  `tests/test_exports_and_safe_fixes.py`,
  `tests/test_monitoring.py`, `tests/test_access_log_context.py`,
  `tests/test_api.py`, and (for `mh-local`) new
  `tests/test_local_app.py` + `tests/test_storage.py`.
- chore: bump `minimal-harness>=0.7.0a1` and `mh-service-kit>=0.1.1a1` for pre-release alignment

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
