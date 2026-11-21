"""Engineering audit runner: per-file line budgets, bytecode syntax verification, and unit tests."""
from __future__ import annotations

from pathlib import Path
import unittest

from config.settings import (
    MAX_SCRIPT_LINES,
    MAX_SOURCE_LINES,
    MAX_TEST_LINES,
    PROJECT_ROOT,
)


def _iter_python_files() -> list[tuple[Path, int]]:
    """Yield `(file_path, max_allowed_lines)` for all tracked Python modules."""
    targets: list[tuple[Path, int]] = [(PROJECT_ROOT / "build.py", MAX_SCRIPT_LINES)]
    for folder, limit in (
        ("config", MAX_SOURCE_LINES),
        ("src", MAX_SOURCE_LINES),
        ("scripts", MAX_SCRIPT_LINES),
        ("tests", MAX_TEST_LINES),
    ):
        base = PROJECT_ROOT / folder
        if base.exists():
            for py_file in sorted(base.rglob("*.py")):
                targets.append((py_file, limit))
    return targets


def audit_line_counts() -> list[str]:
    """Verify every tracked `.py` file stays within its configured line budget."""
    violations: list[str] = []
    for path, limit in _iter_python_files():
        if not path.exists():
            continue
        lines = len(path.read_text(encoding="utf-8").splitlines())
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        if lines > limit:
            violations.append(f"{rel}: {lines} lines (exceeds limit {limit})")
    return violations


def compile_all_modules() -> list[str]:
    """Compile-check all tracked `.py` files in memory without leaving `.pyc` artifacts."""
    errors: list[str] = []
    for path, _limit in _iter_python_files():
        if not path.exists():
            continue
        try:
            source = path.read_text(encoding="utf-8")
            compile(source, str(path), "exec")
        except SyntaxError as exc:
            rel = path.relative_to(PROJECT_ROOT).as_posix()
            errors.append(f"{rel}:{exc.lineno}: {exc.msg}")
    return errors


def run_unit_tests(verbosity: int = 2) -> bool:
    """Discover and run all unit tests under `tests/`."""
    tests_dir = PROJECT_ROOT / "tests"
    suite = unittest.defaultTestLoader.discover(start_dir=str(tests_dir), top_level_dir=str(PROJECT_ROOT))
    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)
    return result.wasSuccessful()


def run_full_audit() -> int:
    """Execute line-budget check, syntax check, and full unit test suite."""
    print("=== [1/3] Line-Count Budget Check ===")
    violations = audit_line_counts()
    if violations:
        for v in violations:
            print(f"  FAIL: {v}")
        return 1
    tracked = [p for p, _ in _iter_python_files() if p.exists()]
    print(f"  PASS: {len(tracked)} Python files within line limits.")

    print("=== [2/3] Syntax & Import Bytecode Check ===")
    compile_errors = compile_all_modules()
    if compile_errors:
        for err in compile_errors:
            print(f"  FAIL: {err}")
        return 1
    print(f"  PASS: {len(tracked)} Python files compiled cleanly.")

    print("=== [3/3] Unit Test Suite ===")
    ok = run_unit_tests(verbosity=2)
    return 0 if ok else 1
