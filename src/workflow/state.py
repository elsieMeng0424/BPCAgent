"""One frozen, cutoff-bounded snapshot shared by all workflow roles."""

from collections import Counter
import hashlib
import json
from pathlib import Path

from ..agents.unit_rules import analyte_type, expected_normalization, normalize_unit


def digest(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()).hexdigest()


def save_json(path: Path, data):
    with path.open("x", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def check_state(state):
    """Validate schema, visible timing, evidence links and null annotations."""
    from bpc_state.models import PatientState

    PatientState.model_validate(state)
    cutoff = state["meta"]["cutoff_day"]
    refs = set(state["evidence_refs"])
    issues = []

    def walk(value, pointer=""):
        if isinstance(value, dict):
            for field, item in value.items():
                if field in {"meta", "evidence_refs", "field_status", "missing_information"}:
                    if field == "evidence_refs" and isinstance(item, list):
                        if not set(item) <= refs:
                            raise ValueError("Patient state has dangling evidence references")
                    continue
                path = pointer + "/" + field.replace("~", "~0").replace("/", "~1")
                if field in {"available_day", "event_day", "start_day", "end_day", "collection_day"}:
                    if item is not None and item > cutoff:
                        raise ValueError("Patient state contains post-cutoff events")
                if item is None and path not in state["field_status"]:
                    issues.append({"path": path, "status": "unknown", "reason": "missing_null_annotation"})
                walk(item, path)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                walk(item, pointer + f"/{i}")

    walk(state)
    for path, status in state["field_status"].items():
        if not set(status["evidence_refs"]) <= refs:
            raise ValueError("Field status has dangling evidence references")
        issues.append({"path": path, **status})
    return {
        "schema_valid": True,
        "complete": not issues,
        "status_counts": dict(Counter(item["status"] for item in issues)),
        "issues": issues,
        "note": "Schema completeness does not imply clinical completeness; missing values remain explicit.",
    }


def normalize_markers(state):
    """Conservative arithmetic only; ambiguous units/assays remain unresolved.

    Reuses the backbone's existing conversion arithmetic. No inferred correction
    or modification of the raw PatientState is performed by this minimal tool.
    """
    result = []
    for marker in state["observations"]["tumor_markers"]:
        kind = analyte_type(marker["analyte_raw"])
        unit = normalize_unit(marker["unit_raw"])
        accepted = (kind == "psa" and unit in {"ng/ml", "ug/l"}) or (
            kind in {"total_testosterone", "free_testosterone"} and unit == "ng/dl"
        )
        converted = expected_normalization(marker, marker["unit_raw"]) if accepted else {
            "status": "unresolved", "value": None, "unit": None, "factor": None
        }
        result.append({
            "tumor_marker_id": marker["tumor_marker_id"],
            "analyte_type": kind, "normalization": converted,
            "missing_status": "unknown" if converted["status"] == "unresolved" else None,
            "reason": "explicit_unit_conversion" if converted["status"] == "normalized"
                      else "requires_unit_or_assay_review",
            "evidence_refs": marker["evidence_refs"],
        })
    return result


class SharedPatientState:
    def __init__(self, *, patient_id, cutoff_day, data_dir, cancer_id=None, rules_path=None, start_day=None):
        from bpc_state import BPCStateExtractor, BoundedPatientTool

        if type(cutoff_day) is not int:
            raise ValueError("cutoff_day must be an integer")
        if start_day is not None and (type(start_day) is not int or start_day > cutoff_day):
            raise ValueError("start_day must be an integer <= cutoff_day")
        self._bound = BoundedPatientTool(
            BPCStateExtractor(data_dir, rules_path), patient_id, cutoff_day, cancer_id
        )
        self.cutoff_day = cutoff_day
        self.start_day = start_day
        self._json = None

    def extract(self):
        if self._json is None:
            state = self._bound.get_patient_state(self.cutoff_day)
            check = check_state(state)
            recent = {
                key: [r for r in records if self.start_day is None or r["event_day"] >= self.start_day]
                for key, records in state["observations"].items()
            }
            payload = {
                "patient_state": state, "patient_state_sha256": digest(state),
                "recent_start_day": self.start_day, "recent_observations": recent,
                "state_check": check, "normalized_markers": normalize_markers(state),
            }
            self._json = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        return self.read()

    def read(self):
        if self._json is None:
            raise ValueError("PatientStateAgent must call get_patient_state first")
        # Each caller gets an independent copy of exactly the same snapshot.
        return json.loads(self._json)

    def get_evidence(self, evidence_refs):
        """Resolve only snapshot evidence at the host-bound cutoff; no future labels."""
        state = self.read()["patient_state"]
        if not set(evidence_refs) <= set(state["evidence_refs"]):
            raise ValueError("Evidence references must belong to the shared patient snapshot")
        return self._bound.get_evidence(self.cutoff_day, evidence_refs)

    @property
    def ready(self):
        return self._json is not None
