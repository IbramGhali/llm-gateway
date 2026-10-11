# ADR 0003: Redact before log, always

Status: accepted

## Context

Prompts contain whatever users type: email addresses, phone numbers, card numbers, tax identifiers. A gateway that logs raw prompts for debugging becomes the largest store of personal data in the organisation, with weaker controls than the systems the data came from.

## Options

1. Log raw prompts and restrict access to the logs.
2. Do not log prompts at all.
3. Redact every prompt before anything is written, keep only a short redacted preview and counts of what was found, and optionally redact prompts before they reach the provider, per team.

## Decision

Option 3. Detectors cover:
- email addresses
- card numbers (Luhn-checked, so order numbers survive)
- IBANs
- nine-digit tax identifiers
- phone numbers with a country, area or mobile prefix
- IP addresses

The ledger stores a redacted, truncated preview and a count of each kind of PII found, never the raw prompt or completion. Teams with `redact_prompts: true` also have their prompts redacted before the provider sees them.

## Consequences

- On a synthetic set of 200 prompts with 400 planted values across seven kinds, all 200 prompts are fully redacted, and **zero** planted values appear anywhere in the persisted ledger. Dollar amounts are never removed.
- Debugging uses the request id, the redacted preview, the attempts and the PII counts. That is enough to trace routing and cost problems without seeing content.
- A test caught an early phone detector that also masked order numbers. Phone numbers now need a prefix, and order numbers survive.
- Accepted cost: regex detectors miss PII they were not designed for, such as names and street addresses. That is why storage keeps only a short preview, and why a named-entity model is on the roadmap rather than claimed.
