"""
插件配置系统 — 分模块 Pydantic 子配置类
"""
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import URL


class DebounceConfig(BaseModel):
    """消息防抖配置"""
    model_config = ConfigDict(extra="ignore")

    mode: Literal["off", "time", "time_bert"] = "time"
    time_window_seconds: float = 3.0
    bert_model_path: Optional[str] = None
    max_fragments: int = 10  # 单次防抖最多拼接条数
    semantic_min_length: int = 20  # L2 语义判断的最小字符长度

    # 事件驱动防抖 (from continuous_message)
    merge_separator: str = "\n"             # 消息拼接分隔符
    command_prefixes: list[str] = ["/", "#"]  # 指令前缀 (跳过防抖)
    enable_forward_analysis: bool = True    # 启用合并转发消息分析
    enable_typing_detection: bool = True    # 启用输入状态感知 (NapCat)
    max_typing_wait: float = 60.0           # 输入状态最大等待秒数 (超时保护)


class TopicConfig(BaseModel):
    """话题路由配置"""
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    similarity_threshold: float = 0.75
    thread_ttl_minutes: int = 30
    max_threads_per_group: int = 10
    summary_interval: int = 20  # 每 N 条消息更新线程摘要
    fast_model_provider_id: Optional[str] = None  # 话题感知用快速模型
    centroid_ema_alpha: float = 0.1  # 话题向量指数移动平均 alpha (0=不更新, 1=完全替换)


class GroupPersonaConfig(BaseModel):
    """千群千面配置"""
    model_config = ConfigDict(extra="ignore")

    batch_learning_threshold: int = 200  # 消息累积阈值触发学习
    learning_cron: str = "0 3 * * *"     # 定时学习 cron 表达式
    max_history_versions: int = 10        # 保留多少个 learned_prompt 历史版本


class EmotionConfig(BaseModel):
    """情绪引擎配置"""
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    sensitivity: float = 0.5   # 0.0=极稳定, 1.0=极善变
    initial_mood: str = "neutral"
    decay_hours: int = 24      # 情绪自然衰减回 neutral 的小时数


class JargonConfig(BaseModel):
    """黑话统计配置"""
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    update_cron: str = "0 4 * * *"  # 定时更新 cron
    min_frequency: int = 5          # 最低出现次数才入库
    batch_infer_size: int = 20      # 每批推断含义的词数
    infer_max_tokens: int = 150     # LLM 推断含义的 max_tokens
    flush_threshold: int = 500      # 每组累计 N 个词后自动 flush 到 DB


class DatabaseConfig(BaseModel):
    """PostgreSQL 数据库配置"""
    model_config = ConfigDict(extra="ignore")

    dsn: str = ""
    host: str = ""
    port: int = Field(default=5432, ge=1)
    user: str = ""
    password: str = ""
    database_name: str = ""
    pool_size: int = Field(default=10, ge=1)
    pool_min_size: int = Field(default=2, ge=1)
    echo: bool = False

    def connection_url(self) -> str:
        """Build the async SQLAlchemy DSN, preferring an explicit override."""
        dsn = self.dsn.strip()
        if dsn:
            return dsn

        host = self.host.strip()
        user = self.user.strip()
        database_name = self.database_name.strip()
        missing_fields: list[str] = []
        if not host:
            missing_fields.append("Database_Settings.host")
        if not user:
            missing_fields.append("Database_Settings.user")
        if not self.password:
            missing_fields.append("Database_Settings.password")
        if not database_name:
            missing_fields.append("Database_Settings.database_name")
        if missing_fields:
            missing = ", ".join(missing_fields)
            raise ValueError(
                "Database configuration is incomplete. Set Database_Settings.dsn "
                f"or provide {missing} in conf."
            )

        return URL.create(
            "postgresql+asyncpg",
            username=user,
            password=self.password,
            host=host,
            port=self.port,
            database=database_name,
        ).render_as_string(hide_password=False)

    def sqlalchemy_pool_options(self) -> dict[str, int]:
        """Map total/persistent pool settings onto SQLAlchemy queue-pool knobs."""
        persistent_pool_size = min(self.pool_min_size, self.pool_size)
        return {
            "pool_size": persistent_pool_size,
            "max_overflow": max(self.pool_size - persistent_pool_size, 0),
        }


class WebUIConfig(BaseModel):
    """WebUI 配置"""
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    port: int = 7834
    host: str = "127.0.0.1"
    auth_token: Optional[str] = None  # Bearer token for API auth; None = no auth
    cors_origins: list[str] = ["http://localhost:7834", "http://127.0.0.1:7834"]


class KnowledgeConfig(BaseModel):
    """知识引擎配置 (LightRAG)"""
    model_config = ConfigDict(extra="ignore")

    engine: Literal["off", "lightrag"] = "off"
    lightrag_working_dir: str = "./lightrag_data"
    query_mode: str = "mix"  # naive / local / global / hybrid / mix
    retrieval_only_query_preferred: bool = True
    min_ingestion_length: int = 15  # 低于此长度不入库
    ingestion_buffer_max: int = 10  # 缓冲区最大消息数
    ingestion_cooldown: int = 60    # 批量入库冷却秒数
    warmup_active_groups_limit: int = 100


