#!/usr/bin/env python3
"""Replay a recorded raw.json to the server, chunk by chunk, as if a phone were driving now.

Default target is POST /api/detect, so the neural network labels the drive as it is replayed and
the live page shows its markers; use --endpoint /api/raw to replay without detection.

    python3 scripts/replay_raw.py devices/100.74.153.26/raw.json --device 10.0.0.5 --fast
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def post_json(url, payload, device=None):
    headers = {'Content-Type': 'application/json'}
    if device:
        headers['X-Forwarded-For'] = device  # the server keys data by client IP
    request = urllib.request.Request(url, data=json.dumps(payload, allow_nan=False).encode(),
                                     headers=headers, method='POST')
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.status, response.read()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('raw', help='raw.json to replay (array of chunks)')
    parser.add_argument('--base-url', default='http://127.0.0.1:3000')
    parser.add_argument('--endpoint', default='/api/detect', help='/api/detect (default) or /api/raw')
    parser.add_argument('--device', help='Pretend to be this device IP (sent as X-Forwarded-For)')
    parser.add_argument('--start', type=int, default=0, help='first chunk index')
    parser.add_argument('--count', type=int, help='how many chunks to send (default: all)')
    parser.add_argument('--fast', action='store_true', help='send without two-second pacing')
    parser.add_argument('--shift-to-now', action='store_true',
                        help='rewrite started_at so the replay appears to happen now (live page shows it as live)')
    args = parser.parse_args()

    with open(args.raw) as fh:
        chunks = json.load(fh)
    if isinstance(chunks, dict):
        chunks = [chunks]
    chunks = chunks[args.start:args.start + args.count if args.count else None]
    if not chunks:
        sys.exit('nothing to send')

    from datetime import datetime, timezone, timedelta
    shift = timedelta(0)
    if args.shift_to_now:
        first = datetime.fromisoformat(chunks[0]['started_at'].replace('Z', '+00:00'))
        shift = datetime.now(timezone.utc) - first

    url = args.base_url.rstrip('/') + args.endpoint
    events = 0
    wall_start = time.monotonic()
    for number, chunk in enumerate(chunks):
        if args.shift_to_now:
            t = datetime.fromisoformat(chunk['started_at'].replace('Z', '+00:00')) + shift
            chunk = dict(chunk, started_at=t.strftime('%Y-%m-%dT%H:%M:%S.') + f'{t.microsecond // 1000:03d}Z')
        if not args.fast:
            period = len(chunk['samples']) * 0.025
            target = wall_start + number * period
            time.sleep(max(0.0, target - time.monotonic()))
        try:
            status, body = post_json(url, chunk, args.device)
        except urllib.error.HTTPError as error:
            print(f'chunk {number}: HTTP {error.code} {error.read()[:200]!r}', file=sys.stderr)
            continue
        except urllib.error.URLError as error:
            sys.exit(f'chunk {number}: {error}')
        if args.endpoint.endswith('/detect'):
            reply = json.loads(body)
            if reply.get('detector_error'):
                print(f'chunk {number}: stored but not scored: {reply["detector_error"]}', file=sys.stderr)
            for event in reply.get('events', []):
                events += 1
                print(f'chunk {number}: {event["label"]} at {event["timestamp"]} score={event["score"]:.2f} '
                      f'({event["latitude"]:.5f}, {event["longitude"]:.5f})')
    print(f'sent {len(chunks)} chunks to {url}; events={events}', file=sys.stderr)


if __name__ == '__main__':
    main()
