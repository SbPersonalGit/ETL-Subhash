import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import duckdb

from core import run_audit


def _store_without_initialization(tmp_path: Path) -> run_audit.RunAuditStore:
    store = object.__new__(run_audit.RunAuditStore)
    store.db_path = tmp_path / "run_audit.duckdb"
    store._write_lock = threading.RLock()
    return store


class RunAuditConnectionTests(unittest.TestCase):
    def setUp(self) -> None:
        # _connect is mocked in these unit tests, so no filesystem setup is
        # needed (and no production audit database can be touched).
        self.store = _store_without_initialization(Path("unused-test-path"))

    def test_connect_retries_windows_file_handle_conflict(self) -> None:
        connection = Mock()
        conflict = duckdb.IOException(
            "Cannot access the file because it is being used by another process"
        )

        with (
            patch.object(run_audit, "CONNECT_RETRY_SECONDS", 1),
            patch.object(run_audit.time, "sleep") as sleep,
            patch.object(run_audit.duckdb, "connect", side_effect=[conflict, connection]) as connect,
        ):
            self.assertIs(self.store._connect(), connection)

        self.assertEqual(connect.call_count, 2)
        sleep.assert_called_once_with(run_audit.CONNECT_RETRY_INTERVAL_SECONDS)

    def test_connect_retries_unique_file_handle_conflict(self) -> None:
        connection = Mock()
        conflict = duckdb.BinderException(
            'Unique file handle conflict: Cannot attach "run_audit"'
        )

        with (
            patch.object(run_audit, "CONNECT_RETRY_SECONDS", 1),
            patch.object(run_audit.time, "sleep"),
            patch.object(run_audit.duckdb, "connect", side_effect=[conflict, connection]) as connect,
        ):
            self.assertIs(self.store._connect(), connection)

        self.assertEqual(connect.call_count, 2)

    def test_connect_does_not_retry_unrelated_duckdb_error(self) -> None:
        failure = duckdb.IOException("database file is corrupted")

        with (
            patch.object(run_audit.time, "sleep") as sleep,
            patch.object(run_audit.duckdb, "connect", side_effect=failure) as connect,
            self.assertRaisesRegex(duckdb.IOException, "corrupted"),
        ):
            self.store._connect()

        connect.assert_called_once()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
