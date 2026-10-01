"""统一解析和校验配置，为模型客户端和智能体提供稳定的配置快照。"""

import os
from typing import Any, ClassVar, Mapping, Optional
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator
from .exceptions import ConfigException


class Config(BaseModel):
    """声明配置字段，通过 resolve 统一合并默认值、环境变量与显式设置。

    配置字段不可直接赋值；修改时通过 resolve 创建并校验新对象。
    to_dict 返回脱敏后的展示数据，不能用于恢复真实客户端的凭据。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    model: Optional[str] = None
    api_key: Optional[SecretStr] = None
    base_url: Optional[str] = None
    temperature: Optional[float] = Field(0.7, ge=0, allow_inf_nan=False)
    max_tokens: Optional[int] = Field(None, gt=0)
    timeout: float = Field(120, gt=0, allow_inf_nan=False)
    max_tool_iterations: int = Field(3, gt=0)
    max_steps: int = Field(5, gt=0)

    # 保留字段，暂未接入提供方切换、日志控制和历史裁剪逻辑。
    default_provider: str = "openai"
    log_level: str = "INFO"
    max_history_length: int = Field(100, gt=0)

    # 环境变量与字段之间的对应表
    ENV_FIELDS: ClassVar[dict[str, str]] = {
        "LLM_MODEL_ID": "model", "LLM_API_KEY": "api_key",
        "LLM_BASE_URL": "base_url", "LLM_TIMEOUT": "timeout",
        "TEMPERATURE": "temperature", "MAX_TOKENS": "max_tokens",
        "MAX_TOOL_ITERATIONS": "max_tool_iterations", "MAX_STEPS": "max_steps",
        "LOG_LEVEL": "log_level",
        "MAX_HISTORY_LENGTH": "max_history_length",
    }

    # 清理连接字段中的空白
    @field_validator("model", "api_key", "base_url", mode="before")
    @classmethod
    def normalize_text(cls, value):
        """在类型转换前去除连接字段的首尾空白，将空字符串转换为 None。"""
        if isinstance(value, str):
            return value.strip() or None
        return value

    # 阻止布尔值充当数字
    @field_validator("temperature", "max_tokens", "timeout", "max_tool_iterations",
                     "max_steps", "max_history_length", mode="before")
    @classmethod
    def reject_boolean_numbers(cls, value):
        """拒绝将布尔值当作数值配置，避免 True 被转换成数字 1。"""
        if isinstance(value, bool):
            raise ValueError("数值配置不能使用布尔值")
        return value

    @classmethod
    def resolve(cls, config: Optional["Config"] = None, *,
                overrides: Optional[Mapping[str, Any]] = None,
                environ: Optional[Mapping[str, str]] = None) -> "Config":
        # 四段优先级
        # 默认配置 -> 环境变量env -> 已有Config -> 最高级覆盖overrides
        env = os.environ if environ is None else environ
        values = cls().model_dump()
        for env_name, field in cls.ENV_FIELDS.items():
            if env_name in env:
                value = env[env_name]
                if field in {"temperature", "max_tokens"} and not value.strip():
                    value = None
                values[field] = value
        if config is not None:
            values.update(config.model_dump(exclude_unset=True))
        values.update(overrides or {})
        try:
            return cls.model_validate(values)
        except ValidationError as exc:
            # hide_input_in_errors 隐藏错误文本中的原始输入，避免泄露凭据。
            raise ConfigException(f"配置校验失败：{exc}") from exc

    def require_model(self) -> None:
        """检查真实模型所需的连接字段，缺失时抛出 ConfigException。

        由真实模型客户端在初始化时调用；普通配置或模拟模型无需调用。
        这里只检查值是否为空，不验证密钥有效性或服务是否可访问。
        """
        missing = [name for name in ("model", "api_key", "base_url")
                   if not getattr(self, name)]
        if self.api_key is not None and not self.api_key.get_secret_value().strip():
            if "api_key" not in missing:
                missing.append("api_key")
        if missing:
            raise ConfigException("创建 MyLLM 缺少配置：" + ", ".join(missing))

    def to_dict(self) -> dict[str, Any]:
        """返回 JSON 兼容的配置字典，密钥使用掩码，仅供展示和诊断。"""
        return self.model_dump(mode="json")
