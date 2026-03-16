# TODO Gap List — `astrbot_plugin_sb_qunyou`

> 目标：对照当前项目的“规划/宣传口径”与“代码实现现状”，整理出一个可执行的差距清单。
>
> 本文主要依据：`README.md`、`metadata.yaml`、`config.py`、`_conf_schema.json`，以及当前实现代码。

---

## 一、结论摘要

当前插件已经具备一个 **可运行的核心雏形**：

- 有消息监听主链路
- 有防抖、话题路由、用户记忆、情绪、黑话、WebUI、LightRAG / Rerank 预留接入
- 有数据库模型和基础 CRUD
- 有 prompt 注入框架

但它和 README / 产品规划之间，仍存在几类明显 gap：

1. **“配置里写了，但运行时没接上”**
   - 典型如 `learning_cron` / `update_cron` / `ingestion_cooldown`
2. **“数据结构建了，但业务链路没闭环”**
   - 典型如 `bot_responses`、`source_whitelist`
3. **“功能有骨架，但只完成了 MVP 版”**
   - 典型如 WebUI、用户记忆、情绪引擎、话题线程
4. **“宣传口径比实现更完整”**
   - 典型如黑话自动学习闭环、知识引擎集成、千群千面管理能力

建议优先级：

- **P0：补齐能否稳定工作/是否真正闭环的缺口**
- **P1：补齐 README 已承诺但尚未真正落地的能力**
- **P2：补体验、可运维性、安全性、性能**

---

## 二、功能对照总表

| 模块 | 规划/README 口径 | 当前实现 | 判断 |
|---|---|---|---|
| 消息监听主链路 | 接收消息 → 存储 → 路由 → 学习/注入 | 已实现主链路 | 基本落地 |
| 消息防抖 | L1 时间窗口 + L2 语义完整性 | 已实现；L2 实际走 LLM 判断，不是 BERT | 部分落地 |
| 话题路由 | 线程池 + centroid + 话题摘要 | 已实现核心路由与摘要更新 | 基本落地 |
| 群画像 | 每群独立画像 + batch 学习 + 人工预设 | 已实现 batch 学习和 base_prompt；但 cron/白名单未落地 | 部分落地 |
| 用户记忆 | 事实抽取 + 向量检索注入 | 已实现 MVP | 部分落地 |
| 情绪引擎 | LLM 情绪分析 + 灵敏度门控 + 自然衰减 | 已实现离散情绪版 | 部分落地 |
| 黑话统计 | 高频词统计 + 批量推断 + 自定义管理 | 已实现计数/推断/CRUD；但自动 flush / 定时推断没接上 | 部分落地 |
| 知识引擎 | LightRAG 知识图谱 | 已有 manager 和注入口，但接入不完整 | 部分落地 |
| Rerank | 重排 extra context | 已有工厂/适配器 | 基本落地 |
| WebUI | 群画像/黑话/情绪/线程/统计管理 | 已有基础版 | 部分落地 |
| 运维/调试命令 | 状态查看、手动调情绪 | 已实现 `/qunyou_status`、`/set_mood` | 基本落地 |
| 学习任务系统 | persona/jargon 等后台 job 记录 | 仅 persona job 接上 | 部分落地 |
| 机器人回复记录 | bot 回复记录与后续分析 | 表和 repo 有，业务未接上 | 未落地 |

---

## 三、按优先级整理的 TODO

## P0 — 先补“闭环缺失 / 运行关键路径”

### 1. 接入真正的定时任务/调度系统
**问题**
- `config.py` 里有：
  - `GroupPersonaConfig.learning_cron`
  - `JargonConfig.update_cron`
  - `KnowledgeConfig.ingestion_cooldown`
- 但代码里没有看到任何 scheduler / cron 注册逻辑。

**影响**
- README 里提到的“后台 batch 学习”“定时更新”目前不成立或不完整。
- 很多功能只能靠消息触发，或者根本不会自动跑。

**建议**
- 在 `Lifecycle.on_load()` 中接入 AstrBot 的任务调度能力，或插件自有 asyncio 定时调度。
- 至少补三条定时任务：
  - 群画像 batch 学习
  - 黑话 flush + meaning inference
  - 知识缓冲区定时 flush

**相关代码**
- `config.py`
- `lifecycle.py`
- `services/group_persona.py`
- `services/jargon.py`
- `main.py`

---

