# 31. A collapsed citation is regenerable only from the record set, so harvest writes every member

Folding near-identical records onto one exemplar makes two facts — which spans are invariant, and
what a reduced member was sent as — properties of the set rather than of any one record, so every
rendered member reaches disk even though a model saw most of them only in reduced form. Re-deriving
the collapse on demand was rejected because a tokenizer change or one extra fetched record would
silently re-answer "was this quote invariant?" for a claim already stored; a subject list on
`Assertion` was rejected because placing a claim that way means dataset scope, which downgrades
every collapsed claim from `asserted` to `inferred`; and fanning a claim only to the records whose
bytes already carry its quote was rejected as anti-correlated with value, since it reads the
majority and skips exactly the records that differ.
