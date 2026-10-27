"""Test the packaged extraction/normalization workflow with real shell calls."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bpcagent import Config, LLMResponse, SandboxConfig, SandboxError, ToolCall, UnitPatientStateAgent
from bpcagent.agents import UnitPatientStateAgent as ExportedAgent
from bpcagent.agents.exceptions import AgentException
from bpcagent.agents.unit_rules import EVIDENCE_OBSERVATION_FIELDS, UNIT_SCHEMA_VERSION, UNIT_RULES_VERSION
from bpcagent.tools.bpc_patient import PatientArtifactStore, PatientEvidenceTool, PatientStateTool


def tool_response(name, arguments, call_id):
    return LLMResponse(tool_calls=[ToolCall(
        id=call_id, name=name, arguments=json.dumps(arguments),
    )], finish_reason="tool_calls")


@unittest.skipUnless(sys.platform == "darwin" or sys.platform.startswith("linux"), "POSIX shell required")
class UnitPatientStateAgentTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        previous = Path.cwd()
        os.chdir(temp.name)
        self.addCleanup(os.chdir, previous)
        self.work_dir = Path(temp.name) / "workspace"
        self.state = {
            "meta": {"patient_id": "P1", "cutoff_day": 220, "cohort": "TEST", "target_cancer_id": "C1"},
            "evidence_refs": {"E-test": {}},
            "observations": {"tumor_markers": [
                {"tumor_marker_id": "M1", "analyte_raw": "PSA", "unit_raw": "ug/L", "value_raw": "0"},
                {"tumor_marker_id": "M2", "analyte_raw": "Total Testosterone", "unit_raw": "nmol/L", "value_raw": "1"},
                {"tumor_marker_id": "M3", "analyte_raw": "PSA", "unit_raw": "nmol/L", "value_raw": "2"},
            ]},
        }
        for marker in self.state["observations"]["tumor_markers"]:
            marker.update(event_day=200, available_day=200, evidence_refs=["E-test"])
        bound = SimpleNamespace(
            patient_id="P1", cancer_id=None, max_cutoff_day=220,
            get_patient_state=Mock(return_value=self.state),
            get_evidence=Mock(return_value={"E-test": {"sources": []}}),
        )
        store = PatientArtifactStore(self.work_dir)
        tools = (PatientStateTool(bound, store), PatientEvidenceTool(bound, store))
        self.llm = Mock()
        with patch("bpcagent.agents.unit_patient_state.create_patient_tools", return_value=tools):
            self.agent = UnitPatientStateAgent(
                patient_id="P1", cutoff_day=220, data_dir="unused", work_dir=self.work_dir,
                config=Config(max_tool_iterations=4), llm=self.llm,
            )
        self.extraction_registry = self.agent.tool_registry
        self.extraction = [
            tool_response("get_patient_state", {"cutoff_day": 220}, "state"),
            tool_response("get_patient_evidence", {"cutoff_day": 220, "evidence_refs": ["E-test"]}, "evidence"),
        ]

    def valid_output(self):
        markers = deepcopy(self.state["observations"]["tumor_markers"])
        norms = [
            dict(status="normalized", value=0, unit="ng/mL", factor=1, reason=None),
            dict(status="normalized", value=28.842, unit="ng/dL", factor=28.842, reason=None),
            dict(status="unresolved", value=None, unit=None, factor=None, reason="PSA molar unit needs confirmation."),
        ]
        for i, marker in enumerate(markers):
            marker["normalization"] = norms[i]
            marker["unit_assessment"] = self.assessment(
                marker=marker,
                status="suspected_error" if i == 2 else "accepted",
                kind="total_testosterone" if i == 1 else "psa",
                unit=None if i == 2 else marker["unit_raw"],
            )
        return dict(schema_version=UNIT_SCHEMA_VERSION, rules_version=UNIT_RULES_VERSION,
                    source_state="patient_state_day_220_001.json", meta=deepcopy(self.state["meta"]), tumor_markers=markers)

    @staticmethod
    def observation(marker):
        return {key: deepcopy(marker.get(key)) for key in EVIDENCE_OBSERVATION_FIELDS}

    def assessment(self, *, marker=None, status="accepted", kind="psa", unit="ug/L", correction_id=None):
        if marker is None:
            marker = self.state["observations"]["tumor_markers"][0]
        return dict(status=status, analyte_type=kind, effective_unit=unit, candidate_unit=None,
                    correction_id=correction_id, supporting_evidence_refs=list(marker["evidence_refs"]),
                    evidence_observations=[self.observation(marker)],
                    conflicting_evidence_refs=[], reason="Synthetic source review; no contradictory evidence.")

    def shell_response(self, *, incorrect=False, tamper=False, output=None):
        out = self.valid_output() if output is None else output
        if incorrect:
            out["tumor_markers"][1]["normalization"]["value"] = 999
        code = (
            "import json; from pathlib import Path; "
            "p=Path('patient_state_day_220_001.json'); "
            f"out={out!r}; "
            "target=Path('tumor_markers_standardized.json'); "
            "target.write_text(json.dumps(out)); json.loads(target.read_text())"
        )
        if tamper:
            code += "; p.write_text('{}')"
        command = "cat > standardize_tm.py <<'PY'\n" + code + "\nPY\n"
        response = tool_response("run_shell", {"command": command}, "shell-write")
        response.tool_calls.append(ToolCall(id="shell-execute", name="run_shell", arguments=json.dumps({
            "command": shlex.quote(sys.executable) + " " + shlex.quote(str(self.agent.work_dir / "standardize_tm.py")),
        })))
        return response

    def validate_fixture(self, output):
        source = self.work_dir / "patient_state_day_220_001.json"
        source.write_text(json.dumps(self.state))
        snapshot = self.agent._prepare_unit_inputs(self.state)
        originals = {p: p.read_bytes() for p in (source, snapshot)}
        target = self.work_dir / "tumor_markers_standardized.json"
        target.write_text(json.dumps(output))
        (self.work_dir / "standardize_tm.py").write_text("# Synthetic saved script")
        self.agent._executed_script_sha256 = hashlib.sha256(b"# Synthetic saved script").hexdigest()
        messages = [{"role": "tool", "content": json.dumps(dict(tool_name="run_shell", status="success",
                                                               data=dict(exit_code=0, timed_out=False)))}]
        self.agent._validate_output(target, source, self.state, originals, messages)

    def correction(self, marker_index=2, **overrides):
        marker = self.state["observations"]["tumor_markers"][marker_index]
        entry = {key: marker[key] for key in ("tumor_marker_id", "event_day", "analyte_raw", "value_raw", "unit_raw")}
        entry.update({key: self.state["meta"][key] for key in ("patient_id", "cohort", "target_cancer_id")})
        entry.update(id="fix-1", corrected_unit="ug/L", analyte_type="psa", available_day=210,
                     source="Synthetic verified source", source_version="v1")
        entry.update(overrides)
        return entry

    def set_corrections(self, *entries):
        path = self.work_dir.parent / "trusted_corrections.json"
        path.write_text(json.dumps(dict(schema_version="1.0", version="synthetic-v1", corrections=list(entries))))
        self.agent.unit_corrections_path = path
        self.agent._correction_document = self.agent._load_unit_corrections()

    def report(self):
        return LLMResponse(content="Normalized: 2. Unresolved: 1. Output: tumor_markers_standardized.json.", finish_reason="stop")

    def test_public_agent_runs_both_stages_and_saves_final_log(self):
        self.assertIs(ExportedAgent, UnitPatientStateAgent)
        self.llm.invoke.side_effect = self.extraction + [self.shell_response(), self.report()]
        result = self.agent.run()
        self.assertTrue(result.finished)
        self.assertEqual(result.info["validation_status"], "passed")
        self.assertEqual(result.info["unit_summary"]["normalized"], 2)
        self.assertEqual(result.info["unit_summary"]["unresolved"], 1)
        self.assertEqual(result.info["unit_summary"]["assessments"]["suspected_error"], 1)
        self.assertEqual(self.llm.invoke.call_count, 4)
        self.assertIs(self.agent.tool_registry, self.extraction_registry)
        self.assertEqual(json.loads((self.work_dir / "patient_state_day_220_001.json").read_text()), self.state)
        self.assertFalse((self.work_dir / "answer.json").exists())
        log = json.loads(self.agent.log_path.read_text())
        self.assertTrue(self.agent.log_path.name.startswith("UnitPatientStateAgent_"))
        self.assertEqual(self.agent.log_path.parent, self.work_dir / "logs")
        self.assertTrue(log["finished"])
        self.assertEqual(log["validation_attempts"], [{"turn": 2, "status": "passed"}])
        processing = self.llm.invoke.call_args_list[2]
        self.assertEqual([tool["function"]["name"] for tool in processing.kwargs["tools"]], ["run_shell"])
        self.assertIn("Use English", processing.args[0][0]["content"])

    def test_private_extraction_stops_after_tools_and_preserves_reasoning(self):
        self.extraction[0].reasoning_content = "Extract first"
        self.extraction[1].reasoning_content = "Resolve evidence"
        self.llm.invoke.side_effect = self.extraction
        messages = []
        self.agent._extract_patient(messages, 3)
        self.assertEqual(self.llm.invoke.call_count, 2)
        self.assertEqual(messages[-1]["role"], "tool")
        self.assertEqual([m["reasoning_content"] for m in messages if m["role"] == "assistant"],
                         ["Extract first", "Resolve evidence"])
        self.assertEqual(len(list(self.work_dir.glob("patient_*.json"))), 2)

    def test_private_extraction_does_not_reuse_previous_success(self):
        self.llm.invoke.side_effect = self.extraction
        self.agent._extract_patient([], 3)
        self.llm.invoke.side_effect = [LLMResponse(content="done", finish_reason="stop")]
        with self.assertRaises(AgentException) as caught:
            self.agent._extract_patient([], 3)
        self.assertEqual(caught.exception.code, "incomplete_extraction")

    def test_private_extraction_wrong_date_is_not_completion(self):
        self.llm.invoke.side_effect = [
            tool_response("get_patient_state", {"cutoff_day": 219}, "state"),
            tool_response("get_patient_evidence", {"cutoff_day": 219, "evidence_refs": ["E-test"]}, "evidence"),
        ]
        with self.assertRaises(AgentException) as caught:
            self.agent._extract_patient([], 2)
        self.assertEqual(caught.exception.code, "step_limit_exceeded")

    def test_bad_conversion_is_retried_and_corrected(self):
        self.llm.invoke.side_effect = self.extraction + [
            self.shell_response(incorrect=True), self.report(), self.shell_response(), self.report(),
        ]
        result = self.agent.run()
        self.assertTrue(result.finished)
        self.assertEqual([item["status"] for item in self.agent.validation_attempts], ["failed", "passed"])
        self.assertIn("incorrect conversion", self.agent.validation_attempts[0]["error"])
        feedback = [m["content"] for m in result.transcript if m["role"] == "user"]
        self.assertTrue(any("Independent validation failed" in text for text in feedback))

    def test_original_file_changes_fail_validation_and_are_logged(self):
        self.llm.invoke.side_effect = self.extraction + [self.shell_response(tamper=True), self.report()]
        result = self.agent.run(max_tool_iterations=2)
        self.assertFalse(result.finished)
        self.assertEqual(result.info["stop_reason"], "unit_processing_failed")
        self.assertIn("Original extracted file was modified", result.info["error"])
        self.assertFalse(json.loads(self.agent.log_path.read_text())["finished"])
        self.assertIs(self.agent.tool_registry, self.extraction_registry)

    def test_text_without_shell_execution_cannot_complete_processing(self):
        self.llm.invoke.side_effect = self.extraction + [self.report(), self.report()]
        result = self.agent.run(max_tool_iterations=2)
        self.assertFalse(result.finished)
        self.assertIn("No successful shell call", result.info["error"])

    def test_script_changed_after_execution_requires_rerun(self):
        shell = self.shell_response()
        shell.tool_calls.append(ToolCall(id="modify-script", name="run_shell", arguments=json.dumps({
            "command": "printf '\\n# changed after execution\\n' >> standardize_tm.py",
        })))
        self.llm.invoke.side_effect = self.extraction + [shell, self.report()]
        result = self.agent.run(max_tool_iterations=2)
        self.assertFalse(result.finished)
        self.assertIn("Execute the saved script", result.info["error"])

    def test_written_script_and_output_without_verified_execution_are_rejected(self):
        shell = self.shell_response()
        shell.tool_calls[1] = ToolCall(id="indirect-execution", name="run_shell", arguments=json.dumps({
            "command": shlex.quote(sys.executable) + " -c " + shlex.quote("exec(open('standardize_tm.py').read())"),
        }))
        self.llm.invoke.side_effect = self.extraction + [shell, self.report()]
        result = self.agent.run(max_tool_iterations=2)
        self.assertFalse(result.finished)
        self.assertIn("Execute the saved script", result.info["error"])

    def test_failed_extraction_does_not_start_processing(self):
        self.llm.invoke.return_value = self.report()
        result = self.agent.run()
        self.assertFalse(result.finished)
        self.assertEqual(result.info["stop_reason"], "incomplete_extraction")
        self.assertEqual(result.info["validation_status"], "not_run")
        self.assertEqual(self.llm.invoke.call_count, 1)
        self.assertFalse((self.work_dir / "tumor_markers_standardized.json").exists())

    def test_sandbox_preflight_failure_logs_and_never_calls_model(self):
        self.agent.sandbox_config = SandboxConfig()
        for name in ("input", "scripts", "output", "tmp"):
            (self.work_dir / name).mkdir()
        with patch("bpcagent.agents.unit_patient_state.DockerSandbox.check_available",
                   side_effect=SandboxError("Image is missing")):
            result = self.agent.run()
        self.assertFalse(result.finished)
        self.assertEqual(result.info["stop_reason"], "sandbox_error")
        self.llm.invoke.assert_not_called()
        log = json.loads(self.agent.log_path.read_text())
        self.assertFalse(log["finished"])
        self.assertEqual(log["sandbox"]["backend"], "docker")
        self.assertEqual(log["sandbox"]["active_containers"], [])

    def test_verified_correction_is_applied_without_changing_raw_record(self):
        self.set_corrections(self.correction())
        output = self.valid_output()
        marker = output["tumor_markers"][2]
        marker["unit_assessment"] = self.assessment(marker=marker, status="confirmed_correction", correction_id="fix-1")
        marker["normalization"] = dict(status="normalized", value=2, unit="ng/mL", factor=1, reason=None)
        self.validate_fixture(output)
        self.assertEqual(marker["unit_raw"], "nmol/L")
        self.assertEqual(self.agent.unit_summary["assessments"]["confirmed_correction"], 1)

    def inferred_output(self):
        output = self.valid_output()
        marker = output["tumor_markers"][2]
        marker["unit_assessment"] = self.assessment(marker=marker, status="inferred_correction", unit="ug/L")
        marker["unit_assessment"]["reason"] = "Source and visible neighboring mass-unit PSA records support a mislabeled unit."
        marker["normalization"] = dict(status="normalized", value=2, unit="ng/mL", factor=1, reason=None)
        return output

    def test_model_inference_is_applied_without_correction_file_and_audited(self):
        self.llm.invoke.side_effect = self.extraction + [self.shell_response(output=self.inferred_output()), self.report()]
        result = self.agent.run()
        self.assertTrue(result.finished, result.info)
        self.assertEqual(result.info["unit_summary"]["assessments"]["inferred_correction"], 1)
        changes = result.info["unit_changes"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["before"]["unit"], "nmol/L")
        self.assertEqual(changes[0]["after"]["effective_unit"], "ug/L")
        self.assertEqual(changes[0]["after"]["normalization"]["value"], 2)
        self.assertEqual(changes[0]["source_location"], "observations.tumor_markers[2]")
        self.assertIn("M3", result.output["text"])
        self.assertIn(changes[0]["reason"], result.output["text"])
        self.assertEqual(json.loads(self.agent.log_path.read_text())["info"]["unit_changes"], changes)
        self.assertEqual(json.loads((self.work_dir / "patient_state_day_220_001.json").read_text()), self.state)

    def test_missing_unit_can_be_filled_by_explicit_inference(self):
        self.state["observations"]["tumor_markers"][2]["unit_raw"] = None
        self.validate_fixture(self.inferred_output())
        self.assertIsNone(self.agent.unit_changes[0]["before"]["unit"])

    def history_output(self):
        # Distinct refs are essential: the regression involved two real records
        # on the same day, a PSA measurement and a testosterone measurement.
        for marker in self.state["observations"]["tumor_markers"]:
            ref = "E-" + marker["tumor_marker_id"]
            marker["evidence_refs"] = [ref]
            self.state["evidence_refs"][ref] = {}
        output = self.inferred_output()
        assessment = output["tumor_markers"][2]["unit_assessment"]
        assessment["supporting_evidence_refs"].append("E-M1")
        assessment["evidence_observations"].append(self.observation(self.state["observations"]["tumor_markers"][0]))
        return output

    def test_checked_history_is_included_in_result_and_report(self):
        output = self.history_output()
        self.llm.invoke.side_effect = self.extraction + [self.shell_response(output=output), self.report()]
        result = self.agent.run()
        self.assertTrue(result.finished, result.info)
        checked = result.info["unit_changes"][0]["evidence_observations"]
        self.assertEqual([r["tumor_marker_id"] for r in checked], ["M3", "M1"])
        self.assertIn("Checked source: M1, day 200, PSA: '0' 'ug/L'", result.output["text"])

    def test_testosterone_reference_cannot_be_psa_history(self):
        output = self.history_output()
        assessment = output["tumor_markers"][2]["unit_assessment"]
        assessment["supporting_evidence_refs"][-1] = "E-M2"
        assessment["evidence_observations"][-1] = self.observation(self.state["observations"]["tumor_markers"][1])
        with self.assertRaisesRegex(AgentException, "different analyte"):
            self.validate_fixture(output)

    def test_mismatched_measurement_facts_are_rejected(self):
        output = self.history_output()
        marker = self.state["observations"]["tumor_markers"][2]
        for field, wrong in (("tumor_marker_id", "M2"), ("event_day", 199), ("event_day", True),
                             ("analyte_raw", "Testosterone"), ("value_raw", "274.31"),
                             ("unit_raw", "nmol/L"), ("normal_range_lower_raw", "8"),
                             ("normal_range_upper_raw", "32"), ("evidence_refs", ["E-M2"])):
            with self.subTest(field=field, wrong=wrong):
                assessment = deepcopy(output["tumor_markers"][2]["unit_assessment"])
                assessment["evidence_observations"][-1][field] = wrong
                with self.assertRaisesRegex(ValueError, "does not match the source"):
                    self.agent._validate_evidence_observations(marker, assessment, self.state)

    def test_measurement_ref_without_snapshot_is_rejected(self):
        output = self.history_output()
        output["tumor_markers"][2]["unit_assessment"]["evidence_observations"].pop()
        with self.assertRaisesRegex(AgentException, "lacks a checked observation"):
            self.validate_fixture(output)

    def test_conflicting_measurement_ref_also_requires_matching_snapshot(self):
        output = self.history_output()
        assessment = output["tumor_markers"][2]["unit_assessment"]
        assessment["supporting_evidence_refs"].remove("E-M1")
        assessment["conflicting_evidence_refs"].append("E-M1")
        assessment["evidence_observations"][-1]["evidence_refs"] = ["E-M2"]
        with self.assertRaisesRegex(AgentException, "evidence_refs does not match"):
            self.validate_fixture(output)

    def test_bad_history_reference_triggers_repair_loop(self):
        corrected = self.history_output()
        wrong = deepcopy(corrected)
        observation = wrong["tumor_markers"][2]["unit_assessment"]["evidence_observations"][-1]
        observation["evidence_refs"] = ["E-M2"]
        self.llm.invoke.side_effect = self.extraction + [
            self.shell_response(output=wrong), self.report(),
            self.shell_response(output=corrected), self.report(),
        ]
        result = self.agent.run()
        self.assertTrue(result.finished, result.info)
        self.assertEqual([a["status"] for a in self.agent.validation_attempts], ["failed", "passed"])
        self.assertIn("evidence_refs does not match", self.agent.validation_attempts[0]["error"])
        self.assertEqual(result.info["unit_changes"][0]["evidence_observations"][-1]["evidence_refs"], ["E-M1"])

    def test_explicit_free_testosterone_cannot_be_total_history(self):
        self.state["observations"]["tumor_markers"][0]["analyte_raw"] = "Free Testosterone"
        output = self.valid_output()
        assessment = output["tumor_markers"][1]["unit_assessment"]
        assessment["evidence_observations"].append(self.observation(self.state["observations"]["tumor_markers"][0]))
        with self.assertRaisesRegex(ValueError, "different analyte"):
            self.agent._validate_evidence_observations(self.state["observations"]["tumor_markers"][1], assessment, self.state)

    def test_generic_testosterone_can_be_resolved_by_explicit_inference(self):
        self.state["observations"]["tumor_markers"][1]["analyte_raw"] = "Testosterone"
        output = self.valid_output()
        output["tumor_markers"][1]["unit_assessment"]["status"] = "inferred_correction"
        self.validate_fixture(output)
        change = self.agent.unit_changes[0]
        self.assertEqual(change["before"]["analyte_type"], "unknown")
        self.assertEqual(change["after"]["analyte_type"], "total_testosterone")
        self.assertEqual(change["after"]["normalization"]["value"], 28.842)

    def test_inference_cannot_override_an_applicable_verified_correction(self):
        self.set_corrections(self.correction())
        with self.assertRaisesRegex(AgentException, "cannot override"):
            self.validate_fixture(self.inferred_output())

    def test_inference_cannot_invent_a_psa_molar_factor(self):
        output = self.inferred_output()
        output["tumor_markers"][2]["unit_assessment"]["effective_unit"] = "nmol/L"
        with self.assertRaisesRegex(AgentException, "no supported conversion"):
            self.validate_fixture(output)

    def test_inference_still_requires_visible_evidence_and_reason(self):
        output = self.inferred_output()
        output["tumor_markers"][2]["unit_assessment"]["supporting_evidence_refs"].append("E-future")
        with self.assertRaisesRegex(AgentException, "out-of-cutoff"):
            self.validate_fixture(output)

    def test_inference_cannot_silently_change_a_known_assay(self):
        output = self.valid_output()
        output["tumor_markers"][1]["unit_assessment"].update(status="inferred_correction", analyte_type="free_testosterone")
        with self.assertRaisesRegex(AgentException, "known assay"):
            self.validate_fixture(output)

    def test_psa_ngdl_needs_confirmation_even_with_correct_math(self):
        self.state["observations"]["tumor_markers"][0].update(unit_raw="ng/dL", value_raw="200")
        output = self.valid_output()
        output["tumor_markers"][0]["normalization"].update(value=2, factor=0.01)
        with self.assertRaisesRegex(AgentException, "trusted source confirmation"):
            self.validate_fixture(output)

    def test_confirmed_psa_ngdl_can_be_converted_without_relabeling(self):
        self.state["observations"]["tumor_markers"][0].update(unit_raw="ng/dL", value_raw="200")
        self.set_corrections(self.correction(0, corrected_unit="ng/dL"))
        output = self.valid_output()
        output["tumor_markers"][0]["unit_assessment"]["correction_id"] = "fix-1"
        output["tumor_markers"][0]["normalization"].update(value=2, factor=0.01)
        self.validate_fixture(output)

    def test_changed_unit_cannot_use_the_original_units_factor(self):
        self.state["observations"]["tumor_markers"][0].update(unit_raw="ng/dL", value_raw="250")
        self.set_corrections(self.correction(0, corrected_unit="ng/mL"))
        output = self.valid_output()
        output["tumor_markers"][0]["unit_assessment"] = self.assessment(
            status="confirmed_correction", unit="ng/mL", correction_id="fix-1")
        output["tumor_markers"][0]["normalization"].update(value=2.5, factor=0.01)
        with self.assertRaisesRegex(AgentException, "expected 250.0 using effective_unit"):
            self.validate_fixture(output)

    def test_fabricated_confirmation_is_rejected(self):
        output = self.valid_output()
        output["tumor_markers"][2]["unit_assessment"] = self.assessment(marker=output["tumor_markers"][2], status="confirmed_correction", correction_id="invented")
        with self.assertRaisesRegex(AgentException, "not authorized"):
            self.validate_fixture(output)

    def test_correction_for_another_record_cannot_be_reused(self):
        self.set_corrections(self.correction(0, corrected_unit="ug/L"))
        output = self.valid_output()
        output["tumor_markers"][0]["unit_assessment"]["correction_id"] = "fix-1"
        output["tumor_markers"][2]["unit_assessment"] = self.assessment(marker=output["tumor_markers"][2], status="confirmed_correction", correction_id="fix-1")
        with self.assertRaisesRegex(AgentException, "not authorized"):
            self.validate_fixture(output)

    def test_corrections_filter_patient_time_and_raw_value_before_exposure(self):
        self.set_corrections(self.correction(id="other-patient", patient_id="P2"),
                             self.correction(id="future", available_day=221),
                             self.correction(id="stale", value_raw="999"),
                             self.correction(id="other-cancer", target_cancer_id="C2"))
        snapshot = self.agent._prepare_unit_inputs(self.state)
        self.assertEqual(json.loads(snapshot.read_text())["corrections"], [])
        self.assertEqual(self.agent._approved_corrections, {})

    def test_correction_configuration_is_snapshotted_before_model_calls(self):
        self.set_corrections(self.correction())
        self.agent.unit_corrections_path.write_text("{}")
        snapshot = self.agent._prepare_unit_inputs(self.state)
        self.assertEqual(json.loads(snapshot.read_text())["corrections"][0]["id"], "fix-1")

    def test_conflicting_trusted_corrections_fail_preparation(self):
        self.set_corrections(self.correction(), self.correction(id="fix-2", corrected_unit="ng/mL"))
        with self.assertRaisesRegex(AgentException, "Multiple applicable"):
            self.agent._prepare_unit_inputs(self.state)

    def test_missing_unit_cannot_be_filled_from_institution_habit(self):
        self.state["observations"]["tumor_markers"][0]["unit_raw"] = None
        output = self.valid_output()
        output["tumor_markers"][0]["unit_assessment"]["effective_unit"] = "ng/mL"
        with self.assertRaisesRegex(AgentException, "authorized source unit"):
            self.validate_fixture(output)

    def test_candidate_does_not_authorize_conversion(self):
        output = self.valid_output()
        marker = output["tumor_markers"][2]
        marker["unit_assessment"]["candidate_unit"] = "ug/L"
        marker["normalization"].update(status="normalized", value=2, unit="ng/mL", factor=1)
        with self.assertRaisesRegex(AgentException, "authorized conversion"):
            self.validate_fixture(output)

    def test_generic_testosterone_cannot_be_assumed_total(self):
        self.state["observations"]["tumor_markers"][1]["analyte_raw"] = "Testosterone"
        with self.assertRaisesRegex(AgentException, "Assay identity"):
            self.validate_fixture(self.valid_output())

    def test_legal_rare_testosterone_unit_can_pass_after_assessment(self):
        self.state["observations"]["tumor_markers"][1]["unit_raw"] = "ng/mL"
        output = self.valid_output()
        output["tumor_markers"][1]["normalization"].update(value=100, factor=100)
        self.validate_fixture(output)

    def test_inequality_is_preserved_and_unresolved(self):
        self.state["observations"]["tumor_markers"][0]["value_raw"] = "<0.1"
        output = self.valid_output()
        output["tumor_markers"][0]["normalization"] = dict(
            status="unresolved", value=None, unit=None, factor=None, reason="Not an exact value.")
        self.validate_fixture(output)
        self.assertEqual(self.agent.unit_summary["unresolved"], 2)

    def test_unknown_evidence_is_rejected(self):
        output = self.valid_output()
        output["tumor_markers"][2]["unit_assessment"]["supporting_evidence_refs"].append("E-future-day-2302")
        with self.assertRaisesRegex(AgentException, "out-of-cutoff evidence"):
            self.validate_fixture(output)

    def test_future_record_is_rejected_before_processing(self):
        self.state["observations"]["tumor_markers"][2].update(event_day=2302, available_day=2302)
        with self.assertRaisesRegex(AgentException, "not visible"):
            self.agent._prepare_unit_inputs(self.state)

    def test_empty_marker_list_is_successful_completed_work(self):
        self.state["observations"]["tumor_markers"] = []
        self.validate_fixture(self.valid_output())
        self.assertEqual(self.agent.unit_summary["total"], 0)

    def test_changed_raw_field_is_rejected(self):
        output = self.valid_output()
        output["tumor_markers"][0]["unit_raw"] = "ng/mL"
        with self.assertRaisesRegex(AgentException, "Original record"):
            self.validate_fixture(output)

    def test_wrong_rule_version_is_rejected(self):
        output = self.valid_output()
        output["rules_version"] = "model-invented-rules"
        with self.assertRaisesRegex(AgentException, "version is incorrect"):
            self.validate_fixture(output)

    def test_free_testosterone_remains_distinct_and_numeric_types_are_strict(self):
        marker = self.state["observations"]["tumor_markers"][1]
        marker.update(analyte_raw="Free Testosterone", unit_raw="pg/mL", value_raw="100")
        output = self.valid_output()
        output["tumor_markers"][1]["unit_assessment"]["analyte_type"] = "free_testosterone"
        output["tumor_markers"][1]["normalization"].update(value=10, factor=0.1)
        self.validate_fixture(output)
        with self.assertRaisesRegex(ValueError, "incorrect conversion"):
            self.agent._validate_normalization(marker, dict(status="normalized", value=True, unit="ng/dL", factor=0.1, reason=None), "pg/mL")

    def test_unrepresentable_numeric_value_is_unresolved(self):
        self.state["observations"]["tumor_markers"][0]["value_raw"] = "1e999999999"
        output = self.valid_output()
        output["tumor_markers"][0]["normalization"] = dict(
            status="unresolved", value=None, unit=None, factor=None, reason="Value overflows numeric representation.")
        self.validate_fixture(output)

    def test_malformed_correction_dates_are_configuration_errors(self):
        with self.assertRaisesRegex(ValueError, "integer day"):
            self.set_corrections(self.correction(available_day=True))


if __name__ == "__main__":
    unittest.main()
