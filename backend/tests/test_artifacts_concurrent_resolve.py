"""Regression: ArtifactStore.resolve/exists must never raise a spurious PathTraversalError for a valid
relative path while another writer is creating or atomically replacing that very file/directory (seen
on Windows in the two-runtime scenario: Path.resolve() cannot open a path mid-create/mid-os.replace and
returns a different normalisation, which broke the containment check)."""

import threading

from storyflow.artifacts import ArtifactStore


def _hammer(store, paths, *, writers, readers_n, rounds):
    stop = threading.Event()
    failures: list[BaseException] = []

    def writer(offset):
        n = 0
        while not stop.is_set():
            n += 1
            for rel in paths:
                store.write(rel, f"w{offset}-{n}".encode() * 30)

    def reader():
        try:
            for _ in range(rounds):
                for rel in paths:
                    store.exists(rel)
                    assert store.resolve(rel).name == "source.txt"
        except BaseException as exc:  # collected and re-raised below
            failures.append(exc)

    ws = [threading.Thread(target=writer, args=(i,)) for i in range(writers)]
    rs = [threading.Thread(target=reader) for _ in range(readers_n)]
    for t in ws + rs:
        t.start()
    for t in rs:
        t.join(timeout=180)
    stop.set()
    for t in ws:
        t.join(timeout=60)
    assert not failures, repr(failures[0])
    assert not any(t.is_alive() for t in rs + ws)


def test_resolve_and_exists_are_stable_while_files_are_replaced(tmp_path):
    store = ArtifactStore(tmp_path / "store")
    rel = "projects/p1/source/0001/source.txt"
    store.write(rel, b"seed")
    _hammer(store, [rel], writers=2, readers_n=4, rounds=1500)


def test_resolve_and_exists_are_stable_while_fresh_paths_are_created(tmp_path):
    store = ArtifactStore(tmp_path / "store")
    paths = [f"projects/p{i}/source/0001/source.txt" for i in range(40)]
    _hammer(store, paths, writers=3, readers_n=4, rounds=40)
