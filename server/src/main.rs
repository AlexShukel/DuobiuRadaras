use core::f64;
use std::{collections::VecDeque, fs, io, time::Instant};

use axum::{Json, Router, http::StatusCode, routing::{get, post}};
use serde::{Deserialize, Serialize};
use axum::{extract::{Query, Request}, middleware::{self, Next}, response::{Html, Response}};

const WINDOW_SIZE: usize = 20;
/// How many recent raw chunks (2 s each) the live view keeps in memory: 300 = 10 minutes.
const LIVE_CHUNK_CAPACITY: usize = 300;
const LIVE_LABEL_CAPACITY: usize = 500;
const UI_HTML: &str = include_str!("ui.html");
const CALIBRATION_TABLE_PATH: &str = "./calibration.json";
const POTHOLES_PATH: &str = "./potholes.json";
const RAW: &str = "./raw.json";
const LABELS_PATH: &str = "./labels.json";
const POTHOLE_GROUP_RADIUS_M: f64 = 10.0;

#[derive(Deserialize, Serialize, Debug, PartialEq)]
struct PotholeLocation {
    latitude: f64,
    longitude: f64,
}

#[derive(Deserialize, Debug, Serialize, Clone)]
struct SampleChunk {
    started_at: String, // ISO 8601 (the mobile app) or unix milliseconds
    samples: Vec<Sample>
}

#[derive(Deserialize, Debug, Serialize, Clone)]
struct Sample {
    x: f64,
    y: f64,
    z: f64,
    latitude: f64,
    longitude: f64   
}

#[derive(Deserialize, Serialize, Clone)]
struct CalibrationTable {
    thresholds: Vec<f64>
}

#[derive(Debug, Deserialize, Serialize, Clone)]
struct LabeledEvent {
    timestamp: u64, // Unix timestamp in milliseconds
    latitude: f64,
    longitude: f64,
    label: EventLabel,
}

#[derive(Debug, Deserialize, Serialize, Clone, Copy)]
#[serde(rename_all = "lowercase")]
enum EventLabel {
    Pothole,
    Bump,
}

#[tokio::main]
async fn main() -> io::Result<()> {
    let app = Router::new()
        .route("/api/readings", post(post_readings))
        .route("/api/potholes", get(get_potholes))
        .route("/api/calibration", post(post_calibration))
        .route("/api/health", get(get_health))
        .route("/api/raw", post(post_raw).get(get_raw_live))
        .route("/api/label", post(post_label))
        .route("/", get(get_ui))
        .route("/live", get(get_ui))
        .layer(middleware::from_fn(log_request));

    println!("[INFO] Starting server: window_size={WINDOW_SIZE}, grouping_radius_m={POTHOLE_GROUP_RADIUS_M}");
    println!("[INFO] Data directory: {}", std::env::current_dir()?.display());
    println!("[INFO] Calibration file: {CALIBRATION_TABLE_PATH}; potholes file: {POTHOLES_PATH}");
    match load_live_buffer_from_disk() {
        Ok((chunks, labels)) => println!("[INFO] Live view preloaded {chunks} chunks and {labels} labels from disk"),
        Err(error) => eprintln!("[WARN] Live view starts empty: {error}"),
    }
    let port = std::env::var("PORT").unwrap_or_else(|_| "3000".to_owned());
    let address = format!("0.0.0.0:{port}");
    let listener = tokio::net::TcpListener::bind(&address).await.map_err(|error| {
        eprintln!("[ERROR] Cannot bind {address}: {error}");
        error
    })?;
    println!("[INFO] Listening on {}", listener.local_addr()?);
    axum::serve(listener, app).await
}

static LABELS_FILE_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

