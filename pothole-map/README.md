# Pothole Map

Minimal web app that fetches pothole coordinates from the server and renders each one as a red dot on an interactive map.

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
- `POTHOLES_API_URL` — upstream detection server endpoint (default `http://100.72.8.35:3000/api/potholes`).
- `USE_MOCK=1` — serve generated mock data from `mock/potholes.js` instead of calling the upstream.

## API

`GET /api/potholes`

Fetches `[{ "latitude", "longitude" }]` from the upstream service and returns it as:

```json
[
  { "id": 1, "lat": 54.6872, "lng": 25.2797 }
]
```

The proxy exists because the upstream does not send CORS headers. If the upstream is unreachable or errors, it responds with `502`.
