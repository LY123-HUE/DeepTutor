"""Tokengine OAuth 回调探针（独立、零侵入）。

用途
----
1. **排查平台发码问题**：不启动 EduBuddy、不碰 DeepTutor，只跑一个回环服务，
   把平台 302 回来的每一个参数原样打印出来（含 error / error_description）。
2. **端到端验证 OAuth 链路**：自己生成 PKCE，给出可直接打开的授权 URL，
   收到 code 后立刻做 token 交换 + userinfo，把平台原始响应摊开。
3. **回归自检**：``--self-test`` 用合成回调验证收码/错误/杂散请求三条分支，
   不需要任何平台依赖。

用法
----
    # 交互式：打印授权 URL，等你在浏览器里完成登录
    python tools/tokengine_callback_probe.py

    # 指定平台与端口
    python tools/tokengine_callback_probe.py \\
        --platform https://tokengine.hanyoai.com --port 54321

    # 只收码不换码（平台 store code 还没修好时用这个看原始回调）
    python tools/tokengine_callback_probe.py --no-exchange

    # 完全离线自检
    python tools/tokengine_callback_probe.py --self-test

设计说明
--------
* 只监听 127.0.0.1（不回 0.0.0.0），且只接受 ``--callback-path`` 指定的路径，
  其余请求（favicon、扫描器）一律 204 丢弃，避免污染结果。
* 平台对 redirect_uri 的策略是「字面量 http://127.0.0.1:<任意端口> 通配」，
  所以这里的端口可以随便挑；``localhost`` / ``[::1]`` 会被拒。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

DEFAULT_PLATFORM = os.environ.get("TOKENGINE_API_BASE", "https://tokengine.hanyoai.com")
DEFAULT_CLIENT_ID = os.environ.get("TOKENGINE_CLIENT_ID", "edubuddy-desktop")
DEFAULT_SCOPE = os.environ.get("TOKENGINE_SCOPE", "openid relay")
DEFAULT_PATH = "/authorize"

C_OK = "\033[92m"
C_ERR = "\033[91m"
C_WARN = "\033[93m"
C_DIM = "\033[90m"
C_BOLD = "\033[1m"
C_END = "\033[0m"


def _c(color: str, text: str, enabled: bool = True) -> str:
    return f"{color}{text}{C_END}" if enabled else text


# --------------------------------------------------------------------------- #
# PKCE / 状态
# --------------------------------------------------------------------------- #
def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def new_verifier() -> str:
    return b64url(secrets.token_bytes(32))


def s256(verifier: str) -> str:
    return b64url(hashlib.sha256(verifier.encode("ascii")).digest())


# --------------------------------------------------------------------------- #
# HTTP 小工具
# --------------------------------------------------------------------------- #
def post_form(url: str, data: dict[str, Any], timeout: float = 20.0) -> tuple[int, str]:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def get_json(url: str, token: str, timeout: float = 20.0) -> tuple[int, str]:
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def pretty(raw: str) -> str:
    try:
        return json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        return raw


# --------------------------------------------------------------------------- #
# 回环服务
# --------------------------------------------------------------------------- #
class CallbackProbe:
    def __init__(self, expected_state: str, port: int, path: str) -> None:
        self.state = expected_state
        self.port = port
        self.path = path.rstrip("/") or "/"
        self.result: Optional[dict] = None
        self.done = threading.Event()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._lock = threading.Lock()

    # -- 渲染 ---------------------------------------------------------- #
    def _page(self, title: str, rows: list[tuple[str, str]], ok: bool) -> str:
        bg, fg = ("#071b12", "#eef5ef") if ok else ("#2a1016", "#ff8f9b")
        body = "".join(
            f"<tr><td style='padding:4px 14px 4px 0;color:#8fa;font-family:Consolas,monospace;"
            f"font-size:13px;vertical-align:top'>{html.escape(k)}</td>"
            f"<td style='padding:4px 0;font-family:Consolas,monospace;font-size:13px;"
            f"word-break:break-all'>{html.escape(v)}</td></tr>"
            for k, v in rows
        )
        return (
            "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
            "<title>Tokengine 回调探针</title>"
            f"<body style='font-family:Segoe UI,Microsoft YaHei,sans-serif;background:{bg};"
            f"color:{fg};display:flex;justify-content:center;padding:48px 24px;margin:0'>"
            f"<div style='max-width:720px;width:100%'>"
            f"<div style='font-size:20px;font-weight:600;margin-bottom:16px'>{html.escape(title)}</div>"
            f"<table style='border-collapse:collapse'>{body}</table>"
            "<p style='color:#9a9a9a;font-size:12px;margin-top:24px'>"
            "本页由探针本地渲染，可安全关闭。完整日志见探针控制台。</p>"
            "</div></body></html>"
        )

    # -- handler ------------------------------------------------------- #
    def _handler(self) -> type:
        probe = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a) -> None:
                pass

            def _send(self, payload: str, status: int = 200) -> None:
                raw = payload.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)

            def _drop(self) -> None:
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                # 只认回调路径；favicon / 扫描器一律静默丢弃，避免污染结果
                if (urllib.parse.urlparse(self.path).path.rstrip("/") or "/") != probe.path:
                    probe.log(f"{C_DIM}忽略非回调请求: {self.path}{C_END}")
                    self._drop()
                    return
                probe.on_callback(self.path, self._send)

            def do_HEAD(self) -> None:  # noqa: N802
                self._drop()

            def do_POST(self) -> None:  # noqa: N802
                self._drop()

        return H

    def start(self) -> int:
        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), self._handler())
        self._httpd.daemon_threads = True
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        self.port = self._httpd.server_address[1]
        return self.port

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:  # noqa: BLE001
                pass

    # -- 回调 ---------------------------------------------------------- #
    def log(self, msg: str) -> None:
        print(msg, flush=True)

    def on_callback(self, raw_path: str, respond) -> None:
        with self._lock:
            if self.done.is_set():
                self.log(f"{C_DIM}重复回调，已忽略{C_END}")
                respond(self._page("重复回调（已忽略）", [], True), 200)
                return

        parsed = urllib.parse.urlparse(raw_path)
        q = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

        def one(name: str) -> str:
            return (q.get(name) or [""])[0]

        code, state = one("code"), one("state")
        error, desc, error_uri = one("error"), one("error_description"), one("error_uri")

        self.log("")
        self.log("=" * 74)
        self.log(f"{C_BOLD}收到回调{C_END}  {time.strftime('%H:%M:%S')}")
        self.log("=" * 74)
        self.log(f"  路径        : {parsed.path}")
        self.log(f"  RAW QUERY   : {parsed.query or '(空)'}")
        self.log("  --- 解析后 ---")
        for k in sorted(q):
            vals = q[k]
            for v in vals:
                self.log(f"    {k:20s} = {v}")

        rows = [(k, (q.get(k) or [""])[0]) for k in sorted(q)]

        if error:
            self.log("")
            self.log(f"  {C_ERR}平台返回了错误回调{C_END}")
            self.log(f"    error             = {error}")
            self.log(f"    error_description = {desc}")
            if error_uri:
                self.log(f"    error_uri         = {error_uri}")
            if "failed to store code" in desc.lower():
                self.log("")
                self.log(f"  {C_WARN}→ 判定：平台侧授权码持久化失败，与桌面端/回调地址无关。{C_END}")
                self.log(f"  {C_WARN}  请平台侧检查：授权码存储后端（Redis 连接串 / 表结构 /"
                         f" 多实例共享），后端日志 grep 'failed to store code'。{C_END}")
            self.finish({"ok": False, "error": error, "error_description": desc, **{k: one(k) for k in q}})
            respond(self._page("平台返回错误回调", rows, False), 200)
            return

        if not code:
            self.log(f"  {C_ERR}回调缺少 code{C_END}")
            self.finish({"ok": False, "error": "missing_code"})
            respond(self._page("回调缺少 code", rows, False), 200)
            return

        if state != self.state:
            self.log(f"  {C_ERR}state 不匹配：期望 {self.state}，收到 {state}{C_END}")
            self.finish({"ok": False, "error": "state_mismatch"})
            respond(self._page("state 不匹配", rows, False), 200)
            return

        self.log("")
        self.log(f"  {C_OK}✓ state 校验通过，已捕获授权码（{len(code)} 字符）{C_END}")
        self.finish({"ok": True, "code": code, "state": state})
        respond(self._page("回调成功，授权码已捕获", rows, True), 200)

    def finish(self, result: dict) -> None:
        with self._lock:
            if self.done.is_set():
                return
            self.result = result
            self.done.set()

    def wait(self, timeout: float) -> Optional[dict]:
        if not self.done.wait(timeout):
            return None
        return self.result


# --------------------------------------------------------------------------- #
# 自检（不依赖平台）
# --------------------------------------------------------------------------- #
def self_test() -> int:
    print(f"{C_BOLD}回环探针自检{C_END}\n")
    failures: list[str] = []

    # 1) 成功回调
    p = CallbackProbe("st-ok", 0, DEFAULT_PATH)
    port = p.start()
    st, body = get_json(f"http://127.0.0.1:{port}{DEFAULT_PATH}?code=abc123&state=st-ok", "")
    ok = st == 200 and p.wait(3) == {"ok": True, "code": "abc123", "state": "st-ok"}
    print(f"  [1] 成功回调            -> {'PASS' if ok else 'FAIL'}  (http {st})")
    if not ok:
        failures.append("成功回调")
    p.stop()

    # 2) 平台错误回调
    p = CallbackProbe("st-x", 0, DEFAULT_PATH)
    port = p.start()
    qs = urllib.parse.urlencode(
        {"error": "server_error",
         "error_description": "failed to store code",
         "state": "st-x"}
    )
    st, _ = get_json(f"http://127.0.0.1:{port}{DEFAULT_PATH}?{qs}", "")
    r = p.wait(3) or {}
    ok = st == 200 and r.get("error") == "server_error" \
        and r.get("error_description") == "failed to store code"
    print(f"  [2] 平台错误回调透传     -> {'PASS' if ok else 'FAIL'}  (http {st})")
    if not ok:
        failures.append("错误回调")
    p.stop()

    # 3) 杂散请求不得污染结果（favicon）
    p = CallbackProbe("st-y", 0, DEFAULT_PATH)
    port = p.start()
    st_f, _ = get_json(f"http://127.0.0.1:{port}/favicon.ico", "")
    still_open = not p.done.is_set()
    print(f"  [3] 杂散请求被丢弃       -> "
          f"{'PASS' if (st_f == 204 and still_open) else 'FAIL'}  (http {st_f}, 结果未被污染={still_open})")
    if not (st_f == 204 and still_open):
        failures.append("杂散请求")
    # 之后仍能正常收码
    st_ok, _ = get_json(f"http://127.0.0.1:{port}{DEFAULT_PATH}?code=late&state=st-y", "")
    ok = (p.wait(3) or {}).get("code") == "late"
    print(f"  [3b] 丢弃后仍可正常收码   -> {'PASS' if ok else 'FAIL'}")
    if not ok:
        failures.append("丢弃后收码")
    p.stop()

    # 4) state 不匹配
    p = CallbackProbe("st-a", 0, DEFAULT_PATH)
    port = p.start()
    get_json(f"http://127.0.0.1:{port}{DEFAULT_PATH}?code=c&state=st-b", "")
    ok = (p.wait(3) or {}).get("error") == "state_mismatch"
    print(f"  [4] state 不匹配被拦截    -> {'PASS' if ok else 'FAIL'}")
    if not ok:
        failures.append("state 不匹配")
    p.stop()

    # 5) PKCE 正确性（对照 RFC 7636 附录 B 的向量）
    v = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    expected = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    ok = s256(v) == expected
    print(f"  [5] PKCE S256 向量        -> {'PASS' if ok else 'FAIL'}")
    if not ok:
        failures.append("PKCE")

    print()
    if failures:
        print(f"{C_ERR}自检失败：{', '.join(failures)}{C_END}")
        return 1
    print(f"{C_OK}全部自检通过{C_END}")
    return 0


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Tokengine OAuth 回调探针（独立、零侵入）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--platform", default=DEFAULT_PLATFORM, help="平台根地址")
    ap.add_argument("--port", type=int, default=54321,
                    help="回环监听端口（0=随机；默认 54321）")
    ap.add_argument("--client-id", default=DEFAULT_CLIENT_ID)
    ap.add_argument("--scope", default=DEFAULT_SCOPE)
    ap.add_argument("--callback-path", default=DEFAULT_PATH)
    ap.add_argument("--timeout", type=float, default=900.0, help="等待回调上限（秒）")
    ap.add_argument("--no-exchange", action="store_true",
                    help="只收码，不调用 /oauth/token")
    ap.add_argument("--self-test", action="store_true", help="离线自检后退出")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()

    global C_OK, C_ERR, C_WARN, C_DIM, C_BOLD, C_END
    if args.no_color or not sys.stdout.isatty():
        C_OK = C_ERR = C_WARN = C_DIM = C_BOLD = C_END = ""

    if args.self_test:
        return self_test()

    base = args.platform.rstrip("/")
    authorize_url = f"{base}/oauth/authorize"
    token_url = f"{base}/oauth/token"
    userinfo_url = f"{base}/oauth/userinfo"

    verifier = new_verifier()
    challenge = s256(verifier)
    state = b64url(secrets.token_bytes(16))
    machine_id = f"probe-{secrets.token_hex(4)}"

    probe = CallbackProbe(state, args.port, args.callback_path)
    try:
        port = probe.start()
    except OSError as e:
        print(f"{C_ERR}端口 {args.port} 被占用（{e}）。换个端口，或用 --port 0。{C_END}")
        return 2

    redirect_uri = f"http://127.0.0.1:{port}{args.callback_path}"
    params = {
        "client_id": args.client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": args.scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "auth_type": "local",
        "login_channel": "native_desktop",
        "machine_id": machine_id,
        "x_machine_id": machine_id,
    }
    url = f"{authorize_url}?{urllib.parse.urlencode(params)}"

    print()
    print("=" * 74)
    print(f"{C_BOLD}Tokengine 回调探针{C_END}")
    print("=" * 74)
    print(f"  平台          : {base}")
    print(f"  回调地址      : {redirect_uri}")
    print(f"  授权端点      : {authorize_url}")
    print(f"  client_id     : {args.client_id}")
    print(f"  scope         : {args.scope}")
    print(f"  state         : {state}")
    print(f"  code_challenge: {challenge}")
    print(f"  machine_id    : {machine_id}")
    print()
    print(f"{C_BOLD}请在浏览器打开下面的地址完成登录并点击「确认授权」：{C_END}")
    print(f"  {url}")
    print()
    print(f"{C_DIM}提示：平台只接受字面量 http://127.0.0.1:<任意端口>/<路径>；"
          f"localhost / [::1] 会被拒。{C_END}")
    print(f"{C_DIM}等待回调中（上限 {args.timeout:.0f}s，Ctrl+C 结束）…{C_END}")

    try:
        result = probe.wait(args.timeout)
    except KeyboardInterrupt:
        print("\n已中断。")
        probe.stop()
        return 130
    finally:
        probe.stop()

    if result is None:
        print(f"\n{C_ERR}等待超时，未收到回调。{C_END}")
        return 3

    if not result.get("ok"):
        print(f"\n{C_ERR}回调失败：{result.get('error')} / "
              f"{result.get('error_description', '')}{C_END}")
        return 4

    if args.no_exchange:
        print(f"\n{C_OK}已捕获授权码（未换码）：{result['code']}{C_END}")
        return 0

    print()
    print("=" * 74)
    print(f"{C_BOLD}令牌交换 POST {token_url}{C_END}")
    print("=" * 74)
    form = {
        "grant_type": "authorization_code",
        "code": result["code"],
        "redirect_uri": redirect_uri,
        "client_id": args.client_id,
        "code_verifier": verifier,
        "machine_id": machine_id,
    }
    print(f"  请求体: {urllib.parse.urlencode(form)}")
    st, body = post_form(token_url, form)
    print(f"  HTTP {st}")
    print(pretty(body))

    if st != 200:
        print(f"\n{C_ERR}换码失败。{C_END}")
        return 5

    try:
        tokens = json.loads(body)
    except Exception:  # noqa: BLE001
        print(f"\n{C_ERR}响应不是 JSON。{C_END}")
        return 5

    token = tokens.get("token") or tokens.get("access_token") or ""
    print()
    print("=" * 74)
    print(f"{C_BOLD}凭证两层模型核对{C_END}")
    print("=" * 74)
    print(f"  业务令牌 token        : {'有（写入 model_catalog 用）' if tokens.get('token') else '缺失'}")
    print(f"  access_token          : {'有（拉 userinfo 用）' if tokens.get('access_token') else '缺失'}")
    print(f"  refresh_token         : {'有' if tokens.get('refresh_token') else '缺失'}")
    print(f"  expires_in            : {tokens.get('expires_in', '-')}")

    access = str(tokens.get("access_token") or "")
    if access:
        st, body = get_json(userinfo_url, access)
        print()
        print(f"  GET {userinfo_url} -> HTTP {st}")
        print("  " + pretty(body).replace("\n", "\n  "))

    if token:
        print()
        print(f"{C_OK}链路全通。业务令牌可直接写入 DeepTutor 的 model_catalog。{C_END}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
