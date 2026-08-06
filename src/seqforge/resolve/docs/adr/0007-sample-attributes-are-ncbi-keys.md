# 7. Sample attributes are an open dict over NCBI's 960 harmonized names

`SampleGroup`'s typed `tissue` and `condition` fields were both wrong: `condition` was a key we
coined, so it accepted whatever an extraction wanted to call one, and neither could hold `strain` —
the fact that separated the pilot's wild-type samples from its *daf-2* mutants. Attributes are now
an open dict keyed by NCBI's 960 harmonized BioSample names, any other key refused; 960 typed fields
lost because NCBI's next addition would then be a schema migration rather than a data refresh, and a
free-form dict lost because a key that accepts everything checks nothing.

**Status.** Amended — an attribute outside the 960 now warns rather than being silently skipped.
