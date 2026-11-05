# Claude QC Development Tool

Status: Operational

## Purpose

SimpleChart uses Claude as an external, read-only reviewer for explicitly
approved agentic work. The wrapper makes those reviews reproducible and keeps
Claude outside the application and its credentials.

The wrapper is development tooling, not application runtime code. It lives at
`devtools/claude_qc/`, is excluded from application package discovery, and has
no installed console entry point.

Run it from the repository root:

```text
python -m devtools.claude_qc.cli status
python -m devtools.claude_qc.cli preview < packet.json
python -m devtools.claude_qc.cli review --approved-digest DIGEST < packet.json
```

## Review boundary

- Only a bounded packet approved under `docs/agentic-workflow.md` may be sent.
- The canonical packet digest must match the approved digest before Claude is
  started.
- Packets travel through standard input, never command arguments.
- Credentials, private databases, raw provider responses, unrelated source,
  and environment dumps are prohibited.
- Claude receives no application tools or workspace access.
- A completed review is not repeated without new falsifying evidence.

## Claude invocation

The wrapper uses Claude Code's single-result headless interface with the full
model identifier `claude-opus-5-5`, high effort, JSON output, and a fixed
structured-output schema.

The invocation disables ordinary tools, MCP servers, slash commands, Chrome,
session persistence, hooks, plugins, project instructions, and interactive
permission prompts. It runs in a new empty temporary directory with a scrubbed
environment and no wrapper retry.

Subscription authentication remains owned by Claude Code. The preflight
accepts only a logged-in first-party `claude.ai` Pro or Max subscription. It
reads only the authentication fields used for that decision and ignores
unknown fields.

## Result validation

The wrapper parses one JSON result object and requires:

1. `type == "result"` and `subtype == "success"`.
2. `is_error == false`.
3. No permission denials.
4. Locally valid structured findings.
5. Positive output-token usage from only the requested full model identifier.
6. A successful process exit and an unchanged empty working directory.

Unknown result fields are ignored. Internal event ordering, `stop_reason`, and
`num_turns` are not treated as stable protocol fields.

Raw Claude output is normalized in memory and discarded. The command returns
only the review status, safe metadata, and validated findings.

## Failure handling

Claude Code, the local wrapper, the sandbox/network path, and Claude's response
are separate failure domains. Report which domain actually failed.

There is no blind retry. After a failed request:

1. Record the sanitized stage, category, and validation reason.
2. Reproduce parser or wrapper defects with synthetic fixtures.
3. Correct and test one falsifiable cause.
4. Retry the unchanged packet only when the workflow permits it.

DNS or TLS failures inside the sandbox must be checked through the approved
outside-sandbox path before attributing them to Claude or a certificate
authority.

## Repository verification

The focused tests are `tests/test_claude_qc_*.py`. They cover packet
canonicalization, authentication preflight, environment isolation, process
control, result parsing, command behavior, and packaging exclusion.

`tests/test_claude_qc_packaging.py` proves both sides of the boundary:

- the module runs from a source checkout without importing application layers;
- an isolated SimpleChart installation contains neither the development tool
  nor a Claude-QC executable.

Read `docs/skills/claude-collaboration.md` before preparing, sending,
monitoring, retrying, or diagnosing a review.
