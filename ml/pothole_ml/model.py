"""Small models over (B, 3, L) feature windows."""

from __future__ import annotations

from pathlib import Path

import torch
from torch import nn

from .config import PipelineConfig


class PotholeCNN(nn.Module):
    """Three 1-D conv blocks + global avg/max pooling. About 10k parameters at width 16."""

    def __init__(self, in_channels: int = 3, n_classes: int = 3, width: int = 16,
                 dropout: float = 0.3):
        super().__init__()
        w = width
        self.features = nn.Sequential(
            nn.Conv1d(in_channels, w, kernel_size=7, padding=3), nn.BatchNorm1d(w), nn.ReLU(),
            nn.MaxPool1d(2),
            nn.Conv1d(w, 2 * w, kernel_size=5, padding=2), nn.BatchNorm1d(2 * w), nn.ReLU(),
            nn.MaxPool1d(2),
            nn.Conv1d(2 * w, 4 * w, kernel_size=3, padding=1), nn.BatchNorm1d(4 * w), nn.ReLU(),
        )
        self.head = nn.Sequential(nn.Dropout(dropout), nn.Linear(8 * w, n_classes))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h = self.features(x)
        pooled = torch.cat([h.mean(dim=2), h.amax(dim=2)], dim=1)
        return self.head(pooled)


class PotholeMLP(nn.Module):
    """Flatten -> hidden -> classes. Baseline to sanity-check the CNN against."""

    def __init__(self, in_channels: int = 3, window_len: int = 64, n_classes: int = 3,
                 hidden: int = 64, dropout: float = 0.3):
        super().__init__()
        self.net = nn.Sequential(
            nn.Flatten(), nn.Linear(in_channels * window_len, hidden), nn.ReLU(),
            nn.Dropout(dropout), nn.Linear(hidden, n_classes),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


def build_model(cfg: PipelineConfig, in_channels: int | None = None) -> nn.Module:
    in_channels = cfg.in_channels if in_channels is None else in_channels
    if cfg.arch == "cnn":
        return PotholeCNN(in_channels, cfg.n_classes, cfg.width, cfg.dropout)
    if cfg.arch == "mlp":
        return PotholeMLP(in_channels, cfg.window_len, cfg.n_classes, 4 * cfg.width, cfg.dropout)
    raise ValueError(f"unknown arch {cfg.arch!r}")


def count_parameters(model: nn.Module) -> int:
    return sum(p.numel() for p in model.parameters())


def save_model(model: nn.Module, cfg: PipelineConfig, path: str | Path) -> None:
    torch.save({"state_dict": model.state_dict(), "arch": cfg.arch, "width": cfg.width,
                "window_len": cfg.window_len, "n_classes": cfg.n_classes,
                "in_channels": cfg.in_channels}, path)


def load_model(path: str | Path, cfg: PipelineConfig) -> nn.Module:
    ckpt = torch.load(path, map_location="cpu", weights_only=True)
    for key in ("arch", "width", "window_len", "in_channels"):
        expected = getattr(cfg, key)
        if ckpt.get(key, 3 if key == "in_channels" else None) != expected:
            raise ValueError(f"checkpoint {key}={ckpt.get(key)!r} does not match config "
                             f"{key}={expected!r}")
    model = build_model(cfg)
    model.load_state_dict(ckpt["state_dict"])
    model.eval()
    return model
