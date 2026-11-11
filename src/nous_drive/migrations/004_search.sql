-- Name/path search that can use the trigram index under row-level security.
-- RLS prevents non-leakproof operators such as LIKE from being used as index conditions, so the
-- matching runs inside this SECURITY DEFINER function, which filters explicitly by the session's own
-- account (app.account_id) and returns only node ids. Callers still read the rows through RLS.
CREATE OR REPLACE FUNCTION drive_index.match_paths(patterns text[], name_prefix text, max_rows int)
RETURNS TABLE (node_id uuid)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = drive_index, pg_temp
AS $$
DECLARE
  acct uuid := drive_index.current_account();
  cond text;
BEGIN
  IF acct IS NULL OR patterns IS NULL OR cardinality(patterns) = 0 THEN
    RETURN;
  END IF;
  -- Exact/prefix name matches first (so ranking stays right when a common term is capped) ...
  IF name_prefix IS NOT NULL THEN
    RETURN QUERY EXECUTE
      'SELECT n.id FROM drive_index.nodes n WHERE n.account_id = $1 AND lower(n.name) LIKE $2 AND NOT n.is_root LIMIT $3'
      USING acct, name_prefix, max_rows;
  END IF;
  -- ... then every node whose path contains all terms (trigram index), capped.
  SELECT string_agg(format('lower(n.path) LIKE $2[%s]', i), ' AND ')
    INTO cond FROM generate_subscripts(patterns, 1) AS i;
  RETURN QUERY EXECUTE
    'SELECT n.id FROM drive_index.nodes n WHERE n.account_id = $1 AND NOT n.is_root AND ' || cond || ' LIMIT $3'
    USING acct, patterns, max_rows;
END
$$;
REVOKE ALL ON FUNCTION drive_index.match_paths(text[], text, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION drive_index.match_paths(text[], text, int) TO drive_index_app;
