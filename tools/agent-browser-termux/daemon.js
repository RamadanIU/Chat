#!/usr/bin/env node
/**
 * agent-browser-termux daemon
 * ----------------------------
 * Persistent Playwright-based Chromium controller exposing the agent-browser
 * CLI surface over a local HTTP API. Designed to run under Termux on Android,
 * reusing a pre-existing playwright-core + Chromium installation
 * (e.g. ~/playwright-termux/.env with CHROMIUM_PATH).
 *
 * Protocol: POST /exec  {argv:[...], stdin?:""}  ->  {stdout,stderr,exit_code}
 *           GET  /health  ->  {ok:true, ...}
 *           POST /shutdown  ->  process.exit
 *
 * The daemon keeps one BrowserContext + a list of Pages (tabs) open between
 * commands so cookies, localStorage, and form state persist across calls.
 */

'use strict';

const fs   = require('fs');
const path = require('path');
const os   = require('os');
const http = require('http');

// ── Locate playwright-core relative to user's setup ─────────────────────────
const PT_ROOT = process.env.PLAYWRIGHT_TERMUX_ROOT
  || path.join(os.homedir(), 'playwright-termux');

function loadDotEnv(file) {
  try {
    const txt = fs.readFileSync(file, 'utf8');
    for (const line of txt.split(/\r?\n/)) {
      const m = line.match(/^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)\s*$/i);
      if (!m) continue;
      let v = m[2];
      if (/^".*"$/.test(v) || /^'.*'$/.test(v)) v = v.slice(1, -1);
      if (!(m[1] in process.env)) process.env[m[1]] = v;
    }
  } catch (_) {}
}
loadDotEnv(path.join(PT_ROOT, '.env'));

let chromium;
try {
  // Prefer playwright-core installed in the user's project
  chromium = require(path.join(PT_ROOT, 'node_modules', 'playwright-core')).chromium;
} catch (_) {
  try { chromium = require('playwright-core').chromium; }
  catch (_) {
    try { chromium = require('playwright').chromium; }
    catch (e) {
      console.error('[agent-browser] Could not load playwright-core. Run `cd ' + PT_ROOT + ' && npm i playwright-core`.');
      process.exit(2);
    }
  }
}

// ── Config ──────────────────────────────────────────────────────────────────
const PORT = parseInt(process.env.AGENT_BROWSER_PORT || '9876', 10);
const HOST = '127.0.0.1';
const HEADLESS = (process.env.AGENT_BROWSER_HEADLESS ?? 'true') !== 'false';
const DEFAULT_VIEWPORT = {
  width:  parseInt(process.env.AGENT_BROWSER_VIEWPORT_W || '1280', 10),
  height: parseInt(process.env.AGENT_BROWSER_VIEWPORT_H || '800',  10),
};
const CHROMIUM_PATH = process.env.CHROMIUM_PATH;
const STORAGE_STATE_FILE = path.join(os.homedir(), '.cache', 'agent-browser', 'storage-state.json');
const LOG_FILE = path.join(os.homedir(), '.cache', 'agent-browser', 'daemon.log');
const PID_FILE = path.join(os.homedir(), '.cache', 'agent-browser', 'daemon.pid');
const DEFAULT_TIMEOUT = 30000;
const STARTED_AT = Date.now();

fs.mkdirSync(path.dirname(LOG_FILE), { recursive: true });

function log(...a) {
  try {
    fs.appendFileSync(LOG_FILE, '[' + new Date().toISOString() + '] ' + a.join(' ') + '\n');
  } catch (_) {}
}

// ── Browser/page state ──────────────────────────────────────────────────────
let browser = null;
let context = null;
let pages = [];           // ordered list of Page; index 0 is "active"
let activeIndex = 0;
let lastSnapshot = {      // ref → meta (selector, role, name)
  refs: new Map(),
  serial: 0,
};
let viewport = { ...DEFAULT_VIEWPORT };

