#!/usr/bin/env python3
"""Replace exact values across one or more files.

Supported modes:

1. Generic mapping mode
   Replace exact values using a two-column mapping CSV. Supported headers include:
       A,B
       old_value,new_value
       Old_entries,new_entries

2. Salesforce user cleanup mode
   Replace any Salesforce User Id (starts with ``005``) that is not present in the
   target-org user list with a fallback user Id that you provide.

Example:
    python scripts/replace_values_in_files.py \
        --mapping scripts/example_replace_mapping.csv \
        --path "state/flat_file_drop/Case" \
        --path "exports/run_123"

User cleanup example:
    python scripts/replace_values_in_files.py \
        --valid-users-file target_users.csv \
        --default-user-id 005S900000b19USIAY \
        --path "state/flat_file_drop/Case"
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Sequence, Tuple


USER_ID_PATTERN = re.compile(r"005[a-zA-Z0-9]{12}(?:[a-zA-Z0-9]{3})?")


def normalize_header(value: str | None) -> str:
    if value is None:
        return ""
    return value.strip().lower().replace(" ", "_")


def parse_mapping(mapping_path: Path) -> list[tuple[str, str]]:
    with mapping_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"Mapping file '{mapping_path}' is missing a header row.")

        field_names = [normalize_header(name) for name in reader.fieldnames]

        old_key = None
        new_key = None
        for candidate in (
            "a",
            "old",
            "old_value",
            "old_entries",
            "old_entry",
            "from",
            "source",
        ):
            if candidate in field_names:
                old_key = reader.fieldnames[field_names.index(candidate)]
                break
        for candidate in (
            "b",
            "new",
            "new_value",
            "new_entries",
            "new_entry",
            "to",
            "target",
        ):
            if candidate in field_names:
                new_key = reader.fieldnames[field_names.index(candidate)]
                break

        if old_key is None or new_key is None:
            raise ValueError(
                "Mapping file must contain columns like A,B, old_value/new_value, "
                "or Old_entries/new_entries. "
                f"Found columns: {reader.fieldnames}"
            )

        mappings: list[tuple[str, str]] = []
        for row in reader:
            old_value = (row.get(old_key) or "").strip()
            new_value = (row.get(new_key) or "").strip()
            if old_value == "":
                continue
            mappings.append((old_value, new_value))

    if not mappings:
        raise ValueError(f"No valid mapping rows found in '{mapping_path}'.")

    # Replace longest values first to avoid partial overlaps.
    mappings.sort(key=lambda item: len(item[0]), reverse=True)
    return mappings


def parse_user_id(value: str | None) -> str | None:
    if value is None:
        return None

    match = USER_ID_PATTERN.search(value.strip())
    if not match:
        return None
    return match.group(0)


def normalize_user_id_variants(user_id: str) -> set[str]:
    user_id = user_id.strip()
    variants = {user_id}
    if len(user_id) >= 15:
        variants.add(user_id[:15])
    return variants


def parse_valid_users(valid_users_path: Path) -> set[str]:
    if not valid_users_path.exists():
        raise FileNotFoundError(f"Valid users file does not exist: {valid_users_path}")

    text = valid_users_path.read_text(encoding="utf-8-sig", errors="surrogateescape")
    valid_users: set[str] = set()
    for match in USER_ID_PATTERN.finditer(text):
        valid_users.update(normalize_user_id_variants(match.group(0)))

    if not valid_users:
        raise ValueError(
            f"No Salesforce User Ids starting with '005' were found in '{valid_users_path}'."
        )

    return valid_users


def expand_input_paths(paths: Sequence[str]) -> list[Path]:
    expanded: list[Path] = []
    for raw_path in paths:
        p = Path(raw_path).expanduser()
        if not p.exists():
            matches = sorted(Path().glob(raw_path))
            if matches:
                expanded.extend(matches)
                continue
            raise FileNotFoundError(f"Path does not exist: {raw_path}")

        if p.is_dir():
            expanded.extend(sorted(p.rglob("*")))
        else:
            expanded.append(p)

    deduped: list[Path] = []
    seen: set[Path] = set()
    for item in expanded:
        if item.is_file() and item not in seen:
            deduped.append(item)
            seen.add(item)
    return deduped


def load_paths_from_file(paths_file: Path) -> list[Path]:
    if not paths_file.exists():
        raise FileNotFoundError(f"Paths file does not exist: {paths_file}")

    entries: list[str] = []
    try:
        with paths_file.open("r", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames and "path" in [normalize_header(name) for name in reader.fieldnames]:
                for row in reader:
                    val = (row.get(next(name for name in reader.fieldnames if normalize_header(name) == "path")) or "").strip()
                    if val:
                        entries.append(val)
                return expand_input_paths(entries)
    except csv.Error:
        pass

    with paths_file.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            value = line.strip()
            if value:
                entries.append(value)

    return expand_input_paths(entries)


def replace_unknown_user_ids_in_text(
    original: str,
    valid_user_ids: set[str],
    default_user_id: str,
) -> tuple[str, int]:
    replacement_count = 0

    def replace_match(match: re.Match[str]) -> str:
        nonlocal replacement_count
        user_id = match.group(0)
        if user_id in valid_user_ids or user_id[:15] in valid_user_ids:
            return user_id
        if user_id == default_user_id or user_id[:15] == default_user_id[:15]:
            return user_id
        replacement_count += 1
        return default_user_id

    updated = USER_ID_PATTERN.sub(replace_match, original)
    return updated, replacement_count


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replace mapped values or unsupported Salesforce User Ids across target files."
    )
    parser.add_argument(
        "--mapping",
        type=Path,
        help="CSV mapping file with A/B, old_value/new_value, or Old_entries/new_entries columns.",
    )
    parser.add_argument(
        "--valid-users-file",
        type=Path,
        help="CSV or text file containing Salesforce User Ids available in the target org.",
    )
    parser.add_argument(
        "--default-user-id",
        help="Fallback Salesforce User Id used when an unsupported user reference is found.",
    )
    parser.add_argument("--path", action="append", default=[], help="File or directory to process. Repeatable.")
    parser.add_argument("--paths-file", type=Path, help="Text file with one path per line, or CSV with a Path column.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing files.",
    )
    args = parser.parse_args()

    if not args.mapping and not args.valid_users_file:
        print(
            "Error: provide at least one mode: --mapping and/or --valid-users-file.",
            file=sys.stderr,
        )
        return 2

    if args.valid_users_file and not args.default_user_id:
        print(
            "Error: --default-user-id is required when --valid-users-file is used.",
            file=sys.stderr,
        )
        return 2

    replacements: list[tuple[str, str]] = []
    valid_user_ids: set[str] = set()
    default_user_id = ""

    if args.mapping:
        try:
            replacements = parse_mapping(args.mapping)
        except Exception as exc:  # pragma: no cover
            print(f"Error: {exc}", file=sys.stderr)
            return 2

    if args.valid_users_file:
        default_user_id = parse_user_id(args.default_user_id)
        if not default_user_id:
            print(
                "Error: --default-user-id must be a valid Salesforce User Id starting with '005'.",
                file=sys.stderr,
            )
            return 2
        try:
            valid_user_ids = parse_valid_users(args.valid_users_file)
            valid_user_ids.update(normalize_user_id_variants(default_user_id))
        except Exception as exc:  # pragma: no cover
            print(f"Error: {exc}", file=sys.stderr)
            return 2

    target_files: list[Path] = []
    try:
        if args.path:
            target_files.extend(expand_input_paths(args.path))
        if args.paths_file:
            target_files.extend(load_paths_from_file(args.paths_file))
    except Exception as exc:  # pragma: no cover
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if not target_files:
        print("No input files were found for processing.", file=sys.stderr)
        return 2

    target_files = list(dict.fromkeys(target_files))

    files_changed = 0
    total_replacements = 0
    for file_path in target_files:
        if not file_path.is_file():
            continue

        original = file_path.read_text(encoding="utf-8", errors="surrogateescape")
        updated = original
        file_matches = 0

        if replacements:
            for old_value, new_value in replacements:
                count = updated.count(old_value)
                if count:
                    updated = updated.replace(old_value, new_value)
                    file_matches += count

        if valid_user_ids:
            updated, user_replacements = replace_unknown_user_ids_in_text(
                updated,
                valid_user_ids,
                default_user_id,
            )
            file_matches += user_replacements

        if updated == original:
            continue

        files_changed += 1
        total_replacements += file_matches
        if args.dry_run:
            print(f"WOULD CHANGE: {file_path} ({file_matches} replacement(s))")
            continue

        file_path.write_text(updated, encoding="utf-8", errors="surrogateescape")
        print(f"UPDATED: {file_path} ({file_matches} replacement(s))")

    print(f"Completed: {files_changed} file(s) changed, {total_replacements} replacement(s) total.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
