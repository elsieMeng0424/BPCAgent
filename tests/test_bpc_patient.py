"""验证患者工具适配和落盘边界；不依赖 bpc_state_extractor 或模型服务。"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bpcagent import ToolRegistry
from bpcagent.tools.bpc_patient import PatientArtifactStore, PatientEvidenceTool, PatientStateTool


class PatientToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.state = {"meta": {"patient_id": "P1", "cutoff_day": 220}, "evidence_refs": {"E-test": {}}}
        self.evidence = {"E-test": {"sources": [{"source_file": "patient.csv", "values": {"sex": "Male"}}]}}
        self.bound = SimpleNamespace(
            patient_id="P1", cancer_id="index:0", max_cutoff_day=220,
            get_patient_state=Mock(return_value=self.state),
            get_evidence=Mock(return_value=self.evidence),
        )
        self.store = PatientArtifactStore(self.directory)
        self.registry = ToolRegistry()
        self.registry.register_tool(PatientStateTool(self.bound, self.store))
        self.registry.register_tool(PatientEvidenceTool(self.bound, self.store))

    def test_shared_sequence_preserves_data_and_context(self):
        state = self.registry.execute_tool("get_patient_state", {"cutoff_day": 220})
        evidence = self.registry.execute_tool("get_patient_evidence", {
            "cutoff_day": 220, "evidence_refs": ["E-test"],
        })
        repeated = self.registry.execute_tool("get_patient_state", {"cutoff_day": 220})
        self.assertEqual([state.status, evidence.status, repeated.status], ["success"] * 3)
        self.assertEqual(state.data, self.state)
        self.assertEqual(evidence.data, self.evidence)
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), [
            "patient_evidence_day_220_002.json",
            "patient_state_day_220_001.json",
            "patient_state_day_220_003.json",
        ])
        for number in (1, 3):
            saved = json.loads((self.directory / f"patient_state_day_220_{number:03d}.json").read_text(encoding="utf-8"))
            self.assertEqual(saved, self.state)
        saved = json.loads((self.directory / "patient_evidence_day_220_002.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, {
            "patient_id": "P1", "cancer_id": "index:0", "cutoff_day": 220,
            "evidence_refs": ["E-test"], "data": self.evidence,
        })

    def test_arguments_are_rejected_before_querying_or_saving(self):
        for arguments in ({"cutoff_day": 221}, {"cutoff_day": True}, {"cutoff_day": "220"},
                          {"cutoff_day": 220, "patient_id": "P2"}):
            with self.subTest(arguments=arguments):
                result = self.registry.execute_tool("get_patient_state", arguments)
                self.assertEqual(result.error.code, "invalid_arguments")
        result = self.registry.execute_tool("get_patient_evidence", {"cutoff_day": 220, "evidence_refs": []})
        self.assertEqual(result.error.code, "invalid_arguments")
        self.bound.get_patient_state.assert_not_called()
        self.bound.get_evidence.assert_not_called()
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_bound_schemas_are_independent_and_allow_negative_days(self):
        early_bound = SimpleNamespace(patient_id="P2", max_cutoff_day=40)
        early = PatientStateTool(early_bound, self.store)
        later = self.registry.get_tool("get_patient_state")
        self.assertEqual(early.to_openai_tool()["function"]["parameters"]["properties"]["cutoff_day"]["maximum"], 40)
        self.assertEqual(later.to_openai_tool()["function"]["parameters"]["properties"]["cutoff_day"]["maximum"], 220)
        self.assertEqual(later.args_schema(cutoff_day=-5).cutoff_day, -5)

    def test_query_and_write_failures_are_tool_errors(self):
        self.bound.get_evidence.side_effect = KeyError("unavailable evidence")
        result = self.registry.execute_tool("get_patient_evidence", {"cutoff_day": 220, "evidence_refs": ["missing"]})
        self.assertEqual(result.error.code, "execution_error")
        self.assertEqual(list(self.directory.iterdir()), [])
        with patch.object(Path, "open", side_effect=PermissionError("cannot write")):
            result = self.registry.execute_tool("get_patient_state", {"cutoff_day": 220})
        self.assertEqual(result.error.code, "execution_error")
        self.assertIsNone(result.data)
        self.bound.get_patient_state.assert_called_once_with(220)
        self.assertEqual(list(self.directory.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
