"""计算器工具：基于 AST 白名单的安全表达式求值（不使用 eval）。"""

from __future__ import annotations

import ast
import math
import operator
from typing import Any

from app.tools.base import Tool, ToolResult

_BIN_OPS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS: dict[type, Any] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

_FUNCTIONS: dict[str, Any] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "pow": pow,
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "floor": math.floor,
    "ceil": math.ceil,
}

_CONSTANTS: dict[str, float] = {"pi": math.pi, "e": math.e}

# 防止 `9**9**9` 这类指数爆炸
_MAX_POW_EXPONENT = 1000


def _eval_node(node: ast.AST) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("仅支持数字常量")
        return node.value

    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _BIN_OPS:
            raise ValueError("不支持的运算符")
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if op_type is ast.Pow and isinstance(right, (int, float)) and abs(right) > _MAX_POW_EXPONENT:
            raise ValueError("指数过大")
        return _BIN_OPS[op_type](left, right)

    if isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in _UNARY_OPS:
            raise ValueError("不支持的一元运算符")
        return _UNARY_OPS[op_type](_eval_node(node.operand))

    if isinstance(node, ast.Name):
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise ValueError(f"未知标识符: {node.id}")

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
            raise ValueError("不支持的函数调用")
        if node.keywords:
            raise ValueError("不支持关键字参数")
        args = [_eval_node(arg) for arg in node.args]
        return _FUNCTIONS[node.func.id](*args)

    raise ValueError(f"不支持的表达式类型: {type(node).__name__}")


def evaluate(expression: str) -> float:
    """求值数学表达式，非法输入抛 ValueError。"""
    expression = (expression or "").strip()
    if not expression:
        raise ValueError("表达式为空")
    if len(expression) > 500:
        raise ValueError("表达式过长")
    tree = ast.parse(expression, mode="eval")
    result = _eval_node(tree)
    if isinstance(result, complex):
        raise ValueError("不支持复数结果")
    if isinstance(result, float) and (math.isnan(result) or math.isinf(result)):
        raise ValueError("计算结果无效")
    if abs(result) > 1e308:
        raise ValueError("计算结果过大")
    return result


async def _calculate(expression: str) -> ToolResult:
    try:
        value = evaluate(expression)
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError) as exc:
        return ToolResult(
            status="error",
            summary=f"计算失败：{exc}",
            content=f"计算失败：{exc}",
            data={"expression": expression},
        )

    # 整数结果去掉 .0 尾巴，展示更自然
    if isinstance(value, float) and value.is_integer():
        display: Any = int(value)
    else:
        display = value
    return ToolResult(
        status="success",
        summary=f"{expression} = {display}",
        content=str(display),
        data={"expression": expression, "result": display},
    )


calculator_tool = Tool(
    name="calculator",
    description=(
        "数学计算器。当需要精确的数值计算（加减乘除、幂、取模、开方、对数、三角函数等）时使用，"
        "不要凭记忆口算。参数 expression 为数学表达式字符串，例如 '12*(3+4)/2' 或 'sqrt(2)'。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "要计算的数学表达式，例如 '12*(3+4)/2'",
            }
        },
        "required": ["expression"],
    },
    handler=_calculate,
    requires_confirmation=False,
    readonly=True,
    timeout=5.0,
)
