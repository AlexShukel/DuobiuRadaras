# pothole_ml — pothole / bump detector

A small PyTorch 1-D CNN (about 10k parameters) that classifies 1.6 s windows of phone
accelerometer data as **none / pothole / bump**, trained on the data the server collects
via `POST /api/raw` (`raw.json`) and `POST /api/label` (`labels.json`).

## Setup

```sh
cd ml
python3 -m venv .venv && . .venv/bin/activate
pip install -U pip
pip install torch --index-url https://download.pytorch.org/whl/cpu   # CPU wheel is enough
pip install -e ".[dev]"
pytest                                                               # ~5 s
```

## Commands

| Step | Command |
|------|---------|
| synthetic drive (for testing) | `python -m pothole_ml.synth --out-dir data/synth --minutes 10` |
| build dataset | `python -m pothole_ml.dataset --raw ../server/raw.json --labels ../server/labels.json --out data/dataset.npz --dump-alignment data/alignment.csv` |
| train | `python -m pothole_ml.train --dataset data/dataset.npz --out artifacts` |
| evaluate a saved model | `python -m pothole_ml.evaluate --dataset data/dataset.npz --split test` |
| HTML report (split timeline, training curves, test run) | `python -m pothole_ml.plot --dataset data/dataset.npz --artifacts artifacts --out artifacts/report.html` |
| predict on new data | `python -m pothole_ml.predict --input ../server/raw.json --output events.json` |

`--raw` and `--labels` can be repeated to merge several recording sessions.
Every command accepts `-h`.

## Data assumptions

- Samples are 40 Hz (25 ms) and carry no timestamp of their own: sample *i* of a chunk is at
  `started_at + 25·i`. Consecutive chunks whose start matches the previous end (±100 ms) are
  stitched into one continuous segment; a gap (dropped packet) starts a new segment.
- `x, y, z` are raw m/s² including gravity, in the phone's own axes. Features are made
  orientation-invariant by projecting onto the per-window gravity estimate, so mounting
  does not matter.
- A label is pressed by a human **after** the event. The builder searches up to
  `--lookback-ms` (1500) before the label for the strongest impact and moves the label there.
  Check the printed offset histogram and `alignment.csv` (a tiny `peak_value` means the
  label found nothing real, usually because the chunk was dropped). Use
  `--label-offset-ms` if phone and label clocks disagree, or `--align-method fixed`.
- Windows: 64 samples, stride 8. A window is positive when the aligned event is within
  ±300 ms of its center, ignored when the event is in the window but off-center, otherwise
  `none`. Everything unlabeled is a hard negative, so label *every* event you feel.

## Artifacts (`artifacts/`)

- `model.pt` — weights; `config.json` — all preprocessing/inference settings (feature
  scale, tuned event threshold, NMS window). Prediction needs both.
- `report.txt` / `report.json` — window-level precision/recall/F1 and confusion matrix,
  plus event-level detection precision/recall/F1, pothole-vs-bump accuracy among detections
  and false alarms per minute, for the validation and test splits.
- `history.json` — per-epoch losses and validation macro-F1.

Splits are by 20 s blocks (all windows of one event stay together) so overlapping windows
never leak between train and test. Use `--split chrono` for a strictly chronological split.

## Prediction output

```json
{"events": [{"timestamp": 1791630049250, "iso": "2026-10-10T11:00:49.250Z",
             "latitude": 54.6865237, "longitude": 25.2790368, "label": "pothole", "score": 0.95}],
 "windows": [{"t_ms": 1791630000787, "p_none": 0.99, "p_pothole": 0.0, "p_bump": 0.01}],
 "summary": {"chunks": 287, "segments": 14, "windows": 2772, "events": 116}}
```

`score = 1 − p_none`. Overlapping windows that fire on the same impact are merged
(`--nms-ms`, default 600). `--no-windows` drops the per-window list. The input may be the
whole `raw.json` array or a single chunk object.

## Verifying the pipeline without real data

```sh
python -m pothole_ml.synth --out-dir data/synth --minutes 10
python -m pothole_ml.dataset --raw data/synth/raw.json --labels data/synth/labels.json --out data/dataset.npz
python -m pothole_ml.train --dataset data/dataset.npz --out artifacts
python -m pothole_ml.predict --input data/synth/raw.json --no-windows --output data/pred.json
```

Synthetic events are clean, so expect event-level F1 above 0.9. `data/synth/truth.json`
holds the exact event times for comparison.
