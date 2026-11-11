-- Nous Drive Index — Phase 1 schema (drive_index)
-- Isolation: shared tables keyed by account_id + Postgres row-level security.
-- The application runs scoped work as role drive_index_app with app.account_id set;
-- the schema owner (migration role) is used only for internal cross-account system work.

CREATE SCHEMA IF NOT EXISTS drive_index;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'drive_index_app') THEN
    CREATE ROLE drive_index_app NOLOGIN;
  END IF;
END$$;

-- Let the connecting (owner) role switch into the restricted role per transaction.
DO $$
BEGIN
  EXECUTE format('GRANT drive_index_app TO %I', current_user);
EXCEPTION WHEN others THEN
  RAISE NOTICE 'could not grant drive_index_app to %: %', current_user, SQLERRM;
END$$;

GRANT USAGE ON SCHEMA drive_index TO drive_index_app;

CREATE OR REPLACE FUNCTION drive_index.current_account() RETURNS uuid
LANGUAGE sql STABLE AS $$
  SELECT nullif(current_setting('app.account_id', true), '')::uuid
$$;

-- ---------------------------------------------------------------- accounts
CREATE TABLE IF NOT EXISTS drive_index.accounts (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name        text NOT NULL,
  email       text,
  org_id      text,
  settings    jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- API keys: admin keys (web app), session keys (MCP clients), device tokens (agents)
CREATE TABLE IF NOT EXISTS drive_index.api_keys (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id    uuid NOT NULL REFERENCES drive_index.accounts(id) ON DELETE CASCADE,
  kind          text NOT NULL CHECK (kind IN ('admin','session','device')),
  name          text NOT NULL,
  key_hash      text NOT NULL UNIQUE,
  key_prefix    text NOT NULL,
  device_id     uuid,
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_used_at  timestamptz,
  revoked_at    timestamptz
);

-- ---------------------------------------------------------------- devices
CREATE TABLE IF NOT EXISTS drive_index.devices (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id     uuid NOT NULL REFERENCES drive_index.accounts(id) ON DELETE CASCADE,
  name           text NOT NULL,
  machine_id     text,
  hostname       text,
  os             text,
  agent_version  text,
  last_seen_at   timestamptz,
  last_status    jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at     timestamptz NOT NULL DEFAULT now(),
  revoked_at     timestamptz,
  UNIQUE (account_id, machine_id)
);

CREATE TABLE IF NOT EXISTS drive_index.pairing_codes (
  code_hash    text PRIMARY KEY,
  account_id   uuid NOT NULL REFERENCES drive_index.accounts(id) ON DELETE CASCADE,
  device_name  text,
  expires_at   timestamptz NOT NULL,
  used_at      timestamptz,
  device_id    uuid,
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- drive connections
CREATE TABLE IF NOT EXISTS drive_index.drive_connections (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id    uuid NOT NULL REFERENCES drive_index.accounts(id) ON DELETE CASCADE,
  google_email  text,
  google_user_id text,
  scopes        text[] NOT NULL DEFAULT '{}',
  created_at    timestamptz NOT NULL DEFAULT now(),
  revoked_at    timestamptz
);

-- ---------------------------------------------------------------- sources
CREATE TABLE IF NOT EXISTS drive_index.sources (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id     uuid NOT NULL REFERENCES drive_index.accounts(id) ON DELETE CASCADE,
  type           text NOT NULL CHECK (type IN ('gdrive','local')),
  root_kind      text NOT NULL CHECK (root_kind IN ('my_drive','folder','shared_drive','local_folder')),
  root_ref       text NOT NULL,              -- Drive folder/drive id, 'root', or absolute local path
  name           text NOT NULL,              -- display name of the root
  connection_id  uuid REFERENCES drive_index.drive_connections(id) ON DELETE CASCADE,
  device_id      uuid REFERENCES drive_index.devices(id) ON DELETE SET NULL,
  status         text NOT NULL DEFAULT 'connecting'
                 CHECK (status IN ('connecting','initial_scan','live','degraded','paused','error')),
  status_reason  text,
  status_since   timestamptz NOT NULL DEFAULT now(),
  settings       jsonb NOT NULL DEFAULT '{}'::jsonb,   -- ignore rules, notifications, content
  pending_command text,                                -- e.g. 'full_rescan' for local agents
  last_sync_at   timestamptz,
  last_error     text,
  last_error_at  timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sources_account_idx ON drive_index.sources(account_id);

-- Encrypted OAuth refresh/access tokens (device tokens are hashed in api_keys)
CREATE TABLE IF NOT EXISTS drive_index.credentials (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id       uuid NOT NULL REFERENCES drive_index.accounts(id) ON DELETE CASCADE,
  connection_id    uuid REFERENCES drive_index.drive_connections(id) ON DELETE CASCADE,
  source_id        uuid REFERENCES drive_index.sources(id) ON DELETE CASCADE,
  kind             text NOT NULL CHECK (kind IN ('google_oauth')),
  encrypted_token  text NOT NULL,
  expires_at       timestamptz,
  updated_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (connection_id, kind)
);

-- ---------------------------------------------------------------- nodes
CREATE TABLE IF NOT EXISTS drive_index.nodes (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id          uuid NOT NULL REFERENCES drive_index.accounts(id) ON DELETE CASCADE,
  source_id           uuid NOT NULL REFERENCES drive_index.sources(id) ON DELETE CASCADE,
  provider_id         text NOT NULL,
  parent_id           uuid REFERENCES drive_index.nodes(id) ON DELETE SET NULL,
  parent_provider_id  text,
  alt_parent_provider_ids text[] NOT NULL DEFAULT '{}',
  is_root             boolean NOT NULL DEFAULT false,
  kind                text NOT NULL CHECK (kind IN ('file','folder','shortcut')),
  name                text NOT NULL,
  path                text NOT NULL,
  depth               int  NOT NULL DEFAULT 0,
  ext                 text,
  category            text,
  mime                text,
  size                bigint,
  checksum            text,
  created_at_src      timestamptz,
  modified_at_src     timestamptz,
  owner               text,
  last_modified_by    text,
  web_link            text,
  local_path          text,
  fs_id               text,
  shortcut_target     text,
  status              text NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active','trashed','deleted','unavailable','removed')),
  status_reason       text,
  extra               jsonb NOT NULL DEFAULT '{}'::jsonb,
  last_seen_scan      text,
  device_id           uuid,
  -- Phase 2 hooks
  content_hash        text,
  content_status      text NOT NULL DEFAULT 'not_requested'
                      CHECK (content_status IN ('not_requested','queued','extracted','failed','skipped')),
  content_version     int NOT NULL DEFAULT 0,
  content_error       text,
  -- bookkeeping
  version             int NOT NULL DEFAULT 1,
  first_indexed_at    timestamptz NOT NULL DEFAULT now(),
  indexed_at          timestamptz NOT NULL DEFAULT now(),
  deleted_at          timestamptz,
  UNIQUE (source_id, provider_id)
);
CREATE INDEX IF NOT EXISTS nodes_parent_idx   ON drive_index.nodes(parent_id);
CREATE INDEX IF NOT EXISTS nodes_pprov_idx    ON drive_index.nodes(source_id, parent_provider_id);
CREATE INDEX IF NOT EXISTS nodes_path_idx     ON drive_index.nodes(source_id, path text_pattern_ops);
CREATE INDEX IF NOT EXISTS nodes_account_idx  ON drive_index.nodes(account_id, status, kind);
CREATE INDEX IF NOT EXISTS nodes_name_lower   ON drive_index.nodes(account_id, lower(name) text_pattern_ops);
CREATE INDEX IF NOT EXISTS nodes_ext_idx      ON drive_index.nodes(account_id, ext);
CREATE INDEX IF NOT EXISTS nodes_cat_idx      ON drive_index.nodes(account_id, category);
CREATE INDEX IF NOT EXISTS nodes_modified_idx ON drive_index.nodes(account_id, modified_at_src DESC);
CREATE INDEX IF NOT EXISTS nodes_content_idx  ON drive_index.nodes(content_status) WHERE content_status = 'queued';
CREATE INDEX IF NOT EXISTS nodes_deleted_idx  ON drive_index.nodes(deleted_at) WHERE deleted_at IS NOT NULL;

-- ---------------------------------------------------------------- change log
CREATE TABLE IF NOT EXISTS drive_index.change_events (
  id              bigserial PRIMARY KEY,
  account_id      uuid NOT NULL REFERENCES drive_index.accounts(id) ON DELETE CASCADE,
  source_id       uuid NOT NULL REFERENCES drive_index.sources(id) ON DELETE CASCADE,
  node_id         uuid REFERENCES drive_index.nodes(id) ON DELETE SET NULL,
  device_id       uuid,
  type            text NOT NULL CHECK (type IN
                    ('added','modified','renamed','moved','trashed','restored','deleted',
                     'removed','unavailable','available')),
  kind            text,
  name            text,
  path            text,
  before          jsonb,
  after           jsonb,
  occurred_at     timestamptz NOT NULL DEFAULT now(),
  detected_at     timestamptz NOT NULL DEFAULT now(),
  tx_id           xid8 NOT NULL DEFAULT pg_current_xact_id(),
  idempotency_key text
);
CREATE INDEX IF NOT EXISTS ce_account_time ON drive_index.change_events(account_id, detected_at DESC);
CREATE INDEX IF NOT EXISTS ce_source_time  ON drive_index.change_events(source_id, detected_at DESC);
CREATE INDEX IF NOT EXISTS ce_node         ON drive_index.change_events(node_id);
CREATE INDEX IF NOT EXISTS ce_tx           ON drive_index.change_events(account_id, tx_id);
CREATE UNIQUE INDEX IF NOT EXISTS ce_idem  ON drive_index.change_events(source_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;

-- ---------------------------------------------------------------- cursors
CREATE TABLE IF NOT EXISTS drive_index.sync_cursors (
  source_id           uuid PRIMARY KEY REFERENCES drive_index.sources(id) ON DELETE CASCADE,
  account_id          uuid NOT NULL REFERENCES drive_index.accounts(id) ON DELETE CASCADE,
  drive_page_token    text,
  local_snapshot_ref  text,
  channel_id          text,
  channel_resource_id text,
  channel_token_hash  text,
  channel_expires_at  timestamptz,
  last_poll_at        timestamptz,
  last_full_scan_at   timestamptz,
  current_scan_id     text,
  scan_progress       jsonb NOT NULL DEFAULT '{}'::jsonb,
  notified_xmin       xid8,
  updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cursors_channel ON drive_index.sync_cursors(channel_id);

-- ---------------------------------------------------------------- job queue
CREATE TABLE IF NOT EXISTS drive_index.index_jobs (
  id           bigserial PRIMARY KEY,
  account_id   uuid NOT NULL REFERENCES drive_index.accounts(id) ON DELETE CASCADE,
  source_id    uuid REFERENCES drive_index.sources(id) ON DELETE CASCADE,
  kind         text NOT NULL,
  payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
  status       text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','done','dead')),
  priority     int NOT NULL DEFAULT 100,
  attempts     int NOT NULL DEFAULT 0,
  max_attempts int NOT NULL DEFAULT 8,
  run_after    timestamptz NOT NULL DEFAULT now(),
  locked_by    text,
  locked_at    timestamptz,
  error        text,
  dedupe_key   text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  finished_at  timestamptz
);
CREATE INDEX IF NOT EXISTS jobs_ready ON drive_index.index_jobs(priority, id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS jobs_source ON drive_index.index_jobs(source_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_dedupe ON drive_index.index_jobs(dedupe_key)
  WHERE status = 'pending' AND dedupe_key IS NOT NULL;

-- ---------------------------------------------------------------- MCP checkpoints
CREATE TABLE IF NOT EXISTS drive_index.mcp_checkpoints (
  account_id     uuid NOT NULL REFERENCES drive_index.accounts(id) ON DELETE CASCADE,
  session_id     text NOT NULL,
  last_event_id  bigint,
  last_xmin      xid8,
  checked_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (account_id, session_id)
);

-- ---------------------------------------------------------------- notifications
CREATE TABLE IF NOT EXISTS drive_index.notifications (
  id           bigserial PRIMARY KEY,
  account_id   uuid NOT NULL REFERENCES drive_index.accounts(id) ON DELETE CASCADE,
  source_id    uuid REFERENCES drive_index.sources(id) ON DELETE CASCADE,
  kind         text NOT NULL DEFAULT 'changes' CHECK (kind IN ('changes','alert')),
  title        text NOT NULL,
  body         text NOT NULL,
  payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
  deliveries   jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS notif_account ON drive_index.notifications(account_id, id DESC);

-- ---------------------------------------------------------------- audit
CREATE TABLE IF NOT EXISTS drive_index.audit_log (
  id          bigserial PRIMARY KEY,
  account_id  uuid NOT NULL REFERENCES drive_index.accounts(id) ON DELETE CASCADE,
  actor       text NOT NULL,
  action      text NOT NULL,
  target      text,
  detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- scheduler bookkeeping (system only)
CREATE TABLE IF NOT EXISTS drive_index.scheduler_state (
  task      text PRIMARY KEY,
  last_run  timestamptz NOT NULL
);

-- ---------------------------------------------------------------- row-level security
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['accounts','api_keys','devices','pairing_codes','drive_connections','sources',
                           'credentials','nodes','change_events','sync_cursors','index_jobs',
                           'mcp_checkpoints','notifications','audit_log']
  LOOP
    EXECUTE format('ALTER TABLE drive_index.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS account_isolation ON drive_index.%I', t);
    IF t = 'accounts' THEN
      EXECUTE format('CREATE POLICY account_isolation ON drive_index.%I TO drive_index_app
                      USING (id = drive_index.current_account())
                      WITH CHECK (id = drive_index.current_account())', t);
    ELSE
      EXECUTE format('CREATE POLICY account_isolation ON drive_index.%I TO drive_index_app
                      USING (account_id = drive_index.current_account())
                      WITH CHECK (account_id = drive_index.current_account())', t);
    END IF;
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON drive_index.%I TO drive_index_app', t);
  END LOOP;
END$$;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA drive_index TO drive_index_app;
