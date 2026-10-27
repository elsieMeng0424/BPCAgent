"""End-to-end five-agent orchestration with real extraction and scripted models."""

from collections import Counter
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime
import csv
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from bpcagent import Config, LLMResponse
from bpcagent.log_paths import LOG_TIMEZONE
from bpcagent.workflow import AgentWorkflow
from bpcagent.workflow.agents import TargetSelectionAgent
from bpcagent.workflow.demo import DemoLLM
from bpcagent.workflow.state import SharedPatientState, check_state, digest, normalize_markers


def fixture_dataset(root):
    from bpc_state.repository import CLINICAL_FILES

    root.mkdir()
    base = {"cohort": "Prostate", "record_id": "P1"}
    rows = {
        "patients": [{**base, "institution": "TEST", "birth_year": 1950, "naaccr_sex_code": "Male"}],
        "index_cancers": [{**base, "ca_seq": 7, "dob_ca_dx_days": 20000,
                           "first_index_ca_days": 0, "ca_type": "Prostate Cancer"}],
        "regimens": [{**base, "ca_seq": 7, "regimen_number": 1, "drugs_drug_1": "ExampleDrug",
                      "drugs_startdt_int_1": 20010, "dx_drug_start_int_1": 10, "dx_drug_end_int_1": 100}],
        "tumor_markers": [
            {**base, "tm_number": 1, "dx_tm_days": 20, "tm_type": "PSA", "tm_num_result": 0,
             "tm_result_units": "ug/L"},
            {**base, "tm_number": 2, "dx_tm_days": 30, "tm_type": "PSA", "tm_num_result": 5,
             "tm_result_units": ""},
            {**base, "tm_number": 3, "dx_tm_days": 120, "tm_type": "PSA", "tm_num_result": 999,
             "tm_result_units": "ng/mL"},
        ],
    }
    for key, name in CLINICAL_FILES.items():
        table = rows.get(key, [])
        fields = list(dict.fromkeys(k for row in table for k in row)) or list(base)
        with (root / name).open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(table)
    return root


