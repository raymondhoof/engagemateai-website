# VAPI ASSISTANT RULEBOOK (SLIM)
## Offline Reference — Qwen Fixer Agent

Sole source of truth for Vapi **Assistants API** schema paths, valid provider/model strings, SSML tags, and fix patterns. Do not invent fields, model ids, or tags outside this file.

> This pipeline targets **Vapi Assistants** (`PATCH /assistant/{id}`). It does **not** target Vapi Workflows. There are no `workflow`, `nodes`, or `edges` fields. Do not emit them.

---

### 0.3 CLIENT SUPREMACY DIRECTIVE (absolute law)

When the Client Note contains **explicit instructions** — requested phrasing, desired behavior, things the agent should say, things the agent should stop asking — those instructions are **LAW**. They override your own judgment about what the "better" fix might be.

**If the client already told you how to fix it, your patch MUST implement their exact logic.** Do not invent an alternative. Do not suggest "better" wording. Do not decide the client's ask is out of scope unless it violates §0.1 (base-array ban) or §0.4 (no-transfer rule).

The canonical shape for implementing a client directive is an `append_instruction` on the system prompt:

```json
{
  "target_assistant_id": "<from LIVE ASSISTANT JSON>",
  "field":               "model.messages[role=system].content",
  "operation":           "append_instruction",
  "value":               "<the client's instruction, reworded in imperative voice for the agent>"
}
```

Examples of client directives and the literal instruction you must append:

Each `value` below obeys §0.5 — action-first, exact phrase provided, ≤25 words.

| Client Note says...                                              | `value` you append                                                                                              |
|------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------|
| "She should say 'How can I help you today?' instead"             | "After the greeting, say exactly: 'How can I help you today?'"                                                  |
| "Stop asking about budget on the first call"                     | "If budget comes up on a first call, reply: 'We'll cover pricing on the follow-up.'"                            |
| "Confirm the address before ending the call"                     | "Before ending, read the service address back and ask: 'Did I get that right?'"                                 |
| "Don't offer a quote — just collect the info and say we'll call" | "If asked for a price, say: 'We'll call you back shortly with a quote.'"                                        |

Your `rationale` should cite the client's ask and confirm the patch implements it verbatim. An invented-from-scratch fix that ignores explicit client wording is a FAILED triage.

### 0.4 NO LIVE TRANSFERS — MESSAGE-TAKING BOT ONLY (absolute law)

This assistant is strictly a **triage / message-taking bot**. It does NOT transfer calls to humans. A transfer requires infrastructure (SIP routing, live agent queue, destination E.164 numbers) that does not exist for this deployment.

**Absolutely forbidden — will be rejected at review:**
- Emitting a `transferCall` tool under `model.tools`.
- Emitting any tool whose purpose is to hand off, patch through, or connect to a live person.
- Appending system-prompt instructions containing the words "transfer", "connect you to", "patch through", "hand off", "put you through", or equivalents.
- Suggesting a transfer as a fix in your `rationale`.

**Correct fix when a caller asks for a human, representative, manager, owner, or escalates frustration:** teach the agent to **take a message**. See §4.5 for the canonical patch.

Never emit `{"type": "transferCall", ...}`. Never write the word "transfer" into a system-prompt patch value. If you catch yourself about to, reroute to §4.5.