### 2. 补齐黑话链路：计数 → flush → 推断 → 注入
**问题**
- `JargonService.count_words()` 只是在内存里计数。
- `flush_to_db()` / `infer_meanings_batch()` 已写好，但没有看到稳定调用点。
- 当前只在 `terminate()` 里 `flush_all()`；也就是说插件不停机时，黑话可能一直留在内存里。

**影响**
- README 的“黑话统计 / 自动推断含义”在长期运行场景下很可能不闭环。
- WebUI 可能长期看不到新黑话或释义更新。

**建议**
- 增加周期性 `flush_to_db(group_id)`
- flush 后自动触发 `infer_meanings_batch(group_id)`
- 增加手动命令或 WebUI 按钮：
  - 立即 flush
  - 立即推断
  - 重新推断某群黑话

**相关代码**
- `services/jargon.py`
- `main.py`
- `webui/api.py`

---

### 3. 完成 Knowledge ingestion cooldown 逻辑
**问题**
- `KnowledgeConfig.ingestion_cooldown` 只在配置里定义，没有实际使用。
- 当前逻辑是 buffer 达到 `ingestion_buffer_max` 时立即 flush；否则要等插件 terminate 才统一 flush。

**影响**
- 长时间运行时，小群/低频群的数据可能迟迟不入库。
- 和 README 中“知识引擎”的预期不一致。

**建议**
- 为每个 group 增加 cooldown timer：
  - 到达缓冲上限 → 立即 flush
  - 未到上限但超过 cooldown → 定时 flush
- 避免 terminate 才刷盘。

**相关代码**
- `config.py`
- `main.py`
- `services/knowledge/lightrag_manager.py`

---

### 4. 补齐依赖与运行文档，保证 README 安装可达
**问题**
- `requirements.txt` 没有包含 `cachetools`
- `LightRAG` 是可选依赖，但 README 未明确写额外安装项
- 当前仓库本身也不是独立可运行程序，而是 AstrBot 插件；README 对“接入 AstrBot”的步骤偏简略
- README 里 WebUI 默认端口写的是 `18930`，代码默认是 `7834`

**影响**
- 按 README 直接安装，容易“装得起来但功能不全”或“和文档不一致”。

**建议**
- README 增补：
  - AstrBot 插件安装方式
  - 必选 / 可选依赖分层
  - PostgreSQL + pgvector 初始化
  - provider 配置示例
  - WebUI 默认端口更正文档
- 明确可选依赖：`lightrag-hku`、测试依赖、可能的 rerank 依赖

**相关代码**
- `README.md`
- `requirements.txt`
- `metadata.yaml`
- `config.py`

---

## P1 — 补齐 README 已承诺但实现不完整的能力

### 5. 把 `source_whitelist` 从“字段”做成“能力”
**问题**
- `GroupProfile` 有 `source_whitelist`
- WebUI/API 也能存这个字段
- 但业务代码里没有任何地方真正使用它

**影响**
- 目前它只是个数据库字段，不是功能。
- README / 设计描述中的“可信信源”未落地。

**建议**
- 明确这个字段的产品语义：
  - 是群画像学习时的外部资料源？
  - 是知识库同步白名单？
  - 还是联网搜索限制？
- 按语义补齐：
  - 拉取逻辑
  - 清洗/摘要逻辑
  - 与群画像/知识引擎的融合策略

**相关代码**
- `services/group_persona.py`
- `db/models.py`
- `webui/api.py`

---

### 6. 让群画像学习真正可管理
**问题**
- 当前 `GroupPersonaService` 只会取最近消息做总结，写入 `learned_prompt`
- 有 history 字段，但没有：
  - 查看历史版本
  - 手动回滚
  - 手动触发学习
  - 查看学习任务结果

**影响**
- “千群千面”具备基础自动学习，但缺少可运维性。

**建议**
- WebUI/API 增加：
  - 查看 `learned_prompt_history`
  - 回滚到历史版本
  - 立即触发一次 batch learning
  - 查看 `learning_jobs`

**相关代码**
- `services/group_persona.py`
- `db/repo.py`
- `webui/api.py`

---

### 7. 话题线程管理补全：不仅能“分线程”，还要能“管线程”
**问题**
- `TopicThreadRouter` 已能按相似度归线程
- 但仍有几个缺口：
  - `max_threads_per_group` 只体现在查询 limit，不是严格线程上限治理
  - WebUI 只有线程列表，没有线程详情/消息流
  - API 参数 `include_archived` 目前传了也没用，仍只查 active threads

