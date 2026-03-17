---
description: "Compare this plugin's emotion system against astrbot_plugin_self_learning"
name: "Compare Emotion Systems"
argument-hint: "Optional focus areas, changed files, or a different comparison repo path"
agent: "agent"
---

Compare this workspace's emotion system against `C:\Users\lsy39\Documents\sbqunyou\astrbot_plugin_self_learning`.

Primary goal:
- Explain how the two plugins differ in emotion modeling, update triggers, persistence, prompt injection, operational behavior, and test coverage.

Working rules:
- Verify conclusions from implementation before stating them.
- Prefer source behavior over README or design-note claims.
- Treat this workspace as the primary subject and `astrbot_plugin_self_learning` as the comparison baseline.
- If the chat includes selected files, changed files, or focus areas, prioritize those and use the rest as supporting context.
- Call out uncertainty explicitly instead of guessing.

Inspect at minimum when relevant:
- Current workspace: `services/emotion.py`, `services/hook_handler.py`, `prompts/templates.py`, `db/models.py`, `db/repo.py`, `config.py`, `main.py`, `tests/test_emotion.py`.
- Comparison repo: `services/state/affection_manager.py`, `services/social/social_context_injector.py`, `services/commands/handlers.py`, `models/orm/psychological.py`, `repositories/bot_mood_repository.py`, `config.py`, `main.py`, and related tests if present.

Focus dimensions:
- Emotion model: discrete labels, intensity, valence or arousal, randomization, and per-group vs per-user scope.
- Update lifecycle: message-driven inference, scheduled rotation, decay or expiry, manual overrides, and background jobs.
- Persistence model: schema shape, active-state handling, history retention, and repository semantics.
- LLM integration: how mood is inferred, where it is injected, and whether config matches actual runtime behavior.
- Coupling: whether emotion is isolated as a dedicated service or folded into affection, persona, social, or psychological subsystems.
- Testability: unit coverage, mockability, and likely regression surface.

Return the result in this structure:

## Current Architecture
- Summarize the current workspace behavior in 4-8 bullets.

## Key Differences
- Provide a compact table with columns: `Area`, `Current workspace`, `astrbot_plugin_self_learning`, `Impact`.

## Findings
- List the most important behavior differences or risks first.
- Include concrete file references for every non-trivial claim.

## Reuse Or Migration Ideas
- Suggest which ideas from `astrbot_plugin_self_learning` are worth adopting here.
- Also note which current design choices should be preserved.

## Open Questions
- List only the uncertainties that could not be resolved from source.

Keep the answer concise, technical, and comparison-first.