use core::f64;
use std::{
    collections::{HashMap, VecDeque},
    fs, io,
    net::{IpAddr, SocketAddr},
    path::{Path, PathBuf},
    sync::{Arc, LazyLock, Mutex, MutexGuard},
    time::{Instant, SystemTime, UNIX_EPOCH},
};

use axum::{
    Json, Router,
    extract::{ConnectInfo, Query, Request},
    http::{HeaderMap, StatusCode},
    middleware::{self, Next},
    response::{Html, Response},
    routing::{get, post},
};
use serde::{Deserialize, Serialize, de::DeserializeOwned};

const WINDOW_SIZE: usize = 20;
/// How many recent raw chunks (2 s each) the live view keeps in memory per device: 300 = 10 minutes.
const LIVE_CHUNK_CAPACITY: usize = 300;
const LIVE_LABEL_CAPACITY: usize = 500;
const UI_HTML: &str = include_str!("ui.html");
/// Every device gets its own folder here, named after its IP address:
/// `devices/<ip>/raw.json`, `devices/<ip>/labels.json`, `devices/<ip>/calibration.json`.
const DEVICES_DIR: &str = "./devices";
const RAW_FILE: &str = "raw.json";
const LABELS_FILE: &str = "labels.json";
const CALIBRATION_FILE: &str = "calibration.json";
/// Detected potholes are shared by all devices: the map shows one picture of the road.
const POTHOLES_PATH: &str = "./potholes.json";
const LEGACY_FILES: [&str; 3] = ["./raw.json", "./labels.json", "./calibration.json"];
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
        .route("/api/devices", get(get_devices))
        .route("/", get(get_ui))
        .route("/live", get(get_ui))
        .layer(middleware::from_fn(log_request));

    println!("[INFO] Starting server: window_size={WINDOW_SIZE}, grouping_radius_m={POTHOLE_GROUP_RADIUS_M}");
    println!("[INFO] Data directory: {}", std::env::current_dir()?.display());
    println!("[INFO] Per-device files: {DEVICES_DIR}/<ip>/{{{RAW_FILE},{LABELS_FILE},{CALIBRATION_FILE}}}; shared potholes file: {POTHOLES_PATH}");
    for legacy in LEGACY_FILES {
        if Path::new(legacy).exists() {
            eprintln!("[WARN] {legacy} is no longer used; data now lives in {DEVICES_DIR}/<ip>/");
        }
    }
    match load_devices_from_disk() {
        Ok(loaded) if loaded.is_empty() => println!("[INFO] No devices on disk yet; the first request from a new IP creates its folder"),
        Ok(loaded) => {
            for (ip, chunks, labels) in loaded {
                println!("[INFO] Device {ip}: live view preloaded {chunks} chunks and {labels} labels from disk");
            }
        }
        Err(error) => eprintln!("[WARN] Could not scan {DEVICES_DIR}: {error}"),
    }
    let port = std::env::var("PORT").unwrap_or_else(|_| "3000".to_owned());
    let address = format!("0.0.0.0:{port}");
    let listener = tokio::net::TcpListener::bind(&address).await.map_err(|error| {
        eprintln!("[ERROR] Cannot bind {address}: {error}");
        error
    })?;
    println!("[INFO] Listening on {}", listener.local_addr()?);
    // `with_connect_info` makes the peer address available to handlers; it is the device identifier.
    axum::serve(listener, app.into_make_service_with_connect_info::<SocketAddr>()).await
}

// ---------------------------------------------------------------------------
// Devices: one entry per client IP. Each device has its own folder on disk, its
// own live buffer, and its own mutex, so requests from different phones are
// processed in parallel and only requests from the same phone wait on each other.

struct Device {
    ip: String,
    dir: PathBuf,
    live: LiveBuffer,
    /// Unix milliseconds of the last request from this device (or the newest data file after a restart).
    last_seen_ms: u64,
}

