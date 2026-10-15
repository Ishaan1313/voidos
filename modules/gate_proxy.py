"""
Void OS — Gate Proxy

A small, standalone, dependency-free (beyond itsdangerous/werkzeug, already
Void OS dependencies) reverse proxy that sits between cloudflared and a
gated app tile's real local port:

    cloudflared --url http://localhost:{gate_port}
                              |
                              v
                gate_proxy.py (this file) — PIN check, sets cookie
                              |
                              v
                http://localhost:{backend_port}   (the real app)

Deliberately placed HERE — as its own subprocess, between cloudflared and
the app — rather than inside the main Flask process or only inside the
optional Cloudflare Worker template. See FEATURE_REQUEST_tunnel_access_gate.md
section 3 for the full reasoning; the short version: most tunneled tiles are
reached via the raw https://xxxx.trycloudflare.com URL directly, which never
touches a Worker even when one is deployed for a *different* tile. The only
place a gate is enforced regardless of which URL (raw tunnel hostname, or a
stable Worker-fronted one) reaches it is here, sitting behind whatever
cloudflared's --url already points at.

This is NOT a replacement for the app's own login (Portainer/Immich/
code-server all keep their own auth exactly as before) and is NOT
Cloudflare Access — no SSO, no per-user identity, no device posture checks.
It's a shared PIN and a signed cookie. That's it.

Run standalone (only used as a subprocess, spawned by voidos.py):
    VOID_GATE_BACKEND_PORT=8443 VOID_GATE_PIN_HASH=... VOID_GATE_SECRET_KEY=... \
        python3 modules/gate_proxy.py

On startup, prints exactly one line to stdout before serving:
    GATE_LISTENING:<port>
so the parent process can learn the OS-assigned listen port the same way
it already sniffs cloudflared's own stdout for the connected tunnel URL.
"""
import asyncio
import json
import os
import sys
import time
from urllib.parse import unquote_plus

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from werkzeug.security import check_password_hash

# ── Config (env vars only — keeps secrets out of argv/`ps` output) ─────
BACKEND_PORT = int(os.environ["VOID_GATE_BACKEND_PORT"])
LISTEN_PORT = int(os.environ.get("VOID_GATE_LISTEN_PORT", "0"))  # 0 = OS picks an ephemeral port
PIN_HASH = os.environ["VOID_GATE_PIN_HASH"]
SECRET_KEY = os.environ["VOID_GATE_SECRET_KEY"]
COOKIE_TTL_HOURS = float(os.environ.get("VOID_GATE_COOKIE_TTL_HOURS", "12"))
# Defaults true because this gate is normally only ever reached through a
# Cloudflare Quick Tunnel, which is HTTPS-only at the edge — there's no
# realistic plain-HTTP path for this cookie today. Only set this to false if
# fronting the gate with something other than a Cloudflare Tunnel (e.g. a raw
# local HTTP test), where Secure would otherwise silently drop the cookie.
COOKIE_SECURE = os.environ.get("VOID_GATE_COOKIE_SECURE", "true").lower() == "true"
APP_NAME = os.environ.get("VOID_GATE_APP_NAME", "this app")
APP_ID = os.environ.get("VOID_GATE_APP_ID", "")
AUDIT_FILE = os.environ.get("VOID_GATE_AUDIT_FILE", "")
IS_PIN = os.environ.get("VOID_GATE_IS_PIN", "true").lower() == "true"
PIN_LENGTH = int(os.environ.get("VOID_GATE_PIN_LENGTH", "6"))

# Same env var names the main Flask process already uses for its own login
# lockout, so an operator only has one pair of settings to think about —
# but this lockout tracker is its own in-memory dict, local to this one
# subprocess. It does NOT share state with the main app's login lockout,
# and resets on restart. That's a deliberate, documented tradeoff (see
# FEATURE_REQUEST_tunnel_access_gate.md §5), not a silent gap: sharing
# state across a process boundary here would be real IPC complexity for
# very little benefit, given a gate proxy already restarts on PIN change.
MAX_ATTEMPTS = int(os.environ.get("VOID_MAX_ATTEMPTS", "5"))
LOCKOUT_SECONDS = int(os.environ.get("VOID_LOCKOUT_SECONDS", "60"))

COOKIE_NAME = "void_gate"
_serial = URLSafeTimedSerializer(SECRET_KEY, salt="void-gate-v1")
# Own salt — same reasoning as the share-link serializer in modules/shares.py: a leaked
# gate cookie can never be replayed as a session/share token or vice versa,
# even though all three ultimately derive from the same VOID_SECRET_KEY.

