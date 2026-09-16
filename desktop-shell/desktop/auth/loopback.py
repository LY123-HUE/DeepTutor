"""本地回环回调服务器（RFC 8252 loopback redirect）。

监听 127.0.0.1 上的一个端口（默认由系统随机分配），只接受平台 302 到
``CALLBACK_PATH`` 的回调；校验 state 后把授权码投递给等待方，
然后返回一张面向用户的"登录成功/失败"页并停止。

健壮性要点：
* 只响应 ``CALLBACK_PATH``，其余路径（favicon、浏览器预取、端口扫描）一律
  204 忽略——否则一个杂散请求就会把登录结果覆写成 state_mismatch。
* 已有结果后重复回调直接忽略，避免 ""拉链式"" 覆盖。
* 平台的 ``error_description`` 原样保留并渲染到失败页，排查时不必再盯地址栏。
"""
from __future__ import annotations

import html
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from . import config as cfg

log = logging.getLogger("dt.auth.loopback")

# 平台错误码 -> 人话（未知码原样展示，不做二次解释）
ERROR_HINTS = {
    "server_error": "平台内部错误（授权码签发失败）",
    "unauthorized_client": "客户端未注册或回调地址未被平台接受",
    "invalid_request": "请求参数不合法（缺少必需参数）",
    "invalid_client": "客户端校验失败",
    "invalid_grant": "授权码无效、已过期或已被使用",
    "access_denied": "您取消了授权",
    "unsupported_response_type": "平台不支持该响应类型",
    "temporarily_unavailable": "平台暂时不可用，请稍后重试",
}

# 平台发码失败时最常见的具体原因 -> 给平台同学的排查提示
DETAIL_HINTS = {
    "failed to store code": (
        "平台在签发授权码时持久化失败。这不是桌面端问题——"
        "请检查平台侧授权码存储（Redis 连接串 / 表结构 / 多实例共享）。"
    ),
}


def _detail_hint(raw: str) -> str:
    """按平台返回的 error_description 追加一句排查方向。"""
    key = (raw or "").strip().lower()
    for needle, hint in DETAIL_HINTS.items():
        if needle in key:
            return hint
    return ""


def _page(body: str, bg: str, fg: str, accent: str) -> str:
    return (
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
        "<title>EduBuddy 登录</title>"
        f"<body style='font-family:Segoe UI,Microsoft YaHei,sans-serif;"
        f"background:{bg};color:{fg};display:flex;align-items:center;"
        "justify-content:center;min-height:100vh;margin:0;padding:24px;"
        "box-sizing:border-box'>"
        f"<div style='text-align:center;max-width:560px'>{body}"
        f"<p style='color:{accent};font-size:13px;margin-top:18px'>"
        "此页面可安全关闭，登录结果已回传到 EduBuddy</p></div>"
        "</body></html>"
    )


def _success_html() -> str:
    return _page(
        "<div style='font-size:22px;font-weight:600'>登录成功</div>"
        "<p style='color:#9fc4ab;margin-top:8px'>请返回 EduBuddy 继续使用</p>",
        bg="#071b12", fg="#eef5ef", accent="#6f8f7d",
    )


def _fail_html(reason: str, detail: str = "") -> str:
    """失败页：把平台给的真实原因摊开，避免"永远显示同一句话"。"""
    title = ERROR_HINTS.get(reason, "授权回调失败")
    lines = [f"<div style='font-size:20px;font-weight:600'>{html.escape(title)}</div>"]
    if reason:
        lines.append(
            "<p style='color:#ff8f9b;font-family:Consolas,monospace;font-size:13px;"
            f"margin-top:10px'>error: {html.escape(reason)}</p>"
        )
    if detail:
        lines.append(
            "<p style='color:#ffb3bc;font-family:Consolas,monospace;font-size:13px;"
            f"margin-top:4px;word-break:break-all'>"
            f"error_description: {html.escape(detail)}</p>"
        )
    hint = _detail_hint(detail)
    if hint:
        lines.append(
            "<p style='color:#ffd7a1;font-size:13px;margin-top:14px;line-height:1.6'>"
            f"{html.escape(hint)}</p>"
        )
    return _page("\n".join(lines), bg="#2a1016", fg="#ff8f9b", accent="#8f6b70")


