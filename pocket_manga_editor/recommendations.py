"""Small, durable recommendation state and weighted title selection."""

from __future__ import annotations

from datetime import datetime
import json
import logging
import math
from pathlib import Path
import random
import re
from typing import Callable

from .errors import AppError
from .library_lock import library_mutation_lock
from .storage import atomic_write_json
from .workspace import recommendations_metadata_path


FILTERS = {"all", "normal", "adult"}
FEEDBACK = {"enjoyed", "too_soon", "not_in_mood", "less_often", "not_for_me", "no_feedback"}
FLAGS = {"favorite", "less_often", "not_for_me"}
TIMES = {"last_offered_at", "last_enjoyed_at", "last_too_soon_at", "last_not_in_mood_at"}
LOGGER = logging.getLogger(__name__)


class RecommendationError(AppError):
    def __init__(self, message: str, code: str = "recommendation_error", status: int = 409):
        super().__init__(message, status, code)


def is_adult(name: str) -> bool:
    return re.match(r"^\[[^\[\]]+\]", name) is not None


def empty_preferences() -> dict:
    return {
        "favorite": False,
        "less_often": False,
        "not_for_me": False,
        "enjoyed_count": 0,
        "last_offered_at": None,
        "last_enjoyed_at": None,
        "last_too_soon_at": None,
        "last_not_in_mood_at": None,
    }


def empty_state() -> dict:
    return {"pending": None, "last_resolution": None, "mangas": {}}


def elapsed_hours(value: str | None, now: datetime) -> float:
    return (
        float("inf")
        if value is None
        else max(0.0, (now - datetime.fromisoformat(value)).total_seconds() / 3600)
    )


def eligible(preferences: dict, now: datetime) -> bool:
    return (
        not preferences["not_for_me"]
        and elapsed_hours(preferences["last_too_soon_at"], now) >= 72
        and elapsed_hours(preferences["last_not_in_mood_at"], now) >= 48
    )


def compute_weight(preferences: dict, now: datetime) -> float:
    weight = 4.0 if preferences["favorite"] else 1.0
    weight *= 1 + 0.25 * min(preferences["enjoyed_count"], 5)
    if preferences["less_often"]:
        weight *= 0.25
    weight *= min(1.0, max(0.08, elapsed_hours(preferences["last_enjoyed_at"], now) / 24))
    weight *= min(1.0, max(0.1, elapsed_hours(preferences["last_offered_at"], now) / (5 / 60)))
    return max(weight, 0.01)


def pick_title(names: list[str], state: dict, filter_value: str, now: datetime) -> str | None:
    if filter_value not in FILTERS:
        raise RecommendationError("Choose All, Normal only, or 18+ only.", status=400)
    candidates = []
    weights = []
    for name in names:
        if filter_value != "all" and is_adult(name) != (filter_value == "adult"):
            continue
        preferences = state["mangas"].get(name, empty_preferences())
        if eligible(preferences, now):
            candidates.append(name)
            weights.append(compute_weight(preferences, now))
    return random.choices(candidates, weights=weights, k=1)[0] if candidates else None


