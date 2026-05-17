"""AST-based idempotency lint for lifecycle methods.

The plan's Section 5 mandated every `async def connect|start|open|join` begin
with an explicit "already alive" early-return. The previous grep-based check
false-passed on the word "return" appearing in docstrings (e.g. discord_voice's
`async def join` was unguarded but the docstring said "return once Discord
login completes").

This lint walks the AST per function and requires the first non-docstring
statement to be an `If` whose body contains a bare `Return`. The condition is
expected to test an "alive" flag (`self.alive`, `self._started`,
`self._connected`, `self._running`), but the structural check is what fails CI.

Usage:
    python -m src.audit_idempotency_lint           # scan src/
    python -m src.audit_idempotency_lint src/      # explicit
    python -m src.audit_idempotency_lint --skip src/audit_dedup_probe.py
"""

from __future__ import annotations

import argparse
import ast
import os
import sys
from dataclasses import dataclass

LIFECYCLE_NAMES = frozenset({"connect", "start", "open", "join"})

# Methods named `join` on these classes are not lifecycle methods. Allowlist
# their fully-qualified names. Add more cases here rather than weakening the
# regex. discord_voice.VoiceBridge.join is the per-call "join this channel"
# action sent to the Node sidecar — every call is a fresh intent, not a
# lifecycle event.
LIFECYCLE_FQN_ALLOWLIST = frozenset({
    "VoiceBridge.join",
    "CursorBridge.create_session",
})

# Top-level functions (not class methods) that are lifecycle but legitimately
# not @bot.command:
TOP_LEVEL_LIFECYCLE_SKIP = frozenset({
    "main",  # CLI entry points; restart-by-fork pattern handles idempotency.
})


@dataclass
class LintMiss:
    fqn: str  # e.g. "GeminiSession.connect"
    path: str
    lineno: int
    reason: str


def _function_fqn(class_stack: list[str], func_name: str) -> str:
    return f"{class_stack[-1]}.{func_name}" if class_stack else func_name


def _first_real_stmt(body: list[ast.stmt]) -> ast.stmt | None:
    """Return the first statement that isn't a docstring or global/nonlocal decl.

    `global`/`nonlocal` are not control flow; they're scope hints and can be
    in any order at the top of a function. They should not push the guard
    further down.
    """
    for stmt in body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) \
                and isinstance(stmt.value.value, str):
            continue
        if isinstance(stmt, (ast.Global, ast.Nonlocal)):
            continue
        return stmt
    return None


def _is_guard_if(stmt: ast.stmt) -> bool:
    """True iff `stmt` is an `if EXPR: ...; return` short-circuit at the head.

    The body of the If may contain logging or other non-control-flow
    statements before the Return; we only require that the If body unwinds
    via a direct Return (no nested loop/with/try changing semantics).
    """
    if not isinstance(stmt, ast.If):
        return False
    for inner in stmt.body:
        if isinstance(inner, ast.Return):
            return True
        # Reject anything that introduces new control flow before the return.
        if isinstance(inner, (ast.For, ast.While, ast.With, ast.AsyncWith,
                              ast.AsyncFor, ast.Try)):
            return False
    return False


def _uses_async_context_manager(func: ast.AsyncFunctionDef) -> bool:
    """If the function body opens an `async with` whose first child is a
    bare-condition early-return, treat that as a lifecycle guard.

    Matches the GeminiSession pattern:
        async with self._lifecycle_lock:
            if self._connected and ...: return
    """
    first = _first_real_stmt(func.body)
    if not isinstance(first, ast.AsyncWith):
        return False
    inner_first = _first_real_stmt(first.body)
    if inner_first is None:
        return False
    return _is_guard_if(inner_first)


def lint_function(
    func: ast.AsyncFunctionDef, fqn: str, path: str,
) -> LintMiss | None:
    """Return a LintMiss if `func` lacks an early-return idempotency guard."""
    first = _first_real_stmt(func.body)
    if first is None:
        return LintMiss(fqn=fqn, path=path, lineno=func.lineno,
                        reason="empty function body")
    if _is_guard_if(first):
        return None
    if _uses_async_context_manager(func):
        return None
    return LintMiss(
        fqn=fqn, path=path, lineno=func.lineno,
        reason=(
            "first non-docstring statement is not `if ALREADY_ALIVE: return`. "
            "Add an explicit early-return so a second call is a no-op."
        ),
    )


def _iter_lifecycle_funcs(tree: ast.Module):
    """Yield (async_func, class_stack) for every lifecycle-named async def."""
    class_stack: list[str] = []

    def walk(nodes: list[ast.stmt]) -> None:
        for node in nodes:
            if isinstance(node, ast.ClassDef):
                class_stack.append(node.name)
                walk(node.body)
                class_stack.pop()
            elif isinstance(node, ast.AsyncFunctionDef) and node.name in LIFECYCLE_NAMES:
                yield_target.append((node, list(class_stack)))
            elif hasattr(node, "body") and isinstance(node.body, list):
                walk(node.body)

    yield_target: list = []
    walk(tree.body)
    return yield_target


def lint_file(path: str) -> list[LintMiss]:
    """Lint one Python file. Returns every miss found."""
    with open(path) as f:
        src = f.read()
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError as exc:
        return [LintMiss(fqn=path, path=path, lineno=exc.lineno or 0,
                         reason=f"syntax error: {exc}")]

    misses: list[LintMiss] = []
    for func, class_stack in _iter_lifecycle_funcs(tree):
        fqn = _function_fqn(class_stack, func.name)
        if fqn in LIFECYCLE_FQN_ALLOWLIST:
            continue
        if not class_stack and func.name in TOP_LEVEL_LIFECYCLE_SKIP:
            continue
        miss = lint_function(func, fqn, path)
        if miss:
            misses.append(miss)
    return misses


def lint_tree(root: str, skip: set[str]) -> list[LintMiss]:
    misses: list[LintMiss] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dirpath, fn)
            if p in skip:
                continue
            misses.extend(lint_file(p))
    return misses


def _cli_main() -> int:
    parser = argparse.ArgumentParser(description="AST idempotency lint for lifecycle methods")
    parser.add_argument("paths", nargs="*", default=["src/"],
                        help="Files or directories to scan (default: src/)")
    parser.add_argument("--skip", action="append", default=[],
                        help="Specific files to skip")
    args = parser.parse_args()

    skip = {os.path.normpath(p) for p in args.skip}
    misses: list[LintMiss] = []
    for p in args.paths:
        if os.path.isdir(p):
            misses.extend(lint_tree(p, skip))
        elif os.path.isfile(p) and os.path.normpath(p) not in skip:
            misses.extend(lint_file(p))
        else:
            print(f"SKIP: {p} (not found)")

    print(f"Idempotency lint — scanned: {args.paths}")
    if not misses:
        print("All lifecycle methods (connect/start/open/join) have idempotency guards.")
        return 0

    print(f"FAIL: {len(misses)} unguarded lifecycle method(s):")
    for m in misses:
        print(f"  {m.path}:{m.lineno}  {m.fqn}  -- {m.reason}")
    return 1


if __name__ == "__main__":
    sys.exit(_cli_main())
