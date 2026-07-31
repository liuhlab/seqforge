#!/usr/bin/env bash
# `pixi run check` — the pre-PR gate, with its four steps run CONCURRENTLY.
#
# It used to be `{ depends-on = ["lint", "fmt-check", "typecheck", "test"] }`, which pixi executes
# sequentially: the three static checks (~6.8s together) ran before pytest and nothing overlapped.
# That was invisible while the suite took 164s. Once xdist brought it to ~15s, the serial preamble
# was 6.8s of a 23s gate — a third of it, spent waiting on ruff and mypy that share no inputs with
# each other or with pytest.
#
# CI is unaffected: `.github/workflows/ci.yml` invokes `lint`, `fmt-check`, `typecheck` and `test`
# as separate JOBS on separate runners, so it already had this concurrency and calls the individual
# tasks, never this script.
#
# Each step's output is captured to its own file and printed whole, in a fixed order, after every
# step has finished. Attribution therefore gets BETTER, not worse: a failure is one contiguous
# labelled block instead of lines from four tools interleaved by whoever flushed first.
#
# Steps are invoked as `pixi run <task>` rather than by spelling their command lines again here.
# The task table stays the one owner of what each step actually runs — duplicating mypy's module
# list into this script is exactly the kind of second copy that drifts.
#
# Takes the steps to run as arguments, so rung 2 (`check-fast`) and rung 3 (`check`) share one
# runner. They must: while `check-fast` was still a serial `depends-on` it measured 19.1s against a
# parallel `check`'s 17.1s, i.e. the cheaper rung of the ladder cost MORE than the expensive one.
set -uo pipefail

STEPS=("$@")
[ ${#STEPS[@]} -gt 0 ] || { echo "usage: check.sh <task>..." >&2; exit 2; }

out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT

start=$SECONDS
for step in "${STEPS[@]}"; do
    # shellcheck disable=SC2086  # each step is a single bare task name, never a word-split command
    pixi run --no-progress $step >"$out/$step.log" 2>&1 &
    echo $! >"$out/$step.pid"
done

rc=0
declare -A status
for step in "${STEPS[@]}"; do
    if wait "$(cat "$out/$step.pid")"; then
        status[$step]="ok"
    else
        status[$step]="FAILED"
        rc=1
    fi
done

for step in "${STEPS[@]}"; do
    printf '\n\033[1m=== %s: %s ===\033[0m\n' "$step" "${status[$step]}"
    # A green step's output is noise; a red one's is the whole point.
    if [ "${status[$step]}" = "ok" ]; then
        tail -n 3 "$out/$step.log"
    else
        cat "$out/$step.log"
    fi
done

# Labelled "gate", not "check": the same runner serves rung 2 and rung 3, and the per-step verdicts
# below already say which rung ran.
printf '\n\033[1m=== gate: '
for step in "${STEPS[@]}"; do printf '%s=%s ' "$step" "${status[$step]}"; done
printf 'in %ss ===\033[0m\n' "$((SECONDS - start))"
exit "$rc"
