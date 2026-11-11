from datetime import datetime, timedelta, timezone

from nous_drive import db
from nous_drive.indexer import Indexer
from nous_drive.models import NodeEvent

from .conftest import apply, events, make_source, nodes

T0 = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)


def f(pid, name, parent="ROOT", kind="file", **kw):
    return dict(provider_id=pid, name=name, parent_provider_id=parent, kind=kind, **kw)


def test_add_tree_and_paths_with_children_before_parents(account):
    src = make_source(account["id"])
    st = apply(account["id"], src, [
        f("doc", "Unmaking_Citizens_v4.docx", "ep2", size=100, modified_at_src=T0),
        f("ep2", "Episode 2", "mad", kind="folder"),
        f("mad", "Madarasa", "ROOT", kind="folder"),
    ])
    assert st.by_type == {"added": 3}
    n = nodes(account["id"])
    assert n["doc"]["path"] == "/Madarasa/Episode 2/Unmaking_Citizens_v4.docx"
    assert n["doc"]["depth"] == 3
    assert n["doc"]["ext"] == "docx" and n["doc"]["category"] == "document"
    assert n["ep2"]["parent_id"] == n["mad"]["id"]


def test_idempotent_replay(account):
    src = make_source(account["id"])
    evs = [f("a", "A", kind="folder"), f("x", "x.mov", "a", size=5, modified_at_src=T0)]
    apply(account["id"], src, evs)
    st = apply(account["id"], src, evs)
    assert st.changes == 0
    assert len(events(account["id"])) == 2


def test_rename_and_move_cascade_paths(account):
    src = make_source(account["id"])
    apply(account["id"], src, [f("a", "A", kind="folder"), f("b", "B", "a", kind="folder"),
                               f("x", "x.txt", "b", size=1), f("z", "Z", kind="folder")])
    st = apply(account["id"], src, [f("a", "Alpha", kind="folder")])
    assert st.by_type == {"renamed": 1}
    assert nodes(account["id"])["x"]["path"] == "/Alpha/B/x.txt"
    st = apply(account["id"], src, [f("b", "B", "z", kind="folder")])
    assert st.by_type == {"moved": 1}
    n = nodes(account["id"])
    assert n["x"]["path"] == "/Z/B/x.txt" and n["x"]["depth"] == 3
    ev = events(account["id"])[-1]
    assert ev["type"] == "moved" and ev["before"]["path"] == "/Alpha/B" and ev["after"]["path"] == "/Z/B"


def test_case_only_rename(account):
    src = make_source(account["id"])
    apply(account["id"], src, [f("x", "report.pdf")])
    st = apply(account["id"], src, [f("x", "Report.pdf")])
    assert st.by_type == {"renamed": 1}


def test_modify_detects_size_checksum_and_time(account):
    src = make_source(account["id"])
    apply(account["id"], src, [f("x", "a.mov", size=1, checksum="aa", modified_at_src=T0)])
    assert apply(account["id"], src, [f("x", "a.mov", size=1, checksum="aa", modified_at_src=T0)]).changes == 0
    assert apply(account["id"], src, [f("x", "a.mov", size=2, checksum="bb",
                                        modified_at_src=T0 + timedelta(minutes=1))]).by_type == {"modified": 1}


def test_delete_is_soft_and_cascades(account):
    src = make_source(account["id"])
    apply(account["id"], src, [f("a", "A", kind="folder"), f("b", "B", "a", kind="folder"), f("x", "x", "b")])
    st = apply(account["id"], src, [NodeEvent(op="delete", provider_id="a")])
    assert st.by_type == {"deleted": 1}
    n = nodes(account["id"])
    assert {n[k]["status"] for k in ("a", "b", "x")} == {"deleted"}
    assert n["x"]["deleted_at"] is not None
    assert events(account["id"])[-1]["after"]["descendants"] == 2
    # replay is a no-op
    assert apply(account["id"], src, [NodeEvent(op="delete", provider_id="a")]).changes == 0
    # it can come back
    assert apply(account["id"], src, [f("x", "x", "ROOT")]).by_type == {"added": 1}


def test_trash_and_restore_folder(account):
    src = make_source(account["id"])
    apply(account["id"], src, [f("a", "A", kind="folder"), f("x", "x", "a")])
    assert apply(account["id"], src, [f("a", "A", kind="folder", trashed=True)]).by_type == {"trashed": 1}
    assert nodes(account["id"])["x"]["status"] == "trashed"
    assert apply(account["id"], src, [f("a", "A", kind="folder")]).by_type == {"restored": 1}
    assert nodes(account["id"])["x"]["status"] == "active"


def test_remove_from_source_reason(account):
    src = make_source(account["id"])
    apply(account["id"], src, [f("x", "x")])
    apply(account["id"], src, [NodeEvent(op="remove", provider_id="x", reason="moved out of the watched folder")])
    n = nodes(account["id"])["x"]
    assert n["status"] == "removed" and n["status_reason"] == "moved out of the watched folder"


def test_finish_scan_sweeps_unseen(account):
    src = make_source(account["id"])
    apply(account["id"], src, [f("a", "A", kind="folder"), f("x", "x", "a"), f("y", "y")])
    with db.scoped(account["id"]) as conn:
        ix = Indexer(conn, account["id"], src)
        ix.apply([NodeEvent(**f("a", "A", kind="folder"), scan_id="s1"), NodeEvent(**f("y", "y"), scan_id="s1")])
        assert ix.finish_scan("s1") == 1
    n = nodes(account["id"])
    assert n["x"]["status"] == "deleted" and n["y"]["status"] == "active"


def test_unavailable_and_back(account):
    src = make_source(account["id"], type_="local", root_kind="local_folder", root_ref="/Volumes/Footage",
                      name="Footage", device_id=None)
    apply(account["id"], src, [f("x", "x.mov", "__root__")])
    apply(account["id"], src, [NodeEvent(op="unavailable", reason="drive unplugged")])
    assert nodes(account["id"])["x"]["status"] == "unavailable"
    apply(account["id"], src, [NodeEvent(op="available")])
    assert nodes(account["id"])["x"]["status"] == "active"
    assert [e["type"] for e in events(account["id"])][-2:] == ["unavailable", "available"]


def test_multiple_parents_and_shortcuts(account):
    src = make_source(account["id"])
    apply(account["id"], src, [f("a", "A", kind="folder"), f("b", "B", kind="folder"),
                               f("x", "x.pdf", "a", alt_parent_provider_ids=["b"]),
                               f("s", "x shortcut", "b", kind="shortcut", shortcut_target="x",
                                 mime="application/vnd.google-apps.shortcut")])
    n = nodes(account["id"])
    assert n["x"]["alt_parent_provider_ids"] == ["b"]
    assert n["s"]["kind"] == "shortcut" and n["s"]["shortcut_target"] == "x"


def test_unicode_and_long_names(account):
    src = make_source(account["id"])
    long = "क" * 200 + ".mov"
    apply(account["id"], src, [f("u", "بنگال تقسیم", kind="folder"), f("d", "देवनागरी फ़ाइल.docx", "u"),
                               f("l", long, "u")])
    n = nodes(account["id"])
    assert n["d"]["path"] == "/بنگال تقسیم/देवनागरी फ़ाइल.docx"
    assert n["l"]["path"].endswith(long)
