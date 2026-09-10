"""Deterministic, sandboxed arithmetic/statistics evaluator.

The executive must never *estimate* what can be computed. This evaluator runs
Python expressions (and small multi-statement programs) with a whitelist of
math/statistics builtins, no imports, no attribute access to dunder names, and
a hard timeout via a worker thread.
"""

from __future__ import annotations

import ast
import math
import statistics
import threading
from typing import Any

_ALLOWED_NODES = (
    ast.Module,
    ast.Expr,
    ast.Assign,
    ast.AugAssign,
    ast.AnnAssign,
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Store,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Set,
    ast.Subscript,
    ast.Slice,
    ast.IfExp,
    ast.ListComp,
    ast.DictComp,
    ast.SetComp,
    ast.GeneratorExp,
    ast.comprehension,
    ast.For,
    ast.If,
    ast.While,
    ast.Break,
    ast.Continue,
    ast.Pass,
    ast.Return,
    ast.FunctionDef,
    ast.arguments,
    ast.arg,
    ast.Lambda,
    ast.Attribute,
    ast.keyword,
    ast.Starred,
    ast.JoinedStr,
    ast.FormattedValue,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
    ast.And,
    ast.Or,
    ast.Not,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Is,
    ast.IsNot,
    ast.BitAnd,
    ast.BitOr,
    ast.BitXor,
    ast.LShift,
    ast.RShift,
    ast.Invert,
)

_SAFE_BUILTINS: dict[str, Any] = {
    "abs": abs,
    "min": min,
    "max": max,
    "sum": sum,
    "round": round,
    "len": len,
    "range": range,
    "sorted": sorted,
    "reversed": reversed,
    "enumerate": enumerate,
    "zip": zip,
    "map": map,
    "filter": filter,
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "list": list,
    "dict": dict,
    "set": set,
    "tuple": tuple,
    "any": any,
    "all": all,
    "pow": pow,
    "divmod": divmod,
    "math": math,
    "statistics": statistics,
    "mean": statistics.mean,
    "median": statistics.median,
    "stdev": statistics.stdev,
    "pstdev": statistics.pstdev,
    "variance": statistics.variance,
    "sqrt": math.sqrt,
    "log": math.log,
    "exp": math.exp,
    "pi": math.pi,
    "e": math.e,
    "print": None,  # replaced per-call with a collector
}


class UnsafeExpression(ValueError):
    pass


def _validate(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise UnsafeExpression(f"disallowed syntax: {type(node).__name__}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise UnsafeExpression("dunder attribute access is not allowed")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise UnsafeExpression("dunder names are not allowed")
        if isinstance(node, ast.While):
            # Loops are allowed but bounded by the timeout; disallow `while True` literal spins.
            if isinstance(node.test, ast.Constant) and node.test.value is True:
                raise UnsafeExpression("unbounded while-loop is not allowed")


def safe_eval(program: str, timeout_seconds: float = 5.0, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    """Evaluate ``program``; returns {"value": ..., "stdout": ..., "variables": {...}}."""
    src = program.strip()
    tree = ast.parse(src, mode="exec")
    _validate(tree)
    out_lines: list[str] = []

    def _print(*args: Any, **_kw: Any) -> None:
        out_lines.append(" ".join(str(a) for a in args))

    env: dict[str, Any] = {**_SAFE_BUILTINS, "print": _print}
    env["__builtins__"] = {}
    local_vars: dict[str, Any] = dict(variables or {})
    result: dict[str, Any] = {}
    error: list[BaseException] = []

    # If the last statement is an expression, capture its value.
    last_expr_value_name = "__cogos_result__"
    body = tree.body
    if body and isinstance(body[-1], ast.Expr):
        body[-1] = ast.Assign(targets=[ast.Name(id=last_expr_value_name, ctx=ast.Store())], value=body[-1].value)
        ast.fix_missing_locations(tree)

    def run() -> None:
        try:
            exec(compile(tree, "<cogos-calc>", "exec"), env, local_vars)  # noqa: S102 - validated AST, no builtins
        except BaseException as exc:  # noqa: BLE001
            error.append(exc)

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(timeout_seconds)
    if th.is_alive():
        raise TimeoutError(f"calculation exceeded {timeout_seconds}s")
    if error:
        raise error[0]
    value = local_vars.pop(last_expr_value_name, None)
    result["value"] = value
    result["stdout"] = "\n".join(out_lines)
    result["variables"] = {k: v for k, v in local_vars.items() if not callable(v)}
    return result
