#!/usr/bin/env node
/**
 * agent-pro-bridge — локальный мост для запуска stdio MCP-серверов из браузера.
 *
 * Слушает ws://127.0.0.1:7777, по сообщению { type:'spawn', config:{ command, args, env, cwd } }
 * запускает дочерний процесс и проксирует JSON-RPC между браузером и его stdin/stdout.
 *
 * Запуск:
 *   npm install
 *   npm start
 *
 * Совместим с любым stdio MCP-сервером, например:
 *   npx -y @modelcontextprotocol/server-filesystem /path/to/dir
 *   npx -y @modelcontextprotocol/server-git --repository /path/to/repo
 */
import { WebSocketServer } from 'ws';
import { spawn } from 'node:child_process';
import http from 'node:http';

const PORT = Number(process.env.AGENT_PRO_BRIDGE_PORT || 7777);
const HOST = process.env.AGENT_PRO_BRIDGE_HOST || '127.0.0.1';
const VERSION = '1.1.0';

// WebSocket keepalive. Без этих ping'ов TCP-сокет может «тихо умереть»
// от idle-timeout'ов NAT/прокси/мобильных операторов: обе стороны считают
// соединение открытым, но трафик уже не ходит. Браузерный WebSocket сам
// автоматически отвечает pong'ом на серверные ping-фреймы, так что от
// frontend-кода ничего не требуется.
const HEARTBEAT_INTERVAL_MS = Number(process.env.AGENT_PRO_BRIDGE_PING_MS || 25_000);

const httpServer = http.createServer((req, res) => {
  // CORS для health-check из браузера
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return; }
  if (req.url === '/health') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, service: 'agent-pro-bridge', version: VERSION }));
    return;
  }
  res.writeHead(404, { 'Content-Type': 'text/plain' });
  res.end('Not found');
});

const wss = new WebSocketServer({ server: httpServer });

// Каждые HEARTBEAT_INTERVAL_MS шлём ping всем клиентам. Если на предыдущий
// ping не пришёл pong (isAlive остался false) — рвём соединение через
// terminate(): close() ждал бы close-handshake, который уже не дойдёт.
const heartbeatTimer = setInterval(() => {
  for (const ws of wss.clients) {
    if (ws.isAlive === false) {
      try { ws.terminate(); } catch {}
      continue;
    }
    ws.isAlive = false;
    try { ws.ping(); } catch {}
  }
}, HEARTBEAT_INTERVAL_MS);
wss.on('close', () => { clearInterval(heartbeatTimer); });

wss.on('connection', (ws, req) => {
  const remoteAddr = req.socket.remoteAddress;
  console.log(`[bridge] connection from ${remoteAddr}`);
  let proc = null;
  let stdoutBuf = '';

  ws.isAlive = true;
  ws.on('pong', () => { ws.isAlive = true; });

  const sendJson = (obj) => { try { ws.send(JSON.stringify(obj)); } catch {} };

  ws.on('message', (raw) => {
    let msg;
    try { msg = JSON.parse(raw.toString()); }
    catch { sendJson({ type: 'error', message: 'Invalid JSON' }); return; }

    if (msg.type === 'spawn') {
      if (proc) { sendJson({ type: 'error', message: 'Process already spawned' }); return; }
      const cfg = msg.config || {};
      const command = cfg.command;
      if (!command) { sendJson({ type: 'error', message: 'Missing command' }); return; }
      const args = Array.isArray(cfg.args) ? cfg.args : [];
      const env = { ...process.env, ...(cfg.env || {}) };
      const cwd = cfg.cwd || undefined;

      console.log(`[bridge] spawn: ${command} ${args.join(' ')}`);
      try {
        proc = spawn(command, args, { env, cwd, stdio: ['pipe', 'pipe', 'pipe'], shell: false });
      } catch (e) {
        sendJson({ type: 'error', message: 'Spawn failed: ' + e.message });
        return;
      }

      proc.on('error', (err) => {
        console.error('[bridge] proc error:', err.message);
        sendJson({ type: 'error', message: err.message });
      });
      proc.on('exit', (code, signal) => {
        console.log(`[bridge] exit code=${code} signal=${signal}`);
        sendJson({ type: 'exit', code, signal });
        try { ws.close(); } catch {}
      });

      proc.stdout.on('data', (chunk) => {
        stdoutBuf += chunk.toString('utf8');
        let idx;
        while ((idx = stdoutBuf.indexOf('\n')) !== -1) {
          const line = stdoutBuf.slice(0, idx).trim();
          stdoutBuf = stdoutBuf.slice(idx + 1);
          if (!line) continue;
          let payload;
          try { payload = JSON.parse(line); }
          catch { sendJson({ type: 'stderr', text: '[non-json from server stdout] ' + line }); continue; }
          sendJson({ type: 'rpc', payload });
        }
      });

      proc.stderr.on('data', (chunk) => {
        const text = chunk.toString('utf8');
        process.stderr.write('[server stderr] ' + text);
        sendJson({ type: 'stderr', text });
      });

      sendJson({ type: 'ready', pid: proc.pid });
      return;
    }

    if (msg.type === 'rpc') {
      if (!proc || !proc.stdin.writable) {
        sendJson({ type: 'error', message: 'No process to write to' });
        return;
      }
      try {
        proc.stdin.write(JSON.stringify(msg.payload) + '\n');
      } catch (e) {
        sendJson({ type: 'error', message: 'stdin write failed: ' + e.message });
      }
      return;
    }
  });

  ws.on('close', () => {
    console.log('[bridge] ws closed');
    if (proc) { try { proc.kill(); } catch {} proc = null; }
  });
  ws.on('error', (err) => console.error('[bridge] ws error:', err.message));
});

httpServer.listen(PORT, HOST, () => {
  console.log(`agent-pro-bridge ${VERSION} listening on http://${HOST}:${PORT}`);
  console.log(`  WebSocket: ws://${HOST}:${PORT}`);
  console.log(`  Health:    http://${HOST}:${PORT}/health`);
  console.log(`  Heartbeat: ping every ${HEARTBEAT_INTERVAL_MS} ms`);
});
