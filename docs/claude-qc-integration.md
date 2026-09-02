# Claude QC Integration Specification

Status: Operational — live fixture and first project design review complete
Version: 0.7
Approved: 2026-09-01
Authority: Approved infrastructure specification under `docs/agentic-workflow.md`

## 1. Graph state and resumption

The specification graph completed its G2 approval gate. On 2026-09-01 the user
approved the TDD implementation graph under the repository's gated-autonomy
profile. Deterministic implementation and the approved harmless live fixture
are complete. No service configuration or application credential handling was
introduced.

Claude is available as a read-only workflow node through the production
`simplechart-claude-qc` wrapper.

A future implementation session must read, in order:

1. `AGENTS.md`
2. `docs/agentic-workflow.md`
3. This specification
4. `docs/credential-security.md` if proposed work changes credential or
   provider boundaries

Future review packets follow the normal preview, approval, review, and finding
adjudication flow. Gated autonomy still stops before an unapproved outbound
packet, material amendment, or final acceptance.

## 2. Problem statement

SimpleChart's agentic workflow requires Claude to act as an external,
read-only design and implementation reviewer. The integration must use the
user's existing Anthropic subscription while preventing Claude from reading,
editing, or discovering workspace material outside an explicitly approved
review packet.

## 3. Current behavior

- Deterministic packet, finding, preflight, process, review-invocation, event,
  and command modules exist. The separate `simplechart-claude-qc` executable is
  registered, verified through an isolated generated-script installation, and
  live-validated against Claude Code 2.1.257.
- Claude Code manages its own subscription authentication.
- `claude -p` supports stdin, structured output, model and effort selection,
  tool restriction, and disabled session persistence.
- Claude Code bare mode disables subscription OAuth; this integration cannot
  use `--bare`.
- Subscription-backed programmatic calls use a monthly Agent SDK credit
  separate from interactive usage.

## 4. Goals

- Invoke Claude Opus 5 using the user's authenticated Anthropic subscription.
- Send only a complete, previewed, explicitly approved packet.
- Give Claude no tools, MCP servers, plugins, skills, memory, or repository
  access.
- Avoid application handling of Anthropic credentials.
- Obtain strict, structured findings that remain inert until orchestrator
  adjudication.
- Suppress local Claude session and prompt persistence.
- Fail closed on unsafe configuration, authentication ambiguity, malformed
  output, quota exhaustion, or service failure.
- Provide deterministic automated tests and a separately approved live
  fixture.

## 5. Non-goals

- No charting-application or GUI feature.
- No direct Messages API or API-key support.
- No `CredentialStore` changes.
- No Claude installation, update, login, logout, or account configuration.
- No filesystem, Bash, edit, read, web, MCP, skill, plugin, subagent, or
  callback access.
- No model fallback.
- No persistent Claude conversation or resumable session.
- No guarantee of US-only inference routing; Claude Code subscription mode
  does not expose that request control.
- No claim that the executable itself can prove human consent.
- No deterministic expectation for finding prose or count.

## 6. Production integration path

Add the following production path:

```text
simplechart-claude-qc
    -> simplechart.claude_qc.cli:main
    -> subscription/authentication preflight
    -> packet validation and canonical preview
    -> approved-byte digest verification
    -> locked-down Claude Code subprocess
    -> event-stream and schema validation
    -> normalized findings returned to orchestrator
```

The executable must not import or construct `MainWindow`, open SQLite, load
SimpleChart plugins, or register market-data providers.

## 7. External prerequisite and authentication

Claude Code is an external prerequisite. The integration does not install or
update it.

Before review:

1. Resolve the executable without a shell.
2. Record its version.
3. Run `claude auth status`.
4. Require an authenticated Claude subscription.
5. Reject Console/API-key, cloud-provider, proxy, bearer-token, or
   indeterminate authentication.
6. Normalize the status result without exposing account identity or credential
   details.

Claude Code gives environment API keys and cloud credentials precedence over
subscription OAuth. The child process therefore receives a minimal environment
allowlist and explicitly excludes:

