# ---消息系统---

from pydantic import BaseModel, ConfigDict, Field, model_validator
from datetime import datetime
from typing import Any, Dict, Literal, Optional

# 定义消息中角色的类型
MessageRole = Literal["user", "assistant", "system", "tool"]


class ToolCall(BaseModel):
    """模型提出的函数调用；参数保留原始 JSON，执行前再校验。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, pattern=r"\S")
    name: str = Field(min_length=1, pattern=r"\S")
    arguments: str

    def to_dict(self) -> Dict[str, Any]:
        """转换为 Chat Completions 的函数调用格式。"""
        return {"id": self.id, "type": "function",
                "function": {"name": self.name, "arguments": self.arguments}}


class LLMResponse(BaseModel):
    """一次模型响应，文本为空时仍可携带有效工具调用。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    content: Optional[str] = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str

    @model_validator(mode="after")
    def validate_call_ids(self):
        """同一响应中的调用 ID 必须唯一。"""
        ids = [call.id for call in self.tool_calls]
        if len(ids) != len(set(ids)):
            raise ValueError("同一响应中的工具调用 ID 不能重复")
        return self


class Message(BaseModel):
    """
    消息类
    """
    content: Optional[str]
    role: MessageRole
    timestamp: datetime = Field(default_factory=datetime.now)
    metadata: Optional[Dict[str, Any]] = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: Optional[str] = Field(None, min_length=1, pattern=r"\S")

    def __init__(self, content: Optional[str], role: MessageRole, **kwargs):
        super().__init__(
            content=content,
            role=role,
            **kwargs,
        )

    @model_validator(mode="after")
    def validate_role_fields(self):
        """限制调用字段的角色，工具结果必须能够与请求配对。"""
        if self.tool_calls and self.role != "assistant":
            raise ValueError("只有 assistant 消息可以携带 tool_calls")
        if (self.role == "tool") != (self.tool_call_id is not None):
            raise ValueError("只有 tool 消息必须携带 tool_call_id")
        if self.content is None and not (self.role == "assistant" and self.tool_calls):
            raise ValueError("只有携带工具调用的 assistant 消息可以省略文本")
        ids = [call.id for call in self.tool_calls]
        if len(ids) != len(set(ids)):
            raise ValueError("同一消息中的工具调用 ID 不能重复")
        return self

    def to_dict(self) -> Dict[str, Any]:
        """
        转换为OpenAI API的格式（字典）
        """
        result = {
            "role": self.role,
            "content": self.content
        }
        if self.tool_calls:
            result["tool_calls"] = [call.to_dict() for call in self.tool_calls]
        if self.tool_call_id is not None:
            result["tool_call_id"] = self.tool_call_id
        return result
    
    def __str__(self) -> str:
        return f"[{self.role}] {self.content}"
