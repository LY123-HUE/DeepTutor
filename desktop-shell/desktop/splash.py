"""EduBuddy 登录门控 / 启动页（WorkBuddy 风格浅色版）。

同一个页面承担两种状态：

* **启动态**：本地服务拉起期间显示品牌吉祥物 + 轻量进度反馈；
* **登录门控态**：服务就绪但未登录时，页面停在「EduBuddy，我帮你 +
  黑色登录按钮」——与 WorkBuddy 桌面端的登录页同款交互。点击按钮用
  系统浏览器打开 Tokengine 平台完成注册/登录（PKCE + 本机回环回调），
  成功后由 Python 侧写入令牌与模型目录，再导航进应用。

退出登录时 Python 侧用 ``load_html()`` 把本页重新载入窗口，应用界面
随之消失（软件功能不可用），直到下一次登录成功。

状态来源：页面每 300ms 轮询 ``pywebview.api.status()``（phase/text），
每 1.2s 轮询 ``pywebview.api.auth_status()``（登录进行态，控制按钮）。
debug=True 时附加技术面板（工作区/端口/子进程日志），仅开发用。
"""
from __future__ import annotations

import base64
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
    --text:#1a1a1a; --muted:#8a8f98; --faint:#b8bcc4;
    --accent:#1db954; --error:#d93025; --line:#ececec;
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  html,body { height:100%; }
  body {
    font-family:"Segoe UI", "Microsoft YaHei", system-ui, -apple-system, sans-serif;
    color:var(--text);
    background:#fff;
    display:flex; flex-direction:column;
    align-items:center; justify-content:center;
    overflow:hidden; user-select:none;
  }
  .wrap { text-align:center; width:460px; padding:0 24px; }
  .logo {
    width:96px; height:96px; margin:0 auto 22px;
    animation:float 3.6s ease-in-out infinite;
  }
  .logo img { width:100%; height:100%; object-fit:contain; display:block; }
  @keyframes float { 0%,100%{transform:translateY(0)} 50%{transform:translateY(-6px)} }
  h1 { font-size:21px; font-weight:700; letter-spacing:.3px; color:var(--text); }
  /* --- 状态行：启动/等待/错误共用 --- */
  .status { margin-top:26px; font-size:13px; color:var(--muted); min-height:20px;
            transition:opacity .2s ease; }
  .status .dot { display:inline-block; width:7px; height:7px; border-radius:50%;
                 margin-right:7px; background:var(--accent);
                 animation:pulse 1.4s infinite; vertical-align:middle; }
  @keyframes pulse { 0%,100%{opacity:.3} 50%{opacity:1} }
  .status.error { color:var(--error); }
  .bar { margin:14px auto 0; width:160px; height:3px; border-radius:99px;
         background:#f0f1f3; overflow:hidden; }
  .bar i { display:block; height:100%; width:40%; border-radius:99px;
           background:var(--accent); animation:slide 1.2s ease-in-out infinite; }
  @keyframes slide { 0%{transform:translateX(-110%)} 100%{transform:translateX(320%)} }
  /* --- 登录门控：黑色胶囊按钮（与 WorkBuddy 登录页同款） --- */
  .login { display:none; margin-top:26px; }
  html.needlogin .login { display:block; }
  html.needlogin .bar { display:none; }
  #btnLogin {
    min-width:112px; height:36px; padding:0 34px;
    border:none; border-radius:999px; cursor:pointer;
    background:#111; color:#fff;
    font-size:14px; font-weight:600; letter-spacing:2px;
    font-family:inherit;
    transition:background .15s ease, opacity .15s ease, transform .05s ease;
  }
  #btnLogin:hover:not(:disabled) { background:#000; }
  #btnLogin:active:not(:disabled) { transform:scale(.98); }
  #btnLogin:disabled { opacity:.55; cursor:default; }
  .hint { margin-top:12px; font-size:12px; color:var(--muted); line-height:1.7; }
  .platlink { margin-top:6px; font-size:12px; }
  .platlink a { color:#555; cursor:pointer; text-decoration:none;
                border-bottom:1px solid #d8d8d8; padding-bottom:1px; }
  .platlink a:hover { color:#111; border-bottom-color:#999; }
  /* --- debug-only: technical detail panel + browser button --- */
  .detail { color:#6b7078; font-size:12px; margin-top:20px; line-height:1.6;
            font-family:Consolas, "Cascadia Mono", monospace; min-height:18px;
            white-space:pre-wrap; text-align:left;
            background:#f7f8f9; border:1px solid var(--line);
            border-radius:10px; padding:12px 14px; display:none; }
  html.debug .detail { display:block; }
  .btnrow { margin-top:18px; display:none; gap:10px; justify-content:center; }
  .btnrow.show { display:flex; }
  .btn {
    padding:8px 18px; border-radius:10px; cursor:pointer;
    font-size:13px; font-weight:500; font-family:inherit;
    border:1px solid var(--line); background:#fff; color:#333;
  }
  .btn:hover { background:#f5f5f6; }
  html:not(.debug) #btnBrowser { display:none; }
  .footer {
    position:fixed; left:0; right:0; bottom:18px;
    text-align:center; color:var(--faint); font-size:11px; letter-spacing:.2px;
  }
  .footer span { margin:0 6px; }
</style>
</head>
<body>
  <div class="wrap">
    <div class="logo"><img alt="EduBuddy" src="__LOGO_SRC__"/></div>
    <h1>EduBuddy，我帮你</h1>
    <div class="status" id="status"><span class="dot"></span>正在启动本地服务…</div>
    <div class="bar" id="bar"><i></i></div>
    <div class="login" id="login">
      <button id="btnLogin" type="button">登录</button>
      <div class="hint" id="hint">登录 Tokengine 账号后开始使用<br/>将打开浏览器完成注册 / 登录，模型与额度自动同步</div>
      <div class="platlink">没有账号？<a id="btnPlatform">打开 Tokengine 平台注册</a></div>
    </div>
    <div class="detail" id="detail"></div>
    <div class="btnrow" id="btnrow">
      <button class="btn" id="btnBrowser">在浏览器中打开</button>
      <button class="btn" id="btnQuit">退出</button>
    </div>
  </div>
  <div class="footer">EduBuddy 桌面端 v__VERSION__<span>·</span>本地运行，学习数据仅保存在本机</div>

<script>
  let phase = "boot";
  const errRe = /^(登录未完成|无法发起|启动失败)/;
  function esc(s) {
    return String(s).replace(/[&<>"']/g, c =>
      ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
  }
  function setPhase(p, text, detail) {
    phase = p;
    const el = document.getElementById("status");
    const isErr = p === "error" || (p === "login" && errRe.test(text || ""));
    if (p === "ready") {
      el.innerHTML = "<span class='dot'></span>" + esc(text || "服务已就绪");
    } else if (isErr) {
      el.innerHTML = esc(text || "出错了");
    } else {
      el.innerHTML = "<span class='dot'></span>" + esc(text || "");
    }
    el.classList.toggle("error", isErr);
    if (detail) { document.getElementById("detail").textContent = detail; }
    // 登录门控态：显示黑色登录按钮，隐藏进度条
    document.documentElement.classList.toggle("needlogin", p === "login");
    document.getElementById("btnrow").classList.toggle("show",
      p === "error" || (p === "login" && isErr) || htmlDebug());
    document.getElementById("bar").style.display =
      (p === "boot") ? "block" : "none";
  }
  function htmlDebug() {
    return document.documentElement.classList.contains("debug");
  }
  async function poll() {
    try {
      const s = await pywebview.api.status();
      if (s.phase !== phase || s.text) { setPhase(s.phase, s.text, s.detail); }
    } catch (e) { /* bridge not ready yet */ }
  }
  // 按钮态跟随真实登录进行态：失败后自动恢复可点，可反复重试
  async function renderAuth() {
    try {
      const a = await pywebview.api.auth_status();
      const btn = document.getElementById("btnLogin");
      if (!btn) return;
      if (a.in_progress) {
        btn.disabled = true;
        btn.textContent = "等待浏览器…";
        btn.style.letterSpacing = "0";
      } else {
        btn.disabled = false;
        btn.textContent = "登录";
        btn.style.letterSpacing = "2px";
      }
    } catch (e) {}
  }
  setInterval(poll, 300);
  setInterval(renderAuth, 1200);
  window.addEventListener("pywebviewready", () => { poll(); renderAuth(); });

  document.getElementById("btnLogin").addEventListener("click", async () => {
    const btn = document.getElementById("btnLogin");
    btn.disabled = true;
    btn.textContent = "正在打开浏览器…";
    btn.style.letterSpacing = "0";
    try {
      const r = await pywebview.api.login();
      if (!r.ok) {
        btn.disabled = false;
        btn.textContent = "登录";
        btn.style.letterSpacing = "2px";
        // 失败原因由 Python 侧 set_status 推过来（「无法发起：…」→ 红字）
      }
      // 成功则交给 renderAuth 轮询接管（显示「等待浏览器…」）
    } catch (e) {
      btn.disabled = false;
      btn.textContent = "登录";
      btn.style.letterSpacing = "2px";
    }
  });
  document.getElementById("btnPlatform").addEventListener("click",
    () => { pywebview.api.open_platform(); });
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


def splash_html(debug: bool = False, version: str = "", gate: bool = False) -> str:
    """Render the gate/splash page, optionally with the technical detail panel.

    ``gate=True``：页面直接以登录门控态渲染（隐藏进度条、显示黑色登录按钮），
    供退出登录后 ``load_html`` 回门控页时使用——避免先闪一下启动进度条。
    """
    cls = "debug" if debug else ""
    if gate:
        cls = (cls + " needlogin").strip()
    html = (
        SPLASH_HTML_TEMPLATE.replace("__DEBUG_CLASS__", cls)
        .replace("__LOGO_SRC__", _logo_data_uri())
        .replace("__VERSION__", version or "")
    )
    if gate:
        # 初始状态行直接给门控文案（首次轮询前不闪「正在启动本地服务…」）
        html = html.replace("正在启动本地服务…", "登录后开始使用")
    return html


__all__ = ["splash_html"]
