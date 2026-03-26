# astrbot_plugin_sb_qunyou · 群聊智能体

> 千群千面。为 AstrBot 提供每群独立人格、群画像学习与上下文注入能力。

## ✨ 功能特性

| 功能 | 说明 |
|---|---|
| **每群人格** | 每个群维护一份独立的本地人格，支持绑定 AstrBot 人格、导入为基础人格、按消息阈值学习新版本，并手动切换历史版本 |
| **群画像学习** | 保留独立的群画像链路，作为补充上下文注入，不与每群人格直接混合 |
| **发言认知** | LLM 抽取用户事实，写入 pgvector，回复时按相似度注入相关记忆 |
| **话题路由** | embedding 余弦相似度 + 滑动质心，多线程话题池解决鸡尾酒会问题 |
| **消息防抖** | L1 时间窗口拼接连续碎片消息，L2 语义完整性判断接口预留 |
| **情绪引擎** | LLM 情绪分析 + 概率灵敏度门控 + 自然衰减 |
| **黑话统计** | jieba 分词 → 高频词 → 批量 LLM 推断含义，支持自定义 |
| **WebUI** | 管理群画像、每群人格、slot 诊断、黑话、情绪、线程、学习任务和 Prompt 模板 |

## 🏗️ 技术栈

- **数据库**: PostgreSQL 14+ + pgvector 扩展
- **ORM**: SQLAlchemy 2.0 (async)
- **配置**: Pydantic v2
- **分词**: jieba
- **WebUI**: FastAPI + 原生 HTML/CSS/JS
- **向量**: pgvector

## 📦 安装

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 准备 PostgreSQL + pgvector

确保 PostgreSQL 已安装 pgvector 扩展：

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

### 3. 配置 AstrBot

在 AstrBot 配置面板中设置以下项：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `Database_Settings.*` | none | PostgreSQL 连接配置：填写 `dsn`，或填写 `host/port/user/password/database_name` |
| `Model_Configuration.embedding_provider_id` | none | Embedding provider，使用 AstrBot 内建 provider 选择器 |
| `Model_Configuration.main_llm_provider_id` | none | 主 LLM provider，使用 AstrBot 内建 provider 选择器 |
| `Model_Configuration.fast_llm_provider_id` | none | 快速 LLM provider，使用 AstrBot 内建 provider 选择器 |
| `Topic_Settings.fast_model_provider_id` | none | 可选。线程摘要专用 provider；留空则使用全局快速模型 |
| `WebUI_Settings.enabled` | `true` | 管理面板开关 |
| `WebUI_Settings.host` | `127.0.0.1` | 管理面板监听地址 |
| `WebUI_Settings.port` | `7834` | 管理面板端口 |

这些 provider 选择项现在都走 AstrBot 的 `_special: select_provider` 可视化选择器。
未配置对应 provider 时，相关功能会跳过并记录日志，不再静默回退到任意可用模型。

## 🤖 模型使用路径

插件内的模型职责如下：

| 配置项 | 使用位置 | 用途 |
|---|---|---|
| `Model_Configuration.embedding_provider_id` | `TopicThreadRouter.route_message`、`Repository.update_message_embedding`、`SpeakerMemoryService.extract_and_store`、`SpeakerMemoryService.retrieve_relevant`、`HookHandler` | 生成消息向量、线程路由向量、用户记忆向量和检索向量 |
| `Model_Configuration.main_llm_provider_id` | `GroupPersonaService.run_batch_learning`、`PersonaBindingService.run_combined_learning` | 群画像学习、每群人格学习 |
| `Model_Configuration.fast_llm_provider_id` | `SpeakerMemoryService.extract_and_store`、`EmotionEngine.maybe_update`、`JargonService.infer_meanings_batch`、`DebounceManager._check_semantic_completeness`、`TopicThreadRouter._update_thread_summary` | 用户记忆抽取、情绪分析、黑话含义推断、L2 防抖语义完整性判断、线程摘要 |
| `Topic_Settings.fast_model_provider_id` | `TopicThreadRouter._update_thread_summary` | 可选覆盖线程摘要专用 provider；仅影响线程摘要，不影响其他快速任务 |

其中：

- 记忆抽取明确走快速模型，不走主模型。
- 若配置了 `Topic_Settings.fast_model_provider_id`，线程摘要优先使用它；否则回落到 `Model_Configuration.fast_llm_provider_id`。
- 主模型和快速模型现在都只会走各自配置好的 provider，不会再互相串用。

## 📐 核心架构

### 消息处理链路

```text
on_message
    -> DebounceManager.handle_event
    -> save_raw_message
    -> TopicThreadRouter.route_message
    -> JargonService.count_words
    -> GroupPersonaService.increment_and_check
    -> PersonaBindingService.increment_and_check
    -> SpeakerMemory.extract_and_store
    -> EmotionEngine.maybe_update
    -> Knowledge ingestion buffer
```