let _ensurePromise = null;
function ensureBrowser() {
  if (browser && browser.isConnected && browser.isConnected() && pages.length) return Promise.resolve();
  if (_ensurePromise) return _ensurePromise;
  _ensurePromise = (async () => {
    try { await _ensureBrowserInner(); } finally { _ensurePromise = null; }
  })();
  return _ensurePromise;
}
async function _ensureBrowserInner() {
  if (browser && browser.isConnected && browser.isConnected() && pages.length) return;

  // Match exactly the args the user's tested ~/playwright-termux skill uses.
  // Anything beyond these three has historically broken Termux Chromium
  // (e.g. --no-zygote, --disable-features=site-per-process).
  // Extra args can be added via AGENT_BROWSER_EXTRA_ARGS env var (space-separated).
  const baseArgs = ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'];
  const extra = (process.env.AGENT_BROWSER_EXTRA_ARGS || '').trim().split(/\s+/).filter(Boolean);
  const launchOpts = {
    headless: HEADLESS,
    args: [...baseArgs, ...extra],
  };
  if (CHROMIUM_PATH) launchOpts.executablePath = CHROMIUM_PATH;
  log('launching chromium', JSON.stringify(launchOpts));

  try {
    browser = await chromium.launch(launchOpts);
  } catch (e) {
    log('chromium.launch failed', e && (e.stack || e.message || e));
    throw new Error('chromium.launch failed: ' + (e && e.message ? e.message : String(e))
      + '\nCHROMIUM_PATH=' + (CHROMIUM_PATH || '(unset)')
      + '\nExecutable check: ' + (CHROMIUM_PATH ? (fs.existsSync(CHROMIUM_PATH) ? 'exists' : 'MISSING') : 'n/a')
      + '\nSee daemon log: ' + LOG_FILE);
  }

  // Browser may die before we get to newContext on broken environments —
  // surface that with a clearer message than Playwright's "Target ... has been closed".
  browser.on('disconnected', () => { log('browser disconnected'); });

  const ctxOpts = { viewport, ignoreHTTPSErrors: true };
  if (fs.existsSync(STORAGE_STATE_FILE)) {
    try {
      const raw = fs.readFileSync(STORAGE_STATE_FILE, 'utf8');
      JSON.parse(raw); // validate
      ctxOpts.storageState = STORAGE_STATE_FILE;
    } catch (e) {
      log('storage-state file corrupt, ignoring:', e.message);
      try { fs.unlinkSync(STORAGE_STATE_FILE); } catch (_) {}
    }
  }

  try {
    context = await browser.newContext(ctxOpts);
  } catch (e) {
    log('newContext failed', e && (e.stack || e.message || e));
    try { await browser.close(); } catch (_) {}
    browser = null;
    throw new Error('newContext failed: ' + (e && e.message ? e.message : String(e))
      + '\nThis usually means Chromium crashed right after launching.'
      + '\nTry running ~/playwright-termux/test-launch.js manually to see if your base setup works.'
      + '\nSee daemon log: ' + LOG_FILE);
  }
  context.setDefaultTimeout(DEFAULT_TIMEOUT);
  context.setDefaultNavigationTimeout(DEFAULT_TIMEOUT);

  // Track newly opened tabs (popups, target=_blank).
  context.on('page', (p) => {
    if (!pages.includes(p)) pages.push(p);
    p.on('close', () => {
      const i = pages.indexOf(p);
      if (i >= 0) {
        pages.splice(i, 1);
        if (activeIndex >= pages.length) activeIndex = Math.max(0, pages.length - 1);
      }
    });
  });

  try {
    const p0 = await context.newPage();
    pages = [p0];
    activeIndex = 0;
  } catch (e) {
    log('newPage failed', e && (e.stack || e.message || e));
    throw new Error('newPage failed: ' + (e && e.message ? e.message : String(e))
      + '\nChromium probably crashed mid-launch. Check daemon log: ' + LOG_FILE);
  }
}