type SharedDevice = Arc<Mutex<Device>>;

static DEVICES: LazyLock<Mutex<HashMap<String, SharedDevice>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

impl Device {
    fn new(ip: &str) -> Self {
        Device {
            ip: ip.to_owned(),
            dir: Path::new(DEVICES_DIR).join(device_dir_name(ip)),
            live: LiveBuffer::new(),
            last_seen_ms: 0,
        }
    }

    fn raw_path(&self) -> PathBuf { self.dir.join(RAW_FILE) }
    fn labels_path(&self) -> PathBuf { self.dir.join(LABELS_FILE) }
    fn calibration_path(&self) -> PathBuf { self.dir.join(CALIBRATION_FILE) }

    fn touch(&mut self) {
        self.last_seen_ms = now_ms();
    }

    /// Seed the live buffer from this device's raw.json / labels.json so the page shows history after a restart.
    fn preload_live(&mut self) -> Result<(usize, usize), String> {
        let raw_path = self.raw_path();
        let labels_path = self.labels_path();
        let chunks: Vec<SampleChunk> = read_json_array(&raw_path)
            .map_err(|e| format!("{}: {e}", raw_path.display()))?;
        let labels: Vec<LabeledEvent> = read_json_array(&labels_path)
            .map_err(|e| format!("{}: {e}", labels_path.display()))?;

        let skip = chunks.len().saturating_sub(LIVE_CHUNK_CAPACITY);
        let mut n_chunks = 0;
        for chunk in chunks.into_iter().skip(skip) {
            self.live.push_chunk(chunk);
            n_chunks += 1;
        }
        let skip = labels.len().saturating_sub(LIVE_LABEL_CAPACITY);
        let mut n_labels = 0;
        for label in labels.into_iter().skip(skip) {
            self.live.push_label(label);
            n_labels += 1;
        }

        self.last_seen_ms = [raw_path, labels_path]
            .iter()
            .filter_map(|path| fs::metadata(path).ok()?.modified().ok())
            .filter_map(|modified| modified.duration_since(UNIX_EPOCH).ok())
            .map(|age| age.as_millis() as u64)
            .max()
            .unwrap_or(0);
        Ok((n_chunks, n_labels))
    }
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// IPv6 addresses contain colons, which are not valid in file names everywhere.
fn device_dir_name(ip: &str) -> String {
    ip.replace(':', "_")
}

fn device_ip_from_dir_name(name: &str) -> String {
    name.replace('_', ":")
}

/// IPv4 clients of a dual-stack socket show up as `::ffff:a.b.c.d`; report them as plain IPv4.
fn canonical_ip(ip: IpAddr) -> IpAddr {
    match ip {
        IpAddr::V6(v6) => v6.to_ipv4_mapped().map(IpAddr::V4).unwrap_or(ip),
        v4 => v4,
    }
}

/// The device identifier: the first `X-Forwarded-For` address when the server sits behind
/// a proxy (or when a test deliberately sets it), otherwise the TCP peer address.
fn client_ip(headers: &HeaderMap, peer: SocketAddr) -> String {
    let forwarded = headers
        .get("x-forwarded-for")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.split(',').next())
        .and_then(|value| value.trim().parse::<IpAddr>().ok());
    canonical_ip(forwarded.unwrap_or(peer.ip())).to_string()
}

fn lock_or_500<'a, T>(mutex: &'a Mutex<T>, what: &str) -> Result<MutexGuard<'a, T>, StatusCode> {
    mutex.lock().map_err(|error| {
        eprintln!("[ERROR] {what} lock failed: {error}");
        StatusCode::INTERNAL_SERVER_ERROR
    })
}

/// Find or register the device for an IP. Registration is cheap; folders are created on first write.
fn device_for(ip: &str) -> Result<SharedDevice, StatusCode> {
    let mut registry = lock_or_500(&DEVICES, "device registry")?;
    Ok(registry
        .entry(ip.to_owned())
        .or_insert_with(|| {
            println!("[INFO] New device: {ip} -> {DEVICES_DIR}/{}", device_dir_name(ip));
            Arc::new(Mutex::new(Device::new(ip)))
        })
        .clone())
}

