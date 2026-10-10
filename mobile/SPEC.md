# DuobiuRadaras Mobile — Specification

## 1. Purpose

DuobiuRadaras ("pothole radar") is a project for detecting and analyzing road
bumps. This repository contains only the **mobile app**: an Android app that
records accelerometer readings together with GPS position while the phone rides
in a vehicle, and uploads them to the backend over HTTP. All analysis happens on
the backend; the app is a data collector.

## 2. Scope

In scope:

- Sampling the accelerometer and device location.
- Grouping samples into fixed-length packets.
- Sending packets to a configurable HTTP endpoint with `POST`.
- Recording in the background, with a persistent notification.

Out of scope (handled by other parts of the project):

- Bump detection, filtering, aggregation, maps, visualization.
- Authentication (not used for now).

## 3. Data collection

| Parameter          | Value                                  |
|--------------------|----------------------------------------|
| Sample interval    | 25 ms (40 Hz)                          |
| Packet interval    | 2 s                                    |
| Samples per packet | 80 (2 s / 25 ms)                       |

Each sample contains:

- Accelerometer reading on three axes (`x`, `y`, `z`).
- The device's latest known location (`latitude`, `longitude`).

### 3.1 Sampling

Android doesn't deliver sensor events exactly every 25 ms, so the app does its
best to keep a steady 25 ms grid:

- The accelerometer is registered at ~100 Hz (10 ms period), well above the 40 Hz
  grid, and the app keeps the latest reading.
- A timer ticks every 25 ms. On each tick the app takes one sample: the latest
  accelerometer reading plus the latest location fix.
- Small timing drift is accepted; the backend treats samples as exactly 25 ms apart
  (see 4.2).

### 3.2 Location

GPS updates far less often than every 25 ms: phones deliver about one fix per
second (1 Hz), and only a few models go up to 5–10 Hz. So roughly 40 consecutive
samples carry the same coordinates (as in the example below). Each sample stores
the most recent fix available at the moment it was taken. At 50 km/h the vehicle
moves about 14 m between fixes, which is about the same as typical phone GPS
accuracy (3–10 m).

**No data before the first fix.** After recording is switched on, the app waits
for the first location fix. No samples are taken and no packets are sent until it
arrives. The first packet starts at the first 25 ms tick after the fix. If the
signal is lost later (e.g. in a tunnel), samples keep using the last known fix.

## 4. Packet format

Packets are sent as JSON in the request body.

```json
{
  "started_at": "2026-10-09T17:44:00.123Z",
  "samples": [
    {
      "x": 0.1,
      "y": 0.2,
      "z": 9.8,
      "latitude": 54.6872,
      "longitude": 25.2797
    },
    {
      "x": 0.2,
      "y": 0.1,
      "z": 9.9,
      "latitude": 54.6872,
      "longitude": 25.2797
    }
  ]
}
```

(Shortened: a real packet has 80 samples.)

### 4.1 Fields

**Packet**

| Field        | Type            | Description                                                                                   |
|--------------|-----------------|-----------------------------------------------------------------------------------------------|
| `started_at` | string          | Time the first sample in the packet was taken. ISO 8601, UTC, with milliseconds: `YYYY-MM-DDTHH:mm:ss.SSSZ`. |
| `samples`    | array of Sample | Exactly 80 samples in chronological order, one every 25 ms.                                   |

**Sample**

| Field       | Type   | Unit             | Description                                  |
|-------------|--------|------------------|----------------------------------------------|
| `x`         | number | m/s²             | Acceleration along the device X axis.        |
| `y`         | number | m/s²             | Acceleration along the device Y axis.        |
| `z`         | number | m/s²             | Acceleration along the device Z axis.        |
| `latitude`  | number | degrees (WGS 84) | Latitude of the latest location fix.         |
| `longitude` | number | degrees (WGS 84) | Longitude of the latest location fix.        |

Values are the raw readings of the Android accelerometer (`Sensor.TYPE_ACCELEROMETER`)
in the sensor's native unit. Axes follow the Android sensor coordinate system
(relative to the phone, not the vehicle). Values include gravity: a phone lying
flat reads about `z ≈ 9.8`, as in the example.

### 4.2 Sample timestamps

Samples do not carry their own timestamps. The time of the sample at index `i`
(starting from 0) is:

```
timestamp(i) = started_at + i × 25 ms
```

Example: with `started_at = 2026-10-09T17:44:00.123Z`, sample 0 is at
`17:44:00.123`, sample 1 at `17:44:00.148`, sample 79 at `17:44:02.098`. The
next packet starts at `17:44:02.123Z`.

## 5. Transport

- Method: `POST`
- Body: the packet JSON above
- Header: `Content-Type: application/json`
- No authentication.
- Endpoint URL: set by the user in the app (see 6.1), not hard-coded.
- One packet is sent every 2 seconds while recording is active.
- **Success:** any `2xx` response. Each success increases the sent-packet counter (see 6.3).
- **Failure:** a non-`2xx` response or a network error. The packet is dropped
  (no retry, no offline storage) and recording continues.

## 6. User interface

A single screen.

### 6.1 Endpoint setting

- A text field labelled **Raw data URL** with the full endpoint URL.
- Pre-filled with a test URL: `http://10.0.2.2:3000/api/readings` (the backend's
  readings route on the host machine, as seen from the Android emulator). On a
  real phone, replace the host with the server's address.
- The value is saved on the device and kept across app restarts.
- The URL must be valid `http://` or `https://`; an invalid URL is rejected and recording can't start.
- The field can't be edited while recording is on.

### 6.2 Recording toggle

- One button that switches recording **on** and **off**, and shows the current state
  ("Record raw data" / "Stop recording").
- **On:** asks for location (and, on Android 13+, notification) permission if it
  hasn't been granted yet, then starts the recording service. If location
  permission is denied, recording stays off.
- **Off:** stops sampling. The packet in progress is discarded and not sent, so every
  packet the server receives has exactly 80 samples (a full 2 seconds).
- Recording is off when the app starts.

### 6.3 Live status

While recording is on, the screen shows:

- **Sent packets:** number of packets sent successfully since recording was switched on.
- **Location:** current latitude and longitude, or "Waiting for GPS…" before the first fix.
- **Accelerometer:** live `x`, `y`, `z` values in m/s².

## 7. Background recording

- Recording keeps running when the app is in the background or the screen is off.
- It runs in a **foreground service** of type `location`, started from the app
  while it is open. Because of this, the app only needs foreground location
  permission (`ACCESS_FINE_LOCATION`), not background location.
- While the service runs, a persistent notification ("Recording raw data") shows that recording is
  active. It displays the sent-packet count, and tapping it opens the app.
- Recording stops only when the user switches it off in the app.

## 8. Open questions

1. **Production endpoint URL** — replaces the test URL in 6.1.
2. **Authentication** — may be added later.
