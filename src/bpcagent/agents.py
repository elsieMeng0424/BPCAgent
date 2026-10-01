"""Agent base class and the Basic / ReAct execution strategies."""

from abc import ABC, abstractmethod
import re
import json
from typing import Iterator, List, Optional, Tuple

from .config import Config
from .messages import Message
from .models import MyLLM
from .tools import ToolRegistry, ToolResult
from .exceptions import AgentException


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


def _decode_arguments(text: str, start: int = 0) -> tuple[dict, int]:
    """读取完整 JSON 对象及外层右括号，支持嵌套数组和字符串中的括号。"""
    while start < len(text) and text[start].isspace():
        start += 1
    try:
        arguments, end = _ARGUMENT_DECODER.raw_decode(text, start)
    except (ValueError, RecursionError) as exc:
        raise ValueError("工具参数必须是完整且合法的 JSON 对象") from exc
    if not isinstance(arguments, dict):
        raise ValueError("工具参数必须是 JSON 对象")
    while end < len(text) and text[end].isspace():
        end += 1
    if end >= len(text) or text[end] != "]":
        raise ValueError("JSON 参数之后必须紧接工具调用的结束括号 ]")
    return arguments, end + 1


class Agent(ABC):
    """
    抽象的Agent基类
    """
    def __init__(
        self, 
        name: str, 
        llm: MyLLM, 
        sys_prompt: Optional[str] = None, 
        config: Optional[Config] = None    
    ):
        self.name = name
        self.llm = llm
        self.sys_prompt = sys_prompt
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
    
    def _invoke_text(self, messages, **kwargs) -> str:
        """执行模型请求并拒绝空响应，失败时不写入成功会话历史。"""
        response = self.llm.invoke(messages, **kwargs)
        if not isinstance(response, str) or not response.strip():
            raise AgentException("empty_response: 模型未返回有效文本")
        return response

    def _execute_tool_call(self, name: str, arguments: dict, error: str | None = None) -> ToolResult:
        """文本解析错误与工具执行结果使用同一结果协议。"""
        if error:
            return ToolResult.failure(name, "invalid_arguments", error)
        if self.tool_registry is None:
            raise AgentException("未配置工具注册表")
        return self.tool_registry.execute_tool(name, arguments)

    def __str__(self) -> str:
        return f"Agent(name={self.name}, model={self.llm.model})"


