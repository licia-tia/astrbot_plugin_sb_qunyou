# Project Guidelines

AstrBot plugin for per-group chat intelligence. Keep instructions here minimal and repo-wide. For deeper architecture and security details, see CLAUDE.md and README.md.

## Build And Test

- Install runtime dependencies with `pip install -r requirements.txt`.
- Install test dependencies with `pip install pytest pytest-asyncio`.
- Run the full test suite with `pytest tests/ -v`.
- Run focused tests with `pytest tests/test_debounce.py -v` or a fully qualified test node.
- There is no build step. This is a pure Python plugin loaded by AstrBot at runtime.

## Architecture

- Follow the three-phase lifecycle in `lifecycle.py`: synchronous bootstrap with no I/O, async `on_load` startup, ordered async shutdown.
- Keep AstrBot framework access behind `services/llm_adapter.py`. Do not call framework providers directly from feature code.
- Route all database access through `db/repo.py` using `async with db.session() as session` from `db/engine.py`.
- Treat `main.py` as the message orchestration layer: persist the raw message first, then schedule secondary work through tracked background tasks.
- Use `services/hook_handler.py` as the source of truth for LLM request context injection and priority ordering.

## Conventions

- Import AstrBot symbols from `astrbot.api`.
- Match the existing logging pattern with `[ModuleName]` prefixes.
- Use `TYPE_CHECKING` guards to avoid circular imports when services reference each other.
- Track fire-and-forget tasks in `self.background_tasks` and attach the done callback so exceptions are logged.
- Keep config changes aligned with both `config.py` and `_conf_schema.json`.
- Prefer tests for pure logic components and mock AstrBot internals instead of trying to unit test the framework.

## Pitfalls

- Do not initialize lazy provider-backed services during bootstrap; AstrBot provider instances are not fully ready until `on_load`.
- Guard learning-job cleanup with `if job_id is not None` in exception paths.
- Truncate user-controlled prompt inputs before templating to preserve the existing prompt-injection safeguards.
- Preserve the current atomic DB update style in repository methods rather than switching to Python-side read-modify-write.
- The default DSN uses a placeholder password and must not be treated as deployment-ready config.