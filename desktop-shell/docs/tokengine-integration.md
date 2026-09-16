# Tokengine 登录接入说明（桌面端）

> 面向接手联调或排查登录问题的人。回答三个问题：**登录拉到了什么、写到了哪里、出问题怎么查。**

## 1. 一句话

桌面客户端启动后**直接进入应用**（启动页只在拉起服务时短暂出现，不再拦截用户）。
应用界面**左下角常驻一个「登录」按钮**；点它用系统浏览器打开平台授权页，完成手机号/账密
登录并点「确认授权」后，平台回调到本机回环服务；客户端换取令牌，并把**域名、业务令牌、
可用模型**三样东西写进 DeepTutor 配置，随后自动刷新页面载入这些模型——用户无需任何手工配置。

> 设计取舍：早期版本用「登录门控」——没令牌就停在启动页。实测这会挡掉所有本地功能，
> 且把登录失败的全部注意力压在一个入口上。现在改成**不拦启动 + 应用内常驻按钮**：
> 未登录也能正常用本地功能，登录是随时可做的增量动作。

## 2. 登录时序

```
应用左下角「登录」按钮                系统浏览器                  平台                    本机回环
  │ 点击（事件槽）                      │                        │                       │
  ├── 生成 PKCE + 启动回环 127.0.0.1:P ──────────────────────────────────────────────▶│
  ├── webbrowser.open(authorize_url) ─▶│                        │                       │
  │                                   ├── GET /oauth/authorize▶│                       │
  │                                   │◀─ 302 /oauth/consent ──┤ （第一趟不发码）        │
  │                                   │  登录 + 「确认授权」      │                       │
  │                                   ├── GET /oauth/authorize▶│ （第二趟，带 Cookie）   │
  │                                   │                        ├── 签发授权码           │
  │                                   │◀─ 302 ?code=&state= ───┤                       │
  │                                   ├───────────────────────────────────────────────▶│
  │◀── 换令牌 POST /oauth/token ──────────────────────────────┤                       │
  │◀── 拉 userinfo（余额 / 模型）──────────────────────────────┤                       │
  │◀── 拉 GET /api/status（平台对外域名 server_address）────────┤                       │
  ├── 解析域名 + 写 model_catalog + DPAPI 落盘                                          │
  └── location.reload()，页面载入可用模型                                               │
```

**按钮与 Python 之间怎么通信**：`desktop/inject.py` 用 `evaluate_js` 轮询一个隐藏 input
（当事件槽）来收点击，而不是依赖 pywebview 的 `js_api` 桥在 `load_url()` 到外站页面后
仍被注入——后者跨版本不稳。`evaluate_js` 是 pywebview 最底层能力，已验证可用。
按钮每 2s 自愈一次（SPA 路由切换会重建 DOM），文案随登录态变化：
`登录` → `等待浏览器…` → `已登录 8899`。

## 3. 登录拉到了什么，写到了哪里

| 拉取项 | 来源 | 落点 | 失败时 |
| --- | --- | --- | --- |
| **域名**（中继 base_url） | 按下面 §3.1 的优先级链解析 | `model_catalog.json` 的 `connections[tokengine].base_url` 与活动 profile 的 `base_url` | 回退到本地配置（`endpoints.json` / 环境变量 / 内置默认） |
| **业务令牌** | `/oauth/token` 响应的 `token` 字段（`sk-Tok...`） | 同上两处的 `api_key`；同时 DPAPI 加密存入 `auth.json` | 视为登录失败并提示 |
| **可用模型** | `userinfo` 的 `models` 字段 | 活动 profile 的 `models`（首模型设为活动） | 回退到 `TOKENGINE_DEFAULT_MODELS` |

### 3.1 域名解析优先级（`config.resolve_relay`）

| # | 条件 | 结果 | `relay_source` |
| --- | --- | --- | --- |
| 1 | `api_base` 是**回环地址**（`127.0.0.1` / `localhost`） | `<api_base>/v1` | `local-loopback` |
| 2 | `userinfo` 返回了中继字段 | 该值（归一化） | `userinfo` |
| 3 | 平台 `/api/status` 的 `server_address` | 该值（归一化） | `platform` |
| 4 | 以上都没有 | 本地 `relay_base` | `local-config` |

