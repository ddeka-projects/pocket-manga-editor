from __future__ import annotations

import io
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from pocket_manga_editor import app
from pocket_manga_editor.config import (
    ConfigurationError,
    DEFAULT_HOST,
    DEFAULT_PORT,
    HOST_VARIABLE,
    PORT_VARIABLE,
    WORKING_DIRECTORY_VARIABLE,
    load_settings,
)


class ConfigurationTests(unittest.TestCase):
    def test_reads_dotenv_and_process_environment_takes_precedence(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            file_library = temporary / "library-from-file"
            environment_library = temporary / "library-from-environment"
            file_library.mkdir()
            environment_library.mkdir()
            env_file = temporary / ".env"
            env_file.write_text(
                "\n".join(
                    (
                        "# Local defaults",
                        f'export {WORKING_DIRECTORY_VARIABLE}="{file_library}"',
                        f"{HOST_VARIABLE}=127.0.0.1",
                        f"{PORT_VARIABLE}='9001'",
                    )
                ),
                encoding="utf-8",
            )

            from_file = load_settings(env_file, environ={})
            overridden = load_settings(
                env_file,
                environ={
                    WORKING_DIRECTORY_VARIABLE: str(environment_library),
                    HOST_VARIABLE: "localhost",
                    PORT_VARIABLE: "9102",
                },
            )

            self.assertEqual(from_file.working_directory, file_library.resolve())
            self.assertEqual(from_file.host, "127.0.0.1")
            self.assertEqual(from_file.port, 9001)
            self.assertEqual(overridden.working_directory, environment_library.resolve())
            self.assertEqual(overridden.host, "localhost")
            self.assertEqual(overridden.port, 9102)

    def test_defaults_host_and_port_when_only_library_is_configured(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            library = Path(temporary_directory)

            configuration = load_settings(
                library / "missing.env",
                environ={WORKING_DIRECTORY_VARIABLE: str(library)},
            )

            self.assertEqual(configuration.host, DEFAULT_HOST)
            self.assertEqual(configuration.port, DEFAULT_PORT)

    def test_requires_an_absolute_existing_directory(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            regular_file = temporary / "not-a-directory"
            regular_file.write_text("content", encoding="utf-8")

            invalid_environments = (
                {},
                {WORKING_DIRECTORY_VARIABLE: "relative/library"},
                {WORKING_DIRECTORY_VARIABLE: str(temporary / "missing")},
                {WORKING_DIRECTORY_VARIABLE: str(regular_file)},
            )
            for environment in invalid_environments:
                with self.subTest(environment=environment):
                    with self.assertRaises(ConfigurationError):
                        load_settings(temporary / "missing.env", environ=environment)

    def test_refuses_a_link_or_reparse_point_as_the_library_root(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            library = Path(temporary_directory)
            with patch(
                "pocket_manga_editor.config.is_link_or_reparse",
                side_effect=lambda path: Path(path) == library,
            ):
                with self.assertRaisesRegex(ConfigurationError, "symbolic link or junction"):
                    load_settings(
                        library / "missing.env",
                        environ={WORKING_DIRECTORY_VARIABLE: str(library)},
                    )

    def test_validates_host_and_port(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            library = Path(temporary_directory)
            env_path = library / "missing.env"
            base = {WORKING_DIRECTORY_VARIABLE: str(library)}

            valid = load_settings(
                env_path,
                environ={
                    **base,
                    HOST_VARIABLE: "manga-pc.local.",
                    PORT_VARIABLE: "65535",
                },
            )
            self.assertEqual(valid.host, "manga-pc.local")
            self.assertEqual(valid.port, 65535)

            invalid_values = (
                (HOST_VARIABLE, ""),
                (HOST_VARIABLE, "bad host"),
                (HOST_VARIABLE, "::1"),
                (HOST_VARIABLE, "999.1.1.1"),
                (HOST_VARIABLE, "-invalid.local"),
                (PORT_VARIABLE, "not-a-number"),
                (PORT_VARIABLE, "0"),
                (PORT_VARIABLE, "65536"),
            )
            for variable, value in invalid_values:
                with self.subTest(variable=variable, value=value):
                    with self.assertRaises(ConfigurationError):
                        load_settings(
                            env_path,
                            environ={**base, variable: value},
                        )


class _ImmediateStopEvent:
    def __init__(self, *, requested: bool) -> None:
        self.requested = requested
        self.set_calls = 0
        self.wait_calls = 0

    def wait(self, _timeout: float) -> bool:
        self.wait_calls += 1
        return self.requested

    def set(self) -> None:
        self.set_calls += 1
        self.requested = True


class ApplicationBootstrapTests(unittest.TestCase):
    def test_unsupported_reload_is_rejected_before_touching_the_library(self) -> None:
        with (
            patch("sys.stderr", new_callable=io.StringIO) as error_output,
            patch.object(app, "load_settings") as load,
        ):
            with self.assertRaises(SystemExit) as stopped:
                app.main(["--reload"])
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("unrecognized arguments: --reload", error_output.getvalue())
        load.assert_not_called()

    def test_configuration_failure_stops_before_acquiring_or_scanning(self) -> None:
        with (
            patch.object(app, "configure_logging"),
            patch.object(
                app, "load_settings", side_effect=ConfigurationError("invalid configuration")
            ),
            patch.object(app, "scan_library") as scan,
            patch.object(app, "ProcessLock") as lock,
        ):
            self.assertEqual(app.main([]), 1)
        scan.assert_not_called()
        lock.assert_not_called()

    def test_duplicate_server_stops_before_installing_or_scanning(self) -> None:
        configuration = SimpleNamespace(working_directory=Path("/absolute/library"))
        with (
            patch.object(app, "configure_logging"),
            patch.object(app, "load_settings", return_value=configuration),
            patch.object(app, "ProcessLock") as lock,
            patch.object(app, "install_companion") as install,
            patch.object(app, "scan_library") as scan,
        ):
            lock.return_value.__enter__.side_effect = RuntimeError(
                "Another server already uses this data"
            )
            self.assertEqual(app.main([]), 1)
        install.assert_not_called()
        scan.assert_not_called()

    def run_application(self, *, running=True, stop_requested=True, install_error=None):
        root = Path("/absolute/library")
        configuration = SimpleNamespace(working_directory=root, host="127.0.0.1", port=8765)

        def install(_root):
            order.append("install")
            if install_error is not None:
                raise install_error
            return root / ".pocket-manga-editor"

        scan_result = SimpleNamespace(manga_names=(), issues=())
        library_service = MagicMock()
        server = MagicMock()
        server.start.return_value = SimpleNamespace(
            running=running,
            url="http://127.0.0.1:8765/",
            error="listener failed" if not running else None,
        )
        server.running = False
        server.error = "listener failed"
        server.stop.return_value = SimpleNamespace(error=None)
        event = _ImmediateStopEvent(requested=stop_requested)
        order = []
        with (
            patch.object(app, "configure_logging"),
            patch.object(app, "load_settings", return_value=configuration),
            patch.object(app, "ProcessLock") as lock,
            patch.object(app, "install_companion", side_effect=install),
            patch.object(
                app,
                "scan_library",
                side_effect=lambda _root: order.append("scan") or scan_result,
            ),
            patch.object(app, "LibraryService", return_value=library_service),
            patch.object(app, "HTTPService", return_value=server),
            patch.object(app.threading, "Event", return_value=event),
            patch.object(app.signal, "getsignal", return_value=object()),
            patch.object(app.signal, "signal"),
        ):
            lock.return_value.__enter__.side_effect = lambda: order.append("lock")
            lock.return_value.__exit__.side_effect = lambda *_: order.append("unlock")
            server.stop.side_effect = lambda **_: order.append("stop") or SimpleNamespace(
                error=None
            )
            result = app.main([])
        self.assertEqual(order, ["lock", "install", "scan", "stop", "unlock"])
        lock.assert_called_once_with(root)
        server.stop.assert_called_once_with(timeout=10.0)
        library_service.clear_controller.assert_called_once_with()
        return result, event

    def test_listener_start_failure_cleans_up_and_releases_the_guard(self) -> None:
        result, event = self.run_application(running=False)
        self.assertEqual(result, 1)
        self.assertEqual(event.wait_calls, 0)

    def test_successful_bootstrap_shuts_down_before_releasing_the_guard(self) -> None:
        result, event = self.run_application()
        self.assertEqual(result, 0)
        self.assertEqual(event.wait_calls, 1)

    def test_companion_install_failure_does_not_stop_the_server(self) -> None:
        result, event = self.run_application(install_error=PermissionError("read-only"))
        self.assertEqual(result, 0)
        self.assertEqual(event.wait_calls, 1)

    def test_unexpected_listener_stop_returns_failure_and_cleans_up(self) -> None:
        result, event = self.run_application(stop_requested=False)
        self.assertEqual(result, 1)
        self.assertEqual(event.wait_calls, 1)


if __name__ == "__main__":
    unittest.main()
