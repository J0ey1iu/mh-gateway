# 系统提示词装配（System Prompt Assembly）

> 适配范围：升级到带装配能力的 minimal-harness / mh-gateway 版本后，**不启用该功能则零改动**。
> 本文档描述可选增强：让最终发给 LLM 的 system prompt 由三部分装配而成。

## 背景

系统提示词过去只有一份——agent metadata 里的原始 prompt（可 locale 化）。
但实际使用中，发给 LLM 的 system prompt 至少包含三类内容：

1. **Agent 原始 prompt**（用户/管理员在注册中心配置的，如 triage 的角色说明）；
2. **系统需要注入的提示词**（平台规则，用户不会自己写，例如“当用户批评你的
   回答时必须先调用 submit_feedback 记录反馈”）；
3. **用户长期记忆**（用户偏好，例如“用户偏好中文回答、喜欢简洁”）。

这三部分不应混写在注册中心的 agent metadata 里（用户可编辑的配置被平台规则
污染，且无法按应用定制）。因此 minimal-harness 定义了装配协议，由应用层
实现，gateway 透传，runtime 在**每次 agent run 时实时装配一次**。

## 协议（minimal-harness 定义，客户实现）

三个协议均定义在 `minimal_harness.agent.runtime`，均为 `runtime_checkable`
Protocol（鸭子类型，无需显式继承）：

```python
class SystemPromptProvider(Protocol):
    """实时提供系统需要注入的提示词（每次 agent run 调用一次）。"""
    async def get_system_prompt(self) -> str: ...

class UserPreferenceProvider(Protocol):
    """实时提供用户长期记忆提示词（每次 agent run 调用一次）。"""
    async def get_preference_prompt(self, user_id: str) -> str: ...

class SystemPromptAssembler(Protocol):
    """把三部分 prompt 装配为最终 system prompt（每次 agent run 调用一次）。"""
    async def assemble(
        self,
        base_prompt: str,             # 1. agent metadata 原始 prompt（locale 解析后）
        system_prompt: str,           # 2. 系统注入的提示词
        user_preference_prompt: str,  # 3. 用户长期记忆（应用层适配好的字符串）
    ) -> str: ...
```

- `SystemPromptAssembler` 是**装配动作**：决定顺序、分隔符、空段跳过等。
- `SystemPromptProvider` / `UserPreferenceProvider` 是**内容来源**：
  框架不关心内容从哪来（配置文件、数据库、LLM 分析……），只保证每次 run 时
  实时调用一次拿到最新值。内容来源是应用层自己的事。

## 客户需要做哪些适配

### 档位一：不启用（默认，零改动）

升级后什么都不传，行为与旧版本完全一致——system prompt 就是 agent metadata
的原始 prompt。`GatewayAdapters` 新增的三个字段全部可选。

### 档位二：注入平台规则（如反馈规则）

1. 实现 `SystemPromptProvider`（返回规则文本）；
2. 实现 `SystemPromptAssembler`（拼接三段、跳过空段）；
3. 在 adapter lifespan 里注入到 `GatewayAdapters`。

> 注意：只配 provider 不配 assembler 时，注入内容会被静默忽略（装配器是
> 唯一消费方）。两个一起配。

### 档位三：注入用户长期记忆（可选叠加）

实现 `UserPreferenceProvider`——按 `user_id` 返回该用户的偏好提示词字符串。
来源（记忆库、文件、上游服务）由你自行适配，框架只把它当一个字符串。

## 接入示例

```python
# adapters/prompt_assembly.py
from minimal_harness.agent.runtime import (
    SystemPromptAssembler,
    SystemPromptProvider,
    UserPreferenceProvider,
)

class RuleSystemPromptProvider(SystemPromptProvider):
    """平台规则：每次 run 实时读取（示例为静态常量，动态来源同理）。"""
    async def get_system_prompt(self) -> str:
        return (
            "[System feedback rule] When the user criticizes your answer, "
            "you MUST first call the submit_feedback tool (type=blame) to "
            "record the feedback, then continue answering."
        )

class UserPreferenceProviderImpl(UserPreferenceProvider):
    """用户长期记忆：按 user_id 查询（示例为假数据，替换为真实来源）。"""
    async def get_preference_prompt(self, user_id: str) -> str:
        prefs = await memory_service.fetch_preferences(user_id)  # 你的实现
        return f"User preferences: {prefs}"

class JoiningAssembler(SystemPromptAssembler):
    """拼接三段，跳过空段。"""
    async def assemble(self, base_prompt, system_prompt, user_preference_prompt):
        return "\n\n".join(
            p for p in (base_prompt, system_prompt, user_preference_prompt) if p
        )
```

```python
# main.py —— adapter lifespan 中注入
yield GatewayAdapters(
    # ...原有字段不变...
    system_prompt_assembler=JoiningAssembler(),
    system_prompt_provider=RuleSystemPromptProvider(),
    user_preference_provider=UserPreferenceProviderImpl(),  # 可选
)
```

## 参考实现

mh-local 提供了最小实现，可直接参照：

- `mh_local/prompts.py`：`LocalSystemPromptAssembler`（拼接跳空段）、
  `LocalSystemPromptProvider`（返回 `SYSTEM_FEEDBACK_RULE` 双语反馈规则）；
- `mh_local/app.py`：`build_adapter_lifespan()` 中注入；
- 反馈规则已从 triage prompt（注册中心）移除，改由装配器注入，全站 agent 生效。

## 语义保证

- **每次 agent run 装配一次**：`AgentRuntime.run()` 开始时装配，Agent 循环内
  的多次 LLM 调用复用同一结果；压缩（compaction）恢复时也用同一字符串。
- **实时获取**：`base_prompt`（每次 run 从注册中心拉取 metadata）、系统注入、
  用户偏好均为 run 时刻实时取值，非构造时快照——运行中修改配置、记忆，下一
  次 run 即生效。
- **摘要不装配**：compaction 生成摘要时用原始 prompt（摘要指令不是对话上下文，
  装配规则对它无意义）。
- **按 agent 无差别注入**：系统注入内容是 runtime 级、全站统一的，装配器签名
  没有 agent 维度；如需按 agent 差异化，可在 `SystemPromptProvider` 内自行读取
  run context 外的信息（如当前 agent 名）。