**为什么第 1 条要压在最前面**：本机跑平台联调时（`localhost:3000`），平台仍然会宣告
生产域名 `server_address=https://tokengine.hanyoai.com`。若无条件采信，本地联调会
**悄悄把中继打到线上**。所以「显式配了回环地址」被视为用户意图，优先尊重。

反之，用户配的是公网域名时，第 3 条让运维**换域名后老客户端自动跟随**，无需重新发版。

域名归一化：缺 scheme 补 `https://`，无路径补 `/v1`（`TOKENGINE_RELAY_AUTO_V1=0` 可关）。
例如 `server_address = "tokengine.hanyoai.com"` → `https://tokengine.hanyoai.com/v1`。

### 3.2 平台真实契约（读 D:\studio\tokengine 源码得出，非探测猜测）

| 端点 | 响应 |
| --- | --- |
| `POST /oauth/token` | **扁平**：`access_token` / `refresh_token` / `token` / `token_type` / `expires_in` / `user{id,phone,username,balance,models[]}` |
| `GET /oauth/userinfo` | **扁平**：`sub` / `phone` / `username` / `balance` / `models[]` |
| `GET /api/status` | `{success, data{server_address, system_name, ...}}`（公开，无需鉴权） |

- `client_id` 必须是平台侧已注册的（DB 配置项 `OAuthClients`，默认空 → 未注册会被拒）；
  联调实例已注册 `edubuddy-desktop`。
- `redirect_uri` 白名单格式为 `http://127.0.0.1:*`：**scheme + host 必须精确匹配
  `127.0.0.1`，端口任意且必须存在**。实测 `localhost` / `[::1]` / 无端口 / 外部域名
  一律 `400 unauthorized_client: redirect_uri not registered`。
- 授权码一次性、5 分钟有效，绑定 `client_id + redirect_uri + code_challenge`；
  换码时 `code_verifier` 必须与授权时的 `code_challenge` 做 S256 校验。
- 业务令牌按 `user_id + machine_id + client_id` **幂等**签发（`oauth-<client>-<machine>`），
  重新登录会复用同一条；吊销后重新登录会重新启用。

> **注意**：只有「业务令牌」（`token`）会被写进 DeepTutor。`access_token` / `refresh_token`
> 是 OAuth 会话凭证，仅用于换/吊销，**绝不进入任何对话请求**。

## 4. 配置与覆盖（联调期指向本地平台）

端点解析优先级：**环境变量 > `<ROOT>/endpoints.json` > 内置默认**
（内置默认为 `config.DEV_API_BASE`，即本联调构建的 `http://127.0.0.1:3000`）。

`<ROOT>` = `%LOCALAPPDATA%\EduBuddy`（可用 `DEEPTUTOR_DESKTOP_ROOT` 改）。

`%LOCALAPPDATA%\EduBuddy\endpoints.json`：

```json
{
  "api_base": "http://127.0.0.1:3000"
}
```

可覆写的键：`api_base`、`authorize_url`、`token_url`、`userinfo_url`、`revoke_url`、
`relay_base`、`status_url`。其余端点由 `api_base` 派生。**这是给打包版准备的**——
装好的客户端无需重新打包即可改指向（联调/私有化部署都用它）。

环境变量（同名大写，前缀 `TOKENGINE_`）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `TOKENGINE_API_BASE` | 联调构建 `http://127.0.0.1:3000`；发布为 `https://tokengine.hanyoai.com` | 平台根地址 |
| `TOKENGINE_DEFAULT_API_BASE` | 同上 | 覆盖**编译进包**的默认值（构建时指定） |
| `TOKENGINE_CLIENT_ID` | `edubuddy-desktop` | 平台侧注册的客户端标识 |
| `TOKENGINE_SCOPE` | `openid relay` | 授权范围 |
| `TOKENGINE_CALLBACK_PORT` | `0`（随机） | 回环端口；设固定值便于比对平台日志 |
| `TOKENGINE_LOGIN_TIMEOUT` | `900` | 单次登录等待上限（秒） |
| `TOKENGINE_USERINFO_RELAY_FIELDS` | 见 §3 | 中继域名候选字段 |
| `TOKENGINE_DEFAULT_MODELS` | `deepseek-ai/DeepSeek-V4-Flash-0731` | 模型列表兜底 |
| `DEEPTUTOR_DESKTOP_SKIP_LOGIN` | — | 设为 `1` 跳过登录门控（离线/开发用） |

