from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, NoReturn

from simplechart.claude_qc.environment import build_claude_environment
from simplechart.claude_qc.process import (
    ProcessBackend,
    ProcessOutcome,
    ProcessRequest,
    ProcessStartError,
    ProcessTimeoutError,
)


PREFLIGHT_TIMEOUT_SECONDS = 10.0
REQUIRED_CLAUDE_FLAGS: Final = (
    "--print",
    "--model",
    "--effort",
    "--tools",
    "--disallowedTools",
    "--setting-sources",
    "--strict-mcp-config",
    "--mcp-config",
    "--no-session-persistence",
    "--output-format",
    "--verbose",
    "--json-schema",
    "--system-prompt",
    "--restricted",
    "--safe-mode",
    "--disable-slash-commands",
    "--no-chrome",
    "--permission-mode",
)
_MAX_TURNS_ARGUMENTS = ("--max-turns", "1")

_VERSION_PATTERN = re.compile(r"(\d+\.\d+\.\d+) \(Claude Code\)\s*")
_PERSONAL_SUBSCRIPTIONS = frozenset({"pro", "max"})
_AUTH_STATUS_FIELDS = frozenset(
    {
        "loggedIn",
        "authMethod",
        "apiProvider",
        "subscriptionType",
        "analyticsDisabled",
        "projectsDirectory",
        "email",
        "orgId",
        "orgName",
    }
)


class PreflightCategory(StrEnum):
    UNAVAILABLE = "unavailable"
    INCOMPATIBLE = "incompatible"
    SUBSCRIPTION = "subscription"


class PreflightError(RuntimeError):
    def __init__(self, category: PreflightCategory, message: str) -> None:
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class ClaudeCodeInstallation:
    executable: Path
    version: str
    subscription_type: str


@dataclass(frozen=True)
class _PreflightFailure:
    category: PreflightCategory
    message: str


def resolve_claude_code(
    finder: Callable[[str], str | None] = shutil.which,
) -> Path:
    found: str | None = None
    lookup_failed = False
    try:
        found = finder("claude")
    except OSError:
        lookup_failed = True
    if lookup_failed or found is None:
        raise PreflightError(
            PreflightCategory.UNAVAILABLE,
            "Claude Code is not available.",
        )
    return Path(found).resolve()


def _run_preflight_command(
    backend: ProcessBackend,
    request: ProcessRequest,
    category: PreflightCategory,
) -> ProcessOutcome:
    outcome: ProcessOutcome | None = None
    failed = False
    try:
        outcome = backend.run(request)
    except (ProcessTimeoutError, ProcessStartError):
        failed = True
    if failed or outcome is None:
        raise PreflightError(category, "Claude Code preflight did not complete.")
    return outcome


def _parse_version(outcome: ProcessOutcome) -> str:
    if outcome.return_code != 0:
        raise PreflightError(
            PreflightCategory.INCOMPATIBLE,
            "Claude Code version detection failed.",
        )
    text: str | None = None
    try:
        text = outcome.stdout.decode("utf-8")
    except UnicodeDecodeError:
        pass
    match = _VERSION_PATTERN.fullmatch(text) if text is not None else None
    if match is None:
        raise PreflightError(
            PreflightCategory.INCOMPATIBLE,
            "Claude Code returned an unsupported version format.",
        )
    return match.group(1)


def _validate_help(outcome: ProcessOutcome) -> None:
    if outcome.return_code != 0:
        raise PreflightError(
            PreflightCategory.INCOMPATIBLE,
            "Claude Code capability detection failed.",
        )
    text: str | None = None
    try:
        text = outcome.stdout.decode("utf-8")
    except UnicodeDecodeError:
        pass
    declared_options: set[str] = set()
    if text is not None:
        for line in text.splitlines():
            tokens = line.strip().split()
            if not tokens or not tokens[0].startswith("-"):
                continue
            for token in tokens:
                if not token.startswith("-"):
                    break
                option = token.rstrip(",")
                if option.startswith("--"):
                    declared_options.add(option)
    if any(flag not in declared_options for flag in REQUIRED_CLAUDE_FLAGS):
        raise PreflightError(
            PreflightCategory.INCOMPATIBLE,
            "Claude Code lacks required isolation controls.",
        )


class _DuplicateAuthFieldError(ValueError):
    pass


def _strict_auth_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateAuthFieldError
        result[key] = value
    return result