function activePage() {
  if (!pages.length) throw new Error('no active page');
  if (activeIndex >= pages.length) activeIndex = pages.length - 1;
  return pages[activeIndex];
}

async function persistStorage() {
  try {
    if (!context) return;
    fs.mkdirSync(path.dirname(STORAGE_STATE_FILE), { recursive: true });
    await context.storageState({ path: STORAGE_STATE_FILE });
  } catch (e) { log('persistStorage error', e.message); }
}

// ── Selector helpers ────────────────────────────────────────────────────────
function isRef(s) { return typeof s === 'string' && /^@e\d+$/.test(s); }

async function resolveLocator(page, selOrRef) {
  if (!selOrRef) throw new Error('selector required');
  // Ref → use injected data attribute we set during snapshot()
  if (isRef(selOrRef)) {
    const meta = lastSnapshot.refs.get(selOrRef);
    if (!meta) throw new Error(`unknown ref ${selOrRef} (run snapshot first)`);
    // Re-resolve via the marker we placed in DOM during snapshot.
    return page.locator(`[data-ab-ref="${selOrRef.slice(1)}"]`);
  }
  // role= or text= → Playwright supports these directly
  if (/^(role|text|css|xpath|id)=/.test(selOrRef)) return page.locator(selOrRef);
  return page.locator(selOrRef);
}

// ── Snapshot ────────────────────────────────────────────────────────────────
const INTERACTIVE_ROLES = new Set([
  'button','link','textbox','combobox','listbox','option','checkbox','radio',
  'switch','slider','spinbutton','menuitem','menuitemcheckbox','menuitemradio',
  'tab','treeitem','searchbox','tooltip',
]);

