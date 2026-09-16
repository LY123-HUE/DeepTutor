# ADR-001: Tokengine OAuth 回调落点（桌面回环 vs 8001 后端）

## Status

**Accepted** — 保留桌面端本地回环接收回调；**「由 8001 后端接收回调」推迟**
（Deferred，触发条件见 §7）。

- 日期：2026-09-15
- 决策者：EduBuddy 桌面端 / Tokengine 平台
- 相关：`docs/tokengine-oauth-spec.md`、`docs/architecture.md`

---

## Context

### 触发这个讨论的现象

平台侧 `http://127.0.0.1:3000/oauth/consent` 已改造为「手机号 + 账密双登录」。
在浏览器完成登录、点击「确认授权」后，浏览器被跳到：

```
http://127.0.0.1:54321/authorize?error=server_error
    &error_description=failed%20to%20store%20code&state=st-test
```

由此提出的问题是：**桌面客户端只是套壳、真正的后端在 8001 监听，能否把这个回调改由
8001 的后端服务来实现？**

### 实测到的平台行为（2026-09-15 对 `127.0.0.1:3000`）

| 探测项 | 实测结果 |
| --- | --- |
| `/oauth/authorize`（无登录态） | `302` → `/oauth/consent`，**原样透传**全部 query |
| 「确认授权」按钮实现 | `window.location.href = "/oauth/authorize?" + 原 query`（**第二趟**、带登录 Cookie 才发码） |
| `redirect_uri` 白名单 | **只认字面量 `http://127.0.0.1:<任意端口>/<任意路径>`** |
| 被拒形态 | `localhost` / `[::1]` / 无端口 / `https://` 同域 / 外部域名 / 自定义 scheme → `400 unauthorized_client: redirect_uri not registered` |
| 前端 bundle 是否含 `failed to store code` | **0 命中** → 该字符串来自后端（Go），非前端文案 |
| 前端 bundle 是否含 `code_challenge` | **0 命中** → 消费页不参与 PKCE，仅透传 |
| `/oauth/token` | 已实现；form 编码；错误码 `invalid_client` / `invalid_grant` |
| 错误回调形态 | `302` 到 `redirect_uri?error=&error_description=&state=`；实测词汇表见下 |

实测错误词汇表（`Location` 头原文）：

```
unknown client_id                      -> unauthorized_client / unknown client_id
空 client_id / 缺参数                   -> invalid_request / missing required parameters
code_challenge_method=plain            -> invalid_request / only S256 code_challenge_method is supported
response_type=token                    -> unsupported_response_type / response_type must be code
非回环 redirect_uri                     -> 400 JSON: unauthorized_client / redirect_uri not registered
```

### 结论性判断

1. **`failed to store code` 与回调落点无关。** 它是平台后端在**第二趟发码**时
   持久化授权码失败，`error_description` 已经正确回送给 `redirect_uri`——
   说明回环**已经接住了**回调，链路本身是通的。
2. **桌面端的随机端口回环本来就是合规的。** 平台对 `redirect_uri` 是回环通配策略，
   不存在"必须换成固定地址才能被平台接受"的问题。把回调搬到 8001 **不会**让这个
   报错消失，只会让它出现在 8001 的日志里。

---

## Decision

**保留桌面端本地回环接收回调（RFC 8252 loopback），不为当前问题改动回调落点。**

同时，在排查过程中发现桌面端的两个真实缺陷，一并修复（这才是本次真正需要改的）：

1. **`error_description` 被丢弃**（`loopback.py`）。平台明明回送了
   `failed to store code`，桌面端只保留了 `error=server_error`，失败页还是固定文案
   「授权回调无效或已过期」——导致必须盯着浏览器地址栏才能知道原因。
2. **`do_GET` 对任意路径都当回调处理**。浏览器取回调页后会再请求 `/favicon.ico`，
   这会立刻把结果覆写成 `state_mismatch`，并与 `wait()` 抢 `_result`（竞态）。
