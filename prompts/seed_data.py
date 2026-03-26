"""
Seed data for system_prompts table.
Copied from prompts/templates.py values for initial DB population.
"""

SEED_PROMPTS = [
    {
        "string_key": "GROUP_PERSONA_LEARN",
        "value": """你是一位群聊分析专家。请根据以下群聊消息，提取出这个群的核心特征。

要求：
1. 用客观的第三人称描述群聊氛围
2. 提取 3-5 个核心关注点/热点话题
3. 描述群内互动风格（是否活跃、正式/informal、是否有明显的群文化）
4. 不要包含任何个人隐私信息
5. 输出不超过 200 字

消息记录：
{messages}

请输出群画像描述：""",
        "description": "群画像学习 - 根据群聊消息提取群的核心特征",
        "category": "learning",
    },
    {
        "string_key": "THREAD_SUMMARY",
        "value": """请用 2-4 个关键词概括以下对话的主题：

{messages}

主题关键词：""",
        "description": "话题摘要生成 - 用关键词概括对话主题",
        "category": "learning",
    },
    {
        "string_key": "EMOTION_ANALYZE",
        "value": """分析以下对话中用户对 Bot 表达的情感倾向。

对话内容：
{message}

请从以下选项中选择最匹配的情绪标签，仅输出标签：
happy, neutral, angry, sad, excited, bored

情绪标签：""",
        "description": "情绪分析 - 分析用户对 Bot 的情感倾向",
        "category": "learning",
    },
    {
        "string_key": "MEMORY_EXTRACT",
        "value": """从以下消息中抽取值得记忆的事实（如果有的话）。
请只抽取客观事实或明确的偏好声明，不要推测。
如果没有值得记忆的内容，输出空的 JSON 数组。

发送者：{sender_name}
消息：{message}

请以 JSON 数组格式输出，每个元素是一句简短的事实描述：
```json
["事实1", "事实2"]
```""",
        "description": "用户记忆抽取 - 从消息中抽取值得记忆的事实",
        "category": "learning",
    },
    {
        "string_key": "JARGON_INFER_BATCH",
        "value": """以下是一个群聊中频繁出现的词语或短语。
请为每个词语推断其在群聊语境下的含义。

词语列表：
{terms}

请以 JSON 对象格式输出，key 为词语，value 为含义（一句话，不超过 30 字）：
```json
{{"词语1": "含义1", "词语2": "含义2"}}
```""",
        "description": "黑话含义推断 - 批量推断群聊中黑话的含义",
        "category": "learning",
    },
    {
        "string_key": "SEMANTIC_COMPLETENESS",
        "value": """判断以下文本是否表达了一个完整的意图或想法。

文本：{text}

如果是完整意图，输出 "complete"
如果是不完整的片段（如只有称呼、语气词、或明显话说一半），输出 "incomplete"

判断：""",
        "description": "语义完整性判断 - L2 防抖，判断文本是否表达完整意图",
        "category": "learning",
    },
    {
        "string_key": "COMBINED_LEARNING_PROMPT",
        "value": """你是一位群聊人格分析专家。请根据以下信息，更新该群的人格描述。

原始人格设定：
{original_persona}

群聊消息（最近 {count} 条）：
{messages}

要求：
1. 提炼群的核心定位和话题
2. 总结群成员的说话风格/语气特征（如口头禅、常用句式、表达习惯）
3. 描述群文化/潜规则
4. 如果有原始人格，请在原有基础上迭代演化，保留核心特征
5. 输出 200 字以内的人格描述，包含以上全部要素
6. 不要包含个人隐私信息

人格描述：""",
        "description": "人格+语气合并学习 - 根据群聊信息更新人格描述",
        "category": "learning",
    },
    {
        "string_key": "INJECTION_GROUP_PERSONA",
        "value": """<group_persona>
{persona}
</group_persona>""",
        "description": "群画像注入 - 将群画像注入到 LLM 上下文",
        "category": "injection",
    },
    {
        "string_key": "INJECTION_EMOTION",
        "value": """<emotion_state>
当前情绪：{mood}
</emotion_state>""",
        "description": "情绪状态注入 - 将当前情绪状态注入到 LLM 上下文",
        "category": "injection",
    },
    {
        "string_key": "INJECTION_THREAD_CONTEXT",
        "value": """<thread_context topic="{topic}">
{messages}
</thread_context>""",
        "description": "话题上下文注入 - 将话题和消息历史注入到 LLM 上下文",
        "category": "injection",
    },
    {
        "string_key": "INJECTION_USER_MEMORIES",
        "value": """<user_memories user="{user_id}" trust="medium">
{memories}
</user_memories>""",
        "description": "用户记忆注入 - 将用户记忆注入到 LLM 上下文",
        "category": "injection",
    },
    {
        "string_key": "INJECTION_JARGON",
        "value": """<jargon_hints trust="low">
{hints}
</jargon_hints>""",
        "description": "黑话提示注入 - 将黑话解释注入到 LLM 上下文",
        "category": "injection",
    },
]