class AlteredDemo(DemoLLM):
    def __init__(self, role, *, disposition=None, early_stop=False):
        super().__init__(role)
        self.disposition = disposition
        self.early_stop = early_stop

    def invoke(self, messages, **kwargs):
        if self.early_stop:
            return LLMResponse(content="I am done", finish_reason="stop")
        response = super().invoke(messages, **kwargs)
        if self.disposition:
            for call in response.tool_calls:
                if call.name == "submit_output":
                    output = json.loads(call.arguments)
                    output["disposition"] = self.disposition
                    output["reason"] = "Please fix the stage output." if self.disposition == "revise" else "Stop this run."
                    call.arguments = json.dumps(output)
        return response


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = fixture_dataset(self.root / "data")
        stdout = redirect_stdout(io.StringIO())
        stdout.__enter__()
        self.addCleanup(stdout.__exit__, None, None, None)

    def workflow(self, **kwargs):
        return AgentWorkflow(data_dir=self.data, workspace_root=self.root / "runs", **kwargs)

    def test_repeated_runs_are_isolated_under_start_date(self):
        workflow = self.workflow()
        with patch("bpcagent.log_paths.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 27, tzinfo=LOG_TIMEZONE)
            first = workflow.run(patient_id="P1", cutoff_day=50)
            second = workflow.run(patient_id="P1", cutoff_day=50)
        folders = [Path(result["task_dir"]) for result in (first, second)]
        self.assertNotEqual(*folders)
        for folder in folders:
            self.assertEqual(folder.parent, self.root / "runs" / "2026-10-27")
            self.assertTrue((folder / "workflow_result.json").is_file())
            self.assertEqual(len(list(folder.glob("*/transcript.json"))), 6)
            self.assertIn("Workflow finished: completed", (folder / "logs/console.log").read_text())
            saved = json.loads((folder / "workflow_result.json").read_text())
            self.assertEqual(Path(saved["log_path"]), folder / "logs/console.log")

    def test_unexpected_failure_preserves_log_and_restores_streams(self):
        import sys
        workflow = self.workflow()
        before = (sys.stdout, sys.stderr)
        with patch.object(workflow, "_run", side_effect=RuntimeError("private-secret")):
            with self.assertRaises(RuntimeError):
                workflow.run(patient_id="P1", cutoff_day=50)
        self.assertEqual((sys.stdout, sys.stderr), before)
        log = (workflow.last_run_dir / "logs/console.log").read_text()
        self.assertIn("Workflow aborted: RuntimeError", log)
        self.assertNotIn("private-secret", log)

    def test_end_to_end_order_shared_snapshot_and_artifacts(self):
        result = self.workflow().run(patient_id="P1", cutoff_day=50, start_day=25)
        self.assertTrue(result["finished"])
        roles = [r["agent"] for r in result["turns"]]
        self.assertEqual(roles, [
            "PatientStateAgent", "ReviewAgent", "TargetSelectionAgent", "ReviewAgent",
            "DrugSelectionAgent", "ReviewAgent",
        ])
        self.assertEqual(len({r["patient_state_sha256"] for r in result["turns"]}), 1)
        folder = Path(result["task_dir"])
        snapshot = json.loads((folder / "shared_patient_state.json").read_text())
        raw = snapshot["patient_state"]
        self.assertEqual(result["patient_state_sha256"], digest(raw))
        self.assertEqual(len(raw["observations"]["tumor_markers"]), 2)
        self.assertEqual(len(snapshot["recent_observations"]["tumor_markers"]), 1)
        self.assertEqual(raw["treatment_history"][0]["start_day"], 10)
        self.assertIsNone(raw["treatment_history"][0]["end_day"])
        self.assertEqual(raw["treatment_history"][0]["status_at_cutoff"], "unknown")
        self.assertFalse(result["rsi_applied"])
        for stage in result["stages"]:
            self.assertEqual(stage["review_result"]["output"]["clinical_validation"], "not_performed")
            self.assertNotIn("meta_result", stage)
        self.assertEqual(result["accepted_outputs"]["DrugSelectionAgent"]["candidates"], [])
        self.assertEqual(len(list(folder.glob("*/transcript.json"))), 6)
        review_context = json.loads((folder / "06_ReviewAgent" / "context.json").read_text())
        self.assertEqual(review_context["stage_result"], result["stages"][-1]["stage_result"])
        self.assertIn("TargetSelectionAgent", review_context["accepted_outputs"])
        self.assertEqual([t["turn"] for t in result["turns"]], list(range(1, 7)))
        self.assertEqual([t["reviews_turn"] for t in result["turns"][1::2]], [1, 3, 5])
        self.assertFalse(result["meta_enabled"])
        transcript = json.loads((folder / "05_DrugSelectionAgent" / "transcript.json").read_text())
        tools = [json.loads(m["content"]) for m in transcript if m["role"] == "tool"]
        self.assertEqual(next(t["data"]["status"] for t in tools if t["tool_name"] == "dock"), "not_implemented")

    def test_review_blocks_without_meta_or_downstream_execution(self):
        result = self.workflow(llm_factory=lambda role: AlteredDemo(
            role, disposition="block" if role == "ReviewAgent" else None
        )).run(patient_id="P1", cutoff_day=50)
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["finished"])
        self.assertEqual(len(result["turns"]), 2)
        self.assertEqual(result["accepted_outputs"], {})

    def test_revision_feedback_and_limit(self):
        counter = Counter()

        def factory(role):
            counter[role] += 1
            return AlteredDemo(role, disposition="revise" if role == "ReviewAgent" and counter[role] == 1 else None)

        result = self.workflow(llm_factory=factory).run(patient_id="P1", cutoff_day=50)
        self.assertTrue(result["finished"])
        self.assertEqual(len(result["turns"]), 8)
        context = json.loads((Path(result["task_dir"]) / "03_PatientStateAgent" / "context.json").read_text())
        self.assertEqual(context["previous_feedback"]["review_result"]["output"]["disposition"], "revise")
        self.assertEqual(context["previous_feedback"]["review_result"]["output"]["reason"], "Please fix the stage output.")
        self.assertNotIn("PatientStateAgent", context["accepted_outputs"])
        limited = self.workflow(max_stage_attempts=1, llm_factory=lambda role: AlteredDemo(
            role, disposition="revise" if role == "ReviewAgent" else None
        )).run(patient_id="P1", cutoff_day=50)
        self.assertEqual(limited["status"], "revision_limit")

    def test_text_without_submit_is_failure_with_review(self):
        result = self.workflow(llm_factory=lambda role: AlteredDemo(
            role, early_stop=role == "TargetSelectionAgent"
        )).run(patient_id="P1", cutoff_day=50)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(result["turns"]), 4)
        self.assertEqual(result["stages"][-1]["stage_result"]["error"], "missing_structured_output")

    def test_failed_review_stops_workflow(self):
        result = self.workflow(llm_factory=lambda role: AlteredDemo(
            role, early_stop=role == "ReviewAgent"
        )).run(patient_id="P1", cutoff_day=50)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(result["turns"]), 2)
        self.assertEqual(result["stages"][0]["review_result"]["status"], "failed")

    def test_live_model_routing_with_mock_transport(self):
        created = []

        class FakeTransport:
            def __init__(self, *, config):
                self.config = config
                self.model = config.model
                self.delegate = None
                created.append(config.model)

            def invoke(self, messages, **kwargs):
                if self.delegate is None:
                    tools = {t["function"]["name"] for t in kwargs["tools"]}
                    role = "ReviewAgent"
                    for tool, candidate in [("get_patient_state", "PatientStateAgent"),
                                            ("graph_ranking", "TargetSelectionAgent"),
                                            ("drug_catalog", "DrugSelectionAgent"),
                                            ("rsi_evaluate", "MetaAgent")]:
                        if tool in tools:
                            role = candidate
                    self.delegate = DemoLLM(role)
                return self.delegate.invoke(messages, **kwargs)

        config = lambda model: Config(model=model, api_key="do-not-log-this-secret",
                                      base_url="https://example.invalid", max_tool_iterations=8)
        with patch("bpcagent.agents.agents.LLM", FakeTransport):
            result = self.workflow(mode="live", agent_configs={
                role: config("review-model" if role == "ReviewAgent" else
                             "meta-model" if role == "MetaAgent" else "task-model")
                for role in ("PatientStateAgent", "TargetSelectionAgent", "DrugSelectionAgent", "ReviewAgent", "MetaAgent")}).run(
                                       patient_id="P1", cutoff_day=50)
        self.assertTrue(result["finished"])
        self.assertEqual(created, ["task-model", "review-model"] * 3)
        artifacts = "".join(p.read_text() for p in Path(result["task_dir"]).rglob("*.json"))
        self.assertNotIn("do-not-log-this-secret", artifacts)

    def test_extraction_failure_still_notifies_review(self):
        result = self.workflow().run(patient_id="UNKNOWN", cutoff_day=50)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(result["turns"]), 2)
        self.assertIsNone(result["patient_state_sha256"])
        self.assertEqual(result["stages"][0]["review_result"]["status"], "completed")

    def test_shared_state_copy_and_no_future_tools(self):
        shared = SharedPatientState(patient_id="P1", cutoff_day=50, data_dir=self.data)
        first = shared.extract()
        first["patient_state"]["meta"]["patient_id"] = "WRONG"
        self.assertEqual(shared.read()["patient_state"]["meta"]["patient_id"], "P1")
        agent = TargetSelectionAgent(shared=shared, context={"top_m": 3}, directory=self.root,
                                     config=Config(), llm=DemoLLM("TargetSelectionAgent"))
        self.assertFalse(any("evaluation" in name for name in agent.tool_registry.list_tools()))
        rejection = agent.tool_registry.execute_tool("get_shared_patient_state", {"cutoff_day": 999})
        self.assertEqual(rejection.status, "error")
        agent.get_shared()
        agent.get_context()
        with self.assertRaisesRegex(ValueError, "outside"):
            agent.submit({"candidates": [{"target": "X", "evidence_refs": ["E-future"]}]})
        with self.assertRaisesRegex(ValueError, "not implemented"):
            agent.submit({"candidates": [{"target": "X", "evidence_refs": list(shared.read()["patient_state"]["evidence_refs"])}]})

    def test_unit_conversion_and_unknown_not_zero(self):
        shared = SharedPatientState(patient_id="P1", cutoff_day=50, data_dir=self.data).extract()
        rows = shared["normalized_markers"]
        self.assertEqual(rows[0]["normalization"]["value"], 0)
        self.assertEqual(rows[0]["normalization"]["unit"], "ng/mL")
        self.assertIsNone(rows[1]["normalization"]["value"])
        self.assertEqual(rows[1]["missing_status"], "unknown")
        self.assertFalse(shared["state_check"]["complete"])
        for raw_unit in ["ng/dl", "nmol/L", None]:
            state = deepcopy(shared["patient_state"])
            state["observations"]["tumor_markers"][0]["unit_raw"] = raw_unit
            self.assertEqual(normalize_markers(state)[0]["normalization"]["status"], "unresolved")

    def test_future_mutation_leaves_snapshot_and_outputs_identical(self):
        before = self.workflow().run(patient_id="P1", cutoff_day=50)
        path = self.data / "tm_level_dataset.csv"
        text = path.read_text().replace("999", "123456")
        path.write_text(text)
        after = self.workflow().run(patient_id="P1", cutoff_day=50)
        self.assertEqual(before["patient_state_sha256"], after["patient_state_sha256"])
        self.assertEqual(before["turns"], after["turns"])
        self.assertNotEqual(before["task_dir"], after["task_dir"])

    def test_checker_rejects_future_and_dangling_evidence(self):
        state = SharedPatientState(patient_id="P1", cutoff_day=50, data_dir=self.data).extract()["patient_state"]
        changed = deepcopy(state)
        changed["observations"]["tumor_markers"][0]["available_day"] = 51
        with self.assertRaisesRegex(ValueError, "post-cutoff"):
            check_state(changed)
        changed = deepcopy(state)
        changed["observations"]["tumor_markers"][0]["evidence_refs"] = ["E-future"]
        with self.assertRaisesRegex(ValueError, "dangling"):
            check_state(changed)

    def test_offline_evaluation_separate_with_matching_hash(self):
        from bpc_state.evaluation import OfflineEvaluationExporter

        result = self.workflow().run(patient_id="P1", cutoff_day=50)
        evaluation = OfflineEvaluationExporter(self.data).export("P1", 50, 150)
        self.assertEqual(evaluation["meta"]["input_state_sha256"], result["patient_state_sha256"])
        self.assertEqual(len(evaluation["observations"]["tumor_markers"]), 1)
        artifacts = "".join(p.read_text() for p in Path(result["task_dir"]).rglob("*.json"))
        self.assertNotIn('"offline_evaluation"', artifacts)
        self.assertNotIn('"value_numeric": 999', artifacts)

    def test_configuration_validation_and_cli(self):
        from bpcagent.workflow.cli import main

        cfg = Config(model="same", api_key="secret", base_url="https://example.invalid")
        with self.assertRaisesRegex(ValueError, "different model"):
            self.workflow(mode="live", agent_configs={role: cfg for role in (
                "PatientStateAgent", "TargetSelectionAgent", "DrugSelectionAgent", "ReviewAgent", "MetaAgent")})
        with self.assertRaises(ValueError):
            self.workflow(top_m=True)
        with self.assertRaises(ValueError):
            self.workflow().run(patient_id="P1", cutoff_day=50, start_day=60)
        with patch.dict(os.environ, {"LLM_API_KEY": "unused-secret"}):
            self.assertEqual(main(["--data-dir", str(self.data), "--patient-id", "P1", "--cutoff-day", "50",
                                   "--workspace-root", str(self.root / "cli")]), 0)
        output = "".join(p.read_text() for p in (self.root / "cli").rglob("*.json"))
        self.assertNotIn("unused-secret", output)


if __name__ == "__main__":
    unittest.main()
