# Getting started (server, Drive, computers, Claude via MCP)

## 1. Run the service

You need Postgres with `pgvector` and `pg_trgm` (the Knowledge Base's Neon database has both).

```bash
pip install -e .
cp .env.example .env        # fill in DATABASE_URL, SECRET_KEY, ENCRYPTION_KEY, PUBLIC_BASE_URL, Google client
set -a; . ./.env; set +a
nous-drive migrate
nous-drive create-account "Nous" --email you@example.org   # prints an admin key; the email can sign in by link
```

Build the web app once (Next.js; the API serves it at `/app`):

```bash
cd web && npm ci && NODE_ENV=production npx next build && cd ..
```

Run three processes (or `docker compose up`, which also builds the web app):

```bash
nous-drive serve --port 8080          # web app + API          → https://<your-domain>/app
nous-drive worker                     # indexing jobs, polling, content extraction + embeddings
nous-drive mcp --http --port 8090     # MCP server for Claude  → https://<your-mcp-domain>/mcp
```

Put both behind HTTPS (e.g. Caddy/nginx, Fly, Render). Google push notifications need the HTTPS API URL.

## 2. Connect Google Drive

1. Google Cloud console → enable *Google Drive API* → OAuth client (*Web application*) with redirect URI
   `https://<your-domain>/oauth/google/callback`. Put the client id/secret in `.env`.
2. Open `https://<your-domain>/app` → **Google Drive → Connect Google account**.
3. The whole My Drive is added and indexing starts at once. The dialog lets you switch to specific folders
   or add shared drives. Progress shows on the page; after the first pass it keeps itself up to date.

Content indexing (text extraction + in-house embeddings) is on by default and runs as queued jobs after the
file index. No LLM is used unless you set `NOUS_DRIVE_SUMMARY_PROVIDER=ollama` (in-house) or `claude`.

## 3. Add a computer (local folders)

**Computers → Add a computer**, then on that computer (Python 3.11+ required):

```bash
curl -fsSL https://<your-domain>/install.sh | bash -s -- --code ABCD-EFGH --server https://<your-domain> --name "Nous MacBook Pro"
nous-sync add ~/Footage
```

## 4. Connect Claude (MCP)

First create a key: web app → **Claude access → Create key** (one key per Claude app/session).
The key starts with `nd_sess_` and is shown once.

### Claude Desktop — remote server via `mcp-remote` (no Python needed on the laptop)

Claude.ai "custom connectors" sign in with OAuth, which this server does not offer yet (it uses keys), so
connect Claude Desktop through the small `mcp-remote` bridge (needs Node.js). In `claude_desktop_config.json`
(Settings → Developer → Edit config):

```json
{
  "mcpServers": {
    "nous-drive": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://<your-mcp-domain>/mcp",
               "--header", "Authorization: Bearer nd_sess_…"]
    }
  }
}
```

### Claude Code

```bash
claude mcp add --transport http nous-drive https://<your-mcp-domain>/mcp \
  --header "Authorization: Bearer nd_sess_…"
```

### Claude Desktop — local (stdio), runs on your computer

Needs this package installed locally (`pip install -e .`) and network access to the database.
Edit `claude_desktop_config.json` (Settings → Developer → Edit config):

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

Restart Claude Desktop; "nous-drive" appears in the tools list.

### The web app

* **Overview** — every source with a live progress bar: the step it is on (finding files → indexing →
  reading contents → making searchable), done/total, the file being processed and its size, speed and time left.
* **Search** — search inside documents and by name; filter by source, *only inside a folder*, type, and
  modified/added time. Filters are kept in the URL, so a search can be bookmarked or shared.
* **Google Drive / Computers** — what's connected and watched; **Activity** — every change, live.

### Things to ask Claude

* "What's new in my Drive?" (asks again later → only what arrived in between)
* "Show the folder tree under Madarasa / Episode 2, two levels deep."
* "Find all .mov files over 2 GB in the Footage folder."
* "What is Unmaking_Citizens_v4.docx?" (where it is, who added it, earlier versions, content summary)
* "Search inside my documents for the Bengal partition." (content search)
* "Is the sync on my MacBook healthy?" · "Re-scan the Footage folder."

Tools available: `search` (contents + names, with folder/type/time filters), `list_sources`, `get_sync_status`, `get_tree`, `list_files`, `search_files`, `get_file`,
`whats_new`, `list_changes`, `describe_file`, `list_notifications`, `resync_source`, `pause_source`,
`resume_source`, `get_connect_link`.

### Other systems (Knowledge Base, Majlis, …)

Use the same keys against the HTTP API: `GET /api/search?q=…`, `GET /api/kb/search?q=…` (content chunks with
file context), `GET /api/tree`, `GET /api/changes`, or read the `drive_index` schema directly in the database.
