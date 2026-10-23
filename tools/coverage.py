"""Coverage report for the gate-n-guard suite (stdlib only).

Runs the offline suite under `sys.monitoring` (PEP 669) — no sys.settrace, so
imports and asyncio are untouched — and reports both:

* **statement coverage** per module: function-body lines the tests execute;
* **decision points** (advisory): conditional jumps the tests execute, with a
  note when only one destination was reported. `sys.monitoring` does not
  report a destination for every CPython jump layout — measured: a synthetic
  guard taken both ways reports one destination — so this is a hint, not a
  gate. Condition-level confidence comes from `tools/mutation.py` instead.

What counts as a statement line: executable lines inside a function, method,
lambda, or comprehension. Excluded: module-level statements (they run at
import), class bodies, and signature/decorator lines (they never emit LINE
events).

A branch is an arc out of a conditional jump (`if`, `while`, `for`, `and`,
`or`, `match`, comprehension filters). Both directions must execute.

Usage:
    .venv/bin/python tools/coverage.py [--show-covered] [--quiet]

Exit status is 1 when any statement line is uncovered.
"""

from __future__ import annotations

import argparse
import ast
import collections
import dis
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
UNCONDITIONAL_JUMPS = {
    "JUMP", "JUMP_NO_INTERRUPT", "JUMP_FORWARD", "JUMP_BACKWARD",
    "JUMP_BACKWARD_NO_INTERRUPT", "INSTRUMENTED_JUMP_FORWARD",
    "INSTRUMENTED_JUMP_BACKWARD",
}


def _load_modules():
    sys.path.insert(0, SRC)
    import gate_n_guard

    modules = {"__init__.py": gate_n_guard}
    for name in sorted(os.listdir(PKG)):
        if name.endswith(".py") and name != "__init__.py":
            modules[name] = __import__(f"gate_n_guard.{name[:-3]}", fromlist=["x"])
    return modules


def code_tree(code: types.CodeType):
    yield code
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            yield from code_tree(const)


def live_codes(obj):
    """Code objects defined in this package and reachable from one attribute.

    Imported callables (dataclass, Field, get_args, …) also sit in a module's
    namespace; their code belongs to another file and must not be counted.
    """
    for code in _reachable_codes(obj):
        if code.co_filename.startswith(PKG):
            yield code


def _reachable_codes(obj):
    if isinstance(obj, types.FunctionType):
        yield from code_tree(obj.__code__)
    elif isinstance(obj, (staticmethod, classmethod)):
        yield from code_tree(obj.__func__.__code__)
    elif isinstance(obj, property):
        for fn in (obj.fget, obj.fset, obj.fdel):
            if fn is not None:
                yield from code_tree(fn.__code__)
    elif isinstance(obj, type) and (obj.__module__ or "").startswith("gate_n_guard"):
        for value in vars(obj).values():
            yield from _reachable_codes(value)


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


def exception_lines(path: str) -> set[int]:
    """Lines owning `try`/`with` machinery.

    Those statements compile to conditional jumps on their cleanup paths that
    only run when an exception (or a context-manager exit) happens; they are
    not decision points a test can take. Their handler bodies still count as
    statements, and an untaken `except` body is still visible as uncovered.
    """
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), path)
    rows: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Try, ast.TryStar, ast.With, ast.AsyncWith)):
            rows.add(node.lineno)
    return rows


