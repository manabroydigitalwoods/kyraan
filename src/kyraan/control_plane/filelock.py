"""Advisory file locking for the JSON stores (G-06).

Every store here is a read-modify-write JSON file (reminders, memory
index, cost ledger). Two writers — an overlapping restart, a worker
thread beside the event loop — can interleave and drop each other's
writes; a reminder double-fired live exactly this way. One lock file per
store, held across the whole RMW section.
"""
import fcntl
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def locked(path: Path):
    """Exclusive advisory lock scoped to `path` (any process, any thread).
    Callers wrap their whole read-modify-write, not just the write."""
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def atomic_write_text(path: Path, text: str) -> None:
    """Write-then-rename: a concurrent reader sees the old file or the
    new one, never a half-written JSON (external review P2)."""
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)
