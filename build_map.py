"""Build static, test-independent impact data from the pinned source checkout."""

from __future__ import annotations

import ast
import gzip
import io
import json
import re
import sys
import tokenize
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


def safe_comment_gaps(path: Path) -> list[int]:
    """Find line boundaries where a standalone Python comment cannot alter code."""
    try:
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        unsafe = {1, 2}  # Python's encoding cookie is recognized on these lines.
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.STRING and token.start[0] < token.end[0]:
                unsafe.update(range(token.start[0] + 1, token.end[0] + 1))
        for gap in range(2, len(lines) + 2):
            if lines[gap - 2].rstrip().endswith("\\"):
                unsafe.add(gap)
        return [gap for gap in range(1, len(lines) + 2) if gap not in unsafe]
    except (OSError, UnicodeDecodeError, tokenize.TokenError, IndentationError):
        return []


def enum_class_names() -> set[str]:
    """Resolve enum classes through local source inheritance."""
    classes: dict[str, ast.ClassDef] = {}
    for path in (ROOT / "sqlglot").rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                classes[node.name] = node
    enums: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, node in classes.items():
            bases = {base.id for base in node.bases if isinstance(base, ast.Name)}
            if name not in enums and ("Enum" in bases or bases & enums):
                enums.add(name)
                changed = True
    return enums


def identity_class_names() -> set[str]:
    """Resolve source classes that inherit object identity equality."""
    classes: dict[str, ast.ClassDef] = {}
    for path in (ROOT / "sqlglot").rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                module = "sqlglot." + path.relative_to(ROOT / "sqlglot").with_suffix("").as_posix().replace("/", ".")
                classes[f"{module}.{node.name}"] = node
    return {
        qualified
        for qualified, node in classes.items()
        if not node.bases
        and not any(
            isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            and child.name in {"__eq__", "__ne__"}
            for child in node.body
        )
    }


