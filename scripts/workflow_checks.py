#!/usr/bin/env python3
"""Run bounded production workflow checks against an ephemeral HTTP server."""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import io
import json
import math
import struct
import sys
import tempfile
import threading
import wave
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from avprovenance.http_api import ProvenanceHTTPServer, build_http_server


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_wav(
    *,
    frequency: float,
    duration_seconds: float = 0.6,
    sample_rate: int = 8000,
    amplitude: int = 9000,
) -> bytes:
    frame_count = int(duration_seconds * sample_rate)
    values = [
        int(amplitude * math.sin(2 * math.pi * frequency * index / sample_rate))
        for index in range(frame_count)
    ]
    payload = io.BytesIO()
    with wave.open(payload, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(struct.pack(f"<{len(values)}h", *values))
    return payload.getvalue()


def make_y4m(
    *,
    width: int = 64,
    height: int = 36,
    frame_count: int = 5,
    rate_numerator: int = 12,
    rate_denominator: int = 1,
) -> bytes:
    header = (
        "YUV4MPEG2 "
        f"W{width} H{height} "
        f"F{rate_numerator}:{rate_denominator} "
        "Ip C420jpeg\n"
    ).encode("ascii")
    chroma_width = width // 2
    chroma_height = height // 2
    parts = [header]
    for frame_index in range(frame_count):
        parts.append(b"FRAME\n")
        y_plane = bytes(
            (x * 3 + y * 5 + frame_index * 11) % 256
            for y in range(height)
            for x in range(width)
        )
        u_plane = bytes(
            (96 + x * 2 + frame_index * 3) % 256
            for _y in range(chroma_height)
            for x in range(chroma_width)
        )
        v_plane = bytes(
            (144 + y * 2 + frame_index * 5) % 256
            for y in range(chroma_height)
            for _x in range(chroma_width)
        )
        parts.extend((y_plane, u_plane, v_plane))
    return b"".join(parts)


class HTTPClient:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        connection = http.client.HTTPConnection(
            self.host,
            self.port,
            timeout=15,
        )
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            value = json.loads(raw.decode("utf-8"))
            return response.status, value
        finally:
            connection.close()

    def upload(self, filename: str, data: bytes) -> dict[str, Any]:
        status, body = self.request(
            "POST",
            "/v1/assets",
            {
                "filename": filename,
                "data_base64": base64.b64encode(data).decode("ascii"),
            },
        )
        require_equal(status, 201, "asset upload status")
        return body

    def create_rendition(self, payload: dict[str, Any]) -> dict[str, Any]:
        status, body = self.request("POST", "/v1/renditions", payload)
        require_equal(status, 201, "rendition creation status")
        return body


@contextmanager
def running_server() -> Iterator[tuple[HTTPClient, ProvenanceHTTPServer]]:
    with tempfile.TemporaryDirectory(prefix="avprovenance-check-") as temporary:
        server = build_http_server("127.0.0.1", 0, Path(temporary))
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        thread.start()
        host, port = server.server_address[:2]
        try:
            yield HTTPClient(str(host), int(port)), server
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


def require_equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run_ingest_media_asset(client: HTTPClient) -> dict[str, Any]:
    health_status, health = client.request("GET", "/health")
    require_equal(health_status, 200, "health status")
    require_equal(health["status"], "ok", "health payload")

    audio = make_wav(frequency=330)
    asset = client.upload("tone-source.wav", audio)
    require_equal(asset["kind"], "audio", "asset kind")
    require_equal(asset["sha256"], _sha256(audio), "asset digest")
    require_equal(asset["descriptor"]["container"], "wav", "asset container")
    require("content_key" not in asset, "public asset exposed an internal content key")

    get_status, fetched = client.request("GET", f"/v1/assets/{asset['id']}")
    require_equal(get_status, 200, "asset get status")
    require_equal(fetched["sha256"], asset["sha256"], "asset read digest")

    bad_status, bad_body = client.request(
        "POST",
        "/v1/assets",
        {
            "filename": "unknown.bin",
            "data_base64": base64.b64encode(b"not-media").decode("ascii"),
        },
    )
    require_equal(bad_status, 415, "unsupported media status")
    require_equal(
        bad_body["error"]["code"],
        "UNSUPPORTED_MEDIA",
        "unsupported media code",
    )
    return {
        "workflow": "ingest-media-asset",
        "asset_id": asset["id"],
        "kind": asset["kind"],
        "digest": asset["sha256"],
    }


def run_execute_rendition(client: HTTPClient) -> dict[str, Any]:
    primary_bytes = make_wav(frequency=440, amplitude=10000)
    secondary_bytes = make_wav(frequency=220, amplitude=7000)
    primary = client.upload("primary-tone.wav", primary_bytes)
    secondary = client.upload("secondary-tone.wav", secondary_bytes)

    audio_rendition = client.create_rendition(
        {
            "primary_asset_id": primary["id"],
            "secondary_asset_id": secondary["id"],
            "output_name": "mixed-tone.wav",
            "operations": [
                {
                    "type": "media_trim",
                    "start_seconds": 0.05,
                    "duration_seconds": 0.3,
                },
                {"type": "audio_gain", "db": -3},
                {
                    "type": "audio_mix",
                    "secondary_gain": -6,
                    "primary_weight": 0.75,
                    "secondary_weight": 0.4,
                },
                {"type": "audio_normalize", "target_peak": 0.8},
            ],
        }
    )
    require_equal(audio_rendition["status"], "completed", "audio rendition status")
    history = [item["to"] for item in audio_rendition["transitions"]]
    require_equal(
        history,
        ["queued", "running", "completed"],
        "audio state history",
    )
    require(
        audio_rendition["output_asset"]["descriptor"]["container"] == "wav",
        "audio output descriptor was not WAV",
    )

    video_bytes = make_y4m()
    video = client.upload("source-frames.y4m", video_bytes)
    video_rendition = client.create_rendition(
        {
            "primary_asset_id": video["id"],
            "output_name": "cropped-frames.y4m",
            "operations": [
                {
                    "type": "video_crop",
                    "x": 8,
                    "y": 6,
                    "width": 32,
                    "height": 18,
                },
                {"type": "video_luma_offset", "amount": 12},
                {"type": "video_mirror"},
            ],
        }
    )
    require_equal(video_rendition["status"], "completed", "video rendition status")
    properties = video_rendition["output_asset"]["descriptor"]["properties"]
    require_equal(properties["width"], 32, "video output width")
    require_equal(properties["height"], 18, "video output height")

    lineage_status, lineage = client.request(
        "GET",
        f"/v1/renditions/{audio_rendition['id']}/lineage",
    )
    require_equal(lineage_status, 200, "lineage status")
    require_equal(len(lineage["parents"]), 2, "lineage parent count")
    require_equal(
        lineage["edge"]["plan_digest"],
        audio_rendition["plan_digest"],
        "lineage plan digest",
    )
    invalid_status, invalid_body = client.request(
        "POST",
        "/v1/renditions",
        {
            "primary_asset_id": primary["id"],
            "output_name": "invalid.wav",
            "operations": [
                {
                    "type": "video_crop",
                    "x": 0,
                    "y": 0,
                    "width": 2,
                    "height": 2,
                }
            ],
        },
    )
    require_equal(invalid_status, 422, "invalid plan status")
    require_equal(invalid_body["error"]["code"], "PLAN_INVALID", "invalid plan code")
    return {
        "workflow": "execute-rendition",
        "audio_rendition_id": audio_rendition["id"],
        "video_rendition_id": video_rendition["id"],
        "audio_history": history,
        "invalid_plan_code": invalid_body["error"]["code"],
        "video_dimensions": [
            properties["width"],
            properties["height"],
        ],
    }


def run_verify_reproduction(
    client: HTTPClient,
    server: ProvenanceHTTPServer,
) -> dict[str, Any]:
    video_bytes = make_y4m(frame_count=7)
    video = client.upload("verification-source.y4m", video_bytes)
    rendition = client.create_rendition(
        {
            "primary_asset_id": video["id"],
            "output_name": "verification-output.y4m",
            "operations": [
                {"type": "media_trim", "start_frame": 1, "frame_count": 4},
                {
                    "type": "video_crop",
                    "x": 4,
                    "y": 4,
                    "width": 40,
                    "height": 20,
                },
                {"type": "video_luma_offset", "amount": -9},
                {"type": "video_mirror"},
            ],
        }
    )
    check_status, check = client.request(
        "POST",
        f"/v1/renditions/{rendition['id']}/integrity-checks",
        {},
    )
    require_equal(check_status, 201, "integrity check status")
    require_equal(check["status"], "verified", "integrity result")
    require_equal(
        check["expected_digest"],
        check["reproduced_digest"],
        "integrity digest equality",
    )
    require_equal(
        check["expected_descriptor"],
        check["reproduced_descriptor"],
        "integrity descriptor equality",
    )

    lineage_status, lineage = client.request(
        "GET",
        f"/v1/renditions/{rendition['id']}/lineage",
    )
    require_equal(lineage_status, 200, "verification lineage status")
    require_equal(len(lineage["parents"]), 1, "verification parent count")

    output_asset_id = rendition["output_asset"]["id"]

    def drift_metadata(state: dict[str, Any]) -> None:
        state["assets"][output_asset_id]["sha256"] = "0" * 64

    server.application.state_file.mutate(drift_metadata)
    mismatch_status, mismatch = client.request(
        "POST",
        f"/v1/renditions/{rendition['id']}/integrity-checks",
        {},
    )
    require_equal(mismatch_status, 409, "mismatch status")
    require_equal(
        mismatch["error"]["code"],
        "INTEGRITY_MISMATCH",
        "mismatch error code",
    )
    require_equal(
        mismatch["error"]["details"]["audit"]["status"],
        "mismatch",
        "persisted mismatch status",
    )
    return {
        "workflow": "verify-reproduction",
        "rendition_id": rendition["id"],
        "integrity_check_id": check["id"],
        "digest": check["reproduced_digest"],
        "mismatch_status": mismatch["error"]["details"]["audit"]["status"],
    }


def run_shared_stage_reuse(
    client: HTTPClient,
    server: ProvenanceHTTPServer,
) -> dict[str, Any]:
    # Two video renditions that share an identical trim+crop prefix and only
    # differ in their tail operation.
    source = make_y4m(width=96, height=54, frame_count=9)
    asset = client.upload("shared-source.y4m", source)

    prefix = [
        {"type": "media_trim", "start_frame": 1, "frame_count": 6},
        {"type": "video_crop", "x": 4, "y": 4, "width": 64, "height": 32},
    ]
    ending_a = [{"type": "video_luma_offset", "amount": 10}]
    ending_b = [{"type": "video_mirror"}]

    rendition_a = client.create_rendition(
        {
            "primary_asset_id": asset["id"],
            "output_name": "tail-a.y4m",
            "operations": prefix + ending_a,
        }
    )
    rendition_b = client.create_rendition(
        {
            "primary_asset_id": asset["id"],
            "output_name": "tail-b.y4m",
            "operations": prefix + ending_b,
        }
    )
    require_equal(rendition_a["status"], "completed", "tail A status")
    require_equal(rendition_b["status"], "completed", "tail B status")
    require(
        rendition_a["output_asset"]["sha256"]
        != rendition_b["output_asset"]["sha256"],
        "different endings must produce different outputs",
    )

    production = server.application.metrics_snapshot()["production"]
    require(
        production["reused"] > 0,
        "the second rendition must reuse shared production stages",
    )
    require(
        production["computed"] > 0 and production["failed"] == 0,
        "production must record real computations and no failures",
    )

    # A different secondary mix can never borrow another mix's voice even with
    # the same primary and the same operation name.
    primary_audio = make_wav(frequency=440, amplitude=10000)
    secondary_one = make_wav(frequency=220, amplitude=7000)
    secondary_two = make_wav(frequency=330, amplitude=7000)
    pa = client.upload("mix-primary.wav", primary_audio)
    s1 = client.upload("mix-secondary-1.wav", secondary_one)
    s2 = client.upload("mix-secondary-2.wav", secondary_two)
    mix_plan = [
        {"type": "media_trim", "start_seconds": 0.05, "duration_seconds": 0.3},
        {
            "type": "audio_mix",
            "secondary_gain": -6,
            "primary_weight": 0.75,
            "secondary_weight": 0.4,
        },
        {"type": "audio_normalize", "target_peak": 0.8},
    ]
    mix_one = client.create_rendition(
        {
            "primary_asset_id": pa["id"],
            "secondary_asset_id": s1["id"],
            "output_name": "mix-1.wav",
            "operations": mix_plan,
        }
    )
    mix_two = client.create_rendition(
        {
            "primary_asset_id": pa["id"],
            "secondary_asset_id": s2["id"],
            "output_name": "mix-2.wav",
            "operations": mix_plan,
        }
    )
    require(
        mix_one["output_asset"]["sha256"]
        != mix_two["output_asset"]["sha256"],
        "distinct secondary mixes must not share an output",
    )

    # Concurrent integrity checks in one round share recomputation, yet each
    # derives its own result from the freshly read sources.
    barrier = threading.Barrier(3)
    results: dict[int, tuple[int, dict[str, Any]]] = {}

    def verify(index: int, rendition_id: str) -> None:
        barrier.wait()
        results[index] = client.request(
            "POST",
            f"/v1/renditions/{rendition_id}/integrity-checks",
            {},
        )

    threads = [
        threading.Thread(
            target=verify, args=(0, rendition_a["id"])
        ),
        threading.Thread(
            target=verify, args=(1, rendition_b["id"])
        ),
        threading.Thread(
            target=verify, args=(2, rendition_a["id"])
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    require_equal(len(results), 3, "all parallel checks returned")
    for index, (status, body) in sorted(results.items()):
        require_equal(status, 201, f"parallel check {index} status")
        require_equal(body["status"], "verified", f"parallel check {index} result")

    snapshot = server.application.metrics_snapshot()
    # The round tears down once every concurrent check completes.
    require_equal(
        snapshot["active_verification_rounds"],
        [],
        "verification round caches are discarded after the round",
    )

    # A later, sequential check opens a fresh round and recomputes from source
    # rather than borrowing the prior round's (or production's) stages.
    follow_status, follow = client.request(
        "POST",
        f"/v1/renditions/{rendition_a['id']}/integrity-checks",
        {},
    )
    require_equal(follow_status, 201, "sequential follow-up check status")
    require_equal(follow["status"], "verified", "sequential follow-up result")
    after = server.application.metrics_snapshot()
    require(
        after["production"]["reused"] > 0,
        "production reuse remains observable",
    )

    metrics_status, metrics = client.request("GET", "/v1/metrics")
    require_equal(metrics_status, 200, "metrics status")
    require(
        "production" in metrics and "by_stage" in metrics["production"],
        "metrics expose per-stage execution counts",
    )
    return {
        "workflow": "shared-stage-reuse",
        "production_reused": snapshot["production"]["reused"],
        "production_computed": snapshot["production"]["computed"],
        "parallel_verified": all(
            body["status"] == "verified" for _, body in results.values()
        ),
        "distinct_mix_outputs": True,
        "metrics_endpoint": metrics_status == 200,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "workflow",
        choices=(
            "ingest-media-asset",
            "execute-rendition",
            "verify-reproduction",
            "shared-stage-reuse",
        ),
    )
    args = parser.parse_args()

    with running_server() as (client, server):
        if args.workflow == "ingest-media-asset":
            result = run_ingest_media_asset(client)
        elif args.workflow == "execute-rendition":
            result = run_execute_rendition(client)
        elif args.workflow == "verify-reproduction":
            result = run_verify_reproduction(client, server)
        else:
            result = run_shared_stage_reuse(client, server)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
