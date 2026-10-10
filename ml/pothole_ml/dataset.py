"""Data pipeline: raw.json + labels.json -> windowed, featurized training set.

Steps:
  1. ``load_chunks``      parse the server's raw.json (array of chunks, or one chunk object)
  2. ``build_segments``   stitch contiguous chunks into continuous 40 Hz segments
  3. ``load_labels``      parse labels.json
  4. ``align_labels``     move each human label back to the actual impact peak
  5. ``make_windows``     slide fixed-length windows over each segment and label them
  6. ``window_features``  orientation-invariant channels, globally scaled

Run as ``python -m pothole_ml.dataset --raw ... --labels ... --out data/dataset.npz``.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy.ndimage import median_filter

from . import CLASS_NAMES
from .config import PipelineConfig

LABEL_TO_CLASS = {name: i for i, name in enumerate(CLASS_NAMES)}  # none=0, pothole=1, bump=2


# ----------------------------------------------------------------------------- data types

@dataclass
class Chunk:
    start_ms: int
    acc: np.ndarray      # (n, 3) float32, raw m/s^2 incl. gravity
    latlon: np.ndarray   # (n, 2) float64


@dataclass
class Segment:
    segment_id: int
    t_ms: np.ndarray     # (N,) int64 unix ms per sample
    acc: np.ndarray      # (N, 3) float32
    latlon: np.ndarray   # (N, 2) float64

    @property
    def start_ms(self) -> int:
        return int(self.t_ms[0])

    @property
    def end_ms(self) -> int:
        return int(self.t_ms[-1])

    def __len__(self) -> int:
        return len(self.t_ms)


@dataclass
class Label:
    t_ms: int
    lat: float
    lon: float
    cls: int             # 1 = pothole, 2 = bump


@dataclass
class Event:
    t_ms: int
    lat: float
    lon: float
    cls: int
    segment_id: int
    sample_idx: int
    peak_value: float
    label_t_ms: int


@dataclass
class AlignmentStats:
    matched: int = 0
    unmatched: int = 0
    offsets_ms: list[int] = field(default_factory=list)   # label_t - event_t

    def summary(self, lookback_ms: int) -> str:
        lines = [f"labels matched={self.matched} unmatched={self.unmatched}"]
        if self.offsets_ms:
            off = np.asarray(self.offsets_ms)
            lines.append(
                f"label - event offset ms: min={off.min()} median={int(np.median(off))} "
                f"max={off.max()}"
            )
            edges = np.arange(-250, lookback_ms + 251, 250)
            hist, _ = np.histogram(off, bins=edges)
            for lo, n in zip(edges[:-1], hist):
                bar = "#" * int(40 * n / max(hist.max(), 1))
                lines.append(f"  [{lo:5d},{lo + 250:5d}) {n:4d} {bar}")
            at_edge = int((off >= lookback_ms - 250).sum())
            if at_edge > max(1, 0.1 * len(off)):
                lines.append(
                    f"  WARNING: {at_edge} offsets sit at the lookback edge; "
                    "increase --lookback-ms or check clock skew (--label-offset-ms)"
                )
        return "\n".join(lines)


@dataclass
class WindowSet:
    X_raw: np.ndarray       # (W, L, 3) float32 raw accelerometer
    y: np.ndarray           # (W,) int8: 0 none, 1 pothole, 2 bump, -1 ignore
    t_center: np.ndarray    # (W,) int64
    latlon: np.ndarray      # (W, 2) float64, at the window center
    group_id: np.ndarray    # (W,) int64
    segment_id: np.ndarray  # (W,) int64
    event_t_ms: np.ndarray  # (W,) int64, -1 if no event is associated
    speed: np.ndarray = None  # (W,) float32 m/s from GPS, 0 when unknown

    FIELDS = ("X_raw", "y", "t_center", "latlon", "group_id", "segment_id", "event_t_ms", "speed")

    def __post_init__(self):
        if self.speed is None:
            self.speed = np.zeros(len(self.y), np.float32)

    def __len__(self) -> int:
        return len(self.y)

    @staticmethod
    def concat(parts: list["WindowSet"]) -> "WindowSet":
        parts = [p for p in parts if len(p)]
        if not parts:
            return WindowSet(
                np.zeros((0, 0, 3), np.float32), np.zeros(0, np.int8), np.zeros(0, np.int64),
                np.zeros((0, 2)), np.zeros(0, np.int64), np.zeros(0, np.int64), np.zeros(0, np.int64),
            )
        return WindowSet(*[np.concatenate([getattr(p, f) for p in parts]) for f in WindowSet.FIELDS])

    def subset(self, idx: np.ndarray) -> "WindowSet":
        return WindowSet(*[getattr(self, f)[idx] for f in WindowSet.FIELDS])


# ----------------------------------------------------------------------------- parsing

def parse_started_at(value) -> int:
    """Chunk start as unix ms. Accepts ISO-8601 (``...Z`` or offset) and int / digit-string ms."""
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if s.lstrip("-").isdigit():
        return int(s)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(round(dt.timestamp() * 1000))


def ms_to_iso(t_ms: int) -> str:
    return datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def chunks_from_json(data) -> list[Chunk]:
    if isinstance(data, dict):
        data = [data]
    chunks = []
    for item in data:
        samples = item.get("samples") or []
        if not samples:
            continue
        acc = np.array([[s["x"], s["y"], s["z"]] for s in samples], dtype=np.float32)
        latlon = np.array([[s["latitude"], s["longitude"]] for s in samples], dtype=np.float64)
        chunks.append(Chunk(parse_started_at(item["started_at"]), acc, latlon))
    return chunks


def load_chunks(path: str | Path) -> list[Chunk]:
    return chunks_from_json(json.loads(Path(path).read_text()))


def load_labels(path: str | Path) -> list[Label]:
    labels = []
    for item in json.loads(Path(path).read_text()):
        name = str(item["label"]).lower()
        if name not in LABEL_TO_CLASS or name == "none":
            raise ValueError(f"unknown label {item['label']!r}")
        labels.append(Label(int(item["timestamp"]), float(item["latitude"]),
                            float(item["longitude"]), LABEL_TO_CLASS[name]))
    return sorted(labels, key=lambda l: l.t_ms)


# ----------------------------------------------------------------------------- segmentation

def build_segments(chunks: list[Chunk], sample_period_ms: int = 25,
                   gap_tolerance_ms: int = 100) -> list[Segment]:
    """Stitch chunks into contiguous segments.

    A chunk continues the current segment when its start is within ``gap_tolerance_ms`` of
    where the previous chunk ended. Per-sample times come from each chunk's own start so
    timer drift never accumulates.
    """
    chunks = sorted(chunks, key=lambda c: c.start_ms)
    segments: list[Segment] = []
    cur: list[Chunk] = []
    seen_starts: set[int] = set()

    def flush():
        if not cur:
            return
        t = np.concatenate([c.start_ms + sample_period_ms * np.arange(len(c.acc), dtype=np.int64)
                            for c in cur])
        segments.append(Segment(len(segments), t,
                                np.concatenate([c.acc for c in cur]),
                                np.concatenate([c.latlon for c in cur])))
        cur.clear()

    for c in chunks:
        if c.start_ms in seen_starts:
            print(f"[warn] duplicate chunk start {ms_to_iso(c.start_ms)} skipped", file=sys.stderr)
            continue
        seen_starts.add(c.start_ms)
        if cur:
            expected = cur[-1].start_ms + sample_period_ms * len(cur[-1].acc)
            if abs(c.start_ms - expected) > gap_tolerance_ms:
                flush()
        cur.append(c)
    flush()
    return segments


# ----------------------------------------------------------------------------- signals / features

def impact_signal(acc: np.ndarray, median_window: int = 21) -> np.ndarray:
    """Orientation-free impact strength: |a| minus its moving median, absolute value."""
    mag = np.linalg.norm(acc.astype(np.float64), axis=1)
    if len(mag) < 2:
        return np.zeros_like(mag)
    base = median_filter(mag, size=min(median_window, len(mag)), mode="nearest")
    return np.abs(mag - base)


def speed_series(latlon: np.ndarray, t_ms: np.ndarray, max_mps: float = 50.0) -> np.ndarray:
    """Per-sample ground speed (m/s) from the GPS fixes carried by the samples.

    Phones update the position about once a second, so consecutive samples share a fix.
    The speed between two consecutive distinct fixes is assigned to every sample of that
    interval; the last known speed is held to the end. Glitches are clipped to ``max_mps``.
    Unknown (no second fix yet) is 0.
    """
    n = len(t_ms)
    out = np.zeros(n, np.float32)
    if n < 2:
        return out
    change = np.flatnonzero(np.any(np.diff(latlon, axis=0) != 0, axis=1)) + 1
    fix_idx = np.concatenate([[0], change])
    if len(fix_idx) < 2:
        return out
    lat = np.radians(latlon[fix_idx, 0])
    lon = np.radians(latlon[fix_idx, 1])
    earth_r = 6_371_000.0
    north = np.diff(lat) * earth_r
    east = np.diff(lon) * earth_r * np.cos((lat[1:] + lat[:-1]) / 2)
    dt = np.diff(t_ms[fix_idx]) / 1000.0
    v = np.clip(np.hypot(north, east) / np.maximum(dt, 1e-3), 0.0, max_mps)
    for k in range(len(v)):
        out[fix_idx[k]:fix_idx[k + 1]] = v[k]
    out[fix_idx[-1]:] = v[-1]
    return out


def window_features(windows: np.ndarray, scale: float = 1.0,
                    speed: np.ndarray | None = None, speed_scale: float = 15.0) -> np.ndarray:
    """(W, L, 3) raw accelerometer -> (W, C, L) orientation-invariant channels.

    Channels: signed vertical component relative to the window's gravity estimate,
    horizontal magnitude (mean-removed), and total magnitude minus gravity. With ``speed``
    (W,) a fourth channel holds the window's speed / ``speed_scale``, constant along L.
    """
    w = windows.astype(np.float32)
    g = w.mean(axis=1, keepdims=True)                         # (W,1,3)
    g_norm = np.linalg.norm(g, axis=2, keepdims=True) + 1e-6  # (W,1,1)
    g_hat = g / g_norm
    vert_full = (w * g_hat).sum(axis=2, keepdims=True)        # (W,L,1) projection on gravity
    vert = vert_full[..., 0] - g_norm[..., 0]
    horiz_vec = w - vert_full * g_hat
    horiz = np.linalg.norm(horiz_vec, axis=2)
    horiz = horiz - horiz.mean(axis=1, keepdims=True)
    mag = np.linalg.norm(w, axis=2) - g_norm[..., 0]
    feats = np.stack([vert, horiz, mag], axis=1) / float(scale)
    if speed is not None:
        s = np.broadcast_to((np.asarray(speed, np.float32) / float(speed_scale))[:, None, None],
                            (len(w), 1, w.shape[1]))
        feats = np.concatenate([feats, s], axis=1)
    return feats.astype(np.float32)


def features_for(ws: "WindowSet", cfg: PipelineConfig) -> np.ndarray:
    """Model input for a window set, honouring the config's feature switches."""
    if len(ws) == 0:
        return np.zeros((0, cfg.in_channels, cfg.window_len), np.float32)
    return window_features(ws.X_raw, cfg.scale, ws.speed if cfg.use_speed else None,
                           cfg.speed_scale_mps)


