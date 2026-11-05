# Claude Collaboration Skill

Use SimpleChart's Claude-QC wrapper as an external, read-only review node. Read
`AGENTS.md`, `docs/agentic-workflow.md`, `docs/claude-qc-integration.md`, and the
active durable project artifact before acting.

The wrapper is source-only development tooling under `devtools/claude_qc/`.
Run it from the repository root as `python -m devtools.claude_qc.cli`; it must
not be imported by, installed with, or exposed as an executable from the
SimpleChart application package.

## Preserve the review boundary

- Send only the exact packet and digest the user approved.
- Do not repeat a completed review unless new evidence invalidates it.
- Keep one external request active at a time. Never start a parallel or
  speculative retry.
- Describe packet contents, review scope, and usage consequences in ordinary
  language before transmission.

## Attribute failures from evidence

Treat Claude Code, the local wrapper, the sandbox/network path, and Claude's
model response as separate components. Do not say Claude failed to answer merely
because the wrapper returned an error.

- JSON mode has no supported early acknowledgement. `starting` means only that
  the local wrapper began the operation. `final_response` means Claude Code
  supplied a result object and local validation began.
- A failure after `final_response` is a local validation rejection unless a
  documented result invariant proves a provider or model failure.
- DNS, TLS, or connection failures inside the sandbox must be tested through
  the approved outside-sandbox path before attributing them to Claude or a
  certificate authority.

Label diagnostic claims as **Observed**, **Inferred**, **Hypothesis**, or
**Verified**. Never promote a hypothesis because it sounds plausible.

## Prevent blind retries

After a failed request:

1. Capture the complete sanitized category, stage, and validation reason.
2. If that information is insufficient, improve local diagnostics and reproduce
   with synthetic result objects before making another external call.
3. Retry the unchanged approved packet only after identifying one falsifiable
   cause, correcting it, and passing a regression test that exercises the same
   layer.
4. If the retry falsifies that cause, stop external calls. Retrace from first
   principles and follow the workflow gate: ask the user whether to wait, waive,
   or substitute the unavailable reviewer.

Never loosen authorization, tool, model, credential, or structured-output
validation merely to make a response pass. Ignore unknown result fields, but
remain strict on the documented fields used to decide success, model identity,
permission denial, and structured findings.

## Keep the transport stable

- Use `claude -p --output-format json --json-schema ...` for single-turn reviews.
- Do not parse `stream-json` on the review path or depend on internal event
  order, `stop_reason`, or `num_turns`.
- Supply the packet only through stdin. Enforce isolation through the locked
  command, scrubbed environment, disabled capabilities, and empty working
  directory.
- Read only the required authentication fields from `claude auth status --json`;
  ignore unknown fields and reject duplicate required fields.
- Use the full model identifier and require positive output-token usage from
  that model with no fallback model.
- Use only official Anthropic and OpenAI documentation when changing this
  workflow. Never install or copy unknown collaboration skills or wrappers.

## Communicate clearly

Use human descriptions such as “the local validator rejected Claude's final
result.” Avoid internal shorthand such as “packet validation passed”
without explaining what that means. During a long review, report that the same
request is still running and explicitly state when no retry has occurred.

At completion, distinguish:

- Claude's actual findings;
- local wrapper or network failures;
- deterministic test evidence;
- any remaining user approval gate.