```text
ANTHROPIC_API_KEY
ANTHROPIC_AUTH_TOKEN
ANTHROPIC_BASE_URL
CLAUDE_CODE_OAUTH_TOKEN
CLAUDE_CODE_USE_BEDROCK
CLAUDE_CODE_USE_VERTEX
CLAUDE_CODE_USE_FOUNDRY
```

The integration never reads, copies, serializes, logs, or stores the OAuth
credential. Claude Code remains solely responsible for its subscription login.

If subscription authentication cannot be proven, Claude QC is unavailable.

## 8. Locked-down invocation

The intended invocation is equivalent to:

```text
claude -p
  --model claude-opus-5
  --effort high
  --tools ""
  --setting-sources ""
  --strict-mcp-config
  --mcp-config {"mcpServers":{}}
  --no-session-persistence
  --max-turns 1
  --output-format stream-json
  --verbose
  --json-schema <fixed non-sensitive schema>
  --system-prompt <fixed reviewer instruction>
  <fixed review instruction>
```

The canonical packet is supplied through stdin. It never appears in process
arguments or a temporary file.

Additional child-process controls:

```text
CLAUDE_CODE_DISABLE_AUTO_MEMORY=1
CLAUDE_CODE_SKIP_PROMPT_HISTORY=1
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
MAX_STRUCTURED_OUTPUT_RETRIES=0
```

Requirements:

- Do not use `--bare`, because it disables subscription OAuth.
- Empty setting sources must be supported by the installed Claude Code
  version.
- Built-in tools must be disabled with `--tools ""`.
- MCP must be restricted to an explicitly empty inline configuration.
- The process runs in a newly created empty temporary working directory.
- No shell expansion is used.
- The prompt explicitly treats all packet content as untrusted evidence.
- No wrapper-level retry occurs.
- Claude Code's internal transient retries are observed and reported from its
  event stream.
- A 60-minute parent-process deadline terminates a hung invocation. This is a
  safety deadline, not a model token ceiling.
- Interruption, forced termination, or uncertain completion leaves the QC gate
  incomplete.

## 9. Model and capacity

- Model: `claude-opus-5`
- Effort: `high`
- No `temperature`
- No integration-defined `max_tokens`
- Maximum accepted findings: 15
- No fallback model

The integration validates that:

- The initialization event identifies Opus 5.
- Final usage metadata contains no other model.
- No fallback or substitution occurred.
- Any `max_output_tokens` termination is incomplete QC.

The model supports up to 128K output tokens, managed by Claude Code rather than
capped by SimpleChart.

## 10. Review packet

Required fields:

```text
schema_version
review_kind                 design | implementation
user_objective
applicable_constraints
specification
architecture_summary
acceptance_criteria
test_matrix
implementation_slices
known_risks
open_questions
implementation_diff         empty for design review
verification_summary        empty for design review
```

Maximum canonical packet size: 128 KiB.

The packet builder receives explicit values only. It must not inspect:

- The repository
- Arbitrary paths
- Environment mappings
- SQLite
- OS credentials
- Logs
- Claude/Codex configuration
- Process output from unrelated commands

Unknown fields and oversized values fail locally.

## 11. Data exclusion

Packets must exclude:

- Credentials and authorization material
- Private databases or database contents
- Environment values
- Unrelated source or diffs
- Arbitrary repository context
- Personal information
- Raw logs
- Local agent configuration
- Absolute paths unless indispensable and separately approved
- Any material outside the active specification or implementation slice

For implementation QC, only the approved slice's diff and relevant
verification summary may be included.

## 12. Preview and consent

Consent remains a workflow responsibility.

The `preview` operation must produce:

- The complete canonical packet exactly as it would be transmitted
- Its SHA-256 digest
- Byte size
- Model and effort
- Included packet categories
- Account-data retention notice
- An explicit statement that material will leave the machine

The user approves the complete rendered packet. The orchestrator then supplies
its digest to `review`.

The digest proves byte identity only. It does not independently prove that a
human granted consent, and neither implementation nor tests may claim
otherwise.

Any byte change after preview invalidates approval.

## 13. Claude authority

Claude:

- Receives no filesystem, command, web, MCP, plugin, skill, or agent tools.
- Receives only Claude Code's internal read-only `StructuredOutput` return
  mechanism required by `--json-schema`.
