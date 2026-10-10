# Pothole Map

Minimal web app with two tabs:

- **Potholes** — fetches pothole coordinates from the server and renders each one as a red dot on an interactive map.
- **Training data** — the drives collected in `../server/devices/<ip>/` (the data the model in `../ml` is trained on): each
  phone's route as a coloured line, with the human-pressed labels on top as red dots (potholes) and purple dots (bumps).
  Click a dot for the device and time; hover a line for the device.

- Map: [Leaflet](https://leafletjs.com/) with [OpenStreetMap](https://www.openstreetmap.org/) tiles — free and open source, no API key needed.
- Server: plain Node.js (no dependencies) serving the static frontend and proxying pothole data from the detection server (`../server`).

## Run

Requires Node.js 18+.

```sh
cd pothole-map
npm start
```

Open http://localhost:3000.

Environment variables:

- `PORT` — port to listen on (default `3000`).
- `HOST` — interface to bind to (default `0.0.0.0`, i.e. all interfaces; set `127.0.0.1` for local-only).
- `POTHOLES_API_URL` — upstream detection server endpoint (default `http://100.72.8.35:3001/api/potholes`).
- `USE_MOCK=1` — serve generated mock data from `mock/potholes.js` instead of calling the upstream.
- `DEVICES_DIR` — folder with the collected drives, one sub-folder per device holding `raw.json` and `labels.json`
  (default `../server/devices`).

## API

`GET /api/potholes`

Fetches `[{ "latitude", "longitude" }]` from the upstream service and returns it as:

```json
[
  { "id": 1, "lat": 54.6872, "lng": 25.2797 }
]
```

The proxy exists because the upstream does not send CORS headers. If the upstream is unreachable or errors, it responds with `502`.

`GET /api/dataset`

Reads every `<DEVICES_DIR>/<ip>/raw.json` and `labels.json` from disk and returns, per device, the route as a list of
polylines (consecutive identical GPS fixes collapsed; a pause of more than 10 s between chunks starts a new polyline)
and the labels:

```json
{
  "devices": [
    {
      "ip": "100.74.153.26",
      "chunks": 1544,
      "from": "2026-10-10T13:11:40.169Z",
      "to": "2026-10-10T14:05:20.180Z",
      "route": [[[54.7283, 25.3489], [54.7284, 25.3488]]],
      "labels": [{ "timestamp": 1791637990755, "lat": 54.7328, "lng": 25.3460, "label": "bump" }]
    }
  ]
}
```

The parsed result is cached per device and rebuilt only when `raw.json` or `labels.json` changes on disk, so reloading
the tab after a new drive picks it up without restarting.