## 5. 文件位置

| 内容 | 路径 |
| --- | --- |
| 应用日志 | `%LOCALAPPDATA%\EduBuddy\logs\app.log` |
| 令牌（DPAPI 加密） | `%LOCALAPPDATA%\EduBuddy\auth.json` |
| 机器指纹 | `%LOCALAPPDATA%\EduBuddy\machine_id` |
| 端点覆盖 | `%LOCALAPPDATA%\EduBuddy\endpoints.json` |
| DeepTutor 工作区（学习数据） | `%USERPROFILE%\EduBuddy`（`DEEPTUTOR_DESKTOP_HOME` 可改） |
| 写入的模型配置 | `<工作区>\data\user\settings\model_catalog.json` |

## 6. 排查清单

1. **看日志**：`endpoints.json 覆盖生效` / `端点解析结果：api_base=... authorize=... relay=...`
   能确认实际生效的平台地址；`relay base 从平台拉取成功：...` 或
   `平台 userinfo 未返回中继域名（候选字段 ...），回退本地配置：...` 能确认域名来源。
2. **不装客户端也能复现**：用零侵入探针独立跑整条握手。
   ```bash
   python tools/tokengine_callback_probe.py --platform http://127.0.0.1:3000 --port 54321
   ```
3. **平台回送了错误**：客户端日志会记 `platform returned error callback: <code> / <desc>`。
   其中 **`server_error / failed to store code` 已定位到平台侧的确定原因**：见 §6.1。
4. **登录失败后无法重试**：按钮会随登录进行态自动恢复可点，重试使用全新
   `state` / `code_verifier` / 回环端口。
5. **平台对回调地址的要求**：只接受字面量 `http://127.0.0.1:<任意端口>/<任意路径>`；
   `localhost`、`[::1]`、无端口一律被拒（实测）。

### 6.1 `failed to store code` 根因（平台侧，已定位）

链路：`controller/oauth_provider.go` 签发授权码 → `model.CreateOAuthAuthorizationCode()`
→ `DB.Create()` 失败 → 回一个 `server_error / failed to store code`。

已逐一排除客户端侧原因（字段长度全部合法，不是超长导致的 Postgres 报错）：

| 字段 | 客户端实际值长度 | 列定义 | 结论 |
| --- | --- | --- | --- |
| `code` | 64（`hex(32B)`） | `char(64)` | 正好占满，不溢出 |
| `code_challenge` | 43（base64url(sha256)） | `varchar(128)` | 余量充足 |
| `state` | 32（base64url(24B)） | `varchar(128)` | 余量充足 |
| `machine_id` | 32（`uuid4().hex`） | `varchar(128)` | 余量充足 |
| `client_id` | 16（`edubuddy-desktop`） | `varchar(64)` | 余量充足 |
| `redirect_uri` | ~33 | `varchar(512)` | 余量充足 |

**真实原因：`.env` 里 `SKIP_AUTO_MIGRATE=true`，服务启动时跳过了 AutoMigrate，
OAuth 相关的新表从未在目标库建成**（目标是 Postgres `10.8.65.5/new-api2`）。
模型本身在迁移清单里（`model/main.go` 的 `AutoMigrate(&OAuthAuthorizationCode{}, ...)`），
只是没被执行，于是 `DB.Create()` 报 `relation "oauth_authorization_codes" does not exist`。

修法（任选其一，改完需重启平台服务）：

```bash
# A. 让 AutoMigrate 跑一次：临时关掉跳过开关，启动一次服务建表，再改回 true
SKIP_AUTO_MIGRATE=false

# B. 手工在 new-api2 库里按 model/oauth_authorization_code.go 建表
#    （连带 oauth_refresh_tokens / oauth_clients / oauth_device_tokens）
```

> 这一项是**平台侧**的待办，桌面客户端无需改动；客户端侧已验证会把平台错误如实显示与记录。