- Has no workspace access.
- Reviews only the supplied packet.
- Treats embedded instructions as untrusted evidence.
- Cannot edit files, communicate with workers, spawn agents, broaden scope,
  approve findings, or transition the graph.
- Returns only the required finding schema.

The locked CLI controls prevent user, project, and local settings from loading
and disable hooks, memory, plugins, skills, agent execution, MCP, and commands. The
event parser independently rejects the run if initialization reports:

- Any tool other than `StructuredOutput`
- Any MCP server
- Any plugin
- Any skill, command, or callable subagent facility
- Any model other than Opus 5

Any tool-use event other than one schema-validated `StructuredOutput` return
rejects the run. Advertised agent names are inert metadata because neither the
`Agent` nor legacy `Task` tool is available.

Claude Code initialization does not report settings-source, hook, or memory
provenance. The integration therefore does not claim that initialization alone
proves those hidden surfaces absent. The separately approved hostile live
fixture must verify that the installed CLI honors their suppression controls
end to end before the integration is considered available.

## 14. Finding schema

Each finding requires:

```text
finding_id
severity                  critical | high | medium | low
claim
evidence
impact
falsification_check
suggested_disposition     accept | reject | defer |
                          needs_user_decision | duplicate
confidence                high | medium | low
```

Rules:

- Every field is required.
- Additional properties are rejected.
- IDs must be unique and non-empty.
- At most 15 findings are accepted.
- An empty array means no findings.
- Claude Code schema validation is not sufficient by itself.
- Independent local validation remains mandatory.
- One invalid finding rejects the entire result.
- Suggested dispositions remain inert review data.

## 15. Event and response validation

The wrapper consumes Claude Code's structured event stream and requires:

1. Exactly one initialization event.
2. The expected Claude Code protocol shape.
3. Empty tool, MCP, plugin, and agent surfaces.
4. The requested model and high effort.
5. No tool-use events.
6. A successful terminal result.
7. Structured output matching the local schema.
8. Usage metadata showing only Opus 5.
9. No quota, authentication, retry-exhaustion, or output-limit error.

Raw Claude Code output is parsed in memory and discarded after normalization.

## 16. Versioned executable protocol

Every normal completion writes one JSON envelope to stdout:

```text
protocol_version
status                     complete | incomplete
category
message
claude_code_version
model
retry_count
duration_ms
findings
```

Exit codes:

- `0`: valid completed review
- `2`: usage, packet, preview, or digest error
- `3`: Claude Code unavailable, incompatible, or not
  subscription-authenticated
- `4`: quota, provider, transport, deadline, or interruption failure
- `5`: unsafe initialization, model mismatch, malformed events, or invalid
  findings
- `130`: direct user interruption

Failure envelopes contain sanitized categories and no raw provider error. Any
unrecognized output or process termination is incomplete QC.

The orchestrator maps every nonzero exit or incomplete envelope to a pending QC
gate and offers the workflow-defined choices: wait, retry, waive, or approve
substitution.

## 17. Command surface

Only these operations are provided:

- `status`: validate executable compatibility and subscription authentication
  without sending a review.
- `preview`: validate and render the exact canonical packet without invoking
  Claude or accessing authentication.
- `review --approved-digest DIGEST`: verify byte identity and invoke Claude.

There are no credential-management commands.

## 18. Retention and privacy

Local behavior:

- Use `--no-session-persistence`.
- Set prompt-history suppression as a second control.
- Do not write packets, raw events, or raw responses.
- Do not create application logs.
- Return only normalized findings and approved metadata.
- Do not submit feedback or enable nonessential telemetry.

Provider behavior depends on subscription type and account privacy settings:

- Consumer Pro/Max accounts may retain data for 30 days when model-improvement
  use is disabled.
- If model-improvement use is enabled, consumer data may be retained longer and
  used for training.
- Team and Enterprise accounts follow their applicable commercial policy.
- Programmatic subscription use consumes the separate monthly Agent SDK
  credit.

Before any real project packet, the user must approve a retention statement
appropriate to the active account. The integration cannot programmatically
verify the account's privacy toggle and must not claim that it can.