async function buildSnapshot(opts) {
  const page = activePage();
  const interactive = !!opts.interactive;
  const maxDepth = opts.maxDepth || 25;

  // Walk DOM ourselves to assign stable @eN refs and emit text.
  // We use page.evaluate so we get real elements, not just the AX-tree (which
  // is more accurate for screen readers but less actionable for raw clicks).
  const tree = await page.evaluate(({ interactiveOnly, maxDepth }) => {
    const INTERACTIVE_TAGS = new Set(['A','BUTTON','INPUT','SELECT','TEXTAREA','SUMMARY','LABEL']);
    const INTERACTIVE_ROLES = new Set([
      'button','link','textbox','combobox','listbox','option','checkbox','radio',
      'switch','slider','spinbutton','menuitem','menuitemcheckbox','menuitemradio',
      'tab','treeitem','searchbox',
    ]);

    let counter = 0;
    function isVisible(el) {
      if (!el || el.nodeType !== 1) return false;
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) return false;
      const cs = window.getComputedStyle(el);
      if (cs.visibility === 'hidden' || cs.display === 'none') return false;
      return true;
    }
    function getRole(el) {
      const explicit = el.getAttribute('role');
      if (explicit) return explicit.toLowerCase();
      const t = el.tagName.toLowerCase();
      if (t === 'a' && el.hasAttribute('href')) return 'link';
      if (t === 'button') return 'button';
      if (t === 'select') return 'combobox';
      if (t === 'textarea') return 'textbox';
      if (t === 'input') {
        const it = (el.type || 'text').toLowerCase();
        if (it === 'submit' || it === 'button' || it === 'reset') return 'button';
        if (it === 'checkbox') return 'checkbox';
        if (it === 'radio') return 'radio';
        if (it === 'range') return 'slider';
        if (it === 'search') return 'searchbox';
        return 'textbox';
      }
      if (t === 'summary') return 'button';
      if (t === 'label') return 'label';
      return '';
    }
    function getName(el) {
      const aria = el.getAttribute('aria-label');
      if (aria && aria.trim()) return aria.trim().slice(0, 200);
      if (el.id) {
        const lbl = el.ownerDocument.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (lbl && lbl.textContent.trim()) return lbl.textContent.trim().slice(0,200);
      }
      const placeholder = el.getAttribute && el.getAttribute('placeholder');
      const value = ('value' in el) ? el.value : '';
      const text = (el.textContent || '').trim().replace(/\s+/g,' ').slice(0,200);
      return text || placeholder || value || '';
    }
    function isFormElement(el) {
      const t = el.tagName;
      if (INTERACTIVE_TAGS.has(t)) return true;
      const r = getRole(el);
      if (INTERACTIVE_ROLES.has(r)) return true;
      if (el.hasAttribute && (el.hasAttribute('onclick') || el.getAttribute('tabindex') === '0')) return true;
      return false;
    }

    // Clear old refs
    document.querySelectorAll('[data-ab-ref]').forEach(e => e.removeAttribute('data-ab-ref'));

    const lines = [];
    const refs = [];
    function walk(node, depth) {
      if (!node || depth > maxDepth) return;
      if (node.nodeType === 1) {
        const el = node;
        if (!isVisible(el)) {
          // Skip subtree for hidden — accessibility tree wouldn't see it either
          return;
        }
        if (isFormElement(el)) {
          counter++;
          const ref = 'e' + counter;
          el.setAttribute('data-ab-ref', ref);
          const role = getRole(el) || el.tagName.toLowerCase();
          const name = getName(el);
          const id   = el.id ? '#' + el.id : '';
          const cls  = el.className && typeof el.className === 'string'
            ? '.' + el.className.trim().split(/\s+/).slice(0,2).join('.') : '';
          const extras = [];
          if (el.disabled) extras.push('disabled');
          if (el.required) extras.push('required');
          if (el.checked)  extras.push('checked');
          if (el.type === 'file') extras.push('file');
          if (el.value && role === 'textbox') extras.push('value=' + JSON.stringify(String(el.value).slice(0,60)));
          const indent = '  '.repeat(Math.min(depth, 8));
          lines.push(`${indent}@${ref} ${role} ${name ? JSON.stringify(name.slice(0,80)) : ''}${id}${cls}${extras.length ? ' [' + extras.join(',') + ']' : ''}`);
          refs.push({ ref: '@' + ref, role, name, tag: el.tagName.toLowerCase() });
        } else if (!interactiveOnly) {
          // For full snapshot include heading text
          const t = el.tagName;
          if (/^H[1-6]$/.test(t)) {
            const txt = (el.textContent||'').trim().replace(/\s+/g,' ').slice(0,120);
            if (txt) lines.push('  '.repeat(Math.min(depth,8)) + t.toLowerCase() + ' ' + JSON.stringify(txt));
          }
        }
        for (const child of el.children) walk(child, depth + 1);
      }
    }
    walk(document.body, 0);

    return {
      url: location.href,
      title: document.title,
      lines,
      refs,
    };
  }, { interactiveOnly: interactive, maxDepth });

  // Update refs map
  lastSnapshot.refs.clear();
  lastSnapshot.serial++;
  for (const r of tree.refs) lastSnapshot.refs.set(r.ref, r);

  let header = `# ${tree.title || '(no title)'}\n# ${tree.url}\n`;
  return header + tree.lines.join('\n') + '\n';
}

// ── Argv parser ────────────────────────────────────────────────────────────
function parseFlags(argv, flagSpec = {}) {
  // flagSpec: {name:{boolean:true|false}}
  const flags = {};
  const positional = [];
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith('--')) {
      const name = a.slice(2);
      const spec = flagSpec[name] || {};
      if (spec.boolean) { flags[name] = true; }
      else { flags[name] = argv[++i]; }
    } else if (a === '-b') {
      flags.b = true;
    } else if (a === '-i') {
      flags.i = true;
    } else if (a.startsWith('-') && a.length === 2) {
      flags[a.slice(1)] = true;
    } else {
      positional.push(a);
    }
  }
  return { flags, positional };
}

// ── Command implementations ────────────────────────────────────────────────
const COMMANDS = {};

