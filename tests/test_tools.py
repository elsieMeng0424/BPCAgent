"""验证工具的注册、Schema、输入约束和统一结果，不调用模型或网络。"""

import json
import unittest
from unittest.mock import Mock

from pydantic import Field, ValidationError

from bpcagent import CalculatorTool, FunctionTool, Tool, ToolArgs, ToolError, ToolRegistry, ToolResult
from bpcagent.agents.exceptions import ToolException


class Options(ToolArgs):
    enabled: bool = True
    precision: int = Field(2, ge=0, le=6)


class ValuesArgs(ToolArgs):
    values: list[float]
    options: Options = Field(default_factory=Options)
    label: str = "values"


class EchoTool(Tool[ValuesArgs]):
    args_schema = ValuesArgs

    def __init__(self, name="echo"):
        super().__init__(name, "返回数组、嵌套选项和标签")

    def run(self, parameters):
        return parameters.model_dump()


class EmptyArgs(ToolArgs):
    pass


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.registry = ToolRegistry()

    def test_object_and_function_use_same_registry_and_defaults(self):
        tool = EchoTool()
        self.registry.register_tool(tool)
        def echo(values, options, label):
            self.assertIsInstance(options, Options)
            return {"values": values, "options": options.model_dump(), "label": label}
        self.registry.register_function("function_echo", "相同功能", echo, args_schema=ValuesArgs)
        self.assertIsInstance(self.registry.get_tool("function_echo"), FunctionTool)
        args = {"values": [1.0, 2.0], "label": '逗号,括号[]和引号"'}
        a = self.registry.execute_tool("echo", args, call_id="given-id")
        b = self.registry.execute_tool("function_echo", args)
        self.assertEqual(a.status, "success")
        self.assertEqual(a.data, b.data)
        self.assertEqual(a.call_id, "given-id")
        self.assertNotEqual(a.call_id, b.call_id)
        self.assertEqual(a.data["options"], {"enabled": True, "precision": 2})

    def test_schema_is_openai_chat_completions_function_format(self):
        self.registry.register_tool(EchoTool())
        definition = self.registry.to_openai_tools()[0]
        self.assertEqual(definition["type"], "function")
        function = definition["function"]
        self.assertEqual(function["name"], "echo")
        self.assertIs(function["strict"], False)
        schema = function["parameters"]
        self.assertEqual(schema["required"], ["values"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["values"]["items"]["type"], "number")
        self.assertFalse(schema["$defs"]["Options"]["additionalProperties"])
        self.assertEqual(json.loads(self.registry.get_descriptions()), [definition])
        definition["function"]["name"] = "changed"
        self.assertEqual(self.registry.to_openai_tools()[0]["function"]["name"], "echo")

    def test_invalid_arguments_never_execute_tool(self):
        tool = EchoTool()
        tool.run = Mock(wraps=tool.run)
        self.registry.register_tool(tool)
        invalid = [
            {}, {"values": "1,2"}, {"values": ["1"]}, {"values": [True]},
            {"values": [], "extra": 1}, {"values": [], "options": {"enabled": "bad"}},
            {"values": [], "options": {"enabled": "false"}},
            {"values": [], "options": {"precision": -1}},
            {"values": [], "options": {"unknown": 1}}, {"values": [float("nan")]},
            "text", [], None,
        ]
        for args in invalid:
            with self.subTest(args=args):
                result = self.registry.execute_tool("echo", args)
                self.assertEqual(result.status, "error")
                self.assertEqual(result.error.code, "invalid_arguments")
        tool.run.assert_not_called()

    def test_input_copy_and_defaults_are_isolated(self):
        class MutateTool(EchoTool):
            def run(self, parameters):
                parameters.values.append(99.0)
                parameters.options.enabled = False
                return parameters.model_dump()
        self.registry.register_tool(MutateTool())
        arguments = {"values": [1.0], "options": {"enabled": True}}
        self.registry.execute_tool("echo", arguments)
        self.assertEqual(arguments, {"values": [1.0], "options": {"enabled": True}})
        self.assertTrue(ValuesArgs(values=[]).options.enabled)

    def test_duplicate_name_and_explicit_replace_across_tool_kinds(self):
        self.registry.register_tool(EchoTool())
        def echo(values, options, label):
            return label
        with self.assertRaises(ToolException):
            self.registry.register_function("echo", "覆盖", echo, args_schema=ValuesArgs)
        self.registry.register_function("echo", "覆盖", echo, args_schema=ValuesArgs, replace=True)
        self.assertEqual(self.registry.list_tools(), ["echo"])
        self.assertEqual(self.registry.execute_tool("echo", {"values": []}).data, "values")
        self.registry.unregister_tool("echo")
        self.assertEqual(self.registry.list_tools(), [])
        self.assertIsNone(self.registry.get_tool("echo"))

    def test_unknown_tool_has_structured_error(self):
        result = self.registry.execute_tool("missing", {}, call_id="id")
        self.assertEqual(result.error.code, "tool_not_found")
        self.assertEqual(result.call_id, "id")
        self.assertIsNone(result.data)

    def test_function_signature_errors_at_registration(self):
        async def asynchronous():
            return 1
        def generator():
            yield 1
        def positional(values, /):
            return values
        for func in (lambda unrelated: None, lambda *args: None, lambda **kwargs: None,
                     asynchronous, generator, positional):
            with self.subTest(func=func), self.assertRaises(ToolException):
                self.registry.register_function("bad", "错误签名", func, args_schema=ValuesArgs)
        self.assertEqual(self.registry.list_tools(), [])

    def test_invalid_tool_name(self):
        for name in ("", "a b", "a" * 65):
            with self.subTest(name=name), self.assertRaises(ToolException):
                self.registry.register_tool(EchoTool(name))

    def test_json_results_and_error_text_can_be_success(self):
        for data in (None, False, 0, 1.5, "错误只是普通文本", [1, {"a": True}], {"a": []}):
            with self.subTest(data=data):
                self.registry.register_function("value", "结果", lambda: data, args_schema=EmptyArgs, replace=True)
                result = self.registry.execute_tool("value", {})
                self.assertEqual(result.status, "success")
                self.assertEqual(result.data, data)
                self.assertIsNone(result.error)
                self.assertEqual(json.loads(result.model_dump_json())["data"], data)

    def test_unserializable_result_is_error_without_reexecution(self):
        cycle = []; cycle.append(cycle)
        for data in (object(), {1: "value"}, (1, 2), float("inf"), float("nan"), cycle):
            calls = []
            def value():
                calls.append(1)
                return data
            self.registry.register_function("value", "结果", value, args_schema=EmptyArgs, replace=True)
            result = self.registry.execute_tool("value", {})
            self.assertEqual(result.error.code, "serialization_error")
            self.assertEqual(calls, [1])

    def test_runtime_exception_and_invalid_result_invariants(self):
        calls = []
        def fail():
            calls.append(1)
            raise RuntimeError("failed")
        self.registry.register_function("fail", "失败", fail, args_schema=EmptyArgs)
        result = self.registry.execute_tool("fail", {})
        self.assertEqual(result.error.code, "execution_error")
        self.assertEqual(calls, [1])
        with self.assertRaises(ValidationError):
            ToolResult(tool_name="bad", status="error")
        with self.assertRaises(ValidationError):
            ToolResult(tool_name="bad", status="success", error=ToolError(code="execution_error", message="bad"))

    def test_calculator_success_and_failure(self):
        self.registry.register_tool(CalculatorTool())
        self.assertEqual(self.registry.execute_tool("calculate", {"input": "2+3*4"}).data, 14)
        for args in ({}, {"input": ""}, {"input": "   "}, {"input": 1}):
            self.assertEqual(self.registry.execute_tool("calculate", args).error.code, "invalid_arguments")
        for expression in ("1/0", "not-valid(", "'text'"):
            self.assertEqual(self.registry.execute_tool("calculate", {"input": expression}).error.code, "execution_error")


if __name__ == "__main__":
    unittest.main()
