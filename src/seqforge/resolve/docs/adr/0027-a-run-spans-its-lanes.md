# 27. A run spans its lanes, and filenames group no further

De-laning was rejected once for role propagation inside a group already formed, and that argument
does not carry to grouping: keeping the lane in the run key compiled a four-lane library as four
quarter-depth samples at exit 0. A run is lane-blind — `run_key` strips a trailing separated `L`
plus exactly three digits and stops, never `_S<n>` and never anything read from the directory, so a
file's sample identity cannot move when a neighbour lands. Three digits rather than `L\d+` because
bcl2fastq pads a lane and a worm's larval stage does not, and the asymmetry settles the rest:
splitting a library gives quarter-depth matrices somebody notices, merging two gives one plausible
matrix nobody notices, so a grouping rule may fail only toward the split. Two runs of one sample
still do not rejoin from filenames; the answer there is to supply a record, not a wider name rule.
