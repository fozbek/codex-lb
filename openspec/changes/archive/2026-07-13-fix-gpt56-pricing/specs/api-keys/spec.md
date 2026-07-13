## ADDED Requirements

### Requirement: GPT-5.6 personality pricing is recognized

The system MUST recognize `gpt-5.6-sol`, `gpt-5.6-terra`, and
`gpt-5.6-luna` when computing request-log costs and API-key `cost_usd`
accounting. Qualified aliases for each personality model MUST resolve to that
model's canonical price table entry instead of the generic `gpt-5` entry.

#### Scenario: GPT-5.6 qualified alias uses canonical pricing

- **WHEN** a request uses a qualified GPT-5.6 personality slug such as `gpt-5.6-sol-xhigh-fast`
- **THEN** pricing resolves to the canonical `gpt-5.6-sol` entry
- **AND** the generic `gpt-5` fallback is not used

#### Scenario: GPT-5.6 service tier uses published rates

- **WHEN** a GPT-5.6 personality request completes with standard, Flex, or Priority service tier
- **THEN** request-log and API-key costs use the published rate for that model and effective service tier

#### Scenario: GPT-5.6 long-context request uses published rates

- **WHEN** a standard-tier or Flex-tier GPT-5.6 personality request contains more than 272,000 input tokens
- **THEN** request-log and API-key costs use the published long-context input, cached-input, and output rates