class LoopbackServer:
    """一次登录尝试的本地回调服务。

    用法：
        cb = LoopbackServer(expected_state, port=0)
        port = cb.start()
        # ... 浏览器完成登录后平台 302 到 http://127.0.0.1:{port}{CALLBACK_PATH}
        result = cb.wait(timeout)   # {'ok': True, 'code':...} 或失败结构
        cb.stop()

    ``result`` 结构：
        成功  {"ok": True,  "code": str, "state": str}
        失败  {"ok": False, "error": str, "error_description": str, "detail": str}
    """

    def __init__(self, expected_state: str, port: int = 0) -> None:
        self._expected_state = expected_state
        self._port = int(port)
        self._result: Optional[dict] = None
        self._closed = threading.Event()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._lock = threading.Lock()

    # -- http handler ---------------------------------------------------- #
    def _handler(self) -> type:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # silence default stderr
                pass

            def _respond(self, html_body: str, status: int = 200) -> None:
                payload = html_body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)

            def _no_content(self) -> None:
                # 非回调路径：静默丢弃，绝不影响本次登录结果
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802 (http.server API)
                path = urlparse(self.path).path.rstrip("/") or "/"
                if path != cfg.CALLBACK_PATH.rstrip("/"):
                    self._no_content()
                    return
                owner._on_callback(self.path, self._respond)

            def do_HEAD(self) -> None:  # noqa: N802
                self._no_content()

            def do_POST(self) -> None:  # noqa: N802
                self._no_content()

        return Handler

    def start(self) -> int:
        """启动服务并返回实际绑定的端口（未手动停止前持续监听）。"""
        self._httpd = ThreadingHTTPServer(
            ("127.0.0.1", self._port), self._handler()
        )
        self._httpd.daemon_threads = True
        t = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        t.start()
        port = self._httpd.server_address[1]
        log.info("loopback callback listening on 127.0.0.1:%s (%s)",
                 port, cfg.CALLBACK_PATH)
        return port

    @property
    def port(self) -> int:
        return self._httpd.server_address[1] if self._httpd else self._port

    def _on_callback(self, path: str, respond) -> None:
        with self._lock:
            if self._closed.is_set():
                # 已经有结论了（重复回调 / 浏览器重放），保持首个结果
                log.info("duplicate callback ignored: %s", path)
                respond(_success_html() if (self._result or {}).get("ok")
                        else _fail_html("already_handled"), 200)
                return
        query = parse_qs(urlparse(path).query)
        code = (query.get("code") or [""])[0]
        state = (query.get("state") or [""])[0]
        error = (query.get("error") or [""])[0]
        description = (query.get("error_description") or [""])[0]

        if error:
            # 平台的错误回调：error + error_description 全留下，前端和日志都要能看到
            log.error("platform returned error callback: %s / %s", error, description)
            self._finish({
                "ok": False,
                "error": error,
                "error_description": description,
                "detail": description or error,
            })
            respond(_fail_html(error, description), 200)
            return

        if not code or state != self._expected_state:
            log.warning(
                "invalid callback: has_code=%s state_match=%s",
                bool(code), state == self._expected_state,
            )
            self._finish({
                "ok": False,
                "error": "state_mismatch",
                "error_description": (
                    "回调缺少授权码" if not code else "回调 state 与本次登录不匹配"
                ),
                "detail": "",
            })
            respond(_fail_html("state_mismatch",
                               "missing code" if not code else "state mismatch"), 200)
            return

        self._finish({"ok": True, "code": code, "state": state})
        log.info("callback received; code captured")
        respond(_success_html(), 200)

    def _finish(self, result: dict) -> None:
        with self._lock:
            if self._closed.is_set():
                return
            self._result = result
            self._closed.set()

    def wait(self, timeout: float) -> dict:
        """阻塞等待回调结果。超时抛 TimeoutError。"""
        if not self._closed.wait(timeout):
            raise TimeoutError("登录超时：等待平台回调超时")
        return self._result or {
            "ok": False, "error": "callback_closed",
            "error_description": "", "detail": "",
        }

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
