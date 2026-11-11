-- Live progress written by workers while they run (current file, counts), read by the web app's live stream.
CREATE TABLE IF NOT EXISTS drive_index.live_progress (
  source_id   uuid PRIMARY KEY REFERENCES drive_index.sources(id) ON DELETE CASCADE,
  account_id  uuid NOT NULL REFERENCES drive_index.accounts(id) ON DELETE CASCADE,
  data        jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at  timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE drive_index.live_progress ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS account_isolation ON drive_index.live_progress;
CREATE POLICY account_isolation ON drive_index.live_progress TO drive_index_app
  USING (account_id = drive_index.current_account()) WITH CHECK (account_id = drive_index.current_account());
GRANT SELECT, INSERT, UPDATE, DELETE ON drive_index.live_progress TO drive_index_app;