fn registered_device(ip: &str) -> Result<Option<SharedDevice>, StatusCode> {
    Ok(lock_or_500(&DEVICES, "device registry")?.get(ip).cloned())
}

fn all_devices() -> Result<Vec<SharedDevice>, StatusCode> {
    Ok(lock_or_500(&DEVICES, "device registry")?.values().cloned().collect())
}

/// The device that talked to us most recently; what the live page shows when none is selected.
fn most_recent_device() -> Result<Option<SharedDevice>, StatusCode> {
    let mut best: Option<(u64, SharedDevice)> = None;
    for device in all_devices()? {
        let last_seen = lock_or_500(&device, "device")?.last_seen_ms;
        if best.as_ref().is_none_or(|(seen, _)| last_seen > *seen) {
            best = Some((last_seen, device.clone()));
        }
    }
    Ok(best.map(|(_, device)| device))
}

/// Scan `devices/*/` on startup and preload each live buffer.
fn load_devices_from_disk() -> Result<Vec<(String, usize, usize)>, String> {
    let entries = match fs::read_dir(DEVICES_DIR) {
        Ok(entries) => entries,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => return Err(error.to_string()),
    };
    let mut loaded = Vec::new();
    for entry in entries {
        let entry = entry.map_err(|e| e.to_string())?;
        if !entry.file_type().map_err(|e| e.to_string())?.is_dir() {
            continue;
        }
        let ip = device_ip_from_dir_name(&entry.file_name().to_string_lossy());
        let device = device_for(&ip).map_err(|_| "device registry lock failed".to_owned())?;
        let mut device = device.lock().map_err(|e| format!("device lock: {e}"))?;
        match device.preload_live() {
            Ok((chunks, labels)) => loaded.push((ip, chunks, labels)),
            Err(error) => eprintln!("[WARN] Device {ip}: live view starts empty: {error}"),
        }
    }
    loaded.sort();
    Ok(loaded)
}

/// File work runs on the blocking pool so slow disks never stall the async workers
/// that are serving other devices.
async fn run_blocking<F, T>(task: F) -> Result<T, StatusCode>
where
    F: FnOnce() -> Result<T, StatusCode> + Send + 'static,
    T: Send + 'static,
{
    tokio::task::spawn_blocking(task).await.map_err(|error| {
        eprintln!("[ERROR] blocking task failed: {error}");
        StatusCode::INTERNAL_SERVER_ERROR
    })?
}

fn read_json_array<T: DeserializeOwned>(path: &Path) -> Result<Vec<T>, String> {
    match fs::read_to_string(path) {
        Ok(json) => serde_json::from_str(&json).map_err(|error| format!("invalid JSON: {error}")),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(Vec::new()),
        Err(error) => Err(format!("cannot read: {error}")),
    }
}

fn read_json_array_or_500<T: DeserializeOwned>(path: &Path, context: &str) -> Result<Vec<T>, StatusCode> {
    read_json_array(path).map_err(|error| {
        eprintln!("[ERROR] {context}: {}: {error}", path.display());
        StatusCode::INTERNAL_SERVER_ERROR
    })
}

fn write_json_or_500<T: Serialize>(path: &Path, value: &T, pretty: bool, context: &str) -> Result<(), StatusCode> {
    let json = if pretty { serde_json::to_string_pretty(value) } else { serde_json::to_string(value) }
        .map_err(|error| {
            eprintln!("[ERROR] {context}: serialization failed: {error}");
            StatusCode::INTERNAL_SERVER_ERROR
        })?;
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| {
            eprintln!("[ERROR] {context}: cannot create {}: {error}", parent.display());
            StatusCode::INTERNAL_SERVER_ERROR
        })?;
    }
    fs::write(path, json).map_err(|error| {
        eprintln!("[ERROR] {context}: cannot write {}: {error}", path.display());
        StatusCode::INTERNAL_SERVER_ERROR
    })
}