class BasicAgent(Agent):
    """
    支持简单对话的Agent
    """
    def __init__(
        self, 
        name: str,
        llm: MyLLM,
        sys_prompt: Optional[str] = None,
        config: Optional[Config] = None,
        tool_registry: Optional['ToolRegistry'] = None,
        enable_tool_calling: bool = True
    ):
        """
        初始化BasicAgent
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
            messages.append({"role": history_message.role, "content": history_message.content})

        # 添加当前用户消息
        messages.append({"role": "user", "content": input})

        # record
        print(messages)

        # 没有启动工具调用
        if not self.enable_tool_calling:
            response = self._invoke_text(messages, **kwargs)
            self.add_message(Message(input, "user"))
            self.add_message(Message(response, "assistant"))
            print(f"✅ Agent {self.name} 响应完成")
            return response
        
        # 多轮工具调用
        return self._run_with_tools(messages, input, max_tool_iterations, **kwargs)

    def _get_system_prompt(self) -> str:
        """
        获取系统消息
        """
        # 构造基础消息
        base_prompt = self.sys_prompt or "You are a helpful AI assistant."

        # 如果没有工具调用
        if not self.enable_tool_calling or not self.tool_registry:
            return base_prompt

        # 获取工具描述
        tools_description = self.tool_registry.get_descriptions()
        if not tools_description or tools_description == "暂无可用工具":
            return base_prompt
        
        # 构造工具提示词
        tools_prompt = "\n\n## 可用工具\n"
        tools_prompt += "你可以使用以下工具来帮助回答问题:\n"
        tools_prompt += tools_description + "\n"

        tools_prompt += "\n## 工具调用格式\n"
        tools_prompt += "当需要使用工具时，请使用以下格式:\n"
        tools_prompt += "`[TOOL_CALL:{tool_name}:{parameters}]`\n"
        tools_prompt += "参数必须为 JSON 对象，例如:`[TOOL_CALL:calculate:{\"input\":\"2+3*4\"}]`\n\n"
        tools_prompt += "工具调用结果会自动插入到对话中，然后你可以基于结果继续回答。\n"

        # 合并为系统提示词
        sys_prompt = base_prompt + tools_prompt
        return sys_prompt
    
    def run_using_stream(self, input: str, **kwargs) -> Iterator[str]:
        """
        流式运行
        """
        print(f"🌊 {self.name} 开始流式处理: {input}")

        # 获取消息
        messages = []
        if self.sys_prompt:
            messages.append({"role": "system", "content": self.sys_prompt})
        for message in self._history:
            messages.append({"role": message.role, "content":message.content})
        messages.append({"role": "user", "content": input})

        # 生成流式调用
        full_response = ""
        for chunk in self.llm.think(messages, **kwargs):
            full_response += chunk
            yield chunk

        # 保存到历史记录
        self.add_message(Message(input, "user"))
        self.add_message(Message(full_response, "assistant"))

    # --- 工具调用的核心逻辑 ---

    def _run_with_tools(self, messages: list, input: str, max_tool_iterations: int, **kwargs) -> str:
        """每轮至多请求一次模型；工具错误占用轮次，上限后立即停止。"""
        for _ in range(max_tool_iterations):
            response = self._invoke_text(messages, **kwargs)

            # record
            print(response)
            
            calls = self._parse_tool_calls(response)
            if not calls:
                self.add_message(Message(input, "user"))
                self.add_message(Message(response, "assistant"))
                return response
            results = [self._execute_tool_call(**call) for call in calls]
            messages.append({"role": "assistant", "content": response})
            messages.append({
                "role": "user",
                "content": json.dumps({"tool_results": [result.model_dump(mode="json") for result in results]}, ensure_ascii=False),
            })
        raise AgentException("step_limit_exceeded: 已达到工具交互轮次上限，尚未获得最终回答")

    def _parse_tool_calls(self, text: str) -> list[dict]:
        """提取文本中的工具调用；JSON 解码器负责参数边界，解析失败不猜测输入。"""
        calls = []
        cursor = 0
        while True:
            start = text.find("[TOOL_CALL", cursor)
            if start < 0:
                return calls
            match = re.match(r"\[TOOL_CALL:([a-zA-Z0-9_-]+):", text[start:])
            if not match:
                calls.append({"name": "<invalid>", "arguments": {}, "error": "工具调用格式应为 [TOOL_CALL:name:{JSON参数}]"})
                return calls
            name = match.group(1)
            try:
                arguments, cursor = _decode_arguments(text, start + match.end())
            except ValueError as exc:
                calls.append({"name": name, "arguments": {}, "error": str(exc)})
                return calls
            calls.append({"name": name, "arguments": arguments})

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


# ReAct智能体的提示词模版(英文)
REACT_PROMPT_TEMPLATE = """
You are an AI assistant with both reasoning and action capabilities.
You are able to analyze problems step by step, determine what information is required, and retrieve that information by invoking appropriate tools in order to produce accurate and well-supported answers.

## Available Tools
{tools}

## Workflow
You MUST strictly follow the response format below.
Only ONE step may be executed per response.

Thought: Analyze the problem, identify the required information, and formulate a research or reasoning strategy.
Action: Select and invoke the appropriate tool to obtain information using one of the following formats:
- `{{tool_name}}[{{JSON object arguments}}]` — invoke a tool with arguments matching its schema.
- `Finish[final conclusion]` — use this only when you are confident that sufficient information has been obtained to answer the question.

## Important Rules
1. Every response MUST include both a Thought and an Action section.
2. Tool invocation MUST follow tool_name[JSON object]. Example: calculate[{{"input":"2+3*4"}}].
3. Use Finish ONLY when you are confident that the available information is sufficient to answer the question.
4. If the returned information is insufficient, continue using the same tool with different parameters or invoke other relevant tools.

## Current Task
**Question:** {question}

## Execution History
{history}

Begin your reasoning and actions now.
"""

class ReActAgent(Agent):
    """
    推理-行动智能体的实现
    """
    def __init__(
        self, 
        name: str,
        llm: MyLLM,
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
        self.current_history: List[str] = []
        print(f"✅ ReAct Agent {name} 初始化完成，最大步数: {self.max_steps}")

    def run(self, input: str, **kwargs) -> str:
        """
        运行智能体
        """
        # 运行前清空历史，步数置零
        self.current_history = []
        current_step = 0

        print(f"\n🤖 ReAct Agent {self.name} 开始处理问题: {input}")

        # 推理-行动循环
        while current_step < self.max_steps:
            current_step += 1
            print(f"\n--- 第 {current_step} 步 ---")

            # 构建提示词
            tools = self.tool_registry.get_descriptions()
            history = "\n".join(self.current_history)
            prompt = self.prompt_template.format(
                tools = tools,
                question = input,
                history = history
            )

            # 每步至多请求一次模型；本阶段将系统提示词传入并保持单次运行历史。
            messages = []
            if self.sys_prompt:
                messages.append({"role": "system", "content": self.sys_prompt})
            messages.append({"role": "user", "content": prompt})
            response = self._invoke_text(messages, **kwargs)
            thought, action = self._parse_response(response)
            if thought:
                print(f"🤔 思考: {thought}")
            if not action:
                result = ToolResult.failure("<invalid>", "invalid_arguments", "缺少 Action 行")
                self.current_history.append(f"Observation: {result.model_dump_json()}")
                continue
            if action.startswith("Finish["):
                final_answer = self._parse_final_answer(action)
                if not final_answer.strip():
                    raise AgentException("invalid_response: Finish 必须包含非空最终回答")
                self.add_message(Message(input, "user"))
                self.add_message(Message(final_answer, "assistant"))
                return final_answer
            match = re.match(r"([a-zA-Z0-9_-]+)\[", action)
            if not match:
                result = ToolResult.failure("<invalid>", "invalid_arguments", "Action 应为 name[JSON对象]")
            else:
                name = match.group(1)
                try:
                    arguments, end = _decode_arguments(action, match.end())
                    if action[end:].strip():
                        raise ValueError("每个 Action 只能包含一次工具调用")
                except ValueError as exc:
                    result = self._execute_tool_call(name, {}, error=str(exc))
                else:
                    result = self._execute_tool_call(name, arguments)
            self.current_history.append(f"Action: {action}")
            self.current_history.append(f"Observation: {result.model_dump_json()}")
        raise AgentException("step_limit_exceeded: 已达到 ReAct 步数上限，尚未获得最终回答")

    def _parse_response(self, response: str) -> Tuple[Optional[str], Optional[str]]:
        """提取思考文本和完整 Action，允许 JSON 参数跨行。"""
        thought_match = re.search(r"^Thought:\s*(.*)", response, re.MULTILINE)
        action_match = re.search(r"^Action:[ \t]*(.*)\Z", response, re.MULTILINE | re.DOTALL)
        thought = thought_match.group(1).strip() if thought_match else None
        action = action_match.group(1).strip() if action_match else None
        return thought, action

    def _parse_final_answer(self, action: str) -> str:
        """完成动作必须完整匹配，不能以名称前缀冒充完成。"""
        match = re.fullmatch(r"Finish\[(.*)\]", action, re.DOTALL)
        if not match:
            raise AgentException("invalid_response: Finish 格式不完整")
        return match.group(1)