async fn post_label(
    Json(event): Json<LabeledEvent>,
) -> Result<StatusCode, StatusCode> {
    println!("[INFO] POST /api/label: timestamp={}, latitude={}, longitude={}, label={:?}",
        event.timestamp, event.latitude, event.longitude, event.label);

    if !event.latitude.is_finite()
        || !event.longitude.is_finite()
        || !(-90.0..=90.0).contains(&event.latitude)
        || !(-180.0..=180.0).contains(&event.longitude)
    {
        eprintln!("[WARN] POST /api/label: invalid coordinates; status=400");
        return Err(StatusCode::BAD_REQUEST);
    }

    let _guard = LABELS_FILE_LOCK.lock().map_err(|error| {
        eprintln!("[ERROR] POST /api/label: lock failed: {error}");
        StatusCode::INTERNAL_SERVER_ERROR
    })?;

    let mut events: Vec<LabeledEvent> = match fs::read_to_string(LABELS_PATH) {
        Ok(json) => serde_json::from_str(&json).map_err(|error| {
            eprintln!("[ERROR] POST /api/label: invalid JSON in {LABELS_PATH}: {error}");
            StatusCode::INTERNAL_SERVER_ERROR
        })?,
        Err(error) if error.kind() == io::ErrorKind::NotFound => Vec::new(),
        Err(error) => {
            eprintln!("[ERROR] POST /api/label: cannot read {LABELS_PATH}: {error}");
            return Err(StatusCode::INTERNAL_SERVER_ERROR);
        }
    };

    events.push(event);

    let json = serde_json::to_string_pretty(&events).map_err(|error| {
        eprintln!("[ERROR] POST /api/label: serialization failed: {error}");
        StatusCode::INTERNAL_SERVER_ERROR
    })?;

    fs::write(LABELS_PATH, json).map_err(|error| {
        eprintln!("[ERROR] POST /api/label: cannot write {LABELS_PATH}: {error}");
        StatusCode::INTERNAL_SERVER_ERROR
    })?;

    println!("[INFO] Appended labeled event; total_labels={}", events.len());
    if let Some(event) = events.last() {
        push_live_label(event.clone());
    }

    Ok(StatusCode::OK)
}

// Also logs requests rejected before a handler runs (for example, invalid JSON).
async fn log_request(request: Request, next: Next) -> Response {
    let method = request.method().clone();
    let path = request.uri().path().to_owned();
    let started = Instant::now();
    println!("[INFO] Request received: {method} {path}");
    let response = next.run(request).await;
    println!("[INFO] Request finished: {method} {path}, status={}, elapsed_ms={}",
        response.status().as_u16(), started.elapsed().as_millis());
    println!("=========================");
    response
}

async fn get_health() -> StatusCode {
    println!("[INFO] GET /api/health: status=200");

    StatusCode::OK
}

static RAW_FILE_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

async fn post_raw(
    Json(sample_chunk): Json<SampleChunk>,
) -> Result<StatusCode, StatusCode> {
    let _guard = RAW_FILE_LOCK.lock().map_err(|error| {
        eprintln!("[ERROR] POST /api/raw: lock failed: {error}");
        StatusCode::INTERNAL_SERVER_ERROR
    })?;

    let mut chunks: Vec<SampleChunk> = match fs::read_to_string(RAW) {
        Ok(json) => serde_json::from_str(&json).map_err(|error| {
            eprintln!("[ERROR] POST /api/raw: invalid JSON in {RAW}: {error}");
            StatusCode::INTERNAL_SERVER_ERROR
        })?,
        Err(error) if error.kind() == io::ErrorKind::NotFound => Vec::new(),
        Err(error) => {
            eprintln!("[ERROR] POST /api/raw: cannot read {RAW}: {error}");
            return Err(StatusCode::INTERNAL_SERVER_ERROR);
        }
    };

    let sample_count = sample_chunk.samples.len();
    let live_copy = sample_chunk.clone();
    chunks.push(sample_chunk);

    let json = serde_json::to_string_pretty(&chunks).map_err(|error| {
        eprintln!("[ERROR] POST /api/raw: serialization failed: {error}");
        StatusCode::INTERNAL_SERVER_ERROR
    })?;

    fs::write(RAW, json).map_err(|error| {
        eprintln!("[ERROR] POST /api/raw: cannot write {RAW}: {error}");
        StatusCode::INTERNAL_SERVER_ERROR
    })?;

    println!(
        "[INFO] Appended {sample_count} raw samples; total_chunks={}",
        chunks.len()
    );
    push_live_chunk(live_copy);

    Ok(StatusCode::OK)
}

