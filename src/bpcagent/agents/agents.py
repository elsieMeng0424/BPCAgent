"""Agent base class and the Basic / ReAct execution strategies."""

from abc import ABC, abstractmethod
import json
from typing import Iterator, Optional

from .config import Config
from .messages import LLMResponse, Message, ToolCall
from .models import MyLLM
from ..tools import ToolRegistry, ToolResult
from .exceptions import AgentException, ConfigException


def _reject_constant(value):
    raise ValueError("JSON 参数不能包含非有限数值")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 参数不能包含重复键")
        result[key] = value
    return result


_ARGUMENT_DECODER = json.JSONDecoder(parse_constant=_reject_constant, object_pairs_hook=_unique_pairs)


def _decode_arguments(text: str) -> dict:
    """解析完整工具参数，拒绝重复键、非有限数值及非对象输入。"""
    try:
        arguments = _ARGUMENT_DECODER.decode(text)
    except (ValueError, RecursionError) as exc:
        raise ValueError("工具参数必须是完整且合法的 JSON 对象") from exc
    if not isinstance(arguments, dict):
        raise ValueError("工具参数必须是 JSON 对象")
    return arguments


class Agent(ABC):
    """
    抽象的Agent基类
    """
    def __init__(
        self, 
        name: str, 
        llm: Optional[MyLLM] = None,
        sys_prompt: Optional[str] = None, 
        config: Optional[Config] = None    
    ):
        """默认根据 config 创建模型；显式 llm 用于模拟或自定义模型。"""
        self.name = name
        self.sys_prompt = sys_prompt
        if llm is None:
            self.llm = MyLLM(config=config)
            # 模型已完成配置解析，Agent 复用同一快照。
            self.config = self.llm.config
        else:
            self.llm = llm
            self.config = Config.resolve(config)
        self._history: list[Message] = []

    @abstractmethod
    def run(self, input: str, **kwargs) -> str:
        """运行Agent"""
        pass

    def add_message(self, message: Message):
        """添加消息到历史记录"""
        self._history.append(message)

    def clear_history(self):
        """清空历史记录"""
        self._history.clear()

    def get_history(self) -> list[Message]:
        """获取历史记录"""
        return self._history.copy()
    
    def _invoke_response(self, messages, **kwargs) -> LLMResponse:
        """检查响应结构和结束状态，工具调用允许没有文本。"""
        response = self.llm.invoke(messages, **kwargs)
        if not isinstance(response, LLMResponse):
            raise AgentException("invalid_response: 模型必须返回 LLMResponse")
        if response.finish_reason not in {"stop", "tool_calls"}:
            raise AgentException(f"invalid_response: 模型未正常结束: {response.finish_reason}")
        if response.tool_calls:
            return response
        if response.finish_reason == "tool_calls":
            raise AgentException("invalid_response: 工具调用结束原因缺少调用列表")
        if not response.content or not response.content.strip():
            raise AgentException("empty_response: 模型未返回有效文本")
        return response

    def _execute_tool_call(self, call: ToolCall) -> ToolResult:
        """解析参数后通过注册表执行，所有结果沿用模型的调用 ID。"""
        try:
            arguments = _decode_arguments(call.arguments)
        except ValueError as exc:
            return ToolResult.failure(call.name, "invalid_arguments", str(exc), call_id=call.id)
        if self.tool_registry is None:
            raise AgentException("未配置工具注册表")
        return self.tool_registry.execute_tool(call.name, arguments, call_id=call.id)

    def _run_with_tools(self, messages: list, input: str, max_tool_iterations: int, **kwargs) -> str:
        """共享原生工具循环；每轮请求一次模型，全部结果配对后才进入下一轮。"""
        if "tools" in kwargs:
            raise ConfigException("Agent 的 tools 必须来自工具注册表")
        enabled = getattr(self, "enable_tool_calling", True)
        tools = self.tool_registry.to_openai_tools() if enabled and self.tool_registry else []
        if not tools and {"tool_choice", "parallel_tool_calls"}.intersection(kwargs):
            raise ConfigException("工具选择参数需要已启用的非空工具注册表")
        options = {**kwargs, "tools": tools} if tools else kwargs
        for _ in range(max_tool_iterations):
            response = self._invoke_response(messages, **options)
            # record
            # print(response.content if not response.tool_calls else response)
            if not response.tool_calls:
                messages.append(Message(response.content, "assistant").to_dict())
                self.add_message(Message(input, "user"))
                self.add_message(Message(response.content, "assistant"))
                return response.content
            if not tools:
                raise AgentException("invalid_response: 未启用工具却收到工具调用")
            # 先验证整批调用，再执行，避免发现缺失或重复 ID 前已执行部分工具。
            assistant = Message(response.content, "assistant", tool_calls=response.tool_calls)
            messages.append(assistant.to_dict())
            for call in response.tool_calls:
                result = self._execute_tool_call(call)
                messages.append(Message(result.model_dump_json(), "tool", tool_call_id=call.id).to_dict())
        raise AgentException("step_limit_exceeded: 已达到模型交互轮次上限，尚未获得最终回答")

    def __str__(self) -> str:
        return f"Agent(name={self.name}, model={self.llm.model})"