## 19. Accessibility

- Text and JSON output do not rely on color.
- Errors use stable categories and plain language.
- Preview shows the complete outbound material.
- Machine-readable output is separated from optional human-facing explanation.
- No interactive credential prompt is introduced.

## 20. Acceptance criteria

1. Normal SimpleChart startup is unaffected when Claude Code is absent.
2. The QC path never opens SQLite, loads chart plugins, or constructs
   application UI.
3. `status` accepts only compatible subscription authentication.
4. API keys, bearer tokens, cloud credentials, and alternate endpoints cannot
   override subscription use.
5. `preview` accesses neither Claude, authentication, repository files, nor
   network.
6. Preview renders the exact canonical bytes subsequently sent.
7. Digest mismatch prevents invocation.
8. Only allowlisted packet fields reach stdin.
9. The packet never appears in arguments, files, logs, or errors.
10. Claude starts in an empty directory with empty setting sources and MCP
    configuration; `StructuredOutput` is the only available tool.
11. Initialization proves the reported plugin, MCP, skill, and command surfaces
    are empty and that no callable agent tool exists. Locked CLI controls
    prevent settings, hooks, and memory from loading, and the hostile live
    fixture verifies that suppression end to end because initialization does
    not expose their provenance.
12. Opus 5 at high effort is the only reported model.
13. No integration-defined output-token ceiling is imposed.
14. Only complete, locally valid findings are accepted.
15. Claude output cannot cause edits, calls, adjudication, or graph transitions.
16. Every failure returns a versioned incomplete result and nonzero exit.
17. The wrapper performs no automatic retry.
18. A 60-minute parent deadline terminates a hung process and leaves completion
    indeterminate.
19. No local Claude transcript or prompt history is created.
20. The registered executable, process-boundary preview, and production review
    handler are all tested.
21. Normal automated tests require neither installed Claude Code nor
    subscription access.
22. The live fixture is synthetic, separately approved, and checks actual
    locked-down initialization.

## 21. TDD matrix

| Slice | Intended RED | Required GREEN |
|---|---|---|
| Packet model | No strict packet or canonical serializer exists | Exact bytes, size limits, unknown-field rejection, and exclusion canaries pass |
| Consent integrity | No preview/digest binding exists | Exact rendering and one-byte mutation rejection pass |
| Authentication preflight | No subscription-only boundary exists | Fake status outputs accept subscription and reject every alternate method |
| Invocation isolation | No locked subprocess contract exists | Exact argv, minimal environment, empty cwd, stdin packet, and no shell are proven |
| Event validation | No Claude stream parser exists | Safe initialization succeeds; tools, plugins, MCP, alternate models, and malformed streams fail |
| Finding validation | No strict result parser exists | Valid fixtures parse; partial, extra, hostile, and oversized findings fail closed |
| Failure protocol | No versioned envelope exists | Every failure category maps to exact output and exit status without secret canaries |
| Command surface | No production commands exist | Status, preview, review, cancellation, and deadline behavior pass through real handlers |
| Packaging | No registered executable exists | Entry-point metadata and a real subprocess preview smoke test fail if registration is absent |
| Production wiring | No complete review path exists | Real handler completes packet -> fake process -> event parsing -> normalized findings |
| Live isolation | Connection has not been verified | Approved synthetic call proves subscription auth, `StructuredOutput`-only tool surface, model, schema, and no session persistence |

## 22. Implementation slices

Each slice follows RED -> red verification -> GREEN -> QC under gated autonomy:

1. Packet, canonical serialization, preview, digest, and finding types.
2. Subscription-authentication and Claude Code compatibility preflight.
3. Locked subprocess invocation and event validation.
4. Versioned command protocol and registered executable.
5. Focused and broader deterministic verification.
6. Explicitly approved harmless live fixture.
7. Claude post-green code QC using a separately previewed and approved packet.
8. Codex/Claude finding adjudication and final verification.

Only one writer may be active per slice.

## 23. Harmless validation fixture

The first live call contains only synthetic material:

```text
Objective: A fictional status badge must render blue.
Draft criterion: The badge renders.
Review whether the criterion proves the objective.
```