class RecommendationStore:
    """Cache state between recommendation actions; page turns never read this file."""

    def __init__(self, working_directory: Path):
        self.root = working_directory
        self.state = self.load()

    def _path(self) -> Path:
        return recommendations_metadata_path(self.root)

    def load(self) -> dict:
        try:
            path = self._path()
            if not path.exists():
                return empty_state()
            with path.open(encoding="utf-8") as handle:
                state = json.load(handle)
            _validate_state(state)
            return state
        except (OSError, ValueError, TypeError, KeyError) as exc:
            LOGGER.warning("Recommendation metadata could not be loaded: %s", exc)
            raise RecommendationError(
                "Recommendation metadata could not be read safely. "
                "Check _recommendations.json; it has been left untouched.",
                "recommendation_metadata_invalid",
            ) from exc

    def mutate(self, update: Callable[[dict], None]) -> dict:
        try:
            with library_mutation_lock(self.root):
                updated = self.load()
                update(updated)
                _validate_state(updated)
                atomic_write_json(self._path(), updated)
            self.state = updated
            return updated
        except RecommendationError:
            raise
        except (OSError, ValueError) as exc:
            LOGGER.warning("Recommendation metadata could not be saved: %s", exc)
            raise RecommendationError(
                "Recommendation changes could not be saved. Retry when the PC is ready.",
                "recommendation_save_failed",
                503,
            ) from exc

    def offer(self, name: str, filter_value: str, offer_id: str, now: datetime) -> dict:
        if not _name(name) or not _name(offer_id) or filter_value not in FILTERS:
            raise RecommendationError("Invalid recommendation title, ID, or filter.", status=400)

        def update(state):
            if state["pending"]:
                raise RecommendationError(
                    "Resolve the pending recommendation first.", "feedback_required"
                )
            state["mangas"].setdefault(name, empty_preferences())[
                "last_offered_at"
            ] = now.isoformat()
            state["pending"] = {
                "id": offer_id,
                "manga": name,
                "filter": filter_value,
                "offered_at": now.isoformat(),
            }

        return self.mutate(update)

    def resolve(self, offer_id: str, feedback: str, now: datetime) -> dict:
        if feedback not in FEEDBACK:
            raise RecommendationError("Choose a feedback option.", status=400)

        def update(state):
            previous = state["last_resolution"]
            if previous and previous["id"] == offer_id:
                if previous["feedback"] != feedback:
                    raise RecommendationError(
                        "This recommendation already received different feedback."
                    )
                return
            pending = state["pending"]
            if not pending or pending["id"] != offer_id:
                raise RecommendationError(
                    "This recommendation is no longer pending.", "recommendation_changed"
                )
            preferences = state["mangas"][pending["manga"]]
            if feedback == "enjoyed":
                preferences["enjoyed_count"] += 1
                preferences["last_enjoyed_at"] = now.isoformat()
            elif feedback in {"too_soon", "not_in_mood"}:
                preferences[f"last_{feedback}_at"] = now.isoformat()
            elif feedback in {"less_often", "not_for_me"}:
                preferences[feedback] = True
                if feedback == "not_for_me":
                    preferences["favorite"] = False
            state["pending"] = None
            state["last_resolution"] = {
                "id": offer_id,
                "feedback": feedback,
                "closed_at": now.isoformat(),
            }

        return self.mutate(update)

    def set_preference(self, name: str, flag: str, value: bool) -> dict:
        if flag not in FLAGS or type(value) is not bool:
            raise RecommendationError("Invalid preference.", status=400)

        def update(state):
            pending = state["pending"]
            if pending and (pending["manga"] != name or flag != "favorite"):
                raise RecommendationError(
                    "Resolve the pending recommendation first.", "feedback_required"
                )
            preferences = state["mangas"].setdefault(name, empty_preferences())
            preferences[flag] = value
            if flag == "favorite" and value:
                preferences["not_for_me"] = False
            if flag == "not_for_me" and value:
                preferences["favorite"] = False

        return self.mutate(update)


def _timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.tzinfo is not None and math.isfinite(parsed.timestamp())
    except (ValueError, OverflowError, OSError):
        return False


def _name(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and not any(char in value for char in ("/", "\\", "\0"))
    )


def _validate_state(state: object) -> None:
    def require(condition):
        if not condition:
            raise ValueError("Invalid recommendation metadata.")

    require(isinstance(state, dict) and set(state) == {"pending", "last_resolution", "mangas"})
    require(isinstance(state["mangas"], dict))
    for name, preferences in state["mangas"].items():
        require(_name(name) and isinstance(preferences, dict))
        require(set(preferences) == FLAGS | TIMES | {"enjoyed_count"})
        require(all(type(preferences[flag]) is bool for flag in FLAGS))
        require(not (preferences["favorite"] and preferences["not_for_me"]))
        require(type(preferences["enjoyed_count"]) is int and preferences["enjoyed_count"] >= 0)
        require(all(preferences[key] is None or _timestamp(preferences[key]) for key in TIMES))
    pending = state["pending"]
    if pending is not None:
        require(
            isinstance(pending, dict) and set(pending) == {"id", "manga", "filter", "offered_at"}
        )
        require(_name(pending["id"]) and _name(pending["manga"]))
        require(pending["manga"] in state["mangas"] and pending["filter"] in FILTERS)
        require(_timestamp(pending["offered_at"]))
    resolution = state["last_resolution"]
    if resolution is not None:
        require(isinstance(resolution, dict) and set(resolution) == {"id", "feedback", "closed_at"})
        require(_name(resolution["id"]) and resolution["feedback"] in FEEDBACK)
        require(_timestamp(resolution["closed_at"]))
