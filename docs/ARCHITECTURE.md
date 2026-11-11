# Architecture and PRD traceability

This document maps the PRD *Drive Connectors (Google Drive + Local Folder) and Index MCP* (Draft v1,
11 Nov 2026) onto the code. Phase 1 (indexing) and Phase 2 (content) are both implemented; Phase 2 is
off until switched on (`NOUS_DRIVE_CONTENT_ENABLED` plus per-source `settings.content.enabled`).

## Layout

```
src/nous_drive/
  migrations/          001 schema + RLS · 002 content tables (pgvector) · 003 scale indexes/stats · 004 search fn
  db.py                pooled connections; scoped(account) = SET ROLE drive_index_app + app.account_id (RLS)
  models.py            NodeEvent: the normalized change event both connectors emit
  indexer.py           applies events: diff → change_events, path cascades, tombstones, adoption, mark-and-sweep
  queue.py             index_jobs: SKIP LOCKED claims, per-source serialization, backoff, dead letters
  worker.py            job runner (thread pool) + scheduler loop
  scheduler.py         polling fallback, channel renewal, nightly reconcile, device health, stats, notify, retention
  accounts.py          accounts, API keys, devices + pairing codes, sources, audit log
  ingest.py            local-agent ingest service (events, scans, heartbeat, content requests)
  notifications.py     batched new-file notifications, alerts, Slack/email/webhook delivery
  stats.py             cached per-source counts
  retention.py         90-day purge of change log and tombstones
  connectors/gdrive/   client (rate limit, backoff, token refresh) · auth (OAuth, encrypted tokens)
                       mapping (Drive file → NodeEvent) · sync (crawl, changes feed, reconcile, channels, webhook)
  content/             policy · extract · chunk · embed (in-house fastembed / hash / Voyage) · summarize (extractive / Ollama / Claude) · pipeline · search
  mcp/tools.py         MCP tools as plain functions (tested directly)
  mcp/server.py        MCP server (stdio + streamable HTTP with bearer keys), notifications resource
  api/app.py           FastAPI: web app API, OAuth callback, webhook, agent ingest, read + KB APIs, metrics
  api/static/index.html  web app (sources/progress, Drive connect + root picker, devices, keys, notifications)
agent/src/nous_sync/   config · ignore · state (SQLite snapshot + outbox) · scanner · engine · watcher · client
                       agent (runner, heartbeat, commands, volumes, content) · service (launchd/systemd/schtasks)
```

## Key design decisions

**Isolation (8.1).** Every table carries `account_id` and has an RLS policy `account_id = app.account_id`
for role `drive_index_app`. All request/job work runs in `db.scoped(account_id)`, which switches to that
role inside the transaction, so a bug in a query cannot read another account's rows. The owner role is used
only for cross-account bookkeeping (claiming jobs, resolving a key hash or webhook channel to its account,
the scheduler) and never to serve data. Name/path search runs through a narrow `SECURITY DEFINER` function
that filters explicitly by the session's account (RLS otherwise blocks the trigram index).

**One event shape.** Both connectors emit `NodeEvent` (`upsert` with full current metadata, `delete`,
`remove`, `unavailable`, `available`). The indexer diffs an upsert against the stored node to classify it as
added / modified / renamed / moved / trashed / restored. This makes replays idempotent (FR-26) and lets the
Drive changes feed, the reconcile, and the agent's catch-up all share one code path.

**Paths.** Nodes keep `parent_provider_id` as well as `parent_id`. Children that arrive before their parent
are stored as orphans and adopted when the parent appears; a folder rename/move recomputes the whole subtree
(path, depth, and absolute local path) in one recursive statement (FR-27, "bulk path update in one job").

**No silent loss (8.4).** Drive: the changes start token is stored *before* the crawl; after listing, the
changes since that token are replayed tagged with the scan id; page tokens are advanced in the same
transaction that commits the batch. Crawls are checkpointed per page and resume after a crash. Agent: a file's
snapshot row and its outgoing event are written in one SQLite transaction into a durable outbox, removed only
after the server accepted them. Jobs retry with exponential backoff, then go to a dead-letter state visible
in `get_sync_status` and the source's status (`degraded`).

