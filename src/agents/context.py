"""Request-only context compaction with exact, run-scoped source retrieval.

This is structural compaction, not an LLM-generated clinical summary. Full
transcripts remain untouched; only copies sent to the model are compacted.
"""

from copy import deepcopy
import hashlib
import json
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .exceptions import AgentException


class ContextPolicy(BaseModel):
    """Local policy, never forwarded as provider API parameters."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    enabled: bool = False
    context_window: int | None = Field(None, gt=0)
    gateway_context_window: int | None = Field(None, gt=0)
    max_input_tokens: int = Field(32000, gt=0)
    reserve_output_tokens: int = Field(8192, gt=0)
    safety_margin_tokens: int = Field(2048, ge=0)
    trigger_ratio: float = Field(0.75, gt=0, le=1, allow_inf_nan=False)
    target_ratio: float = Field(0.55, gt=0, lt=1, allow_inf_nan=False)
    keep_recent_messages: int = Field(4, ge=0)
    min_compact_chars: int = Field(2000, gt=0)

    @model_validator(mode="after")
    def validate_budget(self):
        if self.target_ratio >= self.trigger_ratio:
            raise ValueError("target_ratio must be smaller than trigger_ratio")
        limits = [n for n in (self.context_window, self.gateway_context_window) if n is not None]
        if limits and min(limits) <= self.reserve_output_tokens + self.safety_margin_tokens:
            raise ValueError("Context window must leave a positive input budget")
        return self


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class ContextManager:
    """Archive large completed outputs, retaining tool-call IDs and source roles.

    Token accounting uses a deliberately conservative UTF-8 byte estimate plus
    framing allowance, not an exact tokenizer or a provider capacity guarantee.
    Model limits must be supplied by the caller; model names are not guessed.
    """

    reader_name = "read_context_record"
    protected_tools = {"submit_output"}

    def __init__(self, policy):
        self.policy = policy
        self.reset()

    def reset(self):
        self.records = {}
        self.events = []
        self.last_request = None

    @staticmethod
    def estimate(messages, options):
        # Include tool schemas, response schemas, reasoning items and request extras.
        return len(encode({"messages": messages, "options": options}).encode("utf-8")) + 256 + 16 * len(messages)

    def budget(self, output_tokens):
        policy = self.policy
        reserve = max(policy.reserve_output_tokens, output_tokens or 0)
        windows = [n for n in (policy.context_window, policy.gateway_context_window) if n is not None]
        budget = policy.max_input_tokens
        if windows:
            budget = min(budget, min(windows) - reserve - policy.safety_margin_tokens)
        if budget <= 0:
            raise AgentException("Output reservation leaves no input budget", code="context_budget_exceeded")
        return budget

    @staticmethod
    def _validate_tool_pairs(messages):
        pending = set()
        for message in messages:
            if message.get("role") == "tool":
                identity = message.get("tool_call_id")
                if identity not in pending:
                    raise AgentException("Unpaired tool result in context", code="invalid_context")
                pending.remove(identity)
            else:
                if pending:
                    raise AgentException("Incomplete tool-call batch in context", code="invalid_context")
                calls = message.get("tool_calls", [])
                pending = {call["id"] for call in calls}
                if len(pending) != len(calls):
                    raise AgentException("Duplicate tool-call IDs in context", code="invalid_context")
        if pending:
            raise AgentException("Incomplete tool-call batch in context", code="invalid_context")

    def _candidate(self, message):
        content = message.get("content")
        if not isinstance(content, str):
            return False
        if message["role"] == "tool":
            try:
                result = json.loads(content)
            except ValueError:
                return False
            threshold = 512 if isinstance(result, dict) and result.get("tool_name") == self.reader_name else self.policy.min_compact_chars
            return (len(content) >= threshold and isinstance(result, dict) and result.get("status") == "success"
                    and result.get("tool_name") not in self.protected_tools)
        # Never rewrite reasoning items, function arguments, instructions or users.
        return (len(content) >= self.policy.min_compact_chars and message["role"] == "assistant" and not message.get("tool_calls")
                and message.get("response_items") is None and message.get("reasoning_content") is None)

    def _archive(self, message):
        original = message["content"]
        if message["role"] == "tool":
            result = json.loads(original)
            if result.get("tool_name") == self.reader_name:
                # Retire old readback pages without creating recursive archives.
                # The original record and exact page coordinates remain readable.
                page = result["data"]
                return encode({"context_compacted": True, "read_tool": self.reader_name,
                               "notice": "Previously read page retired; re-read original coordinates if needed.",
                               **{key: page[key] for key in ("record_id", "pointer", "offset", "next_offset", "total_chars")}})
        identity = "ctx_" + hashlib.sha256(original.encode("utf-8")).hexdigest()
        self.records[identity] = original
        descriptor = {"context_compacted": True, "record_id": identity,
                      "original_chars": len(original), "read_tool": self.reader_name,
                      "notice": "Source archived, not summarized. Read needed fields before drawing conclusions; absence here is not a negative finding."}
        if message["role"] == "tool":
            result = json.loads(original)
            data = result.get("data")
            descriptor.update(tool_name=result["tool_name"], status=result["status"])
            if isinstance(data, dict):
                descriptor["execution"] = {k: data[k] for k in
                    ("exit_code", "timed_out", "oom_killed", "stdout_truncated", "stderr_truncated") if k in data}
                # Bounded field index; arbitrary tool keys are data, never instructions.
                keys = sorted(data)
                descriptor["data_keys"] = [key[:100] for key in keys[:24]]
                descriptor["data_keys_truncated"] = len(keys) > 24 or any(len(k) > 100 for k in keys[:24])
                state = data.get("patient_state", data)
                meta = state.get("meta", {}) if isinstance(state, dict) else {}
                if isinstance(meta, dict):
                    descriptor["patient_boundary"] = {k: meta[k] for k in
                        ("patient_id", "target_cancer_id", "cutoff_day", "schema_version") if k in meta}
        return encode(descriptor)

    def prepare(self, messages, options, *, output_tokens=None, can_read=False):
        """Return a bounded request copy; fail closed if protected content cannot fit."""
        if not self.policy.enabled:
            return deepcopy(messages)
        self._validate_tool_pairs(messages)
        request = deepcopy(messages)
        try:
            budget = self.budget(output_tokens)
        except AgentException:
            self.events.append({"request": len(self.events) + 1, "message_count": len(messages),
                                "status": "budget_exceeded", "reason": "output_reservation"})
            raise
        before = self.estimate(request, options)
        target = int(budget * self.policy.target_ratio)
        selected = []
        if before > int(budget * self.policy.trigger_ratio) and can_read:
            boundary = len(messages) - self.policy.keep_recent_messages
            # Prefer older outputs. Recent large TOOL results can be archived only
            # when necessary to meet the hard budget, with exact readback available.
            old = [i for i, m in enumerate(messages) if i < boundary and self._candidate(m)]
            recent = [i for i, m in enumerate(messages) if i >= boundary and m["role"] == "tool" and self._candidate(m)
                      and json.loads(m["content"]).get("tool_name") != self.reader_name]
            for i in old + recent:
                current = self.estimate(request, options)
                if current <= target or (i in recent and current <= budget):
                    break
                compact = self._archive(messages[i])
                if len(compact.encode("utf-8")) >= len(request[i]["content"].encode("utf-8")):
                    continue
                request[i]["content"] = compact
                selected.append(i)
        after = self.estimate(request, options)
        event = {"request": len(self.events) + 1, "estimator": "utf8_bytes_with_framing",
                 "message_count": len(messages),
                 "reserved_output_tokens": max(self.policy.reserve_output_tokens, output_tokens or 0),
                 "input_budget": budget, "estimated_before": before, "estimated_after": after,
                 "compacted_message_indices": selected,
                 "status": "budget_exceeded" if after > budget else "compacted" if selected else "unchanged"}
        self.events.append(event)
        self.last_request = deepcopy(request)
        if after > budget:
            raise AgentException("Protected context exceeds the input budget; narrow tool results or increase the configured limit",
                                 code="context_budget_exceeded")
        return request

    def read(self, record_id, pointer="", offset=0, limit=1000):
        """Read only records archived by this Agent run; never accept a file path.

        JSON Pointer selects a field in the ORIGINAL ToolResult (e.g. /data/meta).
        Character pagination is explicit; partial pages are not complete JSON.
        """
        if record_id not in self.records:
            raise ValueError("Unknown context record for this Agent run")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 2000:
            raise ValueError("Invalid page bounds")
        text = self.records[record_id]
        if pointer:
            if not pointer.startswith("/") or re.search(r"~(?![01])", pointer):
                raise ValueError("Invalid JSON Pointer")
            value = json.loads(text)
            for part in pointer[1:].split("/"):
                key = part.replace("~1", "/").replace("~0", "~")
                if isinstance(value, list):
                    if not re.fullmatch(r"0|[1-9][0-9]*", key):
                        raise ValueError("Invalid array index")
                    value = value[int(key)]
                elif isinstance(value, dict):
                    value = value[key]
                else:
                    raise ValueError("Pointer does not address a container")
            text = encode(value)
        end = min(offset + limit, len(text))
        if offset > len(text):
            raise ValueError("Offset exceeds record length")
        return {"record_id": record_id, "pointer": pointer, "offset": offset,
                "content": text[offset:end], "total_chars": len(text),
                "next_offset": end if end < len(text) else None,
                "complete": offset == 0 and end == len(text)}

    def audit(self):
        """Persist exact sources and the last model-visible request separately."""
        return {"policy": self.policy.model_dump(), "events": deepcopy(self.events),
                "records": deepcopy(self.records), "last_request": deepcopy(self.last_request)}
