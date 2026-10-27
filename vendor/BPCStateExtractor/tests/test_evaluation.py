from __future__ import annotations

import json
from pathlib import Path

import pytest

from bpc_state import BPCStateExtractor, BoundedPatientTool
from bpc_state.cli import build_parser, main
from bpc_state.evaluation import OfflineEvaluationExporter, state_sha256
from bpc_state.models import EvaluationData, PatientState
from bpc_state.tool_schema import evidence_tool_schema, state_tool_schema


def test_evaluation_window_is_exclusive_of_input_and_inclusive_of_end(
    fixture_data: Path,
) -> None:
    result = OfflineEvaluationExporter(fixture_data).export("P1", 20, 40)
    assert result["meta"]["purpose"] == "offline_evaluation"
    assert [i["event_day"] for i in result["observations"]["imaging"]] == [40, 40]
    assert [i["event_day"] for i in result["observations"]["med_onc"]] == [35]
    assert [i["event_day"] for i in result["observations"]["tumor_markers"]] == [40]
    assert result["late_available_observations"]["imaging"] == []
    assert "current_summary" not in result and "demographics" not in result
    assert "molecular_reports" not in result and "treatment_history" not in result
    state = BPCStateExtractor(fixture_data).get_patient_state("P1", 20)
    assert result["meta"]["input_state_sha256"] == state_sha256(state)
    EvaluationData.model_validate(result)
    with pytest.raises(ValueError):
        PatientState.model_validate(result)
    with pytest.raises(ValueError):
        EvaluationData.model_validate(state)


def test_empty_window_means_no_admitted_records_not_no_progression(
    fixture_data: Path,
) -> None:
    result = OfflineEvaluationExporter(fixture_data).export("P1", 40, 45)
    assert all(
        not result["observations"][kind]
        for kind in ["imaging", "med_onc", "tumor_markers"]
    )
    assert set(result["window_status"].values()) == {"none_recorded_in_window"}
    assert result["evidence_refs"] == {}
    assert "stable" not in json.dumps(result)


def test_export_cannot_modify_input_or_bypass_agent_boundary(
    fixture_data: Path,
) -> None:
    extractor = BPCStateExtractor(fixture_data)
    tool = BoundedPatientTool(extractor, "P1", 20)
    before = tool.get_patient_state(20)
    result = OfflineEvaluationExporter(fixture_data).export("P1", 20, 100)
    assert tool.get_patient_state(20) == before
    assert not hasattr(tool, "export_evaluation")
    assert not hasattr(tool, "evaluation")
    for ref in result["evidence_refs"]:
        with pytest.raises(KeyError):
            tool.get_evidence(20, [ref])
    with pytest.raises(PermissionError):
        tool.get_patient_state(100)
    assert all(
        "evaluation" not in schema["function"]["name"]
        for schema in [state_tool_schema("P1", 20), evidence_tool_schema("P1", 20)]
    )


def test_delayed_results_are_not_future_event_labels(
    fixture_data: Path, tmp_path: Path
) -> None:
    exporter = OfflineEvaluationExporter(fixture_data)
    config = exporter._extractor.rules
    config["time_rules"]["imaging"]["reporting_delay_days"] = 10
    result = exporter.export("P1", 45, 50)
    assert result["observations"]["imaging"] == []
    delayed = result["late_available_observations"]["imaging"]
    assert len(delayed) == 2
    assert all(
        item["event_day"] == 40 and item["available_day"] == 50 for item in delayed
    )
    assert result["window_status"]["imaging"] == "none_recorded_in_window"


def test_evaluation_evidence_contains_only_selected_followup(
    fixture_data: Path,
) -> None:
    result = OfflineEvaluationExporter(fixture_data).export("P1", 20, 40)
    used = {
        ref
        for items in result["observations"].values()
        for item in items
        for ref in item["evidence_refs"]
    }
    assert set(result["evidence_refs"]) == used
    allowed = {
        "imaging_level_dataset.csv",
        "med_onc_note_level_dataset.csv",
        "tm_level_dataset.csv",
    }
    assert {
        source["source_file"]
        for item in result["evidence_refs"].values()
        for source in item["sources"]
    } <= allowed
    for status in result["field_status"].values():
        assert set(status["evidence_refs"]) <= used


@pytest.mark.parametrize(
    "cutoff,end,error",
    [
        (20, 20, ValueError),
        (20, 19, ValueError),
        (True, 40, TypeError),
        (20, 40.5, TypeError),
    ],
)
def test_invalid_evaluation_window_is_rejected(
    fixture_data: Path, cutoff, end, error
) -> None:
    with pytest.raises(error):
        OfflineEvaluationExporter(fixture_data).export("P1", cutoff, end)


def test_evaluation_cli_requires_and_writes_a_separate_file(
    fixture_data: Path, tmp_path: Path
) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "evaluation",
                "--patient-id",
                "P1",
                "--cutoff-day",
                "20",
                "--end-day",
                "40",
            ]
        )
    target = tmp_path / "evaluation.json"
    code = main(
        [
            "--data-dir",
            str(fixture_data),
            "evaluation",
            "--patient-id",
            "P1",
            "--cutoff-day",
            "20",
            "--end-day",
            "40",
            "--output",
            str(target),
        ]
    )
    assert code == 0
    EvaluationData.model_validate(json.loads(target.read_text()))
    assert main(["evaluation-schema", "--output", str(tmp_path / "schema.json")]) == 0
