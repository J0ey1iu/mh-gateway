from __future__ import annotations

import os
import typing
from typing import Any, TypeVar

from pydantic import BaseModel

from mh_gateway.adapters import ConfigProvider

T = TypeVar("T", bound=BaseModel)


class ConfigError(Exception):
    """配置解析错误：必填项缺失。"""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(f"Missing required config keys: {', '.join(missing)}")


def _env_key(prefix: str, field: str) -> str:
    """将前缀 + 字段名转为环境变量 key。

    例: ``("ORCH", "token_secret_key")`` → ``"ORCH_TOKEN_SECRET_KEY"``
         ``("my.registry", "api_url")`` → ``"MY_REGISTRY_API_URL"``
    """
    return f"{prefix}_{field}".replace(".", "_").upper()


def _remote_key(prefix: str, field: str) -> str:
    """将前缀 + 字段名转为远程配置中心 key。

    例: ``("ORCH", "token_secret_key")`` → ``"orch.token_secret_key"``
         ``("my.registry", "api_url")`` → ``"my.registry.api_url"``
    """
    return f"{prefix}.{field}".lower()


def _coerce_env_value(value: str, target_type: type[Any] | None) -> Any:
    """将环境变量字符串强制转为目标类型。

    当前支持：
    - ``list[str]`` : 逗号分割（``"a,b,c"`` → ``["a", "b", "c"]``）
    - ``bool`` : ``"true"/"false"``/``"1"/"0"``/``"yes"/"no"``/``"on"/"off"``
      （大小写不敏感）；其他值原样返回，由 Pydantic 处理
    - ``int`` / ``float`` : 严格转换，转换失败原样返回（由 Pydantic 抛错）
    - 其他类型直接返回原字符串（由 Pydantic 做后续转换）。
    """
    if target_type is None:
        return value
    origin = typing.get_origin(target_type)
    args = typing.get_args(target_type)
    if origin is list and args == (str,):
        return [v.strip() for v in value.split(",") if v.strip()]
    if target_type is bool:
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
        return value
    if target_type is int:
        try:
            return int(value)
        except ValueError:
            return value
    if target_type is float:
        try:
            return float(value)
        except ValueError:
            return value
    return value


class ConfigManager:
    """部署单元配置管理工具。

    优先级（每个字段独立）:

    1. ``{PREFIX}_{FIELD}`` 环境变量（已设置则优先）
    2. *sensitive* 字段 → ``secret_resolver.get(key)``
    3. *非* sensitive 字段 → ``config_provider.get(key)``
    4. 必填字段（无默认值）仍缺失 → 抛出 ``ConfigError``
    5. 可选字段（有默认值）仍缺失 → 使用模型默认值

    注：``config_provider`` 和 ``secret_resolver`` 均为 ``ConfigProvider``
    协议类型，可传入不同的实例以区分配置源与密钥源。开箱即用只需传入
    ``ConfigManager()``，不从远程配置中心读取。
    """

    def __init__(
        self,
        config_provider: ConfigProvider | None = None,
        secret_resolver: ConfigProvider | None = None,
    ) -> None:
        self._config = config_provider
        self._secret = secret_resolver

    async def resolve(
        self,
        schema_cls: type[T],
        *,
        prefix: str = "ORCH",
        sensitive_fields: set[str] | None = None,
        key_mapping: dict[str, str] | None = None,
    ) -> T:
        """从环境变量和远程配置中心解析出一个 Pydantic 模型实例。

        Args:
            schema_cls: 配置模型类（如 ``ConfigSchema``）。
            prefix: 环境变量和配置 key 的前缀。默认 ``"ORCH"``。
            sensitive_fields: 敏感字段集合（走 ``secret_resolver`` 实例）。
            key_mapping: key 重映射，格式 ``{field_name: remote_key}``。
                        仅在远程配置中心查询时生效，不影响 env var key。

        Returns:
            已解析的模型实例。

        Raises:
            ConfigError: 必填字段在所有来源中均缺失。
        """
        sensitive = sensitive_fields or set()
        mapping = key_mapping or {}
        kwargs: dict[str, Any] = {}
        missing: list[str] = []

        for field_name in schema_cls.model_fields:
            field_info = schema_cls.model_fields[field_name]
            has_default = not field_info.is_required()

            # ── 1. 环境变量 ──────────────────────────
            env_key = _env_key(prefix, field_name)
            value = os.environ.get(env_key)
            if value is not None:
                kwargs[field_name] = _coerce_env_value(value, field_info.annotation)
                continue

            # ── 2. 远程配置中心 ───────────────────────
            remote = mapping.get(field_name, _remote_key(prefix, field_name))
            if field_name in sensitive and self._secret is not None:
                value = await self._secret.get(remote)
            elif field_name not in sensitive and self._config is not None:
                value = await self._config.get(remote)

            if value is not None:
                kwargs[field_name] = value
                continue

            # ── 3. 仍缺失：必填则报错，可选则不计 ──────
            if not has_default:
                missing.append(field_name)

        if missing:
            raise ConfigError(missing)

        return schema_cls(**kwargs)


def setup_service_logging() -> None:
    """Initialise the root logger to emit to stderr in a uniform format.

    Idempotent: calling more than once is safe.
    """
    import logging
    import sys

    root = logging.getLogger()
    if getattr(setup_service_logging, "_initialised", False):
        return
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(name)s | %(levelname)-7s | %(message)s",
            )
        )
        root.addHandler(handler)
    if root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    setup_service_logging._initialised = True  # type: ignore[attr-defined]
