"""Streaming detector: the server feeds raw chunks on stdin, events come back on stdout.

Run as ``python -m pothole_ml.serve --model artifacts/model.pt --config artifacts/config.json``.
The Rust server spawns this once and keeps it running; one JSON object per line in each
direction, so loading torch and the model happens once rather than once per chunk.

Requests (one per line):

    {"ping": true}
    {"device": "<ip>", "chunk": {"started_at": "...", "samples": [{"x","y","z","latitude","longitude"}, ...]}}
    {"device": "<ip>", "reset": true}

Replies (one per request, same order):

    {"ok": true, "model": "...", "threshold": 0.75, "window_ms": 1600, "lag_ms": 1100}
    {"device": "<ip>", "events": [{"timestamp", "iso", "latitude", "longitude", "label", "score"}],
     "windows": 23, "cutoff_ms": 1791640000000}
    {"device": "<ip>", "error": "..."}

Every device keeps a rolling buffer of its last few chunks. Each new chunk is scored together
with that buffer, so an impact right at a chunk boundary is seen by a window centred on it. An
event is reported only once its whole window has arrived: events later than
``chunk end - lag_ms`` wait for the next chunk, and events already reported are never repeated.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .config import PipelineConfig
from .dataset import chunks_from_json, parse_started_at
from .model import load_model
from .predict import predict_json

# Chunks kept per device. Three 2 s chunks cover the window (1.6 s) plus the lag on both sides.
BUFFER_CHUNKS = 3


@dataclass
class DeviceState:
    chunks: deque = field(default_factory=lambda: deque(maxlen=BUFFER_CHUNKS))
    emitted_until_ms: int = -1    # events up to and including this instant have been reported
    last_event_ms: int = -10**15  # NMS guard across calls


class StreamingDetector:
    def __init__(self, model_path: Path, config_path: Path, lag_ms: int | None = None):
        self.cfg = PipelineConfig.load(config_path)
        self.model = load_model(model_path, self.cfg)
        self.model_path = str(model_path)
        # Half a window after the event plus the centre tolerance: once this much data follows
        # an impact, some window has it near its centre, so the decision will not change later.
        # With the speed channel, wait one more GPS fix (about 1 s) so the speed at the end of
        # that window is measured rather than held from the previous fix.
        if lag_ms is None:
            lag_ms = self.cfg.window_ms // 2 + self.cfg.center_tol_ms + (1000 if self.cfg.use_speed else 0)
        self.lag_ms = lag_ms
        self.devices: dict[str, DeviceState] = {}

    def info(self) -> dict:
        return {"ok": True, "model": self.model_path, "threshold": self.cfg.event_threshold,
                "nms_ms": self.cfg.nms_ms, "window_ms": self.cfg.window_ms, "lag_ms": self.lag_ms,
                "class_names": list(self.cfg.class_names)}

    def reset(self, device: str) -> None:
        self.devices.pop(device, None)

    def push(self, device: str, chunk: dict) -> dict:
        state = self.devices.setdefault(device, DeviceState())
        parsed = chunks_from_json(chunk)
        if not parsed:
            return {"device": device, "events": [], "windows": 0, "cutoff_ms": state.emitted_until_ms,
                    "warning": "empty chunk"}
        start_ms = parse_started_at(chunk["started_at"])
        end_ms = start_ms + self.cfg.sample_period_ms * len(parsed[0].acc)
        state.chunks.append(chunk)

        result = predict_json(list(state.chunks), self.model, self.cfg, include_windows=False)
        cutoff_ms = max(end_ms - self.lag_ms, state.emitted_until_ms)
        events = []
        for e in result["events"]:
            t = e["timestamp"]
            if t <= state.emitted_until_ms or t > cutoff_ms:
                continue
            if t - state.last_event_ms < self.cfg.nms_ms:
                continue
            state.last_event_ms = t
            events.append(e)
        state.emitted_until_ms = cutoff_ms
        return {"device": device, "events": events, "windows": result["summary"]["windows"],
                "cutoff_ms": cutoff_ms}

    def handle(self, request: dict) -> dict:
        if request.get("ping"):
            return self.info()
        device = str(request.get("device") or "")
        if not device:
            return {"error": "missing device"}
        if request.get("reset"):
            self.reset(device)
            return {"device": device, "ok": True}
        chunk = request.get("chunk")
        if not isinstance(chunk, dict):
            return {"device": device, "error": "missing chunk"}
        return self.push(device, chunk)


def serve(detector: StreamingDetector, stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            reply = detector.handle(request)
        except Exception as error:  # noqa: BLE001 - one bad request must not kill the worker
            device = None
            try:
                device = json.loads(line).get("device")
            except Exception:  # noqa: BLE001
                pass
            reply = {"device": device, "error": f"{type(error).__name__}: {error}"}
            print(f"[error] {reply['error']}", file=sys.stderr, flush=True)
        stdout.write(json.dumps(reply, separators=(",", ":")) + "\n")
        stdout.flush()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Streaming pothole / bump detector (JSON lines on stdin/stdout)")
    ap.add_argument("--model", type=Path, default=Path("artifacts/model.pt"))
    ap.add_argument("--config", type=Path, default=Path("artifacts/config.json"))
    ap.add_argument("--lag-ms", type=int, help="report an event only once this much data follows it")
    args = ap.parse_args(argv)
    detector = StreamingDetector(args.model, args.config, args.lag_ms)
    print(f"[info] detector ready: {detector.info()}", file=sys.stderr, flush=True)
    serve(detector)


if __name__ == "__main__":
    main()
