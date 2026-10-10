"""Inference on raw.json-style input: per-window probabilities -> debounced events.

Run as ``python -m pothole_ml.predict --input raw.json --model artifacts/model.pt
--config artifacts/config.json``.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch
from torch import nn

from .config import PipelineConfig
from .dataset import (WindowSet, build_segments, chunks_from_json, features_for, impact_signal,
                      ms_to_iso, windows_from_segments)
from .model import load_model


@dataclass
class Candidate:
    t_ms: int
    lat: float
    lon: float
    cls: int
    score: float
    window_idx: int


@torch.no_grad()
def predict_probs(model: nn.Module, X: np.ndarray, batch_size: int = 256,
                  device: str = "cpu") -> np.ndarray:
    """(W, C, L) features -> (W, n_classes) softmax probabilities."""
    model.eval()
    out = []
    for i in range(0, len(X), batch_size):
        xb = torch.from_numpy(np.ascontiguousarray(X[i:i + batch_size])).to(device)
        out.append(torch.softmax(model(xb), dim=1).cpu().numpy())
    return np.concatenate(out) if out else np.zeros((0, 3), np.float32)


def nms(candidates: list[Candidate], nms_ms: int) -> list[Candidate]:
    """Keep the best-scoring candidate within every ``nms_ms`` neighbourhood."""
    kept: list[Candidate] = []
    for c in sorted(candidates, key=lambda c: -c.score):
        if all(abs(c.t_ms - k.t_ms) >= nms_ms for k in kept):
            kept.append(c)
    return sorted(kept, key=lambda c: c.t_ms)


def refine_event(window_raw: np.ndarray, cfg: PipelineConfig) -> int:
    """Return the sample index of the strongest impact in the window.

    Searching the whole window (not just its center) makes every overlapping window that
    fires on the same event resolve to the same instant, so NMS merges them into one.
    """
    return int(np.argmax(impact_signal(window_raw, cfg.impact_median_window)))


def windows_to_events(probs: np.ndarray, ws: WindowSet, segments_by_id: dict,
                      cfg: PipelineConfig, threshold: float | None = None,
                      nms_ms: int | None = None) -> list[Candidate]:
    threshold = cfg.event_threshold if threshold is None else threshold
    nms_ms = cfg.nms_ms if nms_ms is None else nms_ms
    score = 1.0 - probs[:, 0]
    cands: list[Candidate] = []
    for i in np.flatnonzero(score >= threshold):
        cls = 1 + int(np.argmax(probs[i, 1:]))
        peak = refine_event(ws.X_raw[i], cfg)
        L = cfg.window_len
        # sample time / position: window start = t_center - (L-1)*period/2
        t_start = int(ws.t_center[i]) - ((L - 1) * cfg.sample_period_ms) // 2
        t_ms = t_start + peak * cfg.sample_period_ms
        seg = segments_by_id.get(int(ws.segment_id[i]))
        if seg is not None:
            j = int(np.clip(np.searchsorted(seg.t_ms, t_ms), 0, len(seg) - 1))
            lat, lon = float(seg.latlon[j, 0]), float(seg.latlon[j, 1])
        else:
            lat, lon = float(ws.latlon[i, 0]), float(ws.latlon[i, 1])
        cands.append(Candidate(t_ms, lat, lon, cls, float(score[i]), int(i)))
    return nms(cands, nms_ms)


def events_to_json(events: list[Candidate], cfg: PipelineConfig) -> list[dict]:
    return [{"timestamp": e.t_ms, "iso": ms_to_iso(e.t_ms), "latitude": round(e.lat, 7),
             "longitude": round(e.lon, 7), "label": cfg.class_names[e.cls],
             "score": round(e.score, 4)} for e in events]


def predict_json(data, model: nn.Module, cfg: PipelineConfig, stride: int | None = None,
                 threshold: float | None = None, nms_ms: int | None = None,
                 include_windows: bool = True) -> dict:
    chunks = chunks_from_json(data)
    segments = build_segments(chunks, cfg.sample_period_ms, cfg.gap_tolerance_ms)
    short = [s for s in segments if len(s) < cfg.window_len]
    if short:
        print(f"[warn] {len(short)} segment(s) shorter than window_len={cfg.window_len} skipped",
              file=sys.stderr)
    ws = windows_from_segments(segments, [], cfg, stride)
    X = features_for(ws, cfg)
    probs = predict_probs(model, X)
    events = windows_to_events(probs, ws, {s.segment_id: s for s in segments}, cfg, threshold, nms_ms)
    out = {
        "events": events_to_json(events, cfg),
        "summary": {"chunks": len(chunks), "segments": len(segments), "windows": len(ws),
                    "events": len(events)},
    }
    if include_windows:
        out["windows"] = [
            {"t_ms": int(t), **{f"p_{name}": round(float(p), 4)
                                for name, p in zip(cfg.class_names, row)}}
            for t, row in zip(ws.t_center, probs)
        ]
    return out


def predict_file(input_path: Path, model_path: Path, config_path: Path, **kw) -> dict:
    cfg = PipelineConfig.load(config_path)
    model = load_model(model_path, cfg)
    return predict_json(json.loads(Path(input_path).read_text()), model, cfg, **kw)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Detect potholes / bumps in raw.json data")
    ap.add_argument("--input", type=Path, required=True, help="raw.json array or a single chunk object")
    ap.add_argument("--model", type=Path, default=Path("artifacts/model.pt"))
    ap.add_argument("--config", type=Path, default=Path("artifacts/config.json"))
    ap.add_argument("--output", type=Path, help="write JSON here instead of stdout")
    ap.add_argument("--stride", type=int)
    ap.add_argument("--threshold", type=float)
    ap.add_argument("--nms-ms", type=int)
    ap.add_argument("--no-windows", action="store_true", help="omit per-window probabilities")
    args = ap.parse_args(argv)

    result = predict_file(args.input, args.model, args.config, stride=args.stride,
                          threshold=args.threshold, nms_ms=args.nms_ms,
                          include_windows=not args.no_windows)
    text = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(text + "\n")
        print(f"{result['summary']} -> {args.output}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
