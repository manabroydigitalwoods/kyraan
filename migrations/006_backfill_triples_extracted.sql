-- 005 added fact.triples_extracted_at but left it NULL for facts whose
-- triples were ALREADY extracted, so the next catch-up re-sent every one
-- of them to the model. Re-extraction is not idempotent: the uniqueness
-- constraint is (head, relation, tail, fact_id), and a model that
-- normalizes "Aarav"/"aarav" or phrases a relation slightly differently
-- on the second pass inserts a NEAR-duplicate the constraint can't catch
-- — a silently doubled graph, plus the token spend of reprocessing the
-- whole memory (Bugbot P2).
--
-- Backfill: a fact that already has triples has plainly been extracted.
-- (triple carries no timestamp of its own, so the fact's own creation
-- time is the honest lower bound — it is certainly not LATER than the
-- extraction.)
UPDATE fact f
   SET triples_extracted_at = COALESCE(f.created_at, now())
 WHERE f.triples_extracted_at IS NULL
   AND EXISTS (SELECT 1 FROM triple t WHERE t.fact_id = f.id);
