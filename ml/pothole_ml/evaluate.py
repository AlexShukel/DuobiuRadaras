"""Window-level and event-level evaluation, split logic and threshold tuning.

Run as ``python -m pothole_ml.evaluate --dataset data/dataset.npz --model artifacts/model.pt
--config artifacts/config.json --split test`` to re-evaluate a saved model.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from sklearn.metrics import classification_report, confusion_matrix, f1_score
from torch import nn

from .config import PipelineConfig
from .dataset import WindowSet, load_dataset
from .model import load_model
from .predict import Candidate, predict_probs, windows_to_events


# ----------------------------------------------------------------------------- splits

def split_groups(group_id: np.ndarray, y: np.ndarray, val_frac: float = 0.15,
                 test_frac: float = 0.15, seed: int = 0, method: str = "group",
                 t_center: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Group-aware train/val/test window indices (no overlapping windows across splits)."""
    groups, inverse, counts = np.unique(group_id, return_inverse=True, return_counts=True)
    if method == "chrono":
        first_t = np.full(len(groups), np.iinfo(np.int64).max, dtype=np.int64)
        t = t_center if t_center is not None else np.arange(len(group_id))
        np.minimum.at(first_t, inverse, t)
        order = np.argsort(first_t)
    elif method == "group":
        order = np.random.default_rng(seed).permutation(len(groups))
    else:
        raise ValueError(f"unknown split method {method!r}")

    cum = np.cumsum(counts[order]) / counts.sum()
    assign = np.zeros(len(groups), dtype=np.int8)  # 0 train, 1 val, 2 test
    assign[order[cum > 1 - val_frac - test_frac]] = 1
    assign[order[cum > 1 - test_frac]] = 2
    per_window = assign[inverse]
    splits = tuple(np.flatnonzero(per_window == k) for k in range(3))
    for name, idx in zip(("train", "val", "test"), splits):
        present = set(np.unique(y[idx]).tolist()) - {-1}
        missing = {0, 1, 2} - present
        if missing:
            print(f"[warn] split {name} has no windows of class {sorted(missing)}")
    return splits


# ----------------------------------------------------------------------------- metrics

def window_report(y_true: np.ndarray, y_pred: np.ndarray, class_names) -> str:
    labels = list(range(len(class_names)))
    if len(y_true) == 0:
        return "(no labelled windows in this split)"
    rep = classification_report(y_true, y_pred, labels=labels, target_names=class_names,
                                zero_division=0)
    cm = confusion_matrix(y_true, y_pred, labels=labels)
    rows = ["confusion matrix (rows=true, cols=pred): " + " ".join(f"{n:>8s}" for n in class_names)]
    rows += [f"{class_names[i]:>40s} " + " ".join(f"{v:8d}" for v in cm[i]) for i in labels]
    return rep + "\n" + "\n".join(rows)


def event_metrics(true_events: list[tuple[int, int]], pred_events: list[Candidate],
                  match_tol_ms: int, total_minutes: float) -> dict:
    """Greedy time-sorted matching, label-agnostic for detection, then class accuracy."""
    truths = sorted(true_events)
    preds = sorted(pred_events, key=lambda c: c.t_ms)
    used = np.zeros(len(truths), dtype=bool)
    tp = 0
    cls_correct = 0
    conf = np.zeros((2, 2), dtype=int)  # rows true (pothole,bump), cols pred
    for p in preds:
        best, best_d = -1, match_tol_ms + 1
        for j, (t, _) in enumerate(truths):
            if used[j]:
                continue
            d = abs(p.t_ms - t)
            if d <= match_tol_ms and d < best_d:
                best, best_d = j, d
        if best >= 0:
            used[best] = True
            tp += 1
            tc = truths[best][1]
            conf[tc - 1, p.cls - 1] += 1
            cls_correct += int(tc == p.cls)
    fp = len(preds) - tp
    fn = len(truths) - tp
    prec = tp / max(tp + fp, 1)
    rec = tp / max(tp + fn, 1)
    f1 = 2 * prec * rec / max(prec + rec, 1e-9)
    return {
        "true_events": len(truths), "predicted": len(preds), "tp": tp, "fp": fp, "fn": fn,
        "precision": round(prec, 4), "recall": round(rec, 4), "f1": round(f1, 4),
        "class_accuracy": round(cls_correct / max(tp, 1), 4),
        "class_confusion": {"rows": ["pothole", "bump"], "matrix": conf.tolist()},
        "false_alarms_per_min": round(fp / max(total_minutes, 1e-9), 3),
        "minutes": round(total_minutes, 2),
    }