# ── In-memory per-IP lockout — no lock needed: asyncio is single-threaded
# and none of these three functions contain an `await`, so each one runs to
# completion before any other task gets a turn. Checked under concurrent
# load when written, rather than just assumed (that test script is not part of this repo). ─────────────────
_fails = {}
# Gate-wide failure budget, independent of client IP. Per-IP lockout alone
# lets a distributed guesser walk a 6-digit PIN space (1M) from many
# addresses; this caps total wrong guesses for the whole gate.
_GLOBAL_WINDOW_S = 600
_GLOBAL_MAX_FAILS = int(os.environ.get("VOID_GATE_GLOBAL_MAX_FAILS", "30"))
_global_fail_times = []
_global_locked_until = [0.0]

def _global_locked():
    now = time.time()
    if now < _global_locked_until[0]:
        return True, int(_global_locked_until[0] - now)
    return False, 0

def _global_fail():
    now = time.time()
    _global_fail_times[:] = [t for t in _global_fail_times if now - t < _GLOBAL_WINDOW_S]
    _global_fail_times.append(now)
    if len(_global_fail_times) >= _GLOBAL_MAX_FAILS:
        _global_locked_until[0] = now + max(LOCKOUT_SECONDS, 300)
        _global_fail_times.clear()
def _is_locked(ip):
    c, u = _fails.get(ip, (0, 0.))
    if u and time.time() < u: return True, int(u - time.time())
    return False, 0
def _fail(ip):
    c, u = _fails.get(ip, (0, 0.))
    if u and time.time() >= u: c = 0
    c += 1
    _fails[ip] = (0, time.time() + LOCKOUT_SECONDS) if c >= MAX_ATTEMPTS else (c, 0.)
def _clear_fails(ip):
    _fails.pop(ip, None)


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                  .replace('"', "&quot;").replace("'", "&#39;"))


def _audit(ip: str, result: str):
    line = f"  [gate:{APP_ID or '?'}] {result} from {ip}"
    print(line, flush=True)
    if AUDIT_FILE:
        try:
            with open(AUDIT_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(), "app_id": APP_ID, "ip": ip, "result": result}) + "\n")
        except OSError:
            pass  # audit bridge is best-effort; never let a disk hiccup break the gate itself


def _http_response(status: int, status_text: str, headers: dict, body: bytes) -> bytes:
    head = f"HTTP/1.1 {status} {status_text}\r\n"
    headers = dict(headers)
    headers.setdefault("Content-Length", str(len(body)))
    headers.setdefault("Connection", "close")
    for k, v in headers.items():
        head += f"{k}: {v}\r\n"
    head += "\r\n"
    return head.encode("latin-1") + body


