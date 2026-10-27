from __future__ import annotations

import json
from pathlib import Path

import pytest

from bpc_state import BPCStateExtractor, BoundedPatientTool
from conftest import build_fixture


def test_main_cancer_is_derived_not_assumed_zero(fixture_data: Path) -> None:
    state = BPCStateExtractor(fixture_data).get_patient_state("P1", 25)
    assert state["meta"]["target_cancer_id"] == "index:7"
    assert state["cancer_context"]["diagnosis"]["event_day"] == 0
    assert [x["cancer_id"] for x in state["cancer_context"]["known_cancers"]] == ["index:7", "index:0"]
    assert {x["drug_name_raw"] for x in state["treatment_history"]} == {"AmbiguousDrug", "DrugA"}
    assert next(x for x in state["treatment_history"] if x["drug_name_raw"] == "AmbiguousDrug")["cancer_association"] == "ambiguous"


def test_future_regimen_component_does_not_leak_or_change_early_state(tmp_path: Path) -> None:
    first = BPCStateExtractor(build_fixture(tmp_path / "a", "FutureDrugA")).get_patient_state("P1", 20)
    second = BPCStateExtractor(build_fixture(tmp_path / "b", "FutureDrugB")).get_patient_state("P1", 20)
    assert first == second
    assert "FutureDrugA" not in json.dumps(first)
    assert "FutureDrugB" not in json.dumps(second)


def test_treatment_end_is_not_revealed_early_and_masked_end_is_never_trusted(fixture_data: Path) -> None:
    extractor = BPCStateExtractor(fixture_data)
    early = extractor.get_patient_state("P1", 40)
    drug_a = next(x for x in early["treatment_history"] if x["drug_name_raw"] == "DrugA")
    assert drug_a["status_at_cutoff"] == "unknown"
    assert drug_a["end_day"] is None
    early_fields = early["evidence_refs"][drug_a["evidence_refs"][0]]["sources"][0]["fields"]
    assert "dx_drug_end_int_1" not in early_fields
    late = extractor.get_patient_state("P1", 100)
    drug_a = next(x for x in late["treatment_history"] if x["drug_name_raw"] == "DrugA")
    assert drug_a["status_at_cutoff"] == "ended" and drug_a["end_day"] == 70
    masked = next(x for x in late["treatment_history"] if x["drug_name_raw"] == "Investigational Drug")
    assert masked["status_at_cutoff"] == "unknown"
    assert masked["duration_masked"] is True
    assert masked["end_day"] is None


def test_ngs_enters_only_on_report_day_and_preserves_raw_tmb_and_cna(fixture_data: Path) -> None:
    extractor = BPCStateExtractor(fixture_data)
    assert extractor.get_patient_state("P1", 79)["molecular_reports"] == []
    report = extractor.get_patient_state("P1", 80)["molecular_reports"][0]
    assert report["collection_day"] == 10 and report["available_day"] == 80
    assert report["mutations"][0]["gene"] == "TP53"
    assert {x["gene"]: x["value_raw"] for x in report["copy_number_alterations"]} == {"PTEN": "-2", "FRACTION": "-1.5"}
    assert report["tmb"]["value_raw"] == "0.000004"
    assert report["tmb"]["unit"] == "unconfirmed_source_unit"


def test_pathology_addendum_biomarker_uses_own_report_day(fixture_data: Path) -> None:
    extractor = BPCStateExtractor(fixture_data)
    early = extractor.get_patient_state("P1", 50)
    assert len(early["pathology"]) == 2
    assert all(not x["biomarkers"] for x in early["pathology"])
    late = extractor.get_patient_state("P1", 60)
    biomarkers = [b for p in late["pathology"] for b in p["biomarkers"]]
    assert len(biomarkers) == 1
    assert biomarkers[0]["biomarker"] == "MMR"
    assert biomarkers[0]["result_raw"] == "Proficient"
    assert biomarkers[0]["available_day"] == 60


def test_negative_days_and_same_day_conflicts_are_preserved(fixture_data: Path) -> None:
    extractor = BPCStateExtractor(fixture_data)
    assert extractor.get_patient_state("P1", 39)["radiotherapy_history"] == []
    state = extractor.get_patient_state("P1", 40)
    assert "duration_days_raw" not in state["radiotherapy_history"][0]
    assert state["observations"]["imaging"][0]["event_day"] == -5
    assert {x["assessment"] for x in state["current_summary"]["latest_imaging"]} == {"stable", "progressing"}


def test_evidence_is_whitelisted_and_bound(fixture_data: Path) -> None:
    extractor = BPCStateExtractor(fixture_data)
    tool = BoundedPatientTool(extractor, "P1", 40)
    state = tool.get_patient_state(40)
    ref = state["observations"]["tumor_markers"][0]["evidence_refs"][0]
    resolved = tool.get_evidence(40, [ref])[ref]
    fields = resolved["sources"][0]["fields"]
    assert "tm_num_result" in fields
    assert "tt_os_dx_days" not in fields
    with pytest.raises(PermissionError):
        tool.get_patient_state(41)
    with pytest.raises(KeyError):
        tool.get_evidence(20, ["E9999"])


def test_audit_is_separate_from_agent_state(fixture_data: Path) -> None:
    extractor = BPCStateExtractor(fixture_data)
    state = extractor.get_patient_state("P1", 20)
    assert "audit" not in state
    audit = extractor.get_audit_report()
    assert any(x["reason"] == "ngs_report_after_cutoff" for x in audit["exclusions"])
