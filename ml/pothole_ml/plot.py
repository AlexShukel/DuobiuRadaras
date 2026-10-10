"""Self-contained HTML report: how the recording was split, how training went, how the
model behaves on the held-out test blocks.

Run as ``python -m pothole_ml.plot --dataset data/dataset.npz --artifacts artifacts
--out artifacts/report.html`` after training. No plotting library needed: charts are inline SVG.
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

import numpy as np

from .config import PipelineConfig
from .dataset import load_dataset
from .evaluate import dataset_windows, split_groups, true_events_for
from .model import load_model
from .predict import predict_probs, windows_to_events

SPLIT_NAMES = ("train", "val", "test")

# Validated reference palette (light / dark). Splits take categorical slots 1-3,
# classes take slots 7-8 plus a marker shape so identity never rests on color alone.
CSS = """
<style>
/* layout: a single column of chart cards, summary tiles first */
:root {
  --bg: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
  --train: #2a78d6; --val: #eb6834; --test: #1baf7a; --pothole: #6250d6; --bump: #e34948;
  --seq-1: #cde2fb; --seq-2: #9ec5f4; --seq-3: #6da7ec; --seq-4: #3987e5; --seq-5: #256abf; --seq-6: #184f95;
  --font: system-ui, -apple-system, "Segoe UI", sans-serif;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
  --train: #3987e5; --val: #d95926; --test: #199e70; --pothole: #9085e9; --bump: #e66767;
  --seq-1: #184f95; --seq-2: #1c5cab; --seq-3: #256abf; --seq-4: #3987e5; --seq-5: #6da7ec; --seq-6: #9ec5f4;
  color-scheme: dark; } }
:root[data-theme="dark"] {
  --bg: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
  --train: #3987e5; --val: #d95926; --test: #199e70; --pothole: #9085e9; --bump: #e66767;
  --seq-1: #184f95; --seq-2: #1c5cab; --seq-3: #256abf; --seq-4: #3987e5; --seq-5: #6da7ec; --seq-6: #9ec5f4;
  color-scheme: dark; }
