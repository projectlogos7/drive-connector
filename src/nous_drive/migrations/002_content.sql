-- Phase 2: content extraction, chunks, embeddings (pgvector), summaries.
-- All keyed to node_id + content_version. Same RLS model as Phase 1.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS drive_index.file_contents (
  node_id          uuid NOT NULL REFERENCES drive_index.nodes(id) ON DELETE CASCADE,
  content_version  int  NOT NULL,
  account_id       uuid NOT NULL REFERENCES drive_index.accounts(id) ON DELETE CASCADE,
  source_id        uuid NOT NULL REFERENCES drive_index.sources(id) ON DELETE CASCADE,
  content_hash     text,
  mime             text,
  extractor        text,
  text             text NOT NULL,
  char_count       int  NOT NULL,
  truncated        boolean NOT NULL DEFAULT false,
  extracted_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (node_id, content_version)
);

CREATE TABLE IF NOT EXISTS drive_index.chunks (
  id               bigserial PRIMARY KEY,
  node_id          uuid NOT NULL REFERENCES drive_index.nodes(id) ON DELETE CASCADE,
  content_version  int  NOT NULL,
  account_id       uuid NOT NULL REFERENCES drive_index.accounts(id) ON DELETE CASCADE,
  source_id        uuid NOT NULL REFERENCES drive_index.sources(id) ON DELETE CASCADE,
  ord              int  NOT NULL,
  text             text NOT NULL,
  tsv              tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED,
  UNIQUE (node_id, content_version, ord)
);
CREATE INDEX IF NOT EXISTS chunks_tsv ON drive_index.chunks USING gin(tsv);
CREATE INDEX IF NOT EXISTS chunks_node ON drive_index.chunks(node_id, content_version);

CREATE TABLE IF NOT EXISTS drive_index.embeddings (
  chunk_id    bigint PRIMARY KEY REFERENCES drive_index.chunks(id) ON DELETE CASCADE,
  account_id  uuid NOT NULL REFERENCES drive_index.accounts(id) ON DELETE CASCADE,
  node_id     uuid NOT NULL REFERENCES drive_index.nodes(id) ON DELETE CASCADE,
  model       text NOT NULL,
  embedding   vector(1024) NOT NULL
);
CREATE INDEX IF NOT EXISTS embeddings_hnsw ON drive_index.embeddings USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS embeddings_node ON drive_index.embeddings(node_id);

CREATE TABLE IF NOT EXISTS drive_index.summaries (
  node_id          uuid NOT NULL REFERENCES drive_index.nodes(id) ON DELETE CASCADE,
  content_version  int  NOT NULL,
  account_id       uuid NOT NULL REFERENCES drive_index.accounts(id) ON DELETE CASCADE,
  summary          text NOT NULL,
  model            text NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (node_id, content_version)
);

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['file_contents','chunks','embeddings','summaries']
  LOOP
    EXECUTE format('ALTER TABLE drive_index.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS account_isolation ON drive_index.%I', t);
    EXECUTE format('CREATE POLICY account_isolation ON drive_index.%I TO drive_index_app
                    USING (account_id = drive_index.current_account())
                    WITH CHECK (account_id = drive_index.current_account())', t);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON drive_index.%I TO drive_index_app', t);
  END LOOP;
END$$;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA drive_index TO drive_index_app;
