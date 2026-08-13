"""Portable, atomic persistence for per-volume review sessions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
from typing import Any

from .models import VolumeRef


STATE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    current_index: int
    selected_paths: frozenset[str]
    warnings: tuple[str, ...] = ()


class SessionStore:
    """Save review state under ``<working>/.pocket-manga-editor``."""

    def __init__(self, working_directory: str | Path) -> None:
        self.working_directory = Path(working_directory)
        self.base_directory = self.working_directory / ".pocket-manga-editor" / "selections"

    def path_for(self, volume: VolumeRef) -> Path:
        return self.base_directory / volume.manga_name / f"Vol.{volume.number:02d}.json"

    def load(self, volume: VolumeRef) -> SessionSnapshot:
        path = self.path_for(volume)
        if not path.exists():
            return SessionSnapshot(0, frozenset())

        try:
            self._validate_location(path.parent)
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return SessionSnapshot(0, frozenset(), (f"Could not load saved session: {exc}",))

        if not isinstance(payload, dict) or payload.get("schema_version") != STATE_SCHEMA_VERSION:
            return SessionSnapshot(
                0,
                frozenset(),
                ("Saved session uses an unsupported format and was ignored.",),
            )

        if payload.get("manga") != volume.manga_name or payload.get("volume") != volume.number:
            return SessionSnapshot(
                0,
                frozenset(),
                ("Saved session belongs to a different manga or volume and was ignored.",),
            )

        valid_paths = {page.relative_path for page in volume.pages}
        warnings: list[str] = []
        selected: set[str] = set()
        raw_selected = payload.get("selected_pages", [])
        if not isinstance(raw_selected, list):
            raw_selected = []
            warnings.append("The saved selection list was invalid and was ignored.")

        stale_count = 0
        unsafe_count = 0
        for value in raw_selected:
            if not isinstance(value, str) or not _is_safe_relative_path(value):
                unsafe_count += 1
            elif value in valid_paths:
                selected.add(value)
            else:
                stale_count += 1

        if unsafe_count:
            warnings.append(f"Ignored {unsafe_count} unsafe selection path(s).")
        if stale_count:
            warnings.append(
                f"Ignored {stale_count} selection(s) whose source pages no longer exist."
            )

        index_by_path = {page.relative_path: index for index, page in enumerate(volume.pages)}
        current_page = payload.get("current_page")
        if isinstance(current_page, str) and current_page in index_by_path:
            current_index = index_by_path[current_page]
        else:
            saved_index = payload.get("current_index", 0)
            if not isinstance(saved_index, int) or isinstance(saved_index, bool):
                saved_index = 0
            current_index = min(max(saved_index, 0), max(len(volume.pages) - 1, 0))

        return SessionSnapshot(current_index, frozenset(selected), tuple(warnings))

    def save(
        self,
        volume: VolumeRef,
        current_index: int,
        selected_paths: set[str] | frozenset[str],
    ) -> None:
        if not volume.pages:
            return

        current_index = min(max(current_index, 0), len(volume.pages) - 1)
        valid_selected = [
            page.relative_path for page in volume.pages if page.relative_path in selected_paths
        ]
        payload = {
            "schema_version": STATE_SCHEMA_VERSION,
            "manga": volume.manga_name,
            "volume": volume.number,
            "current_page": volume.pages[current_index].relative_path,
            "current_index": current_index,
            "selected_pages": valid_selected,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._validate_location(self.path_for(volume).parent)
        atomic_write_json(self.path_for(volume), payload)

    def _validate_location(self, destination: Path) -> None:
        root = self.working_directory.expanduser().resolve()
        for candidate in (root / ".pocket-manga-editor", self.base_directory, destination):
            if candidate.exists() and not candidate.resolve().is_relative_to(root):
                raise OSError("The app metadata folder points outside the working directory.")


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON through a same-directory temporary file and atomic replace."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _is_safe_relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts
