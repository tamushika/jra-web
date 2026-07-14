-- SPEC-T34b read-only postcheck.  Run only after the index creation command
-- returns successfully (or preflight reports POSTCHECK_ONLY).

BEGIN TRANSACTION READ ONLY;

DO $t34b_postcheck$
DECLARE
    races_oid OID := 'public.races'::regclass;
    index_oid OID := to_regclass('public.uq_races_natural_key');
    index_table_oid OID;
    index_method TEXT;
    index_unique BOOLEAN;
    index_valid BOOLEAN;
    index_ready BOOLEAN;
    index_nkeyatts SMALLINT;
    index_natts SMALLINT;
    normalized_predicate TEXT;
    duplicate_group_count BIGINT;
BEGIN
    IF index_oid IS NULL THEN
        RAISE EXCEPTION 'T34b postcheck failed: public.uq_races_natural_key does not exist';
    END IF;

    SELECT i.indrelid,
           am.amname,
           i.indisunique,
           i.indisvalid,
           i.indisready,
           i.indnkeyatts,
           i.indnatts,
           regexp_replace(
               lower(COALESCE(pg_catalog.pg_get_expr(i.indpred, i.indrelid, true), '')),
               '[[:space:]()"]', '', 'g'
           )
      INTO index_table_oid,
           index_method,
           index_unique,
           index_valid,
           index_ready,
           index_nkeyatts,
           index_natts,
           normalized_predicate
      FROM pg_catalog.pg_index AS i
      JOIN pg_catalog.pg_class AS c ON c.oid = i.indexrelid
      JOIN pg_catalog.pg_am AS am ON am.oid = c.relam
     WHERE i.indexrelid = index_oid;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'T34b postcheck failed: same-name object is not an index';
    END IF;

    IF index_table_oid IS DISTINCT FROM races_oid
       OR index_method IS DISTINCT FROM 'btree'
       OR index_unique IS DISTINCT FROM true
       OR index_valid IS DISTINCT FROM true
       OR index_ready IS DISTINCT FROM true
       OR index_nkeyatts IS DISTINCT FROM 4
       OR index_natts IS DISTINCT FROM 4
       OR regexp_replace(
              lower(pg_catalog.pg_get_indexdef(index_oid, 1, true)),
              '[[:space:]"]', '', 'g'
          ) IS DISTINCT FROM 'date'
       OR regexp_replace(
              lower(pg_catalog.pg_get_indexdef(index_oid, 2, true)),
              '[[:space:]"]', '', 'g'
          ) IS DISTINCT FROM 'place'
       OR regexp_replace(
              lower(pg_catalog.pg_get_indexdef(index_oid, 3, true)),
              '[[:space:]"]', '', 'g'
          ) IS DISTINCT FROM 'race_num'
       OR regexp_replace(
              lower(pg_catalog.pg_get_indexdef(index_oid, 4, true)),
              '[[:space:]"]', '', 'g'
          ) IS DISTINCT FROM 'horse_number'
       OR normalized_predicate IS DISTINCT FROM
          'dateisnotnullandplaceisnotnullandrace_numisnotnullandhorse_numberisnotnull'
    THEN
        RAISE EXCEPTION USING
            MESSAGE = format(
                'T34b postcheck failed: index state or definition differs: %s',
                pg_catalog.pg_get_indexdef(index_oid)
            ),
            HINT = 'Keep writers stopped and inspect pg_index before any recovery action.';
    END IF;

    SELECT COUNT(*)
      INTO duplicate_group_count
      FROM (
            SELECT date, place, race_num, horse_number
              FROM public.races
             WHERE date IS NOT NULL
               AND place IS NOT NULL
               AND race_num IS NOT NULL
               AND horse_number IS NOT NULL
             GROUP BY date, place, race_num, horse_number
            HAVING COUNT(*) > 1
      ) AS duplicate_keys;

    IF duplicate_group_count <> 0 THEN
        RAISE EXCEPTION 'T34b postcheck failed: % duplicate complete natural-key group(s) remain',
            duplicate_group_count;
    END IF;
END
$t34b_postcheck$;

-- Planning this statement is a non-mutating smoke check of PostgreSQL's
-- partial-index inference.  EXPLAIN without ANALYZE never executes the INSERT.
EXPLAIN (COSTS OFF)
INSERT INTO public.races (date, place, race_num, horse_number)
VALUES (NULL, NULL, NULL, NULL)
ON CONFLICT (date, place, race_num, horse_number)
WHERE date IS NOT NULL
  AND place IS NOT NULL
  AND race_num IS NOT NULL
  AND horse_number IS NOT NULL
DO UPDATE SET date = EXCLUDED.date;

SELECT i.indisunique,
       i.indisvalid,
       i.indisready,
       pg_catalog.pg_get_indexdef(i.indexrelid) AS index_definition,
       pg_catalog.pg_get_expr(i.indpred, i.indrelid, true) AS index_predicate
  FROM pg_catalog.pg_index AS i
 WHERE i.indexrelid = 'public.uq_races_natural_key'::regclass;

COMMIT;
