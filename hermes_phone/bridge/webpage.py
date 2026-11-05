"""The single self-contained web phone page (inline CSS and JS, no external requests).

The page speaks the same websocket protocol the bridge expects from Twilio Media Streams:
``connected``, ``start`` (customParameters.token), ``media`` (base64 G.711 mu-law, 8 kHz, 20 ms
frames), ``mark`` echoes and ``stop``; it understands inbound ``media``, ``mark`` and ``clear``
(plus ``transcript`` and ``ended`` from this bridge).
"""
from __future__ import annotations

from html import escape

# Pure helpers (no DOM), so they can be unit tested under node.
CODEC_JS = r"""
const CODEC = (function () {
  function lin2ulaw(s) {
    const BIAS = 0x84, CLIP = 32635;
    const sign = (s >> 8) & 0x80;
    if (sign) s = -s;
    if (s > CLIP) s = CLIP;
    s += BIAS;
    let exp = 7;
    for (let mask = 0x4000; (s & mask) === 0 && exp > 0; exp--, mask >>= 1) {}
    const mant = (s >> (exp + 3)) & 0x0f;
    return (~(sign | (exp << 4) | mant)) & 0xff;
  }
  function ulaw2lin(u) {
    u = ~u & 0xff;
    const sign = u & 0x80, exp = (u >> 4) & 7, mant = u & 15;
    const s = (((mant << 3) + 0x84) << exp) - 0x84;
    return sign ? -s : s;
  }
  const ULAW = new Float32Array(256);
  for (let i = 0; i < 256; i++) ULAW[i] = ulaw2lin(i) / 32768;

  // Streaming area-average downsampler: any input rate -> 8000 Hz float samples.
  function Downsampler(inRate) {
    const ratio = inRate / 8000;
    let phase = 0, acc = 0;
    this.push = function (f32) {
      const out = [];
      for (let i = 0; i < f32.length; i++) {
        const x = f32[i];
        let rem = 1;
        while (rem > 1e-9) {
          const need = ratio - phase;
          if (rem >= need) { acc += x * need; out.push(acc / ratio); acc = 0; phase = 0; rem -= need; }
          else { acc += x * rem; phase += rem; rem = 0; }
        }
      }
      return out;
    };
  }

  // Collects 8 kHz float samples into 160-byte (20 ms) mu-law frames.
  function Framer(onFrame) {
    let buf = new Uint8Array(160), n = 0;
    this.push = function (samples) {
      for (let i = 0; i < samples.length; i++) {
        let v = Math.round(samples[i] * 32767);
        if (v > 32767) v = 32767; else if (v < -32768) v = -32768;
        buf[n++] = lin2ulaw(v);
        if (n === 160) { onFrame(buf); buf = new Uint8Array(160); n = 0; }
      }
    };
  }

  function decode(bytes) {
    const out = new Float32Array(bytes.length);
    for (let i = 0; i < bytes.length; i++) out[i] = ULAW[bytes[i]];
    return out;
  }
  function upsample(x, outRate) {
    const ratio = outRate / 8000, n = Math.round(x.length * ratio);
    const out = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      const t = i / ratio, i0 = Math.floor(t), f = t - i0;
      const a = x[Math.min(i0, x.length - 1)], b = i0 + 1 < x.length ? x[i0 + 1] : a;
      out[i] = a + (b - a) * f;
    }
    return out;
  }
  function b64ToBytes(b64) {
    const bin = atob(b64), u = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
    return u;
  }
  function bytesToB64(u8) {
    let s = '';
    for (let i = 0; i < u8.length; i++) s += String.fromCharCode(u8[i]);
    return btoa(s);
  }
  return { lin2ulaw, ulaw2lin, Downsampler, Framer, decode, upsample, b64ToBytes, bytesToB64 };
})();
"""