_PAGE_SHELL = """<html><head><title>Access Gate — __APP_NAME__</title>
<style>
:root { --bg:#0a0a12; --bg-rgb:10,10,18; --sf:rgba(18,16,28,.9); --sf2:#1c1929; --bd:rgba(155,133,255,.13);
  --bd2:rgba(155,133,255,.24); --pu:#6d4aff; --pu-rgb:109,74,255; --ps:#9b85ff; --pd:#4a3aa8; --tx:#f3f1fb;
  --td:#8b8696; --tm:#5a5468; --rd:#ff5c7a; --rd-rgb:255,92,122; --gr:#5fe3a6; --ov-rgb:255,255,255; --rs:8px; }
* { box-sizing:border-box; margin:0; padding:0; }
html,body { height:100%; background:var(--bg); color:var(--tx); overflow:hidden;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; font-size:14px; }
button { font-family:inherit; cursor:pointer; }
.blobs { position:fixed; inset:0; pointer-events:none; z-index:0; overflow:hidden; }
.blob { position:absolute; border-radius:50%; filter:blur(90px); opacity:.3; mix-blend-mode:screen; }
.b1 { width:500px; height:500px; background:radial-gradient(circle,var(--pu),transparent 70%); top:-14%; left:-10%; animation:d1 24s ease-in-out infinite alternate; }
.b2 { width:420px; height:420px; background:radial-gradient(circle,var(--ps),transparent 70%); bottom:-16%; right:-8%; animation:d2 30s ease-in-out infinite alternate; }
@keyframes d1 { 0%{transform:translate(-5%,-5%);} to{transform:translate(9%,13%) scale(1.1);} }
@keyframes d2 { 0%{transform:translate(9%,7%) scale(1.06);} to{transform:translate(-11%,-5%) scale(.93);} }
.login-wrap { position:relative; z-index:2; display:flex; align-items:center; justify-content:center; min-height:100vh; padding:24px; }
.lcard { width:348px; max-width:91vw; padding:36px 30px 28px; text-align:center;
  background:rgba(var(--bg-rgb),.8); border:1px solid var(--bd2); border-radius:20px;
  backdrop-filter:blur(28px); box-shadow:0 28px 70px rgba(0,0,0,.65); animation:fadeUp .5s cubic-bezier(.2,.8,.2,1); }
.lcard.shake { animation:shake .5s cubic-bezier(.36,.07,.19,.97); }
@keyframes fadeUp { from{opacity:0;transform:translateY(20px) scale(.98);} to{opacity:1;transform:none;} }
@keyframes shake { 10%,90%{transform:translateX(-2px);} 20%,80%{transform:translateX(3px);} 30%,50%,70%{transform:translateX(-5px);} 40%,60%{transform:translateX(5px);} }
.lock-wrap { display:flex; justify-content:center; margin-bottom:4px; }
.lock-wrap svg { animation:float 4s ease-in-out infinite; }
@keyframes float { 0%,100%{transform:translateY(0);} 50%{transform:translateY(-6px);} }
.brand { font-size:11px; color:var(--ps); letter-spacing:.06em; text-transform:uppercase; margin-bottom:14px; opacity:.75; }
h1 { font-size:17px; color:var(--tx); margin:12px 0 3px; word-break:break-word; }
.meta { font-size:12.5px; color:var(--td); margin-bottom:16px; }
.lstatus { font-size:13px; color:var(--td); min-height:16px; margin-bottom:14px; }
.lstatus.err { color:var(--rd); } .lstatus.ok { color:var(--gr); }
/* PIN keypad. Row-wrap fix: PINs are fixed at exactly 6 digits everywhere
   (see PIN_LEN in voidos.py) — sized deliberately for exactly 6 boxes with
   margin to spare, flex-wrap explicitly nowrap so a future size/gap tweak
   can't silently drift past the container width again unnoticed. Already-
   issued PINs under older, wider length ranges still render at their own
   original box count via the PIN_LENGTH JS variable below. */
.pin-row{display:flex;justify-content:center;gap:7px;margin-bottom:24px;flex-wrap:nowrap}
.pin-box{position:relative;width:36px;height:50px;border-radius:12px;border:none;
  background:linear-gradient(160deg,rgba(var(--ov-rgb),.06),rgba(var(--ov-rgb),.02));
  display:flex;align-items:center;justify-content:center;
  box-shadow:0 1px 0 rgba(var(--ov-rgb),.05) inset,0 6px 14px rgba(0,0,0,.24);
  transition:box-shadow .32s cubic-bezier(.16,1,.3,1),transform .32s cubic-bezier(.16,1,.3,1),filter .32s cubic-bezier(.16,1,.3,1);
  overflow:hidden;isolation:isolate}
.pin-box::after{content:'';position:absolute;inset:0;border-radius:12px;padding:1.5px;pointer-events:none;
  background:linear-gradient(135deg,var(--pu),var(--ps));
  -webkit-mask:linear-gradient(#fff 0 0) content-box,linear-gradient(#fff 0 0);
  -webkit-mask-composite:xor;mask-composite:exclude}
.pin-box::before{content:'';position:absolute;inset:0;border-radius:12px;z-index:-1;opacity:0;
  background:radial-gradient(circle at 50% 30%,rgba(var(--pu-rgb),.5),transparent 72%);
  transition:opacity .35s cubic-bezier(.16,1,.3,1)}
.pin-box.active{filter:brightness(1.25);box-shadow:0 0 0 3px rgba(var(--pu-rgb),.16),0 6px 14px rgba(0,0,0,.24)}
.pin-box.filled{animation:pinPop .45s cubic-bezier(.22,1,.36,1)}
.pin-box.filled::before{opacity:.35}
@keyframes pinPop{0%{transform:scale(1)}35%{transform:scale(1.12)}65%{transform:scale(.98)}100%{transform:scale(1)}}
.pin-box .dot{width:12px;height:12px;border-radius:50%;
  background:linear-gradient(135deg,var(--pu),var(--ps));box-shadow:0 0 10px rgba(var(--pu-rgb),.7);
  transform:scale(0);transition:transform .3s cubic-bezier(.22,1,.36,1)}
.pin-box.filled .dot{transform:scale(1)}
#pinHidden{position:absolute;opacity:0;pointer-events:none;width:1px;height:1px}

.keypad{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:6px;justify-items:center}
.kbtn{position:relative;width:66px;height:66px;border:none;border-radius:50%;
  background:linear-gradient(160deg,rgba(var(--ov-rgb),.07),rgba(var(--ov-rgb),.02));
  border:1px solid rgba(var(--ov-rgb),.1);color:var(--tx);
  display:flex;flex-direction:column;align-items:center;justify-content:center;gap:1px;
  box-shadow:0 1px 0 rgba(var(--ov-rgb),.06) inset,0 3px 8px rgba(0,0,0,.24);
  overflow:hidden;isolation:isolate;transform:translateY(0) scale(1);
  transition:transform .5s cubic-bezier(.22,1.6,.36,1),box-shadow .38s cubic-bezier(.16,1,.3,1),
    background .38s cubic-bezier(.16,1,.3,1),border-color .38s cubic-bezier(.16,1,.3,1),color .38s cubic-bezier(.16,1,.3,1)}
.kbtn::before{content:'';position:absolute;inset:0;border-radius:50%;z-index:-1;opacity:0;
  background:radial-gradient(circle at 50% 30%,rgba(var(--pu-rgb),.55),transparent 70%);
  transition:opacity .38s cubic-bezier(.16,1,.3,1)}
.kbtn .knum{position:relative;z-index:3;font-size:22px;font-weight:700;letter-spacing:-.01em;line-height:1}
.kbtn:hover{border-color:rgba(var(--pu-rgb),.4);transform:translateY(-4px) scale(1);
  box-shadow:0 1px 0 rgba(var(--ov-rgb),.08) inset,0 14px 22px -6px rgba(var(--pu-rgb),.35),0 0 0 1px rgba(var(--pu-rgb),.12)}
.kbtn:hover::before{opacity:.16}
.kbtn:active,.kbtn.kpress{transform:translateY(1px) scale(.88,.86);
  box-shadow:0 1px 8px rgba(var(--pu-rgb),.4) inset,0 2px 4px rgba(0,0,0,.2);
  transition:transform .09s cubic-bezier(.4,0,.6,1),box-shadow .09s ease}
.kbtn:active::before,.kbtn.kpress::before{opacity:.32}
.kbtn.ghost{background:transparent;border-color:transparent;box-shadow:none;color:var(--tm);font-size:11px;font-weight:600;letter-spacing:.04em}
.kbtn.ghost:hover{background:rgba(var(--ov-rgb),.05);border-color:rgba(var(--ov-rgb),.09);color:var(--tx);
  transform:translateY(-2px) scale(1);box-shadow:0 8px 14px -6px rgba(0,0,0,.3)}
.kbtn.ghost:hover::before{opacity:0}
.kbtn.ghost:active,.kbtn.ghost.kpress{transform:translateY(1px) scale(.9,.88);box-shadow:none;
  transition:transform .09s cubic-bezier(.4,0,.6,1)}
.lform { text-align:left; }
.lfi { position:relative; margin-bottom:12px; }
.lfi input { width:100%; padding:12px 14px; background:rgba(var(--ov-rgb),.04); border:1px solid rgba(var(--ov-rgb),.09);
  border-radius:var(--rs); color:var(--tx); font-size:15px; outline:none; }
.lfi input:focus { border-color:var(--pu); background:rgba(var(--pu-rgb),.08); }
.pw-lfi input { padding-right:42px; }
.eyebtn { position:absolute; right:5px; top:50%; transform:translateY(-50%); background:none; border:none;
  color:var(--tm); padding:6px; border-radius:7px; display:flex; align-items:center; }
.eyebtn:hover { color:var(--ps); }
.lbtn { width:100%; margin-top:2px; padding:12px; border:none; border-radius:var(--rs);
  background:linear-gradient(135deg,var(--pu),var(--ps)); color:#fff; font-size:14.5px; font-weight:600;
  box-shadow:0 4px 14px rgba(var(--pu-rgb),.3); }
.lbtn:hover { filter:brightness(1.08); }
.lbtn:disabled { opacity:.55; pointer-events:none; }
.note { text-align:center; font-size:10.5px; color:var(--tm); margin-top:14px; }
@media(prefers-reduced-motion:reduce){*{animation-duration:.01ms!important;transition-duration:.01ms!important}}
</style></head>
<body>
<div class="blobs"><div class="blob b1"></div><div class="blob b2"></div></div>
<div class="login-wrap"><div class="lcard" id="lcard">
  <div class="lock-wrap">
    <svg id="lockSvg" width="52" height="52" viewBox="0 0 64 64" fill="none">
      <path id="shackle" d="M20 27V19C20 11.8 25.8 6 33 6C40.2 6 46 11.8 46 19V27" stroke="url(#rg)" stroke-width="5" stroke-linecap="round"/>
      <rect x="14" y="27" width="38" height="30" rx="8" fill="url(#rg)"/>
      <circle cx="33" cy="40" r="4.5" fill="#0a0a12"/><rect x="30.5" y="40" width="5" height="10" rx="2.5" fill="#0a0a12"/>
    </svg>
    <svg width="0" height="0" style="position:absolute"><defs><linearGradient id="rg" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#6d4aff"/><stop offset="100%" stop-color="#9b85ff"/></linearGradient></defs></svg>
  </div>
  <div class="brand">Void OS &middot; Access Gate</div>
  <h1>__APP_NAME__</h1>
  <div class="meta">A second layer in front of __APP_NAME__'s own sign-in — not a replacement for it.</div>
  <div class="lstatus __STATUS_CLASS__" id="lstat">__STATUS_TEXT__</div>
  __BODY__
  <div class="note">This gate is separate from __APP_NAME__'s own login, which still applies after this.</div>
</div></div>
<script>
// PIN keypad (pin-row + numeric keypad). Builds the pin-row boxes AND the
// keypad's 12 buttons dynamically rather than hardcoding them above —
// PIN_LENGTH varies per gate (an already-issued PIN under an older, wider
// length range still renders at its own original length; only newly-set
// ones are fixed at 6 digits, see PIN_LEN in voidos.py).
function initPinKeypad({pinRowEl, keypadEl, hiddenEl, pinLength, onComplete, isBusy}){
  pinRowEl.innerHTML = '';
  for(let i = 0; i < pinLength; i++){
    const b = document.createElement('div');
    b.className = 'pin-box';
    b.dataset.i = i;
    b.innerHTML = '<div class="dot"></div>';
    pinRowEl.appendChild(b);
  }
  const boxes = [...pinRowEl.querySelectorAll('.pin-box')];

  keypadEl.innerHTML = `
    <button class="kbtn" data-k="1"><span class="knum">1</span></button>
    <button class="kbtn" data-k="2"><span class="knum">2</span></button>
    <button class="kbtn" data-k="3"><span class="knum">3</span></button>
    <button class="kbtn" data-k="4"><span class="knum">4</span></button>
    <button class="kbtn" data-k="5"><span class="knum">5</span></button>
    <button class="kbtn" data-k="6"><span class="knum">6</span></button>
    <button class="kbtn" data-k="7"><span class="knum">7</span></button>
    <button class="kbtn" data-k="8"><span class="knum">8</span></button>
    <button class="kbtn" data-k="9"><span class="knum">9</span></button>
    <button class="kbtn ghost" data-k="clear">Clear</button>
    <button class="kbtn" data-k="0"><span class="knum">0</span></button>
    <button class="kbtn ghost" data-k="back">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M21 12H8M8 12l4-4M8 12l4 4"/><path d="M8 5H4a1 1 0 0 0-1 1v12a1 1 0 0 0 1 1h4"/>
      </svg>
    </button>`;

  function render(){
    const val = hiddenEl.value;
    boxes.forEach((b, i) => {
      b.classList.toggle('filled', i < val.length);
      b.classList.toggle('active', i === val.length);
    });
  }

  function reset(){ hiddenEl.value = ''; render(); }

  function flashKey(k){
    const btn = keypadEl.querySelector(`.kbtn[data-k="${k}"]`);
    if(!btn) return;
    btn.classList.add('kpress');
    setTimeout(() => btn.classList.remove('kpress'), 140);
  }

  function press(k){
    if(isBusy && isBusy()) return;
    if(k === 'back'){ hiddenEl.value = hiddenEl.value.slice(0, -1); render(); return; }
    if(k === 'clear'){ hiddenEl.value = ''; render(); return; }
    if(hiddenEl.value.length >= pinLength) return;
    hiddenEl.value += k;
    render();
    if(hiddenEl.value.length === pinLength) onComplete(hiddenEl.value);
  }

  keypadEl.addEventListener('click', e => {
    const btn = e.target.closest('.kbtn');
    if(!btn) return;
    press(btn.dataset.k);
  });

  const keydownHandler = e => {
    if(isBusy && isBusy()) return;
    if(/^[0-9]$/.test(e.key)){ press(e.key); flashKey(e.key); }
    else if(e.key === 'Backspace'){ press('back'); flashKey('back'); }
    else if(e.key === 'Escape'){ press('clear'); flashKey('clear'); }
  };
  document.addEventListener('keydown', keydownHandler);

  render();
  return {reset, destroy: () => document.removeEventListener('keydown', keydownHandler)};
}

const IS_PIN = __IS_PIN_JS__;
const PIN_LENGTH = __PIN_LENGTH_JS__;
const PATH = __PATH_JS__;
const NEXT = __NEXT_JS__;
const card = document.getElementById('lcard');
const lock = document.getElementById('lockSvg');
const stat = document.getElementById('lstat');
let busy = false;

async function submitSecret(value){
  if(busy) return;
  busy = true;
  stat.className = 'lstatus'; stat.textContent = 'Checking\\u2026';
  const pwBtn = document.getElementById('lBtn');
  if(pwBtn) pwBtn.disabled = true;
  let resp;
  try{
    resp = await fetch(PATH, {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: 'pin=' + encodeURIComponent(value) + '&next=' + encodeURIComponent(NEXT),
    });
  }catch(e){
    stat.textContent = 'Network error — try again'; stat.className = 'lstatus err';
    busy = false; if(pwBtn) pwBtn.disabled = false; return;
  }
  if(resp.ok){
    stat.textContent = 'Access granted\\u2026'; stat.className = 'lstatus ok';
    setTimeout(()=> location.reload(), 500);
    return;
  }
  let msg = IS_PIN ? 'Incorrect PIN' : 'Incorrect password';
  if(resp.status === 429){
    const wait = resp.headers.get('X-Retry-After');
    msg = wait ? `Too many attempts — try again in ${wait}s` : 'Too many attempts — try again shortly';
  }
  card.classList.add('shake');
  stat.textContent = msg; stat.className = 'lstatus err';
  setTimeout(()=> card.classList.remove('shake'), 450);
  busy = false;
  if(pwBtn) pwBtn.disabled = false;
  if(window.resetPin) window.resetPin();
}

// Shared with share.html — see modules/pin_keypad.js
if(IS_PIN){
  const kp = initPinKeypad({
    pinRowEl: document.getElementById('pinRow'),
    keypadEl: document.getElementById('keypad'),
    hiddenEl: document.getElementById('pinHidden'),
    pinLength: PIN_LENGTH,
    onComplete: submitSecret,
    isBusy: () => busy,
  });
  window.resetPin = kp.reset;
}else{
  const pw = document.getElementById('upass');
  const eyeBtn = document.getElementById('eyeBtn');
  const eyeIco = document.getElementById('eyeIco');
  const EO = '<path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/>';
  const EC = '<path d="M17.94 17.94A10.94 10.94 0 0 1 12 19c-7 0-11-7-11-7a18.5 18.5 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 7 11 7a18.5 18.5 0 0 1-2.16 3.19M14.12 14.12a3 3 0 1 1-4.24-4.24M1 1l22 22"/>';
  eyeBtn.addEventListener('click', ()=>{
    const show = pw.type === 'text';
    pw.type = show ? 'password' : 'text';
    eyeIco.innerHTML = show ? EO : EC;
  });
  window.resetPin = ()=>{ pw.value=''; pw.focus(); };
  pw.addEventListener('keydown', e=>{ if(e.key==='Enter') submitSecret(pw.value); });
  document.getElementById('lBtn').addEventListener('click', ()=>{
    if(!pw.value){ stat.textContent='Enter the password'; stat.className='lstatus err'; return; }
    submitSecret(pw.value);
  });
}
</script>
</body></html>"""

