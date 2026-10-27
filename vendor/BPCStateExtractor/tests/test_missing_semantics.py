from __future__ import annotations

import json
from pathlib import Path

import pytest

from bpc_state import BPCStateExtractor
from bpc_state.models import PatientState
from bpc_state.semantics import source_status


@pytest.mark.parametrize(
    "raw,status",
    [
        ("Unknown", "unknown"),
        ("Unknown, no documentation found", "unknown"),
        ("Not stated", "not_reported"),
        ("PSA Change not mentioned", "not_reported"),
        ("Not tested", "not_tested"),
        ("Not detected", "not_detected"),
        ("Not Applicable", "not_applicable"),
        ("Not Collected", "not_collected"),
        ("NA", "unknown"),
        ("", "not_recorded"),
    ],
)
def test_source_missing_semantics_are_not_collapsed(raw: str, status: str) -> None:
    assert source_status(raw)[0] == status


@pytest.mark.parametrize("raw", [0, "0", "No", "Negative", "None noted"])
def test_no_zero_and_negative_are_not_generic_missing(raw) -> None:
    assert source_status(raw) is None


@pytest.mark.parametrize(
    "raw,status",
    [
        ("", "not_recorded"),
        ("Unknown", "unknown"),
        ("NA", "unknown"),
        ("Not tested", "not_tested"),
        ("Not detected", "not_detected"),
        ("Not Applicable", "not_applicable"),
        ("garbled", "invalid"),
        ("<0.1", "non_exact"),
    ],
)
def test_null_numeric_has_explicit_reason_and_preserves_source(
    fixture_data: Path, raw: str, status: str
) -> None:
    extractor = BPCStateExtractor(fixture_data)
    row = extractor.repository.clinical_rows("tumor_markers", "Prostate", "P1")[0]
    row["tm_num_result"] = raw
    state = extractor.get_patient_state("P1", 20)
    marker = state["observations"]["tumor_markers"][0]
    assert marker["value_numeric"] is None
    assert (
        state["field_status"]["/observations/tumor_markers/0/value_numeric"]["status"]
        == status
    )
    if raw:
        evidence = state["evidence_refs"][marker["evidence_refs"][0]]["sources"][0]
        assert evidence["values"]["tm_num_result"] == raw
    PatientState.model_validate(json.loads(json.dumps(state, allow_nan=False)))


def test_zero_and_missing_unit_remain_distinct(fixture_data: Path) -> None:
    extractor = BPCStateExtractor(fixture_data)
    row = extractor.repository.clinical_rows("tumor_markers", "Prostate", "P1")[0]
    row.update(tm_num_result="0", tm_result_units="")
    state = extractor.get_patient_state("P1", 20)
    marker = state["observations"]["tumor_markers"][0]
    assert marker["value_numeric"] == 0
    assert marker["unit_raw"] is None
    assert "/observations/tumor_markers/0/value_numeric" not in state["field_status"]
    assert (
        state["field_status"]["/observations/tumor_markers/0/unit_raw"]["status"]
        == "not_recorded"
    )


def test_policy_exclusion_and_unknown_end_are_not_source_blanks(
    fixture_data: Path,
) -> None:
    extractor = BPCStateExtractor(fixture_data)
    extractor.rules["demographics"].remove("naaccr_sex_code")
    state = extractor.get_patient_state("P1", 40)
    assert state["demographics"]["sex_raw"] is None
    assert (
        state["field_status"]["/demographics/sex_raw"]["status"] == "excluded_by_policy"
    )
    treatments = state["treatment_history"]
    index = next(
        i for i, item in enumerate(treatments) if item["drug_name_raw"] == "DrugA"
    )
    assert treatments[index]["end_day"] is None
    assert (
        state["field_status"][f"/treatment_history/{index}/end_day"]["status"]
        == "unknown"
    )
    masked = next(
        i
        for i, item in enumerate(treatments)
        if item["drug_name_raw"] == "Investigational Drug"
    )
    assert (
        state["field_status"][f"/treatment_history/{masked}/end_day"]["status"]
        == "masked"
    )


def test_nullable_clinical_fields_are_explicit_and_annotated(
    fixture_data: Path,
) -> None:
    state = BPCStateExtractor(fixture_data).get_patient_state("P1", 100)

    def check(value, pointer=""):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {
                    "meta",
                    "evidence_refs",
                    "field_status",
                    "missing_information",
                }:
                    continue
                path = f"{pointer}/{key}"
                if child is None:
                    assert path in state["field_status"], path
                else:
                    check(child, path)
        elif isinstance(value, list):
            for i, child in enumerate(value):
                check(child, f"{pointer}/{i}")

    check(state)
    for entry in state["field_status"].values():
        assert all(ref in state["evidence_refs"] for ref in entry["evidence_refs"])
