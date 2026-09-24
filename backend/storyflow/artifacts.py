"""Phase 3 artifact store: story text and audio files under a single root.

The DB stores only RELATIVE paths. Every write lands in a temp file in the
destination directory and is atomically promoted with os.replace (same filesystem
=> atomic on POSIX and Windows). resolve() rejects absolute paths, ".." traversal
and any target that would escape the store root.
"""

import os
import tempfile
import time
from pathlib import Path, PurePosixPath


_PROMOTE_RETRIES = 8
_PROMOTE_BACKOFF_SECONDS = 0.005


class PathTraversalError(ValueError):
    """A relative artifact path escapes the store root."""


class ArtifactStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._root_real = self.root.resolve()  # canonical root, computed once (the directory exists)

    def resolve(self, rel_path: str) -> Path:
        """Resolve a stored relative path to a path inside the root.

        Textual rules reject absolute paths, ``..`` and empty segments before any filesystem access.
        Containment is then enforced component by component: the canonical root is joined with each
        part and only a component that really is a symlink/junction is followed (and must land inside
        the root). We deliberately do NOT ``Path.resolve()`` the whole target: on Windows that call
        cannot open a file/directory that another writer is creating or atomically replacing at that
        instant and returns a different normalisation, which used to raise a spurious
        PathTraversalError for a perfectly valid path (two runtimes writing the same artifact).
        """
        native = Path(rel_path)
        if native.is_absolute():  # catches drive paths (C:\...) on Windows
            raise PathTraversalError(rel_path)
        rel = PurePosixPath(str(rel_path).replace("\\", "/"))
        if rel.is_absolute() or ".." in rel.parts or "" in rel.parts:
            raise PathTraversalError(rel_path)
        root = self._root_real
        current = root
        for part in rel.parts:
            current = current / part
            if current.is_symlink() or current.is_junction():
                real = current.resolve()
                if not real.is_relative_to(root):
                    raise PathTraversalError(rel_path)
                current = real
        if not current.is_relative_to(root):
            raise PathTraversalError(rel_path)
        return current

    def write(self, rel_path: str, data: bytes) -> str:
        """Write bytes to `rel_path`, atomically. Returns the relative path as stored."""
        dest = self.resolve(rel_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".tmp-", suffix=dest.suffix)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            self._promote(tmp, dest, data)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return rel_path

    @staticmethod
    def _promote(tmp: str, dest: Path, data: bytes) -> None:
        """Atomic promote (os.replace, same filesystem). On Windows a concurrent writer that is
        replacing/reading ``dest`` makes os.replace raise PermissionError. Two writers of the
        SAME bytes (idempotent re-runs from several processes) are harmless: if dest already
        holds identical content we are done. Otherwise retry a bounded number of times and
        re-raise -- a persistent conflict is never hidden."""
        for attempt in range(_PROMOTE_RETRIES):
            try:
                os.replace(tmp, dest)
                return
            except PermissionError:
                try:
                    if dest.is_file() and dest.read_bytes() == data:
                        os.unlink(tmp)
                        return
                except OSError:
                    pass  # dest still busy: fall through to retry
                if attempt == _PROMOTE_RETRIES - 1:
                    raise
                time.sleep(_PROMOTE_BACKOFF_SECONDS * (attempt + 1))

    def read(self, rel_path: str) -> bytes:
        return self.resolve(rel_path).read_bytes()

    def exists(self, rel_path: str) -> bool:
        return self.resolve(rel_path).is_file()

    def delete(self, rel_path: str) -> None:
        self.resolve(rel_path).unlink(missing_ok=True)