COMMANDS.help = async () => {
  return {
    stdout:
`agent-browser (Termux/Playwright shim) commands:
  open <url> [--wait load|domcontentloaded|networkidle]
  back | forward | reload
  set viewport <w> <h>
  screenshot <path> [--full] [--clip x,y,w,h]
  snapshot [-i] [-d <depth>]
  click|dblclick|hover|focus <selector|@ref>
  fill <selector|@ref> <text>
  type <selector|@ref> <text> [--delay ms]
  press [<selector|@ref>] <key>
  upload <selector|@ref> <path...>
  mouse move|down|up|click|wheel ...
  eval [-b] <code>
  get url|title|html|text [<selector>]
  wait <selector|@ref> [timeout-ms] | wait timeout <ms> | wait load|domcontentloaded|networkidle
  tabs list|new [url]|switch <i>|close <i>
  cookies get [domain]|cookies clear
  status | version | kill\n`,
    stderr: '', exit_code: 0,
  };
};

COMMANDS.version = async () => ({
  stdout: 'agent-browser-termux 0.1.0 (chromium ' + (browser ? (browser.version ? await browser.version() : '?') : 'not-running') + ')\n',
  stderr: '', exit_code: 0,
});

COMMANDS.status = async () => {
  const tabs = pages.map((p, i) => `[${i}${i===activeIndex?'*':''}] ${p.url()}`).join('\n');
  return {
    stdout: `running pid=${process.pid} uptime=${Math.round((Date.now()-STARTED_AT)/1000)}s tabs=${pages.length}\n${tabs}\n`,
    stderr: '', exit_code: 0,
  };
};

COMMANDS.kill = async () => {
  setTimeout(() => process.exit(0), 50);
  return { stdout: 'shutting down\n', stderr: '', exit_code: 0 };
};

COMMANDS.open = async (argv) => {
  await ensureBrowser();
  const { flags, positional } = parseFlags(argv);
  const url = positional[0];
  if (!url) throw new Error('open: url required');
  const waitUntil = flags.wait || 'load';
  await activePage().goto(url, { waitUntil, timeout: DEFAULT_TIMEOUT });
  await persistStorage();
  return { stdout: activePage().url() + '\n', stderr: '', exit_code: 0 };
};

COMMANDS.back     = async () => { await ensureBrowser(); await activePage().goBack({ waitUntil: 'load' }).catch(()=>{}); return {stdout:activePage().url()+'\n',stderr:'',exit_code:0};};
COMMANDS.forward  = async () => { await ensureBrowser(); await activePage().goForward({ waitUntil: 'load' }).catch(()=>{}); return {stdout:activePage().url()+'\n',stderr:'',exit_code:0};};
COMMANDS.reload   = async () => { await ensureBrowser(); await activePage().reload({ waitUntil: 'load' }); return {stdout:activePage().url()+'\n',stderr:'',exit_code:0};};

COMMANDS.set = async (argv) => {
  await ensureBrowser();
  const sub = argv[0];
  if (sub === 'viewport') {
    const w = parseInt(argv[1], 10), h = parseInt(argv[2], 10);
    if (!w || !h) throw new Error('set viewport: w h required');
    viewport = { width: w, height: h };
    for (const p of pages) await p.setViewportSize(viewport);
    return { stdout: `${w}x${h}\n`, stderr: '', exit_code: 0 };
  }
  throw new Error('set: unknown subcommand ' + sub);
};

COMMANDS.screenshot = async (argv) => {
  await ensureBrowser();
  const { flags, positional } = parseFlags(argv);
  const out = positional[0];
  if (!out) throw new Error('screenshot: path required');
  const expanded = out.startsWith('~') ? path.join(os.homedir(), out.slice(1)) : out;
  fs.mkdirSync(path.dirname(expanded), { recursive: true });
  const opts = { path: expanded, type: 'png' };
  if (flags.full) opts.fullPage = true;
  if (flags.clip) {
    const m = String(flags.clip).split(',').map(Number);
    if (m.length === 4) opts.clip = { x:m[0], y:m[1], width:m[2], height:m[3] };
  }
  await activePage().screenshot(opts);
  return { stdout: expanded + '\n', stderr: '', exit_code: 0 };
};

