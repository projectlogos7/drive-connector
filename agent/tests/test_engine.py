import os
import time

import pytest

from nous_sync.engine import ROOT_ID, SourceEngine
from nous_sync.ignore import Ignorer
from nous_sync.state import State


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "Footage"
    root.mkdir()
    st = State(tmp_path / "state.sqlite3")
    return root, st, SourceEngine("src1", str(root), st)


def drain(st):
    out = st.peek("src1", 10_000)
    st.ack([o[0] for o in out])
    return [o[2] for o in out]


def write(p, data=b"x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def test_initial_and_incremental_catch_up(env):
    root, st, eng = env
    write(root / "Day 1" / "A001.mov", b"a" * 10)
    write(root / "Day 1" / "A002.mov", b"b" * 10)
    write(root / ".DS_Store")
    write(root / "~$script.docx")
    stats = eng.catch_up()
    ev = drain(st)
    assert stats["added"] == 3 and len(ev) == 3          # folder + 2 files, ignored files skipped
    folder = next(e for e in ev if e["kind"] == "folder")
    assert folder["parent_provider_id"] == ROOT_ID
    assert all(e["parent_provider_id"] == folder["provider_id"] for e in ev if e["kind"] == "file")
    assert eng.catch_up() == {"scanned": 3, "added": 0, "modified": 0, "moved": 0, "deleted": 0}
    assert drain(st) == []


def test_offline_changes_detected_with_ids_kept(env):
    root, st, eng = env
    write(root / "Day 1" / "A001.mov")
    write(root / "Day 1" / "keep.txt")
    write(root / "old.txt")
    eng.catch_up()
    ids = {e["name"]: e["provider_id"] for e in drain(st)}
    # while the agent was "asleep": rename a folder, modify, delete, case-only rename
    os.rename(root / "Day 1", root / "Day One")
    time.sleep(0.01)
    (root / "Day One" / "keep.txt").write_bytes(b"changed content")
    (root / "old.txt").unlink()
    stats = eng.catch_up()
    ev = drain(st)
    assert stats["moved"] == 1 and stats["deleted"] == 1 and stats["modified"] == 1
    moved = next(e for e in ev if e.get("name") == "Day One")
    assert moved["provider_id"] == ids["Day 1"]
    mod = next(e for e in ev if e.get("name") == "keep.txt")
    assert mod["provider_id"] == ids["keep.txt"]
    deleted = next(e for e in ev if e["op"] == "delete")
    assert deleted["provider_id"] == ids["old.txt"]
    assert st.get("src1", "Day One/A001.mov").provider_id == ids["A001.mov"]


def test_deleting_folder_sends_one_delete(env):
    root, st, eng = env
    for i in range(5):
        write(root / "Proj" / "sub" / f"{i}.wav")
    eng.catch_up()
    drain(st)
    import shutil
    shutil.rmtree(root / "Proj")
    eng.catch_up()
    ev = drain(st)
    assert len(ev) == 1 and ev[0]["op"] == "delete"
    assert st.all("src1") == {}


def test_live_process_path_and_move(env):
    root, st, eng = env
    write(root / "a.txt")
    assert eng.process_path("a.txt") == "added"
    pid = drain(st)[0]["provider_id"]
    os.rename(root / "a.txt", root / "b.txt")
    assert eng.process_move("a.txt", "b.txt") == "moved"
    ev = drain(st)
    assert ev[0]["provider_id"] == pid and ev[0]["name"] == "b.txt"
    # A rename seen only as delete+create still keeps the id via the inode.
    os.rename(root / "b.txt", root / "B.txt")
    assert eng.process_path("B.txt") == "moved"
    assert drain(st)[0]["provider_id"] == pid
    (root / "B.txt").unlink()
    assert eng.process_path("B.txt") == "deleted"


def test_folder_copied_in_is_indexed_with_contents(env):
    root, st, eng = env
    write(root / "incoming" / "x" / "1.wav")
    write(root / "incoming" / "2.wav")
    assert eng.process_path("incoming") == "added"
    names = sorted(e["name"] for e in drain(st))
    assert names == ["1.wav", "2.wav", "incoming", "x"]


def test_symlinks_recorded_not_followed(env, tmp_path):
    root, st, eng = env
    outside = tmp_path / "elsewhere"
    write(outside / "big.mov")
    os.symlink(outside, root / "link")
    eng.catch_up()
    ev = drain(st)
    assert len(ev) == 1 and ev[0]["kind"] == "shortcut" and ev[0]["shortcut_target"] == str(outside)


def test_full_scan_reports_everything(env):
    root, st, eng = env
    write(root / "a" / "b.txt")
    eng.catch_up()
    drain(st)
    eng.full_scan("scan-1")
    ev = st.peek("src1", 100)
    assert len(ev) == 2 and all(e[1] == "scan-1" for e in ev)


def test_ignore_rules():
    ig = Ignorer(["*.prproj-autosave", "Render Cache/*"])
    assert ig.ignored(".git/config") and ig.ignored("a/Thumbs.db") and ig.ignored("x/~$doc.docx")
    assert ig.ignored("p/x.prproj-autosave") and ig.ignored("Render Cache/a.mov")
    assert not ig.ignored("Footage/A001.mov")


def test_reused_inode_is_not_mistaken_for_a_rename(env, monkeypatch):
    root, st, eng = env
    write(root / "notes.txt", b"call list")
    eng.catch_up()
    ids = {e["name"]: e["provider_id"] for e in drain(st)}
    rec = st.get("src1", "notes.txt")
    (root / "notes.txt").unlink()
    write(root / "A002.mov", b"b")
    # Simulate the file system handing the freed inode to the new file.
    import nous_sync.engine as E
    real = E.scan

    def fake_scan(*a, **k):
        out = real(*a, **k)
        if "A002.mov" in out:
            out["A002.mov"].fs_key = rec.fs_key
        return out
    monkeypatch.setattr(E, "scan", fake_scan)
    stats = eng.catch_up()
    ev = drain(st)
    assert stats["moved"] == 0 and stats["added"] == 1 and stats["deleted"] == 1
    added = next(e for e in ev if e.get("name") == "A002.mov")
    assert added["provider_id"] != ids["notes.txt"]
