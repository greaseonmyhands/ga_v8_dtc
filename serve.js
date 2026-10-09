// Tiny static server for local testing: node serve.js [port]
const http = require('http'), fs = require('fs'), path = require('path');
const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.webmanifest': 'application/manifest+json', '.png': 'image/png' };
http.createServer((q, r) => {
  let f = path.join(__dirname, decodeURIComponent(q.url.split('?')[0]).replace(/^\/+/, '') || 'index.html');
  if (fs.existsSync(f) && fs.statSync(f).isDirectory()) f = path.join(f, 'index.html');
  fs.readFile(f, (e, d) => { if (e) { r.writeHead(404); r.end('not found'); return; } r.writeHead(200, { 'Content-Type': types[path.extname(f)] || 'application/octet-stream', 'Cache-Control': 'no-cache' }); r.end(d); });
}).listen(+process.argv[2] || 8123, () => console.log('http://localhost:' + (+process.argv[2] || 8123)));