def _parse_subscription(outcome: ProcessOutcome) -> str:
    if outcome.return_code != 0:
        if (
            not outcome.stdout
            and b"unknown option '--max-turns'" in outcome.stderr
        ):
            raise PreflightError(
                PreflightCategory.INCOMPATIBLE,
                "Claude Code lacks the required turn-limit control.",
            )
        raise PreflightError(
            PreflightCategory.SUBSCRIPTION,
            "Claude subscription authentication is unavailable.",
        )
    value: object | None = None
    invalid = False
    try:
        value = json.loads(
            outcome.stdout.decode("utf-8"),
            object_pairs_hook=_strict_auth_object,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateAuthFieldError,
        RecursionError,
    ):
        invalid = True
    if (
        invalid
        or not isinstance(value, dict)
        or set(value) != _AUTH_STATUS_FIELDS
        or not isinstance(value.get("analyticsDisabled"), bool)
        or not isinstance(value.get("projectsDirectory"), str)
        or not isinstance(value.get("email"), str)
        or not isinstance(value.get("orgId"), str)
        or not isinstance(value.get("orgName"), str)
    ):
        raise PreflightError(
            PreflightCategory.SUBSCRIPTION,
            "Claude subscription authentication is indeterminate.",
        )
    subscription_type = value.get("subscriptionType")
    if (
        value.get("loggedIn") is not True
        or value.get("authMethod") != "claude.ai"
        or value.get("apiProvider") != "firstParty"
        or not isinstance(subscription_type, str)
        or subscription_type not in _PERSONAL_SUBSCRIPTIONS
    ):
        raise PreflightError(
            PreflightCategory.SUBSCRIPTION,
            "Claude Code is not using an approved personal subscription.",
        )
    return subscription_type


def _perform_preflight(
    backend: ProcessBackend,
    parent_environment: Mapping[str, str],
    *,
    finder: Callable[[str], str | None] = shutil.which,
    platform: str = sys.platform,
) -> ClaudeCodeInstallation:
    executable = resolve_claude_code(finder)
    child_environment = build_claude_environment(
        parent_environment,
        platform=platform,
    )
    with tempfile.TemporaryDirectory(prefix="simplechart-claude-qc-") as directory:
        cwd = Path(directory)

        def request(*arguments: str) -> ProcessRequest:
            return ProcessRequest(
                argv=(str(executable), *arguments),
                cwd=cwd,
                environment=child_environment,
                stdin=b"",
                timeout_seconds=PREFLIGHT_TIMEOUT_SECONDS,
            )

        version = _parse_version(
            _run_preflight_command(
                backend,
                request("--version"),
                PreflightCategory.INCOMPATIBLE,
            )
        )
        help_outcome = _run_preflight_command(
            backend,
            request("--help"),
            PreflightCategory.INCOMPATIBLE,
        )
        _validate_help(help_outcome)
        subscription_type = _parse_subscription(
            _run_preflight_command(
                backend,
                request(*_MAX_TURNS_ARGUMENTS, "auth", "status", "--json"),
                PreflightCategory.SUBSCRIPTION,
            )
        )
    return ClaudeCodeInstallation(
        executable=executable,
        version=version,
        subscription_type=subscription_type,
    )


def _preflight_sensitive(
    backend: ProcessBackend,
    parent_environment: Mapping[str, str],
    *,
    finder: Callable[[str], str | None],
    platform: str,
) -> ClaudeCodeInstallation | _PreflightFailure:
    installation: ClaudeCodeInstallation | None = None
    failure: _PreflightFailure | None = None
    try:
        installation = _perform_preflight(
            backend,
            parent_environment,
            finder=finder,
            platform=platform,
        )
    except PreflightError as error:
        failure = _PreflightFailure(error.category, str(error))
    if failure is not None:
        return failure
    if installation is None:
        return _PreflightFailure(
            PreflightCategory.UNAVAILABLE,
            "Claude Code preflight did not complete.",
        )
    return installation


def _raise_preflight_error(
    category: PreflightCategory,
    message: str,
) -> NoReturn:
    raise PreflightError(category, message)


def preflight_claude_code(
    backend: ProcessBackend,
    parent_environment: Mapping[str, str],
    *,
    finder: Callable[[str], str | None] = shutil.which,
    platform: str = sys.platform,
) -> ClaudeCodeInstallation:
    result = _preflight_sensitive(
        backend,
        parent_environment,
        finder=finder,
        platform=platform,
    )
    del backend, parent_environment, finder, platform
    if isinstance(result, _PreflightFailure):
        _raise_preflight_error(result.category, result.message)
    return result
