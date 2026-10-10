"# Duobiu-radaras" 

ML pipeline (pothole / bump classifier): see [ml/README.md](ml/README.md).

Live sensor view: with the server running (`cd server && cargo run`), open `http://<server>:3000/` in a
browser. It plots the accelerometer stream from `POST /api/raw` as it arrives, marks `POST /api/label`
events, and keeps the last 10 minutes. The page polls `GET /api/raw?after=<seq>`, which returns the
in-memory buffer of recent chunks and labels newer than the given sequence number.
