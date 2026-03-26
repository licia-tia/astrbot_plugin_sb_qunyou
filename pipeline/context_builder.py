"""
上下文构建器 — 将各数据源组装为注入文本

HookHandler 调用此模块来格式化注入内容。
测试时可以传入一个假的 PromptService 实例，直接验证格式化结果。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..services.prompt_service import PromptService


class ContextBuilder:
    """Assembles context injection strings from raw data."""

    def __init__(self, prompts: "PromptService") -> None:
        self._prompts = prompts

    async def build_persona_injection(self, persona_prompt: str) -> str:
        """Format group persona for extra content injection."""
        if not persona_prompt:
            return ""
        template = await self._prompts.get_prompt("INJECTION_GROUP_PERSONA")
        return template.format(persona=persona_prompt)

    async def build_emotion_injection(self, mood: str) -> str:
        """Format emotion state for extra content injection."""
        if not mood or mood == "neutral":
            return ""
        template = await self._prompts.get_prompt("INJECTION_EMOTION")
        return template.format(mood=mood)

    async def build_thread_injection(
        self,
        topic: str, messages: list[dict]
    ) -> str:
        """Format thread context for extra content injection."""
        if not messages:
            return ""
        lines = []
        for m in messages:
            sender = m.get("sender", "?")
            text = m.get("text", "")
            lines.append(f"[{sender}]: {text}")
        template = await self._prompts.get_prompt("INJECTION_THREAD_CONTEXT")
        return template.format(
            topic=topic or "ongoing",
            messages="\n".join(lines),
        )

    async def build_memory_injection(self, user_id: str, facts: list[str]) -> str:
        """Format user memories for extra content injection."""
        if not facts:
            return ""
        template = await self._prompts.get_prompt("INJECTION_USER_MEMORIES")
        return template.format(
            user_id=user_id,
            memories="\n".join(f"- {f}" for f in facts),
        )

    async def build_jargon_injection(self, matches: list[tuple[str, str]]) -> str:
        """Format jargon hints for extra content injection."""
        if not matches:
            return ""
        hints = "\n".join(f"「{t}」= {m}" for t, m in matches)
        template = await self._prompts.get_prompt("INJECTION_JARGON")
        return template.format(hints=hints)