_PIN_BODY = """<div class="pin-row" id="pinRow"></div>
  <input id="pinHidden" inputmode="numeric" pattern="[0-9]*" maxlength="__PIN_LENGTH__" autocomplete="off"/>
  <div class="keypad" id="keypad"></div>"""

_PASSWORD_BODY = """<div class="lform">
    <div class="lfi pw-lfi">
      <input type="password" id="upass" placeholder="Password" autocomplete="off" autofocus/>
      <button class="eyebtn" id="eyeBtn" type="button" aria-label="Show password">
        <svg id="eyeIco" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/></svg>
      </button>
    </div>
    <button class="lbtn" id="lBtn" type="button">Unlock</button>
  </div>"""


def _js_str(v: str) -> str:
    """JSON string literal that's also safe inside an inline <script>:
    json.dumps leaves "<" alone, so a request path containing "</script>"
    closed the script block and injected markup -- reflected XSS on every
    gated app's hostname via a crafted link."""
    return (json.dumps(v).replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))

def _pin_page(next_path: str, error: str = None) -> bytes:
    next_path = _safe_next(next_path)
    body = (_PIN_BODY.replace("__PIN_LENGTH__", str(PIN_LENGTH)) if IS_PIN else _PASSWORD_BODY)
    status_text = _esc(error) if error else (f"Enter the {PIN_LENGTH}-digit PIN" if IS_PIN else "Enter the password to continue")
    status_class = "lstatus err" if error else "lstatus"
    page = (_PAGE_SHELL
            .replace("__APP_NAME__", _esc(APP_NAME))
            .replace("__STATUS_CLASS__", status_class.replace("lstatus ", "").replace("lstatus", "").strip())
            .replace("__STATUS_TEXT__", status_text)
            .replace("__BODY__", body)
            .replace("__IS_PIN_JS__", "true" if IS_PIN else "false")
            .replace("__PIN_LENGTH_JS__", str(PIN_LENGTH))
            .replace("__PATH_JS__", _js_str(next_path or "/"))
            .replace("__NEXT_JS__", _js_str(next_path or "/")))
    return page.encode("utf-8")