#[derive(Debug, Serialize)]
struct DeviceSummary {
    device: String,
    last_seen_ms: u64,
    live_chunks: usize,
    live_labels: usize,
    calibrated: bool,
}

async fn get_devices() -> Result<Json<Vec<DeviceSummary>>, StatusCode> {
    let mut summaries = Vec::new();
    for device in all_devices()? {
        let device = lock_or_500(&device, "device")?;
        summaries.push(DeviceSummary {
            device: device.ip.clone(),
            last_seen_ms: device.last_seen_ms,
            live_chunks: device.live.chunks.len(),
            live_labels: device.live.labels.len(),
            calibrated: device.calibration_path().exists(),
        });
    }
    summaries.sort_by(|a, b| b.last_seen_ms.cmp(&a.last_seen_ms).then_with(|| a.device.cmp(&b.device)));
    println!("[INFO] GET /api/devices: devices={}", summaries.len());
    Ok(Json(summaries))
}

async fn post_label(
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Json(event): Json<LabeledEvent>,
) -> Result<StatusCode, StatusCode> {
    let ip = client_ip(&headers, peer);
    println!("[INFO] POST /api/label: device={ip}, timestamp={}, latitude={}, longitude={}, label={:?}",
        event.timestamp, event.latitude, event.longitude, event.label);

    if !event.latitude.is_finite()
        || !event.longitude.is_finite()
        || !(-90.0..=90.0).contains(&event.latitude)
        || !(-180.0..=180.0).contains(&event.longitude)
    {
        eprintln!("[WARN] POST /api/label: invalid coordinates; status=400");
        return Err(StatusCode::BAD_REQUEST);
    }

    let device = device_for(&ip)?;
    run_blocking(move || {
        let mut device = lock_or_500(&device, "device")?;
        device.touch();
        let path = device.labels_path();
        let mut events: Vec<LabeledEvent> = read_json_array_or_500(&path, "POST /api/label")?;
        events.push(event.clone());
        write_json_or_500(&path, &events, true, "POST /api/label")?;
        println!("[INFO] Device {}: appended labeled event; total_labels={}", device.ip, events.len());
        device.live.push_label(event);
        Ok(StatusCode::OK)
    }).await
}

// Also logs requests rejected before a handler runs (for example, invalid JSON).
async fn log_request(request: Request, next: Next) -> Response {
    let method = request.method().clone();
    let path = request.uri().path().to_owned();
    let client = request
        .extensions()
        .get::<ConnectInfo<SocketAddr>>()
        .map(|ConnectInfo(peer)| client_ip(request.headers(), *peer))
        .unwrap_or_else(|| "unknown".to_owned());
    let started = Instant::now();
    println!("[INFO] Request received: {method} {path} from {client}");
    let response = next.run(request).await;
    println!("[INFO] Request finished: {method} {path} from {client}, status={}, elapsed_ms={}",
        response.status().as_u16(), started.elapsed().as_millis());
    println!("=========================");
    response
}

async fn get_health() -> StatusCode {
    println!("[INFO] GET /api/health: status=200");

    StatusCode::OK
}

async fn post_raw(
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Json(sample_chunk): Json<SampleChunk>,
) -> Result<StatusCode, StatusCode> {
    let ip = client_ip(&headers, peer);
    let device = device_for(&ip)?;
    run_blocking(move || {
        let mut device = lock_or_500(&device, "device")?;
        device.touch();
        let path = device.raw_path();
        let mut chunks: Vec<SampleChunk> = read_json_array_or_500(&path, "POST /api/raw")?;

        let sample_count = sample_chunk.samples.len();
        let live_copy = sample_chunk.clone();
        chunks.push(sample_chunk);
        write_json_or_500(&path, &chunks, true, "POST /api/raw")?;

        println!("[INFO] Device {}: appended {sample_count} raw samples; total_chunks={}", device.ip, chunks.len());
        device.live.push_chunk(live_copy);
        Ok(StatusCode::OK)
    }).await
}

