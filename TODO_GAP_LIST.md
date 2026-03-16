# TODO Gap List — `astrbot_plugin_sb_qunyou`（刷新版）

> 基线说明：本版 **不是** 基于你本地最初 clone 的快照，而是基于已 fetch 的上游最新代码：
>
> - `upstream/master @ 38479b1` (`update`)
> - 刷新时间：2026-03-16
>
> 这意味着：
>
> - **文档里的“已完成/已补齐”**，是指上游最新 master 已经做了
> - **如果你当前分支还没 merge upstream**，这些修复未必已经在你本地生效

---

## 一、刷新后的结论

和我第一次看的早期版本相比，上游最近几次更新已经补掉了不少明显缺口，尤其是：

- WebUI 安全性（host / auth / CORS）
- `include_archived` 真正生效
- Jargon 的自动 flush 阈值
- LightRAG warmup
- review gate（审核门禁）
- 群独立人格绑定 / 语气学习 / 版本切换
- 一批并发、错误处理、缓存 key、依赖声明问题

所以现在这个项目不太像“功能 PPT 很大、代码骨架很空”，而更像：

- **核心主链路已经成型**
- **中高级能力开始补齐**
- **但仍有几块关键规划项没有真正闭环**

当前最主要的 gap，已经从“基础功能缺失”，转成了：

1. **调度器 / cron 配置仍未真正接线**
2. **黑话推断链路只做了一半（flush 有了，infer 还没接上）**
3. **Knowledge ingestion cooldown 仍未实现**
4. **一些字段/表已经建好，但仍是“存着不用”**（如 `source_whitelist` / `bot_responses`）
5. **部分配置项仍然只是配置，不影响真实运行行为**（如 `CacheConfig`）

---

## 二、相对旧版 TODO，哪些已经可以划掉

下面这些项，**上游最新 master 已经明显改善或基本补齐**：

### 已补齐 / 可从旧 TODO 中降级处理

- [x] **WebUI 默认绑定更安全**
  - 现在 `config.webui.host` 默认是 `127.0.0.1`
- [x] **WebUI auth**
  - 已支持 `auth_token`，`/api/*` 可走 Bearer Token
- [x] **CORS 限制**
  - 已支持 `cors_origins`
- [x] **`include_archived` 真正生效**
  - `webui/api.py` 已分支处理 archived 线程查询
- [x] **Jargon 内存计数自动 flush**
  - 已增加 `flush_threshold`
  - `main.py` 中达到阈值会触发 `flush_to_db()`
- [x] **LightRAG warmup**
  - `Lifecycle.on_load()` 已对最近活跃群预热实例
- [x] **Review gate / 审核链路**
  - 已有 `LearnedPromptReview`
  - 已有群画像/语气学习审核命令与 WebUI API
- [x] **独立人格绑定 / 语气学习**
  - 已有 `PersonaBindingService`
  - 已支持绑定预设人格、语气学习、历史版本、切换版本
- [x] **依赖声明补强**
  - `requirements.txt` 已补 `cachetools`
- [x] **部分并发/错误处理问题**
  - 背景任务 done callback、knowledge 全局缓冲上限等已有明显修补

---

## 三、当前仍成立的 gap（以 upstream/master 为准）

## P0 — 还没闭环、会影响“规划是否真落地”的问题

### 1. `cron` 类配置仍未真正接入调度器
**现状**
以下配置项虽然存在，但没有看到实际 scheduler / cron 注册逻辑：

- `GroupPersonaConfig.learning_cron`
- `JargonConfig.update_cron`
- `PersonaBindingConfig.global_learning_cron`

其中 `config.py` 里甚至直接写了：
- `global_learning_cron  # TODO: 尚未接入调度器，预留配置项`

**影响**
README / 规划里“后台定时学习”“定时更新”的承诺，目前仍主要依赖：
- 消息数阈值触发
- 启停时 flush

而不是可靠的定时任务系统。

**建议**
- 在 `Lifecycle.on_load()` 中注册统一 scheduler
- 至少先接这三类 job：
  - 群画像定时学习
  - 黑话定时推断
  - 全局语气定时学习

---

### 2. 黑话链路仍未完整闭环：**flush 有了，meaning infer 没接上**
**现状**
- `main.py` 里已经会在达到 `flush_threshold` 后触发 `flush_to_db()`
- 但 `services/jargon.py` 中的 `infer_meanings_batch()` 仍然没有看到调用点

也就是说目前黑话能力的真实状态更像：

- 会计数
- 会入库
- **但不会自动批量推断含义**

**影响**
README 里的“高频词 → 批量 LLM 推断含义”还没有完整跑通。

