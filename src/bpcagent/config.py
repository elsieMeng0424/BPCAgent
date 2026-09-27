# --- 配置管理系统 ---

import os
from pydantic import BaseModel
from typing import Any, Dict, Optional

class Config(BaseModel):
    """
    配置类
    """
    # 模型配置
    default_model: str = "gpt-5-mini"
    default_provider: str = "openai"
    temperature: float = 0.7
    max_tokens: Optional[int] = None

    # 系统配置
    debug: bool = False
    log_level: str = "INFO"

    # 其他配置
    max_history_length: int = 100

    @classmethod
    def from_env(cls) -> "Config":
        """
        读取环境变量创建配置
        """
        return cls(
            debug = os.getenv("DEBUG", "false").lower() == "true",
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            temperature=float(os.getenv("TEMPERATURE", 0.7)),
            max_tokens=int(os.getenv("MAX_TOKENS")) if os.getenv("MAX_TOKENS") else None,
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """
        转换为字典
        """
        return self.model_dump() # dict()方法已弃用