// ---------------------------------------------------------------------------
// Live view: a per-device in-memory ring buffer of recent chunks and labels, served to
// the browser page at `/` which polls `GET /api/raw?device=<ip>&after=<seq>` about once
// a second. Chunks and labels share one sequence counter so a single cursor covers both.

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
    fn new() -> Self {
        LiveBuffer { next_seq: 1, chunks: VecDeque::new(), labels: VecDeque::new() }
    }

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
    fn since(&self, after: u64, device: Option<String>) -> LiveResponse {
        let first_new = |seq: u64| seq > after;
        LiveResponse {
            device,
            next: self.next_seq.saturating_sub(1),
            chunks: self.chunks.iter().filter(|c| first_new(c.seq)).cloned().collect(),
            labels: self.labels.iter().filter(|l| first_new(l.seq)).cloned().collect(),
        }
    }
}

#[derive(Debug, Serialize)]
struct LiveResponse {
    /// IP of the device this data belongs to; null when no device has reported yet.
    device: Option<String>,
    /// Pass this back as `?after=` to receive only newer items next time.
    next: u64,
    chunks: Vec<LiveChunk>,
    labels: Vec<LiveLabel>,
}

#[derive(Deserialize)]
struct LiveQuery {
    /// Last sequence number the client has seen; omitted or 0 means "send the whole buffer".
    after: Option<u64>,
    /// Device IP to follow; omitted means the device that reported most recently.
    device: Option<String>,
}

async fn get_raw_live(Query(query): Query<LiveQuery>) -> Result<Json<LiveResponse>, StatusCode> {
    let after = query.after.unwrap_or(0);
    let device = match query.device.as_deref().map(str::trim).filter(|s| !s.is_empty()) {
        Some(ip) => match registered_device(ip)? {
            Some(device) => device,
            None => {
                eprintln!("[WARN] GET /api/raw: unknown device {ip}; status=404");
                return Err(StatusCode::NOT_FOUND);
            }
        },
        None => match most_recent_device()? {
            Some(device) => device,
            None => {
                println!("[INFO] GET /api/raw: no devices yet");
                return Ok(Json(LiveBuffer::new().since(after, None)));
            }
        },
    };
    let device = lock_or_500(&device, "device")?;
    let response = device.live.since(after, Some(device.ip.clone()));
    println!("[INFO] GET /api/raw: device={} after={after} -> chunks={} labels={} next={}",
        device.ip, response.chunks.len(), response.labels.len(), response.next);
    Ok(Json(response))
}

async fn get_ui() -> Html<&'static str> {
    Html(UI_HTML)
}

