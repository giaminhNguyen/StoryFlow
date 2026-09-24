"""Artifact store tests (Phase 3): atomic write/promote, relative paths, and
path-traversal protection."""

import pytest

from storyflow.artifacts import ArtifactStore, PathTraversalError


def test_write_read_roundtrip(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    rel = store.write("proj/ver/chapter.md", b"chapter one")
    assert rel == "proj/ver/chapter.md"
    assert store.exists(rel)
    assert store.read(rel) == b"chapter one"


def test_write_creates_nested_dirs(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    store.write("a/b/c/0.wav", b"wav")
    assert (tmp_path / "artifacts" / "a" / "b" / "c" / "0.wav").is_file()


def test_overwrite_promotes_atomically(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    store.write("f.txt", b"old")
    store.write("f.txt", b"new")
    assert store.read("f.txt") == b"new"
    leftovers = [p.name for p in (tmp_path / "artifacts").iterdir() if p.name.startswith(".tmp-")]
    assert leftovers == [], "temp files must be promoted (removed) after atomic replace"


def test_delete(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    store.write("f.txt", b"x")
    store.delete("f.txt")
    assert not store.exists("f.txt")
    store.delete("f.txt")  # idempotent


def test_backslashes_treated_as_separators(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    rel = store.write("proj\\aud\\0.wav", b"wav")
    assert rel == "proj\\aud\\0.wav"
    assert store.read("proj/aud/0.wav") == b"wav"
    assert store.exists("proj\\aud\\0.wav")


@pytest.mark.parametrize("bad", [
    "..\\escape.txt",
    "../escape.txt",
    "a/../../evil.txt",
    "/etc/passwd",
    "\\\\server\\share\\x",
])
def test_path_traversal_rejected(tmp_path, bad):
    store = ArtifactStore(tmp_path / "artifacts")
    with pytest.raises(PathTraversalError):
        store.resolve(bad)
    with pytest.raises(PathTraversalError):
        store.write(bad, b"x")


def test_absolute_path_rejected(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret")
    store = ArtifactStore(tmp_path / "artifacts")
    with pytest.raises(PathTraversalError):
        store.read(str(outside))
    with pytest.raises(PathTraversalError):
        store.write(str(tmp_path) + "\\evil.txt", b"x")


def test_symlink_out_of_root_rejected(tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"secret")
    target = tmp_path / "artifacts" / "link.txt"
    try:
        target.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this system")
    store = ArtifactStore(tmp_path / "artifacts")
    with pytest.raises(PathTraversalError):
        store.resolve("link.txt")
    with pytest.raises(PathTraversalError):
        store.read("link.txt")