"""Static contracts for the no-build Pocket Manga web application."""

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


class WebAssetContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (ASSET_DIRECTORY / "index.html").read_text(encoding="utf-8")
        self.css = (ASSET_DIRECTORY / "styles.css").read_text(encoding="utf-8")
        self.javascript = (ASSET_DIRECTORY / "app.js").read_text(encoding="utf-8")
        self.parser = _DocumentContractParser()
        self.parser.feed(self.html)

    def test_shell_has_unique_library_activity_and_reader_controls(self) -> None:
        self.assertEqual(len(self.parser.ids), len(set(self.parser.ids)))
        required_ids = {
            "state-screen",
            "library-screen",
            "library-refresh",
            "library-list",
            "activity-screen",
            "activity-title",
            "back-to-library",
            "choose-read",
            "choose-edit",
            "choose-export",
            "export-overlay",
            "export-dialog",
            "export-warning",
            "export-warning-samples",
            "export-cancel",
            "export-confirm",
            "reader-screen",
            "image-display",
            "selection-frame",
            "previous-zone",
            "selection-zone",
            "next-zone",
            "chrome-toggle",
            "back-to-activities",
            "folder-picker",
            "selected-picker",
            "image-picker",
            "action-error-retry",
        }
        self.assertTrue(required_ids.issubset(self.parser.ids))
        self.assertNotIn("pair-form", self.parser.ids)

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
        self.assertIn('target >= folder.images.length', self.javascript)
        self.assertIn('void navigateAdjacentEntry(-1)', self.javascript)
        self.assertIn('void navigateAdjacentEntry(1)', self.javascript)
        self.assertIn("prefetchAdjacentImages", self.javascript)
        self.assertIn("selection-pending-add", self.javascript)
        self.assertIn("selection-failed", self.javascript)
        self.assertIn("responseRevision >= folder.revision", self.javascript)
        self.assertIn("selectionRequestTail: Promise.resolve()", self.javascript)
        self.assertIn(
            "state.selectionRequestTail = request.catch(() => {})",
            self.javascript,
        )
        self.assertIn(
            "aggregateAccepted && Number.isInteger(selection.manga_selected_count)",
            self.javascript,
        )
        self.assertIn(
            "The prior folder retains its last confirmed reading position.",
            self.javascript,
        )
        self.assertIn(
            "The prior folder retains its last confirmed selection state.",
            self.javascript,
        )
        self.assertIn("applySelectionConfirmation", self.javascript)
        self.assertIn(
            'id="chrome-toggle" class="tap-zone chrome-toggle-zone"',
            self.html,
        )
        self.assertIn(
            'class="bottom-selection-zone"',
            self.html,
        )
        self.assertIn('id="selection-frame" class="selection-frame" hidden', self.html)
        self.assertIn('state.activity !== EDIT', self.javascript)
        self.assertIn('elements.selectionZone.hidden = !editing', self.javascript)
        self.assertNotIn("Saving selection…", self.javascript)
        self.assertNotIn("Saving deselection…", self.javascript)
        self.assertNotIn("Selected ✓", self.javascript)
        self.assertNotIn('showReaderFeedback(confirmed ? "Selected', self.javascript)
        show_reader = self.javascript.split("function showReader() {", 1)[1].split(
            "function showImage", 1
        )[0]
        self.assertIn("clearReaderFeedback()", show_reader)

    def test_reader_boundaries_open_adjacent_entries_at_the_correct_edge(self) -> None:
        navigation = self.javascript.split(
            "async function navigateAdjacentEntry(direction) {", 1
        )[1].split("function updateReaderLabels", 1)[0]
        self.assertIn("const targetFolderIndex = currentFolderIndex + step", navigation)
        self.assertIn('showBoundaryCue("No Previous Entry", edge)', navigation)
        self.assertIn('showBoundaryCue("No Next Entry", edge)', navigation)
        self.assertIn('step < 0 ? "Previous Entry" : "Next Entry"', navigation)
        self.assertIn('entryEdge: step < 0 ? "last" : "first"', navigation)
        self.assertIn("persist: true", navigation)
        self.assertNotIn('showBoundaryCue("First image"', self.javascript)
        self.assertNotIn('showBoundaryCue("Last image"', self.javascript)

        open_folder = self.javascript.split(
            "async function openFolder(", 1
        )[1].split("function normalizeImage", 1)[0]
        self.assertIn('entryEdge = ""', open_folder)
        self.assertIn('const index = entryEdge === "first"', open_folder)
        self.assertIn('entryEdge === "last"', open_folder)
        self.assertIn("? images.length - 1", open_folder)
        self.assertLess(open_folder.index("entryEdge === \"last\""), open_folder.index("savedIndex >= 0"))

        folder_change = self.javascript.split(
            'elements.folderPicker.addEventListener("change", (event) => {', 1
        )[1].split("elements.selectedPicker", 1)[0]
        self.assertIn(
            'openFolder(folderId, "", { persist: true, entryEdge: "first" })',
            folder_change,
        )
        self.assertNotIn("currentImageId", folder_change)

    def test_javascript_uses_only_the_web_api_and_exact_write_payloads(self) -> None:
        required_routes = {
            'claim: "/api/controller/claim"',
            'heartbeat: "/api/controller/heartbeat"',
            'release: "/api/controller/release"',
            'library: "/api/library"',
            'rescan: "/api/library/rescan"',
            "`/api/manga/${encodeURIComponent(id)}?activity=${encodeURIComponent(activity)}`",
            "`/api/folder/${encodeURIComponent(id)}?activity=${encodeURIComponent(activity)}`",
            "`/api/image/${encodeURIComponent(id)}`",
            "`/api/read/folder/${encodeURIComponent(id)}/position`",
            "`/api/edit/folder/${encodeURIComponent(id)}/position`",
            "`/api/edit/folder/${encodeURIComponent(id)}/selection`",
            "`/api/manga/${encodeURIComponent(id)}/export-preview`",
            "`/api/manga/${encodeURIComponent(id)}/export`",
        }
        for route in required_routes:
            with self.subTest(route=route):
                self.assertIn(route, self.javascript)

        self.assertIn(
            'body: { client_id: state.clientId, page_id: state.pageInstanceId }',
            self.javascript,
        )
        self.assertIn('body: { image_id: image.id, selected: desired }', self.javascript)
        self.assertIn('body: { image_id: pending.imageId }', self.javascript)
        self.assertIn('method: "PATCH"', self.javascript)
        self.assertNotIn('method: "PUT"', self.javascript)
        self.assertIn('"X-Companion-Instance": state.clientId', self.javascript)
        self.assertIn('"X-Companion-Page": state.pageInstanceId', self.javascript)
        self.assertIn("window.sessionStorage.getItem", self.javascript)
        self.assertIn("pageInstanceId: createOpaqueId()", self.javascript)
        self.assertIn(
            "showImage(confirmedIndex, { persist: false })",
            self.javascript,
        )
        self.assertIn('view: "activity"', self.javascript)
        self.assertIn('view: "reader"', self.javascript)
        self.assertIn("window.addEventListener(\"popstate\"", self.javascript)
        self.assertNotIn("innerHTML", self.javascript)
        self.assertNotIn("/api/complete", self.javascript)
        self.assertNotIn("/api/volume", self.javascript)
        self.assertNotIn("/api/page", self.javascript)
        self.assertNotIn("chapterLabel", self.javascript)
        self.assertNotIn('status: "/api/status"', self.javascript)
        self.assertNotIn('pair: "/api/pair"', self.javascript)

    def test_activity_choice_is_explicit_neutral_and_accessible(self) -> None:
        self.assertIn("Choose an activity", self.html)
        self.assertIn("Read without image-selection controls.", self.html)
        self.assertIn("Review and select images for export.", self.html)
        self.assertIn('id="choose-export"', self.html)
        self.assertIn("Replace output with the current selections.", self.html)
        self.assertIn('id="activity-title" tabindex="-1"', self.html)
        self.assertIn('aria-label="Back to library"', self.html)
        self.assertIn("showActivityChoice(manga", self.javascript)
        self.assertIn("chooseActivity(READ)", self.javascript)
        self.assertIn("chooseActivity(EDIT)", self.javascript)
        self.assertIn("void prepareExport()", self.javascript)
        self.assertNotIn("selectedCount: nonNegativeInteger(manga", self.javascript)
        self.assertNotIn("selectedCount: activity === EDIT", self.javascript)
        self.assertNotIn("mangaSelectedCount: activity === EDIT", self.javascript)

        show_library = self.javascript.split(
            'function showLibrary({ historyMode = "none" } = {}) {', 1
        )[1].split("function showActivityChoice", 1)[0]
        self.assertIn("state.viewRequestToken += 1", show_library)
        self.assertIn("state.activityEpoch += 1", show_library)
        self.assertIn("state.activity = null", show_library)
        self.assertIn("state.currentFolder = null", show_library)
        self.assertIn(
            'await chooseActivity(activity, { historyMode: "none" })',
            self.javascript,
        )

    def test_activity_changes_drain_confirmed_mutations_before_rebinding(self) -> None:
        self.assertIn("positionFlushPromise: Promise.resolve()", self.javascript)
        self.assertIn("mutationBarrierTail: Promise.resolve()", self.javascript)
        self.assertIn("function drainPendingMutations()", self.javascript)
        self.assertIn(
            "await Promise.allSettled([positionTail, selectionTail])",
            self.javascript,
        )
        self.assertIn(
            'elements.backToActivities.addEventListener("click", navigateBackAfterMutations)',
            self.javascript,
        )

        choose_activity = self.javascript.split(
            'async function chooseActivity(activity, { historyMode = "push" } = {}) {',
            1,
        )[1].split("function normalizeFolderSummary", 1)[0]
        self.assertLess(
            choose_activity.index("await drainPendingMutations()"),
            choose_activity.index("const requestToken = ++state.viewRequestToken"),
        )
        self.assertLess(
            choose_activity.index("await drainPendingMutations()"),
            choose_activity.index("requestJson(ROUTES.manga"),
        )

        history_change = self.javascript.split(
            "async function historyChanged(event) {", 1
        )[1].split("function setHistory", 1)[0]
        self.assertLess(
            history_change.index("await drainPendingMutations()"),
            history_change.index('historyState.view === "activity"'),
        )

    def test_server_first_startup_claims_one_controller_and_retries_contention(self) -> None:
        self.assertIn("const HEARTBEAT_INTERVAL_MS = 5_000", self.javascript)
        self.assertIn("const OCCUPIED_RETRY_INTERVAL_MS = 3_000", self.javascript)

        bootstrap = self.javascript.split(
            "async function bootstrap() {", 1
        )[1].split("async function claimController", 1)[0]
        self.assertIn("await claimController()", bootstrap)
        self.assertNotIn("ROUTES.status", bootstrap)
        self.assertNotIn("pair", bootstrap.lower())

        occupied = self.javascript.split(
            "function showOccupied() {", 1
        )[1].split("function showUnavailable", 1)[0]
        self.assertIn("Only one page can be active at a time.", occupied)
        self.assertIn("OCCUPIED_RETRY_INTERVAL_MS", occupied)
        self.assertIn("claimController({ quiet: true })", occupied)

        self.assertIn(
            'window.addEventListener("pagehide", releaseController)',
            self.javascript,
        )
        release = self.javascript.split(
            "function releaseController() {", 1
        )[1].split("async function visibilityChanged", 1)[0]
        self.assertIn("keepalive: true", release)
        self.assertNotIn("positionFlushActive", release)
        self.assertNotIn("selectionPending", release)

    def test_rescan_drains_writes_and_replaces_the_library_snapshot(self) -> None:
        rescan = self.javascript.split(
            "async function rescanLibrary() {", 1
        )[1].split("function applyLibraryPayload", 1)[0]
        self.assertLess(
            rescan.index("await drainPendingMutations()"),
            rescan.index("requestJson(ROUTES.rescan"),
        )
        self.assertIn('method: "POST"', rescan)
        self.assertIn("body: {}", rescan)
        self.assertIn("applyLibraryPayload(payload)", rescan)
        self.assertIn('elements.libraryRefresh.disabled = true', rescan)
        self.assertIn('elements.libraryRefresh.disabled = false', rescan)
        self.assertIn(
            'elements.libraryRefresh.addEventListener("click", () => void rescanLibrary())',
            self.javascript,
        )

        apply_library = self.javascript.split(
            "function applyLibraryPayload(payload) {", 1
        )[1].split("function normalizeMangaSummary", 1)[0]
        self.assertIn("state.snapshotId = payload.snapshot_id", apply_library)
        self.assertIn("showLibrary({ historyMode: \"replace\" })", apply_library)

    def test_export_preview_refusal_confirmation_and_long_commit_are_explicit(self) -> None:
        prepare = self.javascript.split(
            "async function prepareExport() {", 1
        )[1].split("function normalizeExportPreview", 1)[0]
        self.assertLess(
            prepare.index("await drainPendingMutations()"),
            prepare.index("requestJson(ROUTES.exportPreview"),
        )
        self.assertIn("preview.selectedImageCount === 0", prepare)
        self.assertIn('"Nothing selected"', prepare)
        self.assertLess(
            prepare.index("preview.selectedImageCount === 0"),
            prepare.index("openExportDialog(preview)"),
        )

        dialog = self.javascript.split(
            "function openExportDialog(preview) {", 1
        )[1].split("function closeExportDialog", 1)[0]
        self.assertIn("Replace existing output?", dialog)
        self.assertIn("Unrecognized output will be deleted", dialog)
        self.assertIn("preview.unrecognizedEntries.slice(0, 6)", dialog)
        self.assertIn('item.textContent = entry', dialog)
        self.assertNotIn("innerHTML", dialog)

        commit = self.javascript.split(
            "async function commitExport() {", 1
        )[1].split("function setActivityActionsDisabled", 1)[0]
        self.assertLess(
            commit.index("await drainPendingMutations()"),
            commit.index("requestJson(ROUTES.exportManga"),
        )
        self.assertIn(
            "confirm_unrecognized_output: preview.unrecognizedEntries.length > 0",
            commit,
        )
        self.assertIn("timeoutMs: EXPORT_TIMEOUT_MS", commit)
        self.assertIn("showWarnings(result.warnings)", commit)
        self.assertIn('elements.exportConfirm.textContent = "Exporting…"', commit)
        self.assertIn('error.code === "nothing_selected"', commit)
        self.assertIn('error.code === "export_confirmation_required"', commit)
        self.assertIn("const EXPORT_TIMEOUT_MS = 10 * 60_000", self.javascript)

        self.assertIn('id="export-dialog"', self.html)
        self.assertIn('role="dialog"', self.html)
        self.assertIn('aria-modal="true"', self.html)
        self.assertIn("Unrecognized files will be permanently deleted", self.html)
        self.assertIn(".dialog-overlay", self.css)
        self.assertIn(".export-warning", self.css)

    def test_manifest_and_code_native_icons_are_installable(self) -> None:
        manifest = json.loads(
            (ASSET_DIRECTORY / "manifest.webmanifest").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["id"], "/")
        self.assertEqual(manifest["start_url"], "/")
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["name"], "Pocket Manga")
        self.assertEqual(manifest["background_color"], "#050706")
        self.assertEqual(manifest["theme_color"], "#050706")
        self.assertNotIn("Companion", self.html)
        self.assertNotIn("Companion", self.css)
        self.assertNotIn("Companion", manifest["name"])
        self.assertNotIn("Companion", manifest["description"])

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
