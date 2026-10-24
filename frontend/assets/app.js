/* ============================================================
   Thai-Chinese Tutor - 学生端共用逻辑
   ============================================================ */

const TOKEN_KEY = "session_token";
const CLASS_KEY = "class_id";
const TRACK_KEY = "track";
const NAME_KEY = "display_name";

const App = {
  /* ---------- 登录状态检查 ---------- */
  requireLogin() {
    const token = localStorage.getItem(TOKEN_KEY);
    if (!token) { location.href = "index.html"; return false; }
    return true;
  },
  logout() {
    [TOKEN_KEY, CLASS_KEY, TRACK_KEY, NAME_KEY].forEach(k => localStorage.removeItem(k));
    location.href = "index.html";
  },

  /* ---------- 旧 LINE boot 保留为占位 ---------- */
  boot() { return Promise.resolve("form-login"); },

  /* ---------- 后端请求（带 Bearer token） ---------- */
  async call(path, opts = {}) {
    const token = localStorage.getItem(TOKEN_KEY) || "";
    const r = await fetch(path, {
      ...opts,
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer " + token,
        ...(opts.headers || {}),
      },
    });
    if (r.status === 401) { localStorage.removeItem(TOKEN_KEY); location.href = "index.html"; throw new Error("unauthorized"); }
    if (!r.ok) { const text = await r.text(); throw new Error(text || "request failed"); }
    return r.json();
  },

  /* ---------- HTML 转义 ---------- */
  escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
  },

  /* ---------- 语音识别（浏览器内置） ---------- */
  speechSupported() {
    // 浏览器识别可用，或者能用 MediaRecorder 录音（走服务器 ASR）
    return !!(window.SpeechRecognition || window.webkitSpeechRecognition) || this.recorderSupported();
  },
  recorderSupported() {
    return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia
              && window.MediaRecorder && (window.AudioContext || window.webkitAudioContext));
  },
  speech(lang, onFinal, onInterim, onEnd, onError) {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    // ① 有浏览器识别就直接用（快，即时出字）
    if (SR) {
      const rec = new SR();
      rec.lang = lang || "zh-CN";
      rec.interimResults = true;
      rec.maxAlternatives = 1;
      rec.onresult = (e) => {
        let interim = "", final = "";
        for (let i = e.resultIndex; i < e.results.length; i++) {
          const t = e.results[i][0].transcript;
          if (e.results[i].isFinal) final += t;
          else interim += t;
        }
        if (interim && onInterim) onInterim(interim);
        if (final && onFinal) onFinal(final);
      };
      rec.onend = () => { if (onEnd) onEnd(); };
      rec.onerror = (e) => {
        const err = (e && e.error) || "unknown";
        // 浏览器识别被拒/不可用时，自动改用录音上传
        if ((err === "not-allowed" || err === "service-not-allowed" || err === "language-not-supported")
            && App.recorderSupported()) {
          App._recordAsr(onFinal, onInterim, onEnd, onError);
          return;
        }
        if (onError) onError(err);
        if (onEnd) onEnd();
      };
      try { rec.start(); } catch (e) {
        if (App.recorderSupported()) { App._recordAsr(onFinal, onInterim, onEnd, onError); return null; }
        if (onError) onError("start-failed"); if (onEnd) onEnd();
      }
      return rec;
    }
    // ② 没有浏览器识别（LINE 内置浏览器就是这样）→ 录音上传到服务器 ASR
    return App._recordAsr(onFinal, onInterim, onEnd, onError);
  },

  /* 录音 → 16kHz 单声道 WAV → /api/asr → onFinal(文本) */
  _recordAsr(onFinal, onInterim, onEnd, onError) {
    if (!App.recorderSupported()) {
      if (onError) onError("unsupported");
      if (onEnd) onEnd();
      return null;
    }
    let stopped = false, doStop = null, finished = false;
    const done = (err, text) => {
      if (finished) return;
      finished = true;
      if (text && onFinal) onFinal(text);
      if (err && onError) onError(err);
      if (onEnd) onEnd();
    };
    navigator.mediaDevices.getUserMedia({ audio: true }).then(stream => {
      if (stopped) { stream.getTracks().forEach(t => t.stop()); return; }
      const mr = new MediaRecorder(stream);
      const chunks = [];
      mr.ondataavailable = e => { if (e.data && e.data.size) chunks.push(e.data); };
      mr.onstop = async () => {
        stream.getTracks().forEach(t => t.stop());
        try {
          const wav = await App._toWav(new Blob(chunks, { type: mr.mimeType || "audio/webm" }));
          const r = await App.call("/api/asr", {
            method: "POST",
            body: JSON.stringify({ audio_b64: App._b64(wav), sample_rate: 16000 }),
          });
          const t = (r && r.text || "").trim();
          if (t) done(null, t);
          else done("no-speech");
        } catch (e) {
          done("asr-failed");
        }
      };
      try { mr.start(); } catch (e) { done("start-failed"); return; }
      doStop = () => { try { mr.stop(); } catch (e) { done("stop-failed"); } };
    }).catch(e => {
      const name = (e && e.name) || "";
      done(name === "NotAllowedError" || name === "SecurityError" ? "not-allowed" : "audio-capture");
    });
    return { stop: () => { stopped = true; if (doStop) doStop(); else done(null, ""); } };
  },

  /* 把任意音频 Blob 解码后重采样成 16kHz 单声道 16bit WAV */
  async _toWav(blob) {
    const ab = await blob.arrayBuffer();
    const AC = window.AudioContext || window.webkitAudioContext;
    const ctx = new AC();
    let buf;
    try { buf = await ctx.decodeAudioData(ab.slice(0)); }
    finally { try { ctx.close(); } catch (e) {} }
    const SR = 16000;
    const frames = Math.max(1, Math.ceil((buf.duration || 1) * SR));
    let pcm;
    if (window.OfflineAudioContext || window.webkitOfflineAudioContext) {
      const OC = window.OfflineAudioContext || window.webkitOfflineAudioContext;
      const off = new OC(1, frames, SR);
      const src = off.createBufferSource();
      src.buffer = buf; src.connect(off.destination); src.start();
      pcm = (await off.startRendering()).getChannelData(0);
    } else {
      // 退回：直接取第一个声道（采样率可能不是 16k，多数设备仍能识别）
      pcm = buf.getChannelData(0);
    }
    const n = pcm.length;
    const out = new ArrayBuffer(44 + n * 2);
    const v = new DataView(out);
    const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    w(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); w(8, "WAVE");
    w(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true);
    v.setUint16(22, 1, true); v.setUint32(24, SR, true);
    v.setUint32(28, SR * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
    w(36, "data"); v.setUint32(40, n * 2, true);
    let o = 44;
    for (let i = 0; i < n; i++, o += 2) {
      const s = Math.max(-1, Math.min(1, pcm[i]));
      v.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
    }
    return out;
  },
  _b64(ab) {
    const bytes = new Uint8Array(ab);
    let bin = "";
    for (let i = 0; i < bytes.length; i += 0x8000) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return btoa(bin);
  },

  /* ---------- 中文朗读 ----------
     优先播放配音文件；文件不存在（服务器未部署音频）或播放失败时，
     自动退回浏览器内置的中文合成语音，保证"能听得到"。 */
  speak(text, url) {
    const tts = () => {
      try {
        if (!window.speechSynthesis || !text) return;
        window.speechSynthesis.cancel();
        const u = new SpeechSynthesisUtterance(String(text));
        u.lang = "zh-CN";
        u.rate = 0.9;
        const vs = window.speechSynthesis.getVoices() || [];
        const v = vs.find(x => /^zh/i.test(x.lang)) || vs.find(x => /Chinese/i.test(x.name));
        if (v) u.voice = v;
        window.speechSynthesis.speak(u);
      } catch (e) { /* 设备不支持合成语音，静默忽略 */ }
    };
    if (!url) return tts();
    const a = new Audio(url);
    let fell = false;
    const fallback = () => { if (fell) return; fell = true; tts(); };
    a.onerror = fallback;
    const p = a.play();
    if (p && p.catch) p.catch(fallback);
  },
  ttsSupported() {
    return !!window.speechSynthesis;
  },

  /* ---------- 会话埋点 ---------- */
  sessionStart() {
    const key = "session_started_" + new Date().toDateString();
    if (!sessionStorage.getItem(key)) {
      this.call("/api/session_start", { method: "POST", body: "{}" }).catch(() => {});
      sessionStorage.setItem(key, "1");
    }
  },
};