// ---------------------------------------------------------------------------
// Live view: an in-memory ring buffer of recent chunks and labels, served to the
// browser page at `/` which polls `GET /api/raw?after=<seq>` about once a second.
// Chunks and labels share one sequence counter so a single cursor covers both.

#[derive(Debug, Serialize, Clone)]
struct LiveChunk {
    seq: u64,
    #[serde(flatten)]
    chunk: SampleChunk,
}

#[derive(Debug, Serialize, Clone)]
struct LiveLabel {
    seq: u64,
    #[serde(flatten)]
    event: LabeledEvent,
}

struct LiveBuffer {
    next_seq: u64,
    chunks: VecDeque<LiveChunk>,
    labels: VecDeque<LiveLabel>,
}

impl LiveBuffer {
    fn push_chunk(&mut self, chunk: SampleChunk) -> u64 {
        let seq = self.next_seq;
        self.next_seq += 1;
        self.chunks.push_back(LiveChunk { seq, chunk });
        while self.chunks.len() > LIVE_CHUNK_CAPACITY {
            self.chunks.pop_front();
        }
        seq
    }

    fn push_label(&mut self, event: LabeledEvent) -> u64 {
        let seq = self.next_seq;
        self.next_seq += 1;
        self.labels.push_back(LiveLabel { seq, event });
        while self.labels.len() > LIVE_LABEL_CAPACITY {
            self.labels.pop_front();
        }
        seq
    }

    /// Everything with a sequence number strictly greater than `after`.
    fn since(&self, after: u64) -> LiveResponse {
        let first_new = |seq: u64| seq > after;
        LiveResponse {
            next: self.next_seq.saturating_sub(1),
            chunks: self.chunks.iter().filter(|c| first_new(c.seq)).cloned().collect(),
            labels: self.labels.iter().filter(|l| first_new(l.seq)).cloned().collect(),
        }
    }
}

#[derive(Debug, Serialize)]
struct LiveResponse {
    /// Pass this back as `?after=` to receive only newer items next time.
    next: u64,
    chunks: Vec<LiveChunk>,
    labels: Vec<LiveLabel>,
}

#[derive(Deserialize)]
struct LiveQuery {
    /// Last sequence number the client has seen; omitted or 0 means "send the whole buffer".
    after: Option<u64>,
}

static LIVE_BUFFER: std::sync::Mutex<LiveBuffer> = std::sync::Mutex::new(LiveBuffer {
    next_seq: 1,
    chunks: VecDeque::new(),
    labels: VecDeque::new(),
});

fn push_live_chunk(chunk: SampleChunk) {
    match LIVE_BUFFER.lock() {
        Ok(mut buffer) => { buffer.push_chunk(chunk); }
        Err(error) => eprintln!("[ERROR] live buffer lock failed: {error}"),
    }
}

fn push_live_label(event: LabeledEvent) {
    match LIVE_BUFFER.lock() {
        Ok(mut buffer) => { buffer.push_label(event); }
        Err(error) => eprintln!("[ERROR] live buffer lock failed: {error}"),
    }
}

/// Seed the buffer from raw.json / labels.json so the page shows history after a restart.
fn load_live_buffer_from_disk() -> Result<(usize, usize), String> {
    let chunks: Vec<SampleChunk> = match fs::read_to_string(RAW) {
        Ok(json) => serde_json::from_str(&json).map_err(|e| format!("{RAW}: {e}"))?,
        Err(error) if error.kind() == io::ErrorKind::NotFound => Vec::new(),
        Err(error) => return Err(format!("{RAW}: {error}")),
    };
    let labels: Vec<LabeledEvent> = match fs::read_to_string(LABELS_PATH) {
        Ok(json) => serde_json::from_str(&json).map_err(|e| format!("{LABELS_PATH}: {e}"))?,
        Err(error) if error.kind() == io::ErrorKind::NotFound => Vec::new(),
        Err(error) => return Err(format!("{LABELS_PATH}: {error}")),
    };
    let mut buffer = LIVE_BUFFER.lock().map_err(|e| format!("lock: {e}"))?;
    let skip = chunks.len().saturating_sub(LIVE_CHUNK_CAPACITY);
    let mut n_chunks = 0;
    for chunk in chunks.into_iter().skip(skip) {
        buffer.push_chunk(chunk);
        n_chunks += 1;
    }
    let skip = labels.len().saturating_sub(LIVE_LABEL_CAPACITY);
    let mut n_labels = 0;
    for label in labels.into_iter().skip(skip) {
        buffer.push_label(label);
        n_labels += 1;
    }
    Ok((n_chunks, n_labels))
}

