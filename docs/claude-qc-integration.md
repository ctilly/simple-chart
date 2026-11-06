# Direct Claude Code collaboration

Call the installed `claude` CLI directly from the repository root. There is no
project-owned launcher, packet protocol, digest gate or response validator.
Codex supplies a short brief and adjudicates Claude's independent analysis.

## Native invocation

Use the user's normal first-party Claude Code sign-in. `claude auth status --json`
reports authentication status; resolve unexpected API-key/custom-routing
configuration before sending repository material. Start a fresh review without
`--resume` or `--continue`.

```sh
claude -p --model claude-opus-5-5 --effort high \
  --tools Read,Glob,Grep --allowedTools Read,Glob,Grep \
  --permission-mode dontAsk --permission-prompts none \
  --restricted --safe-mode --disable-slash-commands --no-chrome \
  --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
  --settings '{"permissions":{"deny":["Read(**/*.db)","Read(**/*.db-*)","Read(**/*.sqlite*)","Read(**/.env*)","Read(**/*.pem)","Read(**/*.key)","Read(./.git/**)","Read(./.aws/**)","Read(./.ssh/**)","Read(**/credentials.json)","Read(**/secrets.json)"]}}' \
  --no-session-persistence --output-format json < brief.txt
```

These are native Claude Code options. No custom JSON schema or structured-output
retry setting is needed. The native JSON envelope contains the response in
`result` and model usage in `modelUsage`; Codex checks that the process succeeded,
the review completed, the requested model answered, and no permission denials or
unresolved tool failures prevented inspection. No fallback model is accepted.
Plain-language findings are sufficient: location, issue, evidence and impact.

## Source access and review gate

Claude inspects source/tests directly using Read, Glob and Grep. File changes,
shell, agents, web and MCP tools are unavailable. Restricted mode confines file
tools to the workspace; Read denials exclude databases, credentials and private
configuration. Search-tool filtering is best effort under the native permission
contract. Keep credentials, private databases and live provider data out of the
brief. Managed machine policy remains a limitation of native safe/restricted mode.

A failed or incomplete review blocks the next implementation slice. Diagnose the
actual CLI failure and resolve it before a justified retry. Do not silently skip
Claude, accept a partial answer, or rebuild a wrapper to hide the failure.
Completed reviews stay closed; follow up only on unresolved issues or substantive
new changes. Do not archive temporary review output after final acceptance unless
the user requests retention.

See the [collaboration skill](skills/claude-collaboration.md) for the workflow and
the official [CLI reference](https://code.claude.com/docs/en/cli-reference) and
[permission rules](https://code.claude.com/docs/en/permissions) for native controls.
