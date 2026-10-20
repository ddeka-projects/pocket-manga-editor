"""Execute real lifecycle scripts with mocked OS adapters in disposable fixtures."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

REPOSITORY = Path(__file__).resolve().parents[1]
POWERSHELL = (
    os.environ.get("POCKET_TEST_POWERSHELL") or shutil.which("pwsh") or shutil.which("powershell")
)


@unittest.skipUnless(
    POWERSHELL, "PowerShell unavailable; set POCKET_TEST_POWERSHELL to run lifecycle checks"
)
class WindowsLifecycleTests(unittest.TestCase):
    def check_scenario(self, scenario):
        with TemporaryDirectory(prefix="pocket scripts spaces ") as temporary:
            fixture = Path(temporary)
            shutil.copytree(REPOSITORY / "scripts", fixture / "scripts")
            shutil.copyfile(REPOSITORY / ".env.example", fixture / ".env.example")
            shutil.copyfile(REPOSITORY / "requirements.txt", fixture / "requirements.txt")
            (fixture / ".env").write_text("sentinel-existing-settings\n", encoding="utf-8")
            venv = fixture / ".venv" / "Scripts"
            venv.mkdir(parents=True)
            (venv / "python.exe").touch()
            (venv / "pythonw.exe").touch()
            # Identity is an OS boundary. Replace only that native expression;
            # task registration/ordering, flags, errors and state loops stay real.
            installer = fixture / "scripts" / "install-startup-task.ps1"
            installer.write_text(
                installer.read_text().replace(
                    "[Security.Principal.WindowsIdentity]::GetCurrent()",
                    "[pscustomobject]@{ Name = 'POCKET\\Tester' }",
                ),
                encoding="utf-8",
            )
            harness = (REPOSITORY / "tests" / "windows_lifecycle_checks.ps1").read_text()
            adapter = harness.split("function global:Install-PocketTestAdapters {", 1)[1].split(
                "\n}\n\n$failure", 1
            )[0]
            (fixture / "test-adapter.ps1").write_text(
                adapter.replace("function global:", "function "), encoding="utf-8"
            )
            common = fixture / "scripts" / "windows-common.ps1"
            common.write_text(
                common.read_text() + "\n. (Join-Path $PocketRoot 'test-adapter.ps1')\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    POWERSHELL,
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(REPOSITORY / "tests" / "windows_lifecycle_checks.ps1"),
                    "-FixtureRoot",
                    str(fixture),
                    "-Scenario",
                    scenario,
                    "-PythonExecutable",
                    sys.executable,
                ],
                text=True,
                capture_output=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            lines = [
                line
                for line in result.stdout.splitlines()
                if line.startswith("POCKET_TEST_RESULT=")
            ]
            self.assertTrue(lines, result.stdout + result.stderr)
            report = json.loads(lines[-1].partition("=")[2])
            self.assertEqual(report["scenario"], scenario)
            if scenario == "restart-cancel":
                self.assertIn("Administrator request cancelled", result.stdout)
            if scenario == "restart-normal":
                self.assertIn("Restart complete", result.stdout)

    def test_parse_all_powershell_scripts(self):
        self.check_scenario("parse")

    def test_install_and_remove_without_elevation_or_firewall_changes(self):
        for scenario in ("install-normal", "remove-normal"):
            with self.subTest(scenario=scenario):
                self.check_scenario(scenario)

    def test_firewall_is_explicit_and_admin_gate_precedes_changes(self):
        for scenario in (
            "install-admin-gate",
            "remove-admin-gate",
            "install-firewall",
            "remove-firewall",
        ):
            with self.subTest(scenario=scenario):
                self.check_scenario(scenario)

    def test_restart_stops_waits_starts_and_pauses(self):
        self.check_scenario("restart-normal")

    def test_failed_preflight_or_stop_never_starts_another_instance(self):
        for scenario in (
            "restart-missing",
            "restart-disabled",
            "restart-config-error",
            "restart-stop-timeout",
        ):
            with self.subTest(scenario=scenario):
                self.check_scenario(scenario)

    def test_readiness_rejects_old_or_unrelated_server(self):
        for scenario in ("restart-old-health", "restart-wrong-health"):
            with self.subTest(scenario=scenario):
                self.check_scenario(scenario)

    def test_uac_is_conditional_cancellable_and_never_repeated(self):
        for scenario in ("restart-access", "restart-cancel", "restart-elevated"):
            with self.subTest(scenario=scenario):
                self.check_scenario(scenario)

    def test_bootstrap_preserves_configuration(self):
        self.check_scenario("bootstrap-preserve")

    def test_native_stderr_does_not_hide_success_or_failure_exit_code(self):
        self.check_scenario("native")


class RestartWrapperTests(unittest.TestCase):
    def test_double_click_wrapper_uses_own_directory_and_holds_failures_open(self):
        wrapper = (REPOSITORY / "restart-server.cmd").read_text()
        self.assertIn("%~dp0scripts\\restart-server.ps1", wrapper)
        self.assertIn("-Pause", wrapper)
        self.assertIn("pause", wrapper.lower())
        self.assertIn("exit /b", wrapper.lower())
        self.assertIn("powershell.exe", wrapper.lower())


if __name__ == "__main__":
    unittest.main()
