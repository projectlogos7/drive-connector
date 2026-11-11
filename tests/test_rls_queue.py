import pytest
from psycopg import errors

from nous_drive import db, queue
from nous_drive.accounts import AuthError, issue_key, resolve_key

from .conftest import apply, make_source, nodes


def test_rls_isolates_accounts(account, other_account):
    src = make_source(account["id"])
    apply(account["id"], src, [dict(provider_id="x", name="secret.pdf", parent_provider_id="ROOT")])
    assert nodes(other_account["id"]) == {}
    with db.scoped(other_account["id"]) as conn:
        assert conn.execute("SELECT count(*) AS n FROM drive_index.sources").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM drive_index.change_events").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM drive_index.accounts").fetchone()["n"] == 1
    # Writing into another account's space is refused by the policy.
    with pytest.raises(errors.InsufficientPrivilege):
        with db.scoped(other_account["id"]) as conn:
            conn.execute("INSERT INTO drive_index.sources (account_id, type, root_kind, root_ref, name) "
                         "VALUES (%s,'gdrive','folder','R','x')", (account["id"],))


def test_keys_resolve_and_revoke(account):
    ident = resolve_key(account["admin_key"])
    assert str(ident["account_id"]) == account["id"] and ident["kind"] == "admin"
    with db.scoped(account["id"]) as conn:
        k = issue_key(conn, account["id"], "session", "claude")
    assert resolve_key(k, ("session",))["kind"] == "session"
    with pytest.raises(AuthError):
        resolve_key(k, ("admin",))
    with db.scoped(account["id"]) as conn:
        conn.execute("UPDATE drive_index.api_keys SET revoked_at = now() WHERE kind = 'session'")
    with pytest.raises(AuthError):
        resolve_key(k)
    with pytest.raises(AuthError):
        resolve_key("nd_admin_wrong")


def test_queue_dedupe_claim_backoff_dead(account):
    src = make_source(account["id"])
    with db.scoped(account["id"]) as conn:
        a = queue.enqueue(conn, account["id"], "drive_pull_changes", src["id"], dedupe_key="pull:1")
        b = queue.enqueue(conn, account["id"], "drive_pull_changes", src["id"], dedupe_key="pull:1")
    assert a and b is None
    job = queue.claim(1)[0]
    assert job["id"] == a and job["attempts"] == 1
    # Same source is serialised: nothing else claimable while it runs.
    with db.scoped(account["id"]) as conn:
        queue.enqueue(conn, account["id"], "apply_events", src["id"])
    assert queue.claim(1) == []
    assert queue.fail(job, "boom") == "retry"
    job["attempts"] = job["max_attempts"]
    assert queue.fail(job, "boom again") == "dead"
    with db.scoped(account["id"]) as conn:
        s = conn.execute("SELECT status, last_error FROM drive_index.sources WHERE id = %s", (src["id"],)).fetchone()
        assert s["last_error"].startswith("boom")
        assert queue.retry_dead(conn, src["id"]) == 1


def test_stale_running_jobs_requeued(account):
    src = make_source(account["id"])
    with db.scoped(account["id"]) as conn:
        queue.enqueue(conn, account["id"], "apply_events", src["id"])
    queue.claim(1)
    with db.system() as conn:
        conn.execute("UPDATE drive_index.index_jobs SET locked_at = now() - interval '1 hour'")
    assert queue.requeue_stale(60) == 1
