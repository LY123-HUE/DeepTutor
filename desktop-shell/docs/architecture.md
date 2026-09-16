# DeepTutor Desktop — 架构说明

> target / audience：接手这个桌面壳或想改造成自己项目的人。

## 1. 目标与约束

把 DeepTutor 的 **CLI + 本地 Web 服务** 封装成"双击即用"的桌面应用：

- 零命令行：不要求用户懂 `deeptutor start` 或安装 Python/Node。
- 进程生命周期干净：关窗口 = 停掉后端+前端整棵进程树。
- 可离线分发：内置运行时，用户机器无需网络也能装/用（首次配 LLM 除外）。
- 快速落地：优先 pywebview + PyInstaller + Inno 这套"壳"技术栈，不改 DeepTutor 本体。

## 2. 运行时拓扑（设计运行时由三部分组成）

```
┌────────────────────────────────────────────────────────────┐
│  桌面壳  DeepTutorDesktop.exe  (PyInstaller onefile)         │
│  pywebview(WebView2)  ←── 原生窗口，加载 127.0.0.1:3782      │
│  引导线程: 运行时→spawn→健康检查→navigate                    │
│  生命周期: 关闭窗口→taskkill /T 停整棵树                    │
├────────────────────────────────────────────────────────────┤
│  内部运行时(离线)  runtime/                                  │
│   python/  ← python.org embeddable(relocatable)            │
│     run_deeptutor.py (entry shim, 无 Scripts/.exe)          │
│     Lib/site-packages/deeptutor*  (1.6.x, 含完整 Next.js)    │
│   node/    ← Node 22 LTS(portable, deeptutor 拉起前端用)    │
├────────────────────────────────────────────────────────────┤
│  DeepTutor 本体:  deeptutor start --home <工作区> --no-browser
│   后端 uvicorn :8001   +   Next.js standalone :3782(代理/ws)
└────────────────────────────────────────────────────────────┘
```

**为什么用 embeddable Python 而不是 venv？**
venv 里的 `pyvenv.cfg`/绝对路径搬迁即坏；embeddable 分发天生可平移，
配合 `run_deeptutor.py` 这一层 shim 解决"embeddable 没有 console-script .exe"的问题。

**运行时解析顺序**（`runtime.py::ensure_runtime`）：
1. 内置/安装的 `runtime/`（`%LOCALAPPDATA%\DeepTutorDesktop\runtime` 或 exe 旁 `runtime\`）
2. 系统 PATH 上的 `deeptutor` + `node`
3. 都没有 → 用系统 python 建独立 venv 自动 `pip install -U deeptutor`（联网）

## 3. 启动时序

```
双击 exe
 └─ 单实例检查(app.lock)
 └─ create_window(启动画面) → webview.start(...)
     └─ 引导线程:
         1 ensure_runtime()        → (deeptutor_cmd, node_dir)
         2 建工作区 ~/DeepTutor
         3 spawn: deeptutor start --home ... --no-browser   ← CREATE_NO_WINDOW
         4 wait_ready(): 轮询 GET 127.0.0.1:3782 (≤150s)
            run: proc 挂了 → 亮错误页 + 日志
         5 load_url(3782)          → 进入应用
 └─ window.events.closed
     └─ taskkill /PID <deeptutor> /T /F → 后端+前端一起停
```

要点：
- `CREATE_NO_WINDOW` 让子进程无控制台窗口；状态(阶段/文本/subprocess日志)经
  `pywebview.api.status()` 轮询推到启动画面，避免跨线程直接操作 DOM。
- `_as_cmd()` 把"deeptutor 可执行"归一成 argv 前缀：系统 exe 或
  `[embeddable python, run_deeptutor.py]` 皆可。
- 关窗口的收尾放在 `webview.start()` 返回后的 `finally`，双保险。

## 4. 打包

| 产物 | 工具 | 说明 |
|---|---|---|
| `EduBuddyDesktop.exe` | PyInstaller onefile(windowed) | 壳本体，无内置运行时(可选 runtime.zip 内嵌) |
| 离线运行时 | `tools/build_runtime.py --no-zip` | embeddable python + pip --target deeptutor + portable node，产出 `runtime-build/staging` |
| `Portable.zip` | `tools/make_portable.py` | **从 staging 直接打包（无需中间拷贝）+ deflate 压缩**，解压即用；体积约为解压后一半 |
| `DeepTutorSetup.exe` | Inno Setup 7 `installer.iss` | 向导 + 快捷方式 + 卸载器；`runtime/` 树直接装进 `{app}\runtime`(recursesubdirs)，无需解压 |

> 默认裁剪优化：安装到 embeddable python 的 deeptutor 依赖闭包很大(尤其 faiss 63MB、
> litellm 72MB、PyMuPDF 48MB)。`build_runtime.py` 之后默认移除 deeptutor 已弃用的
> `litellm`、可选加速 `hf_xet`、AWS `boto3/botocore`、pip 生成的 `bin/` 脚本与帮助文件，
> 依赖闭包从 ~800MB 降到 ~670MB；再配合 zip deflate 压缩，安装包约 ~350MB。
> 裁剪后已通过 deeptutor 全量导入 + `doctor` 冒烟验证。

构建命令见 `build/build.ps1`。

## 5. 关键取舍 / 后续可做

- **用 pywebview 而非 Electron/Tauri**：Python 单栈、二进制小、WebView2 系统自带，
  半小时能跑通；代价是自定义外壳能力弱一些。
- **deeptutor 不打进 exe**：为的是跟上 `pip install -U deeptutor` 的版本；需要"随发版锁版本"
  可把 wheel 固化进 runtime 并定期重建。
- 后续可加：托盘常驻 + 后台保活、开机自启、多工作区切换、端口冲突时自动换端口。
