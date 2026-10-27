from __future__ import annotations

from typing import Any


def state_tool_schema(patient_id: str, max_cutoff_day: int) -> dict[str, Any]:
    """OpenAI-compatible schema with the patient and upper time bound fixed server-side."""
    return {
        "type": "function",
        "function": {
            "name": "get_patient_state",
            "description": (
                f"Return cutoff-safe BPC facts for the bound patient {patient_id}. "
                f"cutoff_day must not exceed {max_cutoff_day}."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "cutoff_day": {
                        "type": "integer",
                        "maximum": max_cutoff_day,
                        "description": "Inclusive day relative to the first BPC cancer diagnosis.",
                    }
                },
                "required": ["cutoff_day"],
            },
        },
    }


def evidence_tool_schema(patient_id: str, max_cutoff_day: int) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "get_patient_evidence",
            "description": (
                f"Return only whitelisted evidence metadata already visible for {patient_id}; "
                "never returns a raw source row."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "cutoff_day": {"type": "integer", "maximum": max_cutoff_day},
                    "evidence_refs": {
                        "type": "array",
                        "items": {"type": "string", "pattern": "^E-[0-9a-f]{24}$"},
                        "minItems": 1,
                    },
                },
                "required": ["cutoff_day", "evidence_refs"],
            },
        },
    }
