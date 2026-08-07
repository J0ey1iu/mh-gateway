# Portal 版本升级适配指南

本文档面向**已经集成 `mh-gateway` 的客户开发团队**，说明升级到包含用户主页（Portal）的
新版本时，需要在自有代码中做的配套改动。

> 适用版本：本次变更未 bump 版本号，以 PR 引入 `GET /api/v1/portal/scenarios` 端点与
> `MetadataRepository.list_portal_scenarios` 协议方法为标志。

---

## 变更总览

| 项 | 类型 | 客户是否需要适配 |
|----|------|------------------|
| `MetadataRepository` 协议新增 `list_portal_scenarios()` | **破坏性** | **必做**，否则主页接口 500 |
| `SessionRepository.list_sessions()` 返回契约隐性收紧 | 隐性破坏 | 自测确认即可 |
| 新端点 `GET /api/v1/portal/scenarios` | 新增 | 前端需配套升级 |
| 场景元数据字段 `show_on_homepage` | 新增 | 可选（用主页展示开关时需要） |
| 新导出 `enrich_portal_agents()` | 新增 | 可选（推荐复用） |

---

## 一、必须适配：实现 `list_portal_scenarios`

`mh_gateway.adapters.MetadataRepository`（`@runtime_checkable` Protocol）新增了方法：

```python
async def list_portal_scenarios(
    self, user_perms: list[str] | None, locale: str = ""
) -> list[dict[str, Any]]:
    ...
```

网关的 `GET /api/v1/portal/scenarios` 会直接调用它。**未实现的自定义 MetadataRepository
会让该端点抛 `AttributeError`（500）**，其余现有路径不受影响。静态检查（pyright）也会
标记协议未实现。

### 实现要求

1. 返回**全量**应在主页展示的场景，**不要按权限过滤**——无权限场景也要返回，由
   `accessible` 字段标记（前端展示为锁定态）。
2. 过滤掉 `show_on_homepage is False` 的场景（缺省 / `true` 都展示，向后兼容存量数据）。
3. 每个场景携带 `accessible: bool`（是否允许当前用户进入）。
4. `agents` 富化为带元数据的列表（名称 / 展示名 / 描述，含 locale 字段），本地化由网关
   按 `Accept-Language` 负责。

### 参考实现

```python
from mh_gateway.adapters import (
    MetadataRepository,
    enrich_portal_agents,
    has_broad_permission,
    match_permission,
)


class MyRegistry(MetadataRepository):
    # ... 其余既有方法 ...

    async def list_portal_scenarios(
        self, user_perms: list[str] | None, locale: str = ""
    ) -> list[dict]:
        agents_by_name = {
            str(a.get("name")): a for a in self._agents if a.get("name")
        }
        result = []
        for s in self._scenarios:
            if s.get("show_on_homepage") is False:
                continue
            entry = dict(s)
            if user_perms is None:
                # 未配置授权 → 全部可访问
                entry["accessible"] = True
            else:
                entry["accessible"] = bool(
                    has_broad_permission(user_perms, "use:scene")
                    or match_permission(user_perms, f"use:scene:{s.get('id', '')}")
                )
            entry["agents"] = enrich_portal_agents(s.get("agents", []), agents_by_name)
            result.append(entry)
        return result
```

> 权限判定按企业模型实现即可（不强制用 `match_permission`）；关键契约是"全量返回 +
> `accessible` 标记"。完整可运行示例见参考实现 `mh-orch-app`（`InMemoryMetadataRepository`）
> 与 `mh-local`（`FileMetadataRepository`）。

---

## 二、自测：`list_sessions()` 返回契约

主页接口的热度统计直接读取 `SessionRepository.list_sessions()` 的返回值：

```python
sid = s["scenario_id"]
created = datetime.fromisoformat(s["created_at"].replace("Z", "+00:00"))
```

旧版本对"非 dict 自定义 Session 对象"有属性访问兜底；**新版本要求返回 dict**
（`mh_gateway.database._session.SessionSummary` TypedDict），否则该端点 500。

自测要点：

- `list_sessions()` 返回的每一项含 `scenario_id`、`created_at` 键
- `created_at` 为 `datetime.fromisoformat` 可解析的 ISO-8601 字符串
  （如 `2025-01-01T08:00:00.000Z`）；空串 / 无法解析的值不会导致报错，只按"旧会话"
  计权（不影响可用性）

---

## 三、可选适配

### 3.1 场景"在主页展示"开关（`show_on_homepage`）

管理面场景模型新增字段：

- `POST /api/v1/management/scenarios`：`show_on_homepage: bool = true`（缺省展示，
  旧请求不带该字段行为不变）
- `PUT /api/v1/management/scenarios/{id}`：`show_on_homepage: bool | null = null`
  （不传则不更新）

若你的 metadata 实现是 dict 透传（`{**s, **scenario}` merge），无需额外改动；若存在
字段白名单 / 表结构约束，需要支持持久化该布尔字段。存量场景缺省展示，无感知。

### 3.2 复用 `enrich_portal_agents`（推荐）

`mh_gateway.adapters.enrich_portal_agents(scenario_agents, agents_by_name)` 返回
`[{name, display_name, display_name_locale, description, description_locale}]`，
供 `list_portal_scenarios` 实现复用（见第一节示例）。纯新增导出，可选。

---

## 四、新端点：`GET /api/v1/portal/scenarios`

主页数据来源，仅新增，不影响既有端点。

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `page` | int ≥ 1 | `1` | 页码（1 起） |
| `page_size` | int 0-100 | `12` | 每页数量；`0` 表示全量 |

响应：

```json
{
  "items": [
    {
      "id": "code_review",
      "name": "代码审查",
      "icon": "💻",
      "description": "对 MR 进行自动化代码审查",
      "agents": [
        { "name": "code-reviewer", "display_name": "代码审查", "description": "审查代码变更" }
      ],
      "accessible": true,
      "heat": 42
    }
  ],
  "total": 10,
  "page": 1,
  "page_size": 12
}
```

说明：

- `name` / `description` / `agents[].display_name` / `agents[].description` 按请求头
  `Accept-Language`（`zh` / `en`）返回对应语言
- `accessible: false` 的场景仍返回（供展示锁定态），前端不可进入
- `heat` 为热度（近 30 天会话加权），仅用于排序与展示
- 排序：可访问在前，组内按热度降序
- 需要 `use:scene` 相关权限才能调用（`get_current_permissions` 依赖）

**前端配套**：新前端主页依赖此端点，旧网关无此端点会 404——**前端与网关必须同步升级**。
生产部署时注意 vite 配置新增的 `apiPortalScenarios` 占位符 `{wcm_api_portal_scenarios}`
需在构建替换环节补上。

---

## 五、升级后自测清单

1. `GET /api/v1/portal/scenarios` 返回 200，`total` 与场景数一致
2. 无权限场景仍出现在 `items` 中且 `accessible=false`
3. `show_on_homepage=false` 的场景不在 `items` 中
4. 切换 `Accept-Language`（`zh` / `en`），名称与 Agent 描述随之变化
5. 管理面创建 / 编辑场景可设置"在主页展示"，保存后主页可见性正确变化
6. 既有接口（`/api/v1/scenarios`、聊天、会话、管理面 CRUD）行为不变