### LLM 请求注入链路

`HookHandler` 会并发获取多路上下文，并分成两种注入方式：

1. **每群人格**：精确替换 `system_prompt` 中的 `<qunyou_persona_slot>`
2. **补充上下文**：追加到 `extra_user_content_parts`

当前注入源如下：

| 来源 | 位置 | 说明 |
|---|---|---|
| 每群人格 | `system_prompt` | 使用单一 slot 精确替换 |
| 群画像 | `extra_user_content_parts` | 作为高信任补充上下文 |
| 情绪状态 | `extra_user_content_parts` | 非 neutral 时注入 |
| 线程上下文 | `extra_user_content_parts` | 当前消息的主题线程摘要 |
| 用户记忆 | `extra_user_content_parts` | pgvector 召回的相关事实 |
| 知识图谱 | `extra_user_content_parts` | LightRAG 查询结果 |
| 黑话提示 | `extra_user_content_parts` | 群内黑话解释 |

### 每群人格的生效优先级

单个群的最终生效人格按以下顺序解析：

1. 当前激活的人格版本
2. 插件侧维护的本地基础人格
3. 当前绑定的 AstrBot 人格 prompt
4. 三者都没有时，不替换 `system_prompt`

这意味着：

- 绑定 AstrBot 人格只是“来源”之一，不等于最终生效人格
- 若绑定的 AstrBot prompt 自己包含 `<qunyou_persona_slot>`，插件会优先抽取其槽内文本作为群人格来源
- WebUI 中的“导入为基础人格”会把 AstrBot prompt 抽取为插件侧基础人格，之后学习版本都在这条链路上演进
- 群画像依然独立存在，职责是补充上下文，不直接替代每群人格

## 🎯 单 Slot System Prompt 替换

当前运行时只识别一个插槽：`<qunyou_persona_slot>`。

请在 AstrBot 的人格 prompt 中预留：

```xml
<qunyou_persona_slot>
默认群人格
</qunyou_persona_slot>
```

插件行为：

1. 检测 `system_prompt` 中是否存在 `<qunyou_persona_slot>`
2. 若存在，则只替换该标签包裹的内容，其他平台规则和安全指令保持不变
3. 若不存在，则完全不改动 `system_prompt`，并继续把其他上下文注入到 `extra_user_content_parts`

替换示例：

```text
你是一个友善的群聊助手。

<qunyou_persona_slot>
默认群人格
</qunyou_persona_slot>

请遵守平台规则...
```

注入后：

```text
你是一个友善的群聊助手。

你是本群的专属游戏助手，擅长攻略、联机和游戏梗，回复时保持熟络和群内语感。

请遵守平台规则...
```

## 🗃️ 数据库模型（13 表）

| 表 | 用途 |
|---|---|
| `raw_messages` | 原始消息存储 + embedding |
| `group_profiles` | 群画像（基础画像 + 学习画像） |
| `active_threads` | 活跃话题线程 + centroid |
| `thread_messages` | 线程-消息关联 |
| `user_memories` | 用户记忆 + embedding |
| `emotion_states` | 群情绪状态 |
| `jargon_terms` | 黑话词条 |
| `bot_responses` | Bot 回复记录 |
| `learning_jobs` | 学习任务日志 |
| `learned_prompt_reviews` | 学习结果审核记录 |
| `group_persona_bindings` | 每群人格的来源、基础人格和启用状态 |
| `persona_tone_versions` | 每群人格版本历史。表名沿用旧命名，但现在存的是完整人格版本文本 |
| `system_prompts` | Prompt 模板管理 |

## 🖥️ WebUI 能力

WebUI 目前支持：

- 查看和编辑群画像
- 为指定群绑定 AstrBot persona_id
- 导入 AstrBot prompt 为本地基础人格
- 编辑本地基础人格，并查看当前最终生效人格
- 查看 slot 诊断结果，确认最近一次进入 Hook 的原始 `system_prompt` 是否包含 `<qunyou_persona_slot>`
- 触发每群人格学习并切换历史版本
- 查看学习任务、黑话、情绪、线程和 Prompt 模板

## 🧪 测试

```bash
pip install pytest pytest-asyncio
pytest tests/ -v
```

项目是纯 Python AstrBot 插件，无单独 build 步骤。

## 🔧 管理命令

| 命令 | 权限 | 说明 |
|---|---|---|
| `/qunyou_status` | Admin | 查看当前群的插件状态 |
| `/set_mood <mood>` | Admin | 手动设置情绪 |
| `/sbqunyou-getwebtoken` | Admin | 生成 WebUI 临时登录令牌 |

## 📄 许可证

GPL-3.0