async def _read_headers(reader: asyncio.StreamReader):
    """Read raw bytes through the end of the header block and parse just
    enough to make the auth decision: method, path, and a lowercased header
    dict. Returns (raw_bytes_including_blank_line, method, path, headers)."""
    raw = await reader.readuntil(b"\r\n\r\n")
    lines = raw.split(b"\r\n")
    method, path, _ = lines[0].decode("latin-1", errors="replace").split(" ", 2)
    headers = {}
    for line in lines[1:]:
        if b":" in line:
            k, v = line.split(b":", 1)
            headers[k.decode("latin-1").strip().lower()] = v.decode("latin-1").strip()
    return raw, method, path, headers


async def _read_body(reader: asyncio.StreamReader, headers: dict) -> bytes:
    try:
        length = int(headers.get("content-length", "0"))
    except ValueError:
        length = 0
    length = min(max(length, 0), 8192)  # PIN submissions are tiny; capped defensively regardless of what's claimed
    if length <= 0:
        return b""
    try:
        return await asyncio.wait_for(reader.readexactly(length), timeout=10)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError):
        return b""


def _parse_form(body: bytes) -> dict:
    try:
        s = body.decode("utf-8", errors="replace")
    except Exception:
        return {}
    out = {}
    for pair in s.split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            out[unquote_plus(k)] = unquote_plus(v)
    return out


