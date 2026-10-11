# ADR 0001: One normalised request and response schema instead of passing provider payloads through

Status: accepted

## Context

Internal apps want to use Claude, GPT and Gemini without each app learning three APIs, three error models and three ways of counting tokens. A thin proxy that forwards provider payloads unchanged is quick to build, but then every app is still coupled to every provider. Switching models becomes a code change in every app, and the gateway cannot apply policy, cost or fallback, because it does not understand the requests.

## Options

1. Transparent proxy: pass provider-specific payloads through, add auth and logging.
2. Adopt one provider's API shape as the lingua franca.
3. A small normalised schema of our own:
   - request: route or model, messages, system, max tokens, optional prompt reference
   - response: content, usage and cost, model actually used, fallback flag, attempt list

   Each provider has an adapter with two pure translation functions.

## Decision

Option 3. Apps ask for a route (`smart`, `fast`, `cheap`) or a catalogue name, never a provider model id. Adapters translate the request to the provider payload and the provider response back to the result. Those translations are pure functions, tested against recorded provider responses. The transport around them is thin (official SDKs for Anthropic and OpenAI, REST for Gemini).

## Consequences

- Moving a route to a different model is a one-line config change; no app redeploys.
- Fallback across providers is possible, because every provider speaks the same schema to the gateway.
- Cost and token accounting is uniform, so budgets work across providers.
- Accepted cost: provider-specific features (tool use, images, structured outputs, streaming) need explicit schema support before apps can use them through the gateway. That is deliberate: each one is added once, with tests, rather than leaking into every app.
