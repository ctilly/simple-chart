"""
app/errors.py

UI shielding: the message shown for an exception. Only a UserFacingError's
own text is ever shown; data sources translate their failures into such
errors where they raise them. Every other exception gets a generic message
composed here from app-controlled context.
"""

import logging
from dataclasses import dataclass

from data.errors import UserFacingError, describe


@dataclass(frozen=True)
class ErrorContext:
    # The action phrase that completes "{action} failed." (for example
    # "Loading TRAX 5m history").
    action: str


def report_error(
    logger: logging.Logger,
    exc: BaseException,
    context: ErrorContext,
) -> str:
    # Logs only describe()'s safe facts; returns the message to show. A
    # message the user sees is expected, so it is logged at WARNING.
    level = logging.WARNING if isinstance(exc, UserFacingError) else logging.ERROR
    logger.log(level, "%s failed: %s", context.action, describe(exc))
    return user_message(exc, context)


def user_message(exc: BaseException, context: ErrorContext) -> str:
    if isinstance(exc, UserFacingError):
        return str(exc)
    return f"{context.action} failed. Details were logged."
