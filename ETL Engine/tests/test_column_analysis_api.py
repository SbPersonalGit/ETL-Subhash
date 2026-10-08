import csv
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import ui.app as app_module


class ColumnAnalysisApiTest(unittest.TestCase):
    def test_analysis_job_and_downloads(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd() / "temp") as directory:
            root = Path(directory)
            source = root / "sample.csv"
            with source.open("w", encoding="utf-8", newline="") as handle:
                csv.writer(handle).writerows(
                    [["Id", "A", "B"], ["1", "x", ""], ["2", "y", "z"]]
                )
            with patch.object(app_module, "COLUMN_ANALYSIS_DIR", root / "reports"):
                client = app_module.app.test_client()
                response = client.post(
                    "/api/validation/column-analysis",
                    json={"paths": [str(source)], "min_non_empty": 2},
                )
                self.assertEqual(response.status_code, 202)
                activity_key = response.get_json()["activity_key"]

                result = None
                for _ in range(100):
                    result = client.get(
                        f"/api/validation/column-analysis/{activity_key}"
                    ).get_json()
                    if result["status"] in {"COMPLETED", "FAILED"}:
                        break
                    time.sleep(0.01)
                self.assertEqual(result["status"], "COMPLETED")
                self.assertEqual(result["consolidated_qualifying_columns"], 2)
                self.assertEqual(result["completely_empty_columns"], 0)

                download = client.get(result["downloads"]["consolidated"])
                self.assertEqual(download.status_code, 200)
                self.assertIn(b"meets_consolidated_minimum", download.data)
                download.close()


if __name__ == "__main__":
    unittest.main()
