"""两种受限计算。

evaluate_expression 在当前进程里算四则表达式，语法树只允许数字和运算符，给 calculator 用。
run_sandbox 另起一个无用户 site 的解释器，只接受给 result 赋值的表达式，给 python.sandbox 用。
子进程环境只留 PATH 和编码，超时 2 秒。两边都不开放导入、调用和属性访问。
"""

import ast
import os
import subprocess
import sys

ALLOWED = {
    ast.Module,
    ast.Assign,
    ast.Expr,
    ast.BinOp,
    ast.UnaryOp,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.Store,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.UAdd,
    ast.USub,
}

RUNNER = r"""
import ast, sys
src = sys.stdin.read()
if len(src) > 500:
    sys.stderr.write("blocked:length")
    sys.exit(2)
tree = ast.parse(src, mode="exec")
allowed = {
    "Module", "Assign", "Expr", "BinOp", "UnaryOp", "Constant", "Name",
    "Load", "Store", "Add", "Sub", "Mult", "Div", "FloorDiv", "Mod", "UAdd", "USub",
}
bad = sorted({type(node).__name__ for node in ast.walk(tree) if type(node).__name__ not in allowed})
if bad:
    sys.stderr.write("blocked:" + ",".join(bad))
    sys.exit(2)
for node in ast.walk(tree):
    if isinstance(node, ast.Name) and node.id.startswith("_"):
        sys.stderr.write("blocked:name")
        sys.exit(2)
namespace = {}
exec(compile(tree, "<sandbox>", "exec"), {"__builtins__": {}}, namespace)
if "result" not in namespace:
    sys.stderr.write("missing result")
    sys.exit(3)
print(namespace["result"])
"""


def evaluate_expression(expression: str):
    tree = ast.parse(expression, mode="eval")
    for node in ast.walk(tree):
        if type(node) not in ALLOWED | {ast.Expression}:
            raise ValueError("表达式包含不允许的语法")
    return eval(compile(tree, "<calc>", "eval"), {"__builtins__": {}}, {})


def run_sandbox(code: str) -> str:
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-c", RUNNER],
            input=code.encode(),
            capture_output=True,
            timeout=2,
            env={"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("沙箱执行超时") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode() or "沙箱拒绝执行"
        raise ValueError(detail.strip())
    return completed.stdout.decode().strip()
