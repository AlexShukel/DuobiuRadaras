"""Cross-check labels between phones that drove the same road.

When two cars label the same route, a pothole that one driver pressed and the other did not
is a hard negative for the second car, which hurts training. For every label of car A this
tool finds the moment car B passed the same spot and, when B's accelerometer shows a clear
impact there, writes that label into B's set (aligned to B's own peak). Labels are only
transferred when confirmed by B's data, so a bump that B's suspension swallowed stays unlabelled.

Run as ``python -m pothole_ml.crosscheck --raw A/raw.json --labels A/labels.json --raw B/raw.json
--labels B/labels.json --out-dir data/crosschecked``; it writes ``<n>_labels.json`` per session
(original labels plus the transferred ones) for ``pothole_ml.dataset``. The server's files are
never modified.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from .config import PipelineConfig
from .dataset import (Segment, align_labels, build_segments, impact_signal, load_chunks,
                      load_labels, ms_to_iso)

EARTH_R = 6_371_000.0


def distance_m(lat: float, lon: float, lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    la1, la2 = np.radians(lat), np.radians(lats)
    return np.hypot((la2 - la1) * EARTH_R, np.radians(lons - lon) * EARTH_R * np.cos((la1 + la2) / 2))


class Session:
    def __init__(self, raw: Path, labels: Path, cfg: PipelineConfig):
        self.raw_path, self.labels_path = raw, labels
        self.labels_json = json.loads(labels.read_text())
        self.segments: list[Segment] = build_segments(load_chunks(raw), cfg.sample_period_ms, cfg.gap_tolerance_ms)
        self.events, _ = align_labels(load_labels(labels), self.segments, cfg)
        self.lat = np.concatenate([s.latlon[:, 0] for s in self.segments])
        self.lon = np.concatenate([s.latlon[:, 1] for s in self.segments])
        self.t = np.concatenate([s.t_ms for s in self.segments])
        self.impact = np.concatenate([impact_signal(s.acc, cfg.impact_median_window) for s in self.segments])
        self.seg_of = np.concatenate([np.full(len(s), i) for i, s in enumerate(self.segments)])
        self.idx_in_seg = np.concatenate([np.arange(len(s)) for s in self.segments])


def transfer(src: Session, dst: Session, radius_m: float, max_gap_s: float, window_ms: int,
             min_impact: float, own_label_tol_ms: int) -> list[dict]:
    """Labels of ``src`` that ``dst`` should also have, as labels.json entries for ``dst``."""
    added = []
    for e in src.events:
        close = np.flatnonzero(distance_m(e.lat, e.lon, dst.lat, dst.lon) <= radius_m)
        if len(close) == 0:
            continue
        j = close[np.argmin(np.abs(dst.t[close] - e.t_ms))]
        if abs(dst.t[j] - e.t_ms) > max_gap_s * 1000:
            continue
        if any(abs(d.t_ms - dst.t[j]) <= own_label_tol_ms for d in dst.events):
            continue  # dst labelled it too
        if any(abs(a["timestamp"] - dst.t[j]) <= own_label_tol_ms for a in added):
            continue  # already transferred from a neighbouring src label
        win = np.flatnonzero((dst.t >= dst.t[j] - window_ms) & (dst.t <= dst.t[j] + window_ms))
        k = win[np.argmax(dst.impact[win])]
        if dst.impact[k] < min_impact:
            continue  # the other suspension did not feel it: leave unlabelled
        seg = dst.segments[dst.seg_of[k]]
        i = dst.idx_in_seg[k]
        added.append({"timestamp": int(seg.t_ms[i]), "latitude": float(seg.latlon[i, 0]),
                      "longitude": float(seg.latlon[i, 1]), "label": PipelineConfig().class_names[e.cls],
                      "transferred_from": str(src.raw_path.parent.name), "impact": round(float(dst.impact[k]), 3)})
    return added


def main(argv=None):
    ap = argparse.ArgumentParser(description="Transfer confirmed labels between phones on the same route")
    ap.add_argument("--raw", type=Path, action="append", required=True)
    ap.add_argument("--labels", type=Path, action="append", required=True)
    ap.add_argument("--out-dir", type=Path, default=Path("data/crosschecked"))
    ap.add_argument("--radius-m", type=float, default=15.0, help="same spot if within this distance")
    ap.add_argument("--max-gap-s", type=float, default=120.0, help="the other car must pass within this time")
    ap.add_argument("--window-ms", type=int, default=2000, help="search the impact this far around the pass")
    ap.add_argument("--min-impact", type=float, default=3.0, help="|a| - baseline (m/s2) needed to confirm")
    ap.add_argument("--own-label-tol-ms", type=int, default=4000)
    args = ap.parse_args(argv)
    if len(args.raw) != len(args.labels) or len(args.raw) < 2:
        ap.error("give one --labels per --raw, at least two sessions")

    cfg = PipelineConfig()
    sessions = [Session(r, l, cfg) for r, l in zip(args.raw, args.labels)]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for n, dst in enumerate(sessions):
        added = []
        for src in sessions:
            if src is not dst:
                added += transfer(src, dst, args.radius_m, args.max_gap_s, args.window_ms,
                                  args.min_impact, args.own_label_tol_ms)
        added.sort(key=lambda a: a["timestamp"])
        out = args.out_dir / f"{n}_labels.json"
        out.write_text(json.dumps(dst.labels_json + added, indent=2) + "\n")
        print(f"{dst.labels_path}: {len(dst.labels_json)} own labels + {len(added)} transferred -> {out}")
        for a in added:
            print(f"  + {a['label']:7s} {ms_to_iso(a['timestamp'])} impact={a['impact']:.1f} from {a['transferred_from']}",
                  file=sys.stderr)


if __name__ == "__main__":
    main()
