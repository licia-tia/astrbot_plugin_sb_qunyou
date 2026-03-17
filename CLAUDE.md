# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AstrBot plugin for group chat intelligence ("群聊智能体"). Provides per-group adaptive persona, topic routing, message debouncing, speaker memory, emotion engine, jargon tracking, and persona binding with tone learning. Runs on AstrBot 4.11.4+ with PostgreSQL + pgvector.

## Development Commands

```bash
# Install dependencies
pip install -r requirements.txt
pip install pytest pytest-asyncio  # test dependencies

# Run all tests
pytest tests/ -v

# Run a single test file
pytest tests/test_debounce.py -v

# Run a single test class or method
pytest tests/test_debounce.py::TestIsCommand -v
pytest tests/test_debounce.py::TestIsCommand::test_basic_command -v
```

No build step required — this is a pure Python AstrBot plugin loaded at runtime.

## Architecture

### Plugin Lifecycle (3 phases)

1. **Bootstrap** (`lifecycle.py:bootstrap`) — Synchronous, called from `__init__`. Creates all service instances, no I/O.
2. **on_load** (`lifecycle.py:on_load`) — Async. Starts DB, resolves lazy providers (Reranker, LightRAG), starts WebUI.
3. **shutdown** (`lifecycle.py:shutdown`) — Ordered teardown: LightRAG → WebUI → background tasks → DB.