It verifies:

- Subscription authentication
- Opus 5 and high effort
- No action tools, MCP, plugins, settings, or repository context
- Only the internal read-only `StructuredOutput` return mechanism
- Structured output
- Schema validation
- Session suppression
- Sanitized failure behavior
- No local project material in input or output

It does not assert exact prose or finding count.

## 24. Risks and falsification checks

| Risk | Falsification check |
|---|---|
| API key silently overrides subscription | Inject every documented credential variable and prove the child environment excludes it |
| Local configuration grants authority | Seed hostile settings, hooks, MCP, plugins, and memory; verify the reported capability surfaces remain empty and no hostile effect appears in the live fixture |
| `--bare` is accidentally added | Subscription fixture must fail because bare mode disables OAuth |
| Packet differs after approval | One-byte mutation fails digest verification |
| Packet leaks through process metadata | Inspect argv, cwd, environment, temporary directory, logs, and errors |
| Claude uses a fallback model | Return alternate model usage and require rejection |
| Action-tool surface is nonempty | Inject any tool other than `StructuredOutput`, or any MCP entry, and require rejection |
| Output is partly valid | One malformed finding rejects the complete result |
| Claude Code writes a transcript | Inspect the isolated project/session location after the fixture |
| Wrapper hangs | Fake process exceeds the parent deadline and is terminated |
| Codex session reaches its usage cutoff | Persist the exact graph state; absent an explicitly started Codex goal, wait for a new user or product trigger, then resume from this specification without repeating completed work |

## 25. Design QC findings and dispositions

- **C1-001:** Accepted and superseded by Claude Code. No temperature or direct
  API version; high effort; no custom token ceiling.
- **C1-002:** Accepted. Complete canonical preview; digest described only as
  integrity.
- **C1-003:** Accepted. Hard deadline enforced at the subprocess boundary.
- **C1-004:** Accepted. Entry-point, process-preview, and production-handler
  tests.
- **C1-005:** Accepted. Versioned envelopes and exact exit behavior.
- **C1-006:** Accepted. Every remaining command operation has production
  coverage.
- **C1-007:** Model verification accepted. US-only geography is unavailable
  under the approved subscription mechanism and is removed rather than falsely
  claimed.

Claude design QC was unavailable during bootstrap, as required by the workflow.
It was not silently waived or substituted.

## 26. Approved constraints requiring renewed approval to change

- Subscription-backed `claude -p`, not direct API access
- No `--bare`, because it is incompatible with subscription OAuth
- Opus 5 with high effort
- No integration-defined output-token ceiling
- Provider-controlled inference geography
- The applicable subscription retention policy
- A 60-minute subprocess safety deadline
- Claude Code as an external prerequisite that SimpleChart does not install or
  configure
- Per-packet complete preview and explicit user approval
- No action tools, workspace access, session persistence, or model fallback;
  only the internal `StructuredOutput` return mechanism is allowed

## 27. Implementation autonomy envelope

```text
Approval profile: Gated autonomy
Approved objective: Implement the approved Claude QC integration.
In-scope behavior and layers: New simplechart.claude_qc package, deterministic
  tests, separate executable registration, and implementation documentation.
Allowed writes: New Claude-QC modules and tests, pyproject.toml entry-point
  registration, and this durable artifact.
Allowed verification: Deterministic tests, isolated local subprocess fixtures,
  lint, typing, compilation, installed-entry-point checks, and read-only local
  Claude version/help/auth-status inspection with identity redaction.
Allowed external actions and data: Current official Anthropic documentation.
  No inference call or project packet transmission before its explicit gate.
Prohibited actions: Claude installation/update/login/logout/configuration;
  credential access or storage; API-key or direct-API support; application/UI,
  database, provider, plugin, or source-control changes; destructive actions;
  unrelated fixes.
Mandatory user gates: Material amendment; each complete outbound packet and
  retention notice; harmless live fixture; unavailable required reviewer;
  final acceptance.
Required reviewers: Codex QC after deterministic GREEN; Claude QC only after
  the live isolation fixture makes that node available.
Stop conditions: Any approved security or data boundary cannot be proved;
  a dependency is required; live initialization exposes authority; exact
  subscription/model provenance cannot be proved; or a material product or
  architecture decision appears.
Reporting cadence: Evidence update after each logical slice and join.
```

