# Tokengine OAuth 集成接口规范

> 面向：Tokengine 平台后端 / EduBuddy 桌面端联调
> 版本：v0.1（草案，随联调修订）
> 相关实现：`DeepTutorDesktop/desktop/auth/`（桌面端，已落地）

本规范描述 **EduBuddy 桌面端**（Native 客户端）如何通过 OAuth 授权码 + PKCE 从
**Tokengine 平台**换取**业务令牌**（token 中转站的 API 令牌，形如 `sk-...`），
并注入本地 DeepTutor，实现"登录后立即可对话"。

---

## 1. 协议选型

- **OAuth 2.0 Authorization Code + PKCE（RFC 7636），S256 校验器**。
- **原生应用回环回调（RFC 8252 loopback redirect）**：桌面端在随机端口起本地回调服务。
- 参考：Trae / VS Code 的 `auth_type=local` 登录即为此模式。

### 凭证两层模型（重要）

| 凭证 | 名称 | 产生方 | 使用方 | 用途 | 生命周期 |
|---|---|---|---|---|---|
| 登录会话凭证 | `access_token` / `refresh_token` | Tokengine | EduBuddy 桌面端 | 登录态管理、取回/吊销业务令牌 | 短时，可刷新 |
| **业务令牌** | `token`（`sk-...`） | Tokengine | EduBuddy 写入 DeepTutor model catalog | **调用 `/v1/*` 中继、计费/限额** | 长期，可单设备吊销 |

桌面端只把 `token` 写进 DeepTutor；`access_token` 绝不进入对话请求，也不会落盘明文。

---

## 2. 端点清单

| # | 方法 | 路径 | 说明 |
|---|---|---|---|
| 1 | GET | `/oauth/authorize` | 授权页（手机号+短信验证码登录），校验 PKCE，签发一次性 `code` |
| 2 | POST | `/oauth/token` | `authorization_code` 或 `refresh_token` 换凭证，返回业务令牌 |
| 3 | GET | `/oauth/userinfo` | 校验 `access_token`，返回手机号/余额/可用模型列表（可选但推荐） |
| 4 | POST | `/oauth/revoke` | 吊销业务令牌/会话（退出登录用，可选） |
| 5 | — | `/v1/*` | OpenAI 兼容中继：校验 `Authorization: Bearer <token>`，计费/限额 |

> 平台已有 手机号验证码登录 + 余额计费，因此 1、2、5 是改造重点，3、4 为增强。

---

## 3. GET /oauth/authorize

### 请求参数（桌面端拼装后携带跳转过来）

| 参数 | 必填 | 说明 |
|---|---|---|
| `client_id` | 是 | 固定 `edubuddy-desktop`（平台侧注册） |
| `response_type` | 是 | `code` |
| `redirect_uri` | 是 | 桌面端回环地址，如 `http://127.0.0.1:54321/authorize`（端口随机） |
| `scope` | 是 | 建议 `openid relay` |
| `state` | 是 | 桌面端一次性随机值，回调时原样带回（防 CSRF） |
| `code_challenge` | 是 | S256 后的 PKCE 校验串 |
| `code_challenge_method` | 是 | `S256` |
| `machine_id` / `x_machine_id` | 是 | 设备指纹（桌面端本地持久化 UUID） |
| `auth_type` | 是 | `local` |
| `login_channel` | 是 | `native_desktop` |

### 成功流程

1. 平台校验 `client_id` / `redirect_uri` / `code_challenge` 后渲染登录页（**手机号 + 短信验证码**）。
2. 登录成功 → 平台把登录态与会话绑定，**签发一次性 `code`（短时、一次性、绑定 client+redirect+challenge）**。
3. `302` 到 `redirect_uri`：
   ```
   http://127.0.0.1:54321/authorize?code=NCrmQnV4...&state=st-abc
   ```

### 失败流程（同样 302 回 `redirect_uri`）

```
http://127.0.0.1:54321/authorize?error=access_denied&error_description=...&state=st-abc
```

平台侧安全要求：`code` 一次性、5 分钟内有效；短信验证码一次性 + 限频；
`state` 不强校验（由桌面端回环端校验 `state` 是否等于发起值）。

---

## 4. POST /oauth/token

`Content-Type: application/x-www-form-urlencoded`

### 4.1 授权码换令牌（grant_type=authorization_code）

```http
POST /oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=authorization_code
&code=NCrmQnV4...
&redirect_uri=http%3A%2F%2F127.0.0.1%3A54321%2Fauthorize
&client_id=edubuddy-desktop
&code_verifier=...   # 与授权时 code_challenge 对应的明文校验器
&machine_id=...
```

#### 成功响应 200

