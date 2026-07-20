from __future__ import annotations

import json


def parse_locale(accept_language: str | None = None) -> str:
    if accept_language:
        lang = accept_language.split(",")[0].split(";")[0].strip().lower()
        if lang in ("zh", "en"):
            return lang
    return "zh"


def resolve_locale(
    value: str,
    value_locale: str | None,
    locale: str,
) -> str:
    if locale and value_locale:
        try:
            locale_map = json.loads(value_locale)
            if isinstance(locale_map, dict) and locale in locale_map:
                return str(locale_map[locale])
        except (json.JSONDecodeError, TypeError):
            pass
    return value


def parse_locale_json(raw: str | None) -> dict[str, str] | None:
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return {k: str(v) for k, v in parsed.items()}
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def resolve_display_name(
    display_name: str,
    display_name_locale: str | None,
    locale: str,
) -> str:
    return resolve_locale(display_name, display_name_locale, locale)


def resolve_description(
    description: str,
    description_locale: str | None,
    locale: str,
) -> str:
    return resolve_locale(description, description_locale, locale)