3. **失败后重试会失效**（`manager.py` + `splash.py`）。失败路径没有清 `_pending_url`，
   再次点「登录」会命中早返回分支，把**上一轮**的授权 URL（连带已关闭的回环端口）
   原样重开；启动页的登录按钮在发起后也永久置灰。两者叠加使"平台报错后无法重试"。

---

## Considered Options

| 方案 | 做法 | 取舍 |
| --- | --- | --- |
| **A. 桌面回环（选中）** | 桌面进程监听 `127.0.0.1:<随机端口>`，平台 302 到该地址 | ✅ 零侵入 DeepTutor 上游<br>✅ PKCE verifier 不出本机<br>✅ 无需改动启动顺序<br>❌ 仅桌面端可登录（Web 直开不行）<br>❌ 每次登录端口不同（平台侧日志不易比对） |
| **B. 固定端口回环** | 同 A，但端口固定（`TOKENGINE_CALLBACK_PORT`，如 54321） | ✅ 在 A 之上消除"端口每次不同"<br>✅ 平台侧抓包/日志稳定可复现<br>❌ 端口被占用时需回退随机（已实现回退语义）<br>❌ 仍不解 B 之外的场景 |
| **C. 8001 后端接收回调** | `redirect_uri=http://127.0.0.1:8001/authorize`，由 DeepTutor 的 FastAPI 接码 | ✅ 固定地址、常驻监听<br>✅ 令牌**进程内**注入 model_catalog（登出后热更新无需重启）<br>✅ Web 直开前台也能登录<br>✅ 可 `curl` 调试，不依赖桌面端<br>❌ **侵入 DeepTutor 上游代码**（fork drift）<br>❌ 需跨进程把 `state`+`code_verifier` 交给后端<br>❌ **启动顺序倒置**：登录必须在 `deeptutor start` **之后**<br>❌ 8001 是纯文本 HTTP，多一个本地监听面 |

### 关于方案 C 的成本细节（为什么当前不值得）

- **进程边界**。桌面壳（PyInstaller exe）与 DeepTutor 后端（`deeptutor start` 拉起的
  uvicorn）是**两个进程**。8001 收到 `code` 后，必须让桌面端的 `AuthManager` 知道
  结果；或者反过来把 `code_verifier` 交给 8001。无论哪个方向，都要新增一条
  **进程内私有通道**（本地文件 / 命名管道 / WS），凭空多出一处一致性与清理负担。
- **PKCE 归属被打破**。RFC 7636 的 verifier 应由"发起授权请求的一方"持有。
  当前桌面端持有 verifier 是正确的；方案 C 需要主动把它送出去。
- **启动顺序倒置**。现状是 `bootstrap()` 第 5 步做登录门控，而第 3 步才
  `proc.start()`——注意门控在**进程已拉起之后**，所以顺序上可行，但会引入新的
  体验问题：**首次启动需装配 web 资源，实测会超过 150s**。若把登录绑到 8001，
  用户必须先等后端就绪才能看到登录按钮；现状则是"后端在装、登录页同时已经很热"，
  两者**并行**，感知上更快。
- **fork drift**。任何对 `deeptutor/api/` 的改动都会与上游版本产生分叉，
  升级时要重新处理。ADR 的立场是：**只有明确收益大于长期维护成本时才付这笔钱**。

---

## Consequences

### 变容易的

- 桌面端失败原因**可读**：启动页直接显示
  「登录未完成：平台内部错误（授权码签发失败）」+ `server_error: failed to store code`
  + 一句指向平台存储的排查提示。
- 失败**可重试**：按钮随 `in_progress` 自动恢复，重试拿到全新 `state`/`code_verifier`/端口。
- 回环**不再被杂散请求误伤**：非回调路径一律 204 丢弃，重复回调不覆盖首个结果。
- `TOKENGINE_CALLBACK_PORT` 可在需要时固定端口，便于平台侧比对日志。

