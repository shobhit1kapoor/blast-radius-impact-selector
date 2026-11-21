"""Evidence-based test selection for the pinned SQLGlot source tree."""

from __future__ import annotations

import ast
import gzip
import json
import re
import sys
import textwrap
from collections import defaultdict
from pathlib import Path

DATA = Path("/data")
MAP_PATH = Path(__file__).with_name("impact_map.json.gz")
WORKER_COST_MS = 2000
MAX_WORKERS = 8

# These files deliberately get a full-suite fallback. Their dispatch behavior
# can connect a change to tests without a static or observed edge.
DYNAMIC_HUBS = {
    "sqlglot/dialects/__init__.py",
    "sqlglot/dialects/dialect.py",
    "sqlglot/optimizer/__init__.py",
}
DYNAMIC_HUB_SCOPES = {
    "sqlglot/dialects/dialect.py": {"_try_load", "__getitem__", "get", "__new__", "get_or_raise", "classes"},
    "sqlglot/dialects/__init__.py": {"__getattr__"},
    "sqlglot/optimizer/__init__.py": {"__getattr__"},
}


def _load_map() -> dict:
    with gzip.open(MAP_PATH, "rt", encoding="utf-8") as source:
        return json.load(source)


def _parse_patch(path: Path) -> list[dict]:
    """Return each file hunk's changed old lines and insertion points."""
    result: list[dict] = []
    current = ""
    hunk: dict | None = None
    old_line = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("+++ "):
            raw = line[4:].split("\t", 1)[0]
            current = raw[2:] if raw.startswith("b/") else ""
        elif line.startswith("@@ "):
            match = re.match(r"@@ -(\d+)(?:,(\d+))? \+", line)
            if match and current and current != "/dev/null":
                old_line = int(match.group(1))
                hunk = {"path": current, "changed": [], "removed": [], "insertions": []}
                result.append(hunk)
        elif hunk is not None:
            if line.startswith("--- ") or line.startswith("\\"):
                continue
            if line.startswith("-"):
                hunk["changed"].append(old_line)
                hunk["removed"].append((old_line, line[1:]))
                old_line += 1
            elif line.startswith("+"):
                hunk["insertions"].append((old_line, line[1:]))
            elif line.startswith(" "):
                old_line += 1
    return result


def _identity_comparison_equivalent(hunk: dict, file_map: dict) -> bool:
    """Recognize a source-proven identity/equality operator-only edit."""
    removed = hunk["removed"]
    insertions = hunk["insertions"]
    if len(removed) != 1 or len(insertions) != 1:
        return False
    old_line, old_text = removed[0]
    if old_line not in file_map.get("safe_identity_equal_lines", ()):
        return False
    new_text = insertions[0][1]

    def tokens(text: str, old: bool) -> list[str]:
        if old:
            text = re.sub(r"\bis\s+not\b", "!=", text)
            text = re.sub(r"\bis\b", "==", text)
        return re.findall(r"[A-Za-z_]\w*|\d+|==|!=|<=|>=|[^\s]", text)

    return tokens(old_text, True) == tokens(new_text, False)


def _numeric_escape_keys(hunk: dict) -> set[str]:
    """Get dialect escape keys from a changed NUMERIC_ESCAPES class value."""
    removed = hunk["removed"]
    insertions = hunk["insertions"]
    if len(removed) != 1 or len(insertions) != 1:
        return set()
    old_text = removed[0][1]
    new_text = insertions[0][1]
    if "NUMERIC_ESCAPES" not in old_text or "NUMERIC_ESCAPES" not in new_text:
        return set()
    def values(text: str) -> dict[str, str] | None:
        try:
            statement = ast.parse(textwrap.dedent(text)).body[0]
        except (SyntaxError, IndexError):
            return None
        if not isinstance(statement, ast.Assign) or not isinstance(statement.value, ast.Dict):
            return None
        if not any(isinstance(target, ast.Name) and target.id == "NUMERIC_ESCAPES" for target in statement.targets):
            return None
        return {
            key.value: ast.dump(value, include_attributes=False)
            for key, value in zip(statement.value.keys, statement.value.values)
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }

    old_values, new_values = values(old_text), values(new_text)
    if old_values is not None and new_values is not None:
        keys = old_values.keys() | new_values.keys()
        changed = {key for key in keys if old_values.get(key) != new_values.get(key)}
        if changed:
            return changed
    return set(re.findall(r"[\"']([xXuU0])[\"']\s*:", old_text + new_text))


