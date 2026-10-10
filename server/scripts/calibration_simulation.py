#!/usr/bin/env python3
"""Send synthetic known-impact passages to POST /api/calibration (not physical calibration)."""
import argparse
import json
import math
import random
import statistics
import sys
import time
import urllib.error
import urllib.request

WINDOW_SIZE = 20

def generate_chunk(amplitude, seed, started_at, latitude=54.6872, longitude=25.2797):
    rng = random.Random(seed)
    samples = []
    for i in range(100):
        impact = amplitude * (1 if i % 2 == 0 else -1) if 40 <= i < 60 else 0.0
        samples.append(dict(x=rng.uniform(-0.03, 0.03), y=rng.uniform(-0.03, 0.03),
                            z=9.81 + rng.uniform(-0.02, 0.02) + impact,
                            latitude=latitude, longitude=longitude))
    return dict(started_at=str(started_at), samples=samples)

def max_stdev(chunk):
    z = [s['z'] for s in chunk['samples']]
    return max(statistics.pstdev(z[i:i + WINDOW_SIZE]) for i in range(len(z) - WINDOW_SIZE + 1))

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
    parser.add_argument('--amplitudes', type=float, nargs='+', default=[1.5, 2.0, 2.5])
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--dry-run', action='store_true', help='Print request JSON without sending it')
    args = parser.parse_args()
    if any(not math.isfinite(a) or a <= 0 for a in args.amplitudes):
        parser.error('amplitudes must be finite and positive')
    start = time.time_ns() // 1_000_000
    thresholds = []
    for i, amplitude in enumerate(args.amplitudes):
        chunk = generate_chunk(amplitude, args.seed + i, start + i * 1000)
        maximum = max_stdev(chunk)
        thresholds.append(maximum)
        if args.dry_run:
            print(json.dumps(chunk, allow_nan=False))
        else:
            status = post_json(args.base_url.rstrip('/') + '/api/calibration', chunk, args.device)
            print(f'Passage {i + 1}: HTTP {status}; maximum window stdev={maximum:.6f} m/s^2', file=sys.stderr)
    print(f'Minimum threshold for THESE passages: {min(thresholds):.6f} m/s^2', file=sys.stderr)
    print('The server appends to existing calibration; old values can lower its effective threshold.', file=sys.stderr)

if __name__ == '__main__':
    try:
        main()
    except (urllib.error.URLError, OSError, ValueError) as error:
        print(f'Calibration failed: {error}', file=sys.stderr)
        sys.exit(1)

