import csv
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "analyze_csv_column_population.py"
SPEC = importlib.util.spec_from_file_location("column_population", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _write(path: Path, rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)


class ColumnPopulationTest(unittest.TestCase):
    def test_file_and_consolidated_counts(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd() / "temp") as directory:
            tmp_path = Path(directory)
            first = tmp_path / "first.csv"
            second = tmp_path / "second.csv"
            _write(first, [["Id", "A", "B"], ["1", "x", ""], ["2", " ", "y"]])
            _write(second, [["Id", "B", "C"], ["3", "z", "c"], ["4", "", ""]])

            stats = [MODULE.analyze_csv(first), MODULE.analyze_csv(second)]
            self.assertEqual(stats[0].total_rows, 2)
            self.assertEqual(stats[0].counts, {"Id": 2, "A": 1, "B": 1})

            by_file = tmp_path / "by_file.csv"
            consolidated = tmp_path / "consolidated.csv"
            MODULE.write_file_report(stats, by_file, minimum=2)
            MODULE.write_consolidated_report(stats, consolidated, minimum=2)

            with consolidated.open(encoding="utf-8-sig", newline="") as handle:
                rows = {row["column"]: row for row in csv.DictReader(handle)}
            self.assertEqual(rows["B"]["non_empty_count"], "2")
            self.assertEqual(rows["B"]["files_present"], "2")
            self.assertEqual(rows["B"]["meets_consolidated_minimum"], "yes")
            self.assertEqual(rows["C"]["meets_consolidated_minimum"], "no")
