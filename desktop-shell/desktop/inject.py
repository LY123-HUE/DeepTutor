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
import re
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
      // 已登录：点击切换账号下拉菜单（再点一次收起）；未登录：写事件槽发起 OAuth
      var t = window.__edubuddyMenuToggle;
      if (window.__edubuddyLoggedIn && typeof t === 'function') { t(); return; }
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

# 取走事件槽的值（读后即清空，天然去重）。
# 注意：POLL_TMPL 是**模板**，必须按槽位格式化后使用——
# 登录按钮用 POLL_JS（绑定登录槽），账号下拉菜单用 POLL_TMPL 格式化到菜单槽。
POLL_TMPL = """
(function () {
  var e = document.getElementById('%(evt)s');
  if (!e) { return ''; }
  var v = e.value;
  if (v) { e.value = ''; }
  return v;
})();
"""
POLL_JS = POLL_TMPL % {"evt": EVT_ID}

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


# --------------------------------------------------------------------------- #
# 账号下拉菜单（复用同一套「事件槽 + Python 轮询」通道）------------------------ #
# 已登录后点击左下角账号按钮，在其上方弹出自绘菜单（再点一次收起）；不再劫持
# 页面的右键菜单。点菜单项把 menu:<action>:<ts> 写进隐藏事件槽，Python 侧轮询
# 消费。Python 每轮 ensure 会把最新的菜单模型（按登录态渲染）推回页面。
# 危险项需要「再次点击」二次确认。
# --------------------------------------------------------------------------- #
MENU_EVT_ID = "edubuddy-menu-evt"
# 菜单事件槽轮询（读后即清空；模板在菜单槽位格式化后使用）
POLL_MENU_JS = POLL_TMPL % {"evt": MENU_EVT_ID}

# 菜单遍历会用到这些 action（与 Python 分发表对齐，便于一眼核对）
MENU_ACTIONS = ("switch", "platform", "refresh", "copy", "logout", "about")