The pre-existing Claude Code login is user-managed external infrastructure.
SimpleChart neither stores nor loads its OAuth credential and does not offer
login or credential-management behavior. This is the approved distinction from
application provider credentials governed by `CredentialStore`.

## 28. Resumption state

```text
Last completed node: Slice 6 harmless live fixture and first project design QC
Active node: None
Active writer: None
Files owned by active writer: None
Pending join or gate: None for the integration; each future packet retains its
  normal preview and approval gate
Verified evidence available: Claude Code 2.1.257 exposes the required local
  controls; redacted auth status reports first-party claude.ai Pro subscription;
  114 cumulative focused tests pass; Ruff and strict package mypy pass; the
  harmless fixture and a real project design review completed through the
  production wrapper with Opus 5 and zero wrapper retries.
Known unrelated workspace changes: Existing governance/specification documents
  are modified or untracked. Baseline offscreen suite has 276 passing tests and
  three unrelated failures in chart-legend geometry and poly-line expectations.
Exact next transition: Use the normal workflow for the next approved QC packet.
```

## 29. Internal implementation decisions

These decisions resolve underspecified mechanics without changing approved
behavior, architecture, security, data scope, or acceptance criteria:

1. `preview` and `review` read one packet JSON object from stdin. `status` reads
   no packet.
2. Packet schema version is the string `"1"`. Every named content field is a
   JSON string. Core context fields must be non-empty. For design review,
   `implementation_diff` and `verification_summary` must be empty; for
   implementation review they must be non-empty. `known_risks` and
   `open_questions` may be empty.
3. Canonical bytes are UTF-8 JSON with lexicographically sorted keys, compact
   separators, `ensure_ascii=False`, and no trailing newline. Duplicate keys,
   unknown keys, invalid UTF-8, non-string values, and canonical output above
   128 KiB fail locally.
4. All commands return the versioned envelope from section 16 plus a strict
   operation-specific `details` object. Preview details contain the canonical
   packet text, SHA-256 digest, byte size, effort, included categories,
   retention notice, and outbound-data warning. Status and review expose only
   their approved normalized metadata. The packet is never placed in a process
   argument, error, or log.
5. Compatibility is capability-based rather than an invented minimum version:
   the version and help outputs must be well formed and advertise every
   required flag except `--max-turns`. Claude Code 2.1.252 recognizes that
   officially documented option but omits it from help, so the local
   authentication command also acts as a no-inference parser probe for
   `--max-turns 1`. Version/help/auth-status preflights each have a ten-second
   local subprocess deadline. Undocumented or changed auth fields fail closed.
6. The current personal-subscription boundary accepts only `pro` or `max` with
   `loggedIn == true`, `authMethod == "claude.ai"`, and
   `apiProvider == "firstParty"`. In subscription mode initialization reports
   `apiKeySource == "none"`, meaning no API key is active; subscription OAuth is
   proven by the separate preflight and prohibited child-environment variables.
   Other subscription classes require a future explicit compatibility decision
   rather than being guessed.
7. The child environment is constructed from a positive platform-specific
   allowlist of user-home, executable-search, locale, operating-system runtime,
   and temporary-directory variables plus the fixed suppression controls in
   section 8. Provider, API, bearer-token, OAuth-token, gateway, profile,
   proxy, and Claude configuration overrides are never copied.
8. The invocation also uses the installed CLI's documented hardening controls:
   restricted mode, safe mode, disabled slash commands, denied MCP wildcard,
   disabled Chrome integration, and noninteractive permission denial. These
   strengthen the approved no-authority boundary. Initialization is decisive
   for every capability surface it reports. The hostile live fixture verifies
   settings, hook, and memory suppression because their provenance is not
   present in the initialization protocol.
9. Packet and digest validation precede authentication preflight in `review`,
   minimizing side effects for invalid or unapproved input.
10. No per-finding prose limit, raw-event byte ceiling, model token ceiling, or
    wrapper retry is introduced. The approved 15-finding count, Claude-managed
    model capacity, strict completion checks, and 60-minute deadline remain the
    bounds.
