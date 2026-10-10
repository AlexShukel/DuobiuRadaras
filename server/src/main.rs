use core::f64;
use std::{fs, io};

use axum::{Json, Router, http::StatusCode, routing::{get, post}};
use serde::{Deserialize, Serialize};

const WINDOW_SIZE: usize = 20;
const CALIBRATION_TABLE_PATH: &str = "./calibration.json";
const POTHOLES_PATH: &str = "./potholes.json";

#[derive(Deserialize, Serialize, Debug)]
struct PotholeLocation {
    latitude: f64,
    longitude: f64,
}

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

async fn post_readings(Json(sample_chunk): Json<SampleChunk>) -> Result<StatusCode, StatusCode> {
    println!("POST readings");

    let vertical_axis: Vec<f64> = sample_chunk
        .samples
        .iter()
        .map(|sample| sample.z)
        .collect();

    if vertical_axis.len() < WINDOW_SIZE
        || vertical_axis.iter().any(|value| !value.is_finite())
    {
        return Err(StatusCode::BAD_REQUEST);
    }

    let json = fs::read_to_string(CALIBRATION_TABLE_PATH)
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;

    let table: CalibrationTable = serde_json::from_str(&json)
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;

    if table.thresholds.is_empty()
        || table.thresholds.iter().any(|value| !value.is_finite() || *value < 0.0)
    {
        return Err(StatusCode::INTERNAL_SERVER_ERROR);
    }

    let threshold = table.thresholds
        .iter()
        .copied()
        .reduce(f64::min)
        .ok_or(StatusCode::INTERNAL_SERVER_ERROR)?;

    let detections = detect_potholes(&vertical_axis, threshold);

    for index in detections {
        let sample = &sample_chunk.samples[index];

        println!(
            "Possible pothole at sample {index}: latitude={}, longitude={}",
            sample.latitude,
            sample.longitude
        );
    }

    Ok(StatusCode::OK)
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
    let vertical_axis: Vec<f64> = sample_chunk.samples.iter().map(|sample| sample.z).collect();

    let threshold = calibration_threshold(&vertical_axis)
        .ok_or(StatusCode::BAD_REQUEST)?;
    
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

#[cfg(test)]
mod tests {
    use super::*;

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
}