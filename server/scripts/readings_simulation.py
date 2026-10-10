#!/usr/bin/env python3
"""Simulate 100 Hz acceleration and POST 1-second chunks to /api/readings."""
import argparse
import json
import math
import random
import sys
import time
import urllib.error
import urllib.request

def generate_chunk(number, mode, amplitude, rng, started_at, latitude, longitude):
    has_impact = mode == 'impact' or (mode == 'mixed' and number % 5 == 2)
    samples = []
    for i in range(100):
        impact = amplitude * (1 if i % 2 == 0 else -1) if has_impact and 40 <= i < 60 else 0.0
        # Synthetic path moves northeast; position updates once per chunk.
        samples.append(dict(x=rng.uniform(-0.03, 0.03), y=rng.uniform(-0.03, 0.03),
                            z=9.81 + rng.uniform(-0.02, 0.02) + impact,
                            latitude=latitude + number * 0.00005,
                            longitude=longitude + number * 0.00005))
    return dict(started_at=str(started_at), samples=samples), has_impact

def post_json(url, payload, device=None):
    headers = {'Content-Type': 'application/json'}
    if device:
        headers['X-Forwarded-For'] = device  # the server keys data by client IP; this pretends to be another device
    request = urllib.request.Request(url, data=json.dumps(payload, allow_nan=False).encode(),
                                     headers=headers, method='POST')
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.status

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:3000')
    parser.add_argument('--device', help='Pretend to be this device IP (sent as X-Forwarded-For)')
    parser.add_argument('--chunks', type=int, default=20)
    parser.add_argument('--mode', choices=['smooth', 'impact', 'mixed'], default='mixed')
    parser.add_argument('--amplitude', type=float, default=3.0)
    parser.add_argument('--latitude', type=float, default=54.6872)
    parser.add_argument('--longitude', type=float, default=25.2797)
    parser.add_argument('--seed', type=int, default=123)
    parser.add_argument('--fast', action='store_true', help='Send without one-second pacing')
    parser.add_argument('--dry-run', action='store_true', help='Print request JSON without sending it')
    args = parser.parse_args()
    if args.chunks < 1:
        parser.error('chunks must be positive')
    if not math.isfinite(args.amplitude) or args.amplitude <= 0:
        parser.error('amplitude must be finite and positive')
    if not -90 <= args.latitude <= 90 or not -180 <= args.longitude <= 180:
        parser.error('coordinates are out of range')
    if args.latitude + (args.chunks - 1) * 0.00005 > 90:
        parser.error('simulated path goes beyond latitude 90')
    if args.longitude + (args.chunks - 1) * 0.00005 > 180:
        parser.error('simulated path goes beyond longitude 180')
    rng = random.Random(args.seed)
    start = time.time_ns() // 1_000_000
    wall_start = time.monotonic()
    for number in range(args.chunks):
        if not args.fast and not args.dry_run:
            time.sleep(max(0, wall_start + number - time.monotonic()))
        chunk, impact = generate_chunk(number, args.mode, args.amplitude, rng,
                                       start + number * 1000, args.latitude, args.longitude)
        if args.dry_run:
            print(json.dumps(chunk, allow_nan=False))
        else:
            status = post_json(args.base_url.rstrip('/') + '/api/readings', chunk, args.device)
            print(f'Chunk {number + 1}: HTTP {status}; {"impact" if impact else "smooth"}; '
                  f'location=({chunk["samples"][0]["latitude"]:.6f}, {chunk["samples"][0]["longitude"]:.6f})', file=sys.stderr)

if __name__ == '__main__':
    try:
        main()
    except (urllib.error.URLError, OSError, ValueError) as error:
        print(f'Simulation failed: {error}', file=sys.stderr)
        sys.exit(1)