def _parse_cookies(header_val: str) -> dict:
    out = {}
    for part in (header_val or "").split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            out[k] = v
    return out


# The cookie is bound to THIS gate (app id) and to the current PIN. All gates
# are signed with Void OS's one secret key, and the payload used to be just
# {"g": true} -- so a cookie earned with app A's PIN (say, the media server
# you gave a friend) unlocked every other gated app (say, Portainer) when
# replayed against its hostname, and changing a PIN never logged anyone out.
import hashlib as _hashlib
_PIN_FPR = _hashlib.sha256(PIN_HASH.encode("utf-8")).hexdigest()[:16]

def _client_ip(peer_ip: str, headers: dict) -> str:
    """This server only listens on 127.0.0.1, so its peer is always the
    local cloudflared -- which sets CF-Connecting-IP to the real visitor.
    Without this every visitor shared one lockout bucket (127.0.0.1), and
    one stranger's wrong guesses locked out everybody."""
    if peer_ip in ("127.0.0.1", "::1"):
        cf = (headers.get("cf-connecting-ip") or "").strip()
        if cf and len(cf) <= 45 and all(c in "0123456789abcdefABCDEF.:" for c in cf):
            return cf
    return peer_ip

def _safe_next(nxt: str) -> str:
    """Post-unlock redirect target: same-host paths only. Blocks open
    redirects ("//evil.com", "/\\evil.com", "https://...") and CR/LF header
    injection via a crafted form value (it's written straight into the
    Location header)."""
    nxt = str(nxt or "/")
    if (not nxt.startswith("/") or nxt.startswith("//") or nxt.startswith("/\\")
            or any(ord(c) < 32 or c == "\x7f" for c in nxt)):
        return "/"
    return nxt[:2048]