COMMANDS.snapshot = async (argv) => {
  await ensureBrowser();
  const { flags } = parseFlags(argv);
  const text = await buildSnapshot({ interactive: !!flags.i, maxDepth: parseInt(flags.d || '25', 10) });
  return { stdout: text, stderr: '', exit_code: 0 };
};

async function actOnLocator(action, argv) {
  await ensureBrowser();
  const sel = argv[0];
  const text = argv.slice(1).join(' ');
  if (!sel) throw new Error(action + ': selector required');
  const loc = await resolveLocator(activePage(), sel);
  if (action === 'click')      await loc.click({ timeout: DEFAULT_TIMEOUT });
  else if (action === 'dblclick') await loc.dblclick({ timeout: DEFAULT_TIMEOUT });
  else if (action === 'hover') await loc.hover({ timeout: DEFAULT_TIMEOUT });
  else if (action === 'focus') await loc.focus({ timeout: DEFAULT_TIMEOUT });
  else if (action === 'fill')  await loc.fill(text, { timeout: DEFAULT_TIMEOUT });
  else if (action === 'type') {
    const { flags } = parseFlags(argv.slice(1));
    const delay = parseInt(flags.delay || '0', 10);
    const t = argv.slice(1).filter(a => !a.startsWith('--')).join(' ');
    await loc.pressSequentially(t, { delay });
  }
  await persistStorage();
  return { stdout: '', stderr: '', exit_code: 0 };
}
COMMANDS.click    = (a) => actOnLocator('click', a);
COMMANDS.dblclick = (a) => actOnLocator('dblclick', a);
COMMANDS.hover    = (a) => actOnLocator('hover', a);
COMMANDS.focus    = (a) => actOnLocator('focus', a);
COMMANDS.fill     = (a) => actOnLocator('fill', a);
COMMANDS.type     = (a) => actOnLocator('type', a);

COMMANDS.press = async (argv) => {
  await ensureBrowser();
  // press [<selector|@ref>] <key>
  if (argv.length === 1) {
    await activePage().keyboard.press(argv[0]);
  } else {
    const sel = argv[0]; const key = argv.slice(1).join(' ');
    const loc = await resolveLocator(activePage(), sel);
    await loc.press(key);
  }
  return { stdout: '', stderr: '', exit_code: 0 };
};

COMMANDS.upload = async (argv) => {
  await ensureBrowser();
  const sel = argv[0];
  const files = argv.slice(1).map(f => f.startsWith('~') ? path.join(os.homedir(), f.slice(1)) : f);
  if (!sel || !files.length) throw new Error('upload: selector and at least one file required');
  const loc = await resolveLocator(activePage(), sel);
  await loc.setInputFiles(files);
  return { stdout: 'uploaded ' + files.length + ' file(s)\n', stderr: '', exit_code: 0 };
};

COMMANDS.mouse = async (argv) => {
  await ensureBrowser();
  const m = activePage().mouse;
  const sub = argv[0];
  if (sub === 'move') {
    const x = parseFloat(argv[1]), y = parseFloat(argv[2]);
    await m.move(x, y, { steps: parseInt(argv[3] || '1', 10) });
  } else if (sub === 'down') {
    await m.down({ button: argv[1] || 'left' });
  } else if (sub === 'up') {
    await m.up({ button: argv[1] || 'left' });
  } else if (sub === 'click') {
    const x = parseFloat(argv[1]), y = parseFloat(argv[2]);
    await m.click(x, y, { button: argv[3] || 'left' });
  } else if (sub === 'wheel') {
    const dy = parseFloat(argv[1] || '0'), dx = parseFloat(argv[2] || '0');
    await m.wheel(dx, dy);
  } else {
    throw new Error('mouse: unknown subcommand ' + sub);
  }
  return { stdout: '', stderr: '', exit_code: 0 };
};

