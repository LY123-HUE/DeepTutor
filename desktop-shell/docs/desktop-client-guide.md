# EduBuddy 桌面客户端 · 工程化全局指南

> 面向 Python 新手 · 2026-09-21 版
> 目标：读完这一篇，你能独立完成「拉代码 → 改功能 → 出安装包」的完整闭环。
> 本文档随壳工程存放，将来迁入 `D:\studio\DeepTutor\desktop-shell\docs\` 后路径随之变化。

---

## 1. 先建立全局图景：这个项目由哪几块组成

```
┌─────────────────────────────────────────────────────────────┐
│  EduBuddy 桌面客户端（用户双击的东西）                          │
│                                                              │
│  ┌──────────────┐   装进 exe 里，启动后干三件事：               │
│  │  桌面壳(壳工程) │   ① 在本机拉起 DeepTutor 后端(:8001)+前端(:3782) │
│  │  Python+      │   ② 弹一个原生窗口加载前端页面                │
│  │  pywebview    │   ③ 往页面里注入「登录/账号」按钮 + 下拉菜单，      │
│  └──────────────┘      对接 Tokengine 平台的 OAuth 登录         │
│                                                              │
│  ┌──────────────────────────────────────────────────┐        │
│  │  DeepTutor 本体（教育应用，HKU 开源）                │        │
│  │  后端 = Python (FastAPI, deeptutor 包)             │        │
│  │  前端 = Next.js (web/ 目录, 需要 Node.js)           │        │
│  └──────────────────────────────────────────────────┘        │
│                                                              │
│  ┌──────────────────┐      登录时经系统浏览器访问               │
│  │  Tokengine 平台    │ ←── 你的 OAuth 提供方（发 token/模型表）  │
│  │  (localhost:3000   │                                   │
│  │   或线上域名)       │                                   │
│  └──────────────────┘                                        │
└─────────────────────────────────────────────────────────────┘
```

**一句话理解三层关系**：壳是"盒子"，DeepTutor 是"盒子里跑的软件"，
Tokengine 是"给软件供电（LLM 令牌）的电站"。

### 目前两份代码在哪

| 仓库 | 位置 | 是什么 |
|---|---|---|
| `D:\studio\DeepTutor` | 上游教育项目（v1.6.9）+ 你们的 fork | 后端+前端源码 |
| `D:\studio\DeepTutor\desktop-shell` | 壳工程（我们写的） | 打包器 + 登录对接 + 注入逻辑 |

**当前形态（方案 A，monorepo，2026-09-16 已完成迁移）**：壳工程位于 `D:\studio\DeepTutor\desktop-shell\`，一个仓库管全部，版本天然同源。

---

## 2. Git 工作流（已配好，照抄命令即可）

### 2.1 日常命令速查

```bash
cd D:\studio\DeepTutor

# 每天开工：先看官方有没有更新
git fetch upstream                       # 只下载，不合并
git log --oneline main..upstream/main    # 官方比你多的提交（空=没更新）

# 有更新就合并（用 merge 不用 rebase，保留合并点便于审计）
git merge upstream/main
git push origin main                     # 同步到你的 fork 备份

# 改代码的标准流程（永远不要直接在 main 上写代码！）
git switch -c feat/右键菜单               # 建功能分支
# ...改代码...
git add -A && git commit -m "feat: 右键菜单支持退出登录"
git switch main && git merge feat/右键菜单  # 完成后并回 main
git push origin main
```

### 2.3 分支纪律（新手最容易错的地方）

| 规则 | 原因 |
|---|---|
| **main 永远保持"可发布"状态** | main 直接对应你要打安装包的代码 |
| 功能开发开 `feat/xxx` 分支 | 改坏了直接删分支，main 无损 |
| 改上游文件（如 `deeptutor/`、`web/` 里的）要登记到 `desktop-shell/docs/PATCHES.md` | 将来合并官方更新时，冲突文件一眼判断"是不是自己人改的" |
| **自研改动尽量纯新增**（新文件/新目录） | 新增文件永不冲突；改人家的文件才有冲突风险 |

---

## 3. 环境搭建（一次性的）

### 3.1 需要装什么

| 工具 | 版本 | 用途 | 检查命令 |
|---|---|---|---|
| Python | 3.11+ | 跑壳、跑后端、打包 | `python --version` |
| Node.js | 20+ | 构建 DeepTutor 前端 | `node --version` |
| Git | 任意新版 | 版本管理 | `git --version` |
| Inno Setup 7 | 7.x | 出安装向导 | 装 `dist\EduBuddySetup.exe` 需要它 |

### 3.2 壳工程的 Python 虚拟环境

**什么是 venv（新手必读）**：Python 的"项目专属沙箱"。每个项目一套独立依赖，
互不污染。你看到的 `.venv\` 目录就是它。**所有 python 命令都要用 `.venv` 里的那个**，
而不是系统的——这是新手最常踩的坑（装了包却 import 不到）。

```powershell
cd D:\studio\DeepTutor\desktop-shell   # （迁移后：cd D:\studio\DeepTutor\desktop-shell）

