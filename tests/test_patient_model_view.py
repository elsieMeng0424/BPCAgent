"""Lossless compact patient views and evidence access across workflow roles."""

from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from bpcagent import Config, ToolCall
from bpcagent.workflow import AgentWorkflow
from bpcagent.workflow.agents import AGENTS
from bpcagent.workflow.demo import DemoLLM
from bpcagent.workflow.model_view import (
    OBSERVATION_IDS, RECORDS_TABLE, STATUS_TABLE, compact_records, expand_tables,
    patient_model_view, state_check_model_view,
)
from bpcagent.workflow.state import SharedPatientState, digest

if __package__:
    from .test_workflow import fixture_dataset
else:
    from test_workflow import fixture_dataset


class PatientModelViewTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = fixture_dataset(self.root / "data")
        self.shared = SharedPatientState(patient_id="P1", cutoff_day=50, data_dir=self.data)
        stream = redirect_stdout(io.StringIO())
        stream.__enter__()
        self.addCleanup(stream.__exit__, None, None, None)

    def agent(self, role):
        return AGENTS[role](shared=self.shared, context={}, directory=self.root / role,
                            config=Config(), llm=DemoLLM(role), shell_config={"mode": "disabled"})

    def test_tables_round_trip_nested_values_and_keep_sparse_records(self):
        records = [{"event_day": day, "available_day": day + 2,
                    "value_numeric": value, "value_status": status,
                    "evidence_refs": ["E-1"], "duration_masked": False,
                    "nested": [{"long_repeated_field_name": i, "another_field_name": None} for i in range(4)]}
                   for day, value, status in [(1, 0, "observed"), (2, None, "unknown"),
                                               (3, None, "not_applicable"), (4, -2.5, "observed")]]
        packed = compact_records(records)
        self.assertEqual(packed["encoding"], RECORDS_TABLE)
        self.assertLess(len(json.dumps(packed)), len(json.dumps(records)))
        self.assertEqual(expand_tables(packed), records)
        self.assertIs(expand_tables(packed)[0]["duration_masked"], False)
        for value in ([], {}, None, "unknown", [{"value": None}, {}], [{"x": 0}, {"x": False, "y": None}]):
            self.assertEqual(expand_tables(compact_records(value)), value)
        # Different key insertion order does not change column/value alignment.
        reversed_keys = [records[0], dict(reversed(list(records[1].items())))]
        self.assertEqual(expand_tables(compact_records(reversed_keys)), reversed_keys)

    def test_invalid_table_cannot_silently_drop_values(self):
        for table in ({"encoding": RECORDS_TABLE, "columns": ["a"], "rows": [[1, 2]]},
                      {"encoding": RECORDS_TABLE, "columns": ["a", "a"], "rows": [[1, 2]]},
                      {"encoding": STATUS_TABLE, "columns": ["status"], "rows": [["unknown"]]},
                      {"encoding": STATUS_TABLE, "columns": ["path"], "rows": [["/x"], ["/x"]]}):
            with self.subTest(table=table), self.assertRaises(ValueError):
                expand_tables(table)

    def test_projection_preserves_every_value_except_evidence_bodies_and_duplicate_observations(self):
        canonical = self.shared.extract()
        original = deepcopy(canonical)
        view = self.shared.model_view()
        decoded = expand_tables(view)
        expected = deepcopy(canonical)
        expected["patient_state"]["evidence_refs"] = list(expected["patient_state"]["evidence_refs"])
        recent = expected.pop("recent_observations")
        expected["recent_observation_ids"] = {
            key: [row[OBSERVATION_IDS[key]] for row in rows] for key, rows in recent.items()}
        decoded.pop("model_view")
        self.assertEqual(decoded, expected)
        self.assertNotIn('"sources"', json.dumps(view))
        self.assertNotIn('"recent_observations"', json.dumps(view))
        self.assertEqual(self.shared.read(), original)
        self.assertEqual(view["patient_state_sha256"], digest(original["patient_state"]))
        view["patient_state"]["meta"]["patient_id"] = "MUTATED"
        self.assertEqual(self.shared.model_view()["patient_state"]["meta"]["patient_id"], "P1")
        self.assertEqual(canonical, original)

    def test_recent_ids_are_inclusive_and_resolve_to_exact_canonical_records(self):
        for start, count in ((None, 2), (20, 2), (30, 1), (50, 0)):
            with self.subTest(start=start):
                shared = SharedPatientState(patient_id="P1", cutoff_day=50, start_day=start, data_dir=self.data)
                payload = shared.extract()
                decoded = expand_tables(shared.model_view())
                self.assertEqual(decoded["recent_start_day"], start)
                ids = decoded["recent_observation_ids"]["tumor_markers"]
                self.assertEqual(len(ids), count)
                all_rows = decoded["patient_state"]["observations"]["tumor_markers"]
                selected = [row for row in all_rows if row["tumor_marker_id"] in ids]
                self.assertEqual(selected, payload["recent_observations"]["tumor_markers"])
                self.assertEqual(len(all_rows), 2)  # Earlier history is not discarded.

    def test_null_status_reasons_refs_and_normalization_are_preserved(self):
        payload = self.shared.extract()
        decoded = expand_tables(patient_model_view(payload))
        state = decoded["patient_state"]
        treatment = state["treatment_history"][0]
        self.assertIsNone(treatment["end_day"])
        self.assertEqual(treatment["status_at_cutoff"], "unknown")
        status = state["field_status"]["/treatment_history/0/end_day"]
        self.assertEqual(status["status"], "unknown")
        self.assertTrue(status["reason"])
        self.assertTrue(status["evidence_refs"])
        self.assertEqual(state["field_status"], payload["patient_state"]["field_status"])
        self.assertEqual(decoded["normalized_markers"][0]["normalization"]["value"], 0)
        self.assertIsNone(decoded["normalized_markers"][1]["normalization"]["value"])
        self.assertEqual(decoded["normalized_markers"][1]["missing_status"], "unknown")
        check = expand_tables(state_check_model_view(payload))
        self.assertEqual(check, {"completeness": payload["state_check"],
                                 "normalized_markers": payload["normalized_markers"]})
        # Field-status annotations are an exact encoding, including non-unknown statuses.
        statuses = {f"/example/{i}": {"status": status, "reason": "explicit_source_annotation",
                                     "evidence_refs": ["E-1"]}
                    for i, status in enumerate(["unknown", "not_applicable", "not_recorded", "not_normalized"] * 4)}
        payload["patient_state"]["field_status"] = statuses
        packed = patient_model_view(payload)
        self.assertEqual(packed["patient_state"]["field_status"]["encoding"], STATUS_TABLE)
        self.assertEqual(expand_tables(packed)["patient_state"]["field_status"], statuses)

    def test_every_role_can_fetch_and_save_only_admitted_evidence(self):
        canonical = self.shared.extract()
        ref = next(iter(canonical["patient_state"]["evidence_refs"]))
        expected = self.shared.get_evidence([ref])
        for role in AGENTS:
            with self.subTest(role=role):
                agent = self.agent(role)
                tool = agent.tool_registry
                self.assertEqual(tool.execute_tool("get_patient_evidence", {"evidence_refs": [ref]}).status, "error")
                view = agent.extract() if role == "PatientStateAgent" else agent.get_shared()
                self.assertEqual(view, self.shared.model_view())
                result = tool.execute_tool("get_patient_evidence", {"evidence_refs": [ref]})
                self.assertEqual(result.status, "success")
                self.assertEqual(result.data, expected)
                saved = json.loads(Path(agent.artifacts["evidence"][0]).read_text())
                self.assertEqual(saved["data"], expected)
                self.assertEqual(saved["cutoff_day"], 50)
                for args in ({"evidence_refs": ["E-future"]}, {"evidence_refs": []},
                             {"evidence_refs": [ref], "cutoff_day": 120},
                             {"evidence_refs": [ref], "patient_id": "P2"}):
                    self.assertEqual(tool.execute_tool("get_patient_evidence", args).status, "error")
                self.assertEqual(len(agent.artifacts["evidence"]), 1)
        self.assertEqual(self.shared.read(), canonical)

    def test_future_only_reference_and_source_mutation_do_not_leak(self):
        self.shared.extract()
        before = self.shared.model_view()
        later = SharedPatientState(patient_id="P1", cutoff_day=150, data_dir=self.data).extract()
        future = set(later["patient_state"]["evidence_refs"]) - set(before["patient_state"]["evidence_refs"])
        self.assertTrue(future)
        review = self.agent("ReviewAgent")
        review.get_shared()
        with self.assertRaises(ValueError):
            review.get_evidence([next(iter(future))])
        path = self.data / "tm_level_dataset.csv"
        path.write_text(path.read_text().replace("999", "987654321"))
        after = SharedPatientState(patient_id="P1", cutoff_day=50, data_dir=self.data)
        after.extract()
        self.assertEqual(before, after.model_view())

    def test_no_evidence_empty_view_is_not_an_invented_negative_finding(self):
        payload = self.shared.extract()
        payload["patient_state"]["evidence_refs"] = {}
        payload["patient_state"]["field_status"] = {}
        payload["patient_state"]["observations"] = {key: [] for key in OBSERVATION_IDS}
        payload["recent_observations"] = {key: [] for key in OBSERVATION_IDS}
        view = patient_model_view(payload)
        self.assertEqual(view["patient_state"]["evidence_refs"], [])
        self.assertEqual(view["patient_state"]["field_status"], {})
        self.assertEqual(view["recent_observation_ids"], {key: [] for key in OBSERVATION_IDS})
        self.assertEqual(view["patient_state"]["observations"], payload["patient_state"]["observations"])

    def test_native_workflow_uses_compact_views_but_keeps_full_artifacts(self):
        class EvidenceDemo(DemoLLM):
            def invoke(self, messages, **kwargs):
                response = super().invoke(messages, **kwargs)
                if self.role != "PatientStateAgent" and self.tool_step == 2:
                    results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
                    view = next(r["data"] for r in results if r["tool_name"] == "get_shared_patient_state")
                    response.tool_calls.append(ToolCall(
                        id=f"{self.role}_evidence", name="get_patient_evidence",
                        arguments=json.dumps({"evidence_refs": view["patient_state"]["evidence_refs"][:1]})))
                return response

        result = AgentWorkflow(data_dir=self.data, workspace_root=self.root / "runs",
                               llm_factory=EvidenceDemo).run(patient_id="P1", cutoff_day=50)
        self.assertTrue(result["finished"])
        root = Path(result["task_dir"])
        full = json.loads((root / "patient_state.json").read_text())
        self.assertIsInstance(full["evidence_refs"], dict)
        self.assertIsInstance(full["observations"]["tumor_markers"], list)
        views = []
        for turn in result["turns"]:
            directory = root / f'{turn["turn"]:03d}_{turn["agent"]}'
            messages = json.loads((directory / "transcript.json").read_text())
            results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            view = next(r["data"] for r in results if r["tool_name"] in {"bpc_state_extractor", "get_shared_patient_state"})
            views.append(view)
            self.assertIsInstance(view["patient_state"]["evidence_refs"], list)
            self.assertNotIn("recent_observations", view)
            evidence = next(r for r in results if r["tool_name"] == "get_patient_evidence")
            self.assertEqual(evidence["status"], "success")
            self.assertTrue(list(directory.glob("patient_evidence_*.json")))
        self.assertTrue(all(view == views[0] for view in views))
        self.assertEqual(json.loads((root / "patient_model_view.json").read_text()), views[0])
        extracted = next(root.glob("001_PatientStateAgent/patient_state_*.json"))
        self.assertEqual(json.loads(extracted.read_text()), full)


if __name__ == "__main__":
    unittest.main()
