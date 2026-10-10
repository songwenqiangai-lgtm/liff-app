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
    return !!(window.SpeechRecognition || window.webkitSpeechRecognition);
  },
  speech(lang, onFinal, onInterim, onEnd, onError) {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) { if (onError) onError("unsupported"); return null; }
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
    // 把具体错误类型抛出去，方便区分"没权限/不支持/网络问题"
    rec.onerror = (e) => { if (onError) onError((e && e.error) || "unknown"); if (onEnd) onEnd(); };
    try { rec.start(); } catch (e) { if (onError) onError("start-failed"); if (onEnd) onEnd(); }
    return rec;
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