## 7. 重新打包

**默认只出两个包**（`EduBuddyDesktop.exe` + `EduBuddySetup.exe`）；
便携 zip 默认不制作，需要时加 `-MakePortable`。

```powershell
# 复用已有 runtime staging（快）
powershell -ExecutionPolicy Bypass -File build\build.ps1 -SkipRuntime

# 产物
dist\EduBuddyDesktop.exe     # 原生壳（依赖已装的 runtime 或系统 PATH）
dist\EduBuddySetup.exe       # 点击即装安装器（内置运行时）

# 需要便携包时（+约 8 分钟）
powershell -ExecutionPolicy Bypass -File build\build.ps1 -SkipRuntime -MakePortable
```

> ⚠️ **别把这两步放进智能体 Bash 沙箱**：`make_portable.py` / ISCC 在沙箱里会被限流到
> ~110KB/s（实测同一份压缩任务 4 分钟才做 1%），沙箱外 8 分钟就能跑完。
> 另外后台任务可能被中途杀掉，留下一个**截断的 zip/exe**——所以要在前台等它跑完，
> 并核对产物大小与 `exit=0`（参考 `tools\_tmp_verify\portable_l1.log` 的写法）。

验证冻结包确实包含最新代码（比看时间戳可靠）：

```bash
.venv\Scripts\python.exe tools\_tmp_verify\verify_frozen_auth.py
```

它会用全新 ROOT + 一份 `endpoints.json` 启动 exe，然后断言日志里出现新版独有的行，
并截取登录门控界面。

## 8. 构建产物

三个产物的关系：`installer.iss` 打包 `..\dist\EduBuddyDesktop.exe`；便携包把**同一个 exe**
与 `runtime/` 一起压进 zip。因此校验「顶层 exe 的 sha256 == zip 内嵌 exe 的 sha256」即可
确认三者一致。

| 产物 | 大小 | 说明 |
| --- | --- | --- |
| `dist\EduBuddyDesktop.exe` | 13,957,256 B | 原生壳（PyInstaller onefile），依赖旁边的 `runtime/` |
| `dist\EduBuddyPortable.zip` | 268,639,393 B（21,764 条目） | 解压即用，含内置 Python/Node |
| `dist\EduBuddySetup.exe` | 178,943,259 B | 点击即装安装器（Inno Setup 7） |

exe 的 sha256 前缀：`79bbc5ea1986edc0…`（顶层与便携包内嵌一致）。

### 验证脚本一览（`tools\_tmp_verify\`）

| 脚本 | 验什么 | 结果 |
| --- | --- | --- |
| `e2e_mock_login.py` | 用契约仿真的 mock 平台跑完整登录链（发码→换令牌→域名解析→写 catalog→DPAPI 落盘→退出吊销） | 27/27 |
| `e2e_real_platform_probe.py` | 桌面端生成的授权 URL 在**真实平台**上被接受（302→`/oauth/consent`） | 3/3 |
| `verify_catalog_load.py` | 用便携包内嵌运行时的 DeepTutor `ModelCatalogService` 读回 catalog | 7/7 |
| `e2e_portable_boot.py` | 便携包在**全新工作区**首次启动 → 停在登录门控 | — |
| `verify_frozen_auth.py` | 冻结包确实包含最新代码（断言新版独有的日志行） | 9/9 |
| `test_auth.py` | auth 模块单元/集成回归 | 56 项 |

> `verify_catalog_load.py` 必须用**便携包内嵌的 python** 运行（那份环境带齐 DeepTutor 依赖）：
> ```powershell
> <portable>\runtime\python\python.exe tools\_tmp_verify\verify_catalog_load.py
> ```

> **注意（踩过的坑）**：智能体沙箱对「单轮批量删除 >50 个文件」有保护
> （`CODEBUDDY_SAFE_DELETE_BULK_GUARD`），会让 `shutil.rmtree` 直接失败、
> 并可能连带把子进程拖挂（日志里出现 `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]`，
> 一度被误判成 DeepTutor 的缺陷）。**这不影响发布形态**——用户正常双击没有这个 shim。
> 写测试时请**改用带时间戳的全新目录**，不要批量删旧目录。
