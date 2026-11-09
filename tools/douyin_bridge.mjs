// Douyin browser bridge (CDP over pipe, zero dependencies).
// A visible Chrome window is handed to the human for login. The agent talks to
// it through a file-based command queue, so no TCP debug port is needed.
// Cookie / credential material is NEVER exported; it stays inside Chrome's profile.
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const ROOT = path.resolve(import.meta.dirname, '..');
const BR = path.join(ROOT, 'data', 'browser');
const RESULTS = path.join(BR, 'results');
const CMD = path.join(BR, 'cmd.json');
const STATUS = path.join(BR, 'status.json');
const PROFILE = path.join(ROOT, 'data', 'browser-profile-v2');
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const START_URL = 'https://www.douyin.com/';

fs.mkdirSync(RESULTS, { recursive: true });
fs.mkdirSync(PROFILE, { recursive: true });

const child = spawn(CHROME, [
  '--remote-debugging-pipe',
  `--user-data-dir=${PROFILE}`,
  '--no-first-run',
  '--no-default-browser-check',
  '--disable-features=Translate',
  '--window-size=1440,920',
  '--window-position=80,60',
  '--new-window',
  START_URL,
], { stdio: ['ignore', 'ignore', 'pipe', 'pipe', 'pipe'] });

child.on('error', (e) => fs.writeFileSync(path.join(BR, 'fatal.log'), `SPAWN_ERR ${e.code} ${e.message}\n`));
child.on('exit', (c, s) => {
  fs.appendFileSync(path.join(BR, 'bridge.log'), `chrome_exit code=${c} signal=${s}\n`);
});

const toChrome = child.stdio[3];
const fromChrome = child.stdio[4];
let seq = 0;
const pending = new Map();
const sessions = new Map();
let inbox = '';

fromChrome.on('data', (chunk) => {
  inbox += chunk.toString('utf8');
  let i;
  while ((i = inbox.indexOf('\0')) >= 0) {
    const raw = inbox.slice(0, i);
    inbox = inbox.slice(i + 1);
    if (!raw.trim()) continue;
    let msg;
    try { msg = JSON.parse(raw); } catch { continue; }
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) reject(new Error(JSON.stringify(msg.error)));
      else resolve(msg.result);
    }
  }
});

function send(method, params = {}, sessionId) {
  const id = ++seq;
  const frame = { id, method, params };
  if (sessionId) frame.sessionId = sessionId;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    toChrome.write(JSON.stringify(frame) + '\0');
    setTimeout(() => {
      if (pending.has(id)) {
        pending.delete(id);
        reject(new Error(`CDP_TIMEOUT ${method}`));
      }
    }, 15000);
  });
}

async function session() {
  const { targetInfos } = await send('Target.getTargets');
  const pages = targetInfos.filter((t) => t.type === 'page');
  const page = pages.find((t) => (t.url || '').includes('douyin.com')) || pages[0];
  if (!page) throw new Error('NO_PAGE_TARGET');
  if (!sessions.has(page.targetId)) {
    const r = await send('Target.attachToTarget', { targetId: page.targetId, flatten: true });
    sessions.set(page.targetId, r.sessionId);
  }
  return sessions.get(page.targetId);
}

async function evaluate(expression) {
  const sid = await session();
  const r = await send('Runtime.evaluate', {
    expression,
    returnByValue: true,
    awaitPromise: true,
    userGesture: true,
  }, sid);
  if (r.exceptionDetails) throw new Error('EVAL_EXCEPTION ' + (r.exceptionDetails.text || ''));
  return r.result?.value;
}

const PROBE_JS = `(() => {
  const t = (document.body && document.body.innerText) || '';
  return {
    url: location.href,
    title: document.title,
    ready: document.readyState,
    loginWord: /登录|扫码/.test(t.slice(0, 1200)),
    text: t.slice(0, 400)
  };
})()`;

const SCRAPE_JS = `(() => {
  const out = [];
  const seen = new Set();
  document.querySelectorAll('a[href*="/video/"]').forEach(a => {
    const href = a.href.split('?')[0];
    if (!href || seen.has(href)) return;
    seen.add(href);
    let box = a;
    for (let i = 0; i < 3 && box.parentElement; i++) box = box.parentElement;
    const txt = (box.innerText || a.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 220);
    const img = box.querySelector('img') || a.querySelector('img');
    out.push({
      url: href,
      title: (a.getAttribute('title') || a.innerText || '').trim().slice(0, 160),
      card_text: txt,
      cover: img ? (img.src || img.getAttribute('data-src') || '') : '',
    });
  });
  return {
    url: location.href,
    title: document.title,
    body_head: ((document.body && document.body.innerText) || '').slice(0, 500),
    count: out.length,
    items: out.slice(0, 40)
  };
})()`;

async function handle(cmd) {
  switch (cmd.op) {
    case 'status':
      return await evaluate(PROBE_JS);
    case 'eval':
      return await evaluate(cmd.expr);
    case 'goto': {
      const sid = await session();
      await send('Page.navigate', { url: cmd.url }, sid);
      await new Promise((r) => setTimeout(r, cmd.wait || 4000));
      return await evaluate(PROBE_JS);
    }
    case 'search': {
      const sid = await session();
      const url = `https://www.douyin.com/search/${encodeURIComponent(cmd.keyword)}?type=video`;
      await send('Page.navigate', { url }, sid);
      await new Promise((r) => setTimeout(r, cmd.wait || 6000));
      const data = await evaluate(SCRAPE_JS);
      data.keyword = cmd.keyword;
      return data;
    }
    case 'scrape':
      return await evaluate(SCRAPE_JS);
    default:
      throw new Error('UNKNOWN_OP ' + cmd.op);
  }
}

// status heartbeat: cheap, lets the agent see whether the human has logged in.
async function heartbeat() {
  try {
    const s = await evaluate(PROBE_JS);
    s.ts = new Date().toISOString();
    s.bridge = 'alive';
    fs.writeFileSync(STATUS, JSON.stringify(s, null, 2));
  } catch (e) {
    fs.writeFileSync(STATUS, JSON.stringify({ bridge: 'alive', probe_error: String(e.message || e), ts: new Date().toISOString() }, null, 2));
  }
}

async function poll() {
  if (!fs.existsSync(CMD)) return;
  let cmd;
  try {
    cmd = JSON.parse(fs.readFileSync(CMD, 'utf8'));
  } catch (e) {
    fs.renameSync(CMD, path.join(BR, `bad-${Date.now()}.json`));
    return;
  }
  fs.unlinkSync(CMD);
  const result = { id: cmd.id, op: cmd.op, ts: new Date().toISOString() };
  try {
    result.ok = true;
    result.value = await handle(cmd);
  } catch (e) {
    result.ok = false;
    result.error = String(e.message || e);
  }
  const out = path.join(RESULTS, `${String(cmd.id).replace(/[^A-Za-z0-9_-]/g, '_')}.json`);
  fs.writeFileSync(out, JSON.stringify(result, null, 2));
  if (cmd.op === 'stop') {
    setTimeout(() => { try { child.kill(); } catch {} process.exit(0); }, 200);
  }
}

setInterval(() => { poll().catch((e) => fs.appendFileSync(path.join(BR, 'bridge.log'), 'poll_err ' + e + '\n')); }, 350);
setInterval(() => { heartbeat().catch(() => {}); }, 3000);
setTimeout(heartbeat, 1500);
fs.appendFileSync(path.join(BR, 'bridge.log'), `bridge_start ${new Date().toISOString()} pid=${child.pid}\n`);