# 首次创建（已创建过就不用重复）
py -3.12 -m venv .venv

# 装依赖
.\.venv\Scripts\python -m pip install -U pip pywebview pillow pyinstaller

# 以后跑任何东西都是这个前缀：
.\.venv\Scripts\python <你的命令>
```

### 3.3 DeepTutor 前端依赖（首次构建必做）

```powershell
cd D:\studio\DeepTutor\web
npm ci                 # 按锁文件装依赖（首次约 5-10 分钟）
```

---

## 4. 日常开发循环

### 4.1 开发态跑起来（快速看效果，不打安装包）

```powershell
cd D:\studio\DeepTutor\desktop-shell
.\.venv\Scripts\python -m desktop.main
```

会弹出一个窗口：启动页 → 自动拉起 DeepTutor → 进应用 → 左下角出现「登录」按钮。
**日志**在 `%LOCALAPPDATA%\EduBuddy\logs\app.log`，有问题第一件事是看它。

### 4.2 改了壳代码 → 重新打 exe

```powershell
cd D:\studio\DeepTutor\desktop-shell
.\.venv\Scripts\python -m PyInstaller --noconfirm --clean build\EduBuddyDesktop.spec
# 产物：dist\EduBuddyDesktop.exe
# dist\runtime.zip 存在时会被内嵌（约 211MB，可独立分发）；
# 不存在则约 14MB，双击后回落系统 PATH 上的 deeptutor（仅开发态够用）。
```

### 4.3 出安装包（默认只出 2 个包）

```powershell
cd D:\studio\DeepTutor\desktop-shell
powershell -ExecutionPolicy Bypass -File build\build.ps1 -SkipRuntime
# 产物：dist\EduBuddyDesktop.exe + dist\EduBuddySetup.exe
# 便携 zip 默认不出；确需时加 -MakePortable（多花约 8 分钟）
```

### 4.4 DeepTutor 本体升级后（比如官方发了 1.6.10，或团队改了代码）

> **2026-09-21 实测修正**：旧版本文档写的是
> `build_runtime.py --no-zip` + `build.ps1 -SkipRuntime`，
> 那样打出的 exe **不含运行时**，双击后会回落到系统 PATH 上的旧版
> deeptutor（横幅显示旧版本号）。另外 `build_runtime.py` 的门禁只比对
> **版本号**——如果版本号没变但代码变了（常见于团队协作合并），它会
> 静默跳过重装，导致"本地修改没进包"。按下面 5 步走可避免这两个坑。

```powershell
# ① 同步上游（版本号一般会变）
cd D:\studio\DeepTutor
git fetch upstream && git merge upstream/main && git push origin main

# ② 重建前端并填进包（前端源码在 web/，改动不会自动生效，必须重建）
cd D:\studio\DeepTutor\web
npm run build
cd D:\studio\DeepTutor\desktop-shell
.\.venv\Scripts\python D:\studio\DeepTutor\scripts\prepare_web_package.py --skip-build

# ③ 重装 deeptutor 到内嵌 Python + 版本门禁 + 生成 dist\runtime.zip
#    --force-deeptutor：先卸旧再装本地源。版本号没变但代码变了时必须加，
#    否则脚本看到"版本相同"会跳过重装（本地修改就丢了）。
#    不加 --no-zip：这样才会生成 runtime.zip，供 ④ 内嵌。
.\.venv\Scripts\python tools\build_runtime.py --force-deeptutor

# ④ rebrand + 重打 exe + 编译 Setup（-SkipRuntime 指"跳过 ②③"，runtime.zip 已就绪）
powershell -ExecutionPolicy Bypass -File build\build.ps1 -SkipRuntime

