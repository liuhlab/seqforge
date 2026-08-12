# What the umite counter and extractor actually cost, before and after

Measured from 2026-08-11, one section per change in the performance series
([#352](https://github.com/liuhlab/seqforge/issues/352)). **Every change in the series is
count-neutral**: the same plate in, the same `.h5ad` bytes out. Nothing here trades accuracy for
speed, so a moved number is a bug and not a result.

These are measurements, not decisions. Each section states the machine, the input and the method
that produced its numbers; what any of them *decided* lives in the issue that took the decision.

> Laptop numbers are ratios, not budgets. The absolute microseconds below come from an Apple
> Silicon laptop under Python 3.13 on one core unless a section says otherwise, and the cluster
> re-measurement lives in its own section at the end.

## UMI correction: a neighbour index against a quadratic scan

<!-- filled by #393 -->

## The annotation lookup: `bisect` over `array.array` against `np.searchsorted`

<!-- filled by #394 -->

## Building an unaligned record: `fromstring` against attribute-by-attribute

<!-- filled by #395 -->

## The one FASTQ loop: where `BoundedReader`'s microseconds go

<!-- filled by #396 -->

## The fan-in: counting a plate on every core the rule asked for

<!-- filled by #397 -->

## On the cluster, on a real plate

<!-- filled by #398 -->
