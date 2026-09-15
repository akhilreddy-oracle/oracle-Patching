// Static assets only. This process imports no controller code and has no SSH,
// Oracle, credentials, or live API bridge. Every API response must be mocked.
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../../webapp/static/', import.meta.url));
const types = { '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.svg': 'image/svg+xml', '.png': 'image/png' };
const server = http.createServer(async (request, response) => {
  response.setHeader('Cache-Control', 'no-store');
  const pathname = new URL(request.url, 'http://127.0.0.1:18765').pathname;
  if (pathname === '/__fixture_health') {
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ mode: 'fixture-only', live_api_available: false })); return;
  }
  if (!['GET', 'HEAD'].includes(request.method) || pathname.startsWith('/api/')) {
    response.writeHead(501, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ error: 'unmocked_fixture_request', message: 'This server has no live API.' })); return;
  }
  try {
    const file = path.resolve(root, '.' + decodeURIComponent(pathname === '/' ? '/index.html' : pathname));
    if (!file.startsWith(root) || !types[path.extname(file)]) throw new Error('not a static asset');
    const stat = await fs.lstat(file);
    if (!stat.isFile() || stat.isSymbolicLink()) throw new Error('not a regular file');
    response.writeHead(200, { 'Content-Type': types[path.extname(file)] });
    response.end(request.method === 'HEAD' ? '' : await fs.readFile(file));
  } catch { response.writeHead(404); response.end('Fixture asset not found'); }
});
server.listen(18765, '127.0.0.1');
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => server.close(() => process.exit(0)));