# ⑤ 验证（产物应为最新版本号）
dist\EduBuddyDesktop.exe --version          # 或双击看左下角横幅
Get-ChildItem dist | Select-Object Name, Length   # exe 约 211MB 才是内嵌了运行时
```

**什么时候能偷懒**：只改了壳工程（`desktop-shell/`）自己的代码、没动
`deeptutor/`/`web/`，则 ②③ 不用跑，直接 ④ 即可。

---

## 5. Tokengine 登录：架构与你要改代码的位置

### 5.1 登录流程（已全部实现并实测）

```
用户点「登录」按钮
  → 壳在本机起一个临时回环服务(127.0.0.1:随机端口)
  → 打开系统浏览器到 Tokengine /oauth/authorize（PKCE 加密握手）
  → 用户在网页登录并点「确认授权」
  → 平台把授权码送回本机回环服务
  → 壳用授权码换取：业务令牌(sk-Tok...) + 用户信息 + 可用模型列表 + 中继域名
  → 写进 DeepTutor 的配置文件(model_catalog.json)，DPAPI 加密落盘
  → 自动刷新页面 → 模型直接可用，用户零手工配置
```

### 5.2 代码地图（改哪个需求动哪个文件）

| 想改什么 | 动哪里 |
|---|---|
| 按钮样式/位置/文案 | `desktop\inject.py`（CSS 与按钮 JS 都在这） |
| 右键菜单样式/菜单项/文案 | `desktop\inject.py`（`_menu_model` 按登录态渲染两套菜单） |
| 菜单动作怎么落地 | `desktop\main.py` 的 `bootstrap`（`menu_actions` 装订表） |
| 登录后写哪些配置 | `desktop\auth\catalog.py` |
| 平台接口地址/客户端ID | `desktop\auth\config.py` |
| 登录时序/重试/吊销逻辑 | `desktop\auth\manager.py` |
| OAuth 请求的构造 | `desktop\auth\client.py` |
| 启动流程/窗口行为 | `desktop\main.py` |
| 启动页样式 | `desktop\splash.py` |
| 打包配置 | `build\EduBuddyDesktop.spec`（PyInstaller）、`build\installer.iss`（安装器） |

### 5.3 指向哪个平台（联调 vs 线上）

```powershell
# 方式一（推荐，装好的包也能改）：写配置文件
# %LOCALAPPDATA%\EduBuddy\endpoints.json
{ "api_base": "http://127.0.0.1:3000" }        # 本地联调
{ "api_base": "https://tokengine.hanyoai.com" } # 线上

