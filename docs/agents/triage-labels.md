# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those roles to the actual label strings used in this repo's issue tracker.

Each label string **equals its role name** — the mapping is the identity, deliberately, so there is no
translation step to get wrong:

| Label             | Meaning                                  |
| ----------------- | ---------------------------------------- |
| `needs-triage`    | Maintainer needs to evaluate this issue  |
| `needs-info`      | Waiting on reporter for more information |
| `ready-for-agent` | Fully specified, ready for an AFK agent  |
| `ready-for-human` | Requires human implementation            |
| `wontfix`         | Will not be actioned                     |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the label string from this table.

## Repo note

All five labels exist on `liuhlab/seqforge`. `wontfix` is GitHub's stock label and predates this
setup; the other four were created for these skills. The repo's other stock labels (`bug`,
`enhancement`, `documentation`, …) are orthogonal — triage does not read or write them.

**`/triage` is for issues you did not create.** Tickets that `/to-tickets` emits are already
agent-ready and skip triage entirely.