**"What's new" checkpoints.** Change events carry the inserting transaction id (`xid8`). A checkpoint stores
the snapshot `xmin` at read time, and the next read takes events with `last_xmin <= tx_id < current xmin`.
Unlike "id greater than last seen", this never skips an event whose transaction committed late.
The same mechanism drives notification batching.

## Requirement map

| PRD | Where / how |
| --- | --- |
| FR-1 account provisions index space | `accounts.create_account` (+ admin key); RLS space is implicit in `account_id` |
| FR-2 many sources, Drive with several roots | one `drive_connections` row, one `sources` row per selected root (My Drive / folder / shared drive); local source = folder on a device |
| FR-3 pause/resume/resync/remove; remove deletes rows + revokes tokens | `mcp/tools.py` control tools, `DELETE /api/sources/{id}` (stops channel, revokes Google token if last source on the connection, cascades rows) |
| FR-4 statuses with reason | `sources.status/status_reason/status_since`; transitions in sync, ingest, queue (dead letters), scheduler (device offline) |
| FR-5 OAuth offline, metadata scope; content upgrade | `gdrive/auth.py`: `drive.metadata.readonly`; "content access" re-auth requests `drive.readonly` |
| FR-6 crawl with paging, start token first | `DriveSync.crawl` (flat listing for My Drive/shared drives, batched BFS for folders), resumable |
| FR-7 push notifications → pull changes | `/webhooks/drive` → `handle_notification` → coalesced `drive_pull_changes` job → `DriveSync.pull` |
| FR-8 channel renewal + poll fallback | scheduler renews before expiry; polls every 5 min (30 min while a channel is healthy) |
| FR-9 multi-parent, shortcuts, Google-native, trashed vs deleted, shared drives, shared-with-me | `mapping.py` (alt parents, shortcut target, no size/md5), membership rules in `sync.py`, "Shared with me" virtual folder |
| FR-10 rate limits, backoff, spread crawls | `client.py`: token-bucket limiter (`DRIVE_MAX_RPS`), retries on 429/5xx/rate-limit 403 with jittered backoff |
| FR-11 nightly reconcile | `drive_reconcile` job: full listing + replay + mark-and-sweep (`Indexer.finish_scan`) |
| FR-12 Python agent, installer, login start, low CPU | `agent/`, `installers/install.sh|ps1`, `service.py` (launchd/systemd/Task Scheduler, low priority) |
| FR-13 pairing code → device token | `POST /api/devices/pairing-codes`, `POST /agent/pair`; codes single-use, 15 min, stored hashed |
| FR-14 initial scan in batches | `SourceRunner.scan(full=True)`, outbox flushed in batches of 500 |
| FR-15 native events | `watcher.py` (watchdog: FSEvents / ReadDirectoryChangesW / inotify) |
| FR-16 debounce + stable before reporting | `Debouncer`: quiet period + unchanged size/mtime across checks |
| FR-17 moves via file id | watcher move events; inode/file-id matching in `engine.process_path` and `catch_up` |
| FR-18 catch-up after sleep/offline | `SourceEngine.catch_up` diff vs SQLite snapshot, sends only the difference |
| FR-19 ignore rules | `ignore.py` defaults + per-source patterns (synced from server settings) |
| FR-20 symlinks recorded not followed; external drives unavailable | symlink → `shortcut` node; `SourceRunner.check_volume` → `unavailable`/`available` events |
| FR-21 heartbeat | `POST /agent/heartbeat` every 30 s; scheduler marks sources `degraded: device offline` |
| FR-22–23 node table and fields | `nodes` table (+ `category`, `alt_parent_provider_ids`, `fs_id`, `device_id`) |
| FR-24 change log with before/after, 90 days | `change_events` (before/after snapshots, occurred vs detected time); `retention.purge` |
| FR-25 soft deletes, 90 days | status `deleted`/`removed` + `deleted_at`, cascaded; purged after 90 days |
| FR-26 idempotent | diff-based upserts, `ON CONFLICT` idempotency keys; replay tests |
| FR-27 descendant paths follow renames/moves | `Indexer.refresh_subtree` |
| FR-28 Phase 2 hooks | `content_hash`, `content_status`, `content_version` (+ `content_error`) |
| 6.5 MCP read + control tools | `mcp/tools.py`, `mcp/server.py` (all listed tools, plus `list_notifications`, `get_connect_link`) |
| FR-29/40 per-session checkpoint | `mcp_checkpoints` keyed by session (per MCP key by default, overridable) |
| FR-30 paging and size limits | cursors, `limit` ≤ 200, capped counts, `get_tree` entry cap |
| FR-31 caller auth, own account only | bearer session/admin keys (HTTP) or `NOUS_DRIVE_API_KEY` (stdio); RLS |
| FR-32 connect/pair in web app; MCP returns link | `/app`; `get_connect_link` |
| FR-33 batched notifications | `notifications.flush` (window per source, grouped by folder) |
| FR-34 MCP + push channel | `nous-drive://notifications` resource updates via pg `LISTEN/NOTIFY`; Slack / email / webhook |
| FR-35 per-source notification settings | `settings.notifications {enabled, types, quiet_folders}` |
| FR-36 links back to whats_new | notification payload `follow_up` |
| FR-37 one account, many sessions | keys per session, all reading the same index |
| FR-38–39 device names remembered, shown in results | `devices` keyed by machine id; re-pair keeps name; `device` in tool results and change events |
| 8.2 freshness | local: settle 2 s + queue; Drive: push → pull (3 s coalescing); poll fallback 5 min; nightly reconcile |
| 8.3 scale | `scripts/loadtest.py` (results below) |
| 8.4 reliability | see "No silent loss" |
| 8.5 security | read-only scopes, Fernet-encrypted tokens, hashed keys, per-device revocation, webhook channel token check, audit log |
| 8.6 observability | `get_sync_status` (lag, events/min, errors, queue depth, dead letters, last sync), `/metrics` (Prometheus), alerts after 30 min degraded / long device offline |
| 9 edge cases | moved out of folder → `removed`; big folder move → one cascade; multi-parent/shortcuts; permission loss → `delete` with reason; nested local roots rejected; source + detected time; Unicode/long names, case-only renames tested; re-pair without re-index (snapshot kept) |
| 10 Phase 2 | `content/*`: extraction (PDF, DOCX, XLSX, PPTX, Google Docs/Sheets/Slides export, text, HTML, RTF, CSV, JSON), re-extract only on hash change with versions kept, chunks + embeddings (pgvector, HNSW; in-house multilingual model by default), summaries (extractive by default, Ollama in-house or Claude optional), hybrid search in `search_files` and `GET /api/kb/search`, per-source content settings, agent-side extraction for local files |

## Load test (M5)

`scripts/loadtest.py --nodes 1000000 --sources 50` on a single local Postgres 16 (median of 5 calls):

| Call | ms |
| --- | --- |
| list_sources | 2 |
| get_sync_status (50 sources) | 57 |
| get_tree (depth 2, with files) | 9 |
| list_files (one folder) | 4 |
| list_files (recursive, .mov > 2 GB) | 25 |
| list_files (account-wide, type=video) | 15 |
| search_files (selective term, e.g. a file name) | 30–45 |
| search_files (term present in ~most paths; synthetic worst case) | 450–2,000 |
| whats_new (24 h) | 15 |
| list_changes (24 h) | 4 |

Indexer throughput through the full diff path is ~1,500 events/s per worker, so a 100,000-item Drive crawl
is bounded by Drive API quotas (minutes to a few hours), not by indexing. Search terms that occur in most paths (the
synthetic data names every folder "Folder N") are the slow case: candidates are capped at 20,000 and the
result reports `total_is_lower_bound`; real, more varied names behave like the selective case.

## Open question from the PRD

*Notification channel outside MCP — Slack DM, email, or both?* Both are implemented (plus a generic
webhook); whichever is configured in `.env` is used.