```json
{
  "access_token": "oidc 会话凭证（短时）",
  "refresh_token": "换 access_token 用，桌面端 DPAPI 加密落盘",
  "token": "sk-Tokxxxxxxxxxxxxxxxxxxxxxxxx",
  "token_type": "Bearer",
  "expires_in": 900,
  "user": {
    "id": "u_12345",
    "phone": "138****0000",
    "balance": 12.5,
    "plan": "pro",
    "models": ["deepseek-ai/DeepSeek-V4-Flash-0731", "qwen-vl-max"]
  }
}
```

> `token` 即**业务令牌**：桌面端调用 `/v1/*`、写入 DeepTutor 都只用它。
> `user.models` 可选；桌面端拿到后用其覆盖 DeepTutor 的 LLM 模型列表，
> 未返回时回退到配置的默认模型。

#### 失败响应（×），统一错误结构

```json
{ "error": "invalid_grant" }
```

常见：`invalid_grant`（code 无效/已用/过期）、`invalid_client`、
`invalid_request`、`unauthorized_client`（redirect_uri 未注册）。

### 4.2 刷新（grant_type=refresh_token）

```http
grant_type=refresh_token
&refresh_token=...
&client_id=edubuddy-desktop
&scope=openid+relay
&machine_id=...
```

响应同 4.1（`refresh_token` 可轮换，即旋转；建议返回新的）。

### 4.3 平台校验清单（服务端必须做）

- `code_verifier` 与授权时 `code_challenge` 的 SHA-256 匹配；
- `code` 与 `client_id`/`redirect_uri` 绑定；
- refresh_token 轮换 + 复用检测（重复使用即吊销整个会话族）；
- 业务令牌签发时写入 `user_id + device machine_id`，支持按设备吊销。

---

## 5. GET /oauth/userinfo（推荐实现）

`Authorization: Bearer <access_token>`

```json
{
  "sub": "u_12345",
  "phone": "138****0000",
  "balance": 12.5,
  "plan": "pro",
  "models": ["deepseek-ai/DeepSeek-V4-Flash-0731"]
}
```

## 6. POST /oauth/revoke（可选实现）

```http
grant_type=refresh_token   # 或业务令牌
token=sk-...  或  refresh 值
machine_id=...
```

退出登录时桌面端调用，吊销关联的业务令牌/会话。

---

## 7. 中继网关 `/v1/*`

- 请求携带 `Authorization: Bearer <业务令牌>`；
- OpenAI 兼容格式（`/v1/chat/completions` 支持流式 SSE）；
- 按 `token → user` 维度实时计费、限额；
- 令牌无效/过期 → `401`；余额不足/超限 → `402` 或 `429`（建议给可读 message）；
- 支持按 `machine_id` 单设备吊销后立即 401。

---

## 8. 桌面端行为契约（已实现）

| 场景 | 行为 |
|---|---|
| 首次启动且无令牌 | 启动页停在「登录 Tokengine 账号」，登录成功即注入并进入 |
| 已有令牌（含手配） | 直接进入，启动页显示「已配置本机令牌」 |
| 登录中 | 启动页显示「已在浏览器打开登录页…」 |
| 登录成功 | 业务令牌 DPAPI 加密落盘 + 写入 model_catalog（活动 LLM 连接 api_key），立即可对话 |
| 超时/失败 | 启动页提示，可重试 |
| 「稍后再说」 | 直接进入应用，不写任何配置 |
| 退出登录（logout） | 平台侧吊销 + 清空本地存储 |

> 强制跳过登录开发开关：`DEEPTUTOR_DESKTOP_SKIP_LOGIN=1`。
> 平台地址均可用环境变量覆盖（`TOKENGINE_API_BASE` 等），便于联调 mock。

---

## 9. 安全红线

1. **PKCE S256 必须有**（回环地址同机其他进程可访问）。
2. `state` 防 CSRF、`code` 一次性、短信验证码一次性 + 限频。
3. refresh_token 桌面端 DPAPI 加密落盘，`app.log` 严禁明文。
4. 业务令牌最小权限（仅 `/v1` 中继）、绑设备、可单设备吊销、可设额度上限。
5. 平台 token 端点启用 HTTPS 与标准限速。

## 10. 联调冒烟步骤

1. 平台本地起 mock `/oauth/authorize` + `/oauth/token`。
2. `curl` 走通 code→token（手动 PKCE 生成）验证服务端校验。
3. EduBuddy 设 `TOKENGINE_API_BASE=http://127.0.0.1:8000` 启动 → 点登录 → 浏览器 mock 页回调 → 桌面端注入成功 → 首条消息调 `/v1`。
4. 验证：启动页登录态、`auth.json` 加密、model_catalog api_key 更新、退出登录吊销。

---

## 11. 待平台侧确认

- [ ] `client_id=edubuddy-desktop` 注册与 `redirect_uri` 回环通配策略（建议允许 `http://127.0.0.1:*`）
- [ ] 业务令牌命名/前缀（示例 `sk-Tok...`）
- [ ] `userinfo` 的模型字段名（默认 `models`）
- [ ] 计费口径：按 token 实时计费还是按额度预扣
