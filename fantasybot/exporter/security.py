"""Credential filtering and validation for exported JSON data."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any


SENSITIVE_KEY_PARTS = (
    "authorization",
    "password",
    "passwd",
    "credential",
    "cookie",
    "secret",
)
SENSITIVE_KEYS = {
    "token",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "authtoken",
    "oauthtoken",
    "oauthcode",
    "authorizationcode",
    "codeverifier",
    "codechallenge",
    "headers",
    "requestheaders",
    "responseheaders",
}
BEARER_RE = re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
)


class UnsafeExportError(RuntimeError):
    """Raised when potentially sensitive material reaches the export."""


def normalize_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def is_sensitive_key(key: Any) -> bool:
    normalised = normalize_key(key)
    return normalised in SENSITIVE_KEYS or any(
        part in normalised for part in SENSITIVE_KEY_PARTS
    )


def strip_sensitive(value: Any) -> Any:
    """Return a deep copy with sensitive-keyed fields removed."""
    if isinstance(value, dict):
        return {
            str(key): strip_sensitive(item)
            for key, item in value.items()
            if not is_sensitive_key(key)
        }
    if isinstance(value, list):
        return [strip_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [strip_sensitive(item) for item in value]
    return deepcopy(value)


def find_sensitive(value: Any, path: str = "$") -> list[str]:
    """Find forbidden keys or credential-like strings in an export payload."""
    findings: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}"
            if is_sensitive_key(key):
                findings.append(child)
            findings.extend(find_sensitive(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            findings.extend(find_sensitive(item, f"{path}[{index}]"))
    elif isinstance(value, str) and (BEARER_RE.search(value) or JWT_RE.search(value)):
        findings.append(path)
    return findings


def validate_safe(value: Any) -> None:
    findings = find_sensitive(value)
    if findings:
        preview = ", ".join(findings[:5])
        raise UnsafeExportError(
            f"Export cancelled: potentially sensitive data found at {preview}."
        )