### 变困难的

- 仍**只有桌面端能登录**；Web 直开 `127.0.0.1:3782` 的用户拿不到令牌。
- 登出/换号后，运行中的 DeepTutor 后端需要重新读取配置才生效（重启或走设置的
  reload），因为令牌是由桌面端写盘、后端启动时读取的。

### 仍然阻塞的（**平台侧**，不在本 ADR 范围内）

`failed to store code` 必须由平台侧修复。定向排查清单：

1. 后端日志 grep `failed to store code`，确认失败发生在哪个存储调用。
2. 若授权码存 Redis：确认 `REDIS_CONN_STRING` 已配置且可达；
   很多网关类项目的临时态（授权码 / state）**只在 Redis 可用时才工作**，
   未配置 Redis 时内存回退缺失，就会恰好报"store"失败。
3. 确认授权码相关表结构/迁移已执行（若落 DB）。
4. 确认后端**多实例共享**同一存储：第二趟 `/oauth/authorize` 与 `/oauth/token`
   可能落在不同实例上。
5. 复现用的最小命令见 §6。

---

## 6. 复现与调试

### 零侵入回调探针（推荐）

不启动 EduBuddy、不改 DeepTutor，独立跑完整 OAuth 握手（自生成 PKCE、收码、换码）：

```bash
# 交互式：打印授权 URL，在浏览器完成登录后自动换码并打印平台原始响应
python tools/tokengine_callback_probe.py --platform http://127.0.0.1:3000 --port 54321

# 平台存储还没修好时：只看回调原始参数，不换码
python tools/tokengine_callback_probe.py --no-exchange

# 完全离线自检（验证收码/错误/杂散请求三条分支）
python tools/tokengine_callback_probe.py --self-test
```

### 手工探测平台端点（不跟随 302，看原始 Location）

```bash
curl -s -i -m 8 "http://127.0.0.1:3000/oauth/authorize?client_id=edubuddy-desktop\
&response_type=code&redirect_uri=http%3A%2F%2F127.0.0.1%3A54321%2Fauthorize\
&scope=openid%20relay&state=st-1\
&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM\
&code_challenge_method=S256" | grep -iE "^(HTTP/|location:)"
```

> 注意：带登录态的第二趟才会发码，`curl` 无 Cookie 时**只会**看到 302 到 `/oauth/consent`。
> 必须用真实浏览器登录后才能复现发码环节。

---

## 7. 推迟的方案 C：触发条件与设计草案

### 什么时候应该回来做方案 C

满足**任意两条**时重新评估：

1. 需要**不装桌面端**也能登录（例如 Web 直开、或未来接入第二个客户端）。
2. 需要**登录/换号后无需重启**即时生效（令牌热更新）。
3. 需要在 CI/E2E 里**自动化**跑通整条 OAuth 链路（8001 可被脚本驱动）。

若仅是为了排查当前这个 `failed to store code`，**不要**做方案 C——
`tools/tokengine_callback_probe.py` 已经用零成本覆盖了同样的排查能力。

### 设计草案

```
浏览器                      平台 :3000                  DeepTutor 后端 :8001        桌面壳
  │                            │                            │                     │
  │ 1. 点「登录」               │                            │  2. POST /api/edubuddy/oauth/session
  │                           │                            │  ◀──────────────────┤ {state, code_verifier}
  │                           │                            │  ──────────────────▶│ {authorize_url}
  │ 3. 打开 authorize_url      │                            │                     │
  ├───────────────────────────▶│                            │                     │
  │ 4. 登录 + 确认授权          │                            │                     │
  │                            │ 5. 302 /authorize?code=&state=                    │
  │                            ├───────────────────────────▶│                     │
  │                            │                            │ 6. 换码 → 得 token   │
  │                            │                            │ 7. 写 model_catalog │
  │ 8. 看到「登录成功」页        │                            │ 8. 置会话结果        │
  │                           │                            │                     │
  │                           │                            │  9. GET /api/edubuddy/oauth/session/{state}
  │                           │                            │  ◀──────────────────┤ (轮询)
```

