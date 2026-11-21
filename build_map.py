"""Build static, test-independent impact data from the pinned source checkout."""

from __future__ import annotations

import ast
import gzip
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path("/opt/sqlglot").resolve()
OUT = Path("/app/impact_map.json.gz")
PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def scope_records(path: Path, line_contexts: dict[int, set[str]]) -> tuple[list, list]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return [], []
    scopes: list[list] = []
    top_level: list[list[int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = int(getattr(node, "lineno", 1))
            end = int(getattr(node, "end_lineno", start))
            observed: set[str] = set()
            for line in range(start, end + 1):
                observed.update(line_contexts.get(line, ()))
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            scopes.append([start, end, sorted(observed), kind, node.name])
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            top_level.append([int(getattr(node, "lineno", 1)), int(getattr(node, "end_lineno", 1))])
    return scopes, top_level


def fixtures_map() -> dict[str, list[str]]:
    fixture_root = ROOT / "tests" / "fixtures"
    tests_root = ROOT / "tests"
    test_sources: list[tuple[Path, ast.Module]] = []
    for path in tests_root.rglob("test_*.py"):
        try:
            test_sources.append((path, ast.parse(path.read_text(encoding="utf-8"))))
        except (SyntaxError, UnicodeDecodeError):
            continue

    result: dict[str, list[str]] = {}
    for fixture in fixture_root.rglob("*"):
        if not fixture.is_file():
            continue
        rel = fixture.relative_to(ROOT).as_posix()
        stem = fixture.stem
        ids: set[str] = set()
        for source, tree in test_sources:
            source_rel = source.relative_to(ROOT).as_posix()
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not node.name.startswith("test"):
                    continue
                strings = [
                    item.value
                    for item in ast.walk(node)
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                ]
                exact = node.name in {f"test_{stem}", f"test{stem}"}
                exact = exact or any(stem in value for value in strings)
                if exact:
                    # Pytest node IDs use the class name too; choose matching
                    # measured IDs later by file and method name.
                    ids.add(f"{source_rel}::{node.name}")
        if ids:
            result[rel] = sorted(ids)
    return result


def process_test_names() -> set[str]:
    """Identify tests whose work runs in child processes, outside coverage."""
    result: set[str] = set()
    for path in (ROOT / "tests").rglob("test_*.py"):
        try:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text)
        except (SyntaxError, UnicodeDecodeError):
            continue
        if not any(token in text for token in ("ProcessPoolExecutor", "multiprocessing", "get_context(")):
            continue
        file_name = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                if any(token in node.name for token in ("execute", "process", "multiprocess")):
                    result.add(f"{file_name}::{node.name}")
    return result


def dialect_index(test_files: dict[str, set[str]], fixtures: dict[str, list[str]]) -> dict[str, list[str]]:
    names = {
        path.stem.removeprefix("test_")
        for path in (ROOT / "tests" / "dialects").glob("test_*.py")
    }
    names.update(path.stem for path in (ROOT / "sqlglot" / "dialects").glob("*.py") if path.name != "__init__.py")
    result: dict[str, set[str]] = {name.lower(): set() for name in names}
    for test_file, ids in test_files.items():
        for name in result:
            if test_file == f"tests/dialects/test_{name}.py":
                result[name].update(ids)
        if not test_file.startswith("tests/") or not test_file.endswith(".py"):
            continue
        source_path = ROOT / test_file
        try:
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.name.startswith("test"):
                continue
            strings = [
                item.value.lower()
                for item in ast.walk(node)
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            ]
            for name in result:
                if any(value == name or f"dialect={name}" in value or f"read={name}" in value or f"write={name}" in value for value in strings):
                    result[name].update(
                        test_id for test_id in ids if test_id.rsplit("::", 1)[-1].split("[", 1)[0] == node.name
                    )
    for fixture in (ROOT / "tests" / "fixtures").rglob("*"):
        if not fixture.is_file():
            continue
        relative = fixture.relative_to(ROOT).as_posix()
        try:
            text = fixture.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
        for name in result:
            if f"dialect: {name}" in text:
                result[name].update(fixtures.get(relative, ()))
    return {name: sorted(ids) for name, ids in result.items() if ids}


def expression_symbol_index(test_files: dict[str, set[str]], fixtures: dict[str, list[str]]) -> dict[str, list[str]]:
    expression_root = ROOT / "sqlglot" / "expressions"
    class_names: set[str] = set()
    for path in expression_root.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        class_names.update(node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef))

    result: dict[str, set[str]] = {name: set() for name in class_names}
    normalized_names = {name.lower(): name for name in class_names}
    feature_names: dict[str, set[str]] = defaultdict(set)
    generic_features = {
        "expression", "identifier", "table", "function", "type", "types",
        "properties", "column", "query", "data", "command", "literal",
    }
    for name in class_names:
        words = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)", name)
        feature = words[0].lower() if words else ""
        if len(feature) >= 5 and feature not in generic_features:
            feature_names[feature].add(name)
    sources: list[tuple[str, ast.Module, str]] = []
    for test_file in test_files:
        if not test_file.startswith("tests/") or not test_file.endswith(".py"):
            continue
        path = ROOT / test_file
        try:
            text = path.read_text(encoding="utf-8")
            sources.append((test_file, ast.parse(text), text))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue

    def matching_classes(text: str) -> set[str]:
        words = set(re.findall(r"[a-z_][a-z0-9_]*", text.lower()))
        matches = {normalized_names[word] for word in words if word in normalized_names}
        for word in words:
            matches.update(feature_names.get(word, ()))
        return matches

    for test_file, tree, source_text in sources:
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.name.startswith("test"):
                continue
            body = ast.get_source_segment(source_text, node) or node.name
            matches = matching_classes(node.name + "\n" + body)
            test_ids = {
                test_id for test_id in test_files[test_file]
                if test_id.rsplit("::", 1)[-1].split("[", 1)[0] == node.name
            }
            for name in matches:
                result[name].update(test_ids)

    for fixture, ids in fixtures.items():
        try:
            text = (ROOT / fixture).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for name in matching_classes(text):
            result[name].update(ids)
    return {name: sorted(ids) for name, ids in result.items() if ids}


