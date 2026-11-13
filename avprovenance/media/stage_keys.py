"""Deterministic keys that identify one intermediate media stage.

A stage key must change whenever the stage bytes could change.  It therefore
includes the primary source digest, the secondary source digest that has been
mixed in (``None`` before the single allowed mix), and every normalized
operation in the applied prefix.  Because the key carries full operation
payloads, two plans that merely share an operation *name* but use different
rounding, gain, crop, or mix parameters never alias one another.
"""

from __future__ import annotations

from typing import Any

from ..serialization import digest_json

# Stage labels are stable strings so metrics stay readable across versions.
DECODE_STAGE = "decode"


def stage_key(
    *,
    stage: str,
    primary_digest: str,
    secondary_digest: str | None,
    operations_prefix: list[dict[str, Any]],
) -> tuple[str, str, str | None, str]:
    return (
        stage,
        primary_digest,
        secondary_digest,
        digest_json(operations_prefix),
    )


def decode_key(
    *,
    role: str,
    source_digest: str,
) -> tuple[str, str]:
    """Key for a decoded source clip; the role separates primary/secondary."""
    return (DECODE_STAGE, role, source_digest)
