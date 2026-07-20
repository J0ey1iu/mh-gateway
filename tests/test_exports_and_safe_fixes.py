import asyncio
import warnings

import pytest

from mh_gateway import (
    AppState,
    ConfigManager,
    ConfigProvider,
    ConfigSchema,
    MetadataManager,
    OutboundAuthProvider,
    PermissionChecker,
    RegistryProvider,
    SecretResolver,
    UserAuthProvider,
    UserIdentity,
    create_app,
    get_current_auth_token,
    get_current_cookies,
    get_current_locale,
    get_current_request,
    get_current_trace_id,
    get_current_user_id,
    match_permission,
)
from mh_gateway.config_manager import _coerce_env_value


def test_top_level_exports_available():
    assert AppState is not None
    assert ConfigManager is not None
    assert ConfigSchema is not None
    assert ConfigProvider is not None
    assert SecretResolver is ConfigProvider
    assert MetadataManager is not None
    assert RegistryProvider is not None
    assert UserAuthProvider is not None
    assert PermissionChecker is not None
    assert UserIdentity is not None
    assert callable(match_permission)
    assert OutboundAuthProvider is not None


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


class TestRegistryProviderDeprecation:
    def test_setting_registry_provider_emits_warning(self):
        from mh_gateway.config import ConfigSchema

        state = AppState(settings=ConfigSchema())
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            state.registry_provider = None
        deprecation_warnings = [
            w for w in caught if issubclass(w.category, DeprecationWarning)
        ]
        assert deprecation_warnings, "expected a DeprecationWarning"
        msg = str(deprecation_warnings[0].message)
        assert "registry_provider" in msg
        assert "management_provider" in msg

    def test_setting_management_provider_does_not_warn(self):
        from mh_gateway.config import ConfigSchema

        state = AppState(settings=ConfigSchema())
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            state.management_provider = None
        deprecation_warnings = [
            w for w in caught if issubclass(w.category, DeprecationWarning)
        ]
        assert not deprecation_warnings

    def test_init_does_not_warn(self):
        from mh_gateway.config import ConfigSchema

        class _StubRegistry:
            async def get_agent(self, name):
                return None

            async def list_agents(self):
                return []

            async def get_tool(self, name):
                return None

            async def list_tools(self):
                return []

            async def get_scenario(self, scenario_id):
                return None

            async def list_scenarios(self):
                return []

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            AppState(settings=ConfigSchema(), registry_provider=_StubRegistry())
        deprecation_warnings = [
            w for w in caught if issubclass(w.category, DeprecationWarning)
        ]
        assert not deprecation_warnings


@pytest.mark.asyncio
async def test_warn_unknown_adapter_slots_logs_warning(caplog):
    from mh_gateway.app import _warn_unknown_adapter_slots
    from mh_gateway.config import ConfigSchema

    state = AppState(settings=ConfigSchema())
    state.foo = "typo"  # type: ignore[attr-defined]
    state.bar = "another typo"  # type: ignore[attr-defined]
    with caplog.at_level("WARNING", logger="orchestration.app"):
        _warn_unknown_adapter_slots(state)
    assert any("foo" in r.message and "bar" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_warn_unknown_adapter_slots_silent_for_known(caplog):
    from mh_gateway.app import _warn_unknown_adapter_slots
    from mh_gateway.config import ConfigSchema

    state = AppState(settings=ConfigSchema())
    state.token_verifier = None
    with caplog.at_level("WARNING", logger="orchestration.app"):
        _warn_unknown_adapter_slots(state)
    assert not any("unknown AppState" in r.message for r in caplog.records)
