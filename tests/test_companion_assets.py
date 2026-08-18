"""Static contracts for the no-build Companion Home Screen application."""

from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import struct
import unittest


ASSET_DIRECTORY = (
    Path(__file__).resolve().parents[1]
    / "pocket_manga_editor"
    / "companion"
    / "assets"
)


class _DocumentContractParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.links: list[dict[str, str]] = []
        self.metas: list[dict[str, str]] = []
        self.scripts: list[dict[str, str]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        values = {name: value or "" for name, value in attrs}
        if "id" in values:
            self.ids.append(values["id"])
        if tag == "link":
            self.links.append(values)
        elif tag == "meta":
            self.metas.append(values)
        elif tag == "script":
            self.scripts.append(values)


class CompanionAssetContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (ASSET_DIRECTORY / "index.html").read_text(encoding="utf-8")
        self.css = (ASSET_DIRECTORY / "styles.css").read_text(encoding="utf-8")
        self.javascript = (ASSET_DIRECTORY / "app.js").read_text(encoding="utf-8")
        self.parser = _DocumentContractParser()
        self.parser.feed(self.html)

    def test_shell_has_unique_state_library_and_reader_controls(self) -> None:
        self.assertEqual(len(self.parser.ids), len(set(self.parser.ids)))
        required_ids = {
            "state-screen",
            "pair-form",
            "library-screen",
            "library-list",
            "reader-screen",
            "page-image",
            "selection-frame",
            "previous-zone",
            "selection-zone",
            "next-zone",
            "chrome-toggle",
            "back-to-library",
            "volume-picker",
            "selected-picker",
            "page-picker",
            "action-error-retry",
        }
        self.assertTrue(required_ids.issubset(self.parser.ids))

        viewport = next(
            meta["content"]
            for meta in self.parser.metas
            if meta.get("name") == "viewport"
        )
        self.assertIn("viewport-fit=cover", viewport)
        self.assertTrue(
            any(
                link.get("rel") == "manifest"
                and link.get("href") == "/manifest.webmanifest"
                for link in self.parser.links
            )
        )
        self.assertTrue(
            any(script.get("src") == "/assets/app.js" for script in self.parser.scripts)
        )

    def test_reader_layout_keeps_the_required_gesture_and_accessibility_contract(self) -> None:
        self.assertIn("grid-template-columns: 30% 40% 30%", self.css)
        self.assertIn("object-fit: contain", self.css)
        self.assertIn("--reader-image-offset-y: clamp(20px, 3dvh, 28px)", self.css)
        self.assertIn(
            "transform: translateY(var(--reader-image-offset-y))",
            self.css,
        )
        self.assertIn("env(safe-area-inset-top", self.css)
        self.assertIn("env(safe-area-inset-bottom", self.css)
        self.assertIn("prefers-reduced-motion: reduce", self.css)
        self.assertIn("-webkit-touch-callout: none", self.css)
        self.assertIn('target < 0', self.javascript)
        self.assertIn('target >= volume.pages.length', self.javascript)
        self.assertIn("prefetchAdjacentPages", self.javascript)
        self.assertIn("lastVolumeByManga", self.javascript)
        self.assertIn("selection-pending-add", self.javascript)
        self.assertIn("selection-failed", self.javascript)
        self.assertIn("responseRevision >= volume.revision", self.javascript)
        self.assertIn("applySelectionConfirmation", self.javascript)

    def test_javascript_uses_only_the_companion_api_and_exact_write_payloads(self) -> None:
        required_routes = {
            'status: "/api/status"',
            'pair: "/api/pair"',
            'claim: "/api/controller/claim"',
            'heartbeat: "/api/controller/heartbeat"',
            'release: "/api/controller/release"',
            'library: "/api/library"',
            "`/api/manga/${encodeURIComponent(id)}`",
            "`/api/volume/${encodeURIComponent(id)}`",
            "`/api/page/${encodeURIComponent(id)}/image`",
        }
        for route in required_routes:
            with self.subTest(route=route):
                self.assertIn(route, self.javascript)

        self.assertIn(
            'body: { client_id: state.clientId, page_id: state.pageInstanceId }',
            self.javascript,
        )
        self.assertIn('body: { page_id: page.id, selected: desired }', self.javascript)
        self.assertIn('body: { page_id: pageId }', self.javascript)
        self.assertIn('const status = payload.status || payload', self.javascript)
        self.assertIn('"X-Companion-Instance": state.clientId', self.javascript)
        self.assertIn('"X-Companion-Page": state.pageInstanceId', self.javascript)
        self.assertIn("window.sessionStorage.getItem", self.javascript)
        self.assertIn("pageInstanceId: createOpaqueId()", self.javascript)
        self.assertIn(
            "showPage(confirmedIndex, { persist: false })",
            self.javascript,
        )
        self.assertNotIn("innerHTML", self.javascript)
        self.assertNotIn("/api/export", self.javascript)
        self.assertNotIn("/api/complete", self.javascript)

    def test_manifest_and_code_native_icons_are_installable(self) -> None:
        manifest = json.loads(
            (ASSET_DIRECTORY / "manifest.webmanifest").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["id"], "/")
        self.assertEqual(manifest["start_url"], "/")
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["background_color"], "#050706")
        self.assertEqual(manifest["theme_color"], "#050706")

        icon_sources = {icon["src"] for icon in manifest["icons"]}
        self.assertIn("/assets/icon.svg", icon_sources)
        self.assertIn("/assets/icon-180.png", icon_sources)
        self.assertIn("/assets/icon-512.png", icon_sources)
        self.assertIn("<svg", (ASSET_DIRECTORY / "icon.svg").read_text(encoding="utf-8"))
        self.assertEqual(_png_dimensions(ASSET_DIRECTORY / "icon-180.png"), (180, 180))
        self.assertEqual(_png_dimensions(ASSET_DIRECTORY / "icon-512.png"), (512, 512))


def _png_dimensions(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise AssertionError(f"Not a PNG image: {path}")
    return struct.unpack(">II", data[16:24])


if __name__ == "__main__":
    unittest.main()
