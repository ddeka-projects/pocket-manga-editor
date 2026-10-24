"""Execute ownership behavior in JavaScript without requiring a browser build."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "pocket_manga_editor" / "static"


class FrontendTests(unittest.TestCase):
    def test_ownership_release_takeover_and_uncertain_saves(self):
        runtime = shutil.which("node")
        jsc = Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc")
        if runtime is None and jsc.is_file():
            runtime = str(jsc)
        if runtime is None:
            self.skipTest("Optional JavaScript behavior checks require Node.js or JavaScriptCore.")
        source = (STATIC / "app.js").read_text(encoding="utf-8")
        source = source.replace(
            "  bindEvents();\n  void bootstrap();",
            "  globalThis.client = { state, acquireController, releaseController, loseControl,"
            " requestJson, ensurePositionFlush, drainPendingMutations, setSelection };",
        )
        checks = "\n".join(
            (
                (ROOT / "tests" / "frontend_harness.js").read_text(encoding="utf-8"),
                source,
                (ROOT / "tests" / "frontend_checks.js").read_text(encoding="utf-8"),
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "checks.js"
            script.write_text(checks, encoding="utf-8")
            result = subprocess.run(
                [runtime, str(script)], capture_output=True, text=True, timeout=20
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Frontend behavior checks passed", result.stdout)
