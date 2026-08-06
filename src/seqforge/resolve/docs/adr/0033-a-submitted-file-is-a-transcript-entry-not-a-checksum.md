# 33. A submitted file is a transcript entry, and its md5 is an address we never check

The obvious use for an archive's per-file md5 is to hash the local FASTQ and join it to its record:
it costs a whole-file read that R3 forbids and that was removed from this tree once already, buys a
join `_join` already makes from the accession and the declared filename, and checks a hash the
submitter computed on a copy that may since have been recompressed. `ArchiveRecord` instead carries
the whole transcript entry — name, provider md5, size, URI — as data: the md5 is an address over the
bytes at that URI, the size checks a filename join but never creates one, and the URI is printed by
`io records`, which refusals name rather than threading a record set into the byte resolver, where
records deliberately never arrive. SRA's own element was not modelled: it would be one archive's XML
in a model's clothes, unable to hold ENA's or an in-house deposit's spelling of the same four facts.
