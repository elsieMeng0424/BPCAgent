"""Offline regression checks for dated experiment logs."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from bpcagent import BasicAgent, LLMResponse
from bpcagent.log_paths import LOG_TIMEZONE, create_run_directory, dated_directory, new_log_path


class LogPathTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        previous = Path.cwd()
        os.chdir(temp.name)
        self.addCleanup(os.chdir, previous)

    def test_partition_uses_beijing_time_not_host_timezone(self):
        timestamp = datetime(2026, 10, 26, 17, tzinfo=timezone.utc)
        directory = dated_directory("nested/log", timestamp=timestamp)
        self.assertEqual(directory, Path("nested/log/2026-10-27"))
        self.assertTrue(directory.is_dir())
        with self.assertRaises(ValueError):
            dated_directory("log", timestamp=datetime(2026, 10, 27))

    def test_same_timestamp_does_not_overwrite_and_name_cannot_escape(self):
        with patch("bpcagent.log_paths.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 27, tzinfo=LOG_TIMEZONE)
            paths = [new_log_path("../../agent", suffix=".log") for _ in range(2)]
            clock.now.assert_called_with(LOG_TIMEZONE)
        self.assertNotEqual(*paths)
        for path in paths:
            self.assertEqual(path.parent.name, "logs")
            self.assertEqual(path.parents[2], Path("workspace/2026-10-27"))
            self.assertTrue(path.name.startswith("______agent_20261027_"))
            with path.open("x") as handle:
                handle.write("preserved")
        self.assertEqual(paths[0].read_text(), "preserved")

    def test_date_rollover_and_filename_use_same_clock_read(self):
        with patch("bpcagent.log_paths.datetime") as clock:
            clock.now.side_effect = [
                datetime(2026, 10, 26, 23, 59, 59, tzinfo=LOG_TIMEZONE),
                datetime(2026, 10, 27, tzinfo=LOG_TIMEZONE),
            ]
            before, after = new_log_path("test"), new_log_path("test")
            self.assertEqual(clock.now.call_count, 2)
        self.assertEqual(before.parents[2].name, "2026-10-26")
        self.assertEqual(after.parents[2].name, "2026-10-27")
        self.assertTrue(before.name.startswith("test_20261026_"))
        self.assertTrue(after.name.startswith("test_20261027_"))

    def test_basic_agent_saves_success_and_failure_in_dated_directory(self):
        llm = Mock()
        llm.invoke.side_effect = [
            LLMResponse(content="answer", finish_reason="stop"),
            LLMResponse(content="truncated", finish_reason="length"),
        ]
        agent = BasicAgent("test", llm=llm)
        with patch("bpcagent.log_paths.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 27, tzinfo=LOG_TIMEZONE)
            self.assertTrue(agent.run("first").finished)
            first_path = agent.log_path
            self.assertFalse(agent.run("second").finished)
        self.assertEqual(agent.log_path.parents[2], Path("workspace/2026-10-27"))
        self.assertEqual(agent.log_path.parent.name, "logs")
        self.assertNotEqual(first_path, agent.log_path)
        self.assertEqual(json.loads(first_path.read_text())["transcript"][-1]["content"], "answer")
        self.assertEqual(json.loads(agent.log_path.read_text())["transcript"][-1]["content"], "truncated")

    def test_explicit_experiment_keeps_logs_together_across_midnight(self):
        with patch("bpcagent.log_paths.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 26, 23, 59, tzinfo=LOG_TIMEZONE)
            directory = create_run_directory()
            clock.now.return_value = datetime(2026, 10, 27, tzinfo=LOG_TIMEZONE)
            first = new_log_path("patient", run_dir=directory)
            second = new_log_path("patient", run_dir=directory)
        self.assertEqual(directory.parent.name, "2026-10-26")
        self.assertEqual(first.parent, directory / "logs")
        self.assertEqual(second.parent, first.parent)
        self.assertNotEqual(first, second)
        self.assertFalse(Path("log").exists())
