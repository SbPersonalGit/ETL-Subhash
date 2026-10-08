from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import polars as pl


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.loader import LoadResult, Loader


def test_append_csv_writes_failed_rows_immediately():
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "Case_failed.csv"
        first = pl.DataFrame({"Id": ["1"], "sf__Error": ["REQUIRED_FIELD_MISSING"]})
        second = pl.DataFrame({"Id": ["2"], "sf__Error": ["DUPLICATE_VALUE"]})

        fields = Loader._append_csv(path, first, None)
        assert path.exists()
        assert "REQUIRED_FIELD_MISSING" in path.read_text(encoding="utf-8")

        Loader._append_csv(path, second, fields)
        text = path.read_text(encoding="utf-8")
        assert "DUPLICATE_VALUE" in text
        assert text.count("\n") >= 3


def test_load_chunks_publishes_failed_file_after_each_batch():
    with tempfile.TemporaryDirectory() as raw:
        tmp_path = Path(raw)
        loader = Loader.__new__(Loader)
        loader.log_root = tmp_path
        events = []
        seen_files = []

        fail_a = pl.DataFrame({"Id": ["a"], "sf__Error": ["bad-a"]})
        fail_b = pl.DataFrame({"Id": ["b"], "sf__Error": ["bad-b"]})
        chunk_a = pl.DataFrame({"Id": ["a"]})
        chunk_b = pl.DataFrame({"Id": ["b"]})

        def fake_process(_object_name, chunk, *_args, **_kwargs):
            result = LoadResult()
            result.total_processed = 1
            result.failed_count = 1
            failed = fail_a if chunk["Id"][0] == "a" else fail_b
            return result, pl.DataFrame(), failed, pl.DataFrame()

        loader._process_chunk = fake_process

        def on_progress(progress):
            events.append(dict(progress))
            failed_file = progress.get("failed_file")
            if failed_file:
                seen_files.append(Path(failed_file).exists())

        result = loader._load_chunks(
            "Case",
            [chunk_a, chunk_b],
            "ExtId__c",
            "upsert",
            False,
            None,
            "20260817_000000",
            1,
            1,
            "upsert",
            on_progress=on_progress,
        )

        failed_path = tmp_path / "load_failed" / "Case_failed_20260817_000000.csv"
        assert failed_path.exists()
        assert events
        assert events[0]["failed_delta"] == 1
        assert events[0]["failed_count"] == 1
        assert Path(events[0]["failed_file"]).exists()
        assert events[1]["failed_count"] == 2
        assert all(seen_files)
        assert result.failed_count == 2
        text = failed_path.read_text(encoding="utf-8")
        assert "bad-a" in text
        assert "bad-b" in text


if __name__ == "__main__":
    from tests._runner import run
    sys.exit(run(globals()))
