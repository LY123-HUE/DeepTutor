# EduBuddy 桌面客户端工程方案（基于本地 DeepTutor 源）

> 版本：2026-09-16 · 针对 `D:\studio\DeepTutor`（v1.6.8+3）继续开发桌面客户端的工程梳理

## 0. 现状盘点（实测）

| 事实 | 证据 |
|---|---|
| 本地源已升到 **v1.6.8**（且带 3 个自研修复提交） | `git describe` = `v1.6.8-3-g897fce52`；`deeptutor/__version__.py` = `1.6.8` |
| 现在打包的运行时来自 **PyPI 的 1.6.7** | `runtime-build/staging/.../deeptutor-1.6.7.dist-info`；`build_runtime.py` 里是 `pip install deeptutor` |
| 你机器上跑的安装更旧（侧栏 v1.6.5） | 截图；是旧版 setup 装的，与最新运行时都无关 |
| 前端版本徽章读后端 `status.current_version` ← `deeptutor/__version__.py` | `web/lib/version.ts` + `components/sidebar/VersionBadge.tsx` |
| 前端是 `web/`（Next.js, npm, `output:"standalone"`），构建后由 `scripts/prepare_web_package.py` 填进 `deeptutor_web/` | 源码实测；本地源**还没构建**（无 node_modules、无 .next） |

**核心问题**：桌面壳走的是 `pip install deeptutor`（PyPI 官方包），所以
① 版本滞后；② **你们在 DeepTutor 上的 3 个自研修复根本没进安装包**。
只要还在从 PyPI 装，每次上游本地源更新，安装包就必然是旧货。

## 1. 目标工程结构（二选一）

### 方案 A（推荐）：monorepo —— 把壳工程并入 DeepTutor 仓库

```
D:\studio\DeepTutor\
├── deeptutor/            # 后端（含 __version__.py）
├── web/                  # 前端 Next.js 源
├── scripts/              # prepare_web_package.py 等
├── desktop-shell/        # ← 现在的 D:\studio\DeepTutor\desktop-shell 整体迁入
│   ├── desktop/          # 壳代码（main/inject/auth/...）
│   ├── tools/            # build_runtime.py / make_portable.py / make_icon.py
│   ├── build/            # spec / installer.iss / build.ps1
│   └── docs/
└── ...
```

- 好处：版本天然同源，`__version__.py` 一处改，壳、前端、安装包同步；
  一次 CI 就能产出全套；不需要任何"源路径"配置。
- 迁移成本：壳里所有 `STAGE / ROOT / DIST` 都是按脚本相对位置解析的，
  整体平移即可；把 `dist/`、`runtime-build/`、`.venv/` 加进 DeepTutor 的 `.gitignore`。

### 方案 B：两仓库 + 源路径配置

壳留在独立仓库，`build_runtime.py` 增加 `--deeptutor-source <path>` 参数指向本地源
（默认值可放 `.edubuddy-source.json` 或环境变量）。耦合更低，但每次换人/换机都要配。

> 建议选 **A**。你们的自研修复与壳是同一发布节奏，放一起最不容易出错。

## 2. 版本对齐的构建链路（从本地源打包）

```
┌─ D:\studio\DeepTutor\web
│   npm ci && npm run build            # 产出 web/.next/standalone（需 Node 20+）
├─ python scripts/prepare_web_package.py
│                                      # 把 standalone + static + public 填进 deeptutor_web/
├─ pip install --no-compile --target <embeddable>/Lib/site-packages D:\studio\DeepTutor
│                                      # pip 从本地源构建 wheel（MANIFEST 已含 deeptutor_web），依赖照常解析
├─ 版本门禁（新增，必须做）
│   staging 里 import deeptutor; assert __version__ == 本地源 deeptutor/__version__.py
│   不一致直接 fail —— 防止"又装成了 PyPI 版本"这类静默回退
└─ PyInstaller 壳 exe → ISCC 安装器      # 之后流程不变（默认只出 2 个包）
```

`tools/build_runtime.py` 需要的改造（很小）：

1. `install_deeptutor()` 的 pip 目标从字符串 `"deeptutor"`（PyPI）换成
   本地路径 `D:\studio\DeepTutor`（可配置，`--deeptutor-source`）。
2. 前置 `ensure_frontend()`：本地源 `web/.next/standalone/server.js` 不存在时
   自动跑 `npm ci` + `npm run build` + `prepare_web_package.py`；
   存在则跳过（可加 `--skip-frontend-build` 强制跳过 / `--force-frontend-build` 强制重建）。
3. 增加第 4 步的**版本门禁断言**。

> 注意：`deeptutor_web/` 在本地源里目前只有 `__init__.py`，**构建前端这步是第一次必须做的**，
> 不做的话 wheel 里就没有前端，`deeptutor start` 起不了前端。

## 3. 右键菜单设计（登录后的用户）