def main() -> None:
    actual = (ROOT / ".source-revision").read_text(encoding="ascii").strip()
    if actual != PIN:
        raise SystemExit(f"wrong SQLGlot revision: {actual}")
    from coverage import CoverageData

    data = CoverageData(basename=str(ROOT / ".coverage"))
    data.read()
    source: dict[str, dict] = {}
    test_files: dict[str, set[str]] = defaultdict(set)
    all_contexts: set[str] = set()

    for filename in data.measured_files():
        path = Path(filename).resolve()
        try:
            relative = path.relative_to(ROOT).as_posix()
        except ValueError:
            continue
        if not relative.startswith("sqlglot/"):
            continue
        raw = data.contexts_by_lineno(filename) or {}
        line_contexts: dict[int, set[str]] = {}
        for line, contexts in raw.items():
            ids = {context.rsplit("|", 1)[0] for context in contexts if context and "|" in context}
            if ids:
                line_contexts[int(line)] = ids
                all_contexts.update(ids)
        scopes, top_level = scope_records(path, line_contexts)
        module_tests = sorted(set().union(*line_contexts.values()) if line_contexts else set())
        source[relative] = {
            "lines": {str(line): sorted(ids) for line, ids in line_contexts.items()},
            "scopes": scopes,
            "top_level": top_level,
            "module_tests": module_tests,
        }

    for test_id in all_contexts:
        test_file = test_id.split("::", 1)[0]
        test_files[test_file].add(test_id)
    if not source or not all_contexts:
        raise SystemExit("coverage data was empty; refusing to build an unsafe selector image")
    fixture_ids = fixtures_map()
    # Resolve a fixture's source-method key to exact node IDs, preserving
    # parameterized cases and class-qualified test IDs.
    fixture_index: dict[str, list[str]] = {}
    for fixture, keys in fixture_ids.items():
        resolved: set[str] = set()
        for key in keys:
            test_file, method = key.split("::", 1)
            resolved.update(
                test_id
                for test_id in test_files.get(test_file, ())
                if test_id.rsplit("::", 1)[-1].split("[", 1)[0] == method
            )
        if resolved:
            fixture_index[fixture] = sorted(resolved)
    dialects = dialect_index(test_files, fixture_index)
    symbols = expression_symbol_index(test_files, fixture_index)
    process_names = process_test_names()
    process_ids = sorted(
        test_id
        for test_id in all_contexts
        if any(
            test_id.split("::", 1)[0] == key.split("::", 1)[0]
            and test_id.rsplit("::", 1)[-1].split("[", 1)[0] == key.rsplit("::", 1)[-1]
            for key in process_names
        )
    )

    catalog = sorted(all_contexts)
    indices = {test_id: index for index, test_id in enumerate(catalog)}
    compact_source = {}
    for filename, item in source.items():
        compact_source[filename] = {
            "lines": {
                line: [indices[test_id] for test_id in values]
                for line, values in item["lines"].items()
            },
            "scopes": [
                [start, end, [indices[test_id] for test_id in values], kind, name]
                for start, end, values, kind, name in item["scopes"]
            ],
            "top_level": item["top_level"],
            "module_tests": [indices[test_id] for test_id in item["module_tests"]],
        }
    compact_test_files = {
        key: [indices[test_id] for test_id in sorted(value)]
        for key, value in test_files.items()
    }
    compact_fixtures = {
        key: [indices[test_id] for test_id in values]
        for key, values in fixture_index.items()
    }
    compact_dialects = {
        key: [indices[test_id] for test_id in values]
        for key, values in dialects.items()
    }
    compact_symbols = {
        key: [indices[test_id] for test_id in values]
        for key, values in symbols.items()
    }
    payload = {
        "revision": PIN,
        "catalog": catalog,
        "source": compact_source,
        "test_files": compact_test_files,
        "fixtures": compact_fixtures,
        "dialects": compact_dialects,
        "symbols": compact_symbols,
        "process_tests": [indices[test_id] for test_id in process_ids],
        "context_count": len(all_contexts),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8", compresslevel=9) as target:
        json.dump(payload, target, separators=(",", ":"))
    OUT.chmod(0o644)
    print(
        f"impact map: {len(source)} source modules, {len(all_contexts)} test contexts, "
        f"{len(fixture_index)} fixture dependencies, {OUT.stat().st_size} bytes",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
