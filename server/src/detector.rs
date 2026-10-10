//! The neural-network detector: a long-lived Python worker (`python -m pothole_ml.serve`)
//! that the server spawns on demand and talks to over stdin/stdout, one JSON object per line.
//!
//! Loading torch and the model takes a couple of seconds, so the worker is started once and
//! kept; every `POST /api/detect` chunk costs a round trip of a few milliseconds. Requests are
//! serialised through one mutex because the worker answers in order. If the worker dies or
//! stops answering, it is dropped and the next request starts a fresh one.
//!
//! Configuration (environment variables, all optional):
//! - `DETECTOR_ML_DIR`    the `ml/` folder with the `pothole_ml` package (default `../ml`)
//! - `DETECTOR_PYTHON`    interpreter (default `<ML_DIR>/.venv/bin/python`, else `python3`)
//! - `DETECTOR_ARTIFACTS` folder with `model.pt` and `config.json` (default `<ML_DIR>/artifacts`)

use std::{
    path::{Path, PathBuf},
    process::Stdio,
    sync::LazyLock,
    time::{Duration, Instant},
};

use serde::{Deserialize, Serialize};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    process::{Child, ChildStdin, ChildStdout, Command},
    sync::Mutex,
};

use crate::{EventLabel, SampleChunk};

const REQUEST_TIMEOUT: Duration = Duration::from_secs(15);
/// Model start-up (torch import, weights) is slow the first time; wait longer for the ping.
const STARTUP_TIMEOUT: Duration = Duration::from_secs(60);

/// One event the model found. `score = 1 - p_none`.
#[derive(Debug, Deserialize, Serialize, Clone)]
pub struct PredictedEvent {
    pub timestamp: u64,
    pub latitude: f64,
    pub longitude: f64,
    pub label: EventLabel,
    pub score: f64,
}

#[derive(Debug, Deserialize)]
struct WorkerReply {
    #[serde(default)]
    events: Vec<PredictedEvent>,
    #[serde(default)]
    windows: usize,
    #[serde(default)]
    error: Option<String>,
    #[serde(default)]
    ok: Option<bool>,
    #[serde(default)]
    model: Option<String>,
    #[serde(default)]
    threshold: Option<f64>,
}

#[derive(Debug, Clone, Serialize)]
pub struct Detection {
    pub events: Vec<PredictedEvent>,
    /// Windows scored for this chunk (including the rolling buffer of previous chunks).
    pub windows: usize,
    pub elapsed_ms: u128,
}

pub struct DetectorConfig {
    pub ml_dir: PathBuf,
    pub python: PathBuf,
    pub artifacts: PathBuf,
}

impl DetectorConfig {
    pub fn from_env() -> Self {
        let ml_dir = PathBuf::from(std::env::var("DETECTOR_ML_DIR").unwrap_or_else(|_| "../ml".to_owned()));
        let python = match std::env::var("DETECTOR_PYTHON") {
            Ok(p) => PathBuf::from(p),
            Err(_) => {
                let venv = ml_dir.join(".venv").join("bin").join("python");
                if venv.exists() { venv } else { PathBuf::from("python3") }
            }
        };
        let artifacts = std::env::var("DETECTOR_ARTIFACTS")
            .map(PathBuf::from)
            .unwrap_or_else(|_| ml_dir.join("artifacts"));
        DetectorConfig { ml_dir, python, artifacts }
    }

    /// Paths the worker receives are relative to its own working directory (`ml_dir`), so make them absolute.
    fn absolute(&self, path: &Path) -> PathBuf {
        if path.is_absolute() {
            path.to_path_buf()
        } else {
            std::env::current_dir().map(|cwd| cwd.join(path)).unwrap_or_else(|_| path.to_path_buf())
        }
    }

    pub fn describe(&self) -> String {
        format!("python={} ml_dir={} artifacts={}", self.python.display(), self.ml_dir.display(), self.artifacts.display())
    }
}

struct Worker {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
    model: String,
    threshold: f64,
}

static WORKER: LazyLock<Mutex<Option<Worker>>> = LazyLock::new(|| Mutex::new(None));