def _has_valid_cookie(headers: dict) -> bool:
    tok = _parse_cookies(headers.get("cookie", "")).get(COOKIE_NAME)
    if not tok:
        return False
    try:
        data = _serial.loads(tok, max_age=COOKIE_TTL_HOURS * 3600)
    except (BadSignature, SignatureExpired):
        return False
    return (isinstance(data, dict) and data.get("g") is True
            and data.get("a") == APP_ID and data.get("p") == _PIN_FPR)


async def _proxy_through(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, initial_bytes: bytes):
    """Cookie already validated. Forward the original request bytes to the
    real backend, then blindly pump bytes both directions for the rest of
    the connection's life — this is what makes WebSocket upgrades (and
    anything else) transparent: once the initial HTTP Upgrade handshake's
    response has passed through, subsequent bytes are just raw WS frames,
    which this pump forwards without needing to understand them at all."""
    try:
        bpr, bpw = await asyncio.open_connection("127.0.0.1", BACKEND_PORT)
    except OSError:
        writer.write(_http_response(502, "Bad Gateway", {"Content-Type": "text/plain"}, b"App not reachable"))
        try: await writer.drain()
        except Exception: pass
        return

    bpw.write(initial_bytes)
    try:
        await bpw.drain()
    except (ConnectionResetError, BrokenPipeError):
        return

    async def pump(src, dst):
        try:
            while True:
                chunk = await src.read(65536)
                if not chunk:
                    break
                dst.write(chunk)
                await dst.drain()
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            try: dst.write_eof()
            except Exception: pass

    await asyncio.gather(pump(reader, bpw), pump(bpr, writer), return_exceptions=True)
    for w in (writer, bpw):
        try: w.close()
        except Exception: pass


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    peer = writer.get_extra_info("peername")
    ip = peer[0] if peer else "?"
    try:
        raw, method, path, headers = await asyncio.wait_for(_read_headers(reader), timeout=15)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ValueError, asyncio.LimitOverrunError):
        try: writer.close()
        except Exception: pass
        return
    ip = _client_ip(ip, headers)

    try:
        if _has_valid_cookie(headers):
            await _proxy_through(reader, writer, raw)
            return

        if method == "GET":
            writer.write(_http_response(200, "OK", {"Content-Type": "text/html; charset=utf-8"}, _pin_page(path)))
            await writer.drain()

        elif method == "POST":
            body = await _read_body(reader, headers)
            form = _parse_form(body)
            pin = form.get("pin", "")
            nxt = _safe_next(form.get("next", "/"))
            locked, wait = _is_locked(ip)
            if not locked:
                locked, wait = _global_locked()
            if locked:
                _audit(ip, "locked_out")
                writer.write(_http_response(429, "Too Many Requests",
                                             {"Content-Type": "text/html; charset=utf-8", "X-Retry-After": str(wait)},
                                             _pin_page(nxt, error=f"Too many attempts — try again in {wait}s")))
                await writer.drain()
            elif check_password_hash(PIN_HASH, pin):
                _clear_fails(ip)
                _audit(ip, "success")
                tok = _serial.dumps({"g": True, "a": APP_ID, "p": _PIN_FPR})
                secure_flag = "; Secure" if COOKIE_SECURE else ""
                cookie = f"{COOKIE_NAME}={tok}; Path=/; HttpOnly; SameSite=Lax{secure_flag}; Max-Age={int(COOKIE_TTL_HOURS*3600)}"
                writer.write(_http_response(302, "Found", {"Location": nxt or "/", "Set-Cookie": cookie}, b""))
                await writer.drain()
            else:
                _fail(ip)
                _global_fail()
                _audit(ip, "fail")
                writer.write(_http_response(403, "Forbidden", {"Content-Type": "text/html; charset=utf-8"},
                                             _pin_page(nxt, error="Incorrect PIN" if IS_PIN else "Incorrect password")))
                await writer.drain()
        else:
            # Any other method while unauthenticated — including a raw WebSocket
            # upgrade attempt made without ever having loaded the PIN page first —
            # gets a flat denial. There's no meaningful HTML page to show a
            # WS handshake anyway; the normal flow always hits GET first.
            writer.write(_http_response(401, "Unauthorized", {"Content-Type": "text/plain"}, b"Authentication required"))
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        try: writer.close()
        except Exception: pass


async def main():
    server = await asyncio.start_server(handle, "127.0.0.1", LISTEN_PORT, limit=65536)
    actual_port = server.sockets[0].getsockname()[1]
    # The parent process reads this exact line the same way it already
    # sniffs cloudflared's own stdout for the connected tunnel URL.
    print(f"GATE_LISTENING:{actual_port}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