MENU_ENSURE_JS = r"""
(function () {
  var EV = '__EVT__', BTN = '__BTN__', menu = null, toastEl = null, confirmTimer = null, state = null;
  var MENU_CSS = [
    'position:fixed','z-index:2147483001','min-width:192px','max-width:268px',
    'padding:6px','border-radius:10px','border:1px solid rgba(17,17,17,.12)',
    'background:#fff','color:#111',
    'font:13px/1.55 "Segoe UI","Microsoft YaHei",system-ui,sans-serif',
    'box-shadow:0 10px 30px rgba(0,0,0,.18)','display:none','user-select:none'
  ].join(';');

  function slot() {
    var e = document.getElementById(EV);
    if (!e) {
      e = document.createElement('input');
      e.id = EV; e.type = 'hidden'; e.value = '';
      document.body.appendChild(e);
    }
    return e;
  }
  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function headerHtml(st) {
    var h = (st && st.header) || {}, t = h.title || 'EduBuddy', s = h.sub || '';
    var html = '<div class="edubuddy-menu-head" style="border-bottom:1px solid rgba(17,17,17,.08);' +
               'padding:4px 10px 8px;margin-bottom:6px">';
    html += '<div style="font-weight:600;font-size:13px">' + escapeHtml(t) + '</div>';
    if (s) html += '<div class="edubuddy-menu-sub" style="font-size:11px;color:#8a8f98;margin-top:2px">' + escapeHtml(s) + '</div>';
    return html + '</div>';
  }
  function render() {
    if (!menu) return;
    var st = state || { items: [] }, html = headerHtml(st);
    (st.items || []).forEach(function (it) {
      if (it && it.type === 'sep') {
        html += '<div style="height:1px;background:rgba(17,17,17,.08);margin:4px 8px"></div>';
        return;
      }
      if (!it || !it.action) return;
      var danger = it.danger ? ';color:#d93025' : '';
      html += '<div data-action="' + it.action + '" data-confirm="' + escapeHtml(it.confirm || '') + '"'
            + ' style="padding:7px 12px;border-radius:7px;cursor:pointer;white-space:nowrap;'
            + 'overflow:hidden;text-overflow:ellipsis' + danger + '">'
            + escapeHtml(it.label) + '</div>';
    });
    if (!(st.items || []).length) {
      html += '<div style="padding:8px 12px;color:#8a8f98;font-size:12px">暂无可用操作</div>';
    }
    menu.innerHTML = html;
    menu.querySelectorAll('[data-action]').forEach(function (d) {
      d.onmouseenter = function () { d.style.background = 'rgba(17,17,17,.06)'; };
      d.onmouseleave = function () { d.style.background = 'rgba(17,17,17,0)'; };
      d.onclick = function () {
        var a = d.getAttribute('data-action');
        var cf = d.getAttribute('data-confirm');
        if (cf) {
          if (d.__arm) { emit(a); close(); d.__arm = false; return; }
          d.__arm = true;
          var orig = d.textContent;
          d.textContent = '\u518d\u6b21\u70b9\u51fb\u4ee5\u786e\u8ba4';
          d.style.background = 'rgba(217,48,37,.10)'; d.style.color = '#d93025';
          if (confirmTimer) clearTimeout(confirmTimer);
          confirmTimer = setTimeout(function () {
            d.__arm = false; d.textContent = orig;
            d.style.background = 'rgba(17,17,17,0)'; d.style.color = '';
            confirmTimer = null;
          }, 3000);
          return;
        }
        emit(a); close();
      };
    });
  }
  function emit(action) { slot().value = 'menu:' + action + ':' + Date.now(); }
  function button() { return document.getElementById(BTN); }
  function close() {
    if (menu) menu.style.display = 'none';
    if (confirmTimer) { clearTimeout(confirmTimer); confirmTimer = null; }
    document.removeEventListener('mousedown', onDocDown, true);
  }
  function onDocDown(e) {
    // 菜单内点击不关；点登录按钮本身交给按钮 onclick 做切换；其余关闭
    if (!menu || menu.style.display !== 'block') return;
    if (menu.contains(e.target)) return;
    var b = button();
    if (b && (e.target === b || (b.contains && b.contains(e.target)))) return;
    close();
  }
  // 在登录按钮上方打开下拉（右缘对齐按钮，避免超出右边界）
  function openAtButton() {
    var b = button();
    render();
    menu.style.display = 'block';
    var rect = b ? b.getBoundingClientRect() : { right: 296, top: window.innerHeight - 37 };
    var w = menu.offsetWidth, h = menu.offsetHeight;
    var left = Math.max(8, Math.min(rect.right - w, window.innerWidth - w - 8));
    var top = Math.max(8, rect.top - h - 6);
    menu.style.left = left + 'px'; menu.style.top = top + 'px';
    setTimeout(function () { document.addEventListener('mousedown', onDocDown, true); }, 0);
  }
  function toggle() {
    if (!menu && !ensureEls()) return 'no-body';
    if (menu.style.display === 'block') { close(); return 'closed'; }
    render(); openAtButton(); return 'open';
  }
  function ensureEls() {
    if (!document.body) return false;
    slot();
    if (!menu) {
      menu = document.createElement('div');
      menu.id = 'edubuddy-menu';
      menu.style.cssText = MENU_CSS;
      document.body.appendChild(menu);
    }
    if (!toastEl) {
      toastEl = document.createElement('div');
      toastEl.id = 'edubuddy-toast';
      toastEl.style.cssText = 'position:fixed;left:50%;bottom:36px;transform:translateX(-50%);' +
        'z-index:2147483002;background:rgba(17,17,17,.88);color:#fff;' +
        'font:12px/1.5 "Segoe UI","Microsoft YaHei",system-ui,sans-serif;' +
        'padding:8px 16px;border-radius:16px;display:none;max-width:70%;text-align:center;' +
        'box-shadow:0 4px 16px rgba(0,0,0,.20)';
      document.body.appendChild(toastEl);
    }
    return true;
  }
  function ensureEvents() {
    if (window.__edubuddyMenuInit) return;
    // 不劫持右键——保留页面原生右键菜单；菜单只由登录按钮点击切换
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') close(); }, true);
    document.addEventListener('scroll', close, true);
    document.addEventListener('wheel', close, true);
    window.addEventListener('blur', close);
    // 窗口尺寸变化（最大化/还原/拖边框）会让 position:fixed 的菜单坐标失效
    //（实测：开着菜单点最大化，菜单悬在半空错位）。策略从简：任何这类
    // 「其他交互」都直接收起，等待下次点击按钮在正确位置重新展开。
    window.addEventListener('resize', close);
    window.__edubuddyMenuInit = true;
  }
  window.__edubuddyMenuToggle = toggle;
  window.__edubuddyMenuUpdate = function (s) {
    state = s;
    // 登录态决定按钮点击路由：已登录 -> 切换菜单；未登录 -> 发起登录。
    // 只有 logged_in 算登录：configured（本机残留令牌）不算——退出登录后
    // 点按钮必须直接重新发起登录，绝不能再弹出菜单。
    window.__edubuddyLoggedIn = !!(s && s.logged_in);
    if (!menu) return;
    if (s && !s.logged_in && menu.style.display === 'block') {
      close();                    // 退出登录瞬间菜单还开着：立即收起
    } else if (menu.style.display === 'block') {
      render();
    }
  };
  window.__edubuddyToast = function (msg) {
    if (!toastEl) return;
    toastEl.textContent = String(msg == null ? '' : msg);
    toastEl.style.display = 'block';
    clearTimeout(toastEl.__t);
    toastEl.__t = setTimeout(function () { toastEl.style.display = 'none'; }, 2400);
  };
  if (!ensureEls()) return 'no-body';
  ensureEvents();
  return 'ok';
})();
""".replace("__EVT__", MENU_EVT_ID).replace("__BTN__", BTN_ID)


