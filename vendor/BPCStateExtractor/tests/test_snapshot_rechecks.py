"""Fresh-reader invariance checks: changes happen on disk, not in cached indexes."""

from __future__ import annotations

import csv
import re
import shutil
from pathlib import Path

import pytest

from bpc_state import BPCStateExtractor
from bpc_state.repository import CLINICAL_FILES
from conftest import write_csv


def read_rows(path: Path, delimiter: str = ",") -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle, delimiter=delimiter))


@pytest.mark.parametrize(
    "scenario",
    [
        "radiotherapy_completion",
        "future_drug_component",
        "future_drug_end",
        "future_ngs_results",
        "future_biomarker_result",
        "future_imaging",
        "future_tumor_marker",
        "future_med_onc",
        "future_cancer",
    ],
)
def test_fresh_reader_snapshot_unchanged_after_future_file_edit(
    fixture_data: Path, tmp_path: Path, scenario: str
) -> None:
    before = BPCStateExtractor(fixture_data).get_patient_state("P1", 40)
    edited = tmp_path / "edited"
    shutil.copytree(fixture_data, edited)

    if scenario == "radiotherapy_completion":
        name = "ca_radtx_dataset.csv"
        rows = read_rows(edited / name)
        rows[0].update(
            rt_total_dose="9999", rt_fractions="30", rt_rt_int="45", rt_dose="333"
        )
    elif scenario in {"future_drug_component", "future_drug_end"}:
        name = "regimen_cancer_level_dataset.csv"
        rows = read_rows(edited / name)
        if scenario == "future_drug_component":
            rows[0].update(
                drugs_drug_2="CHANGED_FUTURE_DRUG",
                regimen_drugs="DrugA, CHANGED_FUTURE_DRUG",
            )
        else:
            rows[0].update(
                dx_drug_end_int_1="200", drugs_dc_ynu="No", tt_os_g_days="9999"
            )
    elif scenario == "future_ngs_results":
        name = "cancer_panel_test_level_dataset.csv"
        rows = read_rows(edited / name)
        rows[0].update(dob_cpt_report_days="20150", dx_cpt_rep_days="150")
        variants = read_rows(edited / "data_mutations_extended.txt", "\t")
        variants[0].update(HGVSp_Short="p.FUTURE", Hugo_Symbol="CHANGED_FUTURE_GENE")
        write_csv(edited, "data_mutations_extended.txt", variants, "\t")
    elif scenario == "future_biomarker_result":
        name = "pathology_report_level_dataset.csv"
        rows = read_rows(edited / name)
        rows[1].update(mmr_result="CHANGED_FUTURE_MMR", mmr_prepaint="20150")
    elif scenario == "future_cancer":
        name = "cancer_level_dataset_non_index.csv"
        rows = read_rows(edited / name)
        rows[0].update(
            ca_type="CHANGED_FUTURE_CANCER", ca_histology="CHANGED_FUTURE_HISTOLOGY"
        )
    else:
        name, day_field, value_field, identifier = {
            "future_imaging": (
                "imaging_level_dataset.csv",
                "dx_scan_days",
                "image_overall",
                "scan_number",
            ),
            "future_tumor_marker": (
                "tm_level_dataset.csv",
                "dx_tm_days",
                "tm_num_result",
                "tm_number",
            ),
            "future_med_onc": (
                "med_onc_note_level_dataset.csv",
                "dx_md_visit_days",
                "md_ca_status",
                "md_visit_number",
            ),
        }[scenario]
        rows = read_rows(edited / name)
        future = dict(rows[0])
        future.update(
            {
                day_field: "150",
                value_field: "9999" if scenario == "future_tumor_marker" else "FUTURE",
                identifier: "99",
            }
        )
        # Prepending also shifts physical line numbers for all existing facts.
        rows.insert(0, future)
    write_csv(edited, name, rows)

    after = BPCStateExtractor(edited).get_patient_state("P1", 40)
    assert after == before, scenario


@pytest.mark.parametrize("profile", ["mutation", "cna", "sv"])
def test_missing_profile_cannot_leave_positive_results(
    fixture_data: Path, profile: str
) -> None:
    filename, results = {
        "mutation": ("data_mutations_extended.txt", "mutations"),
        "cna": ("data_CNA.txt", "copy_number_alterations"),
        "sv": ("data_sv.txt", "structural_variants"),
    }[profile]
    (fixture_data / filename).rename(fixture_data / (filename + ".absent"))
    state = BPCStateExtractor(fixture_data).get_patient_state("P1", 100)
    report = state["molecular_reports"][0]
    assert report["coverage"][profile] == "unresolved"
    assert report[results] == []
    assert all(
        source["source_file"] != filename
        for evidence in state["evidence_refs"].values()
        for source in evidence["sources"]
    )


@pytest.mark.parametrize("filename", list(CLINICAL_FILES.values()))
def test_each_required_clinical_file_fails_explicitly_when_missing(
    fixture_data: Path, filename: str
) -> None:
    (fixture_data / filename).rename(fixture_data / (filename + ".absent"))
    with pytest.raises(FileNotFoundError, match=re.escape(filename)):
        BPCStateExtractor(fixture_data).get_patient_state("P1", 100)


def test_all_missing_cna_calls_are_not_neutral(fixture_data: Path) -> None:
    write_csv(
        fixture_data,
        "data_CNA.txt",
        [
            {"Hugo_Symbol": "PTEN", "S1": "NA"},
            {"Hugo_Symbol": "TP53", "S1": ""},
        ],
        "\t",
    )
    report = BPCStateExtractor(fixture_data).get_patient_state("P1", 100)[
        "molecular_reports"
    ][0]
    assert report["coverage"]["cna"] == "unresolved"
    assert report["coverage"]["cna_missing_genes"] == ["PTEN", "TP53"]
    assert report["coverage"]["cna_neutral_genes"] == []
    assert report["copy_number_alterations"] == []


def test_same_day_pdl1_tests_remain_independent_and_missing_day_is_excluded(
    fixture_data: Path,
) -> None:
    rows = read_rows(fixture_data / "pathology_report_level_dataset.csv")
    rows[0].update(
        pdl1_yn="Yes",
        pdl1_prepaint="20060",
        pdl1_num="80",
        pdl1_yn_2="Yes",
        pdl1_prepaint_2="20060",
        pdl1_perc_2="0",
        pdl1_tclrange_2="< 1",
        pdl1_yn_3="Yes",
        pdl1_prepaint_3="",
        pdl1_num_3="999",
    )
    write_csv(fixture_data, "pathology_report_level_dataset.csv", rows)
    extractor = BPCStateExtractor(fixture_data)
    assert not extractor.get_patient_state("P1", 59)["pathology"][0]["biomarkers"]
    state = extractor.get_patient_state("P1", 60)
    tests = state["pathology"][0]["biomarkers"]
    assert [item["test_number"] for item in tests] == [1, 2]
    assert tests[0]["details_raw"]["pdl1_num"] == "80"
    assert tests[1]["details_raw"]["pdl1_perc_2"] == "0"
    assert tests[1]["details_raw"]["pdl1_tclrange_2"] == "< 1"
    assert any(
        item["reason"] == "unknown_pdl1_report_day"
        for item in extractor.get_audit_report()["exclusions"]
    )
    assert all(
        "pdl1_num_3" not in source["fields"]
        for evidence in state["evidence_refs"].values()
        for source in evidence["sources"]
    )
