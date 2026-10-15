// Void OS intercom: audio worklets.
// Wire format: mono, 24 kHz, 20 ms frames (480 samples), mu-law (G.711 style), same as
// modules/intercom_audio.py. Loaded by static/app.js with audioWorklet.addModule().
const RATE = 24000, FRAME = 480;

// floor(log2(i)) for 1..255, the exponent table of mu-law
const EXP = new Uint8Array(256);
for (let i = 1; i < 256; i++) EXP[i] = 31 - Math.clz32(i);
const DEC = new Float32Array(256);
for (let u = 0; u < 256; u++) {
  const v = ~u & 0xFF, sign = v & 0x80, e = (v >> 4) & 7, m = v & 15;
  const s = (((m << 3) + 0x84) << e) - 0x84;
  DEC[u] = (sign ? -s : s) / 32768;
}
function enc(f) {                       // float -1..1 -> mu-law byte
  let s = Math.max(-1, Math.min(1, f)) * 32767 | 0;
  const sign = s < 0 ? 0x80 : 0;
  if (sign) s = -s;
  if (s > 32635) s = 32635;
  s += 0x84;
  const e = EXP[(s >> 7) & 0xFF], m = (s >> (e + 3)) & 0x0F;
  return ~(sign | (e << 4) | m) & 0xFF;
}

// Microphone -> 24 kHz frames. Posts {frame: ArrayBuffer(1+480), level} while "on",
// and {level} only when off (so the page can still draw a level meter).
class IcMic extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / RATE; this.phase = 0; this.acc = 0; this.cnt = 0;
    this.pcm = new Float32Array(FRAME); this.n = 0; this.on = false; this.k = 0;
    this.port.onmessage = (e) => { if ('on' in e.data) this.on = !!e.data.on; };
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      this.acc += ch[i]; this.cnt++; this.phase++;
      if (this.phase >= this.ratio) {            // box filter, then take one sample: cheap anti-aliasing
        this.phase -= this.ratio;
        this.pcm[this.n++] = this.acc / this.cnt; this.acc = 0; this.cnt = 0;
        if (this.n === FRAME) { this.flush(); this.n = 0; }
      }
    }
    return true;
  }
  flush() {
    let pk = 0;
    for (let i = 0; i < FRAME; i++) { const a = Math.abs(this.pcm[i]); if (a > pk) pk = a; }
    if (this.on) {
      const out = new Uint8Array(FRAME + 1);
      out[0] = 1;
      for (let i = 0; i < FRAME; i++) out[i + 1] = enc(this.pcm[i]);
      this.port.postMessage({ frame: out.buffer, level: pk }, [out.buffer]);
    } else if ((this.k++ & 1) === 0) {
      this.port.postMessage({ level: pk });
    }
  }
}
registerProcessor('ic-mic', IcMic);

// mu-law bytes (24 kHz) -> the output device, with a small adaptive jitter buffer.
// Plays once ~80 ms is queued, trims if more than ~280 ms piles up, goes quiet and re-buffers on a gap.
class IcPlayer extends AudioWorkletProcessor {
  constructor() {
    super();
    this.q = []; this.qn = 0; this.pos = 0; this.frac = 0; this.step = RATE / sampleRate;
    this.primed = false; this.target = Math.round(RATE * 0.08); this.max = Math.round(RATE * 0.28); this.t = 0;
    this.port.onmessage = (e) => {
      const u = e.data.ulaw;                        // raw mu-law bytes straight from the socket
      if (!u) return;
      const a = new Float32Array(u.length);
      for (let i = 0; i < u.length; i++) a[i] = DEC[u[i]];
      this.q.push(a); this.qn += a.length;
      if (this.qn > this.max) {                   // too far behind: skip ahead to keep the delay short
        let drop = this.qn - this.target;
        while (drop > 0 && this.q.length) {
          const avail = this.q[0].length - this.pos;
          if (avail <= drop) { this.q.shift(); this.qn -= avail; drop -= avail; this.pos = 0; }
          else { this.pos += drop; this.qn -= drop; drop = 0; }
        }
      }
    };
  }
  process(inputs, outputs) {
    const out = outputs[0][0];
    if (!out) return true;
    if (!this.primed) {
      if (this.qn >= this.target) this.primed = true;
      else { out.fill(0); this.stat(); return true; }
    }
    for (let i = 0; i < out.length; i++) {
      if (this.qn < 2 || !this.q.length) { this.primed = false; for (let j = i; j < out.length; j++) out[j] = 0; break; }
      const cur = this.q[0][this.pos];
      let nxt;
      if (this.pos + 1 < this.q[0].length) nxt = this.q[0][this.pos + 1];
      else nxt = this.q.length > 1 ? this.q[1][0] : cur;
      out[i] = cur + (nxt - cur) * this.frac;
      this.frac += this.step;
      while (this.frac >= 1) {
        this.frac -= 1; this.pos++; this.qn--;
        if (this.q.length && this.pos >= this.q[0].length) { this.q.shift(); this.pos = 0; }
      }
    }
    this.stat();
    return true;
  }
  stat() {
    if ((this.t++ & 31) === 0) this.port.postMessage({ buf: Math.round(this.qn / RATE * 1000) });
  }
}
registerProcessor('ic-player', IcPlayer);