def _class_constant_changed(hunk: dict, constant: str) -> bool:
    removed = hunk["removed"]
    insertions = hunk["insertions"]
    return (
        len(removed) == 1
        and len(insertions) == 1
        and re.search(rf"\b{re.escape(constant)}\s*=", removed[0][1]) is not None
        and re.search(rf"\b{re.escape(constant)}\s*=", insertions[0][1]) is not None
    )


def _scope_tests(file_map: dict, first: int, last: int) -> set[int]:
    tests: set[int] = set()
    lines = file_map.get("lines", {})
    for number in range(max(1, first), max(first, last) + 1):
        tests.update(lines.get(str(number), ()))
    return tests


def _affected_tests(patch: Path, maps: dict, all_ids: set[int]) -> tuple[set[int], bool]:
    chosen: set[int] = set()
    uncertain = False
    unmapped_required = False
    for hunk in _parse_patch(patch):
        filename = hunk["path"]
        filename = filename.replace("\\", "/").lstrip("./")
        changed_lines = hunk["changed"]
        insertions = hunk["insertions"]
        added_text = [text for _, text in insertions]
        comment_only_insert = bool(insertions) and not changed_lines and all(
            text.lstrip().startswith("#") for text in added_text
        )
        comment_directive = any(
            token in text.lower()
            for text in added_text
            for token in ("type:", "noqa", "pragma", "coding", "mypy", "pyright", "coverage")
        )
        if filename.startswith("tests/fixtures/"):
            unmapped_required = True
            fixture_ids = maps["fixtures"].get(filename)
            if fixture_ids:
                chosen.update(fixture_ids)
            else:
                # An unindexed fixture dependency is safer to route through
                # optimizer tests than to treat as harmless data.
                chosen.update(maps["test_files"].get("tests/test_optimizer.py", ()))
            continue

        file_map = maps["source"].get(filename)
        if file_map is None:
            unmapped_required = True
            if filename.startswith("tests/") and filename.endswith(".py"):
                chosen.update(maps["test_files"].get(filename, ()))
            else:
                uncertain = True
            continue

        if _identity_comparison_equivalent(hunk, file_map):
            continue

        escape_keys = _numeric_escape_keys(hunk) if filename.startswith("sqlglot/dialects/") else set()
        escape_tests = maps.get("dialect_numeric_escapes", {}).get(Path(filename).stem)
        if escape_keys and escape_tests is not None:
            for key in escape_keys:
                chosen.update(escape_tests.get(key, ()))
            continue

        if (
            filename.startswith("sqlglot/dialects/")
            and filename != "sqlglot/dialects/dialect.py"
            and _class_constant_changed(hunk, "WEEK_OFFSET")
            and (week_tests := maps.get("dialect_week", {}).get(Path(filename).stem)) is not None
        ):
            chosen.update(week_tests)
            continue

        if (
            comment_only_insert
            and not comment_directive
            and all(line in file_map.get("safe_comment_gaps", ()) for line, _ in insertions)
        ):
            continue

        unmapped_required = True

        if changed_lines:
            first, last = min(changed_lines), max(changed_lines)
            needs_scope_fallback = any(
                not file_map.get("lines", {}).get(str(number)) for number in changed_lines
            )
        elif insertions:
            positions = [line for line, _ in insertions]
            first, last = max(1, min(positions) - 1), max(positions) + 1
            needs_scope_fallback = any(
                not any(
                    file_map.get("lines", {}).get(str(number))
                    for number in range(max(1, position - 1), position + 2)
                )
                for position in positions
            )
        else:
            continue

        process_file = (
            maps.get("process_source", {}).get(filename)
            if filename.startswith("sqlglot/optimizer/")
            else None
        )
        if process_file:
            process_lines = process_file.get("lines", {})
            if changed_lines:
                for line_number in changed_lines:
                    chosen.update(process_lines.get(str(line_number), ()))
            else:
                for line_number in positions:
                    for neighbor in range(max(1, line_number - 1), line_number + 2):
                        chosen.update(process_lines.get(str(neighbor), ()))
            if needs_scope_fallback:
                process_overlapping = [
                    scope
                    for scope in process_file.get("scopes", ())
                    if scope[0] <= last and scope[1] >= first
                ]
                process_functions = [scope for scope in process_overlapping if scope[3] == "function"]
                process_classes = [scope for scope in process_overlapping if scope[3] == "class"]
                process_candidates = process_functions or process_classes
                for scope in process_candidates:
                    if not any(
                        other is not scope
                        and other[0] >= scope[0]
                        and other[1] <= scope[1]
                        and (other[1] - other[0]) < (scope[1] - scope[0])
                        for other in process_candidates
                    ):
                        chosen.update(scope[2])

        if filename in DYNAMIC_HUBS and not comment_only_insert:
            hub_scopes = DYNAMIC_HUB_SCOPES.get(filename, set())
            if any(
                scope[4] in hub_scopes and scope[0] <= last and scope[1] >= first
                for scope in file_map.get("scopes", ())
            ):
                uncertain = True
                continue
            if any(start <= last and end >= first for start, end in file_map.get("top_level", ())):
                uncertain = True
                continue

        # Cross-check exact line evidence with broad, source-derived test
        # families. The pinned suite organizes optimizer, parser, generator,
        # and dialect behavior into shared test modules; dormant input branches
        # can fail tests that never executed the old line during map creation.
        stem = Path(filename).stem
        families = maps["test_files"]
        if filename.startswith("sqlglot/optimizer/"):
            stem_tokens = stem.lower().split("_")
            for index in families.get("tests/test_optimizer.py", ()):
                test_name = maps["catalog"][index].rsplit("::", 1)[-1].split("[", 1)[0].lower()
                generic_pipeline = test_name in {
                    "test_optimize",
                    "test_optimize_special_joins",
                }
                if not comment_only_insert:
                    generic_pipeline = generic_pipeline or test_name in {"test_tpcds", "test_tpch"}
                if any(token in test_name for token in stem_tokens) or generic_pipeline:
                    chosen.add(index)
            if not comment_only_insert and filename not in maps.get("process_source", {}):
                chosen.update(maps.get("process_tests", ()))
        elif filename.startswith("sqlglot/parsers/"):
            # A parser module's whole dialect suite is an expensive last
            # resort. Prefer evidence from the changed parser function and
            # retain cross-component tests derived from explicit dialect use.
            function_evidence = any(
                scope[3] == "function"
                and scope[0] <= last
                and scope[1] >= first
                and scope[2]
                for scope in file_map.get("scopes", ())
            )
            dialect_tests = maps.get("dialect_readers", {}).get(
                stem, maps.get("dialects", {}).get(stem, ())
            )
            dialect_test_file = f"tests/dialects/test_{stem}.py"
            if function_evidence:
                chosen.update(
                    index
                    for index in dialect_tests
                    if maps["catalog"][index].split("::", 1)[0] != dialect_test_file
                )
            else:
                class_evidence = [
                    scope
                    for scope in file_map.get("scopes", ())
                    if scope[3] == "class"
                    and scope[0] <= last
                    and scope[1] >= first
                    and scope[2]
                ]
                if class_evidence:
                    for scope in class_evidence:
                        chosen.update(scope[2])
                else:
                    chosen.update(dialect_tests)
        elif filename.startswith(("sqlglot/generators/", "sqlglot/dialects/", "sqlglot/typing/")):
            dialect_tests = maps.get("dialects", {}).get(stem, ())
            dialect_test_file = f"tests/dialects/test_{stem}.py"
            function_evidence = any(
                scope[3] == "function"
                and scope[0] <= last
                and scope[1] >= first
                and scope[2]
                for scope in file_map.get("scopes", ())
            )
            if function_evidence and filename.startswith("sqlglot/generators/"):
                for scope in file_map.get("scopes", ()):
                    if (
                        scope[3] == "function"
                        and scope[0] <= last
                        and scope[1] >= first
                    ):
                        chosen.update(scope[2])
            elif function_evidence:
                chosen.update(
                    index
                    for index in dialect_tests
                    if maps["catalog"][index].split("::", 1)[0] != dialect_test_file
                )
            else:
                chosen.update(dialect_tests)
        elif filename == "sqlglot/serde.py":
            chosen.update(families.get("tests/test_optimizer.py", ()))
            # Expr.dump, Expr.load, and Expr.__reduce__ call into serde.py.
            # Include their observed caller contexts to catch serialization
            # use through deepcopy/pickle paths that line coverage misses.
            core = maps["source"].get("sqlglot/expressions/core.py", {})
            for start, end, ids, kind, name in core.get("scopes", ()):
                if name in {"dump", "load", "__reduce__"}:
                    chosen.update(ids)
        # Expand to tests exercising each enclosing function/class. This finds
        # tests that execute a branch's peers even when the edited line itself
        # had no coverage at the pinned revision.
        overlapping = [
            scope
            for scope in file_map.get("scopes", ())
            if scope[0] <= last and scope[1] >= first
        ]
        functions = [scope for scope in overlapping if scope[3] == "function"]
        classes = [scope for scope in overlapping if scope[3] == "class"]
        named_classes = [scope for scope in classes if scope[4] in maps.get("symbols", {})]
        if not functions and named_classes:
            for scope in named_classes:
                chosen.update(maps["symbols"][scope[4]])
        else:
            if changed_lines:
                for line_number in changed_lines:
                    chosen.update(_scope_tests(file_map, line_number, line_number))
            else:
                for line_number in positions:
                    chosen.update(_scope_tests(file_map, max(1, line_number - 1), line_number + 1))
        candidates = functions or classes
        innermost = [
            scope
            for scope in candidates
            if not any(
                other is not scope
                and other[0] >= scope[0]
                and other[1] <= scope[1]
                and (other[1] - other[0]) < (scope[1] - scope[0])
                for other in candidates
            )
        ]
        if needs_scope_fallback and not (not functions and named_classes):
            for _, _, ids, _, _ in innermost:
                chosen.update(ids)

        # Module-level edits can affect import-time behavior, so include all
        # tests observed importing/executing this module.
        needs_module_fallback = not comment_only_insert or comment_directive or first <= 2
        if needs_module_fallback and any(
            start <= last and end >= first for start, end in file_map.get("top_level", ())
        ):
            chosen.update(file_map.get("module_tests", ()))

    if uncertain:
        return all_ids, True
    return chosen, unmapped_required


