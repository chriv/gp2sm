"""Read Google Takeout archives in either export format: .tgz/.tar.gz (streamed) or .zip (Google's default).

Callers never open archives themselves: `iter_members` walks every file once, in archive order, and
`read_members` returns only the files asked for (stopping early once all are found).
"""

import os
import tarfile
import zipfile
from dataclasses import dataclass
from typing import IO, Callable

ARCHIVE_SUFFIXES = (".tgz", ".tar.gz", ".tar", ".zip")


def is_archive(filename):
    return filename.lower().endswith(ARCHIVE_SUFFIXES)


def list_archives(folder):
    """Archive file names (not paths) in a folder, sorted."""
    return sorted(f for f in os.listdir(folder) if is_archive(f))


@dataclass
class Member:
    name: str      # path inside the archive, '/'-separated
    size: int
    mtime: int
    open: Callable[[], IO[bytes]]  # valid only until the iteration moves on


def iter_members(path):
    """Yield a Member for every regular file. Read each one (if needed) before advancing."""
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                mtime = _zip_mtime(info)
                yield Member(info.filename, info.file_size, mtime, lambda info=info: z.open(info))
        return
    with tarfile.open(path, mode="r|*") as tar:
        for m in tar:
            if m.isfile():
                yield Member(m.name, m.size, int(m.mtime), lambda m=m: tar.extractfile(m))


def read_members(path, names):
    """Yield (name, bytes) for each wanted name found in the archive (archive order); stops once all are read."""
    remaining = set(names)
    if not remaining:
        return
    for m in iter_members(path):
        if m.name in remaining:
            with m.open() as f:
                yield m.name, f.read()
            remaining.discard(m.name)
            if not remaining:
                return


def _zip_mtime(info):
    import calendar
    return calendar.timegm(info.date_time + (0, 0, -1))
