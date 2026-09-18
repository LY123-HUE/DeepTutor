
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