class BasicAgent(Agent):
    """
    支持简单对话的Agent
    """
    def __init__(
        self, 
        name: str,
        llm: Optional[MyLLM] = None,
        sys_prompt: Optional[str] = None,
        config: Optional[Config] = None,
        tool_registry: Optional['ToolRegistry'] = None,
        enable_tool_calling: bool = True
    ):
        """
        根据配置初始化 BasicAgent，默认自动创建模型。
        """
        super().__init__(name, llm, sys_prompt, config)
        self.tool_registry = tool_registry
        self.enable_tool_calling = enable_tool_calling and tool_registry is not None
        print(f"✅ Agent {name} 初始化完成，工具调用: {'启用' if self.enable_tool_calling else '禁用'}")

    def run(self, input: str, max_tool_iterations: Optional[int] = None, **kwargs) -> str:
        """
        实现简单的对话逻辑，支持工具调用
        """
        run_config = self.config if max_tool_iterations is None else Config.resolve(
            self.config, overrides={"max_tool_iterations": max_tool_iterations}, environ={}
        )
        max_tool_iterations = run_config.max_tool_iterations
        print(f"🤖 {self.name} 正在解决问题: {input}")

        # 构建消息列表并添加系统消息
        messages = []
        sys_prompt = self._get_system_prompt()
        messages.append({"role": "system", "content": sys_prompt})

        # 添加历史消息
        for history_message in self._history:
            messages.append(history_message.to_dict())

        # 添加当前用户消息
        messages.append({"role": "user", "content": input})

        # 普通问答与工具调用共用响应检查和成功历史写入逻辑。
        return self._run_with_tools(messages, input, max_tool_iterations, **kwargs)

    def _get_system_prompt(self) -> str:
        """
        获取系统消息
        """
        # 构造基础消息
        base_prompt = self.sys_prompt or "You are a helpful AI assistant."

        return base_prompt
    
    def run_using_stream(self, input: str, **kwargs) -> Iterator[str]:
        """
        流式运行
        """
        print(f"🌊 {self.name} 开始流式处理: {input}")
        if self.enable_tool_calling and self.tool_registry and self.tool_registry.list_tools():
            raise ConfigException("流式入口仅支持普通文本，工具调用请使用 run")

        # 获取消息
        messages = []
        if self.sys_prompt:
            messages.append({"role": "system", "content": self.sys_prompt})
        for message in self._history:
            messages.append(message.to_dict())
        messages.append({"role": "user", "content": input})

        # 生成流式调用
        full_response = ""
        for chunk in self.llm.think(messages, **kwargs):
            full_response += chunk
            yield chunk

        if not full_response.strip():
            raise AgentException("empty_response: 模型未返回有效文本")
        # 保存到历史记录
        self.add_message(Message(input, "user"))
        self.add_message(Message(full_response, "assistant"))

    # --- 添加一些简便的工具处理方式 ---
    def add_tool(self, tool) -> None:
        """向工具注册表添加一个工具"""
        if not self.tool_registry:
            self.tool_registry = ToolRegistry()
            self.enable_tool_calling = True

        self.tool_registry.register_tool(tool)
        print(f"🔧 工具 '{tool.name}' 已添加至 Agent {self.name} 中")

    def has_tools(self) -> bool:
        """检查是否有可用工具"""
        return self.enable_tool_calling and self.tool_registry is not None
    
    def remove_tool(self, name: str) -> None:
        """移除工具"""
        if self.tool_registry:
            self.tool_registry.unregister_tool(name)
            print(f"🔧 工具 '{name}' 已从 Agent {self.name} 中移除")

    def list_tools(self) -> list:
        """列出所有可用工具"""
        if self.tool_registry:
            return self.tool_registry.list_tools()
        return []


# ReAct 的策略提示；工具和观察结果通过原生消息提供。
REACT_PROMPT_TEMPLATE = """
分析任务所需的信息，按需调用提供的工具。
结合工具的实际返回结果决定继续调用还是回答；不要编造工具结果。
信息充分时直接给出最终回答，执行失败时根据错误信息修正参数或说明限制。
"""

class ReActAgent(Agent):
    """
    推理-行动智能体的实现
    """
    def __init__(
        self, 
        name: str,
        llm: Optional[MyLLM] = None,
        sys_prompt: Optional[str] = None,
        config: Optional[Config] = None,
        tool_registry: Optional[ToolRegistry] = None,
        max_steps: Optional[int] = None,
        prompt_template: Optional[str] = None
    ):
        """
        初始化ReAct智能体
        """
        super().__init__(name, llm, sys_prompt, config)

        # 如果没有提供工具注册表就自己注册一个
        self.tool_registry = tool_registry if tool_registry else ToolRegistry()

        # 如果没有提示词模版就使用默认模版
        self.prompt_template = prompt_template if prompt_template else REACT_PROMPT_TEMPLATE

        if max_steps is not None:
            self.config = Config.resolve(self.config, overrides={"max_steps": max_steps}, environ={})
        self.max_steps = self.config.max_steps
        self.current_history: list[dict] = []
        print(f"✅ ReAct Agent {name} 初始化完成，最大步数: {self.max_steps}")

    def run(self, input: str, **kwargs) -> str:
        """建立本次消息历史，使用共享原生工具循环完成推理与行动。"""
        self.current_history = []
        print(f"\n🤖 ReAct Agent {self.name} 开始处理问题: {input}")
        prompt = "\n\n".join(part for part in (self.sys_prompt, self.prompt_template) if part)
        self.current_history.append(Message(prompt, "system").to_dict())
        self.current_history.append(Message(input, "user").to_dict())
        return self._run_with_tools(self.current_history, input, self.max_steps, **kwargs)
