#!/usr/bin/env bash
# `pixi run test-external` runs this FIRST, and the whole point is that it runs first.
#
# The external lane needs binaries this project does not own. They reach the suite on PATH from the
# `test-star` environment, declared once on the `test` feature so every way of invoking the suite
# inherits it. If that environment was never installed, the PATH entry names a directory that does
# not exist — which PATH tolerates in silence. The gated tests then skip themselves, the lane exits
# 0, and a green report means nothing ran. That is exactly the failure the lane was built to end: for
# the life of the repo the STAR-gated tests ran on no host CI could reach, and nothing was ever red
# about it (#333).
#
# So the binaries are proved to ANSWER rather than merely to resolve. `which` says a file is on the
# path; `--version` says it executes — and on a machine that half-installed an environment, or one
# whose STAR cannot run at all, those are different answers. CI proves the same thing before it
# selects, for the same reason.
#
# Nothing here may use an empty array: macOS ships bash 3.2 as /bin/bash, where expanding one under
# `set -u` is an unbound-variable error, and this script would then fail for a reason that has
# nothing to do with the binaries. A space-joined string says everything a list needs to say here.
#
# `set -e` is absent deliberately — a probe that fails must be RECORDED, not fatal, so the message
# below can name every missing binary at once instead of one per run.
set -uo pipefail

missing=""
for binary in STAR samtools bgzip tabix; do
    if ! "$binary" --version >/dev/null 2>&1; then
        missing="$missing $binary"
    fi
done

if [ -n "$missing" ]; then
    cat >&2 <<EOF
external lane: these binaries do not answer:$missing

They come from \`test-star\`, an environment THIS repo owns and that exists only to hold them:

    pixi install -e test-star

Or run the suite without them, which is what that verb is for:

    pixi run -e test test-fast

Refusing to select. A missing binary here is not a smaller test run — it is a green one that
checked nothing, which is the failure this lane exists to end.
EOF
    exit 1
fi
