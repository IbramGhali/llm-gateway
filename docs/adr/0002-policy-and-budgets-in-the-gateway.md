# ADR 0002: Policy and budgets are enforced in the gateway, with a reservation, not in each app

Status: accepted

## Context

Teams have different needs and different budgets. A support bot should use a fast, cheap model; an analytics notebook may use the most capable one. If each app enforces its own limits, limits are inconsistent, easy to bypass and invisible to whoever owns the bill. Budgets checked after the fact let a burst of concurrent requests overspend before anyone notices.

## Options

1. Each app enforces its own model choice and spend.
2. The gateway checks spend after each call and blocks once over budget.
3. The gateway checks every request before any provider call:
   - the team's allow-list
   - a worst-case cost reservation against the monthly budget

   It then settles the reservation to the actual cost.

## Decision

Option 3. Each app key belongs to a team. A team in `config/gateway.yaml` lists the routes and models it may use and a monthly budget in USD. A model without a price cannot be enabled, because its spend could not be bounded.

Before a request is routed, the gateway estimates the worst case: input tokens estimated from the prompt, plus `max_tokens` of output, priced at the most expensive model in the route. It reserves that amount with a single conditional update: `spent + reserved + estimate <= budget`.

- **Reservation fails:** the request is refused with HTTP 402 and no provider is called.
- **Request succeeds:** the reservation is replaced by the actual cost.
- **Every model in the route fails:** the reservation is released and nothing is billed.

## Consequences

- 200 concurrent requests against a $0.05 budget: 115 served, 85 refused, $0.0414 spent. No refused request reached a provider. The same test passes on Postgres with 24 threads and no in-process lock.
- Refusals and policy violations appear in the ledger and the admin page, so owners see demand they are blocking.
- Accepted cost: the worst-case reservation is pessimistic. A team close to its limit is refused slightly before the last cent is spent. Lower `max_tokens` reduces the reservation.
