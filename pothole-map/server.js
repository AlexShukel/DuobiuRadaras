import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';
import { fileURLToPath } from 'node:url';
import { getPotholes } from './mock/potholes.js';

const PORT = Number(process.env.PORT) || 3000;
const PUBLIC_DIR = fileURLToPath(new URL('./public/', import.meta.url));
const POTHOLES_API_URL = process.env.POTHOLES_API_URL ?? 'http://100.72.8.35:3000/api/potholes';
const USE_MOCK = process.env.USE_MOCK === '1';
const UPSTREAM_TIMEOUT_MS = 5000;

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

  if (req.method === 'GET') {
    await serveStatic(url.pathname, res);
    return;
  }

  res.writeHead(405).end('Method not allowed');
});

server.listen(PORT, () => {
  console.log(`Pothole map running at http://localhost:${PORT}`);
  console.log(USE_MOCK ? 'Serving mock pothole data' : `Proxying potholes from ${POTHOLES_API_URL}`);
});