class RerankConfig(BaseModel):
    """Reranker 重排序配置"""
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    provider_id: Optional[str] = None
    top_k: int = 5


class CacheConfig(BaseModel):
    """缓存管理配置"""
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    context_ttl: int = 300       # 上下文缓存 TTL (秒)
    embedding_ttl: int = 600     # embedding 缓存 TTL (秒)


class PersonaBindingConfig(BaseModel):
    """独立人格绑定与语气学习配置"""
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    auto_learning_enabled: bool = True
    auto_apply_learned_tone: bool = True
    tone_learning_threshold: int = 100
    global_learning_cron: str = "0 3 * * *"  # TODO: 尚未接入调度器，预留配置项
    max_tone_history_versions: int = 10


class ReviewGateConfig(BaseModel):
    """学习提示词审核门禁配置"""
    model_config = ConfigDict(extra="ignore")

    enabled_for_group_persona: bool = False
    enabled_for_tone: bool = False
    require_admin_review: bool = True
    auto_approve_minor_diff: bool = False
    minor_diff_threshold_percent: float = 5.0
    max_pending_per_group: int = 1
    retention_days_pending: int = 7
    retention_days_rejected: int = 30


class PluginConfig(BaseModel):
    """插件总配置"""
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    # 各模块子配置
    debounce: DebounceConfig = Field(default_factory=DebounceConfig)
    topic: TopicConfig = Field(default_factory=TopicConfig)
    group_persona: GroupPersonaConfig = Field(default_factory=GroupPersonaConfig)
    emotion: EmotionConfig = Field(default_factory=EmotionConfig)
    jargon: JargonConfig = Field(default_factory=JargonConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    webui: WebUIConfig = Field(default_factory=WebUIConfig)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    rerank: RerankConfig = Field(default_factory=RerankConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    persona_binding: PersonaBindingConfig = Field(default_factory=PersonaBindingConfig)
    review_gate: ReviewGateConfig = Field(default_factory=ReviewGateConfig)

    # 全局 LLM Provider ID
    embedding_provider_id: Optional[str] = None    # 向量 embedding 模型
    main_llm_provider_id: Optional[str] = None     # 主回复/深度分析模型
    fast_llm_provider_id: Optional[str] = None     # 快速分析/分类模型

    # 向量维度 (取决于 embedding 模型, BGE-M3 = 1024)
    embedding_dim: int = 1024

    @classmethod
    def from_astrbot_config(cls, raw: dict) -> "PluginConfig":
        """从 AstrBot 配置面板的原始 dict 构建 PluginConfig。

        AstrBot 的 _conf_schema.json 会把配置按 group 展平，
        这里将它们映射回嵌套结构。
        """
        debounce_raw = raw.get("Debounce_Settings", {})
        topic_raw = raw.get("Topic_Settings", {})
        persona_raw = raw.get("GroupPersona_Settings", {})
        emotion_raw = raw.get("Emotion_Settings", {})
        jargon_raw = raw.get("Jargon_Settings", {})
        db_raw = raw.get("Database_Settings", {})
        webui_raw = raw.get("WebUI_Settings", {})
        model_raw = raw.get("Model_Configuration", {})
        knowledge_raw = raw.get("Knowledge_Settings", {})
        rerank_raw = raw.get("Rerank_Settings", {})
        cache_raw = raw.get("Cache_Settings", {})
        persona_binding_raw = raw.get("PersonaBinding_Settings", {})
        review_gate_raw = raw.get("ReviewGate_Settings", {})

        return cls(
            debounce=DebounceConfig(**debounce_raw) if debounce_raw else DebounceConfig(),
            topic=TopicConfig(**topic_raw) if topic_raw else TopicConfig(),
            group_persona=GroupPersonaConfig(**persona_raw) if persona_raw else GroupPersonaConfig(),
            emotion=EmotionConfig(**emotion_raw) if emotion_raw else EmotionConfig(),
            jargon=JargonConfig(**jargon_raw) if jargon_raw else JargonConfig(),
            database=DatabaseConfig(**db_raw) if db_raw else DatabaseConfig(),
            webui=WebUIConfig(**webui_raw) if webui_raw else WebUIConfig(),
            knowledge=KnowledgeConfig(**knowledge_raw) if knowledge_raw else KnowledgeConfig(),
            rerank=RerankConfig(**rerank_raw) if rerank_raw else RerankConfig(),
            cache=CacheConfig(**cache_raw) if cache_raw else CacheConfig(),
            persona_binding=PersonaBindingConfig(**persona_binding_raw) if persona_binding_raw else PersonaBindingConfig(),
            review_gate=ReviewGateConfig(**review_gate_raw) if review_gate_raw else ReviewGateConfig(),
            embedding_provider_id=model_raw.get("embedding_provider_id"),
            main_llm_provider_id=model_raw.get("main_llm_provider_id"),
            fast_llm_provider_id=model_raw.get("fast_llm_provider_id"),
            embedding_dim=model_raw.get("embedding_dim", 1024),
        )