**建议**
- 在 `flush_to_db()` 完成后串上 `infer_meanings_batch()`
- 或者单独做 scheduler：按 group 定时推断空 meaning 词条
- 补一个手动触发命令 / WebUI 按钮更方便排障

---

### 3. `Knowledge.ingestion_cooldown` 仍然只是配置项，没有影响行为
**现状**
- `config.py` 里有 `ingestion_cooldown`
- 但代码里没有看到它被使用
- 目前 knowledge buffer 的 flush 触发条件仍主要是：
  - 达到 `ingestion_buffer_max`
  - 超过全局缓冲上限，强制刷最老 group
  - 插件终止时统一 flush

**影响**
低频群/小群的知识入库可能拖很久才落盘；和“有冷却时间就自动入库”的直觉不一致。

**建议**
- 给每个 group 增加 cooldown-based delayed flush
- 新消息进来后：
  - 到上限 → 立即刷
  - 未到上限 → 启一个 cooldown timer，到点自动刷

---

### 4. `bot_responses` 表依然没有接入真实业务链路
**现状**
- `db/models.py` 有 `BotResponse`
- `db/repo.py` 有 `save_bot_response()`
- 但目前没有看到实际调用点

**影响**
这张表还是“预备役”，不是生产链路的一部分。

**建议**
- 接入 AstrBot 的发送后 hook / 回复成功回调
- 至少记录：
  - `group_id`
  - `thread_id`
  - `response_text`
  - `timestamp`
- 后续才能做：
  - 回复质量分析
  - thread 级别闭环
  - 学习效果回看

---

### 5. `source_whitelist` 依然只是“可编辑字段”，不是实际能力
**现状**
- 模型/Repo/WebUI 都支持保存 `source_whitelist`
- 但业务逻辑里没有看到任何使用它的地方

**影响**
现在它只是数据库字段，不是功能。

**建议**
先把产品语义定清楚，再实现：
- 它是给群画像学习喂外部资料？
- 还是给知识引擎指定可信来源？
- 还是约束联网搜索范围？

如果语义不定，这个字段会长期处于“看起来很高级，但没实际作用”的状态。

---

## P1 — 现在能用，但和规划/成熟版相比还差一截

### 6. CacheConfig 仍未真正接线到 CacheManager
**现状**
- `config.py` 里有：
  - `context_ttl`
  - `embedding_ttl`
- 但 `utils/cache.py` 里 TTL 还是硬编码初始化：
  - context 300s
  - embedding 600s
  - emotion 60s
  - knowledge 300s

**影响**
配置改了，不一定会影响运行行为。

**建议**
- 用 `PluginConfig.cache` 初始化全局 CacheManager
- 把 cache size / TTL 也纳入统一配置

---

### 7. 群画像与语气学习虽然增强了，但“定时+管理”仍不完整
**现状**
上游已经补了很多：
- 群画像审核
- 语气学习
- 版本切换
- pending review 管理

但仍缺少几个成熟产品该有的能力：
- 定时任务接入（见 P0）
- WebUI 中直接查看 learned prompt history / tone version detail 的体验补全
- 一些手动触发入口仍偏命令行化，不够面板化

**建议**
- WebUI 增加：
  - 群画像历史版本查看
  - 版本对比
  - 一键回滚 / 激活
  - 手动学习按钮

---

### 8. 用户记忆系统仍是 MVP，没有治理“记忆污染”
**现状**
当前记忆链路已经能用：
- 抽取事实
- 存 embedding
- 检索相关记忆
- 注入 prompt

但仍缺：
- 去重 / merge
- 置信度
- 冲突事实处理
- 过期/遗忘
- 质量清洗

**影响**
跑久了以后，很容易出现：
- 重复事实堆积
- 低质量或过时事实污染 prompt

**建议**
- 先加最小治理：
  - 语义近似去重
  - `last_seen_at`
  - `source_count`
  - 基于 recency / importance 的检索筛选

---

### 9. 情绪系统仍更像“被动更新标签”，不是稳定情绪状态机
**现状**
- 情绪解析、衰减、mood 映射已经比早期版本稳一些
- 但当前更新触发条件仍是：
  - `event.is_at_or_wake_command`

所以它更像：
- “被叫到时顺手看一眼情绪”
而不是：
- “持续跟踪群氛围”

**影响**
如果 README 想表达的是“群整体气氛驱动 bot 的情绪”，现在还不够像。

**建议**
- 先明确语义：
  - 是“群对 bot 的情绪”
  - 还是“群当前整体气氛”
- 然后再决定触发面和状态机复杂度

---

### 10. LightRAG 集成比之前更稳，但仍然偏“轻接入”
**现状**
已改善：
- lazy-init
- warmup
- path sanitize
- finalize / close
- retrieval-only 兼容处理