async fn post_readings(
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Json(sample_chunk): Json<SampleChunk>,
) -> Result<StatusCode, StatusCode> {
    let started = Instant::now();
    let ip = client_ip(&headers, peer);
    println!("[INFO] POST /api/readings: device={ip}, chunk_started_at={:?}, samples={}", sample_chunk.started_at, sample_chunk.samples.len());

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

    let device = device_for(&ip)?;
    run_blocking(move || {
        let table = {
            let mut device = lock_or_500(&device, "device")?;
            device.touch();
            read_calibration(&device.calibration_path(), "POST /api/readings")?
        };

        if table.thresholds.is_empty()
            || table.thresholds.iter().any(|value| !value.is_finite() || *value < 0.0)
        {
            eprintln!("[ERROR] POST /api/readings: device {ip}: calibration thresholds are empty or invalid; status=500");
            return Err(StatusCode::INTERNAL_SERVER_ERROR);
        }

        let threshold = table.thresholds
            .iter()
            .copied()
            .reduce(f64::min)
            .ok_or(StatusCode::INTERNAL_SERVER_ERROR)?;

        println!("[INFO] Readings: device={ip}, threshold={threshold:.6}, calibration_entries={}, windows={}",
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
    }).await
}

/// A device must calibrate before its readings can be classified; a missing file is an error, not an empty table.
fn read_calibration(path: &Path, context: &str) -> Result<CalibrationTable, StatusCode> {
    let json = fs::read_to_string(path).map_err(|error| {
        if error.kind() == io::ErrorKind::NotFound {
            eprintln!("[ERROR] {context}: {} does not exist; POST /api/calibration from this device first; status=500", path.display());
        } else {
            eprintln!("[ERROR] {context}: cannot read {}: {error}; status=500", path.display());
        }
        StatusCode::INTERNAL_SERVER_ERROR
    })?;
    serde_json::from_str(&json).map_err(|error| {
        eprintln!("[ERROR] {context}: parse calibration JSON {}: {error}; status=500", path.display());
        StatusCode::INTERNAL_SERVER_ERROR
    })
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
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Json(sample_chunk): Json<SampleChunk>,
) -> Result<StatusCode, StatusCode> {
    let started = Instant::now();
    let ip = client_ip(&headers, peer);
    println!("[INFO] POST /api/calibration: device={ip}, chunk_started_at={:?}, samples={}", sample_chunk.started_at, sample_chunk.samples.len());
    let vertical_axis: Vec<f64> = sample_chunk.samples.iter().map(|sample| sample.z).collect();

    let threshold = calibration_threshold(&vertical_axis).ok_or_else(|| {
        eprintln!("[WARN] POST /api/calibration: insufficient samples or non-finite acceleration; status=400");
        StatusCode::BAD_REQUEST
    })?;
    println!("[INFO] Calibration: windows={}, maximum_window_stdev={threshold:.6}", vertical_axis.len() - WINDOW_SIZE + 1);

    let device = device_for(&ip)?;
    run_blocking(move || {
        let mut device = lock_or_500(&device, "device")?;
        device.touch();
        let path = device.calibration_path();
        let mut thresholds = vec![threshold];

        if path.exists() {
            thresholds.extend(read_calibration(&path, "POST /api/calibration")?.thresholds);
        }

        let calibration_table = CalibrationTable { thresholds };
        println!("[INFO] Calibration: device={}, saving {} thresholds to {}", device.ip, calibration_table.thresholds.len(), path.display());
        write_json_or_500(&path, &calibration_table, false, "POST /api/calibration")?;
        println!("[INFO] POST /api/calibration complete: status=200, elapsed_ms={}", started.elapsed().as_millis());
        Ok(StatusCode::OK)
    }).await
}

async fn get_potholes() -> Result<Json<Vec<PotholeLocation>>, StatusCode> {
    let started = Instant::now();
    println!("[INFO] GET /api/potholes");
    let potholes = run_blocking(|| {
        let _guard = lock_or_500(&POTHOLES_FILE_LOCK, "potholes file")?;
        read_potholes()
    }).await?;
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

static POTHOLES_FILE_LOCK: Mutex<()> = Mutex::new(());

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

    let _guard = lock_or_500(&POTHOLES_FILE_LOCK, "potholes file")?;

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

    write_json_or_500(Path::new(POTHOLES_PATH), &potholes, true, "save_potholes")?;

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

    fn peer(ip: &str) -> SocketAddr {
        SocketAddr::new(ip.parse().unwrap(), 51234)
    }

    #[test]
    fn client_ip_uses_peer_address_without_proxy_header() {
        assert_eq!(client_ip(&HeaderMap::new(), peer("192.168.1.20")), "192.168.1.20");
    }

    #[test]
    fn client_ip_prefers_first_forwarded_address() {
        let mut headers = HeaderMap::new();
        headers.insert("x-forwarded-for", "10.0.0.7, 172.16.0.1".parse().unwrap());
        assert_eq!(client_ip(&headers, peer("127.0.0.1")), "10.0.0.7");
    }

    #[test]
    fn client_ip_ignores_malformed_forwarded_header() {
        let mut headers = HeaderMap::new();
        headers.insert("x-forwarded-for", "not-an-ip".parse().unwrap());
        assert_eq!(client_ip(&headers, peer("127.0.0.1")), "127.0.0.1");
    }

    #[test]
    fn client_ip_unmaps_ipv4_mapped_ipv6() {
        assert_eq!(client_ip(&HeaderMap::new(), peer("::ffff:192.168.1.5")), "192.168.1.5");
    }

    #[test]
    fn device_dir_name_round_trips_ipv6() {
        let ip = "fe80::1";
        let name = device_dir_name(ip);
        assert!(!name.contains(':'));
        assert_eq!(device_ip_from_dir_name(&name), ip);
        assert_eq!(device_dir_name("10.0.0.1"), "10.0.0.1");
    }

    #[test]
    fn device_files_live_under_its_own_folder() {
        let device = Device::new("10.0.0.1");
        assert_eq!(device.raw_path(), Path::new(DEVICES_DIR).join("10.0.0.1").join(RAW_FILE));
        assert_eq!(device.labels_path(), Path::new(DEVICES_DIR).join("10.0.0.1").join(LABELS_FILE));
        assert_eq!(device.calibration_path(), Path::new(DEVICES_DIR).join("10.0.0.1").join(CALIBRATION_FILE));
    }

    #[test]
    fn same_ip_maps_to_the_same_device_and_different_ips_do_not() {
        let a = device_for("203.0.113.1").unwrap();
        let a_again = device_for("203.0.113.1").unwrap();
        let b = device_for("203.0.113.2").unwrap();
        assert!(Arc::ptr_eq(&a, &a_again));
        assert!(!Arc::ptr_eq(&a, &b));
        assert_eq!(registered_device("203.0.113.2").unwrap().map(|d| d.lock().unwrap().ip.clone()), Some("203.0.113.2".to_owned()));
        assert!(registered_device("203.0.113.250").unwrap().is_none());
    }

    #[test]
    fn live_buffer_cursor_returns_only_newer_items() {
        let mut buffer = LiveBuffer::new();
        let first = buffer.push_chunk(chunk("a"));
        let label_seq = buffer.push_label(LabeledEvent {
            timestamp: 1, latitude: 54.0, longitude: 25.0, label: EventLabel::Bump,
        });
        let second = buffer.push_chunk(chunk("b"));
        assert_eq!((first, label_seq, second), (1, 2, 3));

        let all = buffer.since(0, Some("10.0.0.1".to_owned()));
        assert_eq!(all.device.as_deref(), Some("10.0.0.1"));
        assert_eq!(all.chunks.len(), 2);
        assert_eq!(all.labels.len(), 1);
        assert_eq!(all.next, 3);

        let newer = buffer.since(all.next, None);
        assert!(newer.chunks.is_empty() && newer.labels.is_empty());
        assert_eq!(newer.next, 3);

        let after_first = buffer.since(first, None);
        assert_eq!(after_first.chunks.len(), 1);
        assert_eq!(after_first.chunks[0].chunk.started_at, "b");
        assert_eq!(after_first.labels.len(), 1);
    }

    #[test]
    fn live_buffer_drops_oldest_chunks_beyond_capacity() {
        let mut buffer = LiveBuffer::new();
        for i in 0..(LIVE_CHUNK_CAPACITY + 5) {
            buffer.push_chunk(chunk(&i.to_string()));
        }
        assert_eq!(buffer.chunks.len(), LIVE_CHUNK_CAPACITY);
        assert_eq!(buffer.chunks.front().unwrap().chunk.started_at, "5");
        assert_eq!(buffer.since(0, None).next, (LIVE_CHUNK_CAPACITY + 5) as u64);
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
