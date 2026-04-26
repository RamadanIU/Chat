/**
 * Agent Terminal Server — замена ttyd
 * Простой WebSocket-сервер с PTY
 *
 * Эндпоинты:
 *   ws://HOST:PORT/term   — интерактивный терминал (raw text)
 *   ws://HOST:PORT/exec   — выполнение команд (JSON in/out)
 *   http://HOST:PORT/     — проверка доступности (healthcheck)
 *
 * Протокол /term:
 *   Сервер → клиент: строки (вывод PTY)
 *   Клиент → сервер: строка (ввод) или JSON {"type":"resize","cols":N,"rows":N}
 *
 * Протокол /exec:
 *   Клиент → сервер: JSON {"cmd":"ls -la","dir":"/home/user"}
 *   Сервер → клиент: JSON {"stdout":"...","exit_code":0,"duration_ms":123}
 *   Соединение закрывается автоматически после ответа
 *
 * Запуск:
 *   node server.js
 *   PORT=9000 TOKEN=mysecret node server.js
 */

const { WebSocketServer } = require('ws');
const pty  = require('node-pty');
const { execFile } = require('child_process');
const http = require('http');
const url  = require('url');
const os   = require('os');

const PORT  = parseInt(process.env.PORT  || '8765', 10);
const TOKEN = process.env.TOKEN || '';   // Пустая строка = без авторизации
const SHELL = process.env.SHELL || (process.platform === 'win32' ? 'powershell.exe' : 'bash');
const HOME  = process.env.HOME  || os.homedir();

// ──────────────────────────────────────────────────────────────────────────────
// HTTP healthcheck + CORS preflight
// ──────────────────────────────────────────────────────────────────────────────
const httpServer = http.createServer((req, res) => {
  res.setHeader('Access-Control-Allow-Origin',  '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', '*');

  if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return; }

  res.writeHead(200, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ ok: true, server: 'agent-terminal', version: '1.0' }));
});

// ──────────────────────────────────────────────────────────────────────────────
// WebSocket server
// ──────────────────────────────────────────────────────────────────────────────
const wss = new WebSocketServer({ server: httpServer });

wss.on('connection', (ws, req) => {
  const parsed   = url.parse(req.url, true);
  const pathname = parsed.pathname;
  const query    = parsed.query;

  // ── Авторизация по токену ──────────────────────────────────────────────────
  if (TOKEN && query.token !== TOKEN) {
    ws.send(JSON.stringify({ error: 'Unauthorized — неверный токен' }));
    ws.close(4001, 'Unauthorized');
    return;
  }

  // ══════════════════════════════════════════════════════════════════════════
  // /term — интерактивный PTY-терминал
  // ══════════════════════════════════════════════════════════════════════════
  if (pathname === '/term') {
    const cols = Math.max(10, parseInt(query.cols, 10) || 80);
    const rows = Math.max(2,  parseInt(query.rows, 10) || 24);

    let proc;
    try {
      proc = pty.spawn(SHELL, [], {
        name: 'xterm-256color',
        cols, rows,
        cwd:  HOME,
        env:  { ...process.env, TERM: 'xterm-256color', COLORTERM: 'truecolor' },
      });
    } catch (err) {
      ws.send(`\x1b[31mОшибка запуска PTY: ${err.message}\x1b[0m\r\n`);
      ws.close();
      return;
    }

    // PTY → WebSocket
    proc.onData(data => {
      if (ws.readyState === ws.OPEN) ws.send(data);
    });

    // WebSocket → PTY
    ws.on('message', msg => {
      const text = msg.toString();
      // Проверяем — может это resize JSON?
      if (text.startsWith('{')) {
        try {
          const parsed = JSON.parse(text);
          if (parsed.type === 'resize' && parsed.cols && parsed.rows) {
            proc.resize(
              Math.max(10, parsed.cols),
              Math.max(2,  parsed.rows)
            );
            return;
          }
        } catch { /* не JSON — отправляем как ввод */ }
      }
      proc.write(text);
    });

    proc.onExit(() => {
      if (ws.readyState === ws.OPEN) ws.close();
    });
    ws.on('close', () => {
      try { proc.kill(); } catch {}
    });

  // ══════════════════════════════════════════════════════════════════════════
  // /exec — выполнение команды, возвращает JSON-результат
  // ══════════════════════════════════════════════════════════════════════════
  } else if (pathname === '/exec') {

    ws.on('message', msg => {
      let cmd = '', dir = HOME;
      try {
        const parsed = JSON.parse(msg.toString());
        cmd = (parsed.cmd || '').trim();
        dir = parsed.dir || HOME;
      } catch {
        cmd = msg.toString().trim();
      }

      if (!cmd) {
        ws.send(JSON.stringify({ error: 'Команда не указана', exit_code: 1 }));
        return;
      }

      const t0 = Date.now();

      execFile('bash', ['-c', cmd], {
        cwd:       dir,
        timeout:   120_000,
        maxBuffer: 50 * 1024 * 1024,
        env: {
          ...process.env,
          TERM:        'xterm-256color',
          FORCE_COLOR: '1',
          COLORTERM:   'truecolor',
        },
      }, (err, stdout, stderr) => {
        const output = ((stdout || '') + (stderr ? '\n' + stderr : '')).trimEnd();
        ws.send(JSON.stringify({
          stdout:      output,
          exit_code:   err ? (typeof err.code === 'number' ? err.code : 1) : 0,
          duration_ms: Date.now() - t0,
        }));
        // Закрываем после ответа — клиент откроет новое соединение для следующей команды
        ws.close();
      });
    });

  // ══════════════════════════════════════════════════════════════════════════
  // Неизвестный путь
  // ══════════════════════════════════════════════════════════════════════════
  } else {
    ws.send(JSON.stringify({
      error: `Неверный эндпоинт: ${pathname}. Используйте /term или /exec`,
    }));
    ws.close();
  }
});

// ──────────────────────────────────────────────────────────────────────────────
httpServer.listen(PORT, () => {
  const ifaces = Object.values(os.networkInterfaces())
    .flat().filter(i => !i.internal && i.family === 'IPv4').map(i => i.address);

  console.log('\n🟢 Agent Terminal Server запущен\n');
  console.log(`   Интерактивный:   ws://localhost:${PORT}/term`);
  console.log(`   Команды (exec):  ws://localhost:${PORT}/exec`);
  if (ifaces.length) {
    console.log(`\n   Внешние IP:`);
    ifaces.forEach(ip => {
      console.log(`     ws://${ip}:${PORT}/term  |  ws://${ip}:${PORT}/exec`);
    });
  }
  if (TOKEN) {
    console.log(`\n   🔑 Токен: ${TOKEN}  (передавать как ?token=TOKEN)`);
  } else {
    console.log(`\n   ⚠️  Токен не задан — сервер открыт без авторизации`);
    console.log(`      Задайте:  TOKEN=мойпароль node server.js`);
  }
  console.log('');
});
