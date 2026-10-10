"""Train the classifier on a built dataset and save artifacts.

Run as ``python -m pothole_ml.train --dataset data/dataset.npz --out artifacts``.
"""

from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

import numpy as np
import torch
from sklearn.metrics import f1_score
from torch import nn
from tqdm import tqdm

from .config import PipelineConfig
from .dataset import load_dataset
from .evaluate import (dataset_windows, evaluate_windows, format_report, split_groups,
                       true_events_for, tune_threshold)
from .model import build_model, count_parameters, save_model
from .predict import predict_probs


def set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


def class_weights(y: np.ndarray, n_classes: int = 3, max_weight: float = 10.0) -> torch.Tensor:
    counts = np.bincount(y, minlength=n_classes).astype(np.float64)
    freq = counts / max(counts.sum(), 1)
    w = np.where(freq > 0, 1.0 / np.maximum(freq, 1e-9), 1.0)
    w = w / w[freq > 0].min()
    return torch.tensor(np.clip(w, 1.0, max_weight), dtype=torch.float32)


def augment(xb: torch.Tensor, rng: torch.Generator) -> torch.Tensor:
    amp = torch.empty(xb.shape[0], 1, 1).uniform_(0.8, 1.2, generator=rng)
    noise = torch.randn(xb.shape, generator=rng) * 0.02
    return xb * amp + noise


