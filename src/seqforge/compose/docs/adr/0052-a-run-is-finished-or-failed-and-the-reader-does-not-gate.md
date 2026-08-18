# 52. A run is finished or failed, and the reader does not gate on it

A report carried no run state at all — only a count of samples holding a QC artifact — so a chimeric
plate that mapped all sixteen cells and wrote no matrix reported sixteen of sixteen with no alerts:
a count over samples cannot be short about a deliverable with no sample in its path. The state is
decided instead against what each workflow module already declares it leaves for the whole deposit,
its fan-in artifact, expanded once per Component, and it takes two values — a run that did not
produce what was demanded is a failure naming the file, never an `unfinished` a reader has to grade.
`skipped` would be a state with no producer, since nothing here skips a deliverable and a pilot's
exclusions happen at manifest time, before a pipeline exists; a hand-kept table of what a finished
run looks like lost to the declaration, whose one owner is the `rule all` demanding it. The reader's
exit code does not move — rendering succeeded — because merging the two leaves a caller unable to
tell a broken render from a broken run, the line ADR-0026 already draws for an alert.
