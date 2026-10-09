import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';
import { fileURLToPath } from 'node:url';
import { getPotholes } from './mock/potholes.js';

const PORT = Number(process.env.PORT) || 3000;
const PUBLIC_DIR = fileURLToPath(new URL('./public/', import.meta.url));
const MAX_COUNT = 1000;

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
    const count = Number(url.searchParams.get('count') ?? 50);
    if (!Number.isInteger(count) || count < 0 || count > MAX_COUNT) {
      sendJson(res, 400, { error: `count must be an integer between 0 and ${MAX_COUNT}` });
      return;
    }
    sendJson(res, 200, getPotholes(count));
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
});
