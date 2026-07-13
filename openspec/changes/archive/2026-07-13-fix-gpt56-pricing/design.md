## Context

The shared pricing registry resolves unrecognized GPT-5-family names through
the broad `gpt-5*` alias. GPT-5.6 personality models are already accepted by
the model catalog and proxy, so missing pricing entries affect both persisted
request-log costs and API-key cost reservations and settlement.

## Decisions

### Add one canonical price per personality model

`gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna` receive independent
`ModelPrice` entries. Each entry includes the published Standard, Flex,
Priority, and greater-than-272K-context rates. The existing cost helper already
applies the published long-context multipliers to Flex and keeps explicit
Priority rates independent of the long-context branch.

### Resolve qualified names through explicit wildcard aliases

Each personality slug receives a wildcard alias. The resolver chooses the
longest matching pattern, so qualified labels and snapshots resolve to their
personality price before the generic `gpt-5*` fallback without changing the
resolver algorithm.

### Defer cache-write accounting

OpenAI publishes cache-write rates for GPT-5.6, but `ResponseUsage`,
`UsageTokens`, request logs, and API-key counters currently expose only input,
cached-input, and output tokens. Reusing cached-input tokens for cache writes
would misprice cache reads. Cache-write support therefore requires a separate
usage-model change and is intentionally excluded from this fix.

## Verification

- pricing tests cover canonical resolution and every published tier represented
  by the current usage model
- request-log persistence proves qualified aliases use GPT-5.6 Priority rates
- API-key tests prove qualified-alias reservation and long-context Flex
  settlement
- strict OpenSpec validation and repository lint/type checks guard integration
