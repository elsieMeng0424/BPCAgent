from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bpc_state import BPCStateExtractor, BoundedPatientTool
from bpc_state.rules import integer, number
from bpc_state.rules import RuleSet
from bpc_state.tool_schema import evidence_tool_schema
from conftest import write_csv


def raw_row(extractor: BPCStateExtractor, table: str) -> dict[str, str]:
    return extractor.repository.clinical_rows(table, "Prostate", "P1")[0]


def test_radiotherapy_completion_fields_never_enter_start_snapshot(
    fixture_data: Path,
) -> None:
    extractor = BPCStateExtractor(fixture_data)
    before = extractor.get_patient_state("P1", 40)
    row = raw_row(extractor, "radiotherapy")
    row.update(rt_rt_int="45", rt_fractions="30", rt_total_dose="6000", rt_dose="200")
    assert extractor.get_patient_state("P1", 40) == before
    late = extractor.get_patient_state("P1", 100)
    assert "dose_raw" not in late["radiotherapy_history"][0]
    assert "fractions_raw" not in late["radiotherapy_history"][0]
    assert all(
        "rt_total_dose" not in source["fields"]
        for e in late["evidence_refs"].values()
        for source in e["sources"]
    )
    assert any(
        e["reason"] == "radiotherapy_summary_availability_unknown"
        for e in extractor.get_audit_report()["exclusions"]
    )


def test_evidence_refs_stay_bound_to_same_fact_across_cutoffs(
    fixture_data: Path,
) -> None:
    extractor = BPCStateExtractor(fixture_data)
    early = extractor.get_patient_state("P1", 20)
    later = extractor.get_patient_state("P1", 100)
    ref = early["observations"]["tumor_markers"][0]["evidence_refs"][0]
    assert re.fullmatch(r"E-[0-9a-f]{24}", ref)
    assert early["evidence_refs"][ref] == later["evidence_refs"][ref]
    assert extractor.get_evidence("P1", 100, [ref])[ref] == early["evidence_refs"][ref]
    assert early["evidence_refs"][ref]["sources"][0]["values"]["tm_num_result"] == "10"
    report_ref = later["molecular_reports"][0]["evidence_refs"][0]
    with pytest.raises(KeyError):
        extractor.get_evidence("P1", 20, [report_ref])


def test_known_treatment_start_ref_survives_later_end(fixture_data: Path) -> None:
    extractor = BPCStateExtractor(fixture_data)
    early = extractor.get_patient_state("P1", 20)
    later = extractor.get_patient_state("P1", 100)
    a = next(t for t in early["treatment_history"] if t["drug_name_raw"] == "DrugA")
    b = next(t for t in later["treatment_history"] if t["drug_name_raw"] == "DrugA")
    assert a["evidence_refs"][0] == b["evidence_refs"][0]
    assert len(b["evidence_refs"]) == 2


def test_future_row_insertion_does_not_change_earlier_state(fixture_data: Path) -> None:
    extractor = BPCStateExtractor(fixture_data)
    before = extractor.get_patient_state("P1", 40)
    raw_row(extractor, "imaging")
    group = extractor.repository._clinical["imaging"][("Prostate", "P1")]
    for row in group:
        row["__row_number"] = str(int(row["__row_number"]) + 1)
    group.insert(
        0,
        {
            "cohort": "Prostate",
            "record_id": "P1",
            "scan_number": "9",
            "dx_scan_days": "150",
            "image_overall": "Progressing",
            "__row_number": "2",
        },
    )
    assert extractor.get_patient_state("P1", 40) == before
    # Moving a patient row must not change demographic provenance either.
    extractor.repository.find_patient("P1")[1]["__row_number"] = "200"
    assert extractor.get_patient_state("P1", 40) == before


def test_masked_duration_does_not_depend_on_future_end_field(
    fixture_data: Path,
) -> None:
    extractor = BPCStateExtractor(fixture_data)
    before = extractor.get_patient_state("P1", 40)
    raw_row(extractor, "regimens")["dx_drug_end_int_3"] = ""
    assert extractor.get_patient_state("P1", 40) == before
    masked = next(
        t
        for t in before["treatment_history"]
        if t["drug_name_raw"] == "Investigational Drug"
    )
    assert masked["duration_masked"]


