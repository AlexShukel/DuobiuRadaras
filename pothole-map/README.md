# Pothole Map

Minimal web app that fetches pothole coordinates from the server and renders each one as a red dot on an interactive map.

- Map: [Leaflet](https://leafletjs.com/) with [OpenStreetMap](https://www.openstreetmap.org/) tiles — free and open source, no API key needed.
- Server: plain Node.js (no dependencies) serving the static frontend and a mock API.

## Run

Requires Node.js 18+.

```sh
cd pothole-map
npm start
```

Open http://localhost:3000. Set `PORT` to use a different port.

## API

`GET /api/potholes?count=50`

Returns an array of points (`count` is optional, default 50, max 1000):

```json
[
  { "id": 1, "lat": 54.6872, "lng": 25.2797 }
]
```

The data currently comes from `mock/potholes.js`, which generates deterministic pseudo-random points inside Vilnius. Replace `getPotholes` with a real data source when one is available.
