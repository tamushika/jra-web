-- SPEC-T34b read-only preflight.
-- Stop every writer to public.races before running this file.  A CREATE_INDEX
-- result permits the separately executed autocommit migration; a
-- POSTCHECK_ONLY result means that the exact valid index already exists.

BEGIN TRANSACTION READ ONLY;

DO $t34b_preflight$
DECLARE
    races_oid OID := 'public.races'::regclass;
    index_oid OID;
    index_relkind "char";
    index_table_oid OID;
    index_method TEXT;
    index_unique BOOLEAN;
    index_valid BOOLEAN;
    index_ready BOOLEAN;
    index_nkeyatts SMALLINT;
    index_natts SMALLINT;
    normalized_predicate TEXT;
    exact_definition BOOLEAN;
    duplicate_group_count BIGINT;
BEGIN
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

    IF duplicate_group_count > 0 THEN
        RAISE EXCEPTION USING
            MESSAGE = format(
                'T34b preflight aborted: %s duplicate complete natural-key group(s) remain',
                duplicate_group_count
            ),
            HINT = 'Do not create the index. Review and approve the T34b cleanup plan first.';
    END IF;

    index_oid := to_regclass('public.uq_races_natural_key');
    IF index_oid IS NULL THEN
        RAISE NOTICE 'T34b preflight passed: index is absent; next action is CREATE_INDEX';
        RETURN;
    END IF;

    SELECT c.relkind
      INTO index_relkind
      FROM pg_catalog.pg_class AS c
     WHERE c.oid = index_oid;

    IF index_relkind IS DISTINCT FROM 'i'::"char" THEN
        RAISE EXCEPTION USING
            MESSAGE = format(
                'T34b preflight aborted: public.uq_races_natural_key exists but is not an index (relkind=%s)',
                index_relkind
            ),
            HINT = 'Do not remove or replace the object without explicit operator approval.';
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

    exact_definition :=
        index_table_oid = races_oid
        AND index_method = 'btree'
        AND index_unique
        AND index_nkeyatts = 4
        AND index_natts = 4
        AND regexp_replace(
                lower(pg_catalog.pg_get_indexdef(index_oid, 1, true)),
                '[[:space:]"]', '', 'g'
            ) = 'date'
        AND regexp_replace(
                lower(pg_catalog.pg_get_indexdef(index_oid, 2, true)),
                '[[:space:]"]', '', 'g'
            ) = 'place'
        AND regexp_replace(
                lower(pg_catalog.pg_get_indexdef(index_oid, 3, true)),
                '[[:space:]"]', '', 'g'
            ) = 'race_num'
        AND regexp_replace(
                lower(pg_catalog.pg_get_indexdef(index_oid, 4, true)),
                '[[:space:]"]', '', 'g'
            ) = 'horse_number'
        AND normalized_predicate =
            'dateisnotnullandplaceisnotnullandrace_numisnotnullandhorse_numberisnotnull';

    IF exact_definition IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING
            MESSAGE = format(
                'T34b preflight aborted: same-name index has a different definition: %s',
                pg_catalog.pg_get_indexdef(index_oid)
            ),
            HINT = 'Do not replace the index without explicit operator approval.';
    END IF;

    IF NOT index_valid OR NOT index_ready THEN
        RAISE EXCEPTION USING
            MESSAGE = format(
                'T34b preflight aborted: exact index is not usable (indisvalid=%s, indisready=%s)',
                index_valid,
                index_ready
            ),
            HINT = 'Keep writers stopped. DROP/REINDEX CONCURRENTLY requires separate approval.';
    END IF;

    RAISE NOTICE 'T34b preflight passed: exact valid index already exists; next action is POSTCHECK_ONLY';
END
$t34b_preflight$;

SELECT CASE
           WHEN to_regclass('public.uq_races_natural_key') IS NULL
               THEN 'CREATE_INDEX'
           ELSE 'POSTCHECK_ONLY'
       END AS t34b_next_action;

COMMIT;
