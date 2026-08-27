from __future__ import annotations

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
    load_configuration,
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

            from_file = load_configuration(env_file, environ={})
            overridden = load_configuration(
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
            self.assertEqual(
                overridden.working_directory, environment_library.resolve()
            )
            self.assertEqual(overridden.host, "localhost")
            self.assertEqual(overridden.port, 9102)

    def test_defaults_host_and_port_when_only_library_is_configured(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            library = Path(temporary_directory)

            configuration = load_configuration(
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
                        load_configuration(
                            temporary / "missing.env", environ=environment
                        )

    def test_refuses_a_link_or_reparse_point_as_the_library_root(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            library = Path(temporary_directory)
            with patch(
                "pocket_manga_editor.config.is_link_or_reparse",
                side_effect=lambda path: Path(path) == library,
            ):
                with self.assertRaisesRegex(
                    ConfigurationError, "symbolic link or junction"
                ):
                    load_configuration(
                        library / "missing.env",
                        environ={WORKING_DIRECTORY_VARIABLE: str(library)},
                    )

    def test_validates_host_and_port(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            library = Path(temporary_directory)
            env_path = library / "missing.env"
            base = {WORKING_DIRECTORY_VARIABLE: str(library)}

            valid = load_configuration(
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
                        load_configuration(
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
    def test_configuration_failure_stops_before_scanning_or_starting_server(self) -> None:
        with (
            patch.object(app, "_configure_logging"),
            patch.object(
                app,
                "load_configuration",
                side_effect=ConfigurationError("invalid configuration"),
            ),
            patch.object(app, "scan_working_directory") as scan,
            patch.object(app, "CompanionHTTPService") as service_factory,
        ):
            result = app.main()

        self.assertEqual(result, 1)
        scan.assert_not_called()
        service_factory.assert_not_called()

    def test_listener_start_failure_returns_without_entering_wait_loop(self) -> None:
        configuration = SimpleNamespace(
            working_directory=Path("/absolute/test-library"),
            host="0.0.0.0",
            port=8765,
        )
        recovery = SimpleNamespace(recovered_count=0)
        scan_result = SimpleNamespace(mangas=(), issues=())
        service = MagicMock()
        service.start.return_value = SimpleNamespace(
            running=False,
            url="http://127.0.0.1:8765",
            error="address already in use",
        )

        with (
            patch.object(app, "_configure_logging"),
            patch.object(app, "load_configuration", return_value=configuration),
            patch.object(
                app, "recover_interrupted_exports", return_value=recovery
            ),
            patch.object(app, "scan_working_directory", return_value=scan_result),
            patch.object(app, "CompanionCoordinator", return_value=MagicMock()),
            patch.object(app, "CompanionHTTPService", return_value=service),
            patch.object(app.threading, "Event") as event_factory,
        ):
            result = app.main()

        self.assertEqual(result, 1)
        service.start.assert_called_once_with()
        service.stop.assert_called_once_with()
        event_factory.assert_not_called()

    def test_successful_bootstrap_runs_and_shuts_down_cleanly(self) -> None:
        root = Path("/absolute/test-library")
        configuration = SimpleNamespace(
            working_directory=root,
            host="0.0.0.0",
            port=8765,
        )
        recovery = SimpleNamespace(recovered_count=0)
        scan_result = SimpleNamespace(mangas=(object(), object()), issues=())
        coordinator = MagicMock()
        service = MagicMock()
        service.start.return_value = SimpleNamespace(
            running=True,
            url="http://192.168.1.10:8765",
            error=None,
        )
        service.stop.return_value = SimpleNamespace(error=None)
        stop_event = _ImmediateStopEvent(requested=True)

        with (
            patch.object(app, "_configure_logging"),
            patch.object(app, "load_configuration", return_value=configuration),
            patch.object(
                app, "recover_interrupted_exports", return_value=recovery
            ) as recover,
            patch.object(
                app, "scan_working_directory", return_value=scan_result
            ) as scan,
            patch.object(
                app, "CompanionCoordinator", return_value=coordinator
            ) as coordinator_factory,
            patch.object(
                app, "CompanionHTTPService", return_value=service
            ) as service_factory,
            patch.object(app.threading, "Event", return_value=stop_event),
            patch.object(app.signal, "getsignal", return_value=object()),
            patch.object(app.signal, "signal"),
        ):
            result = app.main()

        self.assertEqual(result, 0)
        recover.assert_called_once_with(root)
        scan.assert_called_once_with(root)
        coordinator_factory.assert_called_once_with(root, scan_result)
        service_factory.assert_called_once_with(
            coordinator,
            host="0.0.0.0",
            port=8765,
        )
        service.start.assert_called_once_with()
        service.stop.assert_called_once_with(timeout=10.0)
        coordinator.disconnect_client.assert_called_once_with()
        self.assertEqual(stop_event.wait_calls, 1)

    def test_unexpected_server_stop_returns_failure_and_still_cleans_up(self) -> None:
        configuration = SimpleNamespace(
            working_directory=Path("/absolute/test-library"),
            host="127.0.0.1",
            port=8765,
        )
        recovery = SimpleNamespace(recovered_count=0)
        scan_result = SimpleNamespace(mangas=(), issues=())
        coordinator = MagicMock()
        service = MagicMock()
        service.start.return_value = SimpleNamespace(
            running=True,
            url="http://127.0.0.1:8765",
            error=None,
        )
        service.running = False
        service.error = "listener failed"
        service.stop.return_value = SimpleNamespace(error=None)
        stop_event = _ImmediateStopEvent(requested=False)

        with (
            patch.object(app, "_configure_logging"),
            patch.object(app, "load_configuration", return_value=configuration),
            patch.object(
                app, "recover_interrupted_exports", return_value=recovery
            ),
            patch.object(app, "scan_working_directory", return_value=scan_result),
            patch.object(app, "CompanionCoordinator", return_value=coordinator),
            patch.object(app, "CompanionHTTPService", return_value=service),
            patch.object(app.threading, "Event", return_value=stop_event),
            patch.object(app.signal, "getsignal", return_value=object()),
            patch.object(app.signal, "signal"),
        ):
            result = app.main()

        self.assertEqual(result, 1)
        service.stop.assert_called_once_with(timeout=10.0)
        coordinator.disconnect_client.assert_called_once_with()


class WindowsLauncherContractTests(unittest.TestCase):
    def test_runner_redirects_native_streams_without_powershell_error_records(self) -> None:
        runner = (
            Path(__file__).resolve().parent.parent / "scripts" / "run-server.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("Start-Process", runner)
        self.assertIn("-RedirectStandardError $logFile", runner)
        self.assertIn("-RedirectStandardOutput $standardOutputLog", runner)
        self.assertNotIn(">> $logFile 2>&1", runner)


if __name__ == "__main__":
    unittest.main()
