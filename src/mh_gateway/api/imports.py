"""JSON entity import/export: validation, payload building, and export helpers.

Import format: one JSON file per entity (or a JSON array bundle of
entities).  Each entity must be a JSON object carrying an ``entity_type``
field ("scene" | "agent" | "tool").  Relationship fields
(scene.agents / agent.tool_names) are NOT importable — the user must
configure them manually after import.

Validation is atomic at the API layer: every file in a batch must
pass, otherwise nothing is created.

Export mirrors the import format so an exported file can be re-imported
directly (single entity object, or array for batch export).
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

# Fields that express relationships between entities — rejected on import.
_RELATIONSHIP_FIELDS: dict[str, set[str]] = {
    "scene": {"agents"},
    "agent": {"tool_names"},
    "tool": set(),
}

# Field name → (value kind, required).  Mirrors the CRUD pydantic Create
# models (ScenarioCreate / AgentCreate / ToolCreate) minus relationship
# and audit fields.
_STR = "str"
_BOOL = "bool"
_DICT = "dict"
_OBJ = "object"  # locale value: plain string OR {"zh": ..., "en": ...}

_LOCALE_FIELDS = {
    "name_locale",
    "description_locale",
    "display_name_locale",
    "system_prompt_locale",
}

_ENTITY_SPECS: dict[str, dict[str, tuple[str, bool]]] = {
    "scene": {
        "id": (_STR, True),
        "name": (_STR, True),
        "name_locale": (_OBJ, False),
        "icon": (_STR, False),
        "description": (_STR, False),
        "description_locale": (_OBJ, False),
        "show_on_homepage": (_BOOL, False),
    },
    "agent": {
        "name": (_STR, True),
        "display_name": (_STR, False),
        "display_name_locale": (_OBJ, False),
        "description": (_STR, False),
        "description_locale": (_OBJ, False),
        "system_prompt": (_STR, False),
        "system_prompt_locale": (_OBJ, False),
        # provider / provider_name / model are machine-specific
        # (differ per instance) and are intentionally NOT importable.
        "llm_config": (_DICT, False),
        "agent_type": (_STR, False),
        "compaction": (_DICT, False),
        "tool_compaction": (_DICT, False),
    },
    "tool": {
        "name": (_STR, True),
        "display_name": (_STR, False),
        "display_name_locale": (_OBJ, False),
        "description": (_STR, False),
        "description_locale": (_OBJ, False),
        "parameters": (_DICT, False),
        "endpoint_url": (_STR, False),
        "source_code": (_STR, False),
        "script_path": (_STR, False),
    },
}

# Defaults applied when a field is omitted (mirrors the CRUD pydantic defaults).
_DEFAULTS: dict[str, dict[str, Any]] = {
    "scene": {"show_on_homepage": True},
    "agent": {"provider": "openai", "agent_type": "simple"},
    "tool": {},
}

_TEMPLATES: dict[str, dict[str, Any]] = {
    "scene": {
        "entity_type": "scene",
        "id": "my_scene",
        "name": "我的场景",
        "name_locale": {"zh": "我的场景", "en": "My Scene"},
        "icon": "✨",
        "description": "场景描述（可选）",
        "description_locale": {"zh": "场景描述", "en": "Scene description"},
        "show_on_homepage": True,
    },
    "agent": {
        "entity_type": "agent",
        "name": "my_agent",
        "display_name": "我的 Agent",
        "display_name_locale": {"zh": "我的 Agent", "en": "My Agent"},
        "description": "Agent 描述（可选）",
        "description_locale": {"zh": "Agent 描述", "en": "Agent description"},
        "system_prompt": "你是……",
        "system_prompt_locale": {"zh": "你是……", "en": "You are ..."},
        "llm_config": {"temperature": 0.7},
        "agent_type": "simple",
        "compaction": {},
        "tool_compaction": {},
    },
    "tool": {
        "entity_type": "tool",
        "name": "my_tool",
        "display_name": "我的工具",
        "display_name_locale": {"zh": "我的工具", "en": "My Tool"},
        "description": "工具描述（可选）",
        "description_locale": {"zh": "工具描述", "en": "Tool description"},
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "查询内容"}},
            "required": ["query"],
        },
        "endpoint_url": "",
        "source_code": "",
        "script_path": "",
    },
}


class ImportIssue(BaseModel):
    path: str
    message: str
    code: str = "invalid"
    line: int | None = None
    column: int | None = None


class ImportFileResult(BaseModel):
    filename: str
    ok: bool = True
    entity_type: str | None = None
    entity_key: str | None = None
    payload: dict[str, Any] | None = None
    issues: list[ImportIssue] = []


def _add_issue(
    result: ImportFileResult,
    path: str,
    message: str,
    code: str = "invalid",
    line: int | None = None,
    column: int | None = None,
) -> None:
    result.ok = False
    result.issues.append(
        ImportIssue(path=path, message=message, code=code, line=line, column=column)
    )


def _normalize_locale(value: Any) -> str:
    """Locale fields are stored as JSON strings; accept object form on import."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _validate_import_data(filename: str, data: Any) -> ImportFileResult:
    """Structure-validate one parsed JSON entity (object)."""
    result = ImportFileResult(filename=filename)

    if not isinstance(data, dict):
        _add_issue(result, "$", "实体必须是 JSON 对象（object），而非数组或基本类型")
        return result

    entity_type = data.get("entity_type")
    if entity_type not in _ENTITY_SPECS:
        _add_issue(
            result,
            "$.entity_type",
            '缺少或非法的 entity_type 字段：必须为 "scene"、"agent" 或 "tool"',
            code="invalid_entity_type",
        )
        return result
    result.entity_type = entity_type

    spec = _ENTITY_SPECS[entity_type]
    rel_fields = _RELATIONSHIP_FIELDS[entity_type]

    for key in data:
        if key == "entity_type":
            continue
        if key in rel_fields:
            _add_issue(
                result,
                f"$.{key}",
                f"字段 {key} 是实体之间的关联关系，不支持导入；请删除该字段，"
                "导入后在管理页面中手动配置关联",
                code="relationship_not_supported",
            )
        elif key not in spec:
            allowed = ", ".join(sorted(spec))
            _add_issue(
                result,
                f"$.{key}",
                f"未知字段 {key}（或当前不可导入）。允许的字段：{allowed}",
                code="unknown_field",
            )

    for field, (kind, required) in spec.items():
        if field not in data:
            if required:
                _add_issue(
                    result,
                    f"$.{field}",
                    f"缺少必填字段 {field}",
                    code="missing_field",
                )
            continue
        value = data[field]
        if kind == _STR:
            if not isinstance(value, str):
                _add_issue(
                    result,
                    f"$.{field}",
                    f"字段 {field} 必须是字符串，实际为 {type(value).__name__}",
                    code="invalid_type",
                )
        elif kind == _BOOL:
            if not isinstance(value, bool):
                _add_issue(
                    result,
                    f"$.{field}",
                    f"字段 {field} 必须是布尔值（true/false），实际为 {type(value).__name__}",
                    code="invalid_type",
                )
        elif kind == _DICT:
            if not isinstance(value, dict):
                _add_issue(
                    result,
                    f"$.{field}",
                    f"字段 {field} 必须是 JSON 对象，实际为 {type(value).__name__}",
                    code="invalid_type",
                )
        elif kind == _OBJ:
            if not isinstance(value, (str, dict)):
                _add_issue(
                    result,
                    f"$.{field}",
                    f'字段 {field} 必须是字符串或对象（如 {{"zh": ..., "en": ...}}），'
                    f"实际为 {type(value).__name__}",
                    code="invalid_type",
                )

    if not result.ok:
        return result

    # Build the persisted payload (strip import-only fields, apply defaults).
    payload: dict[str, Any] = dict(_DEFAULTS[entity_type])
    for field, value in data.items():
        if field == "entity_type" or field in rel_fields:
            continue
        if field in _LOCALE_FIELDS and not isinstance(value, str):
            payload[field] = _normalize_locale(value)
        else:
            payload[field] = value
    result.entity_key = str(payload.get("id" if entity_type == "scene" else "name", ""))
    result.payload = payload
    return result


