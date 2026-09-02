from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final


FIXED_CLAUDE_ENVIRONMENT: Final[Mapping[str, str]] = MappingProxyType(
    {
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "MAX_STRUCTURED_OUTPUT_RETRIES": "0",
    }
)

PROHIBITED_CLAUDE_ENVIRONMENT_KEYS: Final = frozenset(
    {
        "ALL_PROXY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_FEDERATION_RULE_ID",
        "ANTHROPIC_ORGANIZATION_ID",
        "ANTHROPIC_PROFILE",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CONFIG_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)

_POSIX_ENVIRONMENT_KEYS = (
    "HOME",
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
)

_WINDOWS_ENVIRONMENT_KEYS = (
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "SystemRoot",
    "WINDIR",
    "PATH",
    "PATHEXT",
    "TEMP",
    "TMP",
)


def build_claude_environment(
    source: Mapping[str, str],
    *,
    platform: str,
) -> dict[str, str]:
    keys = (
        _WINDOWS_ENVIRONMENT_KEYS
        if platform == "win32"
        else _POSIX_ENVIRONMENT_KEYS
    )
    environment: dict[str, str] = {}
    for key in keys:
        value = source.get(key)
        if value:
            environment[key] = value
    environment.update(FIXED_CLAUDE_ENVIRONMENT)
    return environment
