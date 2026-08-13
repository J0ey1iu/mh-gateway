# 权限指南

本文档描述 mh-gateway 的权限模型、权限标识格式、检查方式与扩展点。

---

## 权限格式

所有权限遵循三段式结构，用冒号分隔：

```
action:resource:target
```

| 段 | 说明 | 示例 |
|---|------|------|
| `action` | 操作类型 | `use` / `manage` |
| `resource` | 资源大类 | `scene` / `agent` / `tool` / `eval` |
| `target` | 资源标识或通配符 | `*` / `code-reviewer` / `triage` |

任意一段可用 `*` 匹配全部。匹配逻辑见 `mh_gateway/adapters.py` `match_permission()`。

`has_broad_permission(user_permissions, prefix)` 是快捷检查函数，判断用户是否拥有某类资源的通配权限（如 `use:tool:*`），用于列表接口提前跳过逐条过滤。

---

## 权限总览

### use 类 — 用户侧使用权限

| 权限串 | 控制点 | 检查方式 |
|--------|--------|---------|
| `use:scene:{id}` | `GET /scenarios` / `GET /scenarios/{id}` | `match_permission()` → 过滤列表 / 403 |
| `use:agent:{name}` | `GET /agents` / 场景 agent 过滤 | `match_permission()` → 过滤列表 |
| `use:tool:{name}` | `GET /tools` / Chat 工具过滤 / 运行时 `PermissionMiddleware` | `match_permission()` → 过滤列表 / 拦截 |
| `use:eval:*` | `POST/GET /eval/*` | `match_permission()` → 403 |

### manage 类 — 管理面 CRUD 权限

| 权限串 | 控制点 | 检查方式 |
|--------|--------|---------|
| `manage:scene:*` | `API /management/scenes/*` 所有端点 | `require_permission()` → 403 |
| `manage:agent:*` | `API /management/agents/*` 所有端点 | `require_permission()` → 403 |
| `manage:tool:*` | `API /management/tools/*` 所有端点 | `require_permission()` → 403 |
| `manage:metrics:*` | `API /management/metrics`（数据概览聚合指标） | `require_permission()` → 403 |

---

## 代码中的控制点

### 1. 依赖注入式检查 — `require_permission`

文件: `api/dependencies.py:31`

```python
def require_permission(permission: str):
    async def _check(request, user_id=Depends(get_current_user)):
        adapters = request.app.state.adapters
        ok = await adapters.permission_checker.check(user_id, permission)
        if not ok:
            raise HTTPException(status_code=403, ...)
        return user_id
    return _check
```

用法:
```python
@router.get("/scenarios")
async def list_scenarios(
    user_id: str = Depends(require_permission("manage:scene:*")),
):
    ...
```

应用于 `api/management.py` 全部 19 个端点。

### 2. 过滤式检查 — `match_permission`

文件: `mh_gateway/auth/protocols.py`

用于用户侧列表接口 — 先获取用户全部权限，再逐个过滤结果:
```python
# api/scenarios.py
user_perms = await adapters.permission_checker.get_permissions(user_id)
visible = [s for s in all_scenarios
           if match_permission(user_perms, f"use:scene:{s['id']}")]
```

应用于 `api/scenarios.py`、`api/agents.py`、`api/tools.py`、`api/chat.py`。

### 3. 运行时中间件 — `PermissionMiddleware`

文件: `services/perm_middleware.py:15`

在 agent 每次调用 tool 时拦截（首调用时惰性加载用户完整权限列表，后续使用本地 `match_permission` 避免重复远程调用）:
```python
class PermissionMiddleware(Middleware):
    async def should_allow_tool(self, tool_call, ...):
        tool_name = tool_call["function"]["name"]
        required = f"use:tool:{tool_name}"
        if self._user_perms is None:
            self._user_perms = await self._permission_checker.get_permissions(self._user_id)
        if match_permission(self._user_perms, required):
            return True
        allowed = await self._permission_checker.check(self._user_id, required)
        if not allowed:
            return "Permission denied: missing use:tool:{tool_name}"
        return True
```

