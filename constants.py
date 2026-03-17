"""
常量定义
"""

# 插件标识
PLUGIN_NAME = "astrbot_plugin_sb_qunyou"
PLUGIN_DISPLAY_NAME = "群聊智能体 · 千群千面"
PLUGIN_VERSION = "0.1.0"

# 情绪标签
MOODS = ("happy", "neutral", "angry", "sad", "excited", "bored")

# 向量维度 (BGE-M3 / 通用 embedding 默认维度, 可通过配置覆盖)
DEFAULT_EMBEDDING_DIM = 1024

# 后台任务
THREAD_SUMMARY_INTERVAL = 20  # 每 20 条消息更新一次线程摘要
MEMORY_MAX_FACTS_PER_MESSAGE = 3
MEMORY_TOP_K = 5

INGESTION_BUFFER_GLOBAL_MAX = 1000  # 全局最大缓冲消息数

# 注入标签
TAG_GROUP_PERSONA = "group_persona"
TAG_EMOTION = "emotion_state"
TAG_THREAD_CONTEXT = "thread_context"
TAG_USER_MEMORIES = "user_memories"
TAG_JARGON_HINTS = "jargon_hints"