@pytest.mark.parametrize(
    "filename,profile,issue",
    [
        ("data_mutations_extended.txt", "mutation", "mutation_file_missing"),
        ("data_CNA.txt", "cna", "cna_file_missing"),
        ("data_sv.txt", "sv", "sv_file_missing"),
    ],
)
def test_missing_profile_files_are_unresolved(
    fixture_data: Path, filename: str, profile: str, issue: str
) -> None:
    (fixture_data / filename).rename(fixture_data / (filename + ".hidden"))
    report = BPCStateExtractor(fixture_data).get_patient_state("P1", 100)[
        "molecular_reports"
    ][0]
    assert report["coverage"][profile] == "unresolved"
    assert issue in report["coverage"]["issues"]


def test_missing_cna_column_not_a_negative_assay(fixture_data: Path) -> None:
    write_csv(
        fixture_data,
        "data_CNA.txt",
        [{"Hugo_Symbol": "PTEN", "OTHER_SAMPLE": "0"}],
        "\t",
    )
    report = BPCStateExtractor(fixture_data).get_patient_state("P1", 100)[
        "molecular_reports"
    ][0]
    assert report["coverage"]["cna"] == "unresolved"
    assert "cna_sample_column_missing" in report["coverage"]["issues"]
    assert report["copy_number_alterations"] == []


def test_cna_missing_neutral_and_fractional_calls_are_separate(
    fixture_data: Path,
) -> None:
    report = BPCStateExtractor(fixture_data).get_patient_state("P1", 100)[
        "molecular_reports"
    ][0]
    assert report["coverage"]["cna_missing_genes"] == ["UNKNOWN"]
    assert report["coverage"]["cna_neutral_genes"] == ["TP53"]
    assert {
        c["gene"]: c["value_numeric"] for c in report["copy_number_alterations"]
    } == {"PTEN": -2.0, "FRACTION": -1.5}


def test_zero_mutation_requires_supplied_profile_and_sample_membership(
    fixture_data: Path,
) -> None:
    write_csv(
        fixture_data,
        "data_mutations_extended.txt",
        [{"Tumor_Sample_Barcode": "S2", "Hugo_Symbol": "TP53"}],
        "\t",
    )
    extractor = BPCStateExtractor(fixture_data)
    report = extractor.get_patient_state("P1", 100)["molecular_reports"][0]
    assert report["coverage"]["mutation"] == "unresolved"
    # Membership is supplied through the existing in-memory case-list index.
    extractor.repository._case_lists["sequenced"] = {"S1"}
    report = extractor.get_patient_state("P1", 100)["molecular_reports"][0]
    assert report["coverage"]["mutation"] == "profiled"
    assert report["mutations"] == []


def test_missing_required_clinical_file_fails_explicitly(fixture_data: Path) -> None:
    (fixture_data / "imaging_level_dataset.csv").rename(fixture_data / "imaging.hidden")
    with pytest.raises(FileNotFoundError, match="imaging_level_dataset"):
        BPCStateExtractor(fixture_data).get_patient_state("P1", 40)


def test_pdl1_quantitative_results_use_individual_report_day(
    fixture_data: Path,
) -> None:
    extractor = BPCStateExtractor(fixture_data)
    row = raw_row(extractor, "pathology")
    row.update(
        pdl1_yn="Yes",
        pdl1_prepaint="20060",
        pdl1_sum="",
        pdl1_num="80",
        pdl1_perc="0",
        pdl1_tclrange="< 1",
    )
    assert not extractor.get_patient_state("P1", 59)["pathology"][0]["biomarkers"]
    biomarker = extractor.get_patient_state("P1", 60)["pathology"][0]["biomarkers"][0]
    assert biomarker["details_raw"]["pdl1_num"] == "80"
    assert biomarker["details_raw"]["pdl1_perc"] == "0"
    assert biomarker["details_raw"]["pdl1_tclrange"] == "< 1"


def test_no_reported_testing_is_not_negative_or_missing(fixture_data: Path) -> None:
    state = BPCStateExtractor(fixture_data).get_patient_state("P1", 50)
    path = state["pathology"][0]
    assert path["biomarkers"] == []
    item = path["biomarker_testing"][0]
    assert item["reported_raw"] == "No"
    assert item["meaning"] == "not_reported_on_pathology_report"


