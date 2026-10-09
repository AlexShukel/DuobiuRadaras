use std::{io};

use axum::{Router, http::StatusCode, routing::{get, post}};

#[tokio::main]
async fn main() -> io::Result<()> {
    let app = Router::new()
        .route("/api/readings", post(post_readings))
        .route("/api/potholes", get(get_potholes));

    let listener = tokio::net::TcpListener::bind("0.0.0.0:3000").await?;
    axum::serve(listener, app).await
}

async fn post_readings() -> StatusCode {
    println!("POST readings");
    StatusCode::OK
}

async fn get_potholes() -> StatusCode {
    println!("GET potholes");
    StatusCode::OK
}