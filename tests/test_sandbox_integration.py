"""Opt-in Docker tests with synthetic data only; never contact an LLM.

Run with BPC_RUN_DOCKER_TESTS=1. Missing Docker/image then FAILS, not skips.
"""

import json
import os
from pathlib import Path
import shlex
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bpcagent import Config, DockerSandbox, LLMResponse, PatientStateTask, PatientStateTaskConfig, SandboxConfig, SandboxError, ToolCall, UnitPatientStateAgent
from bpcagent.agents.unit_rules import UNIT_SCHEMA_VERSION, UNIT_RULES_VERSION
from bpcagent.tools.bpc_patient import PatientArtifactStore, PatientEvidenceTool, PatientStateTool


@unittest.skipUnless(os.environ.get("BPC_RUN_DOCKER_TESTS") == "1", "Docker integration tests not requested")
class DockerIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="bpc-sandbox-integration-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        # Non-ASCII and spaces exercise the actual user workspace path shape.
        self.task = self.root / "任务 directory"
        for name in DockerSandbox.directories:
            (self.task / name).mkdir(parents=True)
        self.input = self.task / "input" / "sample.txt"
        self.input.write_text("synthetic input")
        self.sandbox = DockerSandbox(SandboxConfig(), self.task)
        self.sandbox.check_available()
        self.addCleanup(self.sandbox.close)

    def assert_clean(self):
        output = self.sandbox._control([
            "ps", "-aq", "--filter", f"label=bpcagent.sandbox.session={self.sandbox.session_id}",
        ]).stdout
        self.assertEqual(output.strip(), "")

    def test_readonly_input_and_writable_output(self):
        result = self.sandbox.execute("cat input/sample.txt; printf result > output/result.txt; printf bad > input/sample.txt")
        self.assertNotEqual(result["exit_code"], 0)
        self.assertIn("Read-only file system", result["stderr"])
        self.assertEqual(self.input.read_text(), "synthetic input")
        self.assertEqual((self.task / "output" / "result.txt").read_text(), "result")
        self.assert_clean()

    def test_python_privileges_network_and_rootfs(self):
        code = '''import os, socket
from pathlib import Path
assert os.getuid() != 0
status = Path('/proc/self/status').read_text()
assert 'CapEff:\\t0000000000000000' in status
assert 'NoNewPrivs:\\t1' in status
# Docker's Linux VM may expose inactive tunnel interfaces even with network=none.
# Verify there are no IPv4 routes and an external connection is unreachable.
assert len(Path('/proc/net/route').read_text().splitlines()) == 1
sock = socket.socket(); sock.settimeout(1)
try:
    sock.connect(('1.1.1.1', 443))
except OSError as exc:
    import errno
    assert exc.errno == errno.ENETUNREACH, exc
else:
    raise AssertionError('Network unexpectedly available')
for target in ['/tmp/disallowed', '/workspace/disallowed']:
    try:
        Path(target).write_text('bad')
    except OSError:
        pass
    else:
        raise AssertionError('Unexpected writable path')
assert os.environ['TMPDIR'] == '/workspace/tmp'
assert 'LLM_API_KEY' not in os.environ
print('checks passed')
'''
        with patch.dict(os.environ, {"LLM_API_KEY": "synthetic-secret"}):
            result = self.sandbox.execute("python -c " + shlex.quote(code))
        self.assertEqual(result["exit_code"], 0, result)
        self.assertIn("checks passed", result["stdout"])
        self.assert_clean()

    def test_files_persist_but_shell_state_does_not(self):
        self.assertEqual(self.sandbox.execute("printf data > output/saved; cd scripts; export BPC_TEMP_STATE=old")["exit_code"], 0)
        result = self.sandbox.execute('pwd; cat output/saved; test -z "$BPC_TEMP_STATE"')
        self.assertEqual(result["exit_code"], 0, result)
        self.assertIn("/workspace", result["stdout"])
        self.assertIn("data", result["stdout"])
        self.assertNotEqual(*[r["container_name"] for r in self.sandbox.executions])
        self.assert_clean()

    def test_other_task_and_host_files_are_invisible(self):
        other = self.root / "other_task"
        other.mkdir()
        sentinel = other / "secret.txt"
        sentinel.write_text("not exposed")
        result = self.sandbox.execute(
            f"test ! -e {shlex.quote(str(sentinel))} && test ! -e /workspace/other_task && test ! -e /var/run/docker.sock"
        )
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(sentinel.read_text(), "not exposed")

    def test_timeout_removes_container_and_next_command_can_run(self):
        self.sandbox.config = SandboxConfig(command_timeout=1.5)
        result = self.sandbox.execute("sleep 20")
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["cleanup_status"], "removed")
        self.assert_clean()
        self.sandbox.config = SandboxConfig()
        self.assertEqual(self.sandbox.execute("echo recovered")["exit_code"], 0)

    def test_background_child_cannot_modify_files_after_call(self):
        result = self.sandbox.execute("(sleep 2; echo late > output/late) & echo finished")
        self.assertEqual(result["exit_code"], 0, result)
        self.assert_clean()
        time.sleep(2.2)
        self.assertFalse((self.task / "output" / "late").exists())

    def test_output_is_bounded(self):
        self.sandbox.config = SandboxConfig(max_output_bytes=128)
        result = self.sandbox.execute("python -c 'import sys; print(\"x\"*10000); print(\"y\"*10000,file=sys.stderr)'")
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(len(result["stdout"]), 128)
        self.assertEqual(len(result["stderr"]), 128)
        self.assertTrue(result["stdout_truncated"])
        self.assertTrue(result["stderr_truncated"])

    def test_resource_and_mount_settings_reach_the_engine(self):
        original = self.sandbox._stop_and_remove
        settings = []

        def inspect_then_remove(name):
            settings.append(json.loads(self.sandbox._control(["inspect", name]).stdout)[0])
            original(name)

        with patch.object(self.sandbox, "_stop_and_remove", side_effect=inspect_then_remove):
            result = self.sandbox.execute("echo configured")
        self.assertEqual(result["exit_code"], 0, result)
        host = settings[0]["HostConfig"]
        self.assertEqual(host["NetworkMode"], "none")
        self.assertTrue(host["ReadonlyRootfs"])
        self.assertEqual(host["Memory"], 512 * 1024 * 1024)
        self.assertEqual(host["NanoCpus"], 1000000000)
        self.assertEqual(host["PidsLimit"], 64)
        self.assertEqual(host["CapDrop"], ["ALL"])
        mounts = {m["Destination"]: m["RW"] for m in settings[0]["Mounts"]}
        self.assertEqual(mounts, {"/workspace/input": False, "/workspace/scripts": True,
                                  "/workspace/output": True, "/workspace/tmp": True})
        self.assert_clean()

    def test_oom_is_reported_and_container_is_removed(self):
        self.sandbox.config = SandboxConfig(memory_mb=64)
        result = self.sandbox.execute("python -c 'data = bytearray(256 * 1024 * 1024)'")
        self.assertNotEqual(result["exit_code"], 0)
        self.assertTrue(result["oom_killed"], result)
        self.assert_clean()

    def test_workspace_growth_aborts_and_cleans(self):
        self.sandbox.config = SandboxConfig(max_workspace_bytes=1024)
        with self.assertRaisesRegex(SandboxError, "limit"):
            self.sandbox.execute("python -c 'from pathlib import Path; Path(\"output/large\").write_bytes(b\"x\"*4096)'; sleep 5")
        self.assert_clean()
        self.assertTrue(self.sandbox.broken)

    def test_unit_demo_with_synthetic_data_and_validation_retry(self):
        self._exercise_patient_demo()

    def test_patient_task_with_real_agent_and_sandbox(self):
        self._exercise_patient_demo(use_task=True)

    def _exercise_patient_demo(self, *, use_task=False):
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        task_dir = self.root / "patient_demo"
        state = {
            "meta": {"patient_id": "SYNTHETIC", "cutoff_day": 1},
            "evidence_refs": {"E-test": {}},
            "observations": {"tumor_markers": [
                {"tumor_marker_id": "m1", "analyte_raw": "Total Testosterone", "unit_raw": "nmol/L", "value_raw": "1"},
                {"tumor_marker_id": "m2", "analyte_raw": "PSA", "unit_raw": None, "value_raw": "2"},
                {"tumor_marker_id": "m3", "analyte_raw": "PSA", "unit_raw": "nmol/L", "value_raw": "3"},
            ]},
        }
        for marker in state["observations"]["tumor_markers"]:
            marker.update(event_day=1, available_day=1, evidence_refs=["E-test"])
        bound = SimpleNamespace(patient_id="SYNTHETIC", cancer_id=None, max_cutoff_day=1,
                                get_patient_state=Mock(return_value=state),
                                get_evidence=Mock(return_value={"E-test": {"sources": []}}))

        def factory(**kwargs):
            self.assertEqual(Path(kwargs["work_dir"]), task_dir / "input")
            store = PatientArtifactStore(kwargs["work_dir"])
            return PatientStateTool(bound, store), PatientEvidenceTool(bound, store)

        def tool(name, args, identity):
            return LLMResponse(tool_calls=[ToolCall(id=identity, name=name, arguments=json.dumps(args))],
                               reasoning_content="Synthetic test action.", finish_reason="tool_calls")

        def shell(value, identity):
            script = f'''import json
from pathlib import Path
source = Path('/workspace/input/patient_state_day_1_001.json')
state = json.loads(source.read_text())
markers = state['observations']['tumor_markers']
for i, marker in enumerate(markers):
    marker['unit_assessment'] = dict(status='accepted' if i == 0 else 'missing_unit',
        analyte_type='total_testosterone' if i == 0 else 'psa', effective_unit='nmol/L' if i == 0 else None,
        candidate_unit=None, correction_id=None, supporting_evidence_refs=['E-test'],
        conflicting_evidence_refs=[], reason='Synthetic review of source and visible history.')
    marker['unit_assessment']['evidence_observations'] = [dict(
        tumor_marker_id=marker['tumor_marker_id'], event_day=marker['event_day'],
        analyte_raw=marker['analyte_raw'], value_raw=marker['value_raw'], unit_raw=marker['unit_raw'],
        normal_range_lower_raw=None, normal_range_upper_raw=None, evidence_refs=marker['evidence_refs'])]
markers[0]['normalization'] = dict(status='normalized', value={value}, unit='ng/dL', factor=28.842, reason=None)
markers[1]['normalization'] = dict(status='unresolved', value=None, unit=None, factor=None, reason='Missing unit')
markers[2]['unit_assessment'].update(status='inferred_correction', effective_unit='ug/L',
    reason='Synthetic fixture: infer a mass-unit label from the visible assay context.')
markers[2]['normalization'] = dict(status='normalized', value=3, unit='ng/mL', factor=1, reason=None)
Path('/workspace/output/tumor_markers_standardized.json').write_text(json.dumps(dict(schema_version='{UNIT_SCHEMA_VERSION}', rules_version='{UNIT_RULES_VERSION}', source_state=source.name, meta=state['meta'], tumor_markers=markers)))
'''
            response = tool("run_shell", {"command": "cat > /workspace/scripts/standardize_tm.py <<'PY'\n" + script + "PY\n"}, identity)
            response.tool_calls.append(ToolCall(id=identity + "-execute", name="run_shell", arguments=json.dumps({
                "command": "/usr/local/bin/python /workspace/scripts/standardize_tm.py",
            })))
            return response

        model = Mock()
        report = LLMResponse(content="Normalized 2, unresolved 1.", reasoning_content="Results checked.", finish_reason="stop")
        model.invoke.side_effect = [tool("get_patient_state", {"cutoff_day": 1}, "state"),
                                   tool("get_patient_evidence", {"cutoff_day": 1, "evidence_refs": ["E-test"]}, "evidence"),
                                   shell(999, "bad"), report, shell(28.842, "fixed"), report]
        agent = None
        with patch("bpcagent.agents.unit_patient_state.create_patient_tools", side_effect=factory):
            if use_task:
                def create_agent(**kwargs):
                    nonlocal agent, task_dir
                    task_dir = kwargs["work_dir"]
                    agent = UnitPatientStateAgent(**kwargs, llm=model)
                    return agent

                task = PatientStateTask(PatientStateTaskConfig(
                    data_dir=self.root, workspace_root=self.root / "tasks",
                    agent_config=Config(max_tool_iterations=4),
                ))
                with patch("bpcagent.tasks.patient_state.UnitPatientStateAgent", side_effect=create_agent):
                    task_result = task.run(patient_id="SYNTHETIC", cutoff_day=1)
                self.assertTrue(task_result.finished, task_result.info)
                self.assertEqual(task_result.task_dir, task_dir)
                self.assertEqual(task_result.log_path, agent.log_path.resolve())
                self.assertEqual(set(task_result.artifacts), {"state", "evidence", "normalized", "script", "unit_inputs"})
                self.assertTrue(all(path.is_file() for path in task_result.artifacts.values()))
                result = task_result.agent_result
            else:
                agent = UnitPatientStateAgent(patient_id="SYNTHETIC", cutoff_day=1, data_dir="unused",
                                              work_dir=task_dir, config=Config(max_tool_iterations=4),
                                              sandbox_config=SandboxConfig(), llm=model)
                result = agent.run()
        self.assertTrue(result.finished, result.info)
        changes = result.info["unit_changes"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["status"], "inferred_correction")
        self.assertEqual(changes[0]["source_location"], "observations.tumor_markers[2]")
        self.assertEqual(changes[0]["before"]["unit"], "nmol/L")
        self.assertEqual(changes[0]["after"]["normalization"]["value"], 3)
        self.assertIn("observations.tumor_markers[2]", result.output["text"])
        self.assertEqual([v["status"] for v in agent.validation_attempts], ["failed", "passed"])
        self.assertTrue((task_dir / "scripts" / "standardize_tm.py").is_file())
        self.assertEqual(json.loads((task_dir / "input" / "patient_state_day_1_001.json").read_text()), state)
        log = json.loads(agent.log_path.read_text())
        self.assertTrue(log["finished"])
        self.assertTrue(log["sandbox"]["image_id"].startswith("sha256:"))
        self.assertEqual(log["sandbox"]["active_containers"], [])
        self.assertTrue(all(r["cleanup_status"] == "removed" for r in log["sandbox"]["executions"]))
        self.assertFalse((task_dir / "run_result.json").exists())


if __name__ == "__main__":
    unittest.main()
