"# Duobiu-radaras" 

ML pipeline (pothole / bump classifier): see [ml/README.md](ml/README.md).

Live sensor view: with the server running (`cd server && cargo run`, or `PORT=3001 cargo run`), open
`http://<server>:3000/` in a browser. It draws one chart per device: every phone that posts to
`POST /api/raw` is identified by its IP address and gets its own panel, so several devices can be watched
at the same time. Each panel marks that device's `POST /api/label` events and keeps the last 10 minutes.
The page polls `GET /api/devices` (list of known devices) and `GET /api/raw?device=<ip>&after=<seq>`,
which returns that device's in-memory buffer of chunks and labels newer than the given sequence number.

Devices: the server keys all incoming data by the client IP (the TCP peer, or the first
`X-Forwarded-For` address when one is present). Every device gets its own folder with its own files,
written independently so devices never block each other:

```
server/devices/<ip>/raw.json          # POST /api/raw chunks
server/devices/<ip>/labels.json       # POST /api/label events
server/devices/<ip>/calibration.json  # POST /api/calibration thresholds (per phone mount)
server/potholes.json                  # detections from all devices, shared by the pothole map
```

To simulate a second device from one machine pass `--device <ip>` to the scripts in `server/scripts/`.
