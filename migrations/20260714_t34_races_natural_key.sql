-- T34b index creation. Execute this file as one standalone statement only,
-- with the client's transaction wrapper disabled.
-- Run the matching preflight first and the matching postcheck afterwards.

CREATE UNIQUE INDEX CONCURRENTLY uq_races_natural_key
ON public.races (date, place, race_num, horse_number)
WHERE date IS NOT NULL
  AND place IS NOT NULL
  AND race_num IS NOT NULL
  AND horse_number IS NOT NULL;
