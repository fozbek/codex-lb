## Why

GPT-5.6 Sol, Terra, and Luna are available through `codex-lb`, but the shared
pricing registry does not recognize their model IDs. Their request-log and
API-key quota costs therefore resolve through the broad `gpt-5*` fallback and
substantially underreport usage.

## What Changes

- add canonical GPT-5.6 Sol, Terra, and Luna pricing using OpenAI's published
  standard, Flex, Priority, and long-context rates
- add wildcard aliases so qualified and snapshot GPT-5.6 personality slugs
  resolve to the correct canonical pricing entry instead of `gpt-5`
- add regression coverage for pricing resolution, request-log costs, and
  API-key cost accounting
- keep cache-write accounting out of scope because the current usage model
  does not expose cache-write tokens separately

## Capabilities

### New Capabilities

- none

### Modified Capabilities

- `api-keys`: cost accounting recognizes the three GPT-5.6 personality models
  and their tier-specific and long-context prices

## Impact

- Code: `app/core/usage/pricing.py`
- Tests: pricing, request-log persistence, and API-key usage accounting
- Specs: `api-keys` pricing recognition via the change delta
- No API or database schema changes
