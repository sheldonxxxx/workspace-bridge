"""Shared errors and identity validation for Runtime Protocol adapters."""
from __future__ import annotations

import re

from .security import BridgeError


class RuntimeUnavailable(BridgeError):
    """The adapter or its upstream runtime could not be reached."""

    def __init__(self, message: str = "Runtime adapter is unavailable"):
        super().__init__(message, "runtime_unavailable")


class RuntimeRejected(BridgeError):
    """The runtime answered but rejected the bounded request."""

    def __init__(self, message: str, code: str = "runtime_rejected",
                 status: int | None = None):
        super().__init__(message, code)
        self.status = status


class RuntimeUnsupported(BridgeError):
    """The installed adapter does not expose a requested capability."""

    def __init__(self, message: str):
        super().__init__(message, "runtime_unsupported")


# Runtime ids start with a lowercase letter or digit and contain only lowercase
# letters, digits, dashes, and underscores (32 characters maximum).
RUNTIME_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,31}$"
_RUNTIME_ID_RE = re.compile(RUNTIME_ID_PATTERN)


def is_valid_runtime_id(value: object) -> bool:
    """Whether a value has the canonical Runtime Protocol id shape."""
    return isinstance(value, str) and _RUNTIME_ID_RE.fullmatch(value) is not None