def train(cfg: PipelineConfig, data: dict, out_dir: Path, epochs: int = 60, lr: float = 1e-3,
          weight_decay: float = 1e-4, batch_size: int = 64, patience: int = 10, seed: int = 0,
          device: str = "cpu", split_method: str = "group", val_frac: float = 0.15,
          test_frac: float = 0.15, no_class_weights: bool = False, verbose: bool = True,
          progress: bool | None = None) -> dict:
    """Train, tune the event threshold on the validation split, evaluate and save artifacts.

    ``progress`` shows a per-epoch bar on stderr (default: when ``verbose``); each epoch's
    numbers are printed above it when ``verbose``.
    """
    progress = verbose if progress is None else progress
    set_seed(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    ws = dataset_windows(data)
    X = data["X"].astype(np.float32)
    tr, va, te = split_groups(ws.group_id, ws.y, val_frac, test_frac, seed, split_method, ws.t_center)

    def labelled(idx):
        return idx[ws.y[idx] != -1]

    tr_l, va_l = labelled(tr), labelled(va)
    Xtr, ytr = torch.from_numpy(X[tr_l]), torch.from_numpy(ws.y[tr_l].astype(np.int64))
    Xva, yva = torch.from_numpy(X[va_l]).to(device), ws.y[va_l].astype(np.int64)

    model = build_model(cfg, in_channels=X.shape[1]).to(device)
    weights = None if no_class_weights else class_weights(ytr.numpy(), cfg.n_classes).to(device)
    criterion = nn.CrossEntropyLoss(weight=weights)
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)
    sched = torch.optim.lr_scheduler.ReduceLROnPlateau(opt, factor=0.5, patience=4)
    gen = torch.Generator().manual_seed(seed)

    if verbose:
        print(f"windows train={len(tr_l)} val={len(va_l)} test={len(labelled(te))} "
              f"(+{len(tr) - len(tr_l) + len(va) - len(va_l) + len(te) - len(labelled(te))} ignored)")
        print(f"train class counts={np.bincount(ytr.numpy(), minlength=cfg.n_classes).tolist()} "
              f"weights={None if weights is None else [round(float(w), 2) for w in weights]}")
        print(f"model={cfg.arch} params={count_parameters(model)} device={device}")

    history, best_f1, best_loss, best_state, best_epoch, bad = [], -1.0, float("inf"), None, 0, 0
    t0 = time.time()
    bar = tqdm(range(1, epochs + 1), unit="epoch", disable=not progress, leave=False, dynamic_ncols=True)
    for epoch in bar:
        model.train()
        perm = torch.randperm(len(Xtr), generator=gen)
        total, n = 0.0, 0
        for i in range(0, len(perm), batch_size):
            b = perm[i:i + batch_size]
            xb = augment(Xtr[b], gen).to(device)
            yb = ytr[b].to(device)
            opt.zero_grad()
            loss = criterion(model(xb), yb)
            loss.backward()
            opt.step()
            total += loss.item() * len(b)
            n += len(b)
        train_loss = total / max(n, 1)

        model.eval()
        with torch.no_grad():
            logits = model(Xva)
            val_loss = float(criterion(logits, torch.from_numpy(yva).to(device))) if len(yva) else 0.0
            val_pred = logits.argmax(dim=1).cpu().numpy()
        val_f1 = float(f1_score(yva, val_pred, labels=list(range(cfg.n_classes)), average="macro",
                                zero_division=0)) if len(yva) else 0.0
        sched.step(val_loss)
        history.append({"epoch": epoch, "train_loss": round(train_loss, 4),
                        "val_loss": round(val_loss, 4), "val_macro_f1": round(val_f1, 4),
                        "lr": opt.param_groups[0]["lr"]})
        bar.set_postfix(train_loss=f"{train_loss:.3f}", val_loss=f"{val_loss:.3f}",
                        val_f1=f"{val_f1:.3f}", best=best_epoch, refresh=False)
        if verbose:
            bar.write(f"epoch {epoch:3d} train_loss={train_loss:.4f} val_loss={val_loss:.4f} "
                      f"val_macro_f1={val_f1:.4f}")
        # improvement = better macro-F1, or equal macro-F1 with lower val loss
        if val_f1 > best_f1 or (val_f1 == best_f1 and val_loss < best_loss):
            best_f1, best_loss, best_epoch, bad = val_f1, val_loss, epoch, 0
            best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}
        else:
            bad += 1
            if bad >= patience:
                if verbose:
                    bar.write(f"early stopping at epoch {epoch} (best epoch {best_epoch})")
                break
    bar.close()

    if best_state is not None:
        model.load_state_dict(best_state)
    model.eval()

    # tune the event threshold on the validation split (all windows incl. ignore ones)
    va_ws = ws.subset(va)
    thr, thr_metrics = tune_threshold(predict_probs(model, X[va], device=device), va_ws,
                                      true_events_for(va_ws, data["events"]), cfg)
    cfg = cfg.apply_overrides(event_threshold=thr)
    if verbose:
        print(f"tuned event threshold={thr} (val event f1={thr_metrics['f1'] if thr_metrics else None})")

    model_cpu = model.to("cpu")
    reports = {name: evaluate_windows(model_cpu, cfg, X[idx], ws.subset(idx), data["events"])
               for name, idx in (("val", va), ("test", te))}
    text = "\n\n".join(format_report(name, r) for name, r in reports.items())
    if verbose:
        print(text)

    save_model(model_cpu, cfg, out_dir / "model.pt")
    cfg.save(out_dir / "config.json")
    (out_dir / "report.txt").write_text(text + "\n")
    (out_dir / "report.json").write_text(json.dumps(
        {"best_epoch": best_epoch, "best_val_macro_f1": round(best_f1, 4), "threshold": thr,
         "seconds": round(time.time() - t0, 1), "seed": seed, "split_method": split_method,
         "val_frac": val_frac, "test_frac": test_frac, "params": count_parameters(model_cpu),
         **{name: {k: v for k, v in r.items() if k != "window_report"} for name, r in reports.items()}},
        indent=2) + "\n")
    (out_dir / "history.json").write_text(json.dumps(history, indent=2) + "\n")
    if verbose:
        print(f"saved model.pt, config.json, report.txt, report.json, history.json to {out_dir}")
    return {"best_epoch": best_epoch, "best_val_macro_f1": best_f1, "threshold": thr, **reports}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Train the pothole / bump classifier")
    ap.add_argument("--dataset", type=Path, default=Path("data/dataset.npz"))
    ap.add_argument("--out", type=Path, default=Path("artifacts"))
    ap.add_argument("--arch", choices=["cnn", "mlp"])
    ap.add_argument("--width", type=int)
    ap.add_argument("--dropout", type=float)
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--weight-decay", type=float, default=1e-4)
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--patience", type=int, default=10)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--split", choices=["group", "chrono"], default="group")
    ap.add_argument("--val-frac", type=float, default=0.15)
    ap.add_argument("--test-frac", type=float, default=0.15)
    ap.add_argument("--no-class-weights", action="store_true")
    ap.add_argument("--no-progress", action="store_true", help="no per-epoch progress bar")
    args = ap.parse_args(argv)

    data = load_dataset(args.dataset)
    meta_path = args.dataset.with_suffix(".meta.json")
    cfg = PipelineConfig.from_dict(json.loads(meta_path.read_text())["config"])
    cfg = cfg.apply_overrides(arch=args.arch, width=args.width, dropout=args.dropout)
    train(cfg, data, args.out, epochs=args.epochs, lr=args.lr, weight_decay=args.weight_decay,
          batch_size=args.batch_size, patience=args.patience, seed=args.seed, device=args.device,
          split_method=args.split, val_frac=args.val_frac, test_frac=args.test_frac,
          no_class_weights=args.no_class_weights, progress=not args.no_progress)


if __name__ == "__main__":
    main()
