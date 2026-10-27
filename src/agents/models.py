"""使用统一配置入口的 OpenAI 兼容模型客户端。"""

from copy import deepcopy
from typing import Any, Dict, Iterator, List, Optional

from openai import OpenAI

from .config import Config
from .exceptions import ConfigException, LLMException
from .messages import LLMResponse, ToolCall

_UNSET = object()


class LLM:
    """模型客户端，显式构造参数优先于 Config 和环境变量。"""

    _REQUEST_FIELDS = {"temperature", "max_tokens", "timeout", "reasoning_effort"}
    # 配置字段由 Config 管理，仅允许三个请求级字段被单次调用覆盖。
    _RESERVED_FIELDS = (set(Config.model_fields) - _REQUEST_FIELDS) | {
        "messages", "stream", "config",
    }

    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None,
                 base_url: Optional[str] = None, temperature=_UNSET,
                 max_tokens=_UNSET, timeout=_UNSET, reasoning_effort=_UNSET, *, config: Optional[Config] = None,
                 **kwargs):
        """解析配置并创建客户端，额外关键字参数作为模型请求的默认参数。"""
        overrides = {name: value for name, value in (
            ("model", model), ("api_key", api_key), ("base_url", base_url),
        ) if value is not None}
        # 区分未传入与显式 None，后者表示省略可选请求参数。
        for name, value in (("temperature", temperature), ("max_tokens", max_tokens),
                            ("timeout", timeout), ("reasoning_effort", reasoning_effort)):
            if value is not _UNSET:
                overrides[name] = value
        self._check_request_options(kwargs)
        if {"tools", "tool_choice", "parallel_tool_calls"}.intersection(kwargs):
            raise ConfigException("Pass tool request parameters to invoke, not as model constructor defaults")
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
            raise ConfigException("These parameters cannot be overridden in model requests: " + ", ".join(sorted(conflicts)))

    def _build_request_params(self, overrides, *, stream):
        """合并并复制请求参数，校验单次覆盖，不修改原配置或重新读取环境。"""
        self._check_request_options(overrides)
        options = deepcopy({**self._request_defaults, **overrides})
        extra_body = options.get("extra_body") or {}
        if {"tools", "tool_choice", "parallel_tool_calls", "functions", "function_call"}.intersection(extra_body):
            raise ConfigException("Pass tool request parameters explicitly, not inside extra_body")
        if {"functions", "function_call"}.intersection(options):
            raise ConfigException("Only the tools and tool_choice interfaces are supported")
        settings = {name: options.pop(name) for name in self._REQUEST_FIELDS if name in options}
        config = Config.resolve(self.config, overrides=settings, environ={})
        options.update(model=config.model, timeout=config.timeout, stream=stream)
        for name in ("temperature", "max_tokens", "reasoning_effort"):
            value = getattr(config, name)
            if value is not None:
                options[name] = value
        if config.reasoning_effort not in {None, "none"}:
            for name in ("temperature", "top_p", "top_logprobs", "logprobs"):
                options.pop(name, None)
        if config.reasoning_effort is not None and "max_tokens" in options and config.default_provider != "deepseek":
            options["max_completion_tokens"] = options.pop("max_tokens")
        return options

    def think(self, messages: List[Dict[str, Any]], **kwargs) -> Iterator[str]:
        """逐段返回流式文本；配置错误在开始迭代生成器时抛出。"""
        if self.config.api_mode == "responses":
            raise ConfigException("Responses streaming is not implemented; use invoke/run")
        options = self._build_request_params(kwargs, stream=True)
        if {"tools", "tool_choice", "parallel_tool_calls"}.intersection(options):
            raise ConfigException("think supports text streaming only; use invoke for tool calls")
        print(f"🧠 Calling model {self.model}...")
        try:
            response = self._client.chat.completions.create(messages=messages, **options)
            print("✅ LLM streaming response received:")
            for chunk in response:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if getattr(choice.delta, "tool_calls", None):
                    raise LLMException("Text streaming does not support tool call deltas")
                if getattr(choice.delta, "refusal", None):
                    raise LLMException("The model refused the request")
                reason = getattr(choice, "finish_reason", None)
                if reason is not None and reason != "stop":
                    raise LLMException(f"The text stream did not finish normally: {reason}")
                content = getattr(chunk.choices[0].delta, "content", None)
                if not content:
                    continue
                print(content, end="", flush=True)
                yield content
            print()
        except Exception as e:
            print(f"❌ LLM API call failed: {e}")
            raise LLMException(f"LLM streaming call failed: {str(e)}") from e

    def invoke(self, messages: List[Dict[str, Any]], *, tools=None, tool_choice=None,
               **kwargs) -> LLMResponse:
        """返回文本、可选推理内容、原生工具调用和结束原因，不执行工具。"""
        if tools:
            kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        elif tool_choice is not None or "parallel_tool_calls" in kwargs:
            raise ConfigException("Tool selection parameters require nonempty tools")
        options = self._build_request_params(kwargs, stream=False)
        if self.config.api_mode == "responses":
            from .responses import invoke_responses
            return invoke_responses(self._client, messages, options)
        print(f"🧠 Calling model {self.model}...")
        try:
            response = self._client.chat.completions.create(messages=messages, **options)
            print("✅ LLM response received:")
            if not response.choices:
                raise LLMException("invalid_response: the model returned no response choices")
            choice = response.choices[0]
            message = choice.message
            if getattr(message, "refusal", None):
                raise LLMException("The model refused the request")
            calls = []
            for call in message.tool_calls or []:
                if call.type != "function":
                    raise LLMException(f"Unsupported tool call type: {call.type}")
                calls.append(ToolCall(id=call.id, name=call.function.name,
                                      arguments=call.function.arguments))
            return LLMResponse(content=message.content, tool_calls=calls,
                               reasoning_content=getattr(message, "reasoning_content", None),
                               finish_reason=choice.finish_reason)
        except Exception as e:
            print(f"❌ LLM API call failed: {e}")
            raise LLMException(f"LLM call failed: {str(e)}") from e
