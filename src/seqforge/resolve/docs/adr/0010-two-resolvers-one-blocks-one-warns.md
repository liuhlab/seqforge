# 10. Two resolvers, two refusals — the byte resolver blocks, the metadata resolver warns

The consistent-looking rule is that a disagreement is a `Conflict` everywhere, and it would stop
datasets compiling over facts no rule reads: a chemistry the bytes contradict leaves every
downstream parameter undecidable, while a null `tissue` compiles fine. So the byte resolver refuses
an `observed` ↔ `asserted` disagreement until a human confirms, and the metadata resolver decides —
stronger basis wins, equal authorities that disagree leave the attribute null — and emits only a
`Warning`. Code does not get to break a tie between equals, and the metadata resolver is handed a
`FileIdentity` rather than an `Observation`, so no probe signal can reach it and re-decide the
library.
