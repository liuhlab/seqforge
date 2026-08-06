# 9. The LLM provider is pluggable *because* span verification makes it swappable

Locking to a vendor whose structured output is guaranteed would make a shape promise part of the
correctness story, but code re-greps every quote and validates the whole batch afterwards anyway
(ADR-0008), so the guarantee buys nothing and the provider becomes a cost lever instead. One prompt
therefore serves every provider — branching it would make `prompt_version`, and every eval number,
provider-local — a malformed response fails the batch whole rather than half-parsing, and a run
whose credentials name no provider refuses instead of falling back, because extracting under an
unintended model is a provenance bug that looks like success.