def _menu_update_js(model: dict) -> str:
    """把菜单模型推给页面（浅色主题自绘菜单）。JSON 即合法 JS 字面量。"""
    return "window.__edubuddyMenuUpdate(%s)" % json.dumps(model, ensure_ascii=False)


def _mask_phone(phone: str) -> str:
    p = str(phone or "")
    if len(p) >= 7:
        return p[:3] + "****" + p[-4:]
    return p or "已登录"


def _display_name(acct: dict) -> str:
    """账号显示名：优先平台用户名（username），回退脱敏手机号。

    平台 userinfo 返回 username 字段（实测如 ``admin``），落库在
    ``account.raw``，经 AuthManager.status() 透出到 ``account.username``。
    若用户名本身就是未脱敏的 11 位手机号（有的平台这么存），强制打码，
    避免按钮上裸奔完整号码。
    """
    acct = acct or {}
    name = str(acct.get("username") or "").strip()
    if re.fullmatch(r"1\d{10}", name):
        name = _mask_phone(name)
    if not name:
        name = _mask_phone(acct.get("phone"))
    return name


def _identity_of(st: dict) -> str:
    """账号身份签名：用户名/手机号/中继域名任一变化即视为换了账号。

    用于检测「已登录状态下切换账号」——旧逻辑只在 未登录→已登录 跃迁时
    刷新页面，切换账号完成后应用仍显示旧账号/旧模型，看起来像没生效。
    刻意不含模型数量：菜单里的「刷新可用模型」自己会刷新页面，别重复。
    """
    acct = st.get("account") or {}
    return "|".join([
        str(acct.get("username") or ""),
        str(acct.get("phone") or ""),
        str(st.get("relay_base") or ""),
    ])


def _fmt_balance(bal) -> str:
    """把平台返回的余额规整成两位小数金额文本（不含货币符号）。

    平台 userinfo 的 balance 是**成品展示字符串**（实测为 ``'¥16.769472 额度'``），
    也可能给纯数值。若直接拼接会得到「余额 ¥¥16.769472 额度」这种双重格式。
    这里统一只抽数字部分、保留两位小数，货币符号由客户端自己控制。
    抽不出数字时返回空串（调用方跳过余额展示）。
    """
    text = str(bal if bal is not None else "").strip()
    if not text:
        return ""
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    if not m:
        return ""
    try:
        return f"{float(m.group()):,.2f}"
    except ValueError:  # pragma: no cover  正则已保证是数字
        return ""


