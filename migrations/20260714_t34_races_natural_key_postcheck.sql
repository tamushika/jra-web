-- SPEC-T34b rollback-only postcheck.  Run only after the index creation
-- command returns successfully (or preflight reports POSTCHECK_ONLY).
--
-- This transaction deliberately executes the real ON CONFLICT path against
-- one existing complete-key row, verifies that two upserts still converge to
-- one unchanged logical row, and always rolls the smoke operation back.
-- Keep every public.races writer stopped for the whole migration sequence.

BEGIN;

LOCK TABLE public.races IN SHARE ROW EXCLUSIVE MODE;

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

DO $t34b_upsert_smoke$
DECLARE
    sample_date public.races.date%TYPE;
    sample_place public.races.place%TYPE;
    sample_race_num public.races.race_num%TYPE;
    sample_horse_number public.races.horse_number%TYPE;
    before_count BIGINT;
    after_count BIGINT;
    before_row JSONB;
    after_row JSONB;
    smoke_iteration INTEGER;
BEGIN
    SELECT r.date,
           r.place,
           r.race_num,
           r.horse_number,
           to_jsonb(r)
      INTO sample_date,
           sample_place,
           sample_race_num,
           sample_horse_number,
           before_row
      FROM public.races AS r
     WHERE r.date IS NOT NULL
       AND r.place IS NOT NULL
       AND r.race_num IS NOT NULL
       AND r.horse_number IS NOT NULL
     ORDER BY r.date, r.place, r.race_num, r.horse_number
     LIMIT 1;

    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            MESSAGE = 'T34b postcheck failed: no complete-key row is available for the rollback smoke test',
            HINT = 'Verify the target table and run an explicitly approved synthetic smoke test before enabling writers.';
    END IF;

    SELECT COUNT(*)
      INTO before_count
      FROM public.races AS r
     WHERE r.date = sample_date
       AND r.place = sample_place
       AND r.race_num = sample_race_num
       AND r.horse_number = sample_horse_number;

    IF before_count IS DISTINCT FROM 1 THEN
        RAISE EXCEPTION 'T34b postcheck failed: smoke key has % rows before upsert',
            before_count;
    END IF;

    -- Clone the complete existing row so table-level NOT NULL constraints are
    -- preserved.  The natural-key conflict must route both attempts to the
    -- same existing row; no INSERT may survive this transaction.
    FOR smoke_iteration IN 1..2 LOOP
        INSERT INTO public.races
        SELECT r.*
          FROM public.races AS r
         WHERE r.date = sample_date
           AND r.place = sample_place
           AND r.race_num = sample_race_num
           AND r.horse_number = sample_horse_number
         LIMIT 1
        ON CONFLICT (date, place, race_num, horse_number)
        WHERE date IS NOT NULL
          AND place IS NOT NULL
          AND race_num IS NOT NULL
          AND horse_number IS NOT NULL
        DO UPDATE SET date = EXCLUDED.date;
    END LOOP;

    SELECT COUNT(*), MIN(to_jsonb(r)::TEXT)::JSONB
      INTO after_count, after_row
      FROM public.races AS r
     WHERE r.date = sample_date
       AND r.place = sample_place
       AND r.race_num = sample_race_num
       AND r.horse_number = sample_horse_number;

    IF after_count IS DISTINCT FROM 1 THEN
        RAISE EXCEPTION 'T34b postcheck failed: repeated upsert converged to % rows',
            after_count;
    END IF;

    IF after_row IS DISTINCT FROM before_row THEN
        RAISE EXCEPTION USING
            MESSAGE = 'T34b postcheck failed: rollback smoke test changed logical row values',
            HINT = 'Keep writers stopped and inspect public.races triggers and the conflict action.';
    END IF;
END
$t34b_upsert_smoke$;

SELECT i.indisunique,
       i.indisvalid,
       i.indisready,
       pg_catalog.pg_get_indexdef(i.indexrelid) AS index_definition,
       pg_catalog.pg_get_expr(i.indpred, i.indrelid, true) AS index_predicate
  FROM pg_catalog.pg_index AS i
 WHERE i.indexrelid = 'public.uq_races_natural_key'::regclass;

-- The smoke test is intentionally non-persistent, including any trigger side
-- effects and row-version change caused by DO UPDATE.
ROLLBACK;
