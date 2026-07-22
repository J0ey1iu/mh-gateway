import asyncio

import pytest

from mh_gateway import (
    AdapterLifespan,
    AuthorizationProvider,
    ConfigManager,
    ConfigProvider,
    ConfigSchema,
    DefaultLLMProviderService,
    EvalResultRepository,
    GatewayAdapters,
    LLMConfigBackend,
    LLMHeaderResolver,
    LLMProviderConfig,
    LLMProviderService,
    LLMResolveSpec,
    M2MAuthenticator,
    MetadataRepository,
    OutboundAuthProvider,
    OutboundRequestContext,
    SessionRepository,
    UserAuthenticator,
    UserIdentity,
    create_app,
    get_current_auth_token,
    get_current_cookies,
    get_current_locale,
    get_current_request,
    get_current_trace_id,
    get_current_user_id,
    has_broad_permission,
    match_permission,
)
from mh_gateway.config_manager import _coerce_env_value


def test_top_level_exports_available():
    assert AdapterLifespan is not None
    assert GatewayAdapters is not None
    assert ConfigManager is not None
    assert ConfigSchema is not None
    assert ConfigProvider is not None
    assert UserAuthenticator is not None
    assert AuthorizationProvider is not None
    assert M2MAuthenticator is not None
    assert OutboundAuthProvider is not None
    assert OutboundRequestContext is not None
    assert MetadataRepository is not None
    assert LLMProviderService is not None
    assert LLMResolveSpec is not None
    assert DefaultLLMProviderService is not None
    assert LLMConfigBackend is not None
    assert LLMHeaderResolver is not None
    assert LLMProviderConfig is not None
    assert SessionRepository is not None
    assert EvalResultRepository is not None
    assert UserIdentity is not None
    assert callable(match_permission)
    assert callable(has_broad_permission)


def test_create_app_is_callable():
    assert callable(create_app)


def test_get_current_helpers_callable():
    assert callable(get_current_request)
    assert callable(get_current_cookies)
    assert callable(get_current_auth_token)
    assert callable(get_current_user_id)
    assert callable(get_current_locale)
    assert callable(get_current_trace_id)


class TestCoerceEnvValue:
    def test_none_type_returns_value(self):
        assert _coerce_env_value("hello", None) == "hello"

    def test_list_str_comma_split(self):
        assert _coerce_env_value("a,b,c", list[str]) == ["a", "b", "c"]

    def test_list_str_strips_whitespace(self):
        assert _coerce_env_value(" a , b , c ", list[str]) == ["a", "b", "c"]

    def test_list_str_empty(self):
        assert _coerce_env_value("", list[str]) == []

    def test_bool_true_variants(self):
        for v in ("true", "True", "TRUE", "1", "yes", "YES", "on", "ON"):
            assert _coerce_env_value(v, bool) is True

    def test_bool_false_variants(self):
        for v in ("false", "False", "FALSE", "0", "no", "NO", "off", "OFF"):
            assert _coerce_env_value(v, bool) is False

    def test_bool_invalid_passthrough(self):
        assert _coerce_env_value("maybe", bool) == "maybe"

    def test_int_valid(self):
        assert _coerce_env_value("60", int) == 60
        assert _coerce_env_value("-5", int) == -5

    def test_int_invalid_passthrough(self):
        assert _coerce_env_value("not_a_number", int) == "not_a_number"

    def test_float_valid(self):
        assert _coerce_env_value("1.5", float) == 1.5
        assert _coerce_env_value("1e3", float) == 1000.0

    def test_float_invalid_passthrough(self):
        assert _coerce_env_value("nope", float) == "nope"

    def test_unknown_type_returns_value(self):
        assert _coerce_env_value("hello", str) == "hello"


class TestConfigManagerResolveWithCoercion:
    def test_int_field_coerced(self, monkeypatch):
        monkeypatch.setenv("ORCH_METRICS_PUSH_INTERVAL", "120")
        mgr = ConfigManager()
        cfg = asyncio.run(mgr.resolve(ConfigSchema, prefix="ORCH"))
        assert cfg.metrics_push_interval == 120
        assert isinstance(cfg.metrics_push_interval, int)

    def test_bool_field_coerced(self, monkeypatch):
        monkeypatch.setenv("ORCH_DEV_MODE", "true")
        mgr = ConfigManager()
        cfg = asyncio.run(mgr.resolve(ConfigSchema, prefix="ORCH"))
        assert cfg.dev_mode is True
        assert isinstance(cfg.dev_mode, bool)

    def test_list_str_field_coerced(self, monkeypatch):
        monkeypatch.setenv("ORCH_CORS_ORIGINS", "http://a.com,http://b.com")
        mgr = ConfigManager()
        cfg = asyncio.run(mgr.resolve(ConfigSchema, prefix="ORCH"))
        assert cfg.cors_origins == ["http://a.com", "http://b.com"]


def test_no_legacy_aliases():
    """Removed symbols must no longer be importable."""
    import mh_gateway

    legacy = [
        "AppState",
        "PermissionChecker",
        "UserAuthProvider",
        "M2MAuthProvider",
        "RegistryProvider",
        "MetadataManager",
        "LLMProviderFactory",
        "LLMProviderRegistry",
        "LLMProviderStore",
        "DatabaseProtocol",
        "SessionStoreProtocol",
        "EvalResultStorage",
        "SecretResolver",
        "LifespanHook",
        "ExtraHeadersProvider",
    ]
    for name in legacy:
        assert not hasattr(mh_gateway, name), f"{name} should have been removed"
