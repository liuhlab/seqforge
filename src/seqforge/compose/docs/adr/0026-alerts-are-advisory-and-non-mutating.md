# 26. An alert is advisory — the first backward edge writes nothing and changes no exit code

Joining what the compiler decided to what a finished pipeline measured is the only place evidence
flows backwards, and acting on it is unsound: a threshold comparison cannot say which of the
decisions it implicates is wrong, and letting an aligner's summary rewrite `library.chemistry` would
make a dataset's identity a function of the last pipeline anyone ran over it. A user who agrees
wants a second recipe — plural and cheap already — not a mutated manifest, a `--fix` flag, or a
suggested sidecar sitting where inputs live with nothing authoritative behind it. Filing an alert as
a `Warning` would put a judgement about a run inside the compile's record, where the next `validate`
legitimately would not reproduce it, and an exit code would fail a sweep across 10⁴ datasets on
advice about data that is usable.