要点：

- **落点**：`deeptutor/api/routers/edubuddy_oauth.py`（新增独立模块，不散落在
  `main.py` 内），在 `main.py` 加一段**带注释标记**的注册块，便于上游合并时识别与摘除。
- **同源**：`/authorize` 挂在根路径（非 `/api` 前缀），与桌面端现用的
  `CALLBACK_PATH = "/authorize"` 保持一致，两套方案可共存。
- **会话表**：进程内 dict `{state: {verifier, created_at, result}}`，带 TTL 清理。
  注意：多 worker 部署下必须共享（Redis / DB）；单进程 uvicorn 则内存即可。
- **安全**：8001 的会话登记接口**只接受来自 127.0.0.1 的连接**，并校验
  `state` 为服务端生成值；`/authorize` 除了渲染成功页不得回显 `code`。
- **回退**：`redirect_uri` 由配置选择 A/B/C（`TOKENGINE_CALLBACK_MODE`），
  C 不可用时退回 A，避免单点。

### 迁移成本估算（相对当前实现）

| 项 | 改动 |
| --- | --- |
| 桌面端 | `config.py` 增加模式开关；`manager.py` 换掉"本地回环"分支为"登记 + 轮询" |
| DeepTutor | 新增 1 个 router 文件 + `main.py` 1 处注册 |
| 平台 | `redirect_uri` 白名单已天然支持 `127.0.0.1:8001`，**无需改动** |
| 测试 | 现回环的回归测试转为模式 A 的回归；模式 C 需新增 router 级测试 |

---

## 附录：决策依据的原始探测结果

| # | 探测 | 结果 |
| --- | --- | --- |
| 1 | `GET /oauth/authorize`（合法全参，无登录态） | `302` → `/oauth/consent?...`（参数透传） |
| 2 | 同请求，`redirect_uri=127.0.0.1:8001/authorize` | 同样 `302` → `/oauth/consent`（**8001 被放行**） |
| 3 | 同请求，`redirect_uri=127.0.0.1:9999/authorize`、`:8080/callback` | 均放行 |
| 4 | `redirect_uri=localhost:8001/...` | `400 unauthorized_client: redirect_uri not registered` |
| 5 | `redirect_uri=[::1]:8001/...` | 同上被拒 |
| 6 | `redirect_uri=http://127.0.0.1/authorize`（无端口） | 同上被拒 |
| 7 | `redirect_uri=https://evil.example.com/steal` | 同上被拒 |
| 8 | `client_id=__no_such_client__` | `302` → `redirect_uri?error=unauthorized_client&error_description=unknown client_id` |
| 9 | `code_challenge_method=plain` | `302` → `...only S256 code_challenge_method is supported` |
| 10 | `response_type=token` | `302` → `...response_type must be code` |
| 11 | `POST /oauth/token`（伪造 code） | `400 invalid_grant: code is invalid, expired or already used` |
| 12 | `POST /oauth/token`（空表单） | `400 invalid_client: client_id is required` |
| 13 | bundle `index-CwQ7IUEW.js`（11.1 MB）搜 `failed to store code` | **0 命中** |
| 14 | 同 bundle 搜 `code_challenge` | **0 命中** |

> 附带说明：探测过程中曾出现 `WinError 10061`（连接被拒），一度怀疑平台不稳。
> 经夹逼验证（前后夹 TCP 健康检查均为 `UP`），根因是 **urllib 自动跟随了 302
> 跳到 `127.0.0.1:54321`（无进程监听）**，并非平台故障。这也是 §6 强调
> "看原始 Location 要禁用跳转"的原因。