Lazy provider resolution (Issue #78): Reranker and LightRAG use a background retry loop because AstrBot's `provider_manager.inst_map` isn't populated until all plugins are loaded.

### Message Flow

```
on_message → DebounceManager.handle_event → _process_message
                                              ├── repo.save_raw_message
                                              ├── TopicThreadRouter.route_message  (async)
                                              ├── JargonService.count_words        (in-memory)
                                              ├── GroupPersonaService.increment     (async)
                                              ├── SpeakerMemory.extract_and_store   (async)
                                              ├── EmotionEngine.maybe_update        (async, probabilistic)
                                              ├── PersonaBinding.increment_and_check(async)
                                              └── Knowledge ingestion buffer        (async)
```

All async operations after `save_raw_message` are fire-and-forget via `_fire_and_forget()`.

### LLM Hook Injection (on_llm_request)

`HookHandler.handle` gathers 7 context sources concurrently, then injects into the LLM request:

| Priority | Source | Target |
|----------|--------|--------|
| HIGHEST | Persona binding (bound persona + learned tone) | `system_prompt` |
| HIGH | Group persona (base + learned) | `system_prompt` |
| HIGH | Emotion state (if non-neutral) | `system_prompt` |
| MEDIUM | Thread context (topic + recent messages) | `extra_user_content_parts` |
| MEDIUM | User memories (pgvector kNN) | `extra_user_content_parts` |
| MEDIUM | Knowledge graph (LightRAG) | `extra_user_content_parts` |
| LOW | Jargon hints | `extra_user_content_parts` |

Extra parts are optionally reranked before injection.

### Key Abstractions

- **`LLMAdapter`** (`services/llm_adapter.py`) — Bridges AstrBot's Provider API. All LLM/embedding calls go through this. Exposes `fast_chat()`, `main_chat()`, `get_embedding()`. Never access framework providers directly.
- **`Repository`** (`db/repo.py`) — Single unified CRUD class for all 11 tables. All DB access goes through this. Uses `async with db.session() as session` pattern.
- **`Database`** (`db/engine.py`) — SQLAlchemy async engine wrapper. Auto-creates pgvector extension and all tables on `start()`.
- **`CacheManager`** (`utils/cache.py`) — Global singleton with TTL/LRU caches for context, embedding, emotion, knowledge. Uses `cachetools` with fallback to plain dicts.

### Event-Driven Debounce (`pipeline/debounce.py`)

Adapted from `astrbot_plugin_continuous_message`. Uses `asyncio.Event` + `Task.cancel()`:
- Msg1 → create session, block on `flush_event.wait()`
- Msg2+ → append to buffer, `event.stop_event()`, reset timer
- Timer fires → `flush_event.set()` → Msg1 wakes, reconstructs event with merged text

Three modes: `off`, `time` (L1 time window only), `time_bert` (L1 + L2 semantic completeness via LLM).

### Topic Router (`pipeline/topic_router.py`)

Embedding cosine similarity + sliding centroid average. Messages are routed to existing threads or spawn new ones. Stale threads are auto-archived based on TTL.

### Configuration

- **`config.py`** — Pydantic v2 models. `PluginConfig` is the root, with sub-configs for each module.
- **`_conf_schema.json`** — AstrBot config panel schema. Maps flat groups (e.g., `Debounce_Settings`) to nested config.
- **`PluginConfig.from_astrbot_config(raw)`** — Converts AstrBot's flat config dict into nested Pydantic models.

### Database Models (`db/models.py`)

11 tables: `raw_messages`, `group_profiles`, `active_threads`, `thread_messages`, `user_memories`, `emotion_states`, `jargon_terms`, `bot_responses`, `learning_jobs`, `group_persona_bindings`, `persona_tone_versions`. All vector columns use pgvector's `Vector(None)` (dimension set at runtime).

### Prompt Templates (`prompts/templates.py`)

All LLM prompts are centralized here. Injection templates use XML-style tags (`<group_persona>`, `<emotion_state>`, etc.) with trust levels.

## Code Conventions

- All imports from AstrBot use `astrbot.api` namespace (`logger`, `AstrBotConfig`, `star`, `event`).
- Plugin entry point: `QunyouPlugin(star.Star)` in `main.py`, exported via `__init__.py`.
- Services use `TYPE_CHECKING` guards for circular import avoidance.
- Repository methods follow get-or-create pattern (`get_or_create_group_profile`, etc.).
- Background tasks tracked via `self.background_tasks: set[asyncio.Task]` with `_on_background_task_done` callback (logs errors, then discards).
- Log prefix convention: `[ModuleName]` (e.g., `[Qunyou]`, `[Lifecycle]`, `[Emotion]`, `[TopicRouter]`).
- Tests mock AstrBot internals; only pure-logic subcomponents are unit-testable without the framework.
- DB increments use SQL-level `UPDATE ... RETURNING` for atomicity (never Python-side read-modify-write).
- Learning job error handlers always guard `job_id` with `if job_id is not None` to avoid `UnboundLocalError`.
- LLM prompt inputs are truncated before templating (anti prompt injection): messages ≤2000 chars, sender names ≤50 chars.
- Cache keys use `hashlib.md5` hashes instead of text prefixes to avoid collisions.

## Security

- **WebUI auth**: Optional bearer token via `config.webui.auth_token`. When set, all `/api/*` endpoints require `Authorization: Bearer <token>` header. Disabled by default (`None`).
- **CORS**: Restricted to `config.webui.cors_origins` (default: `localhost:7834` only). No wildcard.
- **Bind address**: WebUI defaults to `127.0.0.1` (localhost only). Set `config.webui.host = "0.0.0.0"` to expose externally.
- **Path safety**: `group_id` is sanitized via regex (`[^\w\-.]` → `_`) + `os.path.realpath` guard in LightRAG paths.
- **Cross-group isolation**: WebUI delete endpoints verify `group_id` ownership before mutation.
- **Database config**: No built-in DB credentials. Provide `Database_Settings.dsn` or the structured `Database_Settings.*` fields via conf before use.

## Concurrency Patterns

- **`_tone_learning_locks: set[str]`** — Lock is claimed (`add`) *before* any `await`, released in `finally`. Prevents duplicate concurrent tone learning per group.
- **`_fire_and_forget(coro)`** — Creates background task with `_on_background_task_done` callback that logs exceptions. Never silently swallows errors.
- **Jargon flush** — In-memory counters auto-flush to DB when accumulating ≥ `config.jargon.flush_threshold` words (default 500). Also flushes on shutdown.
- **Knowledge buffer** — Global cap `INGESTION_BUFFER_GLOBAL_MAX` (1000 messages total). Oldest group buffer is force-flushed when cap is reached.
- **Debounce sessions** — `flush_event.wait()` is wrapped in `try/except asyncio.CancelledError` to clean up orphaned sessions.
- **Topic centroid** — Uses configurable EMA alpha (`config.topic.centroid_ema_alpha`, default 0.1) instead of `1/(count+1)` to prevent centroid freeze on long threads.