body { background: var(--bg); color: var(--ink); font-family: var(--font); margin: 0; }
.wrap { max-width: 1080px; margin: 0 auto; padding-block: 28px 48px; padding-inline: 16px; }
h1 { font-size: 1.6rem; margin: 0 0 4px; text-wrap: balance; }
h2 { font-size: 1.05rem; margin: 0 0 2px; }
.sub { color: var(--ink-2); margin: 0 0 24px; font-size: 0.95rem; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 24px; }
.tile { background: var(--surface); border: 1px solid var(--border); border-radius: 6px; padding: 12px 14px; }
.tile .label { font-size: 0.78rem; color: var(--ink-2); letter-spacing: 0.02em; }
.tile .value { font-size: 1.7rem; font-weight: 600; margin-top: 2px; }
.tile .note { font-size: 0.78rem; color: var(--muted); margin-top: 2px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 6px; padding: 16px; margin-bottom: 18px; }
.card p.desc { color: var(--ink-2); font-size: 0.88rem; margin: 0 0 10px; max-width: 70ch; }
.row { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 18px; }
.row > .card { margin-bottom: 0; min-width: 0; }
.legend { display: flex; flex-wrap: wrap; gap: 14px; font-size: 0.82rem; color: var(--ink-2); margin: 8px 0 4px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.sw { width: 12px; height: 12px; border-radius: 2px; display: inline-block; }
.tri { width: 0; height: 0; border-left: 6px solid transparent; border-right: 6px solid transparent; display: inline-block; }
svg { width: 100%; height: auto; display: block; font-family: var(--font); }
svg text { fill: var(--ink-2); font-size: 11px; }
svg .ax { stroke: var(--axis); stroke-width: 1; }
svg .gr { stroke: var(--grid); stroke-width: 1; }
svg .lbl { fill: var(--muted); }
table { border-collapse: collapse; font-size: 0.88rem; font-variant-numeric: tabular-nums; width: 100%; }
th, td { text-align: right; padding: 6px 10px; border-bottom: 1px solid var(--grid); }
th:first-child, td:first-child { text-align: left; }
th { color: var(--ink-2); font-weight: 600; }
.tblwrap { overflow-x: auto; }
details summary { cursor: pointer; color: var(--ink-2); font-size: 0.88rem; }
</style>
"""


# ----------------------------------------------------------------------------- svg helpers

def _fmt(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    if hi <= lo:
        return [lo]
    raw = (hi - lo) / n
    mag = 10 ** np.floor(np.log10(raw))
    step = min((m for m in (1, 2, 2.5, 5, 10) if m * mag >= raw), default=10) * mag
    start = np.ceil(lo / step) * step
    return [float(v) for v in np.arange(start, hi + step / 2, step)]


class Axes:
    """Pixel mapping for one plotting area."""

    def __init__(self, w=640, h=220, left=44, right=12, top=14, bottom=30,
                 xlim=(0, 1), ylim=(0, 1)):
        self.w, self.h, self.l, self.r, self.t, self.b = w, h, left, right, top, bottom
        self.xlim, self.ylim = xlim, ylim

    def x(self, v):
        lo, hi = self.xlim
        return self.l + (v - lo) / max(hi - lo, 1e-9) * (self.w - self.l - self.r)

    def y(self, v):
        lo, hi = self.ylim
        return self.h - self.b - (v - lo) / max(hi - lo, 1e-9) * (self.h - self.t - self.b)

    def frame(self, xlabel="", ylabel="", xticks=None, yticks=None, xfmt=_fmt):
        parts = []
        for v in (yticks if yticks is not None else _ticks(*self.ylim)):
            if self.ylim[0] <= v <= self.ylim[1]:
                parts.append(f'<line class="gr" x1="{self.l}" x2="{self.w - self.r}" y1="{self.y(v):.1f}" y2="{self.y(v):.1f}"/>'
                             f'<text x="{self.l - 6}" y="{self.y(v) + 4:.1f}" text-anchor="end" class="lbl">{_fmt(v)}</text>')
        for v in (xticks if xticks is not None else _ticks(*self.xlim)):
            if self.xlim[0] <= v <= self.xlim[1]:
                parts.append(f'<text x="{self.x(v):.1f}" y="{self.h - self.b + 14}" text-anchor="middle" class="lbl">{xfmt(v)}</text>')
        parts.append(f'<line class="ax" x1="{self.l}" x2="{self.w - self.r}" y1="{self.y(self.ylim[0]):.1f}" y2="{self.y(self.ylim[0]):.1f}"/>')
        if xlabel:
            parts.append(f'<text x="{(self.l + self.w - self.r) / 2:.1f}" y="{self.h - 2}" text-anchor="middle">{xlabel}</text>')
        if ylabel:
            parts.append(f'<text transform="translate(11,{(self.t + self.h - self.b) / 2:.1f}) rotate(-90)" text-anchor="middle">{ylabel}</text>')
        return "".join(parts)

    def line(self, xs, ys, color, width=2):
        pts = " ".join(f"{self.x(a):.1f},{self.y(b):.1f}" for a, b in zip(xs, ys))
        return f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="{width}" stroke-linejoin="round" stroke-linecap="round"/>'

    def dot(self, xv, yv, color, r=4, title=""):
        t = f"<title>{html.escape(title)}</title>" if title else ""
        return (f'<circle cx="{self.x(xv):.1f}" cy="{self.y(yv):.1f}" r="{r + 2}" fill="var(--surface)"/>'
                f'<circle cx="{self.x(xv):.1f}" cy="{self.y(yv):.1f}" r="{r}" fill="{color}">{t}</circle>')

    def triangle(self, xv, ypx, color, up=True, size=5, title=""):
        x = self.x(xv)
        pts = (f"{x - size:.1f},{ypx + size:.1f} {x + size:.1f},{ypx + size:.1f} {x:.1f},{ypx - size:.1f}" if up
               else f"{x - size:.1f},{ypx - size:.1f} {x + size:.1f},{ypx - size:.1f} {x:.1f},{ypx + size:.1f}")
        t = f"<title>{html.escape(title)}</title>" if title else ""
        return f'<polygon points="{pts}" fill="{color}">{t}</polygon>'

    def svg(self, body: str) -> str:
        return f'<svg viewBox="0 0 {self.w} {self.h}" role="img">{body}</svg>'


# ----------------------------------------------------------------------------- charts

def timeline_chart(ws, events, split_of_window, cfg: PipelineConfig) -> tuple[str, dict]:
    """Recorded time (gaps collapsed) as a strip of 20 s blocks colored by split."""
    half = cfg.window_ms / 2
    seg_ids = np.unique(ws.segment_id)
    bounds, offsets, cursor = {}, {}, 0.0
    total = 0.0
    for s in seg_ids:
        m = ws.segment_id == s
        bounds[s] = (float(ws.t_center[m].min() - half), float(ws.t_center[m].max() + half))
        total += bounds[s][1] - bounds[s][0]
    gap = max(total * 0.004, 500)
    for s in seg_ids:
        offsets[s] = cursor
        cursor += bounds[s][1] - bounds[s][0] + gap
    span = cursor - gap

    def xt(seg, t_ms):
        return (offsets[seg] + (t_ms - bounds[seg][0])) / 60000

    ax = Axes(w=960, h=110, left=12, right=12, top=30, bottom=26, xlim=(0, span / 60000), ylim=(0, 1))
    y0, y1 = ax.t + 22, ax.h - ax.b - 2
    body = []
    minutes_by_split = {k: 0.0 for k in SPLIT_NAMES}
    for g in np.unique(ws.group_id):
        m = ws.group_id == g
        seg = int(ws.segment_id[m][0])
        block = int(g - seg * 1_000_000)
        sp = SPLIT_NAMES[int(split_of_window[m][0])]
        lo = max(bounds[seg][0], bounds[seg][0] + block * cfg.group_block_ms)
        hi = min(bounds[seg][1], bounds[seg][0] + (block + 1) * cfg.group_block_ms)
        if hi <= lo:
            continue
        minutes_by_split[sp] += (hi - lo) / 60000
        xa, xb = ax.x(xt(seg, lo)), ax.x(xt(seg, hi))
        n_ev = int(((ws.event_t_ms[m] >= 0)).any())
        body.append(f'<rect x="{xa:.1f}" y="{y0}" width="{max(xb - xa - 1, 1):.1f}" height="{y1 - y0}" fill="var(--{sp})">'
                    f'<title>{sp} · segment {seg} block {block} · {int(m.sum())} windows</title></rect>')
    for t, cls, seg in events:
        name = cfg.class_names[int(cls)]
        body.append(ax.triangle(xt(int(seg), int(t)), y0 - 9, f"var(--{name})", up=(name == "bump"), size=4,
                                title=f"{name} at +{xt(int(seg), int(t)):.2f} min"))
    body.append(ax.frame(xlabel="recorded minutes (gaps between packets collapsed; thin breaks = new segment)",
                         yticks=[]))
    return ax.svg("".join(body)), minutes_by_split


def training_chart(history: list[dict], best_epoch: int) -> str:
    ep = [h["epoch"] for h in history]
    tr = [h["train_loss"] for h in history]
    va = [h["val_loss"] for h in history]
    f1 = [h["val_macro_f1"] for h in history]
    ymax = max(max(tr), max(va)) * 1.05
    ax = Axes(w=480, h=220, xlim=(1, max(ep[-1], 2)), ylim=(0, ymax))
    best_x = ax.x(best_epoch)
    marker = (f'<line x1="{best_x:.1f}" x2="{best_x:.1f}" y1="{ax.t}" y2="{ax.h - ax.b}" class="ax"/>'
              f'<text x="{best_x + 4:.1f}" y="{ax.t + 10}" class="lbl">best epoch {best_epoch}</text>')
    loss = ax.svg(ax.frame("epoch", "cross-entropy loss") + marker + ax.line(ep, tr, "var(--train)") +
                  ax.line(ep, va, "var(--val)") + ax.dot(ep[-1], tr[-1], "var(--train)", title=f"train loss {tr[-1]}") +
                  ax.dot(ep[-1], va[-1], "var(--val)", title=f"val loss {va[-1]}"))
    ax2 = Axes(w=480, h=220, xlim=(1, max(ep[-1], 2)), ylim=(0, 1.0))
    best_x2 = ax2.x(best_epoch)
    marker2 = f'<line x1="{best_x2:.1f}" x2="{best_x2:.1f}" y1="{ax2.t}" y2="{ax2.h - ax2.b}" class="ax"/>'
    f1svg = ax2.svg(ax2.frame("epoch", "validation macro-F1") + marker2 + ax2.line(ep, f1, "var(--val)") +
                    ax2.dot(best_epoch, f1[best_epoch - 1], "var(--val)", title=f"best val macro-F1 {f1[best_epoch - 1]}"))
    return loss, f1svg


def offsets_chart(offsets: list[int], lookback: int) -> str:
    if not offsets:
        return "<p class='desc'>No matched labels.</p>"
    edges = np.arange(-250, lookback + 251, 125)
    hist, _ = np.histogram(offsets, bins=edges)
    ax = Axes(w=480, h=200, xlim=(edges[0], edges[-1]), ylim=(0, max(hist.max(), 1) * 1.1))
    bars = []
    for lo, n in zip(edges[:-1], hist):
        if n == 0:
            continue
        xa, xb = ax.x(lo) + 1, ax.x(lo + 125) - 1
        bars.append(f'<rect x="{xa:.1f}" y="{ax.y(n):.1f}" width="{xb - xa:.1f}" height="{ax.y(0) - ax.y(n):.1f}" '
                    f'rx="3" fill="var(--seq-4)"><title>{lo}–{lo + 125} ms: {n} labels</title></rect>')
    return ax.svg(ax.frame("label time minus impact time (ms)", "labels", xticks=list(range(0, lookback + 1, 500))) + "".join(bars))


def test_trace_chart(ws_test, probs, pred_events, true_events, cfg: PipelineConfig) -> str:
    """Model score over the test blocks in time order, with true and predicted events."""
    order = np.argsort(ws_test.t_center)
    t = ws_test.t_center[order]
    score = 1.0 - probs[order, 0]
    step = cfg.stride * cfg.sample_period_ms
    xs, cur, breaks = np.zeros(len(t)), 0.0, []
    for i in range(len(t)):
        if i > 0:
            d = t[i] - t[i - 1]
            if d > 1.5 * step:
                cur += 2 * step
                breaks.append(cur - step)
            else:
                cur += d
        xs[i] = cur
    xs_min = xs / 60000

    def to_x(t_ms):
        j = int(np.clip(np.searchsorted(t, t_ms), 1, len(t) - 1))
        a, b = t[j - 1], t[j]
        if b - a > 1.5 * step:
            return xs_min[j - 1] if t_ms - a < b - t_ms else xs_min[j]
        return (xs[j - 1] + (t_ms - a)) / 60000

    ax = Axes(w=960, h=260, xlim=(0, max(xs_min[-1], 1e-6)), ylim=(0, 1.0))
    body = [ax.frame("test minutes (blocks concatenated)", "score = 1 − p(none)")]
    for b in breaks:
        body.append(f'<line class="ax" x1="{ax.x(b / 60000):.1f}" x2="{ax.x(b / 60000):.1f}" y1="{ax.t}" y2="{ax.h - ax.b}"/>')
    thr = cfg.event_threshold
    body.append(f'<line x1="{ax.l}" x2="{ax.w - ax.r}" y1="{ax.y(thr):.1f}" y2="{ax.y(thr):.1f}" stroke="var(--muted)" stroke-width="1"/>'
                f'<text x="{ax.w - ax.r - 4}" y="{ax.y(thr) - 4:.1f}" text-anchor="end" class="lbl">threshold {thr}</text>')
    body.append(ax.line(xs_min, score, "var(--ink-2)", width=1.5))
    for t_ev, cls in true_events:
        name = cfg.class_names[cls]
        body.append(ax.triangle(to_x(t_ev), ax.t + 6, f"var(--{name})", up=(name == "bump"), size=5,
                                title=f"true {name}"))
    for e in pred_events:
        name = cfg.class_names[e.cls]
        body.append(ax.dot(to_x(e.t_ms), e.score, f"var(--{name})", r=4, title=f"predicted {name} score {e.score:.2f}"))
    return ax.svg("".join(body))


def confusion_svg(y_true, y_pred, names) -> str:
    n = len(names)
    cm = np.zeros((n, n), dtype=int)
    for a, b in zip(y_true, y_pred):
        cm[a, b] += 1
    cell, left, top = 70, 70, 30
    w, h = left + n * cell + 10, top + n * cell + 10
    parts = [f'<text x="{left + n * cell / 2:.0f}" y="12" text-anchor="middle">predicted</text>',
             f'<text transform="translate(12,{top + n * cell / 2:.0f}) rotate(-90)" text-anchor="middle">true</text>']
    mx = max(cm.max(), 1)
    for i in range(n):
        parts.append(f'<text x="{left - 8}" y="{top + i * cell + cell / 2 + 4:.0f}" text-anchor="end">{names[i]}</text>')
        parts.append(f'<text x="{left + i * cell + cell / 2:.0f}" y="{top - 6}" text-anchor="middle">{names[i]}</text>')
        for j in range(n):
            v = cm[i, j]
            lvl = 1 if v == 0 else min(6, 1 + int(5 * np.log1p(v) / np.log1p(mx)))
            ink = "var(--ink)" if lvl <= 3 else "#fcfcfb"
            parts.append(f'<rect x="{left + j * cell + 1}" y="{top + i * cell + 1}" width="{cell - 2}" height="{cell - 2}" rx="4" fill="var(--seq-{lvl})"/>'
                         f'<text x="{left + j * cell + cell / 2:.0f}" y="{top + i * cell + cell / 2 + 5:.0f}" text-anchor="middle" '
                         f'style="fill:{ink};font-size:15px;font-weight:600">{v}</text>')
    return f'<svg viewBox="0 0 {w} {h}" role="img" style="max-width:{w}px">{"".join(parts)}</svg>'


# ----------------------------------------------------------------------------- page

def tile(label, value, note="") -> str:
    return (f'<div class="tile"><div class="label">{label}</div><div class="value">{value}</div>'
            f'<div class="note">{note}</div></div>')


def build_report(dataset_path: Path, artifacts: Path, title: str = "Pothole Model Report") -> str:
    data = load_dataset(dataset_path)
    meta = json.loads(dataset_path.with_suffix(".meta.json").read_text())
    cfg = PipelineConfig.load(artifacts / "config.json")
    rep = json.loads((artifacts / "report.json").read_text())
    history = json.loads((artifacts / "history.json").read_text())
    model = load_model(artifacts / "model.pt", cfg)

    ws = dataset_windows(data)
    splits = split_groups(ws.group_id, ws.y, rep.get("val_frac", 0.15), rep.get("test_frac", 0.15),
                          rep.get("seed", 0), rep.get("split_method", "group"), ws.t_center)
    split_of = np.zeros(len(ws), dtype=np.int8)
    for k, idx in enumerate(splits):
        split_of[idx] = k

    timeline, minutes_by_split = timeline_chart(ws, data["events"], split_of, cfg)
    loss_svg, f1_svg = training_chart(history, rep["best_epoch"])
    off_svg = offsets_chart(meta["alignment"]["offsets_ms"], cfg.lookback_ms)

    te = splits[2]
    ws_te = ws.subset(te)
    probs = predict_probs(model, data["X"][te])
    pred_events = windows_to_events(probs, ws_te, {}, cfg)
    true_events = true_events_for(ws_te, data["events"])
    trace_svg = test_trace_chart(ws_te, probs, pred_events, true_events, cfg)
    keep = ws_te.y != -1
    cm_svg = confusion_svg(ws_te.y[keep], probs.argmax(axis=1)[keep], cfg.class_names)

    ev = rep["test"]["event"]
    rows = []
    for k, name in enumerate(SPLIT_NAMES):
        idx = splits[k]
        c = {n: int((ws.y[idx] == i).sum()) for i, n in enumerate(cfg.class_names)}
        n_ev = len(true_events_for(ws.subset(idx), data["events"]))
        rows.append(f"<tr><td>{name}</td><td>{minutes_by_split[name]:.1f}</td><td>{len(np.unique(ws.group_id[idx]))}</td>"
                    f"<td>{n_ev}</td><td>{c['none']}</td><td>{c['pothole']}</td><td>{c['bump']}</td>"
                    f"<td>{int((ws.y[idx] == -1).sum())}</td></tr>")
    table = ("<div class='tblwrap'><table><thead><tr><th>split</th><th>minutes</th><th>blocks</th><th>events</th>"
             "<th>none windows</th><th>pothole windows</th><th>bump windows</th><th>ignored windows</th></tr></thead>"
             f"<tbody>{''.join(rows)}</tbody></table></div>")

    src = ", ".join(Path(p).name for p in meta["sources"]["raw"])
    legend_splits = ('<div class="legend"><span><i class="sw" style="background:var(--train)"></i>train</span>'
                     '<span><i class="sw" style="background:var(--val)"></i>validation</span>'
                     '<span><i class="sw" style="background:var(--test)"></i>test</span>'
                     '<span><i class="tri" style="border-top:10px solid var(--pothole)"></i>pothole (label)</span>'
                     '<span><i class="tri" style="border-bottom:10px solid var(--bump)"></i>bump (label)</span></div>')
    legend_train = ('<div class="legend"><span><i class="sw" style="background:var(--train)"></i>train loss</span>'
                    '<span><i class="sw" style="background:var(--val)"></i>validation loss</span></div>')
    legend_trace = ('<div class="legend"><span>line: model score per window</span>'
                    '<span><i class="tri" style="border-top:10px solid var(--pothole)"></i>true pothole</span>'
                    '<span><i class="tri" style="border-bottom:10px solid var(--bump)"></i>true bump</span>'
                    '<span><i class="sw" style="background:var(--pothole);border-radius:50%"></i>'
                    '<i class="sw" style="background:var(--bump);border-radius:50%;margin-left:-4px"></i>predicted event (dot at its score)</span></div>')

    return f"""<title>{html.escape(title)}</title>
{CSS}
<div class="wrap">
<h1>{html.escape(title)}</h1>
<p class="sub">{src} · {meta['minutes']} min recorded in {meta['segments']} segments · {meta['events']} labelled events ·
{cfg.arch.upper()} with {rep.get('params', '?')} parameters · best epoch {rep['best_epoch']} of {len(history)}</p>

<div class="tiles">
{tile("Test event F1", ev['f1'], f"precision {ev['precision']} · recall {ev['recall']}")}
{tile("Test events found", f"{ev['tp']} / {ev['true_events']}", f"{ev['fp']} false alarms · {ev['false_alarms_per_min']} per min")}
{tile("Pothole vs bump", ev['class_accuracy'], "accuracy among detected events")}
{tile("Test window macro-F1", rep['test']['macro_f1'], "none / pothole / bump")}
{tile("Event threshold", cfg.event_threshold, "tuned on validation")}
</div>

<div class="card">
<h2>How the recording was split</h2>
<p class="desc">The drive is cut into {cfg.group_block_ms // 1000} s blocks. Blocks are shuffled (seed {rep.get('seed', 0)})
and dealt into train, validation and test, so overlapping windows never straddle two splits. Every window that
touches a labelled event is anchored to that event's block, so an event is never half in train and half in test.</p>
{legend_splits}
{timeline}
{table}
</div>

<div class="row">
<div class="card"><h2>Training loss</h2><p class="desc">Class-weighted cross-entropy per epoch. Early stopping keeps the
epoch with the best validation macro-F1, ties broken by validation loss.</p>{legend_train}{loss_svg}</div>
<div class="card"><h2>Validation macro-F1</h2><p class="desc">Window-level, averaged over the three classes. The
vertical line marks the epoch whose weights were saved.</p>{f1_svg}</div>
</div>

<div class="card">
<h2>Test run</h2>
<p class="desc">The saved model scores every test window; a window becomes a detection when its score crosses the
threshold, and overlapping detections of the same impact are merged. Triangles are the labelled events, dots are
what the model reported.</p>
{legend_trace}
{trace_svg}
</div>

<div class="row">
<div class="card"><h2>Test confusion matrix</h2><p class="desc">Window-level, ignoring windows where an event sits
off-center.</p>{cm_svg}</div>
<div class="card"><h2>Label alignment</h2><p class="desc">How far each pressed label sat after the impact the
builder matched it to. A pile-up at the right edge means the lookback is too short.
{meta['alignment']['matched']} matched, {meta['alignment']['unmatched']} unmatched.</p>{off_svg}</div>
</div>

<details><summary>Config used</summary><pre style="font-size:0.8rem;overflow-x:auto">{html.escape(json.dumps(cfg.to_dict(), indent=1))}</pre></details>
</div>
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description="Render an HTML report for a trained model")
    ap.add_argument("--dataset", type=Path, default=Path("data/dataset.npz"))
    ap.add_argument("--artifacts", type=Path, default=Path("artifacts"))
    ap.add_argument("--out", type=Path, default=Path("artifacts/report.html"))
    ap.add_argument("--title", default="Pothole Model Report")
    args = ap.parse_args(argv)
    page = build_report(args.dataset, args.artifacts, args.title)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page)
    print(f"wrote {args.out} ({len(page) // 1024} KB)")


if __name__ == "__main__":
    main()
