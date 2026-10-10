"""Synthetic drive generator in the server's exact raw.json / labels.json schema.

Used to verify the pipeline end-to-end before real data exists. Run as
``python -m pothole_ml.synth --out-dir data/synth --minutes 10``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from .dataset import ms_to_iso

FS = 40
PERIOD_MS = 1000 // FS
CHUNK = 80
G = 9.81


def random_rotation(rng: np.random.Generator) -> np.ndarray:
    q = rng.normal(size=4)
    q /= np.linalg.norm(q)
    a, b, c, d = q
    return np.array([
        [a * a + b * b - c * c - d * d, 2 * (b * c - a * d), 2 * (b * d + a * c)],
        [2 * (b * c + a * d), a * a - b * b + c * c - d * d, 2 * (c * d - a * b)],
        [2 * (b * d - a * c), 2 * (c * d + a * b), a * a - b * b - c * c + d * d],
    ])


def _half_sine_pulse(n: int) -> np.ndarray:
    """Negative then positive half-sine over n samples (a wheel dropping into a hole)."""
    t = np.linspace(0, 2 * np.pi, n, endpoint=False)
    return -np.sin(t)


def synth_session(rng: np.random.Generator, minutes: float = 10.0, n_potholes: int = 60,
                  n_bumps: int = 60, drop_rate: float = 0.05,
                  reaction_ms: tuple[int, int] = (300, 1200), start_ms: int | None = None,
                  lat0: float = 54.6872, lon0: float = 25.2797) -> tuple[list[dict], list[dict]]:
    """Return (chunks, labels) as plain JSON-ready dicts."""
    n = int(minutes * 60 * FS)
    n -= n % CHUNK
    start_ms = int(start_ms if start_ms is not None else 1_791_630_000_000)
    t_ms = start_ms + PERIOD_MS * np.arange(n, dtype=np.int64)

    # vehicle frame: x forward, y lateral, z up
    acc = rng.normal(0.0, 0.3, size=(n, 3))
    sway_t = np.arange(n) / FS
    acc[:, 1] += 0.4 * np.sin(2 * np.pi * 0.2 * sway_t + rng.uniform(0, 2 * np.pi))
    acc[:, 2] += G

    # unlabeled distractors: braking ramps (horizontal) and rough-road bursts
    for _ in range(max(1, int(minutes * 2))):
        s = rng.integers(0, n - FS)
        acc[s:s + FS, 0] += np.linspace(0, rng.uniform(2.0, 3.0), FS) * rng.choice([-1, 1])
    for _ in range(max(1, int(minutes))):
        s = rng.integers(0, n - 4 * FS)
        ln = rng.integers(2 * FS, 4 * FS)
        acc[s:s + ln] += rng.normal(0.0, 1.0, size=(ln, 3))

    # labeled events, kept at least 2 s apart and away from the edges
    pulse_n = 6  # ~150 ms
    kinds = [1] * n_potholes + [2] * n_bumps
    rng.shuffle(kinds)
    times = []
    margin = 3 * FS
    for _ in range(10_000):
        if len(times) == len(kinds):
            break
        c = int(rng.integers(margin, n - margin))
        if all(abs(c - t) >= 2 * FS for t in times):
            times.append(c)
    events = sorted(zip(times, kinds))
    labels = []
    for c, cls in events:
        amp = rng.uniform(4.0, 8.0)
        pulse = _half_sine_pulse(pulse_n) * amp
        if cls == 2:
            pulse = -pulse
        acc[c:c + pulse_n, 2] += pulse
        acc[c:c + pulse_n, 0] += rng.uniform(-0.5, 0.5) * amp * np.abs(pulse) / amp
        react = int(rng.integers(reaction_ms[0], reaction_ms[1] + 1))
        labels.append({"t_event_ms": int(t_ms[c]), "t_label_ms": int(t_ms[c] + react), "cls": cls})

    # random phone mounting
    R = random_rotation(rng)
    acc_phone = acc @ R.T

    # GPS random walk at 1 Hz
    fixes = n // FS + 1
    walk = rng.normal(0.0, 1e-4, size=(fixes, 2)).cumsum(axis=0)
    latlon = np.array([lat0, lon0]) + walk[np.arange(n) // FS]

    chunks = []
    for s in range(0, n, CHUNK):
        if rng.uniform() < drop_rate:
            continue
        samples = [{"x": round(float(acc_phone[i, 0]), 6), "y": round(float(acc_phone[i, 1]), 6),
                    "z": round(float(acc_phone[i, 2]), 6), "latitude": round(float(latlon[i, 0]), 8),
                    "longitude": round(float(latlon[i, 1]), 8)} for i in range(s, s + CHUNK)]
        chunks.append({"started_at": ms_to_iso(int(t_ms[s])), "samples": samples})

    label_rows = []
    for lab in labels:
        i = int((lab["t_label_ms"] - start_ms) // PERIOD_MS)
        i = min(max(i, 0), n - 1)
        label_rows.append({"timestamp": lab["t_label_ms"], "latitude": round(float(latlon[i, 0]), 8),
                           "longitude": round(float(latlon[i, 1]), 8),
                           "label": "pothole" if lab["cls"] == 1 else "bump",
                           "_t_event_ms": lab["t_event_ms"]})
    return chunks, label_rows


def write_session(out_dir: Path, chunks: list[dict], labels: list[dict]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "raw.json").write_text(json.dumps(chunks))
    public = [{k: v for k, v in l.items() if not k.startswith("_")} for l in labels]
    (out_dir / "labels.json").write_text(json.dumps(public, indent=2))
    truth = [{"t_event_ms": l["_t_event_ms"], "label": l["label"]} for l in labels]
    (out_dir / "truth.json").write_text(json.dumps(truth, indent=2))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate a synthetic drive")
    ap.add_argument("--out-dir", type=Path, default=Path("data/synth"))
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--potholes", type=int, default=60)
    ap.add_argument("--bumps", type=int, default=60)
    ap.add_argument("--drop-rate", type=float, default=0.05)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)
    rng = np.random.default_rng(args.seed)
    chunks, labels = synth_session(rng, args.minutes, args.potholes, args.bumps, args.drop_rate)
    write_session(args.out_dir, chunks, labels)
    print(f"wrote {len(chunks)} chunks and {len(labels)} labels to {args.out_dir}")


if __name__ == "__main__":
    main()
