"""Offline-only observed follow-up export. Never registered as an Agent tool."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .extractor import BPCStateExtractor, DEFAULT_DATA_DIR
from .models import EvaluationData, EvaluationMeta, Observations, PatientState
from .semantics import field_statuses


def state_sha256(state: dict[str, Any]) -> str:
    canonical = json.dumps(
        state,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class OfflineEvaluationExporter:
    """Explicitly separate offline reader with its own extractor and time scope.

    The caller must keep this capability/output outside the Agent's tools and
    readable directories. The class does not mutate a bounded PatientState tool.
    """

    def __init__(
        self,
        data_dir: str | Path = DEFAULT_DATA_DIR,
        rules_path: str | Path | None = None,
    ):
        self._extractor = BPCStateExtractor(data_dir, rules_path)

    def export(
        self,
        patient_id: str,
        cutoff_day: int,
        end_day: int,
        cancer_id: str | None = None,
    ) -> dict[str, Any]:
        for name, value in [("cutoff_day", cutoff_day), ("end_day", end_day)]:
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
        if end_day <= cutoff_day:
            raise ValueError("end_day must be greater than cutoff_day")

        input_state = self._extractor.get_patient_state(
            patient_id, cutoff_day, cancer_id
        )
        # Keep the input's chosen target fixed even if more cancers are known later.
        target = input_state["meta"]["target_cancer_id"]
        end_state = PatientState.model_validate(
            self._extractor.get_patient_state(patient_id, end_day, target)
        )
        future: dict[str, list[Any]] = {}
        delayed: dict[str, list[Any]] = {}
        used_refs: set[str] = set()
        statuses = {}
        for kind in ("imaging", "med_onc", "tumor_markers"):
            selected = [
                item
                for item in getattr(end_state.observations, kind)
                if cutoff_day < item.available_day <= end_day
            ]
            future[kind] = [
                item for item in selected if cutoff_day < item.event_day <= end_day
            ]
            delayed[kind] = [item for item in selected if item.event_day <= cutoff_day]
            for item in selected:
                used_refs.update(item.evidence_refs)
            statuses[kind] = "recorded" if future[kind] else "none_recorded_in_window"

        evaluation = EvaluationData(
            meta=EvaluationMeta(
                patient_id=patient_id,
                cohort=input_state["meta"]["cohort"],
                target_cancer_id=target,
                input_cutoff_day=cutoff_day,
                evaluation_end_day=end_day,
                input_state_sha256=state_sha256(input_state),
            ),
            observations=Observations(**future),
            late_available_observations=Observations(**delayed),
            window_status=statuses,
            evidence_refs={
                ref: end_state.evidence_refs[ref] for ref in sorted(used_refs)
            },
        )
        evaluation.field_status = field_statuses(evaluation, self._extractor.rule_set)
        return evaluation.model_dump(mode="json")