# 方式二：环境变量
$env:TOKENGINE_API_BASE = "http://127.0.0.1:3000"
```

**不要**为了换平台地址重新打包——配置文件优先级高于内置默认值。

### 5.4 登录后的账号菜单（v2：点击切换下拉）

左下角「登录/账号」按钮负责两件事：

- **未登录**：点击 → 发起 Tokengine 登录（浏览器授权）。
- **已登录 / 已配置**：点击 → 在按钮上方**弹出**账号菜单；**再点一次收起**。

**不再劫持页面的右键菜单**——聊天区/输入框/任意处的原生右键（复制、粘贴、
检查元素等）原样保留。

| 状态 | 菜单项 |
|---|---|
| 已登录 | 账号头行（脱敏手机号 · 余额 · 模型数）／**刷新可用模型**／打开 Tokengine 平台／复制 API 地址／切换账号／**退出登录**（需再次点击确认）／关于 EduBuddy |
| 未登录 | 登录 Tokengine／打开 Tokengine 平台／关于 EduBuddy |

按钮点击路由由菜单模型携带的 `logged_in`/`configured` 标志决定（页面侧见
`inject.py` 的 ENSURE_JS 与 `__edubuddyMenuUpdate`）。关闭方式：再点按钮 /
Esc / 点菜单外 / 滚动 / 滚轮 / 窗口失焦。

动作接线表在 `main.py` 的 `bootstrap`（`menu_actions`），菜单文案/结构在
`inject.py` 的 `_menu_model`。所有动作都走同一套「隐藏事件槽 + 轮询」通道。

**安全检查**：`复制 API 地址` 只复制 `relay_base`，**不会**复制业务 token（防泄露）；
退出登录会吊销 refresh/device token、清本地凭证、并摘除 catalog 里的
Tokengine 连接（用户手动配的连接不受影响）。决策细节见
`docs/adr/ADR-002-login-context-menu.md`。

---

## 6. 版本对齐

**历史问题（已解决）**：早期安装包里装的是 PyPI 上的旧版（1.6.7），而本地源已更新。
现在 `tools\build_runtime.py` 已改为从本地源码安装，链路是：

```
npm 构建前端 → prepare_web_package.py 填包 → pip install <本地源> → 版本门禁 → 出包
```

**版本门禁**是什么：打包脚本最后会断言"内嵌运行时里的 deeptutor 版本 == 本地源版本"，
不一致直接报错。防止"以为自己打了新版、其实还是 PyPI 旧版"的静默事故。

**门禁的盲区（2026-09-21 实测踩过）**：它只比对版本号。如果版本号没变但代码变了
（团队合并、改 logo 等），门禁照样放行、旧内容留在包里——所以每次合并后要用
`--force-deeptutor` 强制重装（见 §4.4 ③）。

**验证版本是否对齐**（打包后随手查）：

```powershell
# 看安装包里实际装的版本
runtime-build\staging\python\python.exe -c "from deeptutor.__version__ import __version__; print(__version__)"
# 应输出本地源码 deeptutor\__version__.py 里的版本号；输出别的就是没对齐
```

侧栏显示的 `v1.6.x` 徽章读的就是这个值（链路：`__version__.py` → 后端 status 接口 → 前端徽章）。

---

## 7. 新手常见坑（都是真踩过的）

| 症状 | 原因 | 解法 |
|---|---|---|
| `pip install` 了却 import 不到 | 装到系统 Python 了，不在 .venv | 永远用 `.\.venv\Scripts\python -m pip ...` |
| 改了壳代码没生效 | 忘了重新打 exe，跑的还是旧包 | 重跑 PyInstaller（§4.2） |
| 打包脚本在 AI 沙箱里奇慢 | 沙箱限流（~110KB/s） | 长构建放沙箱外跑，前台等完 |
| 后台构建出的 zip/exe 是坏的 | 后台任务被中途杀掉，文件截断 | 看文件大小是否还在增长；重要构建前台跑 |
| 打出的 exe 只有 14MB，显示旧版本号 | 打包时没有 `runtime.zip` 可嵌，运行时回落系统 PATH 的旧 deeptutor | 按 §4.4 跑 ③（别加 `--no-zip`），exe 应约 211MB |
| 版本号对但本地修改没生效 | `build_runtime.py` 门禁只比版本号，版本没变就跳过重装 | ③ 加 `--force-deeptutor` 强制重装 |
| 合并团队改动后打包，改动"消失" | staging 里是旧一轮构建的前端/后端 | §4.4 的 ②③ 每次合并后都要跑一遍 |
| 截图看不到窗口底部内容 | DPI 缩放：截图被裁掉 1/3 | 截图工具先声明 DPI 感知（见技能库） |
| 登录报 `failed to store code` | **平台侧**问题：`SKIP_AUTO_MIGRATE=true` 导致 OAuth 表没建 | 见 `tokengine-integration.md` §6.1 |
| merge 官方更新一堆冲突 | 改过上游文件没登记 | 对照 `PATCHES.md` 逐个判断归属 |

---

## 8. 推荐的工程习惯（Python 新手版）

1. **改前先建分支**：`git switch -c feat/xxx`。改坏了随时弃车保帅。
2. **小步提交**：一个功能拆成几个小 commit，写清做了什么。
3. **提交前自测**：至少跑通 §4.1 开发态 + 看一遍 `app.log` 无 ERROR。
4. **不确定就看日志**：`%LOCALAPPDATA%\EduBuddy\logs\app.log` 是你的眼睛。
5. **读代码顺序建议**：`main.py`（入口）→ `inject.py`（注入）→ `auth/manager.py`（登录编排）。
   每个文件开头都有中文注释讲设计取舍。
6. **别怕 PyInstaller**：它只是把 Python 代码+依赖打成一个 exe。spec 文件就是配置，
   一般不用动；动了记得 `-–clean` 重打。

---

## 附：三个文档的分工

| 文档 | 回答什么问题 |
|---|---|
| 本文（desktop-client-guide.md） | **全局怎么干**：环境、git、日常循环、代码地图 |
| engineering-setup-plan.md | **为什么这么设计**：monorepo 选型、版本对齐链路、账号菜单方案 |
| tokengine-integration.md | **登录怎么实现的**：OAuth 时序、契约细节、排查清单 |
