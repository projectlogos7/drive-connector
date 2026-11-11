# Nous Drive Connectors + Index MCP

A live index of Google Drive and local folders, kept in Postgres, with an MCP server on top so Claude
(or any MCP client) can see the file/folder structure, what's new, and what each file is.

* **Phase 1 (indexing):** files, folders, structure, metadata and a change log, from Google Drive and
  from local folders via the Nous Sync agent. Notifications for new files.
* **Phase 2 (content):** text extraction, chunking, embeddings (pgvector), per-file summaries (Claude),
  content and meaning search, and a Knowledge Base retrieval endpoint. Off by default; switch it on
  globally and per source.

Everything lives in the `drive_index` schema of the Knowledge Base's Neon database, isolated per account
with row-level security.

```
 Google Drive ──push/poll──► Drive connector ─┐
                                              ├─► normalized events ─► job queue ─► indexer ─► drive_index.*
 Local folders ─FSEvents/inotify─► Nous Sync ─┘    (ingest API)        (index_jobs)            (nodes, change_events, …)
                                                                                                  │
                     Claude / MCP clients ◄── Index MCP server ◄── row-level security ◄───────────┘
                     Slack / email / webhook ◄── batched notifications
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design and a requirement-by-requirement map of
the PRD, and [agent/README.md](agent/README.md) for the local agent.

## Components

| Command | What it runs |
| --- | --- |
| `nous-drive serve` | HTTP API: web app (`/app`), OAuth callback, Drive webhook, agent ingest API, read API, KB search |
| `nous-drive worker` | Indexer worker (job queue) and the scheduler (polling fallback, channel renewal, nightly reconcile, notifications, alerts, retention) |
| `nous-drive mcp` / `mcp --http` | Index MCP server (stdio, or streamable HTTP with bearer keys) |
| `nous-sync` | The local agent (separate package in `agent/`) |

## Setup

1. **Database.** Postgres 15+ with `pgvector` and `pg_trgm` (Neon has both). Use the Knowledge Base database;
   the migrations create the `drive_index` schema and the restricted `drive_index_app` role.

   ```bash
   pip install -e ".[content]"
   cp .env.example .env   # fill in, then export the variables
   nous-drive migrate
   nous-drive create-account "Nous" --email you@example.org   # prints the first admin key
   ```

2. **Google OAuth client** (Google Cloud console → APIs & Services): enable the *Google Drive API*, create an
   OAuth client of type *Web application*, add the redirect URI `<PUBLIC_BASE_URL>/oauth/google/callback`.
   Phase 1 requests only `drive.metadata.readonly`. Content (Phase 2) is a separate, explicit upgrade to
   `drive.readonly` ("Connect with content access" in the web app). For push notifications the public URL
   must be HTTPS; without it the connector polls the changes feed (every 5 minutes).

3. **Run** the API, worker and MCP server (or `docker compose up`):

   ```bash
   nous-drive serve --port 8080
   nous-drive worker
   nous-drive mcp --http --port 8090      # or use stdio from the MCP client
   ```

4. **Connect sources** in the web app at `<PUBLIC_BASE_URL>/app` (sign in with the admin key):
   * *Connect Drive*: Google sign-in, then pick any mix of My Drive, folders and shared drives.
   * *Devices*: create a pairing code, then on the computer:
     ```bash
     agent/installers/install.sh --code ABCD-EFGH --server https://drive.nous.example --name "Nous MacBook Pro"
     nous-sync add ~/Footage
     ```
   The Sources page shows first-index progress (folders and files counted) and health.

## Using it from Claude

Create one MCP session key per Claude session/client (web app → *MCP keys*, or
`nous-drive create-key --admin-key … "Claude Desktop"`). Each key keeps its own "what's new" checkpoint.

**stdio** (Claude Desktop / Claude Code config):

```json
{
  "mcpServers": {
    "nous-drive": {
      "command": "nous-drive",
      "args": ["mcp"],
      "env": {
        "NOUS_DRIVE_DATABASE_URL": "postgresql://…",
        "NOUS_DRIVE_API_KEY": "nd_sess_…"
      }
    }
  }
}
```

**HTTP:** `https://<mcp-host>/mcp` with header `Authorization: Bearer nd_sess_…`.

