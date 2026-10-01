"""计算器工具"""

import ast
import operator
import math
from pydantic import Field

from .tool import Tool, ToolArgs

class CalculatorArgs(ToolArgs):
    """计算器的输入参数。"""

    input: str = Field(min_length=1, pattern=r"\S", description="需要计算的数学表达式")


class CalculatorTool(Tool[CalculatorArgs]):
    """Python计算器工具"""
    
    args_schema = CalculatorArgs

    # 支持的操作符
    OPERATORS = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Pow: operator.pow,
        ast.BitXor: operator.xor,
        ast.USub: operator.neg,
    }
    
    # 支持的函数
    FUNCTIONS = {
        'abs': abs,
        'round': round,
        'max': max,
        'min': min,
        'sum': sum,
        'sqrt': math.sqrt,
        'sin': math.sin,
        'cos': math.cos,
        'tan': math.tan,
        'log': math.log,
        'exp': math.exp,
        'pi': math.pi,
        'e': math.e,
    }
    
    def __init__(self):
        super().__init__(
            name="calculate",
            description="执行数学计算。支持基本运算、数学函数等。例如：2+3*4, sqrt(16), sin(pi/2)等。"
        )
    
    def run(self, parameters: CalculatorArgs) -> int | float:
        """返回计算数值，非法表达式和执行错误由注册表统一处理。"""
        print(f"正在执行计算器工具哦～～～～")
        node = ast.parse(parameters.input, mode="eval")
        result = self._eval_node(node.body)
        if type(result) not in (int, float):
            raise ValueError("表达式必须产生数值结果")
        return result

    def _eval_node(self, node):
        """递归计算AST节点"""
        if isinstance(node, ast.Constant):  # Python 3.8+
            return node.value
        elif isinstance(node, ast.BinOp):
            return self.OPERATORS[type(node.op)](
                self._eval_node(node.left), 
                self._eval_node(node.right)
            )
        elif isinstance(node, ast.UnaryOp):
            return self.OPERATORS[type(node.op)](self._eval_node(node.operand))
        elif isinstance(node, ast.Call):
            func_name = node.func.id
            if func_name in self.FUNCTIONS:
                args = [self._eval_node(arg) for arg in node.args]
                return self.FUNCTIONS[func_name](*args)
            else:
                raise ValueError(f"不支持的函数: {func_name}")
        elif isinstance(node, ast.Name):
            if node.id in self.FUNCTIONS:
                return self.FUNCTIONS[node.id]
            else:
                raise ValueError(f"未定义的变量: {node.id}")
        else:
            raise ValueError(f"不支持的表达式类型: {type(node)}")