**影响**
- 线程路由做出来了，但很难观测和调试。
- “鸡尾酒会问题”解决方案还停留在算法 MVP。

**建议**
- 严格线程治理策略：
  - 达上限后归档最旧线程，或合并低活跃线程
- API 增加：
  - 获取线程详情
  - 获取线程消息
  - 查询归档线程
- WebUI 增加线程详情页

**相关代码**
- `pipeline/topic_router.py`
- `db/repo.py`
- `webui/api.py`

---

### 8. 用户记忆系统从 MVP 升级到可用版
**问题**
- 当前记忆能力是：
  - 每条消息抽取事实
  - 存 embedding
  - 查询时做 top-k 相似检索
- 但缺少：
  - 去重 / merge
  - 置信度 / 质量评分
  - 过期 / 遗忘机制
  - importance 动态更新
  - 冲突事实处理

**影响**
- 跑久了容易产生重复、过时、低质量记忆。
- prompt 注入噪声会越来越多。

**建议**
- 新增记忆去重策略（语义相近合并）
- 对事实增加：
  - confidence
  - last_seen_at
  - source_count
- 查询时增加二次筛选或 rerank
- 为记忆增加 TTL/衰减规则

**相关代码**
- `services/speaker_memory.py`
- `db/models.py`
- `db/repo.py`

---

### 9. 情绪引擎从“标签切换器”升级到更稳定的状态机
**问题**
- 目前情绪更新逻辑是：
  - 随机门控（`sensitivity`）
  - LLM 输出一个离散标签
  - 更新 mood + 固定映射的 valence/arousal
- 但 README/注释里提到的 V/A 连续模型、门控细化、自然衰减更像长期规划。
- 另外当前 `maybe_update()` 只在 `event.is_at_or_wake_command` 时触发，而不是所有群聊消息。

**影响**
- 当前更像“被叫到时才改一次情绪”，不完全像“群整体情绪状态”。

**建议**
- 明确产品语义：
  - 是“群对 Bot 的情绪”
  - 还是“群当前整体氛围”
- 若是后者，应扩大触发条件
- 若保留离散版，建议加：
  - 平滑切换/滞后机制
  - 连续多次观测后再换 mood

**相关代码**
- `services/emotion.py`
- `main.py`
- `README.md`

---

### 10. Knowledge / LightRAG 集成仍需做实
**问题**
- 代码里已有 `LightRAGKnowledgeManager`
- 但当前实例化只是 `_LightRAG(working_dir=...)`
- `llm_adapter` 虽传入 manager，但没有真正用于 LightRAG 的 embedding/LLM 配置桥接
- `warmup_instances()` 写了但没调用

**影响**
- 现在更像“留了一个可选接入点”，还不是完整的一体化集成。
- 它和 AstrBot provider 体系之间仍是松耦合半成品。

**建议**
- 明确 LightRAG 与 AstrBot provider 的适配方式：
  - embedding 走哪个 provider
  - LLM 走哪个 provider
- 给出 fallback 策略
- 增加 warmup / health check / 日志指标

**相关代码**
- `services/knowledge/lightrag_manager.py`
- `lifecycle.py`
- `services/llm_adapter.py`

---

### 11. `bot_responses` 表要真正接入业务
**问题**
- 有 `BotResponse` 模型
- 有 `save_bot_response()` repo 方法
- 但当前没有看到任何地方实际记录 bot 回复

**影响**
- 无法做：
  - 回复质量追踪
  - thread 维度回复分析
  - 学习闭环评估

**建议**
- 接入 AstrBot 的 bot response hook / after-send hook
- 至少记录：
  - group_id
  - thread_id
  - response_text
  - timestamp
  - 可选：model/provider、耗时、token 等

**相关代码**
- `db/models.py`
- `db/repo.py`
- `main.py`

---

### 12. LearningJob 不要只记录 persona
**问题**
- 现在 `learning_jobs` 基本只服务于 `persona_learn`
- 黑话推断、知识入库、后续可能的记忆清洗都没纳入 job 体系

**影响**
- 运维可观测性不统一，排障麻烦。

**建议**
- 将以下任务统一纳入 `learning_jobs`：
  - `persona_learn`
  - `jargon_infer`
  - `knowledge_ingest`
  - `memory_compact`（若后续实现）

**相关代码**
- `services/group_persona.py`
- `services/jargon.py`
- `db/models.py`
- `db/repo.py`

---

## P2 — 体验、运维、安全、性能完善

