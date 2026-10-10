"# Duobiu-radaras" 

ML pipeline (pothole / bump classifier): see [ml/README.md](ml/README.md).

Live sensor view: with the server running (`cd server && cargo run`, or `PORT=3001 cargo run`), open
`http://<server>:3000/` in a browser. It draws one chart per device: every phone that posts to
`POST /api/raw` is identified by its IP address and gets its own panel, so several devices can be watched
at the same time. Each panel marks that device's `POST /api/label` events; the window selector goes from
30 s to 6 h or "all", and both the browser and the server keep the last 6 hours per device (preloaded
from disk after a restart). Chunks and labels are appended to the device's files in place, so the cost of
a write does not grow with the length of the drive.
The page polls `GET /api/devices` (list of known devices) and `GET /api/raw?device=<ip>&after=<seq>`,
which returns that device's in-memory buffer of chunks and labels newer than the given sequence number.

Manual labels from the browser: every panel has "+ pothole" and "+ bump" buttons that label that device
right now, at its latest known position, like the button in the phone app. Keyboard: panels are ordered
by IP; the first panel is Q (pothole) / A (bump), the second W / S, the third E / D, and so on (top row =
pothole, home row = bump). To place a label at an exact moment, hover the chart over it and press `1`
(pothole) or `2` (bump); or choose "on chart" (pothole or bump) and click the chart at that time.
`Ctrl+Z` undoes the last label added from this page (on the hovered device, or on any device when nothing
is hovered) via `DELETE /api/label?device=<ip>` with `{timestamp, label}`; labels sent from a phone are
never undone from the page. Either way the page sends `POST /api/label?device=<ip>` and the label is stored in
that device's `labels.json` exactly like one sent from the phone. The `device` parameter must name a
device the server already knows.

The page is read from `server/src/ui.html` on every request (with the copy compiled into the binary as a
fallback), so UI edits apply on a browser reload without restarting the server.

Neural-network detection: `POST /api/detect` takes exactly the same body as `POST /api/raw`, stores the
chunk the same way (so the drive can still be used for training), and then scores it with the trained
model from `ml/artifacts/` (see [ml/README.md](ml/README.md)). The reply lists the events found in this
chunk (and the tail of the previous ones), for example:

```json
{"device": "100.74.153.26", "windows": 23, "detector_ms": 12,
 "events": [{"timestamp": 1791638042093, "latitude": 54.7362835, "longitude": 25.3453266, "label": "pothole", "score": 0.998}]}
```

An event is reported once, about 1.1 s after the impact (once the whole window around it has arrived; 2.1 s when the model uses the GPS speed channel, so the next position fix is in).
Events are appended to `server/devices/<ip>/predictions.json` (never to `labels.json`, which stays
human-only), shown on the live page as dashed markers with their score, and potholes are added to the
shared `server/potholes.json` for the map. `GET /api/predictions?device=<ip>` returns everything the model
has found for a device. When the detector cannot run, the chunk is still stored and the reply (status 200,
so the phone must not resend it) carries a `detector_error` instead of events.

The detector is a Python worker (`python -m pothole_ml.serve`) that the server starts on first use and
keeps running; its messages appear in the server log. It needs the `ml/.venv` and trained artifacts.
Environment variables: `DETECTOR_ML_DIR` (default `../ml`), `DETECTOR_PYTHON` (default
`<ML_DIR>/.venv/bin/python`), `DETECTOR_ARTIFACTS` (default `<ML_DIR>/artifacts`). To replay a recorded
drive through the detector: `python3 scripts/replay_raw.py devices/<ip>/raw.json --device 10.0.0.5 --fast`
(`--shift-to-now` makes it look live on the page, `--endpoint /api/raw` skips detection).

Devices: the server keys all incoming data by the client IP (the TCP peer, or the first
`X-Forwarded-For` address when one is present). Every device gets its own folder with its own files,
written independently so devices never block each other:

```
server/devices/<ip>/raw.json          # POST /api/raw chunks
server/devices/<ip>/labels.json       # POST /api/label events
server/devices/<ip>/calibration.json  # POST /api/calibration thresholds (per phone mount)
server/devices/<ip>/predictions.json  # events the neural network found in POST /api/detect chunks
server/potholes.json                  # detections from all devices, shared by the pothole map
```

To simulate a second device from one machine pass `--device <ip>` to the scripts in `server/scripts/`.
