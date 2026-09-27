# ---消息系统---

from pydantic import BaseModel
from datetime import datetime
from typing import Any, Dict, Literal, Optional

# 定义消息中角色的类型
MessageRole = Literal["user", "assistant", "system", "tool"]

class Message(BaseModel):
    """
    消息类
    """
    content: str
    role: MessageRole
    timestamp: datetime = None
    metadata: Optional[Dict[str, Any]] = None

    def __init__(self, content: str, role: MessageRole, **kwargs):
        super().__init__(
            content=content,
            role=role,
            timestamp=kwargs.get('timestamp', datetime.now()),
            metadata=kwargs.get('metadata', {})
        )

    def to_dict(self) -> Dict[str, Any]:
        """
        转换为OpenAI API的格式（字典）
        """
        return {
            "role": self.role,
            "content": self.content
        }
    
    def __str__(self) -> str:
        return f"[{self.role}] {self.content}"
