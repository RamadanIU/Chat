#!/usr/bin/env node
/**
 * agent-browser CLI shim.
 *
 * Forwards command-line arguments to the persistent daemon at
 * 127.0.0.1:9876. If the daemon is not running, it is started in the
 * background (detached, with stdout/stderr redirected to a log file)
 * and we wait for /health to become reachable before forwarding the
 * actual command.
 *
 * Exit code, stdout, and stderr mirror what the daemon returns —
 * so this behaves exactly like a real CLI.
 */

'use strict';

const http  = require('http');
const fs    = require('fs');
const os    = require('os');
const path  = require('path');
const { spawn } = require('child_process');

const PORT = parseInt(process.env.AGENT_BROWSER_PORT || '9876', 10);
const HOST = '127.0.0.1';
const DAEMON_PATH = path.resolve(__dirname, 'daemon.js');
const LOG_DIR = path.join(os.homedir(), '.cache', 'agent-browser');
const LOG_FILE = path.join(LOG_DIR, 'daemon.log');
const STARTUP_TIMEOUT_MS = 30000;

function httpRequest(opts, body) {
  return new Promise((resolve, reject) => {
    const req = http.request(opts, (res) => {
      let buf = '';
      res.on('data', (c) => { buf += c; });
      res.on('end', () => resolve({ status: res.statusCode, body: buf }));
    });
    req.on('error', reject);
    req.setTimeout(60 * 60 * 1000); // 1h hard cap
    if (body) req.write(body);
    req.end();
  });
}

async function isDaemonAlive() {
  try {
    const r = await httpRequest({ host: HOST, port: PORT, path: '/health', method: 'GET', timeout: 1500 });
    return r.status === 200;
  } catch (_) { return false; }
}

async function waitForDaemon(timeoutMs) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (await isDaemonAlive()) return true;
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}

async function spawnDaemon() {
  fs.mkdirSync(LOG_DIR, { recursive: true });
  const out = fs.openSync(LOG_FILE, 'a');
  const child = spawn(process.execPath, [DAEMON_PATH], {
    detached: true,
    stdio: ['ignore', out, out],
    env: process.env,
  });
  child.unref();
}

async function ensureDaemon() {
  if (await isDaemonAlive()) return;
  await spawnDaemon();
  const ok = await waitForDaemon(STARTUP_TIMEOUT_MS);
  if (!ok) {
    process.stderr.write('agent-browser: daemon failed to start within '
      + (STARTUP_TIMEOUT_MS/1000) + 's. See log: ' + LOG_FILE + '\n');
    process.exit(4);
  }
}

async function main() {
  const argv = process.argv.slice(2);

  // Special case: `agent-browser kill` — send to running daemon if any, otherwise no-op.
  if (argv[0] === 'kill') {
    if (await isDaemonAlive()) {
      try { await httpRequest({ host: HOST, port: PORT, path: '/shutdown', method: 'POST' }); } catch (_) {}
    }
    process.stdout.write('killed\n');
    process.exit(0);
  }

  await ensureDaemon();

  const body = JSON.stringify({ argv });
  let response;
  try {
    response = await httpRequest({
      host: HOST, port: PORT, path: '/exec', method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
    }, body);
  } catch (e) {
    process.stderr.write('agent-browser: failed to reach daemon: ' + e.message + '\n');
    process.exit(5);
  }

  let parsed;
  try { parsed = JSON.parse(response.body); }
  catch (_) {
    process.stderr.write('agent-browser: bad daemon response (' + response.status + '): ' + response.body + '\n');
    process.exit(6);
  }

  if (parsed.stdout) process.stdout.write(parsed.stdout);
  if (parsed.stderr) process.stderr.write(parsed.stderr);
  process.exit(typeof parsed.exit_code === 'number' ? parsed.exit_code : 0);
}

main().catch((e) => {
  process.stderr.write('agent-browser: ' + (e && e.message || String(e)) + '\n');
  process.exit(7);
});
