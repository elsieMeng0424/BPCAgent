"""Optional integration checks against the local public release (read-only)."""

from __future__ import annotations

import csv
import os
from pathlib import Path

import pytest

from bpc_state import BPCStateExtractor, BoundedPatientTool
from bpc_state.repository import is_present


def rows(path: Path, delimiter: str = ",") -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle, delimiter=delimiter))


@pytest.fixture(scope="module")
def release() -> tuple[Path, BPCStateExtractor]:
    path = Path(
        os.environ.get("BPC_DATA_DIR", Path(__file__).resolve().parents[1] / "dataset")
    )
    if not (path / "manifest.csv").exists():
        pytest.skip("Local public BPC release not supplied; set BPC_DATA_DIR")
    return path, BPCStateExtractor(path)


def test_real_evidence_remains_stable_and_future_refs_are_denied(release) -> None:
    _, extractor = release
    pid = "GENIE-DFCI-051542"
    early = extractor.get_patient_state(pid, 50)
    later = extractor.get_patient_state(pid, 220)
    ref = early["observations"]["tumor_markers"][0]["evidence_refs"][0]
    for cutoff in [50, 220, 260]:
        assert (
            extractor.get_evidence(pid, cutoff, [ref])[ref]
            == early["evidence_refs"][ref]
        )
    assert early["evidence_refs"].items() <= later["evidence_refs"].items()
    future = extractor.get_patient_state(pid, 249)["molecular_reports"][0][
        "evidence_refs"
    ][0]
    with pytest.raises(KeyError):
        extractor.get_evidence(pid, 248, [future])
    tool = BoundedPatientTool(extractor, pid, 220)
    with pytest.raises(PermissionError):
        tool.get_evidence(249, [future])


def test_real_radiotherapy_and_ngs_exact_day_boundaries(release) -> None:
    _, extractor = release
    pid = "GENIE-DFCI-051542"
    assert extractor.get_patient_state(pid, 214)["radiotherapy_history"] == []
    treatment = extractor.get_patient_state(pid, 215)["radiotherapy_history"][0]
    assert treatment["event_day"] == treatment["available_day"] == 215
    assert not {"dose_raw", "fractions_raw", "duration_days_raw"}.intersection(
        treatment
    )
    assert extractor.get_patient_state(pid, 248)["molecular_reports"] == []
    report = extractor.get_patient_state(pid, 249)["molecular_reports"][0]
    assert report["sample_id"] == "GENIE-DFCI-051542-250158"
    assert report["collection_day"] == 214 and report["available_day"] == 249


def test_all_declared_cna_samples_without_columns_are_unresolved(release) -> None:
    path, extractor = release
    with (path / "data_CNA.txt").open(encoding="utf-8-sig") as handle:
        sample_columns = set(next(csv.reader(handle, delimiter="\t"))[1:])
    missing = {
        row["SAMPLE_ID"]
        for row in rows(path / "data_gene_matrix.txt", "\t")
        if is_present(row.get("cna")) and row["SAMPLE_ID"] not in sample_columns
    }
    reports = rows(path / "cancer_panel_test_level_dataset.csv")
    origins = {
        row["record_id"]: int(row["dob_ca_dx_days"])
        for row in rows(path / "cancer_level_dataset_index.csv")
    }
    assert len(missing) == 102  # This release's known discrepancy, not a code policy.
    for row in reports:
        if row["cpt_genie_sample_id"] not in missing:
            continue
        cutoff = int(row["dob_cpt_report_days"]) - origins[row["record_id"]]
        state = extractor.get_patient_state(row["record_id"], cutoff)
        report = next(
            item
            for item in state["molecular_reports"]
            if item["sample_id"] == row["cpt_genie_sample_id"]
        )
        assert report["coverage"]["cna"] == "unresolved", report["sample_id"]
        assert "cna_sample_column_missing" in report["coverage"]["issues"]
        assert report["copy_number_alterations"] == []


def test_real_pdl1_quantitative_results_and_report_delay(release) -> None:
    path, extractor = release
    origins = {
        row["record_id"]: int(row["dob_ca_dx_days"])
        for row in rows(path / "cancer_level_dataset_index.csv")
    }
    checked = 0
    for row in rows(path / "pathology_report_level_dataset.csv"):
        if row["pdl1_yn"] != "Yes" or not row["pdl1_prepaint"]:
            continue
        quantitative = {
            key: row[key]
            for key in [
                "pdl1_num",
                "pdl1_perc",
                "pdl1_tclrange",
                "pdl1_tcurange",
                "pdl1_icperc",
            ]
            if is_present(row[key])
        }
        if not quantitative:
            continue
        report_day = int(row["pdl1_prepaint"]) - origins[row["record_id"]]
        state = extractor.get_patient_state(row["record_id"], max(0, report_day))
        pathology_id = f"path:{row['path_proc_number']}:{row['path_rep_number']}"
        pathology = next(
            item for item in state["pathology"] if item["pathology_id"] == pathology_id
        )
        result = next(
            item
            for item in pathology["biomarkers"]
            if item["biomarker"] == "PD-L1" and item["test_number"] == 1
        )
        assert result["available_day"] == report_day
        assert all(
            result["details_raw"][key] == raw for key, raw in quantitative.items()
        )
        if report_day > 0:
            earlier = extractor.get_patient_state(row["record_id"], report_day - 1)
            assert not any(
                item["biomarker"] == "PD-L1" and item["test_number"] == 1
                for report in earlier["pathology"]
                if report["pathology_id"] == pathology_id
                for item in report["biomarkers"]
            )
        checked += 1
    assert checked >= 3