def _menu_model(status: dict) -> dict:
    """按登录态渲染两套下拉菜单；文案实时取自 AuthManager.status()。

    顶层 ``logged_in`` / ``configured`` 标志供页面侧决定按钮点击路由：
    已登录 -> 切换菜单；未登录 -> 发起登录。
    """
    acct = status.get("account") or {}
    logged = bool(status.get("logged_in"))
    configured = bool(status.get("configured"))
    base = {"logged_in": logged, "configured": configured}

    if logged:
        models = acct.get("models") or []
        parts = []
        amount = _fmt_balance(acct.get("balance"))
        if amount:
            parts.append(f"余额 ¥{amount}")
        parts.append(f"{len(models)} 个模型")
        return {
            **base,
            "header": {"title": _display_name(acct),
                       "sub": " · ".join(parts)},
            "items": [
                {"action": "refresh", "label": "刷新可用模型"},
                {"type": "sep"},
                {"action": "platform", "label": "打开 Tokengine 平台"},
                {"action": "switch", "label": "切换账号"},
                {"type": "sep"},
                {"action": "logout", "label": "退出登录", "danger": True,
                 "confirm": "再次点击以确认退出登录？"},
                {"type": "sep"},
                {"action": "about", "label": "关于 EduBuddy"},
            ],
        }
    if configured:
        return {
            **base,
            "header": {"title": "已配置令牌", "sub": "本机已配置令牌，可切换账号"},
            "items": [
                {"action": "switch", "label": "登录 / 切换账号"},
                {"action": "platform", "label": "打开 Tokengine 平台"},
                {"type": "sep"},
                {"action": "about", "label": "关于 EduBuddy"},
            ],
        }
    if status.get("in_progress"):
        return {
            **base,
            "header": {"title": "等待浏览器完成…", "sub": "请在浏览器中完成登录与授权"},
            "items": [
                {"action": "switch", "label": "重新发起登录"},
                {"action": "platform", "label": "打开 Tokengine 平台"},
                {"type": "sep"},
                {"action": "about", "label": "关于 EduBuddy"},
            ],
        }
    return {
        **base,
        "header": {"title": "EduBuddy", "sub": "未登录 Tokengine"},
        "items": [
            {"action": "switch", "label": "登录 Tokengine"},
            {"action": "platform", "label": "打开 Tokengine 平台"},
            {"type": "sep"},
            {"action": "about", "label": "关于 EduBuddy"},
        ],
    }


