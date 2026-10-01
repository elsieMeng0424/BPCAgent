"""工具参数、OpenAI 函数工具定义以及统一注册和执行入口。"""

from abc import ABC, abstractmethod
import inspect
import json
import math
import re
from typing import Any, Callable, Generic, Literal, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .exceptions import ToolException


class ToolArgs(BaseModel):
    """工具参数模型；默认严格校验类型、拒绝未知字段，并校验默认值。"""

    model_config = ConfigDict(
        extra="forbid", strict=True, validate_default=True,
        allow_inf_nan=False, hide_input_in_errors=True,
    )


ArgsT = TypeVar("ArgsT", bound=ToolArgs)


def _json_data(value: Any) -> Any:
    """复制原生 JSON 数据；不自动把任意对象、元组或非字符串键转成文本。"""
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is list:
        return [_json_data(item) for item in value]
    if type(value) is dict and all(type(key) is str for key in value):
        return {key: _json_data(item) for key, item in value.items()}
    raise ValueError("工具结果必须是原生 JSON 数据，数值必须有限，字典键必须为字符串")


class ToolError(BaseModel):
    """供程序判断的错误类别及简短说明。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    code: Literal["tool_not_found", "invalid_arguments", "execution_error", "serialization_error"]
    message: str


class ToolResult(BaseModel):
    """一次工具执行的结果；是否成功由 status 判断，成功数据可以为 None。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    call_id: str = Field(default_factory=lambda: f"call_{uuid4().hex}", min_length=1)
    tool_name: str
    status: Literal["success", "error"]
    data: Any = None
    error: ToolError | None = None

    @field_validator("data", mode="before")
    @classmethod
    def validate_data(cls, value):
        """结果必须能无损表示为 JSON 数据。"""
        return _json_data(value)

    @model_validator(mode="after")
    def validate_status(self):
        """禁止成功携带错误或失败携带成功数据。"""
        if self.status == "success" and self.error is not None:
            raise ValueError("成功结果不能携带错误")
        if self.status == "error" and (self.error is None or self.data is not None):
            raise ValueError("失败结果必须包含错误，且 data 必须为 None")
        return self

    @classmethod
    def failure(cls, tool_name: str, code: str, message: str, *, call_id: str | None = None):
        """统一创建失败结果，保留已分配的调用 ID。"""
        return cls(
            tool_name=tool_name, status="error", error=ToolError(code=code, message=message[:500]),
            **({"call_id": call_id} if call_id is not None else {}),
        )


