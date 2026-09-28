"""使用统一配置入口的 OpenAI 兼容模型客户端。"""

from copy import deepcopy
from typing import Dict, Iterator, List, Optional

from openai import OpenAI

from .config import Config
from .exceptions import ConfigException, LLMException

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
        settings = {name: options.pop(name) for name in self._REQUEST_FIELDS if name in options}
        config = Config.resolve(self.config, overrides=settings, environ={})
        options.update(model=config.model, timeout=config.timeout, stream=stream)
        for name in ("temperature", "max_tokens"):
            value = getattr(config, name)
            if value is not None:
                options[name] = value
        return options

    def think(self, messages: List[Dict[str, str]], **kwargs) -> Iterator[str]:
        """逐段返回流式文本；配置错误在开始迭代生成器时抛出。"""
        options = self._build_request_params(kwargs, stream=True)
        print(f"🧠 正在调用 {self.model} 模型...")
        try:
            response = self._client.chat.completions.create(messages=messages, **options)
            print("✅ 大语言模型流式响应成功:")
            for chunk in response:
                if not chunk.choices:
                    continue
                content = getattr(chunk.choices[0].delta, "content", None)
                if not content:
                    continue
                print(content, end="", flush=True)
                yield content
            print()
        except Exception as e:
            print(f"❌ 调用LLM API时发生错误: {e}")
            raise LLMException(f"LLM流式调用失败: {str(e)}") from e

    def invoke(self, messages: List[Dict[str, str]], **kwargs) -> str:
        """返回完整回复，与流式调用共用请求参数构造和校验规则。"""
        options = self._build_request_params(kwargs, stream=False)
        print(f"🧠 正在调用 {self.model} 模型...")
        try:
            response = self._client.chat.completions.create(messages=messages, **options)
            print("✅ 大语言模型非流式响应成功:")
            return response.choices[0].message.content
        except Exception as e:
            print(f"❌ 调用LLM API时发生错误: {e}")
            raise LLMException(f"LLM非流式调用失败: {str(e)}") from e