def estimate_scale(windows: np.ndarray) -> float:
    """Median per-window std of |a|; makes the model amplitude-aware but unit-agnostic."""
    if len(windows) == 0:
        return 1.0
    mag = np.linalg.norm(windows.astype(np.float32), axis=2)
    s = float(np.median(mag.std(axis=1)))
    return s if s > 1e-6 else 1.0


# ----------------------------------------------------------------------------- label alignment

def align_labels(labels: list[Label], segments: list[Segment],
                 cfg: PipelineConfig) -> tuple[list[Event], AlignmentStats]:
    """Move each label to the actual impact within its lookback window."""
    stats = AlignmentStats()
    events: list[Event] = []
    impact = {s.segment_id: impact_signal(s.acc, cfg.impact_median_window) for s in segments}
    claimed: dict[int, list[int]] = {s.segment_id: [] for s in segments}

    for lab in sorted(labels, key=lambda l: l.t_ms):
        t = lab.t_ms + cfg.label_offset_ms
        if cfg.align_method == "fixed":
            lo = hi = t - cfg.fixed_offset_ms
            lo -= cfg.sample_period_ms
            hi += cfg.sample_period_ms
        else:
            lo, hi = t - cfg.lookback_ms, t + cfg.lookahead_ms

        best = None  # (peak_value, segment, idx)
        for seg in segments:
            if seg.end_ms < lo or seg.start_ms > hi:
                continue
            mask = (seg.t_ms >= lo) & (seg.t_ms <= hi)
            for ct in claimed[seg.segment_id]:
                mask &= np.abs(seg.t_ms - ct) >= cfg.min_event_separation_ms
            if not mask.any():
                continue
            idxs = np.flatnonzero(mask)
            if cfg.align_method == "fixed":
                i = idxs[np.argmin(np.abs(seg.t_ms[idxs] - (t - cfg.fixed_offset_ms)))]
            else:
                i = idxs[np.argmax(impact[seg.segment_id][idxs])]
            val = float(impact[seg.segment_id][i])
            if best is None or val > best[0]:
                best = (val, seg, int(i))

        if best is None:
            stats.unmatched += 1
            continue
        val, seg, i = best
        claimed[seg.segment_id].append(int(seg.t_ms[i]))
        events.append(Event(int(seg.t_ms[i]), float(seg.latlon[i, 0]), float(seg.latlon[i, 1]),
                            lab.cls, seg.segment_id, i, val, lab.t_ms))
        stats.matched += 1
        stats.offsets_ms.append(int(t - seg.t_ms[i]))

    events.sort(key=lambda e: e.t_ms)
    return events, stats


