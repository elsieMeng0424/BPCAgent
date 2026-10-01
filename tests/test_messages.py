"""验证原生工具消息的序列化和角色约束。"""

import unittest
from pydantic import ValidationError
from bpcagent import LLMResponse, Message, ToolCall


class MessageTests(unittest.TestCase):
    def setUp(self):
        self.call = ToolCall(id="call_1", name="calculate", arguments='{"input":"2+3*4"}')

    def test_tool_request_and_result_pair(self):
        request = Message(None, "assistant", tool_calls=[self.call]).to_dict()
        self.assertEqual(request, {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {
                "name": "calculate", "arguments": '{"input":"2+3*4"}'}}]})
        result = Message("14", "tool", tool_call_id="call_1").to_dict()
        self.assertEqual(result, {"role": "tool", "content": "14", "tool_call_id": "call_1"})
        self.assertEqual(Message("你好", "user").to_dict(), {"role": "user", "content": "你好"})

    def test_role_constraints(self):
        for content, role, kwargs in (
            (None, "user", {}), (None, "assistant", {}), ("14", "tool", {}),
            ("hi", "user", {"tool_calls": [self.call]}),
            ("hi", "assistant", {"tool_call_id": "call_1"}),
            ("14", "tool", {"tool_call_id": " "}),
        ):
            with self.subTest(role=role, kwargs=kwargs), self.assertRaises(ValidationError):
                Message(content, role, **kwargs)

    def test_call_ids_and_argument_types(self):
        for change in ({"id": ""}, {"id": " "}, {"name": ""}, {"arguments": {}}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                ToolCall(**{**self.call.model_dump(), **change})
        with self.assertRaises(ValidationError):
            LLMResponse(tool_calls=[self.call, self.call], finish_reason="tool_calls")
        with self.assertRaises(ValidationError):
            Message(None, "assistant", tool_calls=[self.call, self.call])
