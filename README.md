# astrbot_plugin_sb_qunyou · 群聊智能体

> 千群千面 — 为 AstrBot 提供群聊自适应智能体能力

## ✨ 功能特性

| 功能 | 说明 |
|---|---|
| **千群千面** | 每个群独立画像，后台 batch 学习自动更新，支持人工预设 |
| **发言认知** | LLM 抽取用户事实 → pgvector 近邻检索，回复时注入相关记忆 |
| **话题路由** | embedding 余弦相似度 + 滑动质心，多线程话题池解决鸡尾酒会问题 |
| **消息防抖** | L1 时间窗口拼接连续碎片消息，L2 语义完整性判断 (预留) |
| **情绪引擎** | LLM 情绪分析 + 概率灵敏度门控 + 自然衰减，V/A 连续模型预留 |
| **黑话统计** | jieba 分词 → 高频词 → 批量 LLM 推断含义，支持自定义 |
| **WebUI** | 暗色风格管理面板：群画像/黑话/情绪/线程/统计 |

## 🏗️ 技术栈

- **数据库**: PostgreSQL 14+ + pgvector 扩展
- **ORM**: SQLAlchemy 2.0 (async)
- **配置**: Pydantic v2
- **分词**: jieba
- **WebUI**: FastAPI + 原生 HTML/CSS/JS
- **向量**: pgvector (统一，不依赖外部向量数据库)

## 📦 安装

### 1. 依赖

```bash
pip install -r requirements.txt
```

### 2. PostgreSQL + pgvector

确保 PostgreSQL 已安装 pgvector 扩展：

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

### 3. AstrBot 配置

在 AstrBot 配置面板中设置以下项：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `Database_Settings.*` | none | PostgreSQL 连接配置：填写 `dsn`，或填写 `host/port/user/password/database_name` |
| `debounce_mode` | `time` | 防抖模式: off / time / time_bert |
| `topic_enabled` | `true` | 话题路由开关 |
| `emotion_enabled` | `true` | 情绪引擎开关 |
| `emotion_sensitivity` | `0.3` | 情绪变化灵敏度 (0-1) |
| `jargon_enabled` | `true` | 黑话统计开关 |
| `webui_enabled` | `true` | 管理面板开关 |
| `webui_port` | `18930` | 管理面板端口 |

## 📐 架构

```
消息流 → DebounceManager → save_raw_message → TopicThreadRouter
                                            → JargonService
                                            → GroupPersona
                                            → SpeakerMemory
                                            → EmotionEngine

LLM 请求 → HookHandler (5 源并发注入)
            ├── [HIGH]   群画像 → system_prompt
            ├── [HIGH]   情绪   → system_prompt
            ├── [MEDIUM] 线程上下文 → extra
            ├── [MEDIUM] 用户记忆   → extra
            └── [LOW]    黑话提示   → extra
```

### 数据库模型 (9 表)

| 表 | 用途 |
|---|---|
| `raw_messages` | 原始消息存储 + embedding |
| `group_profiles` | 群画像 (base + learned) |
| `active_threads` | 活跃话题线程 + centroid |
| `thread_messages` | 线程-消息关联 |
| `user_memories` | 用户记忆 + embedding |
| `emotion_states` | 群情绪状态 |
| `jargon_terms` | 黑话词条 |
| `bot_responses` | Bot 回复记录 |
| `learning_jobs` | 学习任务日志 |

## 🧪 测试

```bash
# 安装测试依赖
pip install pytest pytest-asyncio

# 运行测试
pytest tests/ -v
```

测试覆盖：防抖、话题路由数学、情绪映射/解析、黑话分词/解析、上下文注入格式化、配置默认值/映射、用户记忆解析。

## 🔧 管理命令

| 命令 | 权限 | 说明 |
|---|---|---|
| `/qunyou_status` | Admin | 查看当前群的插件状态 |
| `/set_mood <mood>` | Admin | 手动设置情绪 (happy/neutral/angry/sad/excited/bored) |

## 📄 许可证

GPL-3.0