class LoginButtonInjector:
    """在页面里维持「登录/账号」按钮 + 按登录态自绘的下拉菜单。

    参数
      window : pywebview Window
      on_login : 无参可调用；登录按钮被点击时触发（内部应发起 OAuth 并开浏览器）
      status_of : 无参可调用，返回 AuthManager.status() 的字典，用于刷新按钮/菜单
      on_authenticated : 可选；检测到「未登录 -> 已登录」跃迁时触发（用来刷新页面）
      menu_actions : 可选；{动作名: 无参可调} 分发表，供下拉菜单项分发。
         可选键见 MENU_ACTIONS：switch / platform / refresh / copy / logout / about
      on_toast : 可选；msg -> None，在页面显示一条底部轻提示（操作反馈用）
    """

    def __init__(self, window, on_login, status_of, on_authenticated=None,
                 expect_url: str | None = None,
                 interval: float = 0.4, ensure_every: float = 2.0,
                 menu_actions: dict | None = None,
                 on_toast=None) -> None:
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
        self._last_pushed_logged: bool | None = None
        self._menu_actions = dict(menu_actions or {})
        self._on_toast = on_toast
        self._last_identity: str | None = None
        self.click_count = 0
        self.menu_count = 0

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
        """后台线程：等应用页 -> 装按钮/右键菜单 -> 轮询事件 -> 同步文案/菜单。"""
        last_ensure = 0.0
        installed = False
        while not self._stop.is_set():
            if not self._on_app_page():
                self._stop.wait(self._interval)
                continue
            now = time.monotonic()
            try:
                if not installed or (now - last_ensure) >= self._ensure_every:
                    # 按钮 + 右键菜单一起装；装完顺带把最新菜单模型推回去
                    # （SPA 路由切换/整页刷新会清掉 JS 状态，靠定时器补回）
                    btn_state = self._w.evaluate_js(ENSURE_JS)
                    self._w.evaluate_js(MENU_ENSURE_JS)
                    if btn_state == "created":
                        # 按钮被重建（页面刷新/路由重建 DOM）——ENSURE_JS 里写死了
                        # 初始文案「登录」，若不同步重推，已登录用户会一直看到
                        # 「登录」。清掉缓存强制下方的文案同步重新执行。
                        self._last_label = None
                    self._push_menu()
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

            # 右键菜单动作轮询（读后即清空，与登录按钮同一套通道）
            try:
                mevt = self._w.evaluate_js(POLL_MENU_JS)
                if mevt:
                    parts = mevt.split(":")
                    action = parts[1] if len(parts) > 1 else parts[0]
                    self.menu_count += 1
                    log.info("右键菜单动作（第 %d 次）：%s", self.menu_count, mevt)
                    threading.Thread(
                        target=self._dispatch_menu, args=(action,), daemon=True
                    ).start()
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
                # 登录态翻转时立即重推菜单模型（不等 ensure 周期）：退出登录
                # 后页面里 __edubuddyLoggedIn 必须尽快归假，否则空窗期内点
                # 按钮仍会弹出退出前的旧菜单。
                logged_now = bool(st.get("logged_in"))
                if logged_now is not self._last_pushed_logged:
                    self._push_menu()
                    self._last_pushed_logged = logged_now
                identity = _identity_of(st)
                if self._was_logged_in is False and logged:
                    log.info("检测到登录成功，刷新页面以载入新模型")
                    self._fire_auth()
                elif (logged and identity and self._last_identity
                      and identity != self._last_identity):
                    # 已登录状态下身份变化（切换账号/换绑域名）：同样要刷新，
                    # 否则应用一直显示旧账号的数据，切换看起来像没生效。
                    log.info("检测到账号身份变化，刷新页面以载入新账号数据")
                    self._fire_auth()
                self._last_identity = identity
                self._was_logged_in = logged
            except Exception:  # noqa: BLE001
                pass

            self._stop.wait(self._interval)

    # ---- 内部 ---------------------------------------------------------- #
    def _push_menu(self) -> None:
        """按当前登录态把菜单模型推回页面（自绘菜单据此渲染）。"""
        try:
            self._w.evaluate_js(_menu_update_js(_menu_model(self._status_of())))
        except Exception:  # noqa: BLE001
            pass

    def _dispatch_menu(self, action: str) -> None:
        handler = self._menu_actions.get(action)
        if handler is None:
            log.warning("未知/未注册的菜单动作：%s（可选：%s）",
                        action, ", ".join(MENU_ACTIONS))
            return
        try:
            handler()
        except Exception:  # noqa: BLE001
            log.exception("菜单动作 %s 执行失败", action)

    def _safe_login(self) -> None:
        try:
            self._on_login()
        except Exception:  # noqa: BLE001
            log.exception("发起登录失败")

    def _fire_auth(self) -> None:
        """触发「登录态/账号变化」回调（刷新页面载入新数据），绝不抛出。"""
        if not self._on_auth:
            return
        try:
            self._on_auth()
        except Exception:  # noqa: BLE001
            log.exception("on_authenticated 回调执行失败")

    @staticmethod
    def _label_for(st: dict) -> tuple[str, str]:
        acct = st.get("account") or {}
        models = acct.get("models") or []
        if st.get("logged_in"):
            # 按钮显示用户名（平台 username，回退脱敏手机号），登录态一眼可辨。
            return _display_name(acct), \
                f"Tokengine 账号已连接 · 可用模型 {len(models)} 个"
        if st.get("configured"):
            # configured 不再弹菜单（菜单是登录态专属）：点击直接发起登录
            return "已配置令牌", "本机已配置令牌，点击登录账号"
        if st.get("in_progress"):
            return "等待浏览器…", "请在浏览器中完成登录与授权"
        return "登录", "登录 Tokengine 账号，自动装载令牌与可用模型"