APP_JS = r"""
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const token = new URLSearchParams(location.search).get('t') || '';
  const callId = document.body.getAttribute('data-call');
  let ac = null, ws = null, stream = null, proc = null, src = null, streamSid = '';
  let chunk = 0, playHead = 0, sources = [], marks = [], timer = null, wake = null, over = false;

  function status(t) { $('status').textContent = t; }
  function line(role, text) {
    const d = document.createElement('div');
    d.className = 'line ' + (role === 'agent' ? 'agent' : 'me');
    d.textContent = (role === 'agent' ? 'Assistant: ' : 'You: ') + text;
    $('tx').appendChild(d);
    $('tx').scrollTop = $('tx').scrollHeight;
  }
  function send(o) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }

  function play(b64) {
    if (!ac) return;
    const pcm = CODEC.upsample(CODEC.decode(CODEC.b64ToBytes(b64)), ac.sampleRate);
    if (!pcm.length) return;
    const buf = ac.createBuffer(1, pcm.length, ac.sampleRate);
    buf.getChannelData(0).set(pcm);
    const s = ac.createBufferSource();
    s.buffer = buf;
    s.connect(ac.destination);
    const now = ac.currentTime;
    if (playHead < now + 0.04) playHead = now + 0.08;  // small jitter cushion after a gap
    s.start(playHead);
    playHead += buf.duration;
    sources.push(s);
    s.onended = () => { const i = sources.indexOf(s); if (i >= 0) sources.splice(i, 1); };
  }
  function clearPlayback() {
    sources.slice().forEach((s) => { try { s.onended = null; s.stop(); } catch (e) {} });
    sources = [];
    playHead = 0;
    marks.splice(0).forEach((m) => send({ event: 'mark', streamSid: streamSid, mark: { name: m.name } }));
  }
  function pumpMarks() {  // echo each mark once the audio before it has actually played
    if (!ac) return;
    while (marks.length && ac.currentTime >= marks[0].at) {
      send({ event: 'mark', streamSid: streamSid, mark: { name: marks.shift().name } });
    }
  }

  function startMic() {
    src = ac.createMediaStreamSource(stream);
    proc = ac.createScriptProcessor(2048, 1, 1);
    const down = new CODEC.Downsampler(ac.sampleRate);
    const framer = new CODEC.Framer((bytes) => {
      const ts = chunk * 20;
      chunk += 1;
      send({ event: 'media', streamSid: streamSid, media: {
        track: 'inbound', chunk: String(chunk), timestamp: String(ts),
        payload: CODEC.bytesToB64(bytes) } });
    });
    proc.onaudioprocess = (e) => {
      if (!ws || ws.readyState !== 1) return;
      framer.push(down.push(e.inputBuffer.getChannelData(0)));
    };
    const mute = ac.createGain();
    mute.gain.value = 0;
    src.connect(proc); proc.connect(mute); mute.connect(ac.destination);
  }

  function finish(msg) {
    if (over) return;
    over = true;
    if (timer) clearInterval(timer);
    try { if (ws && ws.readyState <= 1) ws.close(); } catch (e) {}
    try { if (stream) stream.getTracks().forEach((t) => t.stop()); } catch (e) {}
    try { if (proc) proc.disconnect(); if (src) src.disconnect(); } catch (e) {}
    try { if (wake) wake.release(); } catch (e) {}
    sources.forEach((s) => { try { s.stop(); } catch (e) {} });
    setTimeout(() => { try { if (ac) ac.close(); } catch (e) {} }, 300);
    status(msg);
    $('answer').hidden = true;
    $('hang').hidden = true;
    $('dot').className = 'dot off';
  }

  function openSocket(info) {
    streamSid = 'MW' + Array.from(crypto.getRandomValues(new Uint8Array(8)),
      (b) => ('0' + b.toString(16)).slice(-2)).join('');
    ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host
      + '/media/' + encodeURIComponent(callId));
    ws.onopen = () => {
      send({ event: 'connected', protocol: 'Call', version: '1.0.0' });
      send({ event: 'start', sequenceNumber: '1', streamSid: streamSid, start: {
        streamSid: streamSid, callSid: info.call_sid, tracks: ['inbound'],
        mediaFormat: { encoding: 'audio/x-mulaw', sampleRate: 8000, channels: 1 },
        customParameters: { token: info.media_token } } });
      startMic();
      status('Connected. Say hello.');
      $('dot').className = 'dot on';
      $('hang').hidden = false;
      timer = setInterval(pumpMarks, 40);
    };
    ws.onmessage = (ev) => {
      let m;
      try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (m.event === 'media' && m.media && m.media.payload) {
        play(m.media.payload);
      } else if (m.event === 'mark' && m.mark) {
        marks.push({ name: m.mark.name, at: playHead });
      } else if (m.event === 'clear') {
        clearPlayback();
      } else if (m.event === 'transcript') {
        line(m.role, m.text);
      } else if (m.event === 'ended') {
        finish('Call ended.');
      }
    };
    ws.onerror = () => finish('Connection problem. The call ended.');
    ws.onclose = () => finish('Call ended.');
  }

  async function answer() {
    $('answer').disabled = true;
    status('Allow the microphone when asked...');
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      ac = new AC();                 // created inside the tap: required by iOS Safari
      const resumed = ac.resume();   // must start synchronously in the tap handler
      const unlock = ac.createBufferSource();  // silent blip unlocks output on iOS
      unlock.buffer = ac.createBuffer(1, 1, 22050);
      unlock.connect(ac.destination);
      unlock.start(0);
      stream = await navigator.mediaDevices.getUserMedia({ audio: {
        echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
      await resumed;
      try { if (navigator.wakeLock) wake = await navigator.wakeLock.request('screen'); } catch (e) {}
      status('Connecting...');
      const r = await fetch('/phone/' + encodeURIComponent(callId) + '/answer', {
        method: 'POST', cache: 'no-store', credentials: 'omit',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ t: token }) });
      if (!r.ok) throw new Error('link');
      const info = await r.json();
      try { history.replaceState(null, '', location.pathname); } catch (e) {}  // drop the token
      openSocket(info);
    } catch (e) {
      const denied = e && (e.name === 'NotAllowedError' || e.name === 'NotFoundError');
      finish(denied ? 'Microphone blocked. Allow it in your browser settings and use a new link.'
                    : 'This link is no longer valid. Ask for a new call.');
    }
  }
  function hang() {
    send({ event: 'stop', streamSid: streamSid });
    finish('You hung up.');
  }
  $('answer').addEventListener('click', answer);
  $('hang').addEventListener('click', hang);
})();
"""

