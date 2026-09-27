# --- 异常系统 ---

class BasicException(Exception):
    """基础异常类"""
    pass

class LLMException(BasicException):
    """LLM相关异常"""
    pass

class AgentException(BasicException):
    """Agent相关异常"""
    pass

class ConfigException(BasicException):
    """配置相关异常"""
    pass

class ToolException(BasicException):
    """工具相关异常"""
    pass