11. Claude Code implements `--json-schema` through its internal read-only
    `StructuredOutput` tool. The validator permits exactly that tool, its
    matching tool-result handshake, inert thinking/rate-limit status events,
    and the schema-identical terminal output. Every action tool, mismatched
    tool result, duplicate structured return, subagent execution, or other
    capability still fails closed.

## 30. Plan-change and execution evidence

| Change or evidence | Classification | Basis | Result |
|---|---|---|---|
| Replace per-slice user review with gated-autonomy reporting | Approved governance amendment | User approved the reusable autonomy baseline and then authorized implementation | Active |
| Add current CLI hardening flags | Internal adjustment | Claude Code 2.1.252 help and official documentation expose stronger controls inside the approved no-authority boundary | Accepted |
| Treat external OAuth storage as outside SimpleChart credential ownership | Approved specification clarification | Claude Code owns login; SimpleChart never reads, writes, or configures the credential | Accepted |
| Canonical JSON and operation-specific envelope details | Internal adjustment | Required to make byte identity and preview machine-verifiable without changing transmitted categories | Accepted |
| First full-suite run without a display | Verification evidence | Qt aborted during fixture initialization | Superseded by offscreen run |
| Offscreen baseline: 276 passed, 3 failed | Unrelated baseline evidence | Failures concern existing chart palette geometry and poly-line 10-versus-15 behavior, outside this objective | Report only; do not fix |
| Slice 1 test fixture used a spacing-dependent byte replacement | Internal test adjustment | The replacement did not create the intended non-string JSON value | Replaced with direct JSON construction; contract unchanged |
| C2-001 through C2-004 | Verified in-scope QC corrections | Independent re-review verified exception redaction, exact findings root, retention disclosure, and Unicode normalization | Accepted and closed |
| Claude Code 2.1.252 omits documented `--max-turns` from help | Internal compatibility adjustment | Official CLI documentation retains the flag; local `auth status` accepts it and rejects a synthetic unknown flag without inference | Probe it on auth status instead of weakening/removing the approved turn limit |
| C3-001 through C3-003 | Verified in-scope QC corrections | Independent re-review verified duplicate/unknown auth rejection, exact option matching, and complete prohibited-variable canaries | Accepted and closed |
| Production subscription preflight smoke | Verification evidence | The actual isolated preflight returned only normalized version `2.1.252` and tier `pro` | Passed; no inference or identity output |
| C4-001, C4-002, and C4-004 | Verified in-scope QC corrections | Independent re-review verified inert thinking-block compatibility, traceback-local redaction, and refusal rejection | Accepted and closed |
| C4-003 | Approved evidence-model amendment | Claude init reports capability lists but not settings-source, hook, or memory provenance; documented CLI controls and the hostile live fixture cover the hidden surfaces | Accepted; do not claim initialization directly reports hidden provenance |
| C5-001 through C5-003 | Verified in-scope QC corrections | Independent re-review verified generated console-script execution, complete failure mapping, and poison-environment preview isolation | Accepted and closed |
| C6-001 through C6-003 | Verified in-scope QC corrections | Independent re-review verified preflight/packet/finding traceback redaction and exact assistant completion shape | Accepted and closed |
| DQ-001 through DQ-004 | Verified deterministic test corrections | Independent test-architect re-review verified single-call behavior, recursive-JSON failure envelopes, import-layer blocking, and current artifact state | Accepted and closed |
| Live `StructuredOutput` protocol correction | Verified live compatibility correction | Claude Code 2.1.257 exposes only its internal read-only structured return tool; subscription OAuth reports no active API-key source; the return uses a two-turn tool handshake | Accepted; exact allowlist and matching-output validation added |

## 31. Execution evidence

