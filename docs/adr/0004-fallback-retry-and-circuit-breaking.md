# ADR 0004: Fallback chains, classified retries and per-model circuit breakers

Status: accepted

## Context

Providers have outages, rate limits and slow periods. Without care, a gateway can make things worse in three ways:
- retrying requests that can never succeed
- hammering a provider that is down
- making every caller wait through a full timeout before trying anything else

## Options

1. Retry every failure a fixed number of times, then give up.
2. Retry, then fall back to another model, with every failure treated alike.
3. Classify failures:
   - retryable: rate limits, timeouts, server errors
   - not retryable: bad request, refusal

   Retry only the retryable ones, a bounded number of times per model, then fall back along the route. Open a per-model circuit breaker after repeated retryable failures, so later requests skip that model until a cool-down passes.

## Decision

Option 3. Each route is an ordered chain, for example `smart: [claude-opus, demo-large, claude-sonnet]`. A non-retryable error moves straight to the next model. A retryable one is retried up to `GW_MAX_ATTEMPTS` (default 2), and each failure counts towards the breaker. After 3 consecutive failures, a model is skipped for 10 seconds. One trial request is then allowed through: success closes the breaker, failure re-opens it. Every response lists its attempts, so callers and the ledger can see what happened.

## Consequences

Measured with fault injection on 50 requests per case:

| Case | Behaviour |
| --- | --- |
| Primary outage | 50/50 served by the fallback; the primary was called only 3 times before its breaker opened, not 100 |
| Primary rejects the request | 50/50 served by the fallback, with no pointless retries |
| Primary flaky (40% errors) | 50/50 served (49 by the primary after a retry, 1 by the fallback) |
| Whole route down | 50/50 fail cleanly with HTTP 502 and nothing billed |

- Accepted cost: the breaker is per process. With many gateway replicas, each learns about an outage separately. Sharing breaker state in Redis is the production version.