# ----------------------------------------------------------------------------- windowing

def window_starts(n: int, window_len: int, stride: int) -> np.ndarray:
    if n < window_len:
        return np.zeros(0, dtype=np.int64)
    return np.arange(0, n - window_len + 1, stride, dtype=np.int64)


def make_windows(segment: Segment, events: list[Event], cfg: PipelineConfig,
                 stride: int | None = None) -> WindowSet:
    """Slide windows over one segment and label them from the aligned events."""
    L = cfg.window_len
    stride = stride or cfg.stride
    starts = window_starts(len(segment), L, stride)
    W = len(starts)
    if W == 0:
        return WindowSet.concat([])

    idx = starts[:, None] + np.arange(L)[None, :]
    X_raw = segment.acc[idx]                                   # (W, L, 3)
    t_start = segment.t_ms[starts]
    t_end = segment.t_ms[starts + L - 1]
    t_center = (t_start + t_end) // 2
    latlon = segment.latlon[starts + L // 2]
    speed = np.median(speed_series(segment.latlon, segment.t_ms, cfg.speed_max_mps)[idx], axis=1).astype(np.float32)

    y = np.zeros(W, dtype=np.int8)
    event_t = np.full(W, -1, dtype=np.int64)
    anchor = t_center.copy()

    seg_events = [e for e in events if e.segment_id == segment.segment_id]
    if seg_events:
        ev_t = np.array([e.t_ms for e in seg_events], dtype=np.int64)
        ev_c = np.array([e.cls for e in seg_events], dtype=np.int8)
        d = np.abs(t_center[:, None] - ev_t[None, :])          # (W, E)
        nearest = d.argmin(axis=1)
        d_near = d[np.arange(W), nearest]

        inside = ((ev_t[None, :] >= (t_start - cfg.ignore_margin_ms)[:, None]) &
                  (ev_t[None, :] <= (t_end + cfg.ignore_margin_ms)[:, None])).any(axis=1)
        positive = d_near <= cfg.center_tol_ms

        y[inside] = -1
        y[positive] = ev_c[nearest[positive]]
        touched = inside | positive
        event_t[touched] = ev_t[nearest[touched]]
        anchor[touched] = ev_t[nearest[touched]]

    # Blocks of absolute time, shared by every session: two phones that drove the same road at
    # the same time keep their windows of one moment in the same split (same pothole, same block).
    group_id = anchor // cfg.group_block_ms
    seg_id = np.full(W, segment.segment_id, dtype=np.int64)
    return WindowSet(X_raw.astype(np.float32), y, t_center, latlon, group_id, seg_id, event_t, speed)


def windows_from_segments(segments: list[Segment], events: list[Event], cfg: PipelineConfig,
                          stride: int | None = None) -> WindowSet:
    return WindowSet.concat([make_windows(s, events, cfg, stride) for s in segments])


def subsample_negatives(ws: WindowSet, neg_ratio: float, cfg: PipelineConfig,
                        seed: int = 0, keep_top_frac: float = 0.2) -> WindowSet:
    """Keep at most ``neg_ratio`` none-windows per positive window, preferring strong impacts."""
    rng = np.random.default_rng(seed)
    pos = np.flatnonzero(ws.y > 0)
    neg = np.flatnonzero(ws.y == 0)
    keep_n = int(neg_ratio * max(len(pos), 1))
    if len(neg) <= keep_n:
        return ws
    peak = np.array([impact_signal(ws.X_raw[i], cfg.impact_median_window).max() for i in neg])
    order = neg[np.argsort(-peak)]
    top = order[: int(keep_top_frac * keep_n)]
    rest = order[int(keep_top_frac * keep_n):]
    chosen = np.concatenate([top, rng.choice(rest, keep_n - len(top), replace=False)])
    keep = np.sort(np.concatenate([pos, np.flatnonzero(ws.y == -1), chosen]))
    return ws.subset(keep)


# ----------------------------------------------------------------------------- build

def load_sessions(raw_paths: list[Path], label_paths: list[Path], cfg: PipelineConfig
                  ) -> tuple[list[Chunk], list[Label], list[Segment], list[Event], AlignmentStats]:
    """Load recordings as independent sessions and align each session's labels to its own data.

    When ``--raw`` and ``--labels`` are given the same number of times, the i-th pair is one
    session (one device / one drive). Sessions are segmented separately, so two phones that
    recorded at the same time in different cars never break each other's segments, and a label
    pressed in one car can only claim an impact in that car's data. With unequal counts all
    files are merged into one session (several sequential files of the same device).
    """
    if len(raw_paths) == len(label_paths):
        pairs = list(zip(raw_paths, label_paths))
    else:
        print(f"[warn] {len(raw_paths)} raw files and {len(label_paths)} label files: merging "
              "all into one session; pass one --labels per --raw to keep devices apart",
              file=sys.stderr)
        pairs = [(raw_paths, label_paths)]

    all_chunks: list[Chunk] = []
    all_labels: list[Label] = []
    all_segments: list[Segment] = []
    all_events: list[Event] = []
    stats = AlignmentStats()
    for raw, lab in pairs:
        raw_list = raw if isinstance(raw, list) else [raw]
        lab_list = lab if isinstance(lab, list) else [lab]
        chunks = [c for p in raw_list for c in load_chunks(p)]
        labels = [l for p in lab_list for l in load_labels(p)]
        segments = build_segments(chunks, cfg.sample_period_ms, cfg.gap_tolerance_ms)
        offset = len(all_segments)
        for seg in segments:
            seg.segment_id += offset
        events, st = align_labels(labels, segments, cfg)
        all_chunks += chunks
        all_labels += labels
        all_segments += segments
        all_events += events
        stats.matched += st.matched
        stats.unmatched += st.unmatched
        stats.offsets_ms += st.offsets_ms
    all_events.sort(key=lambda e: e.t_ms)
    return all_chunks, all_labels, all_segments, all_events, stats


def build_dataset(raw_paths: list[Path], label_paths: list[Path], cfg: PipelineConfig,
                  out_path: Path, neg_ratio: float | None = None, seed: int = 0,
                  dump_alignment: Path | None = None, scale: float | None = None,
                  verbose: bool = True) -> tuple[WindowSet, PipelineConfig, dict]:
    chunks, labels, segments, events, stats = load_sessions(raw_paths, label_paths, cfg)
    ws = windows_from_segments(segments, events, cfg)
    if neg_ratio is not None:
        ws = subsample_negatives(ws, neg_ratio, cfg, seed)

    cfg = cfg.apply_overrides(scale=scale if scale is not None else estimate_scale(ws.X_raw))
    X = features_for(ws, cfg)

    total_min = sum(len(s) for s in segments) * cfg.sample_period_ms / 60000
    counts = {name: int((ws.y == i).sum()) for i, name in enumerate(cfg.class_names)}
    counts["ignore"] = int((ws.y == -1).sum())
    meta = {
        "config": cfg.to_dict(),
        "chunks": len(chunks), "segments": len(segments), "minutes": round(total_min, 2),
        "labels": len(labels), "events": len(events), "sessions": len(raw_paths),
        "alignment": {"matched": stats.matched, "unmatched": stats.unmatched,
                      "offsets_ms": stats.offsets_ms},
        "windows": counts,
        "sources": {"raw": [str(p) for p in raw_paths], "labels": [str(p) for p in label_paths]},
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out_path, X=X, X_raw=ws.X_raw, y=ws.y, t_center=ws.t_center,
                        latlon=ws.latlon, group_id=ws.group_id, segment_id=ws.segment_id,
                        event_t_ms=ws.event_t_ms, speed=ws.speed,
                        events=np.array([[e.t_ms, e.cls, e.segment_id] for e in events],
                                        dtype=np.int64).reshape(-1, 3))
    out_path.with_suffix(".meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    if dump_alignment is not None:
        with open(dump_alignment, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["label_iso", "label_t_ms", "event_iso", "event_t_ms", "offset_ms",
                        "class", "peak_value", "segment_id", "lat", "lon"])
            for e in events:
                w.writerow([ms_to_iso(e.label_t_ms), e.label_t_ms, ms_to_iso(e.t_ms), e.t_ms,
                            e.label_t_ms + cfg.label_offset_ms - e.t_ms, cfg.class_names[e.cls],
                            f"{e.peak_value:.3f}", e.segment_id, f"{e.lat:.7f}", f"{e.lon:.7f}"])

    if verbose:
        print(f"chunks={len(chunks)} segments={len(segments)} minutes={total_min:.1f}")
        print(stats.summary(cfg.lookback_ms))
        print(f"windows: {counts}  (scale={cfg.scale:.3f})")
        print(f"saved {out_path} and {out_path.with_suffix('.meta.json')}")
    return ws, cfg, meta


def load_dataset(path: str | Path) -> dict:
    with np.load(path) as z:
        return {k: z[k] for k in z.files}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build a windowed dataset from raw.json + labels.json")
    ap.add_argument("--raw", type=Path, action="append", required=True, help="raw.json (repeatable)")
    ap.add_argument("--labels", type=Path, action="append", required=True, help="labels.json (repeatable)")
    ap.add_argument("--out", type=Path, default=Path("data/dataset.npz"))
    ap.add_argument("--config", type=Path, help="start from this config.json instead of defaults")
    ap.add_argument("--neg-ratio", type=float, help="keep at most this many none-windows per positive")
    ap.add_argument("--dump-alignment", type=Path, help="write one CSV row per matched label")
    ap.add_argument("--scale", type=float, help="force the global amplitude scale")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--use-speed", action="store_true",
                    help="add GPS-derived speed as a 4th input channel (saved in the config)")
    for name in ("stride", "window_len", "lookback_ms", "lookahead_ms", "label_offset_ms",
                 "fixed_offset_ms", "center_tol_ms", "gap_tolerance_ms"):
        ap.add_argument(f"--{name.replace('_', '-')}", type=int)
    ap.add_argument("--align-method", choices=["peak", "fixed"])
    args = ap.parse_args(argv)

    cfg = PipelineConfig.load(args.config) if args.config else PipelineConfig()
    cfg = cfg.apply_overrides(
        stride=args.stride, window_len=args.window_len, lookback_ms=args.lookback_ms,
        lookahead_ms=args.lookahead_ms, label_offset_ms=args.label_offset_ms,
        fixed_offset_ms=args.fixed_offset_ms, center_tol_ms=args.center_tol_ms,
        gap_tolerance_ms=args.gap_tolerance_ms, align_method=args.align_method,
        use_speed=True if args.use_speed else None,
    )
    build_dataset(args.raw, args.labels, cfg, args.out, neg_ratio=args.neg_ratio, seed=args.seed,
                  dump_alignment=args.dump_alignment, scale=args.scale)


if __name__ == "__main__":
    main()
