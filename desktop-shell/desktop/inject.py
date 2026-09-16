"""把「登录 Tokengine」按钮注入已加载的 DeepTutor 页面。

设计取舍（为什么不用 pywebview 的 JS 桥直接回调）
--------------------------------------------------
`js_api` 桥在窗口 load_url() 到外站页面后是否仍被注入，取决于 pywebview
版本与注入时机，跨版本不稳（本次调试就卡在这个不确定性上）。
这里改用一个笨但绝对可靠的通道：

    页面侧  —— 按钮点击时把事件写进一个隐藏 input（当事件队列）
    Python 侧 —— 用 window.evaluate_js() 轮询该 input 的 value 并消费

evaluate_js 是 pywebview 最底层的能力，不依赖 js_api 注入，已在
tools/_tmp_verify/probe_js_bridge.py 里单独验证过。
"""
from __future__ import annotations

import json
import logging
import threading
import time

log = logging.getLogger("dt.inject")

BTN_ID = "edubuddy-login-btn"
EVT_ID = "edubuddy-login-evt"

# 注入/自愈脚本：创建按钮 + 隐藏事件槽，并每 1s 自检一次（SPA 路由切换会
# 重建 DOM，靠这个定时器把按钮补回来）。返回字符串便于调试。
ENSURE_JS = """
(function () {
  var ID = '%(btn)s', EV = '%(evt)s';
  function slot() {
    var e = document.getElementById(EV);
    if (!e) {
      e = document.createElement('input');
      e.id = EV; e.type = 'hidden'; e.value = '';
      document.body.appendChild(e);
    }
    return e;
  }
  function ensure() {
    if (!document.body) { return 'no-body'; }
    slot();
    if (document.getElementById(ID)) { return 'exists'; }
    var b = document.createElement('button');
    b.id = ID; b.type = 'button';
    b.textContent = '登录';
    b.title = '登录 Tokengine 账号';
    b.style.cssText = [
      'position:fixed', 'left:104px', 'bottom:11px',
      'z-index:2147483000',
      'height:26px', 'padding:0 13px', 'border-radius:13px',
      'border:1px solid rgba(17,17,17,.14)', 'background:#fff',
      'color:#111', 'cursor:pointer',
      'font:500 12px/1 "Segoe UI","Microsoft YaHei",system-ui,sans-serif',
      'box-shadow:0 1px 3px rgba(0,0,0,.10)',
      'transition:box-shadow .15s ease, opacity .15s ease'
    ].join(';');
    b.onmouseenter = function () { b.style.boxShadow = '0 2px 9px rgba(0,0,0,.18)'; };
    b.onmouseleave = function () { b.style.boxShadow = '0 1px 3px rgba(0,0,0,.10)'; };
    b.onclick = function () {
      // 写事件槽即可：Python 侧轮询到就发起 OAuth 并打开浏览器
      var e = document.getElementById(EV);
      if (e) { e.value = 'login:' + Date.now(); }
      b.disabled = true; b.style.opacity = '.55';
      setTimeout(function () { b.disabled = false; b.style.opacity = '1'; }, 2000);
    };
    document.body.appendChild(b);
    return 'created';
  }
  window.__edubuddyEnsure = ensure;
  var r = ensure();
  if (!window.__edubuddyTimer) { window.__edubuddyTimer = setInterval(ensure, 1000); }
  return r;
})();
""" % {"btn": BTN_ID, "evt": EVT_ID}

# 取走事件槽的值（读后即清空，天然去重）
POLL_JS = """
(function () {
  var e = document.getElementById('%(evt)s');
  if (!e) { return ''; }
  var v = e.value;
  if (v) { e.value = ''; }
  return v;
})();
""" % {"evt": EVT_ID}

# 更新按钮文案（Python 侧把状态推回页面）
LABEL_JS = """
(function () {
  var b = document.getElementById('%(btn)s');
  if (!b) { return 'no-btn'; }
  b.textContent = %(label)s;
  b.title = %(title)s;
  return 'ok';
})();
""" % {"btn": BTN_ID, "label": "%(label)s", "title": "%(title)s"}


