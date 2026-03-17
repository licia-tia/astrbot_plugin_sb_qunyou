---
description: "Compare this plugin's prompt injection, memory retrieval, and learning behavior against astrbot_plugin_self_learning"
name: "Compare Context And Learning"
argument-hint: "Optional focus areas, changed files, or specific questions"
agent: "agent"
model: "GPT-5 (copilot)"
---

Compare this workspace against `C:\Users\lsy39\Documents\sbqunyou\astrbot_plugin_self_learning`.

Primary goal:
- Explain how the two plugins differ in prompt/context injection, prompt hardening, memory extraction and retrieval, LightRAG or knowledge flow, and group learning behavior.

Working rules:
- Verify conclusions from source before stating them.
- Prefer implementation behavior over README claims.
- Treat this workspace as the primary subject and `astrbot_plugin_self_learning` as the comparison baseline.
- If the chat includes selected files, changed files, or a focus area, prioritize those and use the rest as supporting context.
- Call out uncertainty explicitly instead of guessing.

Inspect at minimum when relevant:
- Current workspace: `services/hook_handler.py`, `prompts/templates.py`, `pipeline/context_builder.py`, `services/speaker_memory.py`, `services/group_persona.py`, `services/persona_binding.py`, `services/knowledge/lightrag_manager.py`, `main.py`, `config.py`.
- Comparison repo: `services/hooks/llm_hook_handler.py`, `services/integration/mem0_memory_manager.py`, `services/integration/lightrag_knowledge_manager.py`, `services/learning/message_pipeline.py`, `services/learning/group_orchestrator.py`, `main.py`, `config.py`.

Focus dimensions:
- Injection surface: what goes into `system_prompt` vs `extra_user_content_parts`, and in what order.
- Trust model and hardening: prompt truncation, source separation, reranking, XML or tag wrappers, fallbacks.
- Memory pipeline: extraction method, storage backend, retrieval method, scoping, and deduplication or approval behavior.
- Knowledge pipeline: LightRAG or alternate knowledge engine lifecycle, ingestion path, retrieval shape, and per-group isolation.
- Learning workflow: background jobs, debounce or batching, session-level updates, approval gates, and shutdown behavior.
- Extensibility: provider abstraction, config-driven engine switching, and operational tradeoffs.

Return the result in this structure:

## Current Architecture
- Summarize the current workspace behavior in 4-8 bullets.

## Key Differences
- Provide a compact table with columns: `Area`, `Current workspace`, `astrbot_plugin_self_learning`, `Impact`.

## Findings
- List the most important behavior differences or risks first.
- Include concrete file references for every non-trivial claim.

## Migration Or Reuse Opportunities
- Suggest which ideas from `astrbot_plugin_self_learning` are worth adopting here.
- Also note which current design choices should be preserved.

## Open Questions
- List only the uncertainties that could not be resolved from source.

Keep the answer concise, technical, and comparison-first.

