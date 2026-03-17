"""
统一缓存管理器 — 基于 cachetools

移植自 self-learning 插件的 CacheManager，支持：
  - TTLCache: 有明确过期时间的数据 (context, embedding, emotion)
  - LRUCache: 热点数据 (general)
  - 装饰器模式 (@async_cached)
  - 命中率统计
"""
from __future__ import annotations

from collections import defaultdict
from functools import wraps
from typing import Any, Callable, Dict, Optional

from astrbot.api import logger

try:
    from cachetools import TTLCache, LRUCache, Cache
    HAS_CACHETOOLS = True
except ImportError:
    HAS_CACHETOOLS = False
    # Fallback: dict-based caches (no TTL/LRU)
    Cache = dict  # type: ignore

    class TTLCache(dict):  # type: ignore
        """Fallback TTL cache (no real TTL, but enforces maxsize)."""
        def __init__(self, maxsize=128, ttl=300):
            super().__init__()
            self.maxsize = maxsize

        def __setitem__(self, key, value):
            # Evict oldest entries if at capacity
            while len(self) >= self.maxsize:
                try:
                    oldest_key = next(iter(self))
                    del self[oldest_key]
                except (StopIteration, KeyError):
                    break
            super().__setitem__(key, value)

    class LRUCache(dict):  # type: ignore
        """Fallback LRU cache (approximate LRU via insertion order)."""
        def __init__(self, maxsize=128):
            super().__init__()
            self.maxsize = maxsize

        def __setitem__(self, key, value):
            # If key exists, remove to re-insert at end
            if key in self:
                del self[key]
            # Evict oldest if at capacity
            while len(self) >= self.maxsize:
                try:
                    oldest_key = next(iter(self))
                    del self[oldest_key]
                except (StopIteration, KeyError):
                    break
            super().__setitem__(key, value)

        def __getitem__(self, key):
            # Move to end on access (approximate LRU)
            value = super().__getitem__(key)
            del self[key]
            super().__setitem__(key, value)
            return value

        def get(self, key, default=None):
            try:
                return self.__getitem__(key)
            except KeyError:
                return default


class CacheManager:
    """统一缓存管理器 — cachetools 驱动。"""

    def __init__(self) -> None:
        # TTL caches — 有过期时间
        self.context_cache = TTLCache(maxsize=128, ttl=300)      # 5min
        self.embedding_cache = TTLCache(maxsize=256, ttl=600)    # 10min
        self.emotion_cache = TTLCache(maxsize=500, ttl=60)       # 1min
        self.knowledge_cache = TTLCache(maxsize=64, ttl=300)     # 5min

        # LRU caches — 保持热点
        self.general_cache = LRUCache(maxsize=2000)

        # 统计
        self._hits: Dict[str, int] = defaultdict(int)
        self._misses: Dict[str, int] = defaultdict(int)

        logger.debug("[CacheManager] Initialized")

    def get(self, cache_name: str, key: str) -> Optional[Any]:
        """获取缓存值，记录命中统计。"""
        cache = self._resolve(cache_name)
        if cache is None:
            return None
        result = cache.get(key)
        if result is not None:
            self._hits[cache_name] += 1
        else:
            self._misses[cache_name] += 1
        return result

    def set(self, cache_name: str, key: str, value: Any) -> None:
        """写入缓存。"""
        cache = self._resolve(cache_name)
        if cache is not None:
            cache[key] = value

    def delete(self, cache_name: str, key: str) -> None:
        """删除单个缓存条目。"""
        cache = self._resolve(cache_name)
        if cache and key in cache:
            del cache[key]

    def clear(self, cache_name: str) -> None:
        """清空指定缓存。"""
        cache = self._resolve(cache_name)
        if cache is not None:
            cache.clear()

    def clear_all(self) -> None:
        """清空所有缓存。"""
        for name in ("context", "embedding", "emotion", "knowledge", "general"):
            c = self._resolve(name)
            if c:
                c.clear()

    def get_stats(self) -> Dict[str, Dict[str, Any]]:
        """获取所有缓存命中率统计。"""
        stats = {}
        all_names = set(self._hits.keys()) | set(self._misses.keys())
        for name in all_names:
            hits = self._hits.get(name, 0)
            misses = self._misses.get(name, 0)
            total = hits + misses
            stats[name] = {
                "hits": hits,
                "misses": misses,
                "hit_rate": hits / total if total > 0 else 0.0,
            }
        return stats

    def get_cache_size(self, cache_name: str) -> Dict[str, int]:
        """获取指定缓存的大小信息。"""
        cache = self._resolve(cache_name)
        if cache is None:
            return {}
        result: Dict[str, int] = {"size": len(cache)}
        if hasattr(cache, "maxsize"):
            result["maxsize"] = cache.maxsize
        return result

    def _resolve(self, name: str) -> Optional[Any]:
        """解析缓存名称到实例。"""
        _map = {
            "context": self.context_cache,
            "embedding": self.embedding_cache,
            "emotion": self.emotion_cache,
            "knowledge": self.knowledge_cache,
            "general": self.general_cache,
        }
        return _map.get(name)


# ================================================================== #
#  Decorators
# ================================================================== #

def async_cached(
    cache_name: str = "general",
    key_func: Optional[Callable] = None,
    manager: Optional[CacheManager] = None,
):
    """异步缓存装饰器。

    Example::
        @async_cached("context", key_func=lambda g, q: f"{g}:{q}")
        async def query_context(group_id, query):
            ...
    """
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            if key_func:
                key = key_func(*args, **kwargs)
            else:
                key = f"{func.__name__}:{args}:{kwargs}"

            mgr = manager if manager else get_cache_manager()

            cached = mgr.get(cache_name, key)
            if cached is not None:
                return cached

            result = await func(*args, **kwargs)
            mgr.set(cache_name, key, result)
            return result
        return wrapper
    return decorator


# ================================================================== #
#  Global singleton
# ================================================================== #

_global_manager: Optional[CacheManager] = None


def get_cache_manager() -> CacheManager:
    """获取全局缓存管理器单例。"""
    global _global_manager
    if _global_manager is None:
        _global_manager = CacheManager()
    return _global_manager
