"""Find asset folders that cannot be associated with any Knowledge Article title."""

from __future__ import annotations

import argparse
import csv
import re
from collections import Counter
from pathlib import Path


INVALID_WINDOWS_CHARS = re.compile(r'[<>:"/\\|?*]')
WHITESPACE = re.compile(r"\s+")


def folder_name_for_title(title: str, max_length: int = 60) -> str:
    """Reproduce the filesystem-safe, truncated title used by the asset export."""
    cleaned = INVALID_WINDOWS_CHARS.sub("_", str(title or "").strip())
    cleaned = WHITESPACE.sub(" ", cleaned).rstrip(" .")
    return cleaned[:max_length].rstrip(" .")


def find_orphans(csv_path: Path, folder_root: Path, title_column: str = "Title") -> dict:
    with csv_path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        if title_column not in (reader.fieldnames or []):
            raise ValueError(f"CSV column '{title_column}' was not found")
        titles = [str(row.get(title_column) or "").strip() for row in reader]
    titles = [title for title in titles if title]
    folders = sorted((path for path in folder_root.iterdir() if path.is_dir()), key=lambda p: p.name.casefold())

    normalized_to_titles: dict[str, list[str]] = {}
    for title in titles:
        normalized_to_titles.setdefault(folder_name_for_title(title).casefold(), []).append(title)

    orphans = []
    matches = []
    for folder in folders:
        candidates = normalized_to_titles.get(folder.name.casefold(), [])
        if candidates:
            matches.append((folder, candidates))
        else:
            orphans.append(folder)
    return {
        "titles": titles,
        "folders": folders,
        "matches": matches,
        "orphans": orphans,
        "duplicate_titles": sum(count > 1 for count in Counter(titles).values()),
        "ambiguous_folders": sum(len(candidates) > 1 for _, candidates in matches),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path, help="Knowledge Article CSV containing Title")
    parser.add_argument("folder_root", type=Path, help="Folder containing one asset folder per article")
    parser.add_argument("--title-column", default="Title")
    parser.add_argument("--output", type=Path, default=Path("orphan_knowledge_folders.csv"))
    args = parser.parse_args()
    if not args.csv_path.is_file():
        parser.error(f"CSV was not found: {args.csv_path}")
    if not args.folder_root.is_dir():
        parser.error(f"Folder root was not found: {args.folder_root}")

    result = find_orphans(args.csv_path, args.folder_root, args.title_column)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["OrphanFolderName", "FullPath", "FileCount", "TotalBytes"])
        for folder in result["orphans"]:
            files = [path for path in folder.rglob("*") if path.is_file()]
            writer.writerow([folder.name, str(folder), len(files), sum(path.stat().st_size for path in files)])

    ambiguous_output = args.output.with_name(f"{args.output.stem}_ambiguous.csv")
    with ambiguous_output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["FolderName", "CandidateArticleCount", "CandidateTitles"])
        for folder, candidates in result["matches"]:
            if len(candidates) > 1:
                writer.writerow([folder.name, len(candidates), " | ".join(candidates)])

    print(f"Knowledge titles: {len(result['titles']):,}")
    print(f"Asset folders: {len(result['folders']):,}")
    print(f"Matched folders: {len(result['matches']):,}")
    print(f"Orphan folders: {len(result['orphans']):,}")
    print(f"Ambiguous normalized matches: {result['ambiguous_folders']:,}")
    print(f"Report: {args.output.resolve()}")
    print(f"Ambiguous report: {ambiguous_output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
