"""Line-coverage report for the gate-n-guard suite (stdlib only).

Runs the offline suite under `sys.monitoring` (PEP 669) — no sys.settrace, so
imports and asyncio are untouched — and reports, per module, how many
function-body statement lines the tests execute plus the exact uncovered ones.

What counts: every executable line inside a function, method, lambda, or
comprehension. Excluded: module-level statements (they run at import), class
bodies, and signature/decorator lines (they never emit LINE events).

Usage:
    .venv/bin/python tools/coverage.py [--show-covered]

Exit status is 1 when any function-body line is uncovered.
"""

from __future__ import annotations

import argparse
import ast
import collections
import io
import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
PKG = os.path.join(SRC, "gate_n_guard")

CO_OPTIMIZED = 0x0001
TOOL_ID = 1  # any free monitoring tool id; "coverage" is conventionally 1


def _load_modules():
    sys.path.insert(0, SRC)
    import gate_n_guard

    modules = {"__init__.py": gate_n_guard}
    for name in sorted(os.listdir(PKG)):
        if name.endswith(".py") and name not in ("__init__.py", "__main__.py"):
            modules[name] = __import__(f"gate_n_guard.{name[:-3]}", fromlist=["x"])
    return modules


def code_tree(code: types.CodeType):
    yield code
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            yield from code_tree(const)


def live_codes(obj):
    """Code objects reachable from one module attribute."""
    if isinstance(obj, types.FunctionType):
        yield from code_tree(obj.__code__)
    elif isinstance(obj, (staticmethod, classmethod)):
        yield from code_tree(obj.__func__.__code__)
    elif isinstance(obj, property):
        for fn in (obj.fget, obj.fset, obj.fdel):
            if fn is not None:
                yield from code_tree(fn.__code__)
    elif isinstance(obj, type) and obj.__module__.startswith("gate_n_guard"):
        for value in vars(obj).values():
            yield from live_codes(value)


def signature_lines(path: str) -> set[int]:
    """Lines occupied by def/decorator headers, which never emit LINE events."""
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), path)
    rows: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            rows.update(range(start, node.body[0].lineno))
    return rows


def body_lines(path: str) -> set[int]:
    with open(path, encoding="utf-8") as handle:
        root = compile(handle.read(), path, "exec")
    out: set[int] = set()
    for inner in code_tree(root):
        if not inner.co_flags & CO_OPTIMIZED:
            continue
        first = True
        for _, _, lineno in inner.co_lines():
            if lineno is None or lineno <= 0:
                continue
            if first:  # the def line itself
                first = False
                continue
            out.add(lineno)
    return out - signature_lines(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show-covered", action="store_true",
                        help="also print the covered line numbers")
    args = parser.parse_args()

    os.chdir(ROOT)
    modules = _load_modules()
    targets = {}
    for name, module in modules.items():
        path = os.path.join(PKG, name)
        codes = set()
        for value in vars(module).values():
            codes.update(live_codes(value))
        targets[name] = (path, codes)

    hits: dict[str, set[int]] = collections.defaultdict(set)
    monitor = sys.monitoring

    def on_line(code, lineno):
        hits[code.co_filename].add(lineno)

    monitor.use_tool_id(TOOL_ID, "gate-n-guard-coverage")
    monitor.register_callback(TOOL_ID, monitor.events.LINE, on_line)
    for _, codes in targets.values():
        for code in codes:
            monitor.set_local_events(TOOL_ID, code, monitor.events.LINE)
    try:
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(
            unittest.defaultTestLoader.discover("tests", top_level_dir="tests")
        )
    finally:
        for _, codes in targets.values():
            for code in codes:
                monitor.set_local_events(TOOL_ID, code, 0)
        monitor.set_events(TOOL_ID, 0)
        monitor.free_tool_id(TOOL_ID)

    if not result.wasSuccessful():
        print(f"suite failed ({len(result.failures)} failures, {len(result.errors)} errors)")
        return 2

    total = covered = 0
    for name, (path, _) in targets.items():
        executable = body_lines(path)
        hit = executable & hits[path]
        total += len(executable)
        covered += len(hit)
        pct = 100.0 * len(hit) / len(executable) if executable else 100.0
        missing = sorted(executable - hit)
        print(f"{name:15} {len(hit):4}/{len(executable):4} {pct:5.1f}%"
              + (f"  missing={missing}" if missing else ""))
        if args.show_covered:
            print(f"{'':15} covered={sorted(hit)}")
    print(f"{'TOTAL':15} {covered:4}/{total:4} {100.0 * covered / total:5.1f}%"
          f"  ({result.testsRun} tests)")
    return 0 if covered == total else 1


if __name__ == "__main__":
    sys.exit(main())
