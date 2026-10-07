"""Service-neutral records, interfaces, and capability flags (ROADMAP A1).

The tools' core logic talks to photo services only through these. A service is plugged in by
implementing PhotoDestination (where items go) and/or PhotoSource (where items come from), and by
declaring its Capabilities, so the core can adapt to its behavior instead of hard-coding one service.

"Album" is the neutral word for a collection of items (album, set, gallery, ...).
"""

from dataclasses import dataclass, field
from typing import Iterator, Optional, Protocol, TypedDict, runtime_checkable

# ------------------------------------------------------------------ records

class ItemRecord(TypedDict, total=False):
    """An item as a destination reports it."""
    item_id: str              # stable id of the item at the destination
    serial: int               # destination's version/serial (0 when not applicable)
    item_ref: str             # opaque reference to the item *within a specific album* (what moves/removals act on)
    name: str                 # file name as stored
    format: Optional[str]
    is_video: bool
    md5: Optional[str]        # hash of the stored bytes (may differ from the original; see Capabilities)
    size: Optional[int]
    width: Optional[int]
    height: Optional[int]
    duration_s: Optional[float]
    uploaded: Optional[str]
    capture_time: Optional[str]
    make: Optional[str]              # camera make/model (with_metadata listings)
    model: Optional[str]
    raw: dict                 # service-specific fields, for logging/reporting only
    raw_metadata: Optional[dict]


class AlbumRecord(TypedDict, total=False):
    album_id: str
    ref: str
    name: str
    path: Optional[str]
    folder: Optional[str]            # display-name folder path, e.g. "Family/2023" ("" = top level)
    item_count: Optional[int]
    raw: dict


@dataclass(frozen=True)
class SourceItem:
    """An item as a source provides it (e.g. one Takeout photo with its metadata and Live Photo clip)."""
    source_id: str
    name: str
    kind: str                         # "photo" | "video"
    taken_ts: Optional[int]           # capture time, unix seconds (UTC)
    size: Optional[int] = None
    md5: Optional[str] = None
    motion_ref: Optional[str] = None  # the paired Live Photo clip, if any (source-specific reference)
    extras: dict = field(default_factory=dict)
    width: Optional[int] = None
    height: Optional[int] = None
    duration_s: Optional[float] = None
    own_time: Optional[str] = None    # capture time the file itself records (ISO 8601, offset when known)


# ------------------------------------------------------------- capabilities

@dataclass(frozen=True)
class Capabilities:
    """Behavior a destination declares, so the core adapts instead of hard-coding one service."""
    name: str
    atomic_batch_moves: bool = False            # a rejected batch moves nothing (safe to split and retry)
    writes_may_apply_despite_error: bool = True # a timeout/5xx write may still have happened: check, don't retry blindly
    moves_ignored_while_processing: bool = False  # a move can "succeed" yet not happen while an item processes
    can_rename_items: bool = True               # False: renaming means re-upload + remove
    allows_duplicate_uploads: bool = True       # identical bytes are accepted again (dedup is the client's job)
    stores_original_bytes: frozenset = frozenset()  # lowercase extensions whose stored md5 == original md5
    converts_on_upload: tuple = ()              # ((".heic", ".jpg"), ...) server-side conversions
    rejected_extensions: frozenset = frozenset()
    min_video_pixels: Optional[int] = None      # videos smaller than this (w*h) are rejected; None = unknown/none
    max_items_per_album: Optional[int] = None
    can_collect: bool = False                   # an item can appear in several albums (collect = add, not move)
    removing_original_removes_collected: bool = False  # deleting from its own album deletes every collected copy


# --------------------------------------------------------------- interfaces

@runtime_checkable
class PhotoDestination(Protocol):
    """Where items go and get organized. All methods return/accept neutral records and opaque refs."""
    capabilities: Capabilities

    def root_folder(self) -> str: ...
    def ensure_folder_path(self, root: str, path: str) -> str: ...
    def ensure_album(self, parent: str, name: str) -> tuple: ...   # (album_id, album_ref, node_ref, created)
    def delete_album(self, album_id: str) -> None: ...
    def list_albums(self) -> Iterator[AlbumRecord]: ...
    def list_folder_albums(self, folder_path: str) -> Iterator[AlbumRecord]: ...  # by folder names; "" = all
    def album_ref(self, album_id: str) -> str: ...
    def album_item_count(self, album_id: str) -> int: ...
    def list_album_items(self, album_id: str, with_metadata: bool = False,
                         ids_only: bool = False) -> Iterator[ItemRecord]: ...
    def item_ref(self, album_id: str, item_id: str, serial: int = 0) -> str: ...
    def album_contains(self, album_id: str, item_id: str, serial: int = 0) -> bool: ...
    def item_album_ids(self, item_id: str, serial: int = 0) -> list: ...
    def move_items(self, dest_album_id: str, item_refs: list) -> None: ...
    def collect_items(self, dest_album_id: str, item_refs: list) -> None: ...   # add without removing
    def remove_item(self, item_ref: str) -> None: ...
    def upload_file(self, album_id: str, path: str, filename: str, content_type: str) -> dict: ...
    def preview_bytes(self, item_id: str, serial: int = 0) -> bytes: ...
    def download_item(self, item_id: str, serial: int = 0, is_video: bool = False) -> bytes: ...
    def video_info(self, item_id: str, serial: int = 0) -> dict: ...
    def set_sort_by_filename(self, album_id: str) -> object: ...


@runtime_checkable
class PhotoSource(Protocol):
    """Where items come from (an export, a library, an API)."""
    name: str

    def iter_items(self) -> Iterator[SourceItem]: ...
    def open_bytes(self, item: SourceItem) -> bytes: ...