async fn get_raw_live(Query(query): Query<LiveQuery>) -> Result<Json<LiveResponse>, StatusCode> {
    let buffer = LIVE_BUFFER.lock().map_err(|error| {
        eprintln!("[ERROR] GET /api/raw: lock failed: {error}");
        StatusCode::INTERNAL_SERVER_ERROR
    })?;
    let response = buffer.since(query.after.unwrap_or(0));
    println!("[INFO] GET /api/raw: after={} -> chunks={} labels={} next={}",
        query.after.unwrap_or(0), response.chunks.len(), response.labels.len(), response.next);
    Ok(Json(response))
}

async fn get_ui() -> Html<&'static str> {
    Html(UI_HTML)
}

async fn post_readings(Json(sample_chunk): Json<SampleChunk>) -> Result<StatusCode, StatusCode> {
    let started = Instant::now();
    println!("[INFO] POST /api/readings: chunk_started_at={:?}, samples={}", sample_chunk.started_at, sample_chunk.samples.len());

    let vertical_axis: Vec<f64> = sample_chunk
        .samples
        .iter()
        .map(|sample| sample.z)
        .collect();

    if vertical_axis.len() < WINDOW_SIZE
        || vertical_axis.iter().any(|value| !value.is_finite())
    {
        eprintln!("[WARN] POST /api/readings rejected: samples={}, required={}, non_finite_z={}, status=400",
            vertical_axis.len(), WINDOW_SIZE, vertical_axis.iter().filter(|value| !value.is_finite()).count());
        return Err(StatusCode::BAD_REQUEST);
    }

    let json = fs::read_to_string(CALIBRATION_TABLE_PATH)
        .map_err(|error| {
            eprintln!("[ERROR] POST /api/readings: cannot read {CALIBRATION_TABLE_PATH}: {error}; status=500");
            StatusCode::INTERNAL_SERVER_ERROR
        })?;

    let table: CalibrationTable = serde_json::from_str(&json)
        .map_err(|error| {
            eprintln!("[ERROR] post_readings: parse calibration JSON: {error}; status=500");
            StatusCode::INTERNAL_SERVER_ERROR
        })?;

    if table.thresholds.is_empty()
        || table.thresholds.iter().any(|value| !value.is_finite() || *value < 0.0)
    {
        eprintln!("[ERROR] POST /api/readings: calibration thresholds are empty or invalid; status=500");
        return Err(StatusCode::INTERNAL_SERVER_ERROR);
    }

    let threshold = table.thresholds
        .iter()
        .copied()
        .reduce(f64::min)
        .ok_or(StatusCode::INTERNAL_SERVER_ERROR)?;

    println!("[INFO] Readings: threshold={threshold:.6}, calibration_entries={}, windows={}",
        table.thresholds.len(), vertical_axis.len() - WINDOW_SIZE + 1);
    let locations = detect_pothole_locations(&sample_chunk, threshold);
    println!("[INFO] Readings: flagged_windows={}", locations.len());

    if locations.iter().any(|location| {
        !location.latitude.is_finite()
            || !location.longitude.is_finite()
            || !(-90.0..=90.0).contains(&location.latitude)
            || !(-180.0..=180.0).contains(&location.longitude)
    }) {
        eprintln!("[WARN] POST /api/readings: detected location has invalid coordinates; status=400");
        return Err(StatusCode::BAD_REQUEST);
    }

    save_potholes(locations)?;
    println!("[INFO] POST /api/readings complete: status=200, elapsed_ms={}", started.elapsed().as_millis());
    Ok(StatusCode::OK)
}

fn detect_pothole_locations(
    chunk: &SampleChunk,
    threshold: f64,
) -> Vec<PotholeLocation> {
    let vertical_axis: Vec<f64> =
        chunk.samples.iter().map(|sample| sample.z).collect();

    detect_potholes(&vertical_axis, threshold)
        .into_iter()
        .map(|index| {
            let sample = &chunk.samples[index];

            PotholeLocation {
                latitude: sample.latitude,
                longitude: sample.longitude,
            }
        })
        .collect()
}

