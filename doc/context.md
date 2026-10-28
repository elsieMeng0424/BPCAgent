# Context budgeting and compaction

Context handling belongs to the shared `Agent` layer, immediately before a model
request. BasicAgent, ReActAgent, WorkflowAgent and UnitPatientStateAgent use the
same implementation in `src/agents/context.py`. The runner still owns task/review
turns; it does not summarize their conversations.

## What this version does

This is **structural compaction with exact source retrieval**, not a model-written
summary. It does not call an extra summarization model and never rewrites the shared patient
snapshot, clinical values, evidence files, or original transcript.

When a request grows too large, eligible long tool results and older plain-text
assistant responses are replaced in the **request copy** by a bounded index:

- A content-addressed `record_id` and original character count.
- The source tool name/status and a bounded list of its data keys.
- Patient ID, cancer ID, cutoff and schema version when present in snapshot metadata.
- Instructions to read the original fields before drawing conclusions.

The automatically registered `read_context_record` tool reads exact source text
from this Agent run's archive. It cannot open arbitrary paths, access another
Agent's archive, or retrieve data that was never returned to this Agent. It does
not query the dataset or expose future evaluation labels.

```json
{
  "record_id": "ctx_<64-character SHA-256 digest>",
  "pointer": "/data/patient_state/meta",
  "offset": 0,
  "limit": 1000
}
```

The pointer addresses the original ToolResult, including its `/data` wrapper.
Use an empty pointer for the exact original text. JSON Pointer escaping uses
`~1` for `/` and `~0` for `~`. Pages contain at most 2,000 characters. A page with
`complete: false` is only a fragment; follow `next_offset` and concatenate pages
before parsing the complete selected value. JSON field selection preserves
numbers, `null`, `"unknown"`, and evidence identifiers without inference.

All system/developer instructions and user messages are retained. Function-call
arguments, IDs and provider reasoning items are not rewritten. Tool errors and
`submit_output` results are protected. Stage context and state checks can be
archived losslessly and read back on demand. Old reader pages retire to the
original record ID and page coordinates, without recursive archives; even pages
below the ordinary compaction-size threshold can retire. Recent reader pages
remain visible for the next model request. Recent outputs are preferred for retention;
a recent oversized tool result can still be archived to meet the hard budget.
Calls and their results are never split or removed; incomplete batches fail
before any model request.

## Configuration and dynamic budgets

Library `Config()` leaves this feature disabled for backwards-compatible behavior.
The checked-in live model profiles in `config/models.jsonc` enable it. Demo mode
uses its own offline configurations and does not load live model profiles.

```python
from bpcagent import BasicAgent, Config

config = Config(
    # Add the usual provider/model credentials when using a real model.
    context={
        "enabled": True,
        "context_window": None,
        "gateway_context_window": None,
        "max_input_tokens": 32000,
        "reserve_output_tokens": 8192,
        "safety_margin_tokens": 2048,
        "trigger_ratio": 0.75,
        "target_ratio": 0.55,
        "keep_recent_messages": 4,
        "min_compact_chars": 2000,
    },
)
# Supply a configured real model or an offline test double:
# agent = BasicAgent("research", config=config, llm=your_model, tool_registry=tools)
```

Set verified context sizes separately for each model profile and gateway route.
No model-name-to-capacity table is guessed. A null window means **unknown**, not
unlimited: the local input cap still applies, but is not a guarantee that an
unknown provider accepts that many tokens.

```text
output reservation = max(reserve_output_tokens, effective request output limit)
input budget = min(max_input_tokens,
                   smallest known window - output reservation - safety margin)
```

If no window is known, only `max_input_tokens` bounds input. If no output limit
is set, enabled context management explicitly sends `reserve_output_tokens` as
`max_tokens` instead of relying on an unknown provider default. A larger per-call
output override reduces the available input budget when a window is configured.
Output limits must be top-level parameters, not nested inside `extra_body`, so
provider-specific overrides cannot silently bypass the reservation.

The estimate includes messages, tool definitions, reasoning items and request
extras. It currently counts serialized **UTF-8 bytes plus framing allowance**,
not exact provider tokens. This deliberately conservative approximation can
compress earlier than necessary, especially with large JSON or Chinese text.
Opaque reasoning and transport-specific serialization remain provider-dependent;
this is not an exact tokenizer or an automatic provider-limit discovery feature.

Compaction begins above 75% of the input budget and aims for 55%. These are tunable
engineering defaults, not official model limits. The target is best effort:
protected content may prevent reaching it, but no request is sent above the
estimated hard budget. Compression/readback adds tool turns, so account for this
in `max_tool_iterations`; it does not bypass that limit.

An individual binding in `config/agents.jsonc` can override selected fields:

```json
{"llm": "deepseek_flash_low", "context": {"max_input_tokens": 24000}}
```

Overrides merge into the selected model profile, then the complete policy is
validated. Changing one Agent's policy does not mutate another Agent's settings.
The resolved policies are saved in `workflow_result.json` under `llm_settings`.

## Logs, failure behavior and limitations

- Workflow turns keep full originals in `transcript.json`; `context_usage.json`
  stores budget events, exact archived records and the last model-visible request.
- BasicAgent/ReActAgent and UnitPatientStateAgent logs include the same information
  under `context`. These files contain patient/tool data and must remain local.
- Each run resets its archive. Original transcripts and shared snapshots are not
  used as mutable compression buffers. Existing `_history` persistence semantics
  are unchanged; compaction does not add cross-run persistent memory.
  The legacy `max_history_length` field remains unused; budgeting is not based on
  an arbitrary count of messages.
- If instructions, schemas, protected results or recent reader pages still exceed
  the budget, the Agent returns `context_budget_exceeded` before calling the model.
  Workflow Review receives that explicit failure; no partial result is accepted.
- Streaming and explicitly tool-disabled Agents get the same budget guard, but
  do not archive content without a usable retrieval tool. They stop on oversized
  inputs instead of creating unreadable references. Streaming propagates the
  AgentException while preserving its audit log.
- State checks or stage context can themselves be large. This version intentionally
  stops rather than rewriting their constraints. Narrowing those APIs requires a
  separate, domain-aware design. No LLM-generated clinical summaries are enabled.
- A compressed source being available is not proof that the model read every
  relevant fact. Review and domain-specific output validation remain necessary.

Offline regression checks:

```bash
python -m pytest -q tests/test_context.py tests/test_workflow_config.py
```
