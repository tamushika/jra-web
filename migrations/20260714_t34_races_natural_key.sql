-- SPEC-T34: prevent duplicate JRA result rows at the database boundary.
--
-- Apply this migration manually to Neon before deploying the updated updater.
-- It deliberately aborts when any pre-existing duplicate is found; this script
-- never chooses or deletes a row on the operator's behalf.

BEGIN;

-- Keep the preflight result valid until NOT NULL and UNIQUE are installed.
LOCK TABLE races IN SHARE ROW EXCLUSIVE MODE;

DO $t34_preflight$
DECLARE
    null_key_count BIGINT;
    duplicate_key RECORD;
BEGIN
    SELECT COUNT(*)
      INTO null_key_count
      FROM races
     WHERE date IS NULL
        OR place IS NULL
        OR race_num IS NULL
        OR horse_number IS NULL;

    IF null_key_count > 0 THEN
        RAISE EXCEPTION USING
            MESSAGE = format(
                'T34 migration aborted: %s row(s) contain NULL in date/place/race_num/horse_number',
                null_key_count
            ),
            HINT = 'Back up and correct every NULL natural-key row before rerunning this migration.';
    END IF;

    SELECT date, place, race_num, horse_number, COUNT(*) AS row_count
      INTO duplicate_key
      FROM races
     GROUP BY date, place, race_num, horse_number
    HAVING COUNT(*) > 1
     ORDER BY date, place, race_num, horse_number
     LIMIT 1;

    IF FOUND THEN
        RAISE EXCEPTION USING
            MESSAGE = format(
                'T34 migration aborted: duplicate natural key date=%s place=%s race_num=%s horse_number=%s count=%s',
                duplicate_key.date,
                duplicate_key.place,
                duplicate_key.race_num,
                duplicate_key.horse_number,
                duplicate_key.row_count
            ),
            HINT = 'Back up and resolve every duplicate before rerunning this migration.';
    END IF;
END
$t34_preflight$;

-- Standard PostgreSQL UNIQUE treats NULLs as distinct.  Enforce completeness
-- first so the natural key has no NULL loophole for this or any other writer.
ALTER TABLE races
    ALTER COLUMN date SET NOT NULL,
    ALTER COLUMN place SET NOT NULL,
    ALTER COLUMN race_num SET NOT NULL,
    ALTER COLUMN horse_number SET NOT NULL;

-- PostgreSQL has no ALTER TABLE ... ADD CONSTRAINT IF NOT EXISTS, so guard the
-- named constraint in a catalog check.  ON CONFLICT infers this constraint from
-- its four columns.
DO $t34_constraint$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conrelid = 'races'::regclass
           AND conname = 'uq_races_date_place_race_num_horse_number'
           AND contype = 'u'
    ) THEN
        ALTER TABLE races
            ADD CONSTRAINT uq_races_date_place_race_num_horse_number
            UNIQUE (date, place, race_num, horse_number);
    END IF;
END
$t34_constraint$;

COMMIT;
