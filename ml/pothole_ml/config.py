"""Single source of truth for every tunable in the pipeline.

The config used to build a dataset is saved next to it, and the config used to train a
model is saved as ``artifacts/config.json`` so that prediction applies exactly the same
preprocessing as training.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


@dataclass
class PipelineConfig:
    # --- sampling / segmentation ---
    sample_period_ms: int = 25          # 40 Hz, fixed by the mobile app
    gap_tolerance_ms: int = 100         # larger start-time mismatch starts a new segment

    # --- label alignment ---
    align_method: str = "peak"          # "peak" | "fixed"
    lookback_ms: int = 1500             # search this far before the label timestamp
    lookahead_ms: int = 250             # ...and this far after (clock jitter)
    fixed_offset_ms: int = 700          # used when align_method == "fixed"
    label_offset_ms: int = 0            # global shift added to every label timestamp
    min_event_separation_ms: int = 500  # two labels cannot claim peaks closer than this
    impact_median_window: int = 21      # samples (~0.5 s) for the moving-median baseline

    # --- windowing ---
    window_len: int = 64                # samples (1.6 s)
    stride: int = 8                     # samples (200 ms)
    center_tol_ms: int = 300            # event within +/- this of window center -> positive
    ignore_margin_ms: int = 200         # event anywhere in window (+margin) but off-center -> ignore
    group_block_ms: int = 20000         # windows are grouped per 20 s block (absolute time) for leak-free splits

    # --- features ---
    scale: float = 1.0                  # global amplitude divisor, estimated at build time
    use_speed: bool = False             # add GPS-derived speed as a 4th (constant) input channel
    speed_scale_mps: float = 15.0       # speed channel divisor (15 m/s = 54 km/h -> 1.0)
    speed_max_mps: float = 50.0         # GPS glitches above this are clipped (180 km/h)

    # --- model ---
    arch: str = "cnn"                   # "cnn" | "mlp"
    width: int = 16
    dropout: float = 0.3
    class_names: tuple[str, ...] = field(default_factory=lambda: ("none", "pothole", "bump"))

    # --- inference ---
    event_threshold: float = 0.5        # on score = 1 - p_none
    nms_ms: int = 600                   # one event per this many ms
    match_tol_ms: int = 500             # event-level evaluation matching tolerance

    @property
    def n_classes(self) -> int:
        return len(self.class_names)

    @property
    def in_channels(self) -> int:
        return 3 + int(self.use_speed)

    @property
    def window_ms(self) -> int:
        return self.window_len * self.sample_period_ms

    def to_dict(self) -> dict:
        d = asdict(self)
        d["class_names"] = list(self.class_names)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PipelineConfig":
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in d.items() if k in known}
        if "class_names" in kwargs:
            kwargs["class_names"] = tuple(kwargs["class_names"])
        return cls(**kwargs)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2) + "\n")

    @classmethod
    def load(cls, path: str | Path) -> "PipelineConfig":
        return cls.from_dict(json.loads(Path(path).read_text()))

    def apply_overrides(self, **overrides) -> "PipelineConfig":
        """Return a copy with the given non-None overrides applied."""
        d = self.to_dict()
        for k, v in overrides.items():
            if v is not None:
                if k not in d:
                    raise KeyError(f"unknown config field: {k}")
                d[k] = v
        return PipelineConfig.from_dict(d)
