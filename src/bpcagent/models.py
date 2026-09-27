# --- 大模型客户端 ---

import os
from typing import Optional, List, Dict, Iterator
from openai import OpenAI
from .exceptions import BasicException, LLMException

class MyLLM:
    """
    大语言模型客户端
    """
    def __init__(
            self, 
            model: Optional[str] = None, 
            apiKey: Optional[str] = None, 
            baseUrl: Optional[str] = None, 
            temperature: float = 0.7,
            max_tokens: Optional[int] = None,
            timeout: Optional[int] = None,
            **kwargs
            ):
        """
        初始化客户端。优先使用传入参数，如果未提供，则从环境变量加载。
        """
        # 配置大模型变量，如果没有提供则从.env文件加载
        self.model = model or os.getenv("LLM_MODEL_ID")
        self.apiKey = apiKey or os.getenv("LLM_API_KEY")
        self.baseUrl = baseUrl or os.getenv("LLM_BASE_URL")
        self.timeout = timeout or int(os.getenv("LLM_TIMEOUT", 120))
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.kwargs = kwargs

        # 检测必要参数
        if not all([self.model, self.apiKey, self.baseUrl]):
            raise BasicException("模型ID、API密钥和服务地址必须被提供或在.env文件中定义。")
        
        # 创建OpenAI客户端
        self._client = self._create_OpenAI_client()

    def _create_OpenAI_client(self) -> OpenAI:
        """
        创建大语言模型客户端（OpenAI）
        """
        return OpenAI(
            api_key = self.apiKey,
            base_url = self.baseUrl,
            timeout = self.timeout
        )

    def think(self, messages: List[Dict[str, str]], **kwargs) -> Iterator[str]:
        """
        调用大语言模型进行思考，并返回流式响应。
        """
        print(f"🧠 正在调用 {self.model} 模型...")
        try:
            # 生成响应
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=kwargs.get('temperature', self.temperature),
                max_tokens=kwargs.get('max_tokens', self.max_tokens),
                stream=True,
                **{k: v for k, v in kwargs.items() if k not in ['temperature', 'max_tokens', 'stream']}
            )

            # 处理流式响应
            print("✅ 大语言模型流式响应成功:")
            for chunk in response:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                content = getattr(delta, "content", None)
                if not content:
                    continue
                print(content, end="", flush=True)
                yield content
            print()  # 在流式输出结束后换行
        
        except Exception as e:
            print(f"❌ 调用LLM API时发生错误: {e}")
            raise LLMException(f"LLM流式调用失败: {str(e)}") from e
        
    def invoke(self, messages: List[Dict[str, str]], **kwargs) -> str:
        """
        调用大语言模型进行思考，并返回非流式响应。
        """
        print(f"🧠 正在调用 {self.model} 模型...")
        try:
            # 生成响应
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=kwargs.get('temperature', self.temperature),
                max_tokens=kwargs.get('max_tokens', self.max_tokens),
                **{k: v for k, v in kwargs.items() if k not in ['temperature', 'max_tokens']}
            )
            # 处理流式响应
            print("✅ 大语言模型非流式响应成功:")

            return response.choices[0].message.content
        
        except Exception as e:
            print(f"❌ 调用LLM API时发生错误: {e}")
            raise LLMException(f"LLM非流式调用失败: {str(e)}") from e