### 3.1 技术方案（复用已验证的注入机制）

和左下角「登录」按钮同一套机制（`desktop/inject.py`）：

- 页面侧注入：拦截 `contextmenu` 事件（`preventDefault()`），在鼠标位置渲染自绘菜单
  （样式对齐 DeepTutor 浅色主题：白底、圆角、细阴影、hover 高亮）。
- 点击菜单项 → 把 `menu:<action>:<ts>` 写进隐藏事件槽 → Python 侧 `evaluate_js` 轮询消费。
- 点菜单外任意处 / Esc / 页面滚动 → 关闭菜单。
- **按登录态渲染两套菜单**，文案从 `AuthManager.status()` 实时取。

### 3.2 菜单项（v1 草案）

**已登录**：

| 项 | 行为 |
|---|---|
| `138****8602 · 余额 ¥x · N 个模型` | 只读头行（或点击展开明细弹层） |
| 刷新可用模型 | 重拉 `userinfo`+`/api/status` → 重写 `model_catalog.json` → 页面 reload |
| 打开 Tokengine 平台 | 系统浏览器打开平台域名 |
| 复制 API 地址 | 把 `relay_base` 写入剪贴板（**不提供复制业务 token**，避免泄露；要的话做成二次确认） |
| 切换账号 | 重新走登录流程（新 PKCE/端口） |
| 退出登录 | 吊销 refresh + device token → 清本地 DPAPI 凭证 → 移除 catalog 连接 → 按钮回到「登录」 |
| 关于 EduBuddy | 壳版本 + deeptutor 版本 + 中继域名 |

**未登录**：登录 Tokengine / 打开 Tokengine 平台 / 关于 EduBuddy。

### 3.3 Python 侧需要的能力

| 方法 | 状态 |
|---|---|
| `logout()`（吊销） | ✅ 已有 |
| `auth_status()`（账号/模型/域名） | ✅ 已有 |
| `start_login()` | ✅ 已有 |
| `refresh_models()`（重拉 + 重写 catalog） | 🔨 新增小方法（复用 watchdog 里现成的拉取+写入逻辑，抽成函数） |
| `open_platform()` | 🔨 一行 `webbrowser.open` |

### 3.4 一个待定项：菜单生效范围

- **方案①（建议）**：整个窗口任意处右键都弹（WebView2 默认右键菜单本来就没用，
  正好接管，把 Tokengine 相关操作统一收口）。
- 方案②：只在左下角账号按钮上右键才弹（更克制，但入口隐蔽）。

## 4. 实施顺序建议

| # | 事项 | 工作量 | 说明 |
|---|---|---|---|
| 1 | 仓库整合（迁入 monorepo） | 小 | 与 #2 一起做最顺 |
| 2 | 版本对齐 1.6.8 | ~半天 | 前端 npm 构建（首次要装依赖，10–20 分钟）+ build_runtime 改造 + 版本门禁 + 重出 2 个包 |
| 3 | 右键菜单 | ~1 天 | 注入层有现成模式；含 refresh_models 等小方法 |
| 4 | （可选）CI 化 | 后续 | 一次构建出全套产物 |

## 5. 上游更新怎么合并（fork 工作流，与方案 A 兼容）

**结论：方案 A 不增加合并难度。** git 合并冲突只发生在“双方改了同一个文件”——
`desktop-shell/` 是纯新增目录，上游永远不会有这个路径，合并时原样带入、零冲突。
唯一的风险源是**修改上游文件**（如已有的 mastery 修复改了 8 个文件），该风险在 A/B 下完全相同。

当前隐患（实测）：origin 直指 `HKUDS/DeepTutor`，无推送权限，
3 个自研提交只存在于本机。接线成标准 fork 工作流：

```bash
git remote rename origin upstream        # 上游 = HKUDS，只拉不推
git remote add origin <自己的fork>        # 推送到自己的仓库
git push -u origin master                # 先把自研提交保护起来

# 上游有更新时：
git fetch upstream && git merge upstream/master   # merge 优于 rebase（保留合并点，便于审计）
```

工程纪律：自研改动**尽量纯新增**（新文件/新目录）；必须动上游文件时，
登记到 `desktop-shell/docs/PATCHES.md`（文件、改动点、原因），冲突时秒判归属。

## 6. 版本纪律（写进工程约定）

- 上游源版本只看 `deeptutor/__version__.py`；壳构建后**必须**通过版本门禁。
- 壳自己的版本（`desktop/__init__.py` 的 `__version__`）与 deeptutor 版本分开：
  安装包版本号建议形如 `0.2.0+dt1.6.8`（壳版本 + 打包的 deeptutor 版本），排查问题时一眼定位。
- 出包只认 **2 个**：`EduBuddyDesktop.exe` + `EduBuddySetup.exe`（便携 zip 按需 `-MakePortable`）。
