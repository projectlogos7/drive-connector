from nous_drive import db, notifications
from nous_drive.accounts import set_status, update_source_settings

from .conftest import apply, make_source


def _live(account, src):
    with db.scoped(account["id"]) as conn:
        set_status(conn, src["id"], "live")


def test_batched_notification_per_source(account):
    src = make_source(account["id"], name="Projects")
    _live(account, src)
    apply(account["id"], src, [dict(provider_id="f", name="Footage", parent_provider_id="ROOT", kind="folder")])
    assert notifications.flush() == 1          # first flush: "index ready", sets the baseline
    apply(account["id"], src, [dict(provider_id=f"c{i}", name=f"clip{i}.mov", parent_provider_id="f")
                               for i in range(200)])
    assert notifications.flush() == 0          # still inside the batching window
    assert notifications.flush(force=True) == 1
    with db.scoped(account["id"]) as conn:
        notes = conn.execute("SELECT * FROM drive_index.notifications ORDER BY id").fetchall()
    assert notes[0]["title"].startswith("Index ready")
    assert notes[1]["title"] == "200 files added in Projects"
    assert "/Footage — 200 added" in notes[1]["body"]
    assert notes[1]["payload"]["follow_up"]["tool"] == "whats_new"
    assert notifications.flush(force=True) == 0  # nothing new


def test_quiet_folders_and_types(account):
    src = make_source(account["id"], name="Projects")
    _live(account, src)
    with db.scoped(account["id"]) as conn:
        update_source_settings(conn, account["id"], src["id"],
                               {"notifications": {"types": ["added", "deleted"], "quiet_folders": ["/Cache"]}}, "t")
    notifications.flush()
    apply(account["id"], src, [dict(provider_id="q", name="Cache", parent_provider_id="ROOT", kind="folder"),
                               dict(provider_id="q1", name="x.tmp2", parent_provider_id="q"),
                               dict(provider_id="k", name="keep.pdf", parent_provider_id="ROOT")])
    notifications.flush(force=True)
    with db.scoped(account["id"]) as conn:
        last = conn.execute("SELECT * FROM drive_index.notifications ORDER BY id DESC LIMIT 1").fetchone()
    assert last["payload"]["counts"] == {"added": 1}


def test_alert_for_long_degraded(account):
    src = make_source(account["id"], name="Projects")
    with db.system() as conn:
        conn.execute("UPDATE drive_index.sources SET status = 'degraded', status_reason = 'x', "
                     "status_since = now() - interval '2 hours'")
    assert notifications.check_alerts() == 1
    assert notifications.check_alerts() == 0   # once per incident