_CSS = r"""
:root{--bg:#f6f7f9;--fg:#14171c;--mut:#5b6472;--card:#fff;--ok:#17803d;--bad:#c62828;--line:#e3e6eb}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--fg:#eceff4;--mut:#9aa4b2;--card:#181b22;--ok:#3ddc84;--bad:#ff6b6b;--line:#262b35}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;min-height:100vh;display:flex;justify-content:center}
main{width:100%;max-width:440px;padding:24px 16px;display:flex;flex-direction:column;gap:16px}
h1{font-size:1.25rem;margin:0}
p{margin:0;color:var(--mut)}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px;display:flex;flex-direction:column;gap:12px}
.st{display:flex;align-items:center;gap:10px;font-weight:600}
.dot{width:12px;height:12px;border-radius:50%;background:#e0a800;flex:none}
.dot.on{background:var(--ok)}.dot.off{background:var(--mut)}
button{font:inherit;font-weight:700;font-size:1.2rem;border:0;border-radius:14px;padding:18px;min-height:64px;width:100%;cursor:pointer;color:#fff;touch-action:manipulation}
#answer{background:var(--ok);color:#06210f}
#hang{background:var(--bad)}
button:disabled{opacity:.6}
[hidden]{display:none!important}
#tx{max-height:34vh;overflow:auto;display:flex;flex-direction:column;gap:6px;font-size:.95rem}
#tx:empty{display:none}
.line.agent{color:var(--fg)}.line.me{color:var(--mut)}
"""


def render_page(*, call_id: str, callee: str, nonce: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<meta name="robots" content="noindex,nofollow">'
        '<meta name="referrer" content="no-referrer">'
        "<title>Hermes phone</title>"
        f'<style nonce="{nonce}">{_CSS}</style></head>'
        f'<body data-call="{escape(call_id, quote=True)}"><main>'
        "<h1>Hermes phone call</h1>"
        f"<p>Your assistant is calling as <b>{escape(callee)}</b>. Tap Answer and speak. "
        "Headphones avoid echo.</p>"
        '<div class="card"><div class="st"><span id="dot" class="dot"></span>'
        '<span id="status" role="status" aria-live="polite">Ringing...</span></div>'
        '<button id="answer" type="button">Answer</button>'
        '<button id="hang" type="button" hidden>Hang up</button>'
        '<div id="tx" aria-label="Live transcript"></div></div>'
        "<noscript><p>JavaScript is required for the web phone.</p></noscript>"
        f'</main><script nonce="{nonce}">{CODEC_JS}{APP_JS}</script></body></html>'
    )


def render_error() -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow"><title>Hermes phone</title>'
        "<style>body{font:16px system-ui,sans-serif;margin:0;padding:32px 16px;max-width:440px;"
        "margin:auto;color:#14171c;background:#f6f7f9}@media (prefers-color-scheme:dark)"
        "{body{color:#eceff4;background:#0f1115}}</style></head><body>"
        "<h1>This link is not valid</h1><p>It may have expired or already been used. "
        "Ask your assistant to start a new call.</p></body></html>"
    )