COMMANDS.eval = async (argv) => {
  await ensureBrowser();
  const { flags, positional } = parseFlags(argv);
  let code = positional.join(' ');
  if (flags.b) {
    code = Buffer.from(code, 'base64').toString('utf8');
  }
  // Wrap so the user can pass either an expression or a statement block.
  const wrapped = `(async () => { return (${code}); })()`;
  let result;
  try {
    result = await activePage().evaluate(wrapped);
  } catch (e1) {
    // Fall back: maybe it was a statement block — wrap accordingly.
    const stmt = `(async () => { ${code}; })()`;
    result = await activePage().evaluate(stmt);
  }
  let out = '';
  if (result === undefined) out = '';
  else if (typeof result === 'string') out = result;
  else { try { out = JSON.stringify(result); } catch (_) { out = String(result); } }
  return { stdout: out + (out.endsWith('\n') ? '' : '\n'), stderr: '', exit_code: 0 };
};

COMMANDS.get = async (argv) => {
  await ensureBrowser();
  const what = argv[0]; const sel = argv[1];
  const p = activePage();
  let out = '';
  if (what === 'url') out = p.url();
  else if (what === 'title') out = await p.title();
  else if (what === 'html') {
    if (sel) out = await (await resolveLocator(p, sel)).innerHTML();
    else out = await p.content();
  }
  else if (what === 'text') {
    if (sel) out = (await (await resolveLocator(p, sel)).innerText()) || '';
    else out = await p.evaluate(() => document.body && document.body.innerText || '');
  }
  else throw new Error('get: unknown what ' + what);
  return { stdout: out + (out.endsWith('\n') ? '' : '\n'), stderr: '', exit_code: 0 };
};

COMMANDS.wait = async (argv) => {
  await ensureBrowser();
  const a = argv[0];
  const p = activePage();
  if (a === 'timeout') {
    await p.waitForTimeout(parseInt(argv[1] || '1000', 10));
  } else if (a === 'load' || a === 'domcontentloaded' || a === 'networkidle') {
    await p.waitForLoadState(a, { timeout: DEFAULT_TIMEOUT });
  } else {
    const timeout = parseInt(argv[1] || String(DEFAULT_TIMEOUT), 10);
    const loc = await resolveLocator(p, a);
    await loc.waitFor({ timeout });
  }
  return { stdout: '', stderr: '', exit_code: 0 };
};

COMMANDS.tabs = async (argv) => {
  await ensureBrowser();
  const sub = argv[0];
  if (!sub || sub === 'list') {
    const out = pages.map((p, i) => `[${i}${i===activeIndex?'*':''}] ${p.url()}`).join('\n');
    return { stdout: out + '\n', stderr: '', exit_code: 0 };
  }
  if (sub === 'new') {
    const np = await context.newPage();
    pages.push(np);
    activeIndex = pages.length - 1;
    if (argv[1]) await np.goto(argv[1], { waitUntil: 'load' });
    return { stdout: 'tab ' + activeIndex + '\n', stderr: '', exit_code: 0 };
  }
  if (sub === 'switch') {
    const i = parseInt(argv[1], 10);
    if (isNaN(i) || i < 0 || i >= pages.length) throw new Error('tabs switch: bad index');
    activeIndex = i;
    await pages[i].bringToFront();
    return { stdout: pages[i].url() + '\n', stderr: '', exit_code: 0 };
  }
  if (sub === 'close') {
    const i = parseInt(argv[1], 10);
    if (isNaN(i) || i < 0 || i >= pages.length) throw new Error('tabs close: bad index');
    const target = pages[i];
    // Splice synchronously so subsequent activePage() calls don't return a closing page.
    pages.splice(i, 1);
    if (activeIndex > i) activeIndex--;
    else if (activeIndex === i) activeIndex = Math.min(activeIndex, pages.length - 1);
    if (activeIndex < 0) activeIndex = 0;
    if (!pages.length) {
      // Always keep at least one page open so subsequent commands work.
      const fresh = await context.newPage();
      pages.push(fresh);
      activeIndex = 0;
    }
    try { await target.close({ runBeforeUnload: false }); } catch (_) {}
    return { stdout: 'closed ' + i + '\n', stderr: '', exit_code: 0 };
  }
  throw new Error('tabs: unknown subcommand ' + sub);
};

