"""PCM audio operations with explicit clipping and length rules."""

from __future__ import annotations

import math
from typing import Any

from ..serialization import sha256_hex
from .errors import MediaOperationFailure
from .stage_cache import StageCache
from .stage_keys import decode_key, stage_key
from .wav import AudioClip, decode_wav, encode_wav, replace_channels


PCM_MIN = -32768
PCM_MAX = 32767


def _clip_sample(value: float) -> int:
    if value >= PCM_MAX:
        return PCM_MAX
    if value <= PCM_MIN:
        return PCM_MIN
    return int(round(value))


def _gain_factor(db: float) -> float:
    return math.pow(10.0, float(db) / 20.0)


def trim_audio(clip: AudioClip, operation: dict[str, Any]) -> AudioClip:
    start = int(round(float(operation["start_seconds"]) * clip.sample_rate))
    duration = int(round(float(operation["duration_seconds"]) * clip.sample_rate))
    if start < 0 or start >= clip.frame_count:
        raise MediaOperationFailure(
            "AUDIO_TRIM_OUT_OF_RANGE",
            "The requested audio trim starts outside the source.",
        )
    end = min(clip.frame_count, start + max(1, duration))
    if end <= start:
        raise MediaOperationFailure(
            "AUDIO_TRIM_EMPTY",
            "The requested audio trim produces no sample frames.",
        )
    return replace_channels(clip, (values[start:end] for values in clip.channels))


def gain_audio(clip: AudioClip, operation: dict[str, Any]) -> AudioClip:
    factor = _gain_factor(float(operation["db"]))
    return replace_channels(
        clip,
        (
            [_clip_sample(value * factor) for value in values]
            for values in clip.channels
        ),
    )


def normalize_audio(clip: AudioClip, operation: dict[str, Any]) -> AudioClip:
    peak = max(
        (abs(value) for channel in clip.channels for value in channel),
        default=0,
    )
    if peak == 0:
        return clip
    target = float(operation["target_peak"])
    desired = max(1.0, target * PCM_MAX)
    factor = desired / peak
    return replace_channels(
        clip,
        (
            [_clip_sample(value * factor) for value in values]
            for values in clip.channels
        ),
    )


def mix_audio(
    primary: AudioClip,
    secondary: AudioClip,
    operation: dict[str, Any],
) -> AudioClip:
    if primary.sample_rate != secondary.sample_rate:
        raise MediaOperationFailure(
            "AUDIO_MIX_RATE_MISMATCH",
            "Mixed audio sources must share a sample rate.",
        )
    if primary.channel_count != secondary.channel_count:
        raise MediaOperationFailure(
            "AUDIO_MIX_CHANNEL_MISMATCH",
            "Mixed audio sources must share a channel count.",
        )
    frame_count = max(primary.frame_count, secondary.frame_count)
    primary_weight = float(operation["primary_weight"])
    secondary_weight = float(operation["secondary_weight"])
    secondary_factor = _gain_factor(float(operation["secondary_gain"])) * secondary_weight

    mixed: list[list[int]] = []
    for channel_index in range(primary.channel_count):
        left = primary.channels[channel_index]
        right = secondary.channels[channel_index]
        values: list[int] = []
        for frame_index in range(frame_count):
            left_value = left[frame_index] if frame_index < len(left) else 0
            right_value = right[frame_index] if frame_index < len(right) else 0
            combined = left_value * primary_weight + right_value * secondary_factor
            values.append(_clip_sample(combined))
        mixed.append(values)
    return replace_channels(primary, mixed)


def _apply_audio_operation(
    clip: AudioClip,
    operation: dict[str, Any],
    secondary: AudioClip | None,
) -> AudioClip:
    """Apply one normalized operation, preserving prior error semantics."""
    try:
        operation_type = operation["type"]
        if operation_type == "media_trim":
            return trim_audio(clip, operation)
        if operation_type == "audio_gain":
            return gain_audio(clip, operation)
        if operation_type == "audio_normalize":
            return normalize_audio(clip, operation)
        if operation_type == "audio_mix":
            if secondary is None:
                raise MediaOperationFailure(
                    "AUDIO_MIX_SOURCE_MISSING",
                    "audio_mix requires a secondary media asset.",
                )
            return mix_audio(clip, secondary, operation)
        raise MediaOperationFailure(
            "UNSUPPORTED_AUDIO_OPERATION",
            f"Unsupported audio operation: {operation_type}",
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MediaOperationFailure(
            "AUDIO_OPERATION_INVALID",
            "The audio operation payload is malformed.",
        ) from exc


def render_audio_staged(
    primary_bytes: bytes,
    operations: list[dict[str, Any]],
    secondary_bytes: bytes | None,
    *,
    primary_digest: str,
    secondary_digest: str | None,
    cache: StageCache,
) -> bytes:
    """Render while reusing every shared, fully computed intermediate stage."""
    clip = cache.get_or_compute(
        decode_key(role="primary", source_digest=primary_digest),
        "decode_primary",
        lambda: decode_wav(primary_bytes),
    )

    secondary: AudioClip | None = None
    if secondary_bytes is not None:
        if secondary_digest is None:
            secondary_digest = sha256_hex(secondary_bytes)
        secondary = cache.get_or_compute(
            decode_key(role="secondary", source_digest=secondary_digest),
            "decode_secondary",
            lambda: decode_wav(secondary_bytes),
        )

    mixed = False
    # The secondary only contributes after the single allowed mix operation.
    active_secondary_digest: str | None = None
    for index, operation in enumerate(operations):
        if operation["type"] == "audio_mix":
            if secondary is None:
                raise MediaOperationFailure(
                    "AUDIO_MIX_SOURCE_MISSING",
                    "audio_mix requires a secondary media asset.",
                )
            if mixed:
                raise MediaOperationFailure(
                    "AUDIO_MIX_DUPLICATE",
                    "An audio plan may mix the secondary source only once.",
                )
            mixed = True
            active_secondary_digest = secondary_digest

        prefix = operations[: index + 1]
        key = stage_key(
            stage="audio",
            primary_digest=primary_digest,
            secondary_digest=active_secondary_digest,
            operations_prefix=prefix,
        )
        current_clip = clip
        current_secondary = secondary
        clip = cache.get_or_compute(
            key,
            f"audio:{operation['type']}",
            lambda: _apply_audio_operation(
                current_clip, operation, current_secondary
            ),
        )

    return encode_wav(clip)


def render_audio(
    primary_bytes: bytes,
    operations: list[dict[str, Any]],
    secondary_bytes: bytes | None,
) -> bytes:
    """Backward-compatible entry point using a private, request-local cache."""
    cache = StageCache(scope="standalone-audio")
    primary_digest = sha256_hex(primary_bytes)
    secondary_digest = (
        sha256_hex(secondary_bytes) if secondary_bytes is not None else None
    )
    return render_audio_staged(
        primary_bytes,
        operations,
        secondary_bytes,
        primary_digest=primary_digest,
        secondary_digest=secondary_digest,
        cache=cache,
    )
