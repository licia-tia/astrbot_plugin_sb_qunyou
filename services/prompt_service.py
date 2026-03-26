"""
Prompt service for runtime database-based prompt retrieval.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..db.engine import Database


class PromptService:
    def __init__(self, db: "Database") -> None:
        self._db = db

    async def get_prompt(self, key: str) -> str:
        """Get prompt value by key. Returns empty string if not found."""
        async with self._db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            prompt = await repo.get_prompt(key)
            return prompt.value if prompt else ""

    async def ensure_seeded(self) -> None:
        """Seed database with any missing default prompts without overwriting edits."""
        async with self._db.session() as session:
            from ..db.repo import Repository
            from ..prompts.seed_data import SEED_PROMPTS
            repo = Repository(session)
            existing = await repo.list_all_prompts()
            existing_keys = {prompt.string_key for prompt in existing}
            missing = [prompt for prompt in SEED_PROMPTS if prompt["string_key"] not in existing_keys]
            if missing:
                await repo.batch_upsert_prompts(missing)
                await session.commit()
