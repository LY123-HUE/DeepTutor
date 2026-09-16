"""Embedded splash page shown while EduBuddy boots.

The splash is a local page served inside the WebView until the frontend on
127.0.0.1:3782 answers, then the window navigates to the real app. Status is
polled from the Python bridge via ``pywebview.api.status()``.

The page is intentionally *clean* for end users: it shows only a branded
loader and a friendly status line — no ports, no paths, no backend logs.
When ``debug=True`` the same page additionally renders a technical detail
panel (workspace, ports, live subprocess output) plus an "open in browser"
button, which is handy while developing.

Branding: the center logo is the app icon (assets/icon.png). The raw PNG is
inlined as a base64 data-URI so the splash is a self-contained page and the
real mascot (not a text monogram) shows during boot.
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

SPLASH_HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN" class="__DEBUG_CLASS__">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>EduBuddy</title>
<style>
  :root {
    --bg1:#071b12; --bg2:#0d2f1f; --accent:#1cc859; --accent2:#4ade80;
    --text:#eef5ef; --muted:#9fc4ab;
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  html,body { height:100%; }
  body {
    font-family:"Segoe UI", "Microsoft YaHei", system-ui, -apple-system, sans-serif;
    color:var(--text);
    background:radial-gradient(1200px 800px at 20% -10%, #164a2c 0%, transparent 55%),
               radial-gradient(1000px 700px at 110% 110%, #0a3320 0%, transparent 50%),
               linear-gradient(160deg, var(--bg1), var(--bg2));
    display:flex; align-items:center; justify-content:center;
    overflow:hidden; user-select:none;
  }
  .wrap { text-align:center; width:420px; padding:0 24px; }
  .logo {
    width:88px; height:88px; margin:0 auto 20px;
    border-radius:22px;
    background:#fff;
    display:flex; align-items:center; justify-content:center;
    overflow:hidden;
    box-shadow:0 18px 48px rgba(0,0,0,.35);
    animation:float 3.6s ease-in-out infinite;
  }
  .logo img { width:100%; height:100%; object-fit:cover; display:block; }
  @keyframes float { 0%,100%{transform:translateY(0)} 50%{transform:translateY(-7px)} }
  h1 { font-size:25px; font-weight:700; letter-spacing:.5px; }
  h1 span { background:linear-gradient(90deg,var(--accent),var(--accent2));
            -webkit-background-clip:text; background-clip:text; color:transparent; }
  .tag { color:var(--muted); margin-top:8px; font-size:13px; }
  .status { margin-top:40px; font-size:14.5px; min-height:22px; }
  .dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:8px;
         background:var(--accent2); animation:pulse 1.4s infinite; vertical-align:middle;}
  @keyframes pulse { 0%,100%{opacity:.35} 50%{opacity:1} }
  .bar { margin:16px auto 0; width:180px; height:4px; border-radius:99px;
         background:rgba(255,255,255,.10); overflow:hidden; }
  .bar i { display:block; height:100%; width:42%; border-radius:99px;
           background:linear-gradient(90deg,var(--accent),var(--accent2));
           animation:slide 1.2s ease-in-out infinite; }
  @keyframes slide { 0%{transform:translateX(-110%)} 100%{transform:translateX(320%)} }
  /* --- debug-only: technical detail panel + browser button --- */
  .detail { color:var(--muted); font-size:12.5px; margin-top:22px; line-height:1.6;
            font-family:Consolas, "Cascadia Mono", monospace; min-height:18px;
            white-space:pre-wrap; text-align:left;
            background:rgba(0,0,0,.25); border:1px solid rgba(255,255,255,.07);
            border-radius:12px; padding:12px 14px; display:none; }
  html.debug .detail { display:block; }
  .btnrow { margin-top:22px; display:none; gap:10px; justify-content:center; }
  .btnrow.show { display:flex; }
  .btn.btnLogin { min-width:190px; }
  html:not(.needlogin) #btnLogin,
  html:not(.needlogin) #btnSkip { display:none; }
  .account { margin-top:16px; font-size:12.5px; color:var(--muted); display:none;
             background:rgba(255,255,255,.05); border:1px solid rgba(255,255,255,.08);
             border-radius:10px; padding:8px 12px; }
  .account b { color:var(--accent2); font-weight:600; }
  .btn {
    padding:10px 18px; border-radius:12px; border:none; cursor:pointer;
    font-size:14px; font-weight:600; color:#fff;
    background:linear-gradient(135deg,var(--accent), #16a34a);
    box-shadow:0 8px 22px rgba(22,163,74,.4);
  }
  .btn.ghost { background:rgba(255,255,255,.08); color:var(--text); box-shadow:none; }
  .btn:hover { filter:brightness(1.08); }
  html:not(.debug) #btnBrowser { display:none; }
  .error { color:#ff8f9b; }
  .footer { margin-top:34px; color:var(--muted); font-size:11.5px; opacity:.75; }
</style>
</head>
<body>
  <div class="wrap">
    <div class="logo"><img alt="EduBuddy" src="__LOGO_SRC__"/></div>
    <h1>Edu<span>Buddy</span></h1>
    <p class="tag">本地运行 · Agent-native 个性化学习工作区</p>
    <div class="status" id="status"><span class="dot"></span>正在启动本地服务…</div>
    <div class="bar"><i></i></div>
    <div class="account" id="account"></div>
    <div class="detail" id="detail"></div>
    <div class="btnrow" id="btnrow">
      <button class="btn btnLogin" id="btnLogin">登录 Tokengine 账号</button>
      <button class="btn ghost" id="btnSkip">稍后再说</button>
      <button class="btn" id="btnBrowser">在浏览器中打开</button>
      <button class="btn ghost" id="btnQuit">退出</button>
    </div>
    <div class="footer">本地运行 · 数据仅保存在本机 · 可离线使用</div>
  </div>

<script>
  let phase = "booting";
  function setPhase(p, text, detail) {
    phase = p;
    // 登录阶段的失败文案（登录未完成 / 无法发起）用错误色，且不要再转菊花
    const loginErr = p === "login" && /^(登录未完成|无法发起)/.test(text || "");
    document.getElementById("status").innerHTML =
      loginErr ? ("<span class='error'>" + text + "</span>")
        : (p === "boot" || p === "login")
          ? "<span class='dot'></span>" + text
          : p === "error" ? ("<span class='error'>"+text+"</span>")
          : "<span class='dot' style='background:#4ade80'>" + text;
    if (detail) { document.getElementById("detail").textContent = detail; }
    document.getElementById("btnrow").classList.toggle("show",
      p === "error" || p === "ready" || p === "login");
    document.documentElement.classList.toggle("needlogin", p === "login");
  }
  async function poll() {
    try {
      const s = await pywebview.api.status();
      if (s.phase !== phase) { setPhase(s.phase, s.text, s.detail); }
      if (s.detail) { document.getElementById("detail").textContent = s.detail; }
    } catch (e) { /* bridge not ready yet */ }
  }
  function esc(s) {
    return String(s).replace(/[&<>"']/g, c =>
      ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
  }
  function relayHost(u) {
    if (!u) return "";
    try { return new URL(u).host; }
    catch (e) { return String(u).split("//").pop().split("/")[0]; }
  }
  async function renderAuth() {
    try {
      const a = await pywebview.api.auth_status();
      const acct = a.account || {};
      const models = acct.models || [];
      const el = document.getElementById("account");
      if (a.logged_in) {
        const bits = [];
        if (models.length) bits.push(models.length + " 个模型");
        const host = relayHost(a.relay_base);
        if (host) bits.push(host);
        const tail = bits.length
          ? "<span style='opacity:.7'>　· " + bits.map(esc).join("　· ") + "</span>" : "";
        el.innerHTML = "已登录：<b>" + (acct.phone ? esc(acct.phone) : "账号") + "</b>" + tail;
        if (models.length) el.title = "可用模型：" + models.join("、");
        el.style.display = "block";
      } else if (a.configured) {
        el.textContent = "已配置本机令牌（可在设置里切换账号）";
        el.style.display = "block";
      } else {
        el.style.display = "none";
      }
      // 按钮状态跟随真实的登录进行态：失败后自动恢复可点，可反复重试
      const btn = document.getElementById("btnLogin");
      if (btn) {
        btn.disabled = !!a.in_progress;
        btn.textContent = a.in_progress ? "等待浏览器完成登录…" : "登录 Tokengine 账号";
      }
    } catch (e) {}
  }
  setInterval(poll, 300);
  setInterval(renderAuth, 1500);
  window.addEventListener("pywebviewready", () => { poll(); renderAuth(); });

  document.getElementById("btnLogin").addEventListener("click", async () => {
    const btn = document.getElementById("btnLogin");
    btn.disabled = true;
    btn.textContent = "正在打开浏览器…";
    try {
      const r = await pywebview.api.login();
      if (!r.ok) {
        setPhase("login", "无法发起登录", r.detail || r.error || "");
        btn.disabled = false;
        btn.textContent = "登录 Tokengine 账号";
      }
      // 成功则交给 renderAuth 的轮询接管按钮状态（显示「等待浏览器完成登录…」）
    } catch (e) {
      btn.disabled = false;
      btn.textContent = "登录 Tokengine 账号";
    }
  });
  document.getElementById("btnSkip").addEventListener("click", () => {
    pywebview.api.skip_login();
  });
  document.getElementById("btnBrowser").addEventListener("click",
    () => { pywebview.api.open_browser(); });
  document.getElementById("btnQuit").addEventListener("click",
    () => { pywebview.api.quit(); });
</script>
</body>
</html>
"""


def _asset_path(name: str) -> Path:
    """Resolve a bundled asset. PyInstaller/embed EXE unpacks to sys._MEIPASS;
    in dev the file sits under the project assets/ dir."""
    if getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None):
        p = Path(sys._MEIPASS) / "assets" / name
        if p.exists():
            return p
    # Dev / raw source tree
    p = Path(__file__).resolve().parents[1] / "assets" / name
    return p


def _logo_data_uri() -> str:
    """Read the app icon PNG and return it as a data: URI (best-effort)."""
    path = _asset_path("icon.png")
    try:
        if not path.exists():
            return ""
        with path.open("rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        return "data:image/png;base64," + b64
    except OSError:
        return ""


def splash_html(debug: bool = False) -> str:
    """Render the splash page, optionally with the technical detail panel."""
    return (
        SPLASH_HTML_TEMPLATE.replace("__DEBUG_CLASS__", "debug" if debug else "")
        .replace("__LOGO_SRC__", _logo_data_uri())
    )


__all__ = ["splash_html"]

