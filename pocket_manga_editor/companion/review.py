"""Serialized review-state reads and immediate SessionStore persistence."""

from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Callable

from ..storage import SessionStore
from .snapshot import LibrarySnapshot, VolumeSnapshotEntry


class ReviewError(RuntimeError):
    code = "review_error"


class ReviewSaveError(ReviewError):
    code = "save_failure"


@dataclass(frozen=True, slots=True)
class ReviewContext:
    manga_id: str
    manga_name: str
    volume_id: str
    volume_name: str
    page_id: str
    page_label: str
    selected_count: int


@dataclass(frozen=True, slots=True)
class ReviewMutation:
    volume_id: str
    page_id: str
    current_page_id: str
    selected: bool | None
    selected_count: int
    revision: int


@dataclass(slots=True)
class _VolumeState:
    current_index: int
    selected_paths: set[str]
    revision: int = 0


class ReviewService:
    def __init__(
        self,
        snapshot: LibrarySnapshot,
        *,
        session_store: SessionStore | None = None,
        context_callback: Callable[[ReviewContext], None] | None = None,
    ) -> None:
        self.snapshot = snapshot
        self._store = session_store or SessionStore(snapshot.working_directory)
        self._context_callback = context_callback
        self._lock = threading.RLock()
        self._states: dict[str, _VolumeState] = {}
        self._last_context: ReviewContext | None = None

    def library_payload(self) -> dict[str, object]:
        with self._lock:
            mangas: list[dict[str, object]] = []
            for manga in self.snapshot.mangas:
                selected_count = sum(
                    len(self._state(volume_id).selected_paths)
                    for volume_id in manga.volume_ids
                )
                mangas.append(
                    {
                        "id": manga.id,
                        "name": manga.ref.name,
                        "volume_count": len(manga.volume_ids),
                        "selected_count": selected_count,
                    }
                )
            return {
                "snapshot_id": self.snapshot.snapshot_id,
                "mangas": mangas,
                "context": self._context_payload_locked(),
                "issue_count": self.snapshot.issue_count,
            }

    def manga_payload(self, manga_id: str) -> dict[str, object]:
        with self._lock:
            manga = self.snapshot.manga(manga_id)
            volumes: list[dict[str, object]] = []
            for volume_id in manga.volume_ids:
                volume = self.snapshot.volume(volume_id)
                state = self._state(volume_id)
                volumes.append(
                    {
                        "id": volume.id,
                        "label": volume.ref.label,
                        "display_name": volume.ref.display_name,
                        "page_count": len(volume.page_ids),
                        "selected_count": len(state.selected_paths),
                        "current_page_id": volume.page_ids[state.current_index],
                    }
                )
            return {
                "snapshot_id": self.snapshot.snapshot_id,
                "manga": {
                    "id": manga.id,
                    "name": manga.ref.name,
                    "volumes": volumes,
                },
            }

    def volume_payload(self, volume_id: str) -> dict[str, object]:
        with self._lock:
            volume = self.snapshot.volume(volume_id)
            state = self._state(volume_id)
            self._publish_context(volume, state)
            pages: list[dict[str, object]] = []
            for page_id in volume.page_ids:
                page = self.snapshot.page(page_id)
                pages.append(
                    {
                        "id": page.id,
                        "page_label": page.ref.page_label,
                        "chapter_label": page.ref.chapter_label,
                        "chapter_title": page.ref.chapter_title,
                        "selected": page.ref.relative_path in state.selected_paths,
                        "image_url": f"/api/page/{page.id}/image",
                    }
                )
            return {
                "snapshot_id": self.snapshot.snapshot_id,
                "volume": {
                    "id": volume.id,
                    "manga_id": volume.manga_id,
                    "label": volume.ref.label,
                    "display_name": volume.ref.display_name,
                    "current_page_id": volume.page_ids[state.current_index],
                    "current_index": state.current_index,
                    "selected_count": len(state.selected_paths),
                    "revision": state.revision,
                    "pages": pages,
                },
            }

    def set_position(self, volume_id: str, page_id: str) -> ReviewMutation:
        with self._lock:
            volume = self.snapshot.volume(volume_id)
            self.snapshot.page_in_volume(volume_id, page_id)
            self.snapshot.validate_live_page(page_id)
            state = self._state(volume_id)
            index = volume.page_ids.index(page_id)
            if index != state.current_index:
                self._save(volume, index, state.selected_paths)
                state.current_index = index
                state.revision += 1
            mutation = self._mutation(volume, state, page_id, None)
            self._publish_context(volume, state)
            return mutation

    def set_selection(
        self, volume_id: str, page_id: str, selected: bool
    ) -> ReviewMutation:
        if not isinstance(selected, bool):
            raise ReviewError("selected must be a boolean.")
        with self._lock:
            volume = self.snapshot.volume(volume_id)
            page = self.snapshot.page_in_volume(volume_id, page_id)
            self.snapshot.validate_live_page(page_id)
            state = self._state(volume_id)
            updated = set(state.selected_paths)
            if selected:
                updated.add(page.ref.relative_path)
            else:
                updated.discard(page.ref.relative_path)
            if updated != state.selected_paths:
                self._save(volume, state.current_index, updated)
                state.selected_paths = updated
                state.revision += 1
            mutation = self._mutation(volume, state, page_id, selected)
            self._publish_context(volume, state)
            return mutation

    def context(self) -> ReviewContext | None:
        with self._lock:
            return self._last_context

    def flush(self) -> None:
        """Wait for any in-flight immediate save to finish."""

        with self._lock:
            return

    def _state(self, volume_id: str) -> _VolumeState:
        state = self._states.get(volume_id)
        if state is not None:
            return state
        volume = self.snapshot.volume(volume_id)
        loaded = self._store.load(volume.ref)
        index = min(max(loaded.current_index, 0), len(volume.page_ids) - 1)
        state = _VolumeState(index, set(loaded.selected_paths))
        self._states[volume_id] = state
        return state

    def _save(
        self, volume: VolumeSnapshotEntry, current_index: int, selected_paths: set[str]
    ) -> None:
        try:
            self._store.save(volume.ref, current_index, selected_paths)
        except OSError as exc:
            raise ReviewSaveError(f"Could not save review state: {exc}") from exc

    def _mutation(
        self,
        volume: VolumeSnapshotEntry,
        state: _VolumeState,
        page_id: str,
        selected: bool | None,
    ) -> ReviewMutation:
        return ReviewMutation(
            volume.id,
            page_id,
            volume.page_ids[state.current_index],
            selected,
            len(state.selected_paths),
            state.revision,
        )

    def _publish_context(
        self, volume: VolumeSnapshotEntry, state: _VolumeState
    ) -> None:
        page_id = volume.page_ids[state.current_index]
        page = self.snapshot.page(page_id)
        manga = self.snapshot.manga(volume.manga_id)
        context = ReviewContext(
            manga.id,
            manga.ref.name,
            volume.id,
            volume.ref.display_name,
            page.id,
            page.ref.page_label,
            len(state.selected_paths),
        )
        self._last_context = context
        if self._context_callback is not None:
            self._context_callback(context)

    def _context_payload_locked(self) -> dict[str, object] | None:
        context = self._last_context
        if context is None:
            return None
        return {
            "manga_id": context.manga_id,
            "volume_id": context.volume_id,
            "page_id": context.page_id,
            "selected_count": context.selected_count,
        }
