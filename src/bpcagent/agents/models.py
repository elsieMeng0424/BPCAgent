"""使用统一配置入口的 OpenAI 兼容模型客户端。"""

from copy import deepcopy
from typing import Any, Dict, Iterator, List, Optional

from openai import OpenAI

from .config import Config
from .exceptions import ConfigException, LLMException
from .messages import LLMResponse, ToolCall

_UNSET = object()


class MyLLM:
    """模型客户端，显式构造参数优先于 Config 和环境变量。"""

    _REQUEST_FIELDS = {"temperature", "max_tokens", "timeout"}
    # 配置字段由 Config 管理，仅允许三个请求级字段被单次调用覆盖。
    _RESERVED_FIELDS = (set(Config.model_fields) - _REQUEST_FIELDS) | {
        "messages", "stream", "config",
    }

    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None,
                 base_url: Optional[str] = None, temperature=_UNSET,
                 max_tokens=_UNSET, timeout=_UNSET, *, config: Optional[Config] = None,
                 **kwargs):
        """解析配置并创建客户端，额外关键字参数作为模型请求的默认参数。"""
        overrides = {name: value for name, value in (
            ("model", model), ("api_key", api_key), ("base_url", base_url),
        ) if value is not None}
        # 区分未传入与显式 None，后者表示省略可选请求参数。
        for name, value in (("temperature", temperature), ("max_tokens", max_tokens),
                            ("timeout", timeout)):
            if value is not _UNSET:
                overrides[name] = value
        self._check_request_options(kwargs)
        if {"tools", "tool_choice", "parallel_tool_calls"}.intersection(kwargs):
            raise ConfigException("工具请求参数应在 invoke 时传入，不能作为模型构造默认值")
        self.config = Config.resolve(config, overrides=overrides)
        self.config.require_model()
        self._request_defaults = deepcopy(kwargs)
        self._client = OpenAI(
            api_key=self.config.api_key.get_secret_value(),
            base_url=self.config.base_url,
            timeout=self.config.timeout,
        )

    @property
    def model(self):
        """提供 Agent 所需的模型名称，其余配置统一通过 config 访问。"""
        return self.config.model

    @classmethod
    def _check_request_options(cls, options):
        """拒绝通过请求参数改写连接设置、Agent 配置或接口控制参数。"""
        conflicts = cls._RESERVED_FIELDS.intersection(options)
        if conflicts:
            raise ConfigException("以下参数不允许作为模型请求覆盖项：" + ", ".join(sorted(conflicts)))

    def _build_request_params(self, overrides, *, stream):
        """合并并复制请求参数，校验单次覆盖，不修改原配置或重新读取环境。"""
        self._check_request_options(overrides)
        options = deepcopy({**self._request_defaults, **overrides})
        extra_body = options.get("extra_body") or {}
        if {"tools", "tool_choice", "parallel_tool_calls", "functions", "function_call"}.intersection(extra_body):
            raise ConfigException("工具请求参数必须显式传入，不能放在 extra_body 中")
        if {"functions", "function_call"}.intersection(options):
            raise ConfigException("仅支持 tools 和 tool_choice 工具接口")
        settings = {name: options.pop(name) for name in self._REQUEST_FIELDS if name in options}
        config = Config.resolve(self.config, overrides=settings, environ={})
        options.update(model=config.model, timeout=config.timeout, stream=stream)
        for name in ("temperature", "max_tokens"):
            value = getattr(config, name)
            if value is not None:
                options[name] = value
        return options

    def think(self, messages: List[Dict[str, Any]], **kwargs) -> Iterator[str]:
        """逐段返回流式文本；配置错误在开始迭代生成器时抛出。"""
        options = self._build_request_params(kwargs, stream=True)
        if {"tools", "tool_choice", "parallel_tool_calls"}.intersection(options):
            raise ConfigException("think 仅支持文本流，请使用 invoke 执行工具调用")
        print(f"🧠 正在调用 {self.model} 模型...")
        try:
            response = self._client.chat.completions.create(messages=messages, **options)
            print("✅ 大语言模型流式响应成功:")
            for chunk in response:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if getattr(choice.delta, "tool_calls", None):
                    raise LLMException("文本流不支持工具调用增量")
                if getattr(choice.delta, "refusal", None):
                    raise LLMException("模型拒绝了请求")
                reason = getattr(choice, "finish_reason", None)
                if reason is not None and reason != "stop":
                    raise LLMException(f"文本流未正常结束: {reason}")
                content = getattr(chunk.choices[0].delta, "content", None)
                if not content:
                    continue
                print(content, end="", flush=True)
                yield content
            print()
        except Exception as e:
            print(f"❌ 调用LLM API时发生错误: {e}")
            raise LLMException(f"LLM流式调用失败: {str(e)}") from e

    def invoke(self, messages: List[Dict[str, Any]], *, tools=None, tool_choice=None,
               **kwargs) -> LLMResponse:
        """返回文本、原生工具调用和结束原因，不在模型层执行工具。"""
        if tools:
            kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        elif tool_choice is not None or "parallel_tool_calls" in kwargs:
            raise ConfigException("工具选择参数需要非空 tools")
        options = self._build_request_params(kwargs, stream=False)
        print(f"🧠 正在调用 {self.model} 模型...")
        try:
            response = self._client.chat.completions.create(messages=messages, **options)
            print("✅ 大语言模型非流式响应成功:")
            if not response.choices:
                raise LLMException("invalid_response: 模型未返回任何候选响应")
            choice = response.choices[0]
            message = choice.message
            if getattr(message, "refusal", None):
                raise LLMException("模型拒绝了请求")
            calls = []
            for call in message.tool_calls or []:
                if call.type != "function":
                    raise LLMException(f"不支持的工具调用类型: {call.type}")
                calls.append(ToolCall(id=call.id, name=call.function.name,
                                      arguments=call.function.arguments))
            return LLMResponse(content=message.content, tool_calls=calls,
                               finish_reason=choice.finish_reason)
        except Exception as e:
            print(f"❌ 调用LLM API时发生错误: {e}")
            raise LLMException(f"LLM非流式调用失败: {str(e)}") from e
