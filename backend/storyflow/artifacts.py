"""Phase 3 artifact store: story text and audio files under a single root.

The DB stores only RELATIVE paths. Every write lands in a temp file in the
destination directory and is atomically promoted with os.replace (same filesystem
=> atomic on POSIX and Windows). resolve() rejects absolute paths, ".." traversal
and any target that would escape the store root.
"""

import os
import tempfile
from pathlib import Path, PurePosixPath


class PathTraversalError(ValueError):
    """A relative artifact path escapes the store root."""


class ArtifactStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, rel_path: str) -> Path:
        """Resolve a stored relative path to a real path inside the root."""
        native = Path(rel_path)
        if native.is_absolute():  # catches drive paths (C:\...) on Windows
            raise PathTraversalError(rel_path)
        rel = PurePosixPath(str(rel_path).replace("\\", "/"))
        if rel.is_absolute() or ".." in rel.parts or "" in rel.parts:
            raise PathTraversalError(rel_path)
        target = (self.root / rel).resolve()
        if not target.is_relative_to(self.root.resolve()):
            raise PathTraversalError(rel_path)
        return target

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
            os.replace(tmp, dest)  # atomic promote on the same filesystem
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return rel_path

    def read(self, rel_path: str) -> bytes:
        return self.resolve(rel_path).read_bytes()

    def exists(self, rel_path: str) -> bool:
        return self.resolve(rel_path).is_file()

    def delete(self, rel_path: str) -> None:
        self.resolve(rel_path).unlink(missing_ok=True)