def validate_import_content(filename: str, content: bytes) -> list[ImportFileResult]:
    """Validate an uploaded file that is either a single entity object or an
    array bundle of entities (batch export format).

    Each array element is validated independently and reported under the
    ``filename[i]`` pseudo-name so issues stay traceable.
    """
    result = ImportFileResult(filename=filename)
    try:
        data = json.loads(content.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        _add_issue(
            result,
            "$",
            f"JSON 语法错误：{e.msg}（第 {e.lineno} 行，第 {e.colno} 列）",
            code="json_syntax",
            line=e.lineno,
            column=e.colno,
        )
        return [result]

    if isinstance(data, list):
        if not data:
            result.issues.append(
                ImportIssue(
                    path="$", message="数组为空，没有可导入的实体", code="empty_bundle"
                )
            )
            result.ok = False
            return [result]
        return [
            _validate_import_data(f"{filename}[{i}]", item)
            for i, item in enumerate(data)
        ]
    return [_validate_import_data(filename, data)]


def get_template(entity_type: str) -> dict[str, Any] | None:
    if entity_type not in _TEMPLATES:
        return None
    return _TEMPLATES[entity_type]


# ── Export helpers ──


def build_export_payload(entity_type: str, entity: dict[str, Any]) -> dict[str, Any]:
    """Turn stored entity metadata into the shareable/importable JSON payload.

    Drops server-managed audit fields and relationship fields (which the
    import format rejects), keeps only importable fields, and expands
    locale values stored as JSON strings back into objects.
    """
    spec = _ENTITY_SPECS.get(entity_type)
    if spec is None:
        raise ValueError(f"Unknown entity type: {entity_type}")
    out: dict[str, Any] = {"entity_type": entity_type}
    for field in spec:
        if field in entity:
            out[field] = entity[field]
    for f in _LOCALE_FIELDS:
        if f in out and isinstance(out[f], str):
            try:
                parsed = json.loads(out[f])
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(parsed, (dict, str)):
                out[f] = parsed
    return out


def sanitize_filename(name: str) -> str:
    """Make an entity key safe for use in a download filename."""
    cleaned = "".join(c if c.isalnum() or c in ".-_" else "_" for c in name)
    return cleaned or "entity"