def _schedule(test_numbers: set[int], tests: list[dict]) -> list[list[int]]:
    by_number = {item["number"]: item for item in tests}
    ordered = sorted(
        (by_number[n] for n in test_numbers),
        key=lambda item: (-round(item["seconds"] * 1000), item["number"]),
    )
    best_workers: list[list[int]] = []
    best_cost = 0
    for count in range(1, min(MAX_WORKERS, len(ordered)) + 1):
        workers: list[list[int]] = [[] for _ in range(count)]
        loads = [0] * count
        for item in ordered:
            lightest = min(range(count), key=lambda i: (loads[i], i))
            workers[lightest].append(item["number"])
            loads[lightest] += round(item["seconds"] * 1000)
        cost = max(loads) + WORKER_COST_MS * count
        if cost < best_cost or not best_workers:
            best_cost, best_workers = cost, workers
    return best_workers


def main() -> None:
    maps = _load_map()
    tests = json.loads((DATA / "tests.json").read_text(encoding="utf-8"))["tests"]
    id_to_number = {item["id"]: item["number"] for item in tests}
    catalog = maps["catalog"]
    all_ids = set(range(len(catalog)))
    unmapped_numbers = {
        item["number"] for item in tests if item["id"] not in set(catalog)
    }
    output = []
    for patch in sorted((DATA / "commits").glob("*.patch")):
        indices, unmapped_required = _affected_tests(patch, maps, all_ids)
        numbers = {
            id_to_number[catalog[index]]
            for index in indices
            if index < len(catalog) and catalog[index] in id_to_number
        }
        if unmapped_required:
            numbers.update(unmapped_numbers)
        workers = _schedule(numbers, tests)
        output.append({"commit": patch.stem, "workers": workers})
    for item in output:
        print(json.dumps(item, separators=(",", ":")))
    print(f"selected {sum(sum(len(w) for w in x['workers']) for x in output)} test instances", file=sys.stderr)


if __name__ == "__main__":
    main()
