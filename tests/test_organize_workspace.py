"""The historical log organizer is conservative, lossless and repeatable."""

import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


spec = importlib.util.spec_from_file_location(
    "organize_workspace", Path(__file__).resolve().parents[1] / "scripts/organize_workspace.py"
)
organizer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(organizer)


class OrganizeWorkspaceTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def make_run(self, number, *, name="test", record=None):
        run = self.root / f"run_{number:032x}"
        logs = run / "logs"
        logs.mkdir(parents=True)
        path = logs / f"{name}_20261027_120000_000000_{number:032x}.json"
        path.write_text(json.dumps(record if record is not None else
                                   {"history": [], "transcript": []}), encoding="utf-8")
        return run, path

    def test_dry_run_and_apply_preserve_content_and_workflow(self):
        test_run, log = self.make_run(1)
        original = log.read_bytes()
        workflow, _ = self.make_run(2)
        result = {"mode": "demo", "patient_id": "example", "cutoff_day": 50,
                  "status": "completed", "turns": [{}, {}]}
        (workflow / "workflow_result.json").write_text(json.dumps(result))
        before = sorted(self.root.rglob("*"))
        self.assertEqual(organizer.organize(self.root)["test_runs"], 1)
        self.assertEqual(before, sorted(self.root.rglob("*")))
        self.assertEqual(organizer.organize(self.root, apply=True)["moved"], 1)
        self.assertFalse(test_run.exists())
        archived = self.root / "_test_runs" / log.relative_to(self.root)
        self.assertEqual(archived.read_bytes(), original)
        self.assertTrue(workflow.is_dir())
        self.assertIn(workflow.name, (self.root / "RUN_INDEX.md").read_text())
        self.assertEqual(organizer.organize(self.root, apply=True)["moved"], 0)
        self.assertEqual(len(list((self.root / "_test_runs").glob("move_plan_*"))), 1)

    def test_unknown_names_invalid_json_patient_data_and_symlinks_stay(self):
        self.make_run(1, name="PatientStateAgent")
        _, invalid = self.make_run(2)
        invalid.write_text("not json")
        self.make_run(3, record={"history": ["GENIE-example"], "transcript": []})
        linked, log = self.make_run(4)
        target = self.root / "external.json"
        log.rename(target)
        log.symlink_to(target)
        self.assertEqual(organizer.organize(self.root)["test_runs"], 0)
        self.assertTrue(linked.is_dir())

    def test_collision_rejects_entire_batch(self):
        first, _ = self.make_run(1)
        second, _ = self.make_run(2)
        (self.root / "_test_runs" / second.name).mkdir(parents=True)
        with self.assertRaises(FileExistsError):
            organizer.organize(self.root, apply=True)
        self.assertTrue(first.is_dir())
        self.assertTrue(second.is_dir())

    def test_symlink_archive_is_rejected(self):
        first, _ = self.make_run(1)
        (self.root / "_test_runs").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            organizer.organize(self.root, apply=True)
        self.assertTrue(first.is_dir())