但目前实例化仍然比较轻：
- `LightRAG(working_dir=...)`
- 没看到更深的 provider 桥接（例如 embedding / LLM 显式统一走 AstrBot provider）

**影响**
它现在更像：
- “能挂上去跑的可选知识引擎”
而不是：
- “和 AstrBot provider 体系彻底一体化的知识模块”

**建议**
- 如果后续真要主打知识引擎，建议补：
  - provider 适配说明
  - 失败回退策略
  - 健康检查 / 诊断信息
  - 更明确的性能边界

---

### 11. 线程路由可用了，但 WebUI 观测维度仍偏浅
**现状**
- 有线程列表
- `include_archived` 已修
- 话题路由本身也能工作

但缺：
- 单线程详情页
- 线程消息回放
- centroid / 匹配分数等调试视图

**影响**
问题排查时仍然偏黑箱。

**建议**
- 增加线程详情 API + 页面
- 至少能看到：
  - 线程最近消息
  - 当前摘要
  - 是否归档
  - 最近活跃时间

---

## P2 — 文档一致性、工程化、长期维护问题

### 12. README 仍有文档陈旧 / 与代码不一致的问题
**现状**
比较明显的一处：
- README 写 `webui_port = 18930`
- `config.py` 默认端口其实是 `7834`

另外 README 对“这是 AstrBot 插件、不是独立程序”的落地说明仍然不算很细。

**建议**
- 修默认端口
- 明确：
  - AstrBot 插件安装方式
  - 可选依赖（LightRAG / tests / rerank）
  - 数据库初始化要求
  - 常见失败排查路径

---

### 13. 仍缺 migration / schema 演进方案
**现状**
- 目前仍偏向 `create_all()` 风格
- 新表和字段已经明显增多（review / tone versions / bindings 等）

**影响**
后续 schema 演进成本会越来越高。

**建议**
- 引入 Alembic
- 补基础 migration
- 规划 embedding 维度变化 / 索引升级路径

---

### 14. 向量索引与大数据量性能策略仍未明确
**现状**
- 有 pgvector 列
- 但还没有看到明确的 ANN 索引策略文档或建索引逻辑

**影响**
数据量上来后，记忆检索 / 线程路由 / 消息检索性能可能掉得比较快。

**建议**
- 为关键向量列补索引策略
- 明确维度、距离函数、索引类型（HNSW / IVF 等）

---

### 15. 测试覆盖比之前更好了，但仍偏“单元测试优先”
**现状**
- 已新增 review gate 测试
- config 测试也更完整

但仍缺：
- AstrBot 生命周期集成测试
- WebUI API 烟测
- DB/Provider 不可用时的降级测试

**建议**
- 补最小 E2E：
  - 插件初始化
  - 收消息 → 入库 → 路由 → 注入
  - review 审核通过/拒绝
  - WebUI 核心接口 smoke test

---

## 四、建议的新版开发顺序

### Sprint 1：先把“看起来支持”变成“真的闭环”
- [ ] 接入 scheduler / cron
- [ ] 黑话 `infer_meanings_batch()` 接线
- [ ] Knowledge `ingestion_cooldown` 接线
- [ ] 接入 `bot_responses` 持久化
- [ ] README 端口/安装文档修正

### Sprint 2：补产品管理面
- [ ] `source_whitelist` 定义语义并落地
- [ ] WebUI 线程详情 / 历史视图
- [ ] 群画像 / 语气版本详情与回滚体验优化
- [ ] 手动触发学习 / 推断操作面板化

### Sprint 3：补工程化与长期质量
- [ ] CacheConfig 接线
- [ ] 用户记忆去重/衰减/治理
- [ ] migration 方案
- [ ] vector index 策略
- [ ] 集成测试补齐

---

## 五、如果现在只修 5 个点，我建议先修这 5 个

1. **scheduler 接入**
2. **jargon meaning inference 接线**
3. **knowledge cooldown flush 接线**
4. **bot_responses 接入实际发送链路**
5. **README / 文档一致性修正**

---

## 六、当前项目成熟度判断（刷新后）

刷新后的评价比第一次更高一些：

- 早期看起来像 **0.5~0.7**
- 现在上游 head 更接近 **0.7~0.8**

原因是：
- 核心能力不是空壳了
- 关键产品线（群画像 / 线程 / 审核 / 人格绑定 / 语气版本）已经明显成型
- 剩下的主要是“闭环补完”和“工程化补强”

一句话总结：

> **上游最新代码已经不是“规划远大、实现稀薄”的状态了；现在更像“产品轮廓已经出来，但还有几个关键环节没彻底打通”。**
