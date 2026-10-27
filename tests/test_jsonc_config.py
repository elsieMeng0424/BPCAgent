"""Inline configuration comments must not corrupt URLs, strings or diagnostics."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from bpcagent.workflow.config import WorkflowConfiguration, read_object, strip_line_comments


class JSONCConfigTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_comments_preserve_urls_quotes_backslashes_and_unicode(self):
        values = {
            "url": "https://gateway.invalid/v1//route?q=//text",
            "quote": 'embedded "quote" // not a comment',
            "path": "C:\\folder\\",
            "escaped": '\\\"// still in string',
            "text": "中文说明 // data",
        }
        source = "// 开头注释\r\n{\r\n" + ", // 字段注释\r\n".join(
            json.dumps(key) + ": " + json.dumps(value) for key, value in values.items()
        ) + "\r\n} // EOF comment"
        path = self.root / "config.jsonc"
        path.write_text(source, encoding="utf-8")
        self.assertEqual(read_object(path), values)
        stripped = strip_line_comments(source)
        self.assertEqual(len(stripped), len(source))
        self.assertEqual([i for i, char in enumerate(stripped) if char in "\r\n"],
                         [i for i, char in enumerate(source) if char in "\r\n"])

    def test_plain_json_and_secrets_remain_strict(self):
        path = self.root / "secrets.local.json"
        path.write_text('{"KEY": "https://value//unchanged"}')
        self.assertEqual(read_object(path)["KEY"], "https://value//unchanged")
        path.write_text('{"KEY": "DO-NOT-ECHO"} // comment')
        with self.assertRaises(ValueError) as error:
            read_object(path)
        self.assertNotIn("DO-NOT-ECHO", str(error.exception))

    def test_error_coordinates_preserved_without_echoing_values(self):
        path = self.root / "bad.jsonc"
        path.write_text('// comment\n{\n "key": "DO-NOT-ECHO" BROKEN // ignored\n}')
        with self.assertRaisesRegex(ValueError, "line 3") as error:
            read_object(path)
        self.assertNotIn("DO-NOT-ECHO", str(error.exception))

    def test_non_supported_json_extensions_still_fail(self):
        path = self.root / "bad.jsonc"
        for source in ('{"key": 1,}', '{/* block */ "key": 1}', "{'key': 1}", '// comment\n[]'):
            with self.subTest(source=source):
                path.write_text(source)
                with self.assertRaises(ValueError):
                    read_object(path)

    def test_repository_configs_load_without_real_secrets(self):
        repository = Path(__file__).resolve().parents[1]
        for name in ("workflow", "providers", "models", "agents", "pipeline"):
            filename = f"{name}.jsonc"
            (self.root / filename).write_text((repository / "config" / filename).read_text())
        cfg = WorkflowConfiguration(self.root / "workflow.jsonc")
        self.assertEqual(cfg.workflow_options({})["mode"], "demo")
        configs = cfg.model_configs(environ={"DEEPSEEK_API_KEY": "test-only"})["agent_configs"]
        self.assertEqual(len(configs), 4)
        for value in configs.values():
            self.assertEqual(value.api_key.get_secret_value(), "test-only")


if __name__ == "__main__":
    unittest.main()
