# Agentic Project Startup and Resumption Checklist

Use this checklist for complex work governed by `docs/agentic-workflow.md`.

## Start or resume

- Read `AGENTS.md` and every applicable nested instruction file.
- Read `docs/agentic-workflow.md` completely.
- Locate the active durable project artifact or create one from
  `docs/agentic-project-template.md` after authorization.
- Read every security, extension, platform, or task reference directly required
  by the objective.
- Inspect workspace state and preserve unrelated user changes.
- Identify the current graph phase; do not repeat completed work or cross a
  pending gate.
- Confirm the approval profile and autonomy envelope.

## Intake

- Restate the concrete outcome and definition of done.
- Separate observed facts, inferences, hypotheses, and verified claims.
- Identify user-visible behavior, non-goals, production entry points, and
  integration-test obligations.
- Identify credentials, external data, dependencies, destructive actions, and
  other mandatory gates.
- Decide whether the task needs a full specification graph or is a bounded
  change already authorized by the request.

## Before a research or QC wave

- Report every node, scope, read/write authority, expected join, and approval
  gate.
- Give every delegated node the complete contract required by the workflow.
- Keep parallel work read-only unless one explicit writer owns the workspace.
- Give external reviewers only the approved bounded packet.

## Before writing

- Confirm that the specification and current slice are approved.
- Inspect existing changes in every expected file.
- Declare the single writer and file ownership.
- Write a focused failing test tied to an approved acceptance criterion.
- Verify RED for the intended reason before implementation.

## During gated-autonomy execution

- Report each logical unit and its evidence without pausing mechanically.
- Continue only while behavior, architecture, security, data, dependencies, and
  acceptance criteria remain inside the autonomy envelope.
- Record internal plan adjustments and their falsification evidence.
- Stop before material amendments or any mandatory gate.
- Never weaken a test to obtain GREEN.
- Do not fix unrelated findings.
- Keep only one writer active.

## Verification and QC

- Verify focused GREEN through the production-relevant path.
- Run independent Codex and Claude QC required by the graph.
- Adjudicate every substantive finding using evidence.
- Route verified in-scope findings through the correction loop.
- Escalate findings that require a material amendment.
- Run broader checks proportional to risk.
- Distinguish related, unrelated, and uncertain failures.

## Interruption or usage cutoff

- Preserve the workspace; do not discard partial work.
- Record the last completed node, active writer, pending gate, verification
  state, and exact next transition in the durable artifact.
- Do not describe durable resumption state as an automatic wake-up mechanism.
  Without an explicitly started Codex goal, another user or product trigger is
  required after the usage window resets.
- On resumption, reread instructions and the artifact, inspect workspace and
  agent state, and verify partial work before continuing.
- Treat an interrupted external request as indeterminate unless a complete,
  valid result is available. Do not retry without the required authority.

## Completion

- Name the production call site and integration test proving it is wired.
- Report focused and broader checks, QC dispositions, unrelated failures, and
  limitations.
- Confirm every acceptance criterion and required logical unit is complete.
- Stop for final user acceptance.
