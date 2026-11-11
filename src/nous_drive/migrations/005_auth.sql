-- Email magic-link login: members per account + one-time login tokens.

CREATE TABLE IF NOT EXISTS drive_index.members (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id      uuid NOT NULL REFERENCES drive_index.accounts(id) ON DELETE CASCADE,
  email           text NOT NULL,
  name            text,
  role            text NOT NULL DEFAULT 'admin' CHECK (role IN ('admin','member')),
  created_at      timestamptz NOT NULL DEFAULT now(),
  deactivated_at  timestamptz,
  UNIQUE (account_id, email)
);
CREATE INDEX IF NOT EXISTS members_email_idx ON drive_index.members (lower(email))
  WHERE deactivated_at IS NULL;

CREATE TABLE IF NOT EXISTS drive_index.login_tokens (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id   uuid NOT NULL REFERENCES drive_index.accounts(id) ON DELETE CASCADE,
  member_id    uuid NOT NULL REFERENCES drive_index.members(id) ON DELETE CASCADE,
  email        text NOT NULL,
  token_hash   text NOT NULL UNIQUE,
  expires_at   timestamptz NOT NULL,
  used_at      timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS login_tokens_hash_idx ON drive_index.login_tokens (token_hash)
  WHERE used_at IS NULL;

-- Seed members from existing account emails (first admin for each account).
INSERT INTO drive_index.members (account_id, email, name, role)
SELECT a.id, lower(a.email), a.name, 'admin'
FROM drive_index.accounts a
WHERE a.email IS NOT NULL AND a.email <> ''
ON CONFLICT (account_id, email) DO NOTHING;

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['members','login_tokens']
  LOOP
    EXECUTE format('ALTER TABLE drive_index.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS account_isolation ON drive_index.%I', t);
    EXECUTE format('CREATE POLICY account_isolation ON drive_index.%I TO drive_index_app
                    USING (account_id = drive_index.current_account())
                    WITH CHECK (account_id = drive_index.current_account())', t);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON drive_index.%I TO drive_index_app', t);
  END LOOP;
END$$;
