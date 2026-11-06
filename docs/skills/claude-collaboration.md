# Claude Collaboration

Use the installed Claude Code CLI directly, in a fresh independent session, with
read-only access to repository source. Do not add a project wrapper, source
packets, digest approvals, custom schemas or response-validation infrastructure.
Codex owns implementation and makes final decisions on Claude's findings.

## Workflow

1. Read the active objective and inspect the relevant source/diff.
2. Supply a short brief: objective, relevant paths, questions and test results.
   Claude reads related source/tests itself; do not copy source into Markdown or
   JSON packets. A focused diff is optional.
3. Invoke `claude` directly from the repository root using the native read-only
   command in [the integration notes](../claude-qc-integration.md). Use the user's
   first-party sign-in, exact `claude-opus-5-5` and high effort. Never substitute a
   fallback model or reuse Codex's implementation session as the independent review.
4. Inspect native completion/error metadata, actual model usage, permission
   denials and the answer. A failed or incomplete required review blocks further
   implementation until resolved and completed. Diagnose before retrying; never
   skip the gate or substitute an internal review.
5. Adjudicate findings against source and falsifiable checks. Fix supported
   issues; reject or defer unsupported/out-of-scope suggestions with reasons.
6. Continue approved work. Keep only the concise state needed while work is
   active. Remove temporary review material after acceptance unless retention
   is explicitly requested; do not create an archive by default.

Standing authorization covers routine direct reviews of approved work. Do not
repeat consent notices or ask for per-review packet approval. Stop for actual
scope/product/security/data changes, unavailable required reviewers or final
acceptance. Read-only access excludes credentials, private databases and live
provider data; managed machine policy remains the documented native limitation.

## Efficiency

- One external request at a time. Do not repeat completed reviews.
- Follow up only on substantive new changes or unresolved findings; verify
  clear wording/evidence corrections locally.
- Use cheaper subagents for suitable bounded work, narrow contexts and one
  writer. Codex integrates their work; Claude remains the independent reviewer.
- Report meaningful outcomes and remaining decisions, not raw transcripts.

No additional external review of these workflow instructions is required.
