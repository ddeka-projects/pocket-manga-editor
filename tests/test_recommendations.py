"""Recommendation behavior, durable recovery, and boundaries with reader data."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pocket_manga_editor.activity import Activity
from pocket_manga_editor.api import API
from pocket_manga_editor.controller import ControllerTransferredError
from pocket_manga_editor.recommendations import (
    RecommendationError,
    RecommendationStore,
    compute_weight,
    eligible,
    empty_preferences,
    is_adult,
    pick_title,
)
from pocket_manga_editor.scanner import scan_library
from pocket_manga_editor.service import LibraryService


NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)


class WeightTests(unittest.TestCase):
    def test_author_prefix_and_filters(self):
        for name, adult in [
            ("[Author] Title", True),
            ("[Author]", True),
            ("Title [Author]", False),
            ("[] Title", False),
            (" [Author] Title", False),
            ("[[Author]] Title", False),
        ]:
            with self.subTest(name=name):
                self.assertEqual(is_adult(name), adult)
        state = {"mangas": {}}
        for filter_value, expected in [("normal", "Normal"), ("adult", "[Author] Adult")]:
            self.assertEqual(
                pick_title(["Normal", "[Author] Adult"], state, filter_value, NOW), expected
            )
        with patch(
            "pocket_manga_editor.recommendations.random.choices", return_value=["Normal"]
        ) as choose:
            pick_title(["Normal", "[Author] Adult"], state, "all", NOW)
        self.assertEqual(choose.call_args.args[0], ["Normal", "[Author] Adult"])
        with self.assertRaises(RecommendationError):
            pick_title([], state, "invalid", NOW)

    def test_weights_and_cooldown_boundaries_follow_media_manager(self):
        preferences = empty_preferences()
        self.assertEqual(compute_weight(preferences, NOW), 1)
        preferences["favorite"] = True
        self.assertEqual(compute_weight(preferences, NOW), 4)
        preferences["enjoyed_count"] = 100
        self.assertEqual(compute_weight(preferences, NOW), 9)
        preferences["less_often"] = True
        self.assertEqual(compute_weight(preferences, NOW), 2.25)
        preferences["last_enjoyed_at"] = NOW.isoformat()
        self.assertAlmostEqual(compute_weight(preferences, NOW), 2.25 * 0.08)
        preferences["last_offered_at"] = NOW.isoformat()
        self.assertAlmostEqual(compute_weight(preferences, NOW), 2.25 * 0.08 * 0.1)
        for key, hours in [("last_too_soon_at", 72), ("last_not_in_mood_at", 48)]:
            preferences = empty_preferences()
            preferences[key] = (NOW - timedelta(hours=hours - 0.01)).isoformat()
            self.assertFalse(eligible(preferences, NOW))
            preferences[key] = (NOW - timedelta(hours=hours)).isoformat()
            self.assertTrue(eligible(preferences, NOW))
        preferences["not_for_me"] = True
        self.assertFalse(eligible(preferences, NOW))


class RecommendationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ["Normal", "[Author] Adult"]:
            folder = self.root / name / "Chapter 1"
            folder.mkdir(parents=True)
            (folder / "1.jpg").write_bytes(b"image")
        self.service = LibraryService(scan_library(self.root))
        self.page = "page"
        self.token = self.service.claim_controller(self.page)["token"]

    def offer(self, filter_value="normal", offer_id="offer-1"):
        return self.service.recommend(self.page, self.token, filter_value, offer_id)

    def resolve(self, feedback="enjoyed", offer_id="offer-1"):
        return self.service.recommendation_feedback(self.page, self.token, offer_id, feedback)

    def preferences(self, name="Normal"):
        return self.service.library(self.page, self.token)["recommendations"]["preferences"][name]

    def test_offer_is_durable_and_retry_returns_same_pending_without_scanning(self):
        first = self.offer()
        pending = first["recommendations"]["pending"]
        self.assertEqual(pending["manga"], "Normal")
        self.assertEqual(pending["filter"], "normal")
        restarted = LibraryService(scan_library(self.root))
        token = restarted.claim_controller("new-page")["token"]
        with patch(
            "pocket_manga_editor.service.scan_manga",
            side_effect=AssertionError("No rescan on retry"),
        ):
            restored = restarted.recommend("new-page", token, "adult", "different-id")
        self.assertEqual(restored["recommendations"]["pending"]["id"], pending["id"])
        self.assertEqual(restored["recommendations"]["pending"]["manga"], "Normal")

    def test_favorite_and_no_feedback_are_independent(self):
        self.offer()
        result = self.service.recommendation_preference(
            self.page, self.token, "Normal", "favorite", True
        )
        self.assertIsNotNone(result["recommendations"]["pending"])
        before = self.preferences()
        self.resolve("no_feedback")
        self.assertEqual(self.preferences(), before)
        self.assertIsNone(self.service.library(self.page, self.token)["recommendations"]["pending"])

    def test_all_feedback_options_and_duplicate_resolution(self):
        for index, feedback in enumerate(
            ["enjoyed", "too_soon", "not_in_mood", "less_often", "not_for_me", "no_feedback"]
        ):
            with self.subTest(feedback=feedback):
                self.service.reset_recommendations(self.page, self.token)
                offer_id = f"offer-{index}"
                self.offer(offer_id=offer_id)
                self.service.recommendation_preference(
                    self.page, self.token, "Normal", "favorite", True
                )
                self.resolve(feedback, offer_id)
                before = self.preferences()
                self.resolve(feedback, offer_id)
                self.assertEqual(self.preferences(), before)
                self.assertEqual(before["enjoyed_count"], int(feedback == "enjoyed"))
                self.assertEqual(before["less_often"], feedback == "less_often")
                self.assertEqual(before["not_for_me"], feedback == "not_for_me")
                self.assertEqual(before["favorite"], feedback != "not_for_me")
                if feedback == "too_soon":
                    self.assertIsNotNone(before["last_too_soon_at"])
                if feedback == "not_in_mood":
                    self.assertIsNotNone(before["last_not_in_mood_at"])
                with self.assertRaises(RecommendationError):
                    self.resolve("not_for_me" if feedback != "not_for_me" else "enjoyed", offer_id)

    def test_pending_blocks_other_manga_edit_rescan_cleanup_and_reset(self):
        library = self.offer()
        normal_id = library["recommendations"]["pending"]["manga_id"]
        adult_id = next(m["id"] for m in library["mangas"] if m["name"].startswith("["))
        for operation in [
            lambda: self.service.open_manga(self.page, self.token, normal_id, Activity.EDIT),
            lambda: self.service.open_manga(self.page, self.token, adult_id, Activity.READ),
            lambda: self.service.scan_manga(self.page, self.token, normal_id),
            lambda: self.service.rescan(self.page, self.token),
            lambda: self.service.cleanup_recommendations(self.page, self.token),
            lambda: self.service.reset_recommendations(self.page, self.token),
            lambda: self.service.recommendation_preference(
                self.page, self.token, "Normal", "less_often", True
            ),
        ]:
            with self.assertRaises(RecommendationError) as caught:
                operation()
            self.assertEqual(caught.exception.code, "feedback_required")
        manga = self.service.open_manga(self.page, self.token, normal_id, Activity.READ)["manga"]
        folder_id = manga["folders"][0]["id"]
        folder = self.service.folder(self.page, self.token, folder_id, Activity.READ)["folder"]
        # Per-page work must not parse the recommendation file or scan the library.
        with patch.object(
            RecommendationStore, "load", side_effect=AssertionError("No per-page load")
        ):
            self.service.set_position(
                self.page, self.token, Activity.READ, folder_id, folder["images"][0]["id"]
            )

    def test_removed_pending_title_can_receive_feedback_then_cleanup_preserves_other_files(self):
        self.offer()
        metadata = self.root / ".pocket-manga-editor"
        read = metadata / "Normal.read.json"
        edit = metadata / "Normal.edit.json"
        read.write_bytes(b"keep read")
        edit.write_bytes(b"keep edit")
        (self.root / "Normal").rename(self.root / "Renamed")
        restarted = LibraryService(scan_library(self.root))
        token = restarted.claim_controller("restarted")["token"]
        pending = restarted.library("restarted", token)["recommendations"]["pending"]
        self.assertIsNone(pending["manga_id"])
        restarted.recommendation_feedback("restarted", token, "offer-1", "enjoyed")
        self.assertEqual(
            restarted.cleanup_recommendations("restarted", token)["orphans"], ["Normal"]
        )
        result = restarted.cleanup_recommendations("restarted", token, ["Normal"])
        self.assertEqual(result["recommendations"]["preferences"], {})
        self.assertEqual(read.read_bytes(), b"keep read")
        self.assertEqual(edit.read_bytes(), b"keep edit")
        self.assertFalse(any("backup" in file.name for file in metadata.iterdir()))

    def test_cleanup_rechecks_live_folders_before_deletion(self):
        self.offer()
        self.resolve()
        (self.root / "Normal").rename(self.root / "Renamed")
        self.assertEqual(
            self.service.cleanup_recommendations(self.page, self.token)["orphans"], ["Normal"]
        )
        (self.root / "Normal").mkdir()
        with self.assertRaises(RecommendationError):
            self.service.cleanup_recommendations(self.page, self.token, ["Normal"])
        self.assertTrue(self.preferences())

    def test_reset_preserves_positions_and_selections(self):
        manga_id = self.service.library(self.page, self.token)["mangas"][0]["id"]
        manga = self.service.open_manga(self.page, self.token, manga_id, Activity.EDIT)["manga"]
        folder_id = manga["folders"][0]["id"]
        folder = self.service.folder(self.page, self.token, folder_id, Activity.EDIT)["folder"]
        self.service.set_selection(
            self.page, self.token, Activity.EDIT, folder_id, folder["images"][0]["id"], True
        )
        edit = next((self.root / ".pocket-manga-editor").glob("*.edit.json"))
        before = edit.read_bytes()
        self.offer()
        self.resolve()
        result = self.service.reset_recommendations(self.page, self.token)
        self.assertEqual(result["recommendations"], {"pending": None, "preferences": {}})
        self.assertEqual(edit.read_bytes(), before)

    def test_unreadable_candidates_are_skipped_and_new_folder_is_eligible_without_refresh(self):
        (self.root / "Empty").mkdir()
        (self.root / "Normal" / "Chapter 1" / "1.jpg").unlink()
        with patch(
            "pocket_manga_editor.recommendations.random.choices",
            side_effect=lambda names, **kwargs: [names[0]],
        ):
            result = self.offer("all")
        self.assertEqual(result["recommendations"]["pending"]["manga"], "[Author] Adult")
        self.assertIn("Empty", [m["name"] for m in result["mangas"]])

    def test_no_eligible_title_creates_no_pending(self):
        self.service.recommendation_preference(self.page, self.token, "Normal", "not_for_me", True)
        before = self.service.library(self.page, self.token)
        with self.assertRaises(RecommendationError) as caught:
            self.offer()
        self.assertEqual(caught.exception.code, "no_recommendations")
        self.assertEqual(
            self.service.library(self.page, self.token)["snapshot_id"], before["snapshot_id"]
        )
        self.assertIsNone(self.service.library(self.page, self.token)["recommendations"]["pending"])

    def test_atomic_failure_and_damaged_json_do_not_overwrite_saved_state(self):
        self.offer()
        path = self.root / ".pocket-manga-editor" / "_recommendations.json"
        original = path.read_bytes()
        with patch("pocket_manga_editor.storage.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(RecommendationError):
                self.resolve()
        self.assertEqual(path.read_bytes(), original)
        self.assertIsNotNone(
            self.service.library(self.page, self.token)["recommendations"]["pending"]
        )
        path.write_text('{"broken": true}', encoding="utf-8")
        with self.assertRaises(RecommendationError):
            self.resolve()
        self.assertEqual(path.read_text(), '{"broken": true}')

    def test_explore_restores_excluded_title(self):
        self.offer()
        self.resolve("not_for_me")
        self.service.recommendation_preference(self.page, self.token, "Normal", "not_for_me", False)
        self.assertEqual(
            self.offer(offer_id="second")["recommendations"]["pending"]["manga"], "Normal"
        )

    def test_takeover_protects_pending_feedback(self):
        self.offer()
        new_token = self.service.claim_controller("other", takeover=True)["token"]
        with self.assertRaises(ControllerTransferredError):
            self.resolve()
        self.service.recommendation_feedback("other", new_token, "offer-1", "enjoyed")
        self.assertEqual(
            self.service.library("other", new_token)["recommendations"]["preferences"]["Normal"][
                "enjoyed_count"
            ],
            1,
        )

    def test_api_ownership_exact_payloads_and_retry(self):
        api = API(self.service)
        headers = {
            "Host": "localhost",
            "Content-Type": "application/json",
            "Origin": "http://localhost",
            "X-Page-Id": self.page,
            "X-Controller-Token": self.token,
        }

        def call(action, payload, custom_headers=headers):
            return api.handle(
                "POST",
                f"/api/recommendations/{action}",
                custom_headers,
                json.dumps(payload).encode(),
            )

        self.assertEqual(
            call(
                "offer",
                {"filter": "normal", "id": "offer-1"},
                {"Host": "localhost", "Content-Type": "application/json"},
            ).status,
            400,
        )
        self.assertEqual(
            call("offer", {"filter": "normal", "id": "offer-1", "extra": True}).status, 400
        )
        self.assertEqual(call("offer", {"filter": "normal", "id": "offer-1"}).status, 200)
        self.assertEqual(
            call("preference", {"manga": "Normal", "flag": "favorite", "value": "true"}).status, 400
        )
        self.assertEqual(call("feedback", {"id": "offer-1", "feedback": "other"}).status, 400)
        self.assertEqual(call("feedback", {"id": "offer-1", "feedback": "enjoyed"}).status, 200)
        self.assertEqual(call("feedback", {"id": "offer-1", "feedback": "enjoyed"}).status, 200)
        self.assertEqual(self.preferences()["enjoyed_count"], 1)

    def test_unsafe_recommendation_path_is_refused(self):
        directory = self.root / ".pocket-manga-editor"
        directory.mkdir()
        (directory / "_recommendations.json").mkdir()
        with self.assertRaises(RecommendationError):
            LibraryService(scan_library(self.root))