def _label_js(label: str, title: str) -> str:
    return LABEL_JS % {
        "label": json.dumps(label, ensure_ascii=False),
        "title": json.dumps(title, ensure_ascii=False),
    }


class LoginButtonInjector:
    """在页面里维持一个「登录」按钮，并把点击事件转成 Python 侧动作。

    参数
      window : pywebview Window
      on_login : 无参可调用；按钮被点击时触发（内部应发起 OAuth 并开浏览器）
      status_of : 无参可调用，返回 AuthManager.status() 的字典，用于刷新按钮文案
      on_authenticated : 可选；检测到「未登录 -> 已登录」跃迁时触发（用来刷新页面）
    """

    def __init__(self, window, on_login, status_of, on_authenticated=None,
                 expect_url: str | None = None,
                 interval: float = 0.4, ensure_every: float = 2.0) -> None:
        self._w = window
        self._on_login = on_login
        self._status_of = status_of
        self._on_auth = on_authenticated
        self._expect_url = expect_url
        self._interval = interval
        self._ensure_every = ensure_every
        self._stop = threading.Event()
        self._last_label: str | None = None
        self._was_logged_in: bool | None = None
        self.click_count = 0

    # ---- 对外 ---------------------------------------------------------- #
    def stop(self) -> None:
        self._stop.set()

    def _on_app_page(self) -> bool:
        """只在应用页面（而非启动页）里注入按钮。"""
        if not self._expect_url:
            return True
        try:
            cur = self._w.get_current_url() or ""
        except Exception:  # noqa: BLE001
            return False
        return cur.startswith(self._expect_url)

    def run(self) -> None:
        """在后台线程里跑：等应用页 -> 装按钮 -> 轮询事件 -> 同步文案。"""
        last_ensure = 0.0
        installed = False
        while not self._stop.is_set():
            if not self._on_app_page():
                self._stop.wait(self._interval)
                continue
            now = time.monotonic()
            try:
                if not installed or (now - last_ensure) >= self._ensure_every:
                    self._w.evaluate_js(ENSURE_JS)
                    last_ensure = now
                    installed = True
            except Exception:  # noqa: BLE001  页面还没加载完，下一轮再试
                installed = False
                self._stop.wait(self._interval)
                continue

            try:
                evt = self._w.evaluate_js(POLL_JS)
                if evt:
                    self.click_count += 1
                    log.info("页面按钮被点击（第 %d 次）：%s", self.click_count, evt)
                    threading.Thread(target=self._safe_login, daemon=True).start()
            except Exception:  # noqa: BLE001
                pass

            # 文案与登录态同步
            try:
                st = self._status_of()
                logged = bool(st.get("logged_in")) or bool(st.get("configured"))
                label, title = self._label_for(st)
                if label != self._last_label:
                    self._w.evaluate_js(_label_js(label, title))
                    self._last_label = label
                if self._was_logged_in is False and logged:
                    log.info("检测到登录成功，刷新页面以载入新模型")
                    if self._on_auth:
                        try:
                            self._on_auth()
                        except Exception:  # noqa: BLE001
                            pass
                self._was_logged_in = logged
            except Exception:  # noqa: BLE001
                pass

            self._stop.wait(self._interval)

    # ---- 内部 ---------------------------------------------------------- #
    def _safe_login(self) -> None:
        try:
            self._on_login()
        except Exception:  # noqa: BLE001
            log.exception("发起登录失败")

    @staticmethod
    def _label_for(st: dict) -> tuple[str, str]:
        acct = st.get("account") or {}
        phone = acct.get("phone") or ""
        models = acct.get("models") or []
        if st.get("logged_in"):
            tail = phone[-4:] if len(phone) >= 4 else phone
            return (f"已登录 {tail}" if tail else "已登录",
                    f"Tokengine 账号已连接 · 可用模型 {len(models)} 个")
        if st.get("configured"):
            return "已配置令牌", "本机已配置令牌，点击可切换账号"
        if st.get("in_progress"):
            return "等待浏览器…", "请在浏览器中完成登录与授权"
        return "登录", "登录 Tokengine 账号，自动装载令牌与可用模型"