Tools: `list_sources`, `get_sync_status`, `get_tree`, `list_files`, `search_files`, `get_file`, `whats_new`,
`list_changes`, `describe_file`, `list_notifications`, and control tools `resync_source`, `pause_source`,
`resume_source`, `get_connect_link`. The resource `nous-drive://notifications` is updated (and subscribed
clients are notified) whenever a new-files notification is sent.

Example questions it answers: *What was added to my Drive since yesterday?* · *Show the folder tree under
Madarasa / Episode 2, two levels deep.* · *Find all .mov files over 2 GB in the local Footage folder.* ·
*What is this new file Unmaking_Citizens_v4.docx?* · *Is the sync agent on my laptop online?*

## Notifications

New and changed files are batched per source (default 3-minute window): a 200-file upload becomes one
message with folder and counts, linking back to `whats_new`. Delivery: MCP (resource update), plus any of
Slack incoming webhook, email (SMTP) and a generic webhook — configure the ones you want in `.env`
(the PRD left Slack vs email open, so both are supported). Per source: on/off, which change types notify
(added by default; modified, moved, deleted optional), and quiet folders. Alerts are sent when a source
is degraded for over 30 minutes or a device is offline for a long time while its source is live.

## Phase 2: content

Runs entirely in-house by default — **no API keys and no per-use cost**:

| Piece | Default (free, in-house) | Other options |
| --- | --- | --- |
| Text extraction | built in (PDF, Word, Excel, PowerPoint, Google Docs/Sheets/Slides, text) | — |
| Embeddings (meaning search) | `fastembed`: open multilingual model (paraphrase-multilingual-MiniLM-L12-v2, English/Hindi/Urdu and 50+ languages) on CPU; downloaded once, ~220 MB | `hash` (no model, keyword-like only), `voyage` (paid API) |
| Summaries | `extractive`: the file's opening sentences | `ollama`: an open LLM on your own server (e.g. `ollama pull qwen2.5:3b`), `claude`: paid API, cheapest model (Haiku 5.5) with input capped at ~6k tokens per file |
| Keyword search | Postgres full-text | — |

```bash
pip install -e ".[content]"
NOUS_DRIVE_CONTENT_ENABLED=true
# optional in-house LLM summaries:
#   install Ollama on the server, `ollama pull qwen2.5:3b`, then NOUS_DRIVE_SUMMARY_PROVIDER=ollama
```

Then per source: `PATCH /api/sources/<id>/settings {"content": {"enabled": true, "extensions": [], "max_bytes": null, "skip_folders": ["/Archive"]}}`
and `POST /api/sources/<id>/backfill-content`. Drive files are downloaded/exported by the server (needs the
content scope); local files are extracted on the machine by the agent, which sends text only for files the
server requests. Re-extraction happens only when the file's content hash changes; every version's text and
summary is kept. `search_files(mode="content")` and `GET /api/kb/search` do hybrid keyword + vector search.
If you change the embedding model later, run `nous-drive reembed`.

## Tests

```bash
pip install -e ".[content,dev]" -e ./agent
NOUS_DRIVE_TEST_ADMIN_URL=postgresql://postgres:postgres@localhost/postgres pytest
```

The suite creates a throwaway database and covers the indexer, row-level security, the job queue, the Drive
connector against a fake Drive API (crawl, changes feed, moves in/out, rate limits, reconcile, webhooks), the
MCP tools and the real MCP server over stdio, notifications, Phase 2 content, the agent engine and watcher,
and an agent → API → index → MCP end-to-end run. `scripts/loadtest.py --nodes 1000000 --sources 50` checks
read latency at the target scale.
