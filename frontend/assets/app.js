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
  speech(lang, onFinal, onInterim, onEnd) {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) return null;
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
    rec.onerror = () => { if (onEnd) onEnd(); };
    try { rec.start(); } catch (e) { if (onEnd) onEnd(); }
    return rec;
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
