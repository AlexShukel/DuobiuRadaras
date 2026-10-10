use core::f64;
use std::{fs, io};

use axum::{Json, Router, http::StatusCode, routing::{get, post}};
use serde::{Deserialize, Serialize};

#[derive(Deserialize, Debug)]
struct SampleChunk {
    started_at: u64, // in milliseconds
    samples: Vec<Sample>
}

#[derive(Deserialize, Debug)]
struct Sample {
    x: f64,
    y: f64,
    z: f64,
    latitude: f64,
    longitude: f64   
}

#[derive(Clone)]
struct Config {
    threshold: f64
}

#[derive(Deserialize, Serialize, Clone)]
struct CalibrationTable {
    thresholds: Vec<f64>
}

#[tokio::main]
async fn main() -> io::Result<()> {
    let app = Router::new()
        .route("/api/readings", post(post_readings))
        .route("/api/potholes", get(get_potholes))
        .route("/api/calibration", post(post_calibration));

    let listener = tokio::net::TcpListener::bind("0.0.0.0:3000").await?;
    axum::serve(listener, app).await
}

async fn post_readings(Json(sample_chunk): Json<SampleChunk>) -> StatusCode {
    println!("POST readings");

    StatusCode::OK
}

async fn post_calibration(
    Json(sample_chunk): Json<SampleChunk>,
) -> Result<StatusCode, StatusCode> {
    const CALIBRATION_TABLE_PATH: &str = "./calibration.json";

    let vertical_axis: Vec<f64> = sample_chunk.samples.iter().map(|sample| sample.z).collect();

    let threshold = stdev(&vertical_axis);
    let mut thresholds = vec![threshold];

    if fs::exists(CALIBRATION_TABLE_PATH).map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)? {
        let json = fs::read_to_string(CALIBRATION_TABLE_PATH)
            .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;

        let calibration_table: CalibrationTable = serde_json::from_str(&json)
            .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;

        thresholds.extend(calibration_table.thresholds);
    }

    let calibration_table = CalibrationTable { 
        thresholds
    };

    let json = serde_json::to_string(&calibration_table)
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;

    fs::write(CALIBRATION_TABLE_PATH, &json)
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;

    Ok(StatusCode::OK)
}

async fn get_potholes() -> StatusCode {
    println!("GET potholes");
    StatusCode::OK
}

fn average(readings: &[f64]) -> f64 {
    readings.iter().sum::<f64>() / readings.len() as f64
}

fn stdev(readings: &[f64]) -> f64 {
    let mean = average(readings);
    let sum: f64 = readings
        .iter()
        .map(|reading| (reading - mean).powf(2.0))
        .sum();

    f64::sqrt(sum / readings.len() as f64)
}