COMMANDS.cookies = async (argv) => {
  await ensureBrowser();
  const sub = argv[0];
  if (!sub || sub === 'get') {
    const c = await context.cookies(argv[1]);
    return { stdout: JSON.stringify(c, null, 2) + '\n', stderr: '', exit_code: 0 };
  }
  if (sub === 'clear') {
    await context.clearCookies();
    await persistStorage();
    return { stdout: 'cleared\n', stderr: '', exit_code: 0 };
  }
  throw new Error('cookies: unknown subcommand');
};

// ── Dispatch ────────────────────────────────────────────────────────────────
async function dispatch(argv) {
  if (!argv || !argv.length) return COMMANDS.help([]);
  const cmd = argv[0];
  const rest = argv.slice(1);
  if (!(cmd in COMMANDS)) {
    return { stdout: '', stderr: `unknown command "${cmd}". Try "help".\n`, exit_code: 2 };
  }
  try {
    const result = await COMMANDS[cmd](rest);
    return result || { stdout: '', stderr: '', exit_code: 0 };
  } catch (e) {
    log('cmd error', cmd, e && e.stack || e);
    return { stdout: '', stderr: (e && e.message ? e.message : String(e)) + '\n', exit_code: 1 };
  }
}

// ── HTTP server ────────────────────────────────────────────────────────────
const server = http.createServer(async (req, res) => {
  if (req.method === 'GET' && req.url === '/health') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    return res.end(JSON.stringify({
      ok: true,
      pid: process.pid,
      uptime_ms: Date.now() - STARTED_AT,
      pages: pages.length,
      headless: HEADLESS,
      chromium_path: CHROMIUM_PATH || null,
    }));
  }
  if (req.method === 'POST' && req.url === '/exec') {
    let body = '';
    req.on('data', (c) => { body += c; if (body.length > 50*1024*1024) req.destroy(); });
    req.on('end', async () => {
      try {
        const data = JSON.parse(body || '{}');
        const argv = Array.isArray(data.argv) ? data.argv : [];
        const result = await dispatch(argv);
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(result));
      } catch (e) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ stdout:'', stderr: 'daemon error: ' + e.message + '\n', exit_code: 99 }));
      }
    });
    return;
  }
  if (req.method === 'POST' && req.url === '/shutdown') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true }));
    setTimeout(() => process.exit(0), 50);
    return;
  }
  res.writeHead(404).end();
});

server.on('error', (e) => {
  log('server error', e && e.message);
  if (e && e.code === 'EADDRINUSE') {
    console.error('agent-browser daemon: port ' + PORT + ' already in use');
    process.exit(3);
  }
  process.exit(1);
});

server.listen(PORT, HOST, async () => {
  fs.writeFileSync(PID_FILE, String(process.pid));
  log('listening', `http://${HOST}:${PORT}`);
  // Pre-warm the browser so first command is fast.
  ensureBrowser().catch((e) => log('warmup error', e.message));
});

// Graceful shutdown
function shutdown() {
  log('shutdown');
  Promise.resolve()
    .then(() => persistStorage())
    .then(() => browser && browser.close().catch(()=>{}))
    .finally(() => process.exit(0));
}
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
process.on('uncaughtException', (e) => { log('uncaught', e && e.stack); });
process.on('unhandledRejection', (e) => { log('unhandled', e && (e.stack || e.message || e)); });