---

## 检查链全貌

```
HTTP Request
  │
  ├─ GET /api/v1/scenarios
  │    ├─ get_current_user           → 401 if unauthenticated
  │    ├─ get_current_permissions    → 获取用户权限列表
  │    └─ match_permission()         → 过滤不可见场景
  │
  ├─ GET /api/v1/management/scenarios
  │    ├─ get_current_user           → 401 if unauthenticated
  │    └─ require_permission("manage:scene:*")
  │         └─ permission_checker.check() → 403 if denied
  │
  ├─ POST /api/v1/chat/{id}
  │    ├─ resolve_request_identity   → 用户 Token 或 M2M 鉴权，401 if both fail
  │    ├─ resolve_request_permissions → 获取身份对应的权限列表
  │    ├─ session ownership check     → 403 if not owner
  │    └─ match_permission("use:tool:{name}")
  │         └─ 过滤可用工具列表
  │
  └─ Agent Runtime (tool call)
       └─ PermissionMiddleware.should_allow_tool()
            └─ permission_checker.check("use:tool:{name}") → 拦截
```

## JSON 批量导入（scene / agent / tool）

管理 API 提供 JSON 导入与模板下载：

- `POST /api/v1/management/import` — 一次上传多个 JSON 文件（每个文件定义一个
  scene / agent / tool 实体）。权限按**文件实际携带的实体类型**逐项校验：
  导入含 scene 文件需要 `manage:scene:*`，含 agent 文件需要 `manage:agent:*`，
  含 tool 文件需要 `manage:tool:*`，缺任一权限即整批 403。
- `GET /api/v1/management/import/templates/{entity_type}` — 下载导入模板，
  权限对应 `manage:{entity_type}:*`（scene / agent / tool）。

导入为原子操作：任一文件存在语法/结构错误（或与现有实体重名、批内重复）时整批
失败（422），并返回每个文件的问题明细（JSON 路径、行列号），引导用户修改。
实体之间的关联关系（scene.agents、agent.tool_names）不支持导入。

导出与导入互为镜像，导出的文件可直接重新导入（单实体为 JSON 对象，批量导出为
JSON 数组）：

- `GET /api/v1/management/export/{entity_type}` — 批量导出（`?ids=a,b,c` 选指定
  实体，缺省导出全部），权限 `manage:{entity_type}:*`。
- `GET /api/v1/management/export/{entity_type}/{key}` — 单个实体导出，权限同上。

导出仅包含可导入的元数据字段，不含服务端审计字段与关联关系。分享的元数据在接收
方可能没有对应实现（如工具缺 endpoint/脚本、Agent 引用未配置的 provider）：运行时
对这类调用会记录异常并以可恢复的方式失败，不会拖垮整个系统。

---

## 内置 Demo 用户

定义在 `services/auth_client.py:16` `DEFAULT_PERMISSIONS`:

| 用户 | use:scene | use:agent | use:tool | use:eval | manage:scene | manage:agent | manage:tool |
|------|-----------|-----------|----------|----------|--------------|--------------|-------------|
| admin | `*` | `*` | `*` | `*` | `*` | `*` | `*` |
| member | `triage` | `triage` | `calculator` | — | `*` | — | — |
| user | `code_review`, `writing` | `code-reviewer`, `writer` | `web_search` | — | — | — | — |
| scene-manager | — | — | — | — | `*` | — | — |
| agent-manager | — | — | — | — | — | `*` | — |
| tool-manager | — | — | — | — | — | — | `*` |

---

## 生产环境扩展

用户认证和权限系统通过 `mh_gateway/auth/` 中的 Protocol 接口注入:

```python
# app.py — LifespanHook 中替换
adapters.token_verifier = MySSOProvider()        # UserAuthProvider
adapters.permission_checker = MyRBACProvider()   # PermissionChecker
```

客户只需实现这两个 Protocol，即可对接任意企业 SSO / RBAC / OPA / OpenFGA。