def safe_identity_equal_lines(
    path: Path,
    enum_classes: set[str],
    identity_classes: set[str],
    mutated_attributes: set[str],
) -> list[int]:
    """Find comparisons where identity and equality agree by source type.

    Enum singleton comparisons are safe when the field has a constructor
    invariant. ``is None`` is also safe for an annotated field whose other
    values are source classes inheriting object identity equality. Tests must
    not assign the field directly.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return []

    assignments: list[ast.Assign | ast.AnnAssign] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            assignments.append(node)

    def enum_member(value: ast.AST) -> bool:
        return (
            isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id in enum_classes
        )

    module_name = "sqlglot." + path.relative_to(ROOT / "sqlglot").with_suffix("").as_posix().replace("/", ".")
    imported_classes = {
        alias.asname or alias.name: f"{node.module}.{alias.name}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("sqlglot.")
        for alias in node.names
    }

    # Establish the pinned enum field's constructor invariant from assignments
    # in this source file and enum-valued class constants across this file.
    enum_constants = {
        target.id
        for node in assignments
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
        and enum_member(node.value)
    }
    typed_attributes: set[str] = set()
    identity_typed_attributes: set[str] = set()
    for node in assignments:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)):
                continue
            if target.value.id != "self":
                continue
            value = node.value
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id in enum_classes
            ) or (isinstance(value, ast.Attribute) and value.attr in enum_constants):
                typed_attributes.add(target.attr)
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Attribute):
                annotation_names = {
                    child.id for child in ast.walk(node.annotation) if isinstance(child, ast.Name)
                }
                qualified_annotation_names = {
                    imported_classes.get(name, f"{module_name}.{name}")
                    for name in annotation_names
                }
                if qualified_annotation_names and qualified_annotation_names <= identity_classes:
                    identity_typed_attributes.add(target.attr)

    safe: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1 or len(node.comparators) != 1:
            continue
        if not isinstance(node.ops[0], (ast.Is, ast.IsNot)):
            continue
        left, right = node.left, node.comparators[0]
        pairs = ((left, right), (right, left))
        enum_safe = any(
            isinstance(field, ast.Attribute)
            and isinstance(field.value, ast.Name)
            and field.value.id == "self"
            and field.attr in typed_attributes
            and field.attr not in mutated_attributes
            and enum_member(member)
            for field, member in pairs
        )
        none_safe = (
            isinstance(right, ast.Constant)
            and right.value is None
            and isinstance(left, ast.Attribute)
            and isinstance(left.value, ast.Name)
            and left.value.id == "self"
            and left.attr in identity_typed_attributes
            and left.attr not in mutated_attributes
        ) or (
            isinstance(left, ast.Constant)
            and left.value is None
            and isinstance(right, ast.Attribute)
            and isinstance(right.value, ast.Name)
            and right.value.id == "self"
            and right.attr in identity_typed_attributes
            and right.attr not in mutated_attributes
        )
        if enum_safe or none_safe:
            safe.append(node.lineno)
    return safe


def test_mutated_attributes() -> set[str]:
    """Return attributes directly assigned by the pinned test suite."""
    result: set[str] = set()
    tests = ROOT / "tests"
    for path in tests.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            elif isinstance(node, ast.AugAssign):
                targets = [node.target]
            for target in targets:
                for child in ast.walk(target):
                    if isinstance(child, ast.Attribute):
                        result.add(child.attr)
    return result


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


def dialect_direction_index(
    test_files: dict[str, set[str]], fixtures: dict[str, list[str]], direction: str
) -> dict[str, list[str]]:
    """Map tests that read or write a named dialect, preserving call direction."""
    names = {
        path.stem.removeprefix("test_")
        for path in (ROOT / "tests" / "dialects").glob("test_*.py")
    }
    names.update(
        path.stem
        for path in (ROOT / "sqlglot" / "dialects").glob("*.py")
        if path.name != "__init__.py"
    )
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
            reads: set[str] = set()
            writes: set[str] = set()
            both: set[str] = set()
            for call in (child for child in ast.walk(node) if isinstance(child, ast.Call)):
                for keyword in call.keywords:
                    values = {
                        child.value.lower()
                        for child in ast.walk(keyword.value)
                        if isinstance(child, ast.Constant) and isinstance(child.value, str)
                    }
                    if keyword.arg == "read":
                        reads.update(values)
                    elif keyword.arg == "write":
                        writes.update(values)
                    elif keyword.arg == "dialect":
                        both.update(values)
            for child in ast.walk(node):
                if not isinstance(child, ast.Constant) or not isinstance(child.value, str):
                    continue
                value = child.value.lower()
                reads.update(re.findall(r"read\s*=\s*([a-z_][a-z0-9_]*)", value))
                writes.update(re.findall(r"write\s*=\s*([a-z_][a-z0-9_]*)", value))
                both.update(re.findall(r"dialect\s*=\s*([a-z_][a-z0-9_]*)", value))
            for name in result:
                matched = name in both or name in (reads if direction == "read" else writes)
                if matched:
                    result[name].update(
                        test_id
                        for test_id in ids
                        if test_id.rsplit("::", 1)[-1].split("[", 1)[0] == node.name
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


def dialect_numeric_escape_index(
    test_files: dict[str, set[str]], fixtures: dict[str, list[str]]
) -> dict[str, dict[str, list[str]]]:
    """Index tests whose source inputs contain dialect-specific escape keys."""
    names = {
        path.stem.removeprefix("test_")
        for path in (ROOT / "tests" / "dialects").glob("test_*.py")
    }
    names.update(
        path.stem
        for path in (ROOT / "sqlglot" / "dialects").glob("*.py")
        if path.name != "__init__.py"
    )
    result: dict[str, dict[str, set[str]]] = {
        name.lower(): {key: set() for key in "xXuU0"} for name in names
    }

    def escape_keys(text: str) -> set[str]:
        return set(re.findall(r"\\([xXuU0])", text))

    def dialect_values(value: ast.AST) -> set[str]:
        return {
            child.value.lower()
            for child in ast.walk(value)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        }

    for test_file, ids in test_files.items():
        if not test_file.startswith("tests/") or not test_file.endswith(".py"):
            continue
        path = ROOT / test_file
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        file_dialect = (
            Path(test_file).stem.removeprefix("test_").lower()
            if test_file.startswith("tests/dialects/test_")
            else None
        )
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.name.startswith("test"):
                continue
            keys: set[str] = set()
            dialects = {file_dialect} if file_dialect in result else set()
            for child in ast.walk(node):
                if isinstance(child, ast.Constant) and isinstance(child.value, str):
                    keys.update(escape_keys(child.value))
                if not isinstance(child, ast.Call):
                    continue
                for keyword in child.keywords:
                    if keyword.arg in {"read", "dialect"}:
                        dialects.update(dialect_values(keyword.value) & result.keys())
                func_name = child.func.attr if isinstance(child.func, ast.Attribute) else child.func.id if isinstance(child.func, ast.Name) else ""
                if func_name in {"parse", "parse_one"} and len(child.args) > 1:
                    dialects.update(dialect_values(child.args[1]) & result.keys())
            for dialect in dialects:
                for key in keys:
                    result[dialect][key].update(
                        test_id
                        for test_id in ids
                        if test_id.rsplit("::", 1)[-1].split("[", 1)[0] == node.name
                    )

    for fixture, ids in fixtures.items():
        path = ROOT / fixture
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
        keys = escape_keys(text)
        dialects = set(re.findall(r"(?:#\s*)?dialect\s*:\s*([a-z_][a-z0-9_]*)", text)) & result.keys()
        for dialect in dialects:
            for key in keys:
                result[dialect][key].update(ids)
    return {
        dialect: {key: sorted(ids) for key, ids in values.items()}
        for dialect, values in result.items()
    }


def dialect_week_tests(
    test_files: dict[str, set[str]], fixtures: dict[str, list[str]]
) -> dict[str, list[str]]:
    """Map week-related test inputs to dialects that define WEEK_OFFSET."""
    names = {
        path.stem.removeprefix("test_")
        for path in (ROOT / "tests" / "dialects").glob("test_*.py")
    }
    names.update(
        path.stem
        for path in (ROOT / "sqlglot" / "dialects").glob("*.py")
        if path.name != "__init__.py"
    )
    result: dict[str, set[str]] = {name.lower(): set() for name in names}
    week_pattern = re.compile(r"\b(?:week\w*|day_of_week)\b", re.IGNORECASE)
    for test_file, ids in test_files.items():
        if not test_file.startswith("tests/") or not test_file.endswith(".py"):
            continue
        path = ROOT / test_file
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        file_dialect = (
            Path(test_file).stem.removeprefix("test_").lower()
            if test_file.startswith("tests/dialects/test_")
            else None
        )
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.name.startswith("test"):
                continue
            body = ast.get_source_segment(source, node) or node.name
            if not week_pattern.search(body):
                continue
            dialects = {file_dialect} if file_dialect in result else set()
            for child in ast.walk(node):
                if not isinstance(child, ast.Call):
                    continue
                for keyword in child.keywords:
                    if keyword.arg in {"read", "write", "dialect"}:
                        dialects.update(
                            value.value.lower()
                            for value in ast.walk(keyword.value)
                            if isinstance(value, ast.Constant)
                            and isinstance(value.value, str)
                            and value.value.lower() in result
                        )
            for dialect in dialects:
                result[dialect].update(
                    test_id
                    for test_id in ids
                    if test_id.rsplit("::", 1)[-1].split("[", 1)[0] == node.name
                )
    for fixture, ids in fixtures.items():
        path = ROOT / fixture
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if not week_pattern.search(text):
            continue
        dialects = {
            value.lower()
            for value in re.findall(r"(?:#\s*)?dialect\s*:\s*([a-z_][a-z0-9_]*)", text, re.IGNORECASE)
        } & result.keys()
        for dialect in dialects:
            result[dialect].update(ids)
    return {dialect: sorted(ids) for dialect, ids in result.items()}


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
    enum_classes = enum_class_names()
    identity_classes = identity_class_names()
    mutated_attributes = test_mutated_attributes()
    source: dict[str, dict] = {}
    process_source: dict[str, dict] = {}
    test_files: dict[str, set[str]] = defaultdict(set)
    all_contexts: set[str] = set()
    all_process_contexts: set[str] = set()

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
        process_line_contexts: dict[int, set[str]] = {}
        for line, contexts in raw.items():
            ids: set[str] = set()
            process_ids: set[str] = set()
            for context in contexts:
                if not context or "|" not in context:
                    continue
                test_id, phase = context.rsplit("|", 1)
                (process_ids if phase == "subprocess" else ids).add(test_id)
            if ids:
                line_contexts[int(line)] = ids
                all_contexts.update(ids)
            if process_ids:
                process_line_contexts[int(line)] = process_ids
                all_contexts.update(process_ids)
                all_process_contexts.update(process_ids)
        scopes, top_level = scope_records(path, line_contexts)
        module_tests = sorted(set().union(*line_contexts.values()) if line_contexts else set())
        source[relative] = {
            "lines": {str(line): sorted(ids) for line, ids in line_contexts.items()},
            "scopes": scopes,
            "top_level": top_level,
            "module_tests": module_tests,
            "safe_comment_gaps": safe_comment_gaps(path),
            "safe_identity_equal_lines": safe_identity_equal_lines(
                path, enum_classes, identity_classes, mutated_attributes
            ),
        }
        if process_line_contexts:
            process_scopes, process_top_level = scope_records(path, process_line_contexts)
            process_module_tests = sorted(
                set().union(*process_line_contexts.values()) if process_line_contexts else set()
            )
            process_source[relative] = {
                "lines": {
                    str(line): sorted(ids)
                    for line, ids in process_line_contexts.items()
                },
                "scopes": process_scopes,
                "top_level": process_top_level,
                "module_tests": process_module_tests,
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
    dialect_readers = dialect_direction_index(test_files, fixture_index, "read")
    dialect_writers = dialect_direction_index(test_files, fixture_index, "write")
    dialect_numeric_escapes = dialect_numeric_escape_index(test_files, fixture_index)
    dialect_week = dialect_week_tests(test_files, fixture_index)
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
            "safe_comment_gaps": item["safe_comment_gaps"],
            "safe_identity_equal_lines": item["safe_identity_equal_lines"],
        }
    compact_process_source = {}
    for filename, item in process_source.items():
        compact_process_source[filename] = {
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
    compact_dialect_readers = {
        key: [indices[test_id] for test_id in values]
        for key, values in dialect_readers.items()
    }
    compact_dialect_writers = {
        key: [indices[test_id] for test_id in values]
        for key, values in dialect_writers.items()
    }
    compact_dialect_numeric_escapes = {
        dialect: {
            key: [indices[test_id] for test_id in values]
            for key, values in escape_tests.items()
        }
        for dialect, escape_tests in dialect_numeric_escapes.items()
    }
    compact_dialect_week = {
        dialect: [indices[test_id] for test_id in values]
        for dialect, values in dialect_week.items()
    }
    compact_symbols = {
        key: [indices[test_id] for test_id in values]
        for key, values in symbols.items()
    }
    payload = {
        "revision": PIN,
        "catalog": catalog,
        "source": compact_source,
        "process_source": compact_process_source,
        "process_contexts": [indices[test_id] for test_id in sorted(all_process_contexts)],
        "test_files": compact_test_files,
        "fixtures": compact_fixtures,
        "dialects": compact_dialects,
        "dialect_readers": compact_dialect_readers,
        "dialect_writers": compact_dialect_writers,
        "dialect_numeric_escapes": compact_dialect_numeric_escapes,
        "dialect_week": compact_dialect_week,
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
