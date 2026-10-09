use std::{io};

use axum::{Json, Router, http::StatusCode, routing::{get, post}};
use serde::Deserialize;

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

#[tokio::main]
async fn main() -> io::Result<()> {
    let app = Router::new()
        .route("/api/readings", post(post_readings))
        .route("/api/potholes", get(get_potholes));

    let listener = tokio::net::TcpListener::bind("0.0.0.0:3000").await?;
    axum::serve(listener, app).await
}

async fn post_readings(Json(sample_chunk): Json<SampleChunk>) -> StatusCode {
    println!("POST readings");

    StatusCode::OK
}

async fn get_potholes() -> StatusCode {
    println!("GET potholes");
    StatusCode::OK
}