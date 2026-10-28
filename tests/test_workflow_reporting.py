"""Live turn visibility and read-only replay of successful and failed runs."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from bpcagent.workflow import AgentWorkflow, print_workflow_report
from bpcagent.workflow.reporting import print_turn_report

if __package__:
    from .test_workflow import fixture_dataset
else:
    from test_workflow import fixture_dataset


class WorkflowReportingTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = fixture_dataset(self.root / "data")
        self.workflow = AgentWorkflow(data_dir=self.data, workspace_root=self.root / "runs")

    def test_verbose_reports_each_turn_before_the_next_one_and_keeps_log(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            result = self.workflow.run(patient_id="P1", cutoff_day=50, verbose=True, max_print_chars=100)
        text = stream.getvalue()
        self.assertTrue(result["finished"])
        log = Path(result["log_path"]).read_text()
        for number in range(1, 7):
            self.assertIn(f">>> Starting turn {number:03d}", text)
            heading = f"=== Turn {number:03d} |"
            self.assertIn(heading, text)
            self.assertIn(heading, log)
            if number < 6:
                self.assertLess(text.index(heading), text.index(f">>> Starting turn {number + 1:03d}"))
        for required in ("Tool call bpc_state_extractor", "Tool call submit_output", "Tool result",
                         "Structured output", "Stage context / input", "display truncated"):
            self.assertIn(required, text)
        # Display limits do not truncate or otherwise alter the recorded source data.
        transcript = json.loads((Path(result["task_dir"]) / "001_PatientStateAgent/transcript.json").read_text())
        self.assertTrue(any(len(m.get("content") or "") > 100 for m in transcript))

    def test_default_keeps_console_private_and_replay_does_not_execute(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            result = self.workflow.run(patient_id="P1", cutoff_day=50)
        self.assertEqual(stream.getvalue(), "")
        with patch.object(self.workflow, "run", side_effect=AssertionError("Must not rerun")):
            print_workflow_report(result["task_dir"], max_chars=None, stream=stream)
        self.assertIn("Turn 006 | ReviewAgent", stream.getvalue())
        self.assertNotIn("display truncated", stream.getvalue())

    def test_failed_turn_reports_codes_tool_errors_and_budget_without_huge_archives(self):
        directory = self.root / "001_PatientStateAgent"
        directory.mkdir()
        turn = {"turn": 1, "agent": "PatientStateAgent", "status": "failed", "output": None,
                "error": "context_budget_exceeded", "artifacts": {}}
        records = {
            "output.json": turn,
            "context.json": {"user_request": "Inspect patient P1 through day 50"},
            "context_usage.json": {"policy": {"max_input_tokens": 32000}, "events": [
                {"status": "budget_exceeded", "estimated_after": 33018, "input_budget": 32000}],
                "records": {"archived": "SECRET_ARCHIVE_SENTINEL"}},
            "transcript.json": [
                {"role": "assistant", "content": None, "reasoning_content": "HIDDEN_REASONING_SENTINEL",
                 "tool_calls": [{"id": "shell1", "function": {"name": "run_shell", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "shell1", "content": json.dumps({
                    "tool_name": "run_shell", "status": "error", "data": None,
                    "error": {"code": "execution_error", "message": "Docker CLI not found"}})}],
        }
        for name, data in records.items():
            (directory / name).write_text(json.dumps(data))
        stream = io.StringIO()
        # Final workflow_result.json need not exist after an interrupted run.
        print_workflow_report(self.root, stream=stream, max_chars=10)
        report = stream.getvalue()
        for required in ("incomplete", "context_budget_exceeded", "33018", "32000", "Docker CLI not found"):
            self.assertIn(required, report)
        self.assertNotIn("SECRET_ARCHIVE_SENTINEL", report)
        self.assertNotIn("HIDDEN_REASONING_SENTINEL", report)

    def test_report_handles_a_missing_transcript_and_full_structured_output(self):
        stream = io.StringIO()
        long_reason = "The current task must be revised. " * 100
        print_turn_report({"turn": 2, "agent": "ReviewAgent", "status": "failed",
                           "error": "step_limit_exceeded", "output": {"reason": long_reason}},
                          self.root, max_chars=10, stream=stream)
        self.assertIn("step_limit_exceeded", stream.getvalue())
        self.assertIn("Transcript:\nNot available", stream.getvalue())
        self.assertIn(long_reason, stream.getvalue())

    def test_verbose_exception_preserves_log_and_restores_streams(self):
        stream = io.StringIO()
        before = sys.stderr
        with redirect_stdout(stream), patch.object(self.workflow, "_run", side_effect=RuntimeError("private-secret")):
            with self.assertRaises(RuntimeError):
                self.workflow.run(patient_id="P1", cutoff_day=50, verbose=True)
            self.assertIs(sys.stdout, stream)
            self.assertIs(sys.stderr, before)
        self.assertIn("Workflow aborted: RuntimeError", stream.getvalue())
        self.assertNotIn("private-secret", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
