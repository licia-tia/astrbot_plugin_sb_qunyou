"""
消息防抖 Pipeline — 事件驱动架构

参考 astrbot_plugin_continuous_message 的核心设计：
  - asyncio.Event + Task.cancel() 实现精确计时器重置
  - event.stop_event() 拦截后续消息
  - 结算时重构事件让其继续传播

在原设计基础上扩展：
  - 支持群聊 (原插件仅私聊)
  - L2 语义防抖 (可选, 使用 BERT/小模型判断意图完整性)
  - 图片 URL 收集
  - 合并转发/引用消息处理 (aiocqhttp)
  - 输入状态感知 (NapCat input_status)

模式 (config.debounce.mode):
  "off"       → 直接放行
  "time"      → L1 时间窗口防抖
  "time_bert" → L1 + L2 语义完整性判断
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..config import DebounceConfig

if TYPE_CHECKING:
    from ..services.llm_adapter import LLMAdapter

# ------------------------------------------------------------------ #
#  Platform detection
# ------------------------------------------------------------------ #

try:
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
        AiocqhttpMessageEvent,
    )
    IS_AIOCQHTTP = True
except ImportError:
    IS_AIOCQHTTP = False

# ------------------------------------------------------------------ #
#  Message component detection
# ------------------------------------------------------------------ #

_ImageComponent: Any = None
_PlainComponent: Any = None

try:
    from astrbot.api.message_components import Image, Plain
    _ImageComponent = Image
    _PlainComponent = Plain
except ImportError:
    try:
        from astrbot.api.message import Image, Plain
        _ImageComponent = Image
        _PlainComponent = Plain
    except ImportError:
        pass


# ================================================================== #
#  Session data structure
# ================================================================== #

@dataclass
class DebounceSession:
    """Per-user debounce session (event-driven, like continuous_message)."""
    buffer: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    flush_event: asyncio.Event = field(default_factory=asyncio.Event)
    timer_task: Optional[asyncio.Task] = None
    is_typing: bool = False


# ================================================================== #
#  Message Parser (adapted from continuous_message)
# ================================================================== #

class MessageParser:
    """Extracts text, images from events and reconstructs events."""

    @staticmethod
    def parse_message(message_obj: Any) -> tuple[str, bool, list[str]]:
        """Parse a message object → (text, has_image, image_urls)."""
        text = ""
        has_image = False
        image_urls: list[str] = []

        try:
            if not hasattr(message_obj, "message"):
                return "", False, []

            for comp in message_obj.message:
                # Skip Reply components
                if comp.__class__.__name__ == "Reply":
                    continue

                # Extract text
                if hasattr(comp, "text") and comp.text:
                    text += comp.text
                elif hasattr(comp, "content") and comp.content:
                    text += comp.content

                # Detect images
                is_img = False
                if _ImageComponent and isinstance(comp, _ImageComponent):
                    is_img = True
                elif comp.__class__.__name__ == "Image":
                    is_img = True

                if is_img:
                    has_image = True
                    if hasattr(comp, "url") and comp.url:
                        image_urls.append(comp.url)
                    elif hasattr(comp, "file") and comp.file:
                        image_urls.append(comp.file)
        except Exception:
            pass

        return text, has_image, image_urls

    @staticmethod
    def reconstruct_event(
        event: AstrMessageEvent, text: str, image_urls: list[str]
    ) -> None:
        """Rewrite event with merged text + images so it can continue propagating."""
        event.message_str = text

        if not _PlainComponent:
            return

        chain = []
        if text:
            chain.append(_PlainComponent(text=text))

        if image_urls and _ImageComponent:
            for url in image_urls:
                try:
                    chain.append(_ImageComponent(file=url))
                except TypeError:
                    try:
                        chain.append(_ImageComponent(url=url))
                    except Exception:
                        pass

        if hasattr(event.message_obj, "message"):
            try:
                event.message_obj.message = chain
            except Exception:
                pass

    @staticmethod
    def is_typing_event(event: AstrMessageEvent) -> bool:
        """Detect NapCat input_status typing notification."""
        if not IS_AIOCQHTTP:
            return False
        try:
            raw = getattr(event.message_obj, "raw_message", None)
            if raw is None:
                return False
            return (
                raw.get("post_type") == "notice"
                and raw.get("sub_type") == "input_status"
            )
        except Exception:
            return False

    @staticmethod
    def is_command(text: str, prefixes: list[str]) -> bool:
        """Check if message starts with a command prefix."""
        text = text.strip()
        if not text:
            return False
        return any(text.startswith(p) for p in prefixes)


# ================================================================== #
#  Forward Handler (adapted from continuous_message)
# ================================================================== #

class ForwardHandler:
    """Handle QQ forward/reply messages (aiocqhttp only)."""

    REPLY_FORMAT = "[引用消息({sender_name}: {full_text})]"
    BOT_REPLY_HINT = "[系统提示：以上引用的消息是你(助手)之前发送的内容]"

    @staticmethod
    async def extract_forward_or_reply(
        event: AstrMessageEvent,
    ) -> tuple[str, list[str]]:
        """Try to extract forward/reply content from event.

        Returns (text, image_urls). Empty if not applicable.
        """
        if not IS_AIOCQHTTP:
            return "", []

        try:
            from astrbot.api.message_components import Forward, Reply
        except ImportError:
            return "", []

        if not isinstance(event, AiocqhttpMessageEvent):
            return "", []

        # Check for Forward component
        for seg in event.message_obj.message:
            if isinstance(seg, Forward):
                return await ForwardHandler._extract_forward(event, seg.id)

        # Check for Reply component (quoted message)
        for seg in event.message_obj.message:
            if isinstance(seg, Reply):
                return await ForwardHandler._extract_reply(event, seg.id)

        return "", []

    @staticmethod
    async def _extract_forward(
        event: AstrMessageEvent, forward_id: str
    ) -> tuple[str, list[str]]:
        """Extract merged-forward message content."""
        try:
            client = event.bot
            data = await client.api.call_action("get_forward_msg", id=forward_id)

            if not data or "messages" not in data:
                return "", []

            texts = []
            images = []
            for node in data["messages"]:
                sender = node.get("sender", {}).get("nickname", "未知用户")
                raw_content = node.get("message") or node.get("content", [])
                content = ForwardHandler._parse_raw(raw_content)

                parts = []
                for seg in content:
                    if not isinstance(seg, dict):
                        continue
                    if seg.get("type") == "text":
                        parts.append(seg.get("data", {}).get("text", ""))
                    elif seg.get("type") == "image":
                        url = seg.get("data", {}).get("url")
                        if url:
                            images.append(url)
                            parts.append("[图片]")

                node_text = "".join(parts).strip()
                if node_text:
                    texts.append(f"{sender}: {node_text}")

            return "\n".join(texts), images

        except Exception as e:
            logger.debug(f"[Debounce] Forward extract failed: {e}")
            return "", []

    @staticmethod
    async def _extract_reply(
        event: AstrMessageEvent, reply_id: str
    ) -> tuple[str, list[str]]:
        """Extract quoted reply message content."""
        try:
            client = event.bot
            msg = await client.api.call_action("get_msg", message_id=reply_id)

            if not msg or "message" not in msg:
                return "", []

            sender_name = msg.get("sender", {}).get("nickname", "未知")
            content = ForwardHandler._parse_raw(msg["message"])

            text_parts = []
            image_urls = []
            for seg in content:
                if not isinstance(seg, dict):
                    continue
                if seg.get("type") == "text":
                    text_parts.append(seg.get("data", {}).get("text", ""))
                elif seg.get("type") == "image":
                    url = seg.get("data", {}).get("url")
                    if url:
                        image_urls.append(url)

            full_text = "".join(text_parts).strip()
            if not full_text:
                return "", image_urls

            # Check if sender is the bot
            is_bot = False
            try:
                sender_id = msg.get("sender", {}).get("user_id")
                bot_id = getattr(event.message_obj, "self_id", None)
                if bot_id and sender_id:
                    is_bot = str(sender_id) == str(bot_id)
            except Exception:
                pass

            formatted = ForwardHandler.REPLY_FORMAT.format(
                sender_name=sender_name, full_text=full_text
            )
            if is_bot:
                formatted += "\n" + ForwardHandler.BOT_REPLY_HINT

            return formatted, image_urls

        except Exception as e:
            logger.debug(f"[Debounce] Reply extract failed: {e}")
            return "", []

    @staticmethod
    def _parse_raw(raw_content: Any) -> list[dict]:
        """Normalize raw message content to list of dicts."""
        import json as _json
        if isinstance(raw_content, list):
            return raw_content
        if isinstance(raw_content, str):
            try:
                parsed = _json.loads(raw_content)
                if isinstance(parsed, list):
                    return parsed
            except (ValueError, TypeError):
                pass
            return [{"type": "text", "data": {"text": raw_content}}]
        return []


# ================================================================== #
#  DebounceManager — event-driven architecture
# ================================================================== #

class DebounceManager:
    """Event-driven message debouncing.

    Core pattern (from continuous_message):
      Msg1 → create session, start timer, `await flush_event.wait()`
      Msg2 → append to buffer, cancel old timer, start new, `event.stop_event()`
      Timer fires → `flush_event.set()` → msg1 handler wakes up
      → reconstruct event with merged text → let it propagate
    """

    def __init__(self, config: DebounceConfig, llm: Optional["LLMAdapter"] = None) -> None:
        self._config = config
        self._llm = llm
        self._sessions: Dict[str, DebounceSession] = {}
        self._parser = MessageParser()

    @property
    def _pending(self) -> Dict[str, DebounceSession]:
        """Expose sessions for testing compatibility."""
        return self._sessions

    async def handle_event(self, event: AstrMessageEvent) -> bool:
        """Process an event through the debounce pipeline.

        Returns:
            True  → event has been handled (merged text is set on event, continue)
            False → event has been intercepted (stop_event called, or skipped)
            None  → debounce is off, treat as passthrough
        """
        if self._config.mode == "off":
            return True  # passthrough

        # 0a. Input status detection (NapCat typing notification)
        if self._config.enable_typing_detection and MessageParser.is_typing_event(event):
            await self._handle_typing_event(event)
            return False

        # 0b. Extract forward/reply content (aiocqhttp)
        forward_text = ""
        forward_images: list[str] = []
        if self._config.enable_forward_analysis:
            forward_text, forward_images = await ForwardHandler.extract_forward_or_reply(event)

        # 1. Parse message content
        raw_text, has_image, current_urls = self._parser.parse_message(event.message_obj)
        if not raw_text:
            raw_text = (event.get_message_str() or "").strip()

        # Merge forward content
        if forward_text:
            raw_text = forward_text + ("\n" + raw_text if raw_text else "")
        if forward_images:
            current_urls.extend(forward_images)
            has_image = True

        uid = event.unified_msg_origin

        # 2. Command passthrough → interrupt debounce and flush
        if MessageParser.is_command(raw_text, self._config.command_prefixes):
            if uid in self._sessions:
                session = self._sessions[uid]
                if session.timer_task:
                    session.timer_task.cancel()
                session.flush_event.set()
            return True  # let command pass through

        # 3. Skip empty messages
        if not raw_text and not has_image:
            return False

        # ---- Core debounce logic ----

        # Scenario A: Append to existing session (Msg 2, 3...)
        if uid in self._sessions:
            session = self._sessions[uid]

            if raw_text and len(session.buffer) < self._config.max_fragments:
                session.buffer.append(raw_text)
            if current_urls:
                session.images.extend(current_urls)

            # Reset timer
            if session.timer_task:
                session.timer_task.cancel()
            session.timer_task = asyncio.create_task(
                self._timer_coroutine(uid, self._config.time_window_seconds)
            )

            event.stop_event()
            return False

        # Scenario B: First message → create session + block
        flush_event = asyncio.Event()
        timer_task = asyncio.create_task(
            self._timer_coroutine(uid, self._config.time_window_seconds)
        )

        self._sessions[uid] = DebounceSession(
            buffer=[raw_text] if raw_text else [],
            images=current_urls,
            flush_event=flush_event,
            timer_task=timer_task,
        )

        logger.debug(f"[Debounce] Session started - {uid}")

        # Block until timer fires or interrupt
        try:
            await flush_event.wait()
        except asyncio.CancelledError:
            # Clean up on cancellation
            if uid in self._sessions:
                session_data = self._sessions.pop(uid)
                if session_data.timer_task:
                    session_data.timer_task.cancel()
            raise

        # ---- Settlement ----
        if uid not in self._sessions:
            return False

        session_data = self._sessions.pop(uid)
        merged_text = self._config.merge_separator.join(session_data.buffer).strip()

        if not merged_text and not session_data.images:
            return False

        # L2: Semantic completeness check (optional)
        if self._config.mode == "time_bert" and merged_text:
            is_complete = await self._check_semantic_completeness(merged_text)
            if not is_complete:
                # If incomplete, wait a bit more for additional input
                logger.debug(
                    f"[Debounce] L2 incomplete, extending wait - {uid}"
                )
                # Re-enter as new session with existing buffer
                flush_event2 = asyncio.Event()
                timer_task2 = asyncio.create_task(
                    self._timer_coroutine(uid, self._config.time_window_seconds)
                )
                self._sessions[uid] = DebounceSession(
                    buffer=[merged_text],
                    images=session_data.images,
                    flush_event=flush_event2,
                    timer_task=timer_task2,
                )
                await flush_event2.wait()
                if uid not in self._sessions:
                    return False
                session_data = self._sessions.pop(uid)
                merged_text = self._config.merge_separator.join(
                    session_data.buffer
                ).strip()

        img_info = f" + {len(session_data.images)}图" if session_data.images else ""
        logger.info(
            f"[Debounce] Settlement: {len(session_data.buffer)} msg(s){img_info} → merge"
        )

        # Reconstruct event
        MessageParser.reconstruct_event(event, merged_text, session_data.images)
        return True  # event continues with merged content

    async def _timer_coroutine(self, uid: str, duration: float) -> None:
        """Timer that triggers settlement after duration."""
        try:
            await asyncio.sleep(duration)
            if uid in self._sessions:
                self._sessions[uid].flush_event.set()
        except asyncio.CancelledError:
            pass

    async def _handle_typing_event(self, event: AstrMessageEvent) -> None:
        """Handle NapCat input_status typing notification."""
        try:
            raw = event.message_obj.raw_message
            status_text = raw.get("status_text", "")
            uid = event.unified_msg_origin
            is_typing = "正在输入" in status_text

            if uid not in self._sessions:
                event.stop_event()
                return

            session = self._sessions[uid]

            if is_typing:
                # User is typing → cancel timer, pause settlement
                session.is_typing = True
                if session.timer_task:
                    session.timer_task.cancel()
                # Timeout protection
                session.timer_task = asyncio.create_task(
                    self._timer_coroutine(uid, self._config.max_typing_wait)
                )
                logger.debug(
                    f"[Debounce] Typing detected, paused "
                    f"(timeout {self._config.max_typing_wait}s) - {uid}"
                )
            else:
                # User stopped typing → resume debounce timer
                if session.is_typing:
                    session.is_typing = False
                    if session.timer_task:
                        session.timer_task.cancel()
                    session.timer_task = asyncio.create_task(
                        self._timer_coroutine(uid, self._config.time_window_seconds)
                    )
                    logger.debug(
                        f"[Debounce] Typing stopped, resumed "
                        f"{self._config.time_window_seconds}s - {uid}"
                    )

            event.stop_event()
        except Exception as e:
            logger.debug(f"[Debounce] Typing event error: {e}")

    async def _check_semantic_completeness(self, text: str) -> bool:
        """L2: Use LLM/model to check if text is a complete intention.

        Returns True if text is complete, False if it seems cut off.
        """
        if len(text) >= self._config.semantic_min_length:
            return True  # Long enough, assume complete

        if not self._llm:
            return True  # No LLM available, assume complete

        try:
            from ..prompts.templates import SEMANTIC_COMPLETENESS
            prompt = SEMANTIC_COMPLETENESS.format(text=text)
            response = await self._llm.fast_chat(prompt)
            if response and "incomplete" in response.lower():
                return False
            return True
        except Exception:
            return True  # On error, assume complete

    async def flush_all(self) -> None:
        """Force-release all pending sessions (shutdown)."""
        for uid in list(self._sessions.keys()):
            session = self._sessions.get(uid)
            if session:
                if session.timer_task:
                    session.timer_task.cancel()
                session.flush_event.set()

    def set_release_callback(self, cb) -> None:
        """Legacy compatibility — no-op in event-driven mode."""
        pass  # Not needed in event-driven architecture