def branch_sources(code: types.CodeType) -> dict[int, int]:
    """Decision points of one code object: conditional-jump offset -> line."""
    instructions = list(dis.get_instructions(code))
    machinery = set()
    for entry in dis.Bytecode(code).exception_entries:
        machinery.update(range(entry.start, entry.end))
    sources: dict[int, int] = {}
    previous = None
    for ins in instructions:
        is_conditional = (
            ins.opcode in dis.hasjrel
            and ins.opname not in UNCONDITIONAL_JUMPS
            and isinstance(ins.argval, int)
        )
        is_dispatch = previous is not None and previous.opname in (
            "CHECK_EXC_MATCH", "CHECK_EG_MATCH",
        )
        if is_conditional and not is_dispatch and ins.offset not in machinery:
            sources[ins.offset] = ins.positions.lineno or code.co_firstlineno
        previous = ins
    return sources


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show-covered", action="store_true",
                        help="also print the covered line/arc sets")
    parser.add_argument("--quiet", action="store_true",
                        help="print only the totals")
    args = parser.parse_args()

    os.chdir(ROOT)
    modules = _load_modules()
    targets = {}
    for name, module in modules.items():
        path = os.path.join(PKG, name)
        codes = set()
        for value in vars(module).values():
            # a module namespace also re-exports other modules' callables
            codes.update(c for c in live_codes(value) if c.co_filename == path)
        if path.endswith("__main__.py"):
            # the module body is the entry point: covered by the subprocess
            # test, not by function bodies, so it carries no statements here
            pass
        targets[name] = (path, codes)

    hits: dict[str, set[int]] = collections.defaultdict(set)
    taken: dict[types.CodeType, set[tuple[int, int]]] = collections.defaultdict(set)
    monitor = sys.monitoring

    def on_line(code, lineno):
        hits[code.co_filename].add(lineno)

    def on_branch(code, src, dst):
        taken[code].add((src, dst))

    monitor.use_tool_id(TOOL_ID, "gate-n-guard-coverage")
    monitor.register_callback(TOOL_ID, monitor.events.LINE, on_line)
    monitor.register_callback(TOOL_ID, monitor.events.BRANCH, on_branch)
    events = monitor.events.LINE | monitor.events.BRANCH
    for _, codes in targets.values():
        for code in codes:
            monitor.set_local_events(TOOL_ID, code, events)
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
        for test, trace in result.failures + result.errors:
            print(f"\n{test}\n{trace[-1200:]}")
        return 2

    lines_total = lines_covered = 0
    arcs_total = arcs_covered = 0
    for name, (path, codes) in targets.items():
        executable = body_lines(path)
        hit = executable & hits[path]
        lines_total += len(executable)
        lines_covered += len(hit)

        # decision points: advisory only (see the module docstring)
        skip_lines = exception_lines(path)
        expected: dict[tuple[types.CodeType, int], int] = {}
        for code in codes:
            if code.co_qualname.endswith(".__annotate__"):
                continue  # PEP 649 lazy-annotation thunk, not a decision point
            for offset, line in branch_sources(code).items():
                if line in skip_lines:
                    continue
                expected[(code, offset)] = line
        reached = {(code, src) for code, arcs in taken.items() for src, _dst in arcs}
        missing = expected.keys() - reached
        arcs_total += len(expected)
        arcs_covered += len(expected) - len(missing)

        if args.quiet:
            continue
        line_pct = 100.0 * len(hit) / len(executable) if executable else 100.0
        print(f"{name:15} lines {len(hit):4}/{len(executable):4} {line_pct:5.1f}%"
              f"   decisions {len(expected) - len(missing):3}/{len(expected):3}"
              + (f"   missing lines={sorted(executable - hit)}" if executable - hit else "")
              + (f"   unexecuted decision points: "
                 f"{sorted(f'{a[0].co_qualname}:{expected[a]}' for a in missing)}"
                 if missing else ""))
        if args.show_covered:
            print(f"{'':15} covered lines={sorted(hit)}")
            print(f"{'':15} observed destinations={dict(collections.Counter(
                len(dsts) for arcs in taken.values() for dsts in [set(arcs)]))}")

    print(f"{'TOTAL':15} lines {lines_covered:4}/{lines_total:4} "
          f"{100.0 * lines_covered / lines_total:5.1f}%   decisions "
          f"{arcs_covered:3}/{arcs_total:3} (advisory)   ({result.testsRun} tests)")
    return 0 if lines_covered == lines_total else 1


if __name__ == "__main__":
    sys.exit(main())
