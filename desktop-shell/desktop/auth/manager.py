"""AuthManager：桌面端登录流程的门面。

职责：登录触发 → 回环收码 → 换令牌 → 落地 model catalog → 状态查询 / 退出。
main.py 的 Api 桥、bootstrap 门控只与它交互。
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Optional

from .. import runtime as rt
from . import config as cfg
from . import pkce
from .catalog import (
    ensure_tokengine_catalog,
    has_configured_token,
    remove_tokengine_catalog,
)
from .client import OAuthClient, OAuthError
from .loopback import ERROR_HINTS, LoopbackServer
from .store import TokenStore

log = logging.getLogger("dt.auth.manager")

# 门控三种结果，bootstrap 依此决定是否放行
GATE_LOGIN = "login"      # 必须（或强烈建议）先登录
GATE_READY = "ready"      # 已有可用令牌，直接进


class AuthManager:
    def __init__(self, root: Optional[Path] = None, home: Optional[Path] = None) -> None:
        self._root = root or rt.ROOT
        self._home = home or rt.default_workspace()
        self._store = TokenStore(self._root)

        # 端点优先级：环境变量 > <root>/endpoints.json > 内置默认。
        # endpoints.json 让打包版无需重建即可指向本地平台（联调期用）。
        self._ep = cfg.endpoints(self._root)
        self._client = OAuthClient(
            authorize_url=self._ep["authorize_url"],
            token_url=self._ep["token_url"],
            userinfo_url=self._ep["userinfo_url"],
            revoke_url=self._ep["revoke_url"],
            status_url=self._ep["status_url"],
        )
        self._api_base = self._ep["api_base"]
        self._fallback_relay = self._ep["relay_base"]
        self._lock = threading.Lock()

        # 一次登录尝试的中间态
        self._pending_verifier: Optional[str] = None
        self._pending_state: Optional[str] = None
        self._pending_url: Optional[str] = None
        self._loopback: Optional[LoopbackServer] = None

        # 门控等待（bootstrap 阻塞用）
        self._done = threading.Event()
        self._result: dict[str, Any] = {"ok": False, "error": "not_started"}

    # -- 只读状态 -------------------------------------------------------- #
    @property
    def home(self) -> Path:
        return self._home

    def is_logged_in(self) -> bool:
        return bool((self._store.load() or {}).get("token"))

    def has_usable_token(self) -> bool:
        """本地已有一份可用令牌（我们存的 或 用户在设置里手配的）。"""
        if self.is_logged_in():
            return True
        return has_configured_token(self.home)

    def gate_state(self) -> str:
        return GATE_READY if self.has_usable_token() else GATE_LOGIN

    def account(self) -> dict[str, Any]:
        payload = self._store.load()
        return dict(payload.get("account") or {})

    def status(self) -> dict[str, Any]:
        payload = self._store.load()
        account = dict(payload.get("account") or {})
        return {
            "logged_in": bool(payload.get("token")),
            "configured": has_configured_token(self.home),
            "account": {
                "phone": account.get("phone") or "",
                "balance": account.get("balance"),
                "models": account.get("models") or [],
            },
            # 登录后实际生效的中继域名（平台拉取的，或本地回退值）
            "relay_base": payload.get("relay_base") or self._fallback_relay,
            "relay_source": payload.get("relay_source") or "local-config",
            "login_supported": bool(self._client.authorize_url and self._client.token_url),
            "in_progress": self._done.is_set() is False and self._pending_url is not None,
        }

    def _derive(self, account: dict[str, Any], status: dict[str, Any]) -> tuple[list[str], str, str, str]:
        """从一份 account（可能刚拉的 userinfo）+ 平台 status 推导可用信息。

        与登录落库的推导逻辑共用，供 refresh_models 复用（不重复实现）。
        返回 ``(models, phone, relay_base, relay_source)``。
        """
        models = account.get(cfg.USERINFO_MODELS_FIELD) or cfg.DEFAULT_MODELS
        models = [str(m) for m in models if str(m).strip()]
        phone = str(account.get(cfg.USERINFO_PHONE_FIELD) or "") or ""
        relay_base, relay_source = cfg.resolve_relay(
            api_base=self._api_base,
            userinfo=account,
            status=status,
            fallback=self._fallback_relay,
        )
        return models, phone, relay_base, relay_source

    # -- 刷新 / 平台入口（右键菜单用）---------------------------------- #
    def refresh_models(self) -> dict[str, Any]:
        """重拉 userinfo + /api/status → 重写 model_catalog → 回写账号信息。

        供右键菜单「刷新可用模型」调用；成功后 DeepTutor 重新读取刚写入的
        模型列表（调用方负责 reload 页面）。未登录时直接返回 not_logged_in。
        """
        with self._lock:
            payload = self._store.load()
            token = str(payload.get("token") or "")
            access = str(payload.get("access_token") or "")
            if not (token or access):
                return {"ok": False, "error": "not_logged_in",
                        "message": "尚未登录，无法刷新模型"}
            # userinfo 只认 access_token；失败/缺失则降级用本地已存账号
            fresh: dict[str, Any] = {}
            if access and self._client.userinfo_url:
                try:
                    fresh = self._client.userinfo(access)
                except Exception as exc:  # noqa: BLE001
                    log.warning("刷新时 userinfo 失败（将沿用本地账号）：%s", exc)
            account = dict(payload.get("account") or {})
            account.update({k: v for k, v in fresh.items()
                            if v not in (None, "")})
            status = self._client.status()
            models, phone, relay_base, relay_source = self._derive(account, status)

            try:
                path = ensure_tokengine_catalog(
                    home=self._home, api_key=token,
                    base_url=relay_base, models=models)
            except Exception as exc:  # noqa: BLE001
                log.exception("刷新模型写入 catalog 失败")
                return {"ok": False, "error": "write_failed",
                        "message": f"写入模型配置失败：{exc}"}

            # 回写 store，让 status()/菜单头行反映最新数据
            acc = dict(payload.get("account") or {})
            if models:
                acc["models"] = models
            if not acc.get("phone") and phone:
                acc["phone"] = phone
            if acc.get("balance") is None and account.get("balance") is not None:
                acc["balance"] = account.get("balance")
            payload["account"] = acc
            payload["relay_base"] = relay_base
            payload["relay_source"] = relay_source
            self._store.save(payload)

            log.info("models refreshed: %d model(s) @ %s (relay=%s)",
                     len(models) if models else 0, phone or "?", relay_source)
            return {
                "ok": True, "models": models, "phone": phone,
                "relay_base": relay_base, "relay_source": relay_source,
                "path": str(path),
            }

    def open_platform(self) -> dict[str, Any]:
        """用系统浏览器打开 Tokengine 平台首页。"""
        try:
            import webbrowser
        except ImportError:  # pragma: no cover  环境异常时才可能走到
            return {"ok": False, "error": "no_webbrowser"}
        url = (self._api_base or self._fallback_relay or "").strip()
        if not url:
            return {"ok": False, "error": "no_platform_url", "message": "未配置平台地址"}
        webbrowser.open(url)
        log.info("opened platform: %s", url)
        return {"ok": True, "url": url}

    # -- 登录 ------------------------------------------------------------ #
    def start_login(self) -> dict[str, Any]:
        """发起登录：生成 PKCE 与回环服务，返回可打开的授权 URL。"""
        with self._lock:
            if self._pending_url:
                return {"ok": True, "url": self._pending_url, "in_progress": True}

            verifier = pkce.generate_verifier()
            challenge = pkce.s256_challenge(verifier)
            state = pkce.generate_state()
            machine_id = self._store.machine_id()

            self._pending_state = state
            self._pending_verifier = verifier

            try:
                loopback = LoopbackServer(state, port=cfg.CALLBACK_PORT)
                port = loopback.start()
            except Exception as exc:  # noqa: BLE001
                log.exception("loopback start failed")
                return {"ok": False, "error": f"本地回调启动失败：{exc}"}
            self._loopback = loopback

            redirect_uri = f"http://127.0.0.1:{port}{cfg.CALLBACK_PATH}"
            url = self._client.build_authorize_url(
                redirect_uri=redirect_uri,
                code_challenge=challenge,
                state=state,
                machine_id=machine_id,
            )
            self._pending_url = url
            self._done.clear()

            threading.Thread(target=self._watchdog, args=(loopback, redirect_uri, machine_id),
                             daemon=True).start()
            log.info("login started; open:%s", url.split("?")[0])
            return {"ok": True, "url": url, "in_progress": True}

    def _finalize(self, result: dict[str, Any]) -> None:
        """收尾一次登录尝试：落结果、清中间态、放行门控。

        必须清掉 _pending_*：否则失败后用户再点一次「登录」，会命中
        start_login 的早返回分支，把**上一轮**的授权 URL（连带已经关闭的
        回环端口）原样重新打开，表现为「点了没反应 / 一直失败」。
        """
        self._result = result
        self._pending_url = None
        self._pending_verifier = None
        self._pending_state = None
        self._loopback = None
        self._done.set()

    def _watchdog(self, loopback: LoopbackServer, redirect_uri: str, machine_id: str) -> None:
        try:
            cb = loopback.wait(timeout=cfg.LOGIN_TIMEOUT)
        except TimeoutError:
            self._finalize({
                "ok": False,
                "error": "timeout",
                "message": "登录超时",
                "detail": "等待平台回调超时，请重新发起登录",
            })
            return
        finally:
            loopback.stop()

        if not cb.get("ok"):
            # 平台的 error / error_description 一路带到 UI，别再只显示一个 slug
            err = str(cb.get("error") or "callback_failed")
            desc = str(cb.get("error_description") or "")
            log.error("login callback failed: %s / %s", err, desc)
            self._finalize({
                "ok": False,
                "error": err,
                "message": ERROR_HINTS.get(err, "授权回调失败"),
                "detail": f"{err}: {desc}" if desc else err,
            })
            return

        verifier = self._pending_verifier
        if not verifier:
            self._finalize({
                "ok": False, "error": "session_lost",
                "message": "会话状态丢失", "detail": "请重新发起登录",
            })
            return

        try:
            tokens = self._client.exchange(
                cb["code"], verifier, redirect_uri=redirect_uri, machine_id=machine_id
            )
        except OAuthError as exc:
            self._finalize({
                "ok": False, "error": "exchange_failed",
                "message": "令牌交换失败", "detail": str(exc),
            })
            return

        token = str(tokens.get("token") or tokens.get("access_token") or "")
        if not token:
            self._finalize({
                "ok": False, "error": "no_token",
                "message": "平台未返回业务令牌",
                "detail": "响应缺少 token 字段",
            })
            return

        # 可选：带 access_token 拉一次 userinfo，拿余额/模型列表
        account: dict[str, Any] = {}
        access_token = str(tokens.get("access_token") or "")
        if access_token and self._client.userinfo_url:
            try:
                account = self._client.userinfo(access_token)
                # 只记字段名不记值：便于按平台实际命名调整字段映射
                log.info("userinfo 字段：%s", sorted(account) if account else "(空)")
            except Exception as exc:  # noqa: BLE001
                log.warning("userinfo 失败（不影响登录）：%s", exc)
        elif not access_token:
            log.warning("平台未返回 access_token，跳过 userinfo（模型将走本地回退）")

        # 模型 / 手机号 / 中继域名：复用 refresh_models 同款推导。
        # /api/status 的 server_address 是「域名」的权威来源——运维换域名，
        # 客户端自动跟随，这就是「登录拉取域名」的落点。
        status = self._client.status()
        models, phone, relay_base, relay_source = self._derive(account, status)
        log.info("中继域名解析：%s（来源 %s；平台宣告 %s）",
                 relay_base, relay_source,
                 cfg.pick_relay_from_status(status) or "(无)")
        log.info("中继域名解析：%s（来源 %s；平台宣告 %s）",
                 relay_base, relay_source,
                 cfg.pick_relay_from_status(status) or "(无)")

        payload = {
            "token": token,
            "access_token": access_token,
            "refresh_token": str(tokens.get("refresh_token") or ""),
            "expires_at": int(tokens.get("expires_in") or 0),
            "account": {
                "phone": phone,
                "balance": account.get("balance"),
                "models": models,
                "raw": {k: v for k, v in account.items()
                        if k not in (cfg.USERINFO_MODELS_FIELD, cfg.USERINFO_PHONE_FIELD)},
            },
            "relay_base": relay_base,
            "relay_source": relay_source,
        }
        self._store.save(payload)

        # 落地到 DeepTutor：写业务令牌 + 中继域名 + 模型列表
        try:
            ensure_tokengine_catalog(
                home=self._home, api_key=token, base_url=relay_base, models=models
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("write model_catalog failed")
            self._finalize({
                "ok": True, "token": token, "models": models, "phone": phone,
                "relay_base": relay_base, "message": "令牌已获取",
                "warning": f"令牌已获取，但写入 DeepTutor 配置失败：{exc}",
            })
            return

        log.info("login completed for %s with %d model(s) @ %s",
                 phone or "?", len(models) if models else 0, relay_base)
        self._finalize({
            "ok": True, "token": token, "models": models,
            "phone": phone, "relay_base": relay_base, "account": self.account(),
        })

    def wait_done(self, timeout: float) -> dict[str, Any]:
        """门控等待登录完成；超时返回当前未完成状态。

        **消费语义**：返回结果后会重置完成标志，使下一次调用阻塞到
        **下一次**尝试结束。否则调用方（bootstrap 的重试循环）会在失败后
        立刻再次拿到同一个失败结果，形成忙等。
        """
        if not self._done.wait(timeout):
            return {"ok": False, "error": "timeout"}
        result = dict(self._result)
        self._done.clear()
        return result

    def skip(self) -> None:
        """用户选择『稍后再说』：结束门控放行（不写任何配置）。"""
        self._finalize({"ok": False, "error": "skipped",
                        "message": "已跳过登录", "detail": ""})

    def last_error(self) -> dict[str, Any]:
        """最近一次登录尝试的结果（供 UI 展示真实失败原因）。"""
        return dict(self._result)

    def pending_url(self) -> Optional[str]:
        return self._pending_url

    # -- 退出 ------------------------------------------------------------ #
    def logout(self) -> dict[str, Any]:
        payload = self._store.load()
        token = str(payload.get("token") or "")
        access = str(payload.get("access_token") or "")
        refresh = str(payload.get("refresh_token") or "")
        machine_id = self._store.machine_id()

        # 平台 /oauth/revoke 的实现（controller/oauth_provider.go::OAuthRevoke）是：
        #   先用 JWT 解出用户；只有拿到 userID 才会吊销「绑设备的业务令牌」。
        # 业务令牌（sk-...）不是 JWT，平台无法从它反查用户 → 只传它等于没吊销。
        # 所以必须把 access_token 一并送过去；refresh_token 由平台按值吊销。
        for candidate in (access, refresh):
            if candidate:
                try:
                    self._client.revoke(candidate, machine_id)
                except Exception:  # noqa: BLE001
                    pass
        if not access and token:
            # 没有 access_token（例如旧版落盘数据）时退而求其次，至少试一次业务令牌
            try:
                self._client.revoke(token, machine_id)
            except Exception:  # noqa: BLE001
                pass

        self._store.clear()

        # 摘除我们写进 DeepTutor model_catalog 的 Tokengine 连接/配置，
        # 让应用不再拿已吊销的 token 去请求（"移除 catalog 连接"）。
        try:
            remove_tokengine_catalog(self._home, token=token)
            detached = True
        except Exception as exc:  # noqa: BLE001
            log.exception("退出登录时摘除 catalog 失败")
            detached = False

        log.info("logged out (catalog detached=%s)", detached)
        return {"ok": True}