fn detect_potholes(vertical_axis: &[f64], threshold: f64) -> Vec<usize> {
    vertical_axis
        .windows(WINDOW_SIZE)
        .enumerate()
        .filter_map(|(start_index, window)| {
            if stdev(window) >= threshold {
                Some(start_index + WINDOW_SIZE - 1)
            } else {
                None
            }
        })
        .collect()
}

async fn post_calibration(
    Json(sample_chunk): Json<SampleChunk>,
) -> Result<StatusCode, StatusCode> {
    let started = Instant::now();
    println!("[INFO] POST /api/calibration: chunk_started_at={:?}, samples={}", sample_chunk.started_at, sample_chunk.samples.len());
    let vertical_axis: Vec<f64> = sample_chunk.samples.iter().map(|sample| sample.z).collect();

    let threshold = calibration_threshold(&vertical_axis).ok_or_else(|| {
        eprintln!("[WARN] POST /api/calibration: insufficient samples or non-finite acceleration; status=400");
        StatusCode::BAD_REQUEST
    })?;
    println!("[INFO] Calibration: windows={}, maximum_window_stdev={threshold:.6}", vertical_axis.len() - WINDOW_SIZE + 1);
    let mut thresholds = vec![threshold];

    if fs::exists(CALIBRATION_TABLE_PATH).map_err(|error| {
            eprintln!("[ERROR] POST /api/calibration: cannot check whether calibration file exists: {error}; status=500");
            StatusCode::INTERNAL_SERVER_ERROR
        })? {
        let json = fs::read_to_string(CALIBRATION_TABLE_PATH)
            .map_err(|error| {
            eprintln!("[ERROR] POST /api/calibration: cannot read calibration file: {error}; status=500");
            StatusCode::INTERNAL_SERVER_ERROR
        })?;

        let calibration_table: CalibrationTable = serde_json::from_str(&json)
            .map_err(|error| {
            eprintln!("[ERROR] POST /api/calibration: cannot parse calibration JSON: {error}; status=500");
            StatusCode::INTERNAL_SERVER_ERROR
        })?;

        thresholds.extend(calibration_table.thresholds);
    }

    let calibration_table = CalibrationTable { thresholds };
    println!("[INFO] Calibration: saving {} thresholds to {CALIBRATION_TABLE_PATH}", calibration_table.thresholds.len());

    let json = serde_json::to_string(&calibration_table)
        .map_err(|error| {
            eprintln!("[ERROR] POST /api/calibration: cannot serialize calibration JSON: {error}; status=500");
            StatusCode::INTERNAL_SERVER_ERROR
        })?;

    fs::write(CALIBRATION_TABLE_PATH, &json)
        .map_err(|error| {
            eprintln!("[ERROR] POST /api/calibration: cannot write {CALIBRATION_TABLE_PATH}: {error}; status=500");
            StatusCode::INTERNAL_SERVER_ERROR
        })?;
    println!("[INFO] POST /api/calibration complete: status=200, elapsed_ms={}", started.elapsed().as_millis());
    Ok(StatusCode::OK)
}

async fn get_potholes() -> Result<Json<Vec<PotholeLocation>>, StatusCode> {
    let started = Instant::now();
    println!("[INFO] GET /api/potholes");
    let _guard = POTHOLES_FILE_LOCK
        .lock()
        .map_err(|error| {
            eprintln!("[ERROR] get_potholes: potholes file lock: {error}; status=500");
            StatusCode::INTERNAL_SERVER_ERROR
        })?;

    let potholes = read_potholes()?;
    println!("[INFO] GET /api/potholes complete: locations={}, status=200, elapsed_ms={}", potholes.len(), started.elapsed().as_millis());
    Ok(Json(potholes))
}

fn average(readings: &[f64]) -> f64 {
    readings.iter().sum::<f64>() / readings.len() as f64
}

fn stdev(readings: &[f64]) -> f64 {
    let mean = average(readings);

    let variance = readings
        .iter()
        .map(|reading| (reading - mean).powi(2))
        .sum::<f64>()
        / readings.len() as f64;

    variance.sqrt()
}

fn calibration_threshold(vertical_axis: &[f64]) -> Option<f64> {
    if vertical_axis.len() < WINDOW_SIZE
        || vertical_axis.iter().any(|value| !value.is_finite())
    {
        return None;
    }

    vertical_axis
        .windows(WINDOW_SIZE)
        .map(stdev)
        .reduce(f64::max)
}

static POTHOLES_FILE_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

fn read_potholes() -> Result<Vec<PotholeLocation>, StatusCode> {
    match fs::read_to_string(POTHOLES_PATH) {
        Ok(json) => serde_json::from_str(&json)
            .map_err(|error| {
            eprintln!("[ERROR] read_potholes: parse potholes JSON: {error}; status=500");
            StatusCode::INTERNAL_SERVER_ERROR
        }),

        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            println!("[INFO] {POTHOLES_PATH} does not exist yet; returning an empty collection");
            Ok(Vec::new())
        }

        Err(error) => {
            eprintln!("[ERROR] Cannot read {POTHOLES_PATH}: {error}; status=500");
            Err(StatusCode::INTERNAL_SERVER_ERROR)
        },
    }
}

fn save_potholes(locations: Vec<PotholeLocation>) -> Result<(), StatusCode> {
    if locations.is_empty() {
        println!("[INFO] No candidate locations to save");
        return Ok(());
    }
    let candidates = locations.len();

    let _guard = POTHOLES_FILE_LOCK
        .lock()
        .map_err(|error| {
            eprintln!("[ERROR] save_potholes: potholes file lock: {error}; status=500");
            StatusCode::INTERNAL_SERVER_ERROR
        })?;

    let mut potholes = Vec::new();

    // Also remove nearby duplicates already stored in the file.
    let stored = read_potholes()?;
    let previous_count = stored.len();
    for location in stored {
        add_unique_pothole(&mut potholes, location);
    }
    let removed_duplicates = previous_count - potholes.len();

    let mut added = 0;

    for location in locations {
        if add_unique_pothole(&mut potholes, location) {
            added += 1;
        }
    }

    let json = serde_json::to_string_pretty(&potholes)
        .map_err(|error| {
            eprintln!("[ERROR] save_potholes: serialize or write potholes JSON: {error}; status=500");
            StatusCode::INTERNAL_SERVER_ERROR
        })?;

    fs::write(POTHOLES_PATH, json)
        .map_err(|error| {
            eprintln!("[ERROR] save_potholes: serialize or write potholes JSON: {error}; status=500");
            StatusCode::INTERNAL_SERVER_ERROR
        })?;

    println!("[INFO] Saved {POTHOLES_PATH}: new_locations={added}, skipped_nearby={}, removed_stored_duplicates={removed_duplicates}, total={}", candidates - added, potholes.len());

    Ok(())
}

fn distance_meters(a: &PotholeLocation, b: &PotholeLocation) -> f64 {
    const EARTH_RADIUS_M: f64 = 6_371_000.0;

    let mean_latitude = ((a.latitude + b.latitude) / 2.0).to_radians();

    let north = (b.latitude - a.latitude).to_radians()
        * EARTH_RADIUS_M;

    let east = (b.longitude - a.longitude).to_radians()
        * EARTH_RADIUS_M
        * mean_latitude.cos();

    north.hypot(east) // sqrt(north² + east²)
}

