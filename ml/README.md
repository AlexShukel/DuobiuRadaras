# pothole_ml — pothole / bump detector

A small PyTorch 1-D CNN (about 10k parameters) that classifies 1.6 s windows of phone
accelerometer data as **none / pothole / bump**, trained on the data the server collects
via `POST /api/raw` (`raw.json`) and `POST /api/label` (`labels.json`); the server stores them per device
under `server/devices/<ip>/`.

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
| build dataset | `python -m pothole_ml.dataset --raw ../server/devices/<ip>/raw.json --labels ../server/devices/<ip>/labels.json --out data/dataset.npz --dump-alignment data/alignment.csv` |
| train | `python -m pothole_ml.train --dataset data/dataset.npz --out artifacts` |
| evaluate a saved model | `python -m pothole_ml.evaluate --dataset data/dataset.npz --split test` |
| HTML report (split timeline, training curves, test run) | `python -m pothole_ml.plot --dataset data/dataset.npz --artifacts artifacts --out artifacts/report.html` |
| predict on new data | `python -m pothole_ml.predict --input ../server/devices/<ip>/raw.json --output events.json` |
| cross-check labels between phones on the same route | `python -m pothole_ml.crosscheck --raw A/raw.json --labels A/labels.json --raw B/raw.json --labels B/labels.json --out-dir data/crosschecked` |
| streaming worker (used by the server's `POST /api/detect`) | `python -m pothole_ml.serve --model artifacts/model.pt --config artifacts/config.json` |

`--raw` and `--labels` can be repeated; the i-th `--raw` is paired with the i-th `--labels` as one
session (one device), so phones that recorded at the same time in different cars stay apart.
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
- `--use-speed` adds the GPS-derived ground speed (distance between consecutive position
  fixes over their time gap, clipped at 180 km/h, median over the window) as a fourth, constant
  input channel. The switch is saved in `config.json`, so prediction and the streaming worker
  compute it the same way. On the first real drives potholes were hit at a median 21 km/h and
  bumps at 35 km/h, so it mainly helps the pothole-vs-bump decision.
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

Splits are by 20 s blocks of absolute time (all windows of one event stay together, and two
phones that drove the same road at the same time land in the same split) so overlapping
windows never leak between train and test. Use `--split chrono` for a strictly chronological split.

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

## Streaming detection (`pothole_ml.serve`)

The Rust server does not run torch itself: it spawns `python -m pothole_ml.serve` once and
sends every `POST /api/detect` chunk to it as one JSON line on stdin; the worker answers one
JSON line per request on stdout (see the module docstring for the protocol). Per device it
keeps the last three chunks, scores them together so a window centred on a chunk boundary
still exists, and reports an event only when the window around it is complete (`chunk end −
lag_ms`, lag = half a window + centre tolerance = 1.1 s, plus one GPS fix = 2.1 s when the model uses speed) and only once. On real recordings the
streamed events are identical to `pothole_ml.predict` on the whole file.

## Training on the first real drives (2026-10-10, two cars in Vilnius)

```sh
A=../server/devices/100.74.153.26; B=../server/devices/100.88.117.101
python -m pothole_ml.crosscheck --raw $A/raw.json --labels $A/labels.json --raw $B/raw.json --labels $B/labels.json --out-dir data/crosschecked
python -m pothole_ml.dataset --raw $A/raw.json --labels data/crosschecked/0_labels.json \
                             --raw $B/raw.json --labels data/crosschecked/1_labels.json \
                             --out data/real_speed_xc.npz --use-speed --dump-alignment data/real_speed_xc_alignment.csv
python -m pothole_ml.train --dataset data/real_speed_xc.npz --out artifacts --seed 5
python -m pothole_ml.plot --dataset data/real_speed_xc.npz --artifacts artifacts --out artifacts/report.html
```

The two cars drove the same route within seconds of each other, so `pothole_ml.crosscheck`
can transfer a label from one car to the other when the other car's accelerometer confirms an
impact at that spot (7 labels were added to the first car this way; the server's files are
untouched, the merged label files go to `data/crosschecked/`).

103 minutes, 79 own labels (30 potholes, 49 bumps) + 7 transferred, all matched. With this
little data the model is a rough first cut. Mean over 6 seeds on the held-out test blocks
(event level):

| variant | event F1 | precision | recall | false alarms / min | pothole-vs-bump accuracy |
|---|---|---|---|---|---|
| accelerometer only | 0.36 ± 0.18 | 0.47 | 0.40 | 0.47 | 0.77 |
| + speed channel | 0.36 ± 0.12 | 0.35 | 0.49 | 0.78 | 0.85 |
| + speed + cross-checked labels (deployed) | 0.38 ± 0.17 | 0.47 | 0.51 | 0.67 | 0.78 |

The differences are within the seed noise. The deployed model (seed 5, chosen by validation
F1) finds two thirds of the events at about one false alarm every two minutes; the
pothole-vs-bump decision is unreliable. A model trained on one car barely transfers to the
other (event F1 below 0.2). Expect quality to improve mainly with more labelled kilometres;
keep labelling every event you feel. Training runs on the CPU in under a minute; the model is
too small for a GPU to help (`--device cuda` is slower).

## Verifying the pipeline without real data

```sh
python -m pothole_ml.synth --out-dir data/synth --minutes 10
python -m pothole_ml.dataset --raw data/synth/raw.json --labels data/synth/labels.json --out data/dataset.npz
python -m pothole_ml.train --dataset data/dataset.npz --out artifacts
python -m pothole_ml.predict --input data/synth/raw.json --no-windows --output data/pred.json
```

Synthetic events are clean, so expect event-level F1 above 0.9. `data/synth/truth.json`
holds the exact event times for comparison.
