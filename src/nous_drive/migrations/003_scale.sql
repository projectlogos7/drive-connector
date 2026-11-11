-- Scale hardening (M5): fast name/path search, ordered category listings, cached per-source stats.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS nodes_path_trgm ON drive_index.nodes USING gin (lower(path) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS nodes_cat_name  ON drive_index.nodes (account_id, category, lower(name), id)
  WHERE status = 'active';
CREATE INDEX IF NOT EXISTS nodes_src_stats ON drive_index.nodes (source_id, status, kind) INCLUDE (size, content_status);
CREATE INDEX IF NOT EXISTS ce_account_type_id ON drive_index.change_events (account_id, type, id DESC);

ALTER TABLE drive_index.sync_cursors ADD COLUMN IF NOT EXISTS stats jsonb;
ALTER TABLE drive_index.sync_cursors ADD COLUMN IF NOT EXISTS stats_at timestamptz;