def test_whitelist_removes_values_evidence_and_summary(fixture_data: Path) -> None:
    extractor = BPCStateExtractor(fixture_data)
    extractor.rules["demographics"] = []
    extractor.rules["imaging"].remove("image_overall")
    extractor.rules["tumor_marker"].remove("tm_num_result")
    state = extractor.get_patient_state("P1", 40)
    assert state["demographics"]["sex_raw"] is None
    assert all(i["assessment_raw"] is None for i in state["observations"]["imaging"])
    assert all(
        i["assessment"] is None for i in state["current_summary"]["latest_imaging"]
    )
    assert all(
        i["value_numeric"] is None
        for i in state["current_summary"]["latest_tumor_markers"]
    )
    for evidence in state["evidence_refs"].values():
        for source in evidence["sources"]:
            assert "image_overall" not in source["values"]
            assert "tm_num_result" not in source["values"]


def test_time_rules_and_normalization_are_executable(fixture_data: Path) -> None:
    extractor = BPCStateExtractor(fixture_data)
    extractor.rules["time_rules"]["imaging"]["reporting_delay_days"] = 10
    extractor.rules["normalization_rules"]["imaging_assessment"]["Stable"] = "other"
    # Remove the earlier matching entry to make the custom mapping decisive.
    del extractor.rules["normalization_rules"]["imaging_assessment"]["stable|no change"]
    assert len(extractor.get_patient_state("P1", 40)["observations"]["imaging"]) == 1
    images = extractor.get_patient_state("P1", 50)["observations"]["imaging"]
    assert images[1]["event_day"] == 40 and images[1]["available_day"] == 50
    assert images[1]["assessment"] == "other"


def test_ngs_relative_report_date_fallback(fixture_data: Path) -> None:
    extractor = BPCStateExtractor(fixture_data)
    raw_row(extractor, "cpt")["dob_cpt_report_days"] = ""
    assert extractor.get_patient_state("P1", 79)["molecular_reports"] == []
    assert (
        extractor.get_patient_state("P1", 80)["molecular_reports"][0]["available_day"]
        == 80
    )


def test_ambiguous_ngs_cancer_mapping_is_retained(fixture_data: Path) -> None:
    extractor = BPCStateExtractor(fixture_data)
    raw_row(extractor, "cpt")["cpt_n_ca_seq"] = "2"
    report = extractor.get_patient_state("P1", 100)["molecular_reports"][0]
    assert report["cancer_association"] == "ambiguous"
    assert report["cancer_id"] is None


def test_future_ngs_association_does_not_reclassify_existing_report(
    fixture_data: Path,
) -> None:
    extractor = BPCStateExtractor(fixture_data)
    before = extractor.get_patient_state("P1", 100)
    future = dict(raw_row(extractor, "cpt"))
    future.update(
        ca_seq="0", dob_cpt_report_days="20150", dx_cpt_rep_days="130", __row_number="3"
    )
    extractor.repository._clinical["cpt"][("Prostate", "P1")].append(future)
    assert extractor.get_patient_state("P1", 100) == before


def test_bound_tool_schema_and_validation(fixture_data: Path) -> None:
    tool = BoundedPatientTool(BPCStateExtractor(fixture_data), "P1", 40)
    for invalid in [True, 20.5, "20"]:
        with pytest.raises(TypeError):
            tool.get_patient_state(invalid)
    with pytest.raises(PermissionError):
        tool.get_evidence(41, [])
    schema = evidence_tool_schema("P1", 40)
    assert (
        schema["function"]["parameters"]["properties"]["evidence_refs"]["items"][
            "pattern"
        ]
        == "^E-[0-9a-f]{24}$"
    )


def test_numeric_validation_does_not_round_dates_or_emit_nonfinite_values() -> None:
    assert integer("40.5") is None
    assert number("inf") is None
    assert number("0") == 0


def test_custom_rules_file_is_loaded_and_validated(
    fixture_data: Path, tmp_path: Path
) -> None:
    rules = RuleSet().config
    rules["imaging"].remove("image_overall")
    rules["time_rules"]["imaging"]["reporting_delay_days"] = 10
    rules_path = tmp_path / "custom-rules.json"
    rules_path.write_text(json.dumps(rules), encoding="utf-8")
    state = BPCStateExtractor(fixture_data, rules_path).get_patient_state("P1", 40)
    assert len(state["observations"]["imaging"]) == 1
    assert state["observations"]["imaging"][0]["assessment_raw"] is None
    rules["time_rules"]["imaging"]["reporting_delay_days"] = -1
    rules_path.write_text(json.dumps(rules), encoding="utf-8")
    with pytest.raises(ValueError, match="nonnegative"):
        BPCStateExtractor(fixture_data, rules_path)
