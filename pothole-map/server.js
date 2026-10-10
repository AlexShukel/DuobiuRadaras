import { createServer } from 'node:http';
import { readFile, readdir, stat } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';
import { fileURLToPath } from 'node:url';
import { getPotholes } from './mock/potholes.js';

const PORT = Number(process.env.PORT) || 3000;
const HOST = process.env.HOST ?? '0.0.0.0';
const PUBLIC_DIR = fileURLToPath(new URL('./public/', import.meta.url));
const POTHOLES_API_URL = process.env.POTHOLES_API_URL ?? 'http://100.72.8.35:3001/api/potholes';
const USE_MOCK = process.env.USE_MOCK === '1';
const UPSTREAM_TIMEOUT_MS = 5000;
// Training data collected by ../server: one folder per phone with raw.json (sensor chunks with
// GPS) and labels.json (human-pressed pothole / bump events).
const DEVICES_DIR = process.env.DEVICES_DIR ?? fileURLToPath(new URL('../server/devices/', import.meta.url));
// A pause longer than this between consecutive chunks starts a new route segment (dropped
// packets, app restarted, phone put away), so the map does not draw a straight line across the gap.
const ROUTE_GAP_MS = 10_000;
const SAMPLE_PERIOD_MS = 25;

const MIME_TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
};

function sendJson(res, status, body) {
  res.writeHead(status, { 'Content-Type': MIME_TYPES['.json'] });
  res.end(JSON.stringify(body));
}

// Fetches potholes from the detection server and maps them to the frontend's { id, lat, lng } shape.
async function fetchPotholes() {
  const response = await fetch(POTHOLES_API_URL, {
    signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
  });
  if (!response.ok) {
    throw new Error(`upstream responded with HTTP ${response.status}`);
  }
  const locations = await response.json();
  return locations.map(({ latitude, longitude }, i) => ({
    id: i + 1,
    lat: latitude,
    lng: longitude,
  }));
}

function isFix(lat, lng) {
  return Number.isFinite(lat) && Number.isFinite(lng) && !(lat === 0 && lng === 0);
}

// Turns a device's raw chunks into a list of polylines: consecutive identical GPS fixes are
// collapsed (the phone reports a new fix about once a second while samples arrive at 40 Hz),
// and a long pause between chunks starts a new polyline.
function buildRoute(chunks) {
  const segments = [];
  let segment = [];
  let prevEnd = null;

  for (const chunk of chunks) {
    const start = Date.parse(chunk.started_at);
    const samples = Array.isArray(chunk.samples) ? chunk.samples : [];
    if (prevEnd !== null && Number.isFinite(start) && start - prevEnd > ROUTE_GAP_MS && segment.length) {
      segments.push(segment);
      segment = [];
    }
    for (const { latitude, longitude } of samples) {
      if (!isFix(latitude, longitude)) continue;
      const last = segment[segment.length - 1];
      if (last && last[0] === latitude && last[1] === longitude) continue;
      segment.push([latitude, longitude]);
    }
    if (Number.isFinite(start)) {
      prevEnd = start + samples.length * SAMPLE_PERIOD_MS;
    }
  }
  if (segment.length) segments.push(segment);
  // A lone fix cannot be drawn as a line.
  return segments.filter((points) => points.length > 1);
}

async function readJsonArray(path) {
  try {
    const parsed = JSON.parse(await readFile(path, 'utf8'));
    return Array.isArray(parsed) ? parsed : [];
  } catch (err) {
    if (err.code === 'ENOENT') return [];
    throw err;
  }
}

async function mtimeOf(path) {
  try {
    return (await stat(path)).mtimeMs;
  } catch {
    return 0;
  }
}

async function loadDevice(ip) {
  const dir = join(DEVICES_DIR, ip);
  const [chunks, labels] = await Promise.all([
    readJsonArray(join(dir, 'raw.json')),
    readJsonArray(join(dir, 'labels.json')),
  ]);
  const starts = chunks.map((c) => Date.parse(c.started_at)).filter(Number.isFinite);
  return {
    ip,
    chunks: chunks.length,
    from: starts.length ? new Date(Math.min(...starts)).toISOString() : null,
    to: starts.length ? new Date(Math.max(...starts)).toISOString() : null,
    route: buildRoute(chunks),
    labels: labels
      .filter((l) => isFix(l.latitude, l.longitude))
      .map(({ timestamp, latitude, longitude, label }) => ({ timestamp, lat: latitude, lng: longitude, label })),
  };
}

// raw.json files are tens of megabytes, so the parsed dataset is cached per device and only
// rebuilt when raw.json or labels.json changes on disk.
const datasetCache = new Map();

async function loadDataset() {
  let entries;
  try {
    entries = await readdir(DEVICES_DIR, { withFileTypes: true });
  } catch (err) {
    if (err.code === 'ENOENT') return { devices: [] };
    throw err;
  }
  const ips = entries.filter((e) => e.isDirectory()).map((e) => e.name).sort();

  const devices = await Promise.all(ips.map(async (ip) => {
    const dir = join(DEVICES_DIR, ip);
    const version = `${await mtimeOf(join(dir, 'raw.json'))}:${await mtimeOf(join(dir, 'labels.json'))}`;
    const cached = datasetCache.get(ip);
    if (cached && cached.version === version) return cached.device;
    const device = await loadDevice(ip);
    datasetCache.set(ip, { version, device });
    return device;
  }));

  return { devices: devices.filter((d) => d.chunks > 0 || d.labels.length > 0) };
}

async function serveStatic(pathname, res) {
  const relative = normalize(pathname === '/' ? '/index.html' : pathname);
  const filePath = join(PUBLIC_DIR, relative);

  // Block path traversal outside of public/.
  if (!filePath.startsWith(PUBLIC_DIR)) {
    res.writeHead(403).end('Forbidden');
    return;
  }

  try {
    const content = await readFile(filePath);
    res.writeHead(200, {
      'Content-Type': MIME_TYPES[extname(filePath)] ?? 'application/octet-stream',
    });
    res.end(content);
  } catch {
    res.writeHead(404).end('Not found');
  }
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);

  if (req.method === 'GET' && url.pathname === '/api/potholes') {
    if (USE_MOCK) {
      sendJson(res, 200, getPotholes());
      return;
    }
    try {
      sendJson(res, 200, await fetchPotholes());
    } catch (err) {
      console.error(`Failed to fetch potholes from ${POTHOLES_API_URL}:`, err.message);
      sendJson(res, 502, { error: 'Failed to fetch potholes from upstream service' });
    }
    return;
  }

  if (req.method === 'GET' && url.pathname === '/api/dataset') {
    try {
      sendJson(res, 200, await loadDataset());
    } catch (err) {
      console.error(`Failed to read training data from ${DEVICES_DIR}:`, err.message);
      sendJson(res, 500, { error: 'Failed to read training data' });
    }
    return;
  }

  if (req.method === 'GET') {
    await serveStatic(url.pathname, res);
    return;
  }

  res.writeHead(405).end('Method not allowed');
});

server.listen(PORT, HOST, () => {
  console.log(`Pothole map running at http://${HOST}:${PORT}`);
  console.log(USE_MOCK ? 'Serving mock pothole data' : `Proxying potholes from ${POTHOLES_API_URL}`);
  console.log(`Training data from ${DEVICES_DIR}`);
});
