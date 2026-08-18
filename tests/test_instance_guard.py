from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from PySide6.QtCore import QStandardPaths

from pocket_manga_editor.instance_guard import (
    INSTANCE_LOCK_FILENAME,
    InstanceAlreadyRunningError,
    acquire_instance_guard,
    default_instance_lock_path,
)


class InstanceGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.lock_path = Path(self.temporary_directory.name) / "instance.lock"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_default_path_uses_current_users_local_app_data(self) -> None:
        app_data = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppLocalDataLocation
        )

        lock_path = default_instance_lock_path()

        self.assertEqual(lock_path.parent, Path(app_data))
        self.assertEqual(lock_path.name, INSTANCE_LOCK_FILENAME)

    def test_live_duplicate_is_rejected_with_owner_information(self) -> None:
        first = acquire_instance_guard(self.lock_path)
        try:
            with self.assertRaises(InstanceAlreadyRunningError) as raised:
                acquire_instance_guard(self.lock_path)

            error = raised.exception
            self.assertEqual(error.lock_path, self.lock_path)
            self.assertIsNotNone(error.owner)
            self.assertEqual(error.owner.pid, os.getpid())
            self.assertIn(str(os.getpid()), str(error))
            self.assertIn(str(self.lock_path), str(error))
            self.assertIn("Close the existing instance", str(error))
        finally:
            first.release()

        replacement = acquire_instance_guard(self.lock_path)
        replacement.release()

    def test_live_lock_is_not_evicted_because_its_file_is_old(self) -> None:
        first = acquire_instance_guard(self.lock_path)
        try:
            ten_years_ago = time.time() - (10 * 365 * 24 * 60 * 60)
            try:
                os.utime(self.lock_path, (ten_years_ago, ten_years_ago))
            except OSError as exc:  # Some platforms deny changes to open files.
                self.skipTest(f"Cannot change an open lock file timestamp: {exc}")

            with self.assertRaises(InstanceAlreadyRunningError):
                acquire_instance_guard(self.lock_path)
        finally:
            first.release()

    def test_lock_from_crashed_process_is_reclaimed(self) -> None:
        child_code = "\n".join(
            (
                "import os",
                "import sys",
                "from pocket_manga_editor.instance_guard import acquire_instance_guard",
                "guard = acquire_instance_guard(sys.argv[1])",
                "os._exit(0)",
            )
        )
        result = subprocess.run(
            [sys.executable, "-c", child_code, str(self.lock_path)],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.lock_path.exists())

        replacement = acquire_instance_guard(self.lock_path)
        replacement.release()

    def test_unverifiable_lock_is_not_removed_and_has_recovery_guidance(self) -> None:
        self.lock_path.write_text("not a Qt lock file\n", encoding="utf-8")

        with self.assertRaises(InstanceAlreadyRunningError) as raised:
            acquire_instance_guard(self.lock_path)

        error = raised.exception
        self.assertIsNone(error.owner)
        self.assertTrue(self.lock_path.exists())
        self.assertIn("If none is running", str(error))
        self.assertIn(str(self.lock_path), str(error))

    def test_release_is_idempotent_and_context_manager_releases(self) -> None:
        guard = acquire_instance_guard(self.lock_path)
        guard.release()
        guard.release()
        self.assertFalse(guard.is_acquired)

        with acquire_instance_guard(self.lock_path) as active:
            self.assertTrue(active.is_acquired)
        self.assertFalse(active.is_acquired)


if __name__ == "__main__":
    unittest.main()