/// Returns true when a new location is added.
fn add_unique_pothole(
    potholes: &mut Vec<PotholeLocation>,
    location: PotholeLocation,
) -> bool {
    let already_known = potholes.iter().any(|existing| {
        distance_meters(existing, &location) <= POTHOLE_GROUP_RADIUS_M
    });

    if already_known {
        return false;
    }

    potholes.push(location);
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    fn chunk(started_at: &str) -> SampleChunk {
        SampleChunk {
            started_at: started_at.to_owned(),
            samples: vec![Sample { x: 0.0, y: 0.0, z: 9.8, latitude: 54.0, longitude: 25.0 }],
        }
    }

    fn fresh_buffer() -> LiveBuffer {
        LiveBuffer { next_seq: 1, chunks: VecDeque::new(), labels: VecDeque::new() }
    }

    #[test]
    fn live_buffer_cursor_returns_only_newer_items() {
        let mut buffer = fresh_buffer();
        let first = buffer.push_chunk(chunk("a"));
        let label_seq = buffer.push_label(LabeledEvent {
            timestamp: 1, latitude: 54.0, longitude: 25.0, label: EventLabel::Bump,
        });
        let second = buffer.push_chunk(chunk("b"));
        assert_eq!((first, label_seq, second), (1, 2, 3));

        let all = buffer.since(0);
        assert_eq!(all.chunks.len(), 2);
        assert_eq!(all.labels.len(), 1);
        assert_eq!(all.next, 3);

        let newer = buffer.since(all.next);
        assert!(newer.chunks.is_empty() && newer.labels.is_empty());
        assert_eq!(newer.next, 3);

        let after_first = buffer.since(first);
        assert_eq!(after_first.chunks.len(), 1);
        assert_eq!(after_first.chunks[0].chunk.started_at, "b");
        assert_eq!(after_first.labels.len(), 1);
    }

    #[test]
    fn live_buffer_drops_oldest_chunks_beyond_capacity() {
        let mut buffer = fresh_buffer();
        for i in 0..(LIVE_CHUNK_CAPACITY + 5) {
            buffer.push_chunk(chunk(&i.to_string()));
        }
        assert_eq!(buffer.chunks.len(), LIVE_CHUNK_CAPACITY);
        assert_eq!(buffer.chunks.front().unwrap().chunk.started_at, "5");
        assert_eq!(buffer.since(0).next, (LIVE_CHUNK_CAPACITY + 5) as u64);
    }

    #[test]
    fn live_chunk_serializes_flat() {
        let json = serde_json::to_value(LiveChunk { seq: 7, chunk: chunk("t") }).unwrap();
        assert_eq!(json["seq"], 7);
        assert_eq!(json["started_at"], "t");
        assert_eq!(json["samples"][0]["z"], 9.8);
    }

    #[test]
    fn stdev_first_dataset_window_matches_expected_value() {
        let readings = [
            9.394, 9.155, 9.547, 9.956, 10.070,
            9.949, 9.885, 9.776, 9.755, 10.128,
            10.170, 9.944, 9.616, 9.790, 10.041,
            9.780, 9.869, 9.857, 9.757, 9.682,
        ];

        let actual = stdev(&readings);

        let expected = 0.24029575006645465;
        let tolerance = 1e-10;

        assert!(
            (actual - expected).abs() < tolerance,
            "Expected {expected}, got {actual}"
        );
    }

    #[test]
    fn detect_potholes_constant_acceleration_returns_no_detections() {
        let readings = vec![9.8; WINDOW_SIZE * 2];

        let detections = detect_potholes(&readings, 0.5);

        assert!(detections.is_empty());
    }

    #[test]
    fn detect_potholes_flags_windows_containing_an_impact() {
        let mut readings = vec![0.0; 60];
        readings[30] = 10.0;

        let detections = detect_potholes(&readings, 2.0);

        // Every 20-sample window containing the impact has:
        // standard deviation = sqrt(4.75) ≈ 2.179.
        // Those windows end at indices 30 through 49.
        let expected: Vec<usize> = (30..=49).collect();

        assert_eq!(detections, expected);
    }

    #[test]
    fn detect_potholes_value_equal_to_threshold_is_detected() {
        // Equal numbers of -1 and +1 have mean 0 and stdev 1.
        let readings: Vec<f64> = (0..WINDOW_SIZE)
            .map(|index| if index % 2 == 0 { -1.0 } else { 1.0 })
            .collect();

        let detections = detect_potholes(&readings, 1.0);

        assert_eq!(detections, vec![WINDOW_SIZE - 1]);
    }

    #[test]
    fn detect_potholes_incomplete_window_returns_no_detections() {
        let readings = vec![9.8; WINDOW_SIZE - 1];

        let detections = detect_potholes(&readings, 0.5);

        assert!(detections.is_empty());
    }

    #[test]
    fn calibration_threshold_uses_maximum_window_stdev() {
        let mut readings = vec![0.0; WINDOW_SIZE];

        readings.extend(
            (0..WINDOW_SIZE)
                .map(|index| if index % 2 == 0 { -2.0 } else { 2.0 }),
        );

        // The final window has mean 0 and stdev 2.
        // Earlier windows contain zeros and cannot exceed stdev 2.
        // Whole-chunk stdev would instead be sqrt(2).
        let actual = calibration_threshold(&readings).unwrap();

        assert!((actual - 2.0).abs() < 1e-10);
    }

    #[test]
    fn calibration_threshold_incomplete_window_returns_none() {
        let readings = vec![9.8; WINDOW_SIZE - 1];

        assert_eq!(calibration_threshold(&readings), None);
    }

    #[test]
    fn calibration_threshold_non_finite_readings_returns_none() {
        for invalid in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            let mut readings = vec![9.8; WINDOW_SIZE];
            readings[5] = invalid;

            assert_eq!(calibration_threshold(&readings), None);
        }
    }

    fn sample_chunk_with_distinct_locations() -> SampleChunk {
        SampleChunk {
            started_at: "0".to_owned(),
            samples: (0..60)
                .map(|index| Sample {
                    x: 0.0,
                    y: 0.0,
                    z: 9.8,
                    latitude: index as f64,
                    longitude: -(index as f64),
                })
                .collect(),
        }
    }

    #[test]
    fn detected_location_matches_last_sample_of_flagged_window() {
        let mut chunk = sample_chunk_with_distinct_locations();

        // An impact at the final sample flags only the final window.
        chunk.samples[59].z = 19.8;

        let locations = detect_pothole_locations(&chunk, 2.0);

        assert_eq!(
            locations,
            vec![PotholeLocation {
                latitude: 59.0,
                longitude: -59.0,
            }]
        );
    }

    #[test]
    fn overlapping_detections_map_to_correct_sample_locations() {
        let mut chunk = sample_chunk_with_distinct_locations();
        chunk.samples[30].z = 19.8;

        let locations = detect_pothole_locations(&chunk, 2.0);

        // Windows containing sample 30 end at indices 30 through 49.
        let expected: Vec<PotholeLocation> = (30..=49)
            .map(|index| PotholeLocation {
                latitude: index as f64,
                longitude: -(index as f64),
            })
            .collect();

        assert_eq!(locations, expected);
    }

    #[test]
    fn smooth_chunk_returns_no_pothole_locations() {
        let chunk = sample_chunk_with_distinct_locations();

        let locations = detect_pothole_locations(&chunk, 2.0);

        assert!(locations.is_empty());
    }

    #[test]
    fn same_location_is_saved_only_once() {
        let mut potholes = Vec::new();

        assert!(add_unique_pothole(
            &mut potholes,
            PotholeLocation {
                latitude: 54.6872,
                longitude: 25.2797,
            },
        ));

        assert!(!add_unique_pothole(
            &mut potholes,
            PotholeLocation {
                latitude: 54.6872,
                longitude: 25.2797,
            },
        ));

        assert_eq!(potholes.len(), 1);
    }

    #[test]
    fn nearby_location_is_skipped_and_original_is_preserved() {
        let original = PotholeLocation {
            latitude: 54.6872,
            longitude: 25.2797,
        };

        let mut potholes = vec![original];

        // Approximately 5.6 metres north.
        let added = add_unique_pothole(
            &mut potholes,
            PotholeLocation {
                latitude: 54.68725,
                longitude: 25.2797,
            },
        );

        assert!(!added);
        assert_eq!(
            potholes,
            vec![PotholeLocation {
                latitude: 54.6872,
                longitude: 25.2797,
            }]
        );
    }

    #[test]
    fn distant_location_is_saved_separately() {
        let mut potholes = vec![PotholeLocation {
            latitude: 54.6872,
            longitude: 25.2797,
        }];

        // Approximately 111 metres north.
        let added = add_unique_pothole(
            &mut potholes,
            PotholeLocation {
                latitude: 54.6882,
                longitude: 25.2797,
            },
        );

        assert!(added);
        assert_eq!(potholes.len(), 2);
    }
}