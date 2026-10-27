"""Offline model double, NOT a workflow definition.

tool_step counts model/tool exchanges inside ONE Agent turn. Scheduling and
review retries belong to runner.py; configurable stage plans to definition.py.
"""

import json

from ..agents import LLMResponse, ToolCall
from .agents import PLACEHOLDERS


class DemoLLM:
    def __init__(self, role):
        self.role = role
        self.model = "offline-review" if role == "ReviewAgent" else "offline-task"
        self.tool_step = 0

    def invoke(self, messages, **kwargs):
        self.tool_step += 1
        if self.tool_step == 1:
            calls = [("get_patient_state" if self.role == "PatientStateAgent" else "get_shared_patient_state", {}),
                     ("get_stage_context", {})]
        elif self.tool_step == 2:
            calls = [("state_check", {})] if self.role == "PatientStateAgent" else [
                (name, {"request": "Demonstrate the reserved interface; do not fabricate results."})
                for name in PLACEHOLDERS[self.role] if name != "bash"
            ]
            if self.role == "PatientStateAgent":
                results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
                extracted = next((r["data"] for r in results
                                  if r["tool_name"] == "get_patient_state" and r["status"] == "success"), None)
                refs = list(extracted["patient_state"]["evidence_refs"]) if extracted else []
                if refs:
                    calls.append(("get_patient_evidence", {"evidence_refs": refs[:1]}))
        elif self.tool_step == 3:
            results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            context = next(r["data"] for r in results if r["tool_name"] == "get_stage_context")
            if self.role == "PatientStateAgent":
                output = {"summary": "Cutoff-bounded patient state extracted and checked.",
                          "limitations": ["Ambiguous units remain unresolved; missingness is not imputed."]}
            elif self.role in {"TargetSelectionAgent", "DrugSelectionAgent"}:
                output = {"candidates": [], "limitations": [
                    "Scientific connectors are placeholders; no validated ranking or recommendation is available."
                ]}
            elif self.role == "ReviewAgent":
                failed = context["stage_result"]["status"] != "completed"
                output = {"disposition": "block" if failed else "proceed",
                          "reason": "Stage execution failed; stop the offline demo." if failed else
                                    "Structured output is present; offline orchestration check only.",
                          "issues": ["Stage failed"] if failed else [],
                          "evidence_gaps": ["Offline review checks orchestration only, not medical correctness."]}
            else:
                output = {"proposals": [{"scope": "workflow", "change": "Implement and validate the reserved connectors.",
                                         "reason": "The current run uses placeholders.",
                                         "validation_plan": "Add fixture tests and held-out patient evaluation before activation."}]}
            calls = [("submit_output", output)]
        else:
            return LLMResponse(content="Stage submitted.", finish_reason="stop")
        return LLMResponse(finish_reason="tool_calls", tool_calls=[
            ToolCall(id=f"{self.role}_{self.tool_step}_{i}", name=name, arguments=json.dumps(args))
            for i, (name, args) in enumerate(calls)
        ])