def true_events_for(ws: WindowSet, events: np.ndarray) -> list[tuple[int, int]]:
    """Unique (t_ms, cls) of events touching any window in ``ws``."""
    cls_of = {int(t): int(c) for t, c, _ in events}
    ts = np.unique(ws.event_t_ms[ws.event_t_ms >= 0])
    return [(int(t), cls_of.get(int(t), 1)) for t in ts]


def split_minutes(ws: WindowSet, cfg: PipelineConfig) -> float:
    return len(ws) * cfg.stride * cfg.sample_period_ms / 60000


def tune_threshold(probs: np.ndarray, ws: WindowSet, true_events, cfg: PipelineConfig,
                   grid=np.arange(0.30, 0.951, 0.05)) -> tuple[float, dict]:
    best_t, best = cfg.event_threshold, None
    minutes = split_minutes(ws, cfg)
    for t in grid:
        m = event_metrics(true_events, windows_to_events(probs, ws, {}, cfg, float(t)),
                          cfg.match_tol_ms, minutes)
        if best is None or m["f1"] > best["f1"]:
            best_t, best = float(round(t, 2)), m
    return best_t, best


def evaluate_windows(model: nn.Module, cfg: PipelineConfig, X: np.ndarray, ws: WindowSet,
                     events: np.ndarray, threshold: float | None = None) -> dict:
    """Full report for a set of windows (window-level excludes ignore windows)."""
    probs = predict_probs(model, X)
    keep = ws.y != -1
    y_pred = probs.argmax(axis=1)
    text = window_report(ws.y[keep], y_pred[keep], cfg.class_names)
    macro_f1 = float(f1_score(ws.y[keep], y_pred[keep], labels=[0, 1, 2], average="macro",
                              zero_division=0)) if keep.any() else 0.0
    ev = event_metrics(true_events_for(ws, events), windows_to_events(probs, ws, {}, cfg, threshold),
                       cfg.match_tol_ms, split_minutes(ws, cfg))
    counts = {name: int((ws.y == i).sum()) for i, name in enumerate(cfg.class_names)}
    counts["ignore"] = int((~keep).sum())
    return {"windows": counts, "macro_f1": round(macro_f1, 4), "window_report": text,
            "event": ev, "threshold": cfg.event_threshold if threshold is None else threshold}


def format_report(name: str, r: dict) -> str:
    ev = r["event"]
    lines = [
        f"=== {name} ===",
        f"windows: {r['windows']}",
        f"window-level macro-F1: {r['macro_f1']}",
        r["window_report"],
        f"event-level @threshold={r['threshold']}: true={ev['true_events']} predicted={ev['predicted']} "
        f"tp={ev['tp']} fp={ev['fp']} fn={ev['fn']}",
        f"  detection precision={ev['precision']} recall={ev['recall']} f1={ev['f1']}",
        f"  class accuracy among matches={ev['class_accuracy']} "
        f"(pothole/bump confusion rows=true: {ev['class_confusion']['matrix']})",
        f"  false alarms/min={ev['false_alarms_per_min']} over {ev['minutes']} min",
    ]
    return "\n".join(lines)


def dataset_windows(d: dict) -> WindowSet:
    return WindowSet(d["X_raw"], d["y"], d["t_center"], d["latlon"], d["group_id"],
                     d["segment_id"], d["event_t_ms"])


def main(argv=None):
    ap = argparse.ArgumentParser(description="Evaluate a saved model on a dataset split")
    ap.add_argument("--dataset", type=Path, default=Path("data/dataset.npz"))
    ap.add_argument("--model", type=Path, default=Path("artifacts/model.pt"))
    ap.add_argument("--config", type=Path, default=Path("artifacts/config.json"))
    ap.add_argument("--split", choices=["train", "val", "test", "all"], default="test")
    ap.add_argument("--split-method", choices=["group", "chrono"], default="group")
    ap.add_argument("--seed", type=int, default=0, help="must match the training seed")
    ap.add_argument("--threshold", type=float)
    ap.add_argument("--json", type=Path, help="also write the metrics here")
    args = ap.parse_args(argv)

    cfg = PipelineConfig.load(args.config)
    model = load_model(args.model, cfg)
    d = load_dataset(args.dataset)
    ws = dataset_windows(d)
    if args.split != "all":
        splits = split_groups(ws.group_id, ws.y, seed=args.seed, method=args.split_method,
                              t_center=ws.t_center)
        idx = splits[["train", "val", "test"].index(args.split)]
        ws, X = ws.subset(idx), d["X"][idx]
    else:
        X = d["X"]
    r = evaluate_windows(model, cfg, X, ws, d["events"], args.threshold)
    print(format_report(args.split, r))
    if args.json:
        out = {k: v for k, v in r.items() if k != "window_report"}
        args.json.write_text(json.dumps(out, indent=2) + "\n")


if __name__ == "__main__":
    main()
