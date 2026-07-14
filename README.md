# gayastock

AI-assisted Korean stock trading agent.

## Strategy diagnostics

Each scheduled run records structured diagnostics in `logs/agent_runs.jsonl`:

- `loops[].market_regime`: raw regime metrics and decision
- `loops[].llm`: model, completion status, response length, and tool-call count
- `loops[].tool_log`: function names, arguments, success flags, and result previews

These fields make it possible to distinguish market-rule blocking, LLM/API failure, candidate rejection, and order failure without relying only on the natural-language summary.