class Tool(ABC, Generic[ArgsT]):
    """工具定义；run 接收校验后的参数模型，框架通过 ToolRegistry 执行。"""

    args_schema: type[ArgsT]

    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description

    @abstractmethod
    def run(self, parameters: ArgsT) -> Any:
        """返回原生 JSON 数据；执行失败应抛出异常，不能伪装成成功文本。"""
        raise NotImplementedError

    def to_openai_tool(self) -> dict[str, Any]:
        """导出 Chat Completions 函数工具定义；本地严格校验与服务端 strict 分开。

        当前使用 strict=False，保留参数默认值与可省略字段的本地语义。
        此方法不调用服务，也不承诺任意 Schema 都满足服务端严格模式子集。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args_schema.model_json_schema(),
                "strict": False,
            },
        }


class FunctionTool(Tool[ArgsT]):
    """将同步函数包装成普通工具，按参数字段名传入关键字参数。"""

    def __init__(self, name: str, description: str, func: Callable[..., Any], *, args_schema: type[ArgsT]):
        super().__init__(name, description)
        if not isinstance(args_schema, type) or not issubclass(args_schema, ToolArgs):
            raise ToolException("args_schema 必须是 ToolArgs 的子类")
        if not callable(func) or inspect.iscoroutinefunction(func) or inspect.isgeneratorfunction(func):
            raise ToolException("函数工具必须是普通同步函数")
        try:
            signature = inspect.signature(func)
            if any(param.kind in (param.POSITIONAL_ONLY, param.VAR_POSITIONAL, param.VAR_KEYWORD)
                   for param in signature.parameters.values()):
                raise ValueError("函数参数必须按名称显式声明")
            signature.bind(**dict.fromkeys(args_schema.model_fields))
        except (TypeError, ValueError) as exc:
            raise ToolException("函数签名必须能接收参数模型的全部字段，且不能缺少必填参数") from exc
        self.func = func
        self.args_schema = args_schema

    def run(self, parameters: ArgsT) -> Any:
        """保留嵌套模型的类型，避免 model_dump 将其提前转为字典。"""
        return self.func(**{name: getattr(parameters, name) for name in self.args_schema.model_fields})


class ToolRegistry:
    """用同一个字典管理对象工具和函数工具，集中校验、执行和包装结果。"""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register_tool(self, tool: Tool, *, replace: bool = False) -> None:
        """注册工具；名称遵循 OpenAI 函数名称规则，重名必须显式替换。"""
        if not isinstance(tool, Tool):
            raise ToolException("只能注册 Tool 对象")
        if not isinstance(tool.name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", tool.name):
            raise ToolException("工具名称必须为 1 到 64 个字母、数字、下划线或连字符")
        if tool.name == "Finish":
            raise ToolException("Finish 是 ReAct 的完成动作名称，不能注册为工具")
        schema = getattr(tool, "args_schema", None)
        if not isinstance(schema, type) or not issubclass(schema, ToolArgs):
            raise ToolException("工具必须声明 ToolArgs 子类作为 args_schema")
        if inspect.iscoroutinefunction(tool.run) or inspect.isgeneratorfunction(tool.run):
            raise ToolException("当前工具入口仅支持同步执行")
        try:
            tool.to_openai_tool()
        except Exception as exc:
            raise ToolException("工具参数模型无法生成 JSON Schema") from exc
        if tool.name in self._tools and not replace:
            raise ToolException(f"工具 '{tool.name}' 已注册；替换时指定 replace=True")
        self._tools[tool.name] = tool
        print(f"✅ 工具 '{tool.name}' 已注册。")

    def register_function(self, name: str, description: str, func: Callable[..., Any], *,
                          args_schema: type[ToolArgs], replace: bool = False) -> None:
        """包装函数后走同一注册入口，不另行存储函数。"""
        self.register_tool(FunctionTool(name, description, func, args_schema=args_schema), replace=replace)

    def get_tool(self, name: str) -> Tool | None:
        """按名称查找已注册工具。"""
        return self._tools.get(name)

    def unregister_tool(self, name: str) -> None:
        """删除工具；名称不存在时不执行额外操作。"""
        if self._tools.pop(name, None) is not None:
            print(f"🗑️ 工具 '{name}' 已注销。")
        else:
            print(f"⚠️ 工具 '{name}' 不存在。")

    def list_tools(self) -> list[str]:
        """按注册顺序列出名称。"""
        return list(self._tools)

    def to_openai_tools(self) -> list[dict[str, Any]]:
        """返回可用于 Chat Completions tools 参数的函数定义列表。"""
        return [tool.to_openai_tool() for tool in self._tools.values()]

    def get_descriptions(self) -> str:
        """文本 Agent 使用同一份工具定义，包含完整参数 Schema。"""
        return json.dumps(self.to_openai_tools(), ensure_ascii=False) if self._tools else "暂无可用工具"

    def execute_tool(self, name: str, arguments: dict[str, Any], *, call_id: str | None = None) -> ToolResult:
        """校验后执行一次；失败不自动重试，结果序列化失败也不会重新执行工具。"""
        identity = call_id if call_id is not None else f"call_{uuid4().hex}"
        if not isinstance(identity, str) or not identity:
            raise ToolException("call_id 必须是非空字符串")
        tool = self.get_tool(name)
        if tool is None:
            print(f"❌ 工具 '{name}' 未注册。")
            return ToolResult.failure(name, "tool_not_found", f"工具 '{name}' 未注册", call_id=identity)
        if not isinstance(arguments, dict):
            print(f"❌ 工具 '{name}' 参数必须为 JSON 对象，未执行。")
            return ToolResult.failure(name, "invalid_arguments", "工具参数必须为 JSON 对象", call_id=identity)
        try:
            parameters = tool.args_schema.model_validate(_json_data(arguments), strict=True)
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(map(str, error['loc'])) or 'arguments'}: {error['msg']}"
                for error in exc.errors(include_input=False, include_url=False, include_context=False)
            )
            print(f"❌ 工具 '{name}' 参数校验失败，未执行: {details}")
            return ToolResult.failure(name, "invalid_arguments", details, call_id=identity)
        except Exception:
            print(f"❌ 工具 '{name}' 参数校验失败，未执行。")
            return ToolResult.failure(name, "invalid_arguments", "参数校验失败", call_id=identity)
        try:
            print(f"🔧 正在执行工具 '{name}'，参数: {parameters}")
            data = tool.run(parameters)
            if inspect.isawaitable(data):
                if inspect.iscoroutine(data):
                    data.close()
                raise ToolException("当前同步入口不能执行异步工具")
        except Exception as exc:
            print(f"❌ 工具 '{name}' 执行失败: {exc}")
            return ToolResult.failure(name, "execution_error", str(exc), call_id=identity)
        try:
            result = ToolResult(call_id=identity, tool_name=name, status="success", data=data)
        except (ValidationError, ValueError, TypeError, RecursionError):
            print(f"❌ 工具 '{name}' 已执行，但返回值无法序列化。")
            return ToolResult.failure(name, "serialization_error", "工具已执行，但返回值不是可序列化的原生 JSON 数据", call_id=identity)
        print(f"✅ 工具 '{name}' 执行结果: {result.data}")
        return result
