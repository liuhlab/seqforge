# 1. A probe joins a head to a whole file; there is no read-source seam

A review read the four callers that each assemble a head plus a file identity longhand as a missing
`ReadSource` abstraction, and it lost three ways: the adapter cannot live in `probe/` without
forfeiting the stdlib-only foundation `fingerprint`, `io` and `resolve` all depend on; one
`BoundedReader` already removed the read variance it would abstract over; and
`probed_from_fingerprint` reads a slice in order to describe a different file. The naming knowledge
is irreducibly distributed, so four sources keep four naming authorities and share a type instead —
which is also why `isize` never joins `FileIdentity`.
