#!/usr/bin/env bash
# `pixi run check` — the pre-PR gate, with its six steps run CONCURRENTLY.
#
# It used to be `{ depends-on = ["lint", "fmt-check", "typecheck", "test"] }`, which pixi executes
# sequentially: the three static checks (~6.8s together) ran before pytest and nothing overlapped.
# That was invisible while the suite took 164s. Once xdist brought it to ~15s, the serial preamble
# was 6.8s of a 23s gate — a third of it, spent waiting on ruff and mypy that share no inputs with
# each other or with pytest.
#
# CI is unaffected: `.github/workflows/ci.yml` calls the individual tasks and never this script. It
# runs `lint`, `fmt-check` and `typecheck` as three STEPS of one job (with the docs build), and gives
# each of the three test lanes a job of its own — so the concurrency this script buys is a local-only
# win, and CI's shape is more runners rather than more background jobs. What the two DO share is the
# selections: the lanes named on the `check` line are the ones CI's jobs run, so a lane that is red
# here is red there and for the same reason.
#
# Each step's output is captured to its own file and printed whole, in a fixed order, after every
# step has finished. Attribution therefore gets BETTER, not worse: a failure is one contiguous
# labelled block instead of lines from six tools interleaved by whoever flushed first.
#
# Steps are invoked as `pixi run <task>` rather than by spelling their command lines again here.
# The task table stays the one owner of what each step actually runs — duplicating mypy's module
# list into this script is exactly the kind of second copy that drifts.
#
# Takes the steps to run as arguments rather than hard-coding them, so any gate can share this
# runner. That generality earned itself immediately: `check-fast` was left as a serial `depends-on`
# when `check` was parallelised, measured 19.1s against a parallel `check`'s 17.1s -- the cheap rung
# costing MORE than the expensive one -- and running it here too showed it saved nothing at all.
# It was deleted; nothing sits between the targeted run and this one, which is why the ladder in
# AGENTS.md goes from a selector to the whole gate with no cheap middle.
#
# Nothing here may use an associative array. macOS ships bash 3.2 as /bin/bash and 3.2 has none, so
# `declare -A status` failed there -- and `set -e` is deliberately absent below, because the runner
# must collect EVERY step's status before it reports, so the failed declaration did not stop the
# script. It ran on into the collect loop, where bash evaluated the subscript arithmetically, `set -u`
# tripped on the unset name, and the shell exited 0 having verified nothing and printed no verdict.
# The gate reported green on every macOS host. Nothing needed a map: STEPS is an ordered list and the
# verdicts are only ever read back in STEPS order, so a plain indexed array parallel to it says
# everything, on every shell.
#
# `-m` puts each step in its OWN process group, which is what makes the cleanup below able to reach
# past `pixi` to the pytest run underneath it.
set -muo pipefail

STEPS=("$@")
[ ${#STEPS[@]} -gt 0 ] || { echo "usage: check.sh <task>..." >&2; exit 2; }

out=$(mktemp -d)
# Kill, then delete -- in that order, and never one without the other. The steps write into $out, so
# removing it while any of them is alive leaves live processes writing at a path that is gone; that
# is what an early death used to do, and the orphaned pytest runs had to be killed by hand. Each
# leftover job is a process-group leader, so the negated pid takes its children with it.
cleanup() {
    local leftover
    leftover=$(jobs -p)
    # shellcheck disable=SC2086  # jobs -p emits one bare pid per line, and none may be quoted as one
    for pgid in $leftover; do kill -- "-$pgid" 2>/dev/null; done
    wait 2>/dev/null
    rm -rf "$out"
}
trap cleanup EXIT
# A signal kills the shell without running the EXIT trap, so turn the two that reach an abandoned
# gate into an ordinary exit. Ctrl-C used to leave the whole gate running behind it.
trap 'exit 130' INT
trap 'exit 143' TERM

start=$SECONDS
pids=()
for step in "${STEPS[@]}"; do
    # shellcheck disable=SC2086  # each step is a single bare task name, never a word-split command
    pixi run --no-progress $step >"$out/$step.log" 2>&1 &
    pids+=("$!")
done

rc=0
# Indexed by position, parallel to STEPS -- see the note above on why this is not a map.
status=()
for i in "${!STEPS[@]}"; do
    if wait "${pids[$i]}"; then
        status[$i]="ok"
    else
        status[$i]="FAILED"
        rc=1
    fi
done

for i in "${!STEPS[@]}"; do
    printf '\n\033[1m=== %s: %s ===\033[0m\n' "${STEPS[$i]}" "${status[$i]}"
    # A green step's output is noise; a red one's is the whole point.
    if [ "${status[$i]}" = "ok" ]; then
        tail -n 3 "$out/${STEPS[$i]}.log"
    else
        cat "$out/${STEPS[$i]}.log"
    fi
done

# Labelled "gate", not "check": the same runner serves whatever step list it is handed, and the
# per-step verdicts below already name which selections ran.
printf '\n\033[1m=== gate: '
for i in "${!STEPS[@]}"; do printf '%s=%s ' "${STEPS[$i]}" "${status[$i]}"; done
printf 'in %ss ===\033[0m\n' "$((SECONDS - start))"
exit "$rc"