### 13. WebUI 补齐“管理面板”应有的最小运维能力
**问题**
当前 WebUI 有基础页面，但仍缺：
- 鉴权
- 分页 / 搜索
- 线程详情
- 学习任务查看
- 群画像历史版本查看 / 回滚
- source whitelist 编辑能力
- 手动触发 batch 操作

**影响**
- 现在更像 demo 面板，不太像生产可运维后台。

**建议**
- 先做最小增量：
  - 只读鉴权（至少本地 token / basic auth）
  - 线程详情页
  - 学习任务页
  - 群画像历史页
  - “立即学习 / 立即推断黑话”按钮

**相关代码**
- `webui/api.py`
- `webui/frontend/index.html`

---

### 14. 把 CacheConfig 真正接到 CacheManager
**问题**
- `CacheConfig` 定义了 `context_ttl` / `embedding_ttl`
- 但 `CacheManager` 里的 TTL 是硬编码，且 embedding/emotion 缓存几乎没用起来

**影响**
- 配置和行为不一致。

**建议**
- 启动时用 `PluginConfig.cache` 初始化 `CacheManager`
- 把 embedding 请求也纳入缓存（尤其是 topic / memory 查询）
- 视情况增加 invalidation 策略

**相关代码**
- `config.py`
- `utils/cache.py`
- `services/hook_handler.py`
- `services/llm_adapter.py`

---

### 15. 增加数据库迁移与向量索引策略
**问题**
- 当前只靠 `Base.metadata.create_all()` 建表
- 没有 migration 方案
- 向量列有定义，但没看到 HNSW / IVF 等向量索引创建

**影响**
- 版本演进困难
- 数据量一大，检索性能会掉

**建议**
- 引入 Alembic
- 为 `raw_messages.embedding`、`user_memories.embedding`、线程 centroid 明确索引策略
- 明确 embedding 维度变化时的升级路径

**相关代码**
- `db/engine.py`
- `db/models.py`

---

### 16. 增加 AstrBot 集成测试 / 端到端测试
**问题**
- 目前测试更偏单元测试，覆盖的是 config、builder、数学逻辑、解析逻辑
- 缺少真实 AstrBot 生命周期下的集成验证

**影响**
- 很多运行问题要到接进 AstrBot 才暴露。

**建议**
- 增加集成测试场景：
  - 插件初始化
  - on_message → DB → topic route → hook injection
  - WebUI API 基础烟测
  - 配置错误 / provider 缺失 / DB 不可用时的降级行为

**相关代码**
- `tests/`
- `main.py`
- `lifecycle.py`

---

## 四、建议的落地顺序（可直接开工）

### Sprint 1：先把闭环跑起来
- [ ] 接入 scheduler
- [ ] 黑话定时 flush + 推断
- [ ] knowledge buffer cooldown flush
- [ ] 修 README / requirements / 端口文档
- [ ] 增加最基本的启动健康检查日志

### Sprint 2：补齐管理与观测
- [ ] WebUI：线程详情、学习任务、手动触发按钮
- [ ] 群画像 history 查看 / 回滚
- [ ] `include_archived` 真正生效
- [ ] bot response 持久化
- [ ] job 体系扩展到 jargon / knowledge

### Sprint 3：提升质量
- [ ] 用户记忆去重 / 衰减 / 置信度
- [ ] 情绪状态机平滑化
- [ ] CacheConfig 接线
- [ ] DB migration + vector index
- [ ] 集成测试补齐

---

## 五、最值得优先修的 5 个点

如果只做最关键的 5 个，我建议先修这几个：

1. **scheduler 接入**
2. **黑话 flush / inference 闭环**
3. **knowledge cooldown flush**
4. **README / 依赖 / 安装文档修正**
5. **bot response / learning jobs / WebUI 观测补齐**

---

## 六、附：当前实现里“已经不错”的部分

也不是只有坑，当前代码里有几块基础打得还可以：

- `HookHandler` 的注入分层比较清晰：system / extra 分离
- `Lifecycle` 把 bootstrap / on_load / shutdown 分阶段处理，结构干净
- `Repository` 统一 CRUD，维护成本比散乱 repo 低
- `TopicThreadRouter` 的 centroid 路由和自动摘要是个靠谱 MVP
- WebUI 前后端已经有可用骨架，不是纯空壳

说明这个项目不是“从零到一没写完”，而是 **已经到 0.5~0.7 版本，下一步主要是补闭环、补可运维性、补一致性**。