| Slice | RED evidence | GREEN evidence | QC disposition | Broader checks |
|---|---|---|---|---|
| 1. Packet and findings | Both focused modules failed collection only because `simplechart.claude_qc` did not exist; correction RED reproduced all four QC findings | 37 focused tests pass; Ruff clean; strict mypy clean; compileall clean | C2-001 through C2-004 accepted, corrected, and independently verified; no new high/critical issue; Claude unavailable until live fixture | Baseline collected 279 tests; unrelated offscreen failures recorded above |
| 2. Subscription preflight | Focused module failed because environment/process/preflight modules did not exist; correction RED reproduced all three C3 findings and the help-hidden turn-limit mismatch | 54 cumulative focused tests pass; Ruff clean; strict mypy clean; compileall clean; real normalized preflight passes | C3-001 through C3-003 and max-turns compatibility adjustment independently verified; no high/critical regression | No inference, network review, auth change, or identity output |
| 3. Invocation and events | Both focused modules failed collection because `events` and `runner` did not exist; correction RED reproduced protocol-trailing, retry-shape, provider-category, thinking-block, traceback-retention, and refusal gaps | 34 slice tests and 88 cumulative focused tests pass; Ruff clean; strict mypy clean; compileall clean | C4-001, C4-002, and C4-004 corrected and independently verified; C4-003 evidence-model amendment approved and recorded | No inference call; official protocol compatibility rechecked; no packet or identity transmitted |
| 4. Command protocol and packaging | Command tests failed because `cli` did not exist; packaging failed because no separate script was declared; C5 correction exposed that module invocation did not prove generated-script availability | 17 slice tests and 105 cumulative focused tests pass; isolated installation generates and runs `simplechart-claude-qc preview`; Ruff clean; strict mypy clean; compileall clean | C5-001 through C5-003 corrected and independently verified; no new high/critical regression | Preview subprocess uses synthetic data only; isolated install writes only below pytest temporary storage; no Claude invocation |
| 5. Broader deterministic verification | C6 and DQ adversarial tests reproduced traceback-retention, stop-shape, retry-evidence, recursive-JSON, import-boundary, and artifact gaps before correction | 113 focused tests pass; whole-repository Ruff clean; strict package mypy clean; compileall and diff-check clean; full offscreen suite 389 passed with the same three unrelated baseline failures | C6-001 through C6-003 and DQ-001 through DQ-004 independently verified; no open material deterministic finding | Production `status` passes against Claude Code 2.1.257 Pro; no inference or packet transmission |
| 6. Harmless live fixture | Initial live fixture failed closed because the validator incorrectly expected no return tool, empty agent metadata, OAuth in the API-key field, and a one-turn `end_turn` result | Observed-protocol regression test passes; 114 focused tests pass; Ruff and strict package mypy clean; production fixture completes with three valid findings | `StructuredOutput` is allowlisted only as a read-only schema return; all action capability checks remain fail-closed | Claude Code 2.1.257, Opus 5, high effort, zero wrapper retries, empty isolated directory, no MCP/plugins/skills/commands |

## 32. Completed harmless live fixture

The following exact 791-byte canonical packet completed through the production
wrapper on 2026-09-01:

```json
{"acceptance_criteria":"Review whether the draft criterion proves that the fictional badge renders blue.","applicable_constraints":"Use only the supplied synthetic evidence. Do not use tools, request context, or take actions.","architecture_summary":"Synthetic connection fixture only; it contains no repository or application material.","implementation_diff":"","implementation_slices":"One harmless read-only design review.","known_risks":"The draft criterion may omit the required color.","open_questions":"","review_kind":"design","schema_version":"1","specification":"Draft criterion: The badge renders.","test_matrix":"Return only locally validated structured findings under the supplied schema.","user_objective":"A fictional status badge must render blue.","verification_summary":""}
```

```text
SHA-256: a0e724dcbd50ee720d16e9701b182c1ee8e70c6da1f3a1dc9aaba81ea039af7c
Model: claude-opus-5
Effort: high
Output-token ceiling: none imposed by SimpleChart
Outbound warning: The complete canonical packet will leave this machine and be sent to Anthropic.
Retention notice: For consumer Pro and Max accounts, Anthropic may retain
  submitted data for 30 days when model-improvement use is disabled. If it is
  enabled, data may be retained longer and used for model training.
```

Result: Claude Code 2.1.257, Opus 5, high effort, zero wrapper retries, three
locally validated findings, no unexpected files, and no action capabilities.
