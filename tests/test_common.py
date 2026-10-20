"""Behavioral checks for the operating conventions shared by all Pocket apps."""

from __future__ import annotations

import importlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT.name.replace("-", "_")
env = importlib.import_module(PACKAGE + ".env")
controller = importlib.import_module(PACKAGE + ".controller")
process_lock = importlib.import_module(PACKAGE + ".process_lock")
logging_setup = importlib.import_module(PACKAGE + ".logging_setup")


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / ".env"

    def test_literal_windows_paths_quotes_bom_and_environment_override(self):
        self.path.write_text(
            "\ufeff# Literal values, not shell expressions\n"
            'WINDOWS_PATH="C:\\Users\\Pocket\\New folder\\video.mp4"\n'
            "LITERAL='$HOME/${HOME}/%USERPROFILE%/#part'\n"
            "PORT=8000\nEMPTY=\nexport EXTRA=literal=value\n",
            encoding="utf-8",
        )
        values = env.load_env(self.path, environ={"PORT": "8766", "ONLY_ENV": "yes"})
        self.assertEqual(r"C:\Users\Pocket\New folder\video.mp4", values["WINDOWS_PATH"])
        self.assertEqual("$HOME/${HOME}/%USERPROFILE%/#part", values["LITERAL"])
        self.assertEqual("8766", values["PORT"])
        self.assertEqual("", values["EMPTY"])
        self.assertEqual("literal=value", values["EXTRA"])
        self.assertEqual("yes", values["ONLY_ENV"])

    def test_missing_file_is_allowed_without_changing_the_environment(self):
        self.assertEqual(
            {"FROM_ENV": "value"}, env.load_env(self.path, environ={"FROM_ENV": "value"})
        )

    def test_duplicate_invalid_and_unclosed_assignments_are_rejected(self):
        for text in (
            "PORT=1\nPORT=2\n",
            "export PORT=1\nPORT=2\n",
            "NOT-VALID=1\n",
            "NO_ASSIGNMENT\n",
            'PATH="unterminated\n',
            "PATH='mismatched\"\n",
        ):
            with self.subTest(text=text):
                self.path.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    env.load_env(self.path, environ={})

    def test_configuration_directory_and_file_link_are_rejected(self):
        self.path.mkdir()
        with self.assertRaises(ValueError):
            env.load_env(self.path, environ={})
        self.path.rmdir()
        target = self.path.with_name("actual.env")
        target.write_text("VALUE=yes\n", encoding="utf-8")
        try:
            self.path.symlink_to(target)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"File symlink creation is unavailable: {error}")
        with self.assertRaises(ValueError):
            env.load_env(self.path, environ={})


class ControllerTests(unittest.TestCase):
    def test_one_page_claim_takeover_stale_token_and_release(self):
        owner = controller.ControllerOwnership()
        first = owner.claim("page-a")["token"]
        owner.authorize("page-a", first)
        with self.assertRaises(controller.ControllerError) as occupied:
            owner.claim("page-b")
        self.assertEqual("controller_occupied", occupied.exception.code)
        second = owner.takeover("page-b")["token"]
        self.assertNotEqual(first, second)
        for page, token in (("page-a", first), ("page-b", first), ("page-b", "")):
            with self.subTest(page=page, token=token):
                with self.assertRaises(controller.ControllerError):
                    owner.authorize(page, token)
        with self.assertRaises(controller.ControllerError):
            owner.release("page-a", first)
        owner.authorize("page-b", second)
        owner.release("page-b", second)
        self.assertEqual({"claimed": False}, owner.snapshot())
        with self.assertRaises(controller.ControllerError) as required:
            owner.authorize("page-b", second)
        self.assertEqual("controller_required", required.exception.code)
        owner.claim("page-c")

    def test_idle_ownership_needs_no_renewal_and_exposes_no_expiry(self):
        owner = controller.ControllerOwnership()
        token = owner.claim("idle-page")["token"]
        # No heartbeat is sent between complete independent operations.
        with owner.operation("idle-page", token):
            pass
        self.assertEqual({"claimed": True}, owner.snapshot())
        with owner.operation("idle-page", token):
            pass
        owner.disconnect()
        self.assertEqual({"claimed": False}, owner.snapshot())

    def test_invalid_page_id_is_rejected_without_claiming(self):
        owner = controller.ControllerOwnership()
        for value in (None, "", [], "not a page", "x" * 129, "\u2603"):
            with self.subTest(value=value):
                with self.assertRaises(controller.ControllerError):
                    owner.claim(value)
                self.assertEqual({"claimed": False}, owner.snapshot())

    def test_takeover_waits_for_complete_authorized_operation(self):
        owner = controller.ControllerOwnership()
        token = owner.claim("first-page")["token"]
        entered = threading.Event()
        finish = threading.Event()
        attempting = threading.Event()
        transferred = threading.Event()
        failures = []

        def operation():
            try:
                with owner.operation("first-page", token):
                    entered.set()
                    if not finish.wait(3):
                        raise RuntimeError("The test did not finish its operation")
                    owner.authorize("first-page", token)
            except BaseException as error:
                failures.append(error)

        def takeover():
            try:
                attempting.set()
                owner.takeover("second-page")
                transferred.set()
            except BaseException as error:
                failures.append(error)

        working = threading.Thread(target=operation)
        taking = threading.Thread(target=takeover)
        working.start()
        try:
            self.assertTrue(entered.wait(1))
            taking.start()
            self.assertTrue(attempting.wait(1))
            self.assertFalse(transferred.wait(0.05))
        finally:
            finish.set()
            working.join(3)
            if taking.ident is not None:
                taking.join(3)
        self.assertFalse(working.is_alive())
        self.assertFalse(taking.is_alive())
        self.assertEqual([], failures)
        self.assertTrue(transferred.is_set())
        with self.assertRaises(controller.ControllerError):
            owner.authorize("first-page", token)


class ProcessGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.data = self.directory / "library.data"

    def test_release_reacquires_and_different_data_can_run(self):
        with process_lock.ProcessLock(self.data):
            with self.assertRaises(RuntimeError):
                process_lock.ProcessLock(self.data).acquire()
            with process_lock.ProcessLock(self.directory / "other.data"):
                pass
        with process_lock.ProcessLock(self.data):
            pass

    def test_duplicate_subprocess_guard_survives_temp_override_and_releases_after_crash(self):
        ready = self.directory / "ready"
        different_temp = self.directory / "different-temp"
        different_temp.mkdir()
        script = (
            "import importlib,pathlib,sys; "
            "sys.path.insert(0,sys.argv[1]); "
            "m=importlib.import_module(sys.argv[2]+'.process_lock'); "
            "guard=m.ProcessLock(sys.argv[3]).acquire(); "
            "pathlib.Path(sys.argv[4]).write_text('ready'); "
            "sys.stdin.buffer.read()"
        )
        child_env = dict(
            os.environ,
            TMPDIR=str(different_temp),
            TMP=str(different_temp),
            TEMP=str(different_temp),
        )
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(ROOT), PACKAGE, str(self.data), str(ready)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env,
        )
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            if not ready.exists():
                child.kill()
                output, error = child.communicate(timeout=3)
                self.fail(f"Child guard did not start: {output!r} {error!r}")
            with self.assertRaises(RuntimeError):
                process_lock.ProcessLock(self.data).acquire()
            # Kill, rather than politely exiting, to verify OS-owned cleanup.
            child.kill()
            child.communicate(timeout=3)
            self.assertNotEqual(0, child.returncode)
            with process_lock.ProcessLock(self.data):
                pass
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=3)


class ReadinessTests(unittest.TestCase):
    def test_readiness_identifies_app_and_stable_process_instance(self):
        health = importlib.import_module(PACKAGE + ".health")
        first = health.make_health()
        self.assertTrue(first["ok"])
        self.assertEqual(PACKAGE, first["app"])
        self.assertTrue(first["instance_id"])
        self.assertEqual(first, health.make_health())
        script = (
            "import importlib,json,sys; sys.path.insert(0,sys.argv[1]); "
            "m=importlib.import_module(sys.argv[2]+'.health'); print(json.dumps(m.make_health()))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(ROOT), PACKAGE],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        another = json.loads(result.stdout)
        self.assertEqual(PACKAGE, another["app"])
        self.assertNotEqual(first["instance_id"], another["instance_id"])


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.logger = logging.getLogger()
        self.old_handlers = self.logger.handlers[:]
        self.old_level = self.logger.level
        self.old_stdout, self.old_stderr = sys.stdout, sys.stderr
        self.logger.handlers = []
        self.addCleanup(self.restore_logging)

    def restore_logging(self):
        sys.stdout, sys.stderr = self.old_stdout, self.old_stderr
        for handler in self.logger.handlers[:]:
            self.logger.removeHandler(handler)
            handler.close()
        self.logger.handlers = self.old_handlers
        self.logger.setLevel(self.old_level)

    def test_idempotent_configuration_emits_each_record_once(self):
        path = logging_setup.configure_logging(console=False, root=self.root)
        self.assertEqual(self.root / "logs/server.log", path)
        self.assertEqual(path, logging_setup.configure_logging(console=False, root=self.root))
        logging.getLogger("shared-test").info("one record marker")
        self.assertEqual(1, path.read_text(encoding="utf-8").count("one record marker"))

    def test_log_rotation_bounds_retained_files_and_keeps_latest_record(self):
        path = logging_setup.configure_logging(console=False, root=self.root)
        logger = logging.getLogger("rotation-test")
        payload = "x" * (1024 * 1024)
        for number in range(24):
            logger.info("record %s %s", number, payload)
        logger.info("latest record marker")
        files = list(path.parent.glob("server.log*"))
        self.assertEqual(4, len(files))
        self.assertTrue(all(file.stat().st_size <= 5 * 1024 * 1024 for file in files))
        self.assertIn("latest record marker", path.read_text(encoding="utf-8"))

    def test_redirected_standard_streams_log_without_recursive_console(self):
        path = logging_setup.configure_logging(console=False, root=self.root)
        logging_setup.redirect_standard_streams()
        logging_setup.configure_logging(console=True, root=self.root)
        print("stdout marker")
        sys.stderr.write("stderr marker\n")
        sys.stdout.write("partial marker")
        sys.stdout.flush()
        sys.stderr.flush()
        text = path.read_text(encoding="utf-8")
        for marker in ("stdout marker", "stderr marker", "partial marker"):
            self.assertEqual(1, text.count(marker))


if __name__ == "__main__":
    unittest.main()