async fn spawn_worker(config: &DetectorConfig) -> Result<Worker, String> {
    let model = config.absolute(&config.artifacts.join("model.pt"));
    let model_config = config.absolute(&config.artifacts.join("config.json"));
    for path in [&model, &model_config] {
        if !path.exists() {
            return Err(format!("{} does not exist; train the model first (see ml/README.md)", path.display()));
        }
    }
    let mut child = Command::new(&config.python)
        .args(["-m", "pothole_ml.serve", "--model"])
        .arg(&model)
        .arg("--config")
        .arg(&model_config)
        .current_dir(&config.ml_dir)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .kill_on_drop(true)
        .spawn()
        .map_err(|error| format!("cannot start {} in {}: {error}", config.python.display(), config.ml_dir.display()))?;
    let stdin = child.stdin.take().ok_or("worker stdin unavailable")?;
    let stdout = BufReader::new(child.stdout.take().ok_or("worker stdout unavailable")?);
    let mut worker = Worker { child, stdin, stdout, model: String::new(), threshold: f64::NAN };

    let reply = tokio::time::timeout(STARTUP_TIMEOUT, exchange(&mut worker, r#"{"ping":true}"#))
        .await
        .map_err(|_| format!("detector did not answer the start-up ping within {}s", STARTUP_TIMEOUT.as_secs()))??;
    if reply.ok != Some(true) {
        return Err(format!("unexpected start-up reply from the detector: error={:?}", reply.error));
    }
    worker.model = reply.model.unwrap_or_default();
    worker.threshold = reply.threshold.unwrap_or(f64::NAN);
    Ok(worker)
}

async fn exchange(worker: &mut Worker, line: &str) -> Result<WorkerReply, String> {
    worker.stdin.write_all(line.as_bytes()).await.map_err(|e| format!("write to detector: {e}"))?;
    worker.stdin.write_all(b"\n").await.map_err(|e| format!("write to detector: {e}"))?;
    worker.stdin.flush().await.map_err(|e| format!("write to detector: {e}"))?;
    let mut reply = String::new();
    let n = worker.stdout.read_line(&mut reply).await.map_err(|e| format!("read from detector: {e}"))?;
    if n == 0 {
        let status = worker.child.try_wait().ok().flatten();
        return Err(format!("detector exited (status={status:?}); see its stderr above"));
    }
    serde_json::from_str(&reply).map_err(|e| format!("invalid reply from detector: {e}: {}", reply.trim()))
}

/// Lock the worker slot, starting a worker when none is running.
async fn running_worker() -> Result<tokio::sync::MutexGuard<'static, Option<Worker>>, String> {
    let mut slot = WORKER.lock().await;
    if slot.is_none() {
        let config = DetectorConfig::from_env();
        println!("[INFO] Detector: starting worker ({})", config.describe());
        let worker = spawn_worker(&config).await?;
        println!("[INFO] Detector: ready, model={} threshold={}", worker.model, worker.threshold);
        *slot = Some(worker);
    }
    Ok(slot)
}

/// Make sure a worker is running; returns a description of the loaded model.
pub async fn ensure_started() -> Result<String, String> {
    let slot = running_worker().await?;
    let worker = slot.as_ref().expect("worker present");
    Ok(format!("model={} threshold={}", worker.model, worker.threshold))
}

/// Score one chunk of a device. Starts the worker when needed. A worker that died (or hung)
/// is replaced and the chunk is sent once more, so a crash costs one restart, not a chunk.
pub async fn detect(device_ip: &str, chunk: &SampleChunk) -> Result<Detection, String> {
    #[derive(Serialize)]
    struct Request<'a> {
        device: &'a str,
        chunk: &'a SampleChunk,
    }
    let line = serde_json::to_string(&Request { device: device_ip, chunk }).map_err(|e| e.to_string())?;

    let mut slot = running_worker().await?;
    for attempt in 1..=2 {
        let worker = match slot.as_mut() {
            Some(worker) => worker,
            None => {
                let config = DetectorConfig::from_env();
                println!("[INFO] Detector: restarting worker ({})", config.describe());
                let worker = spawn_worker(&config).await?;
                println!("[INFO] Detector: ready, model={} threshold={}", worker.model, worker.threshold);
                slot.insert(worker)
            }
        };
        let started = Instant::now();
        let outcome = tokio::time::timeout(REQUEST_TIMEOUT, exchange(worker, &line))
            .await
            .map_err(|_| format!("detector did not answer within {}s", REQUEST_TIMEOUT.as_secs()))
            .and_then(|r| r);
        match outcome {
            Ok(reply) => {
                if let Some(error) = reply.error {
                    // A bad chunk, not a dead worker: keep it running.
                    return Err(format!("detector rejected the chunk: {error}"));
                }
                return Ok(Detection { events: reply.events, windows: reply.windows, elapsed_ms: started.elapsed().as_millis() });
            }
            Err(error) => {
                eprintln!("[ERROR] Detector: {error} (attempt {attempt})");
                *slot = None;
                if attempt == 2 {
                    return Err(error);
                }
            }
        }
    }
    unreachable!("the retry loop returns on its last attempt")
}
