/*
 * splash 页面 JS 的运行时冒烟测试（node + 极简 DOM 桩）。
 *
 * 背景：2026-09-18 事故——模板里 JS 的 `"\""` 经 Python 三引号转义变成 `"""`，
 * 整个 <script> 语法报错、一行不执行，门控页永远不出现；而静态预览（CSS 预置
 * 门控类）掩盖了死脚本。node --check 只能抓语法，本文件进一步**真实执行**页面
 * 脚本，验证状态机行为：
 *
 *   1. setPhase(login) → needlogin 门控态 + 隐藏进度条
 *   2. setPhase(login, 登录未完成…) → 红字 + 操作按钮行显示
 *   3. poll() 消费 pywebview.api.status() → 相位/文案同步
 *   4. renderAuth() → 登录按钮随 in_progress 禁用/恢复
 *   5. 点击登录按钮 → 调 pywebview.api.login → 按钮进入「正在打开浏览器…」
 *
 * 用法：node tools/splash_js_smoke.js <script.js>
 *   （script.js 由 tools/test_login_gate.py 从 splash_html() 发射结果中提取）
 * 退出码 0 = 全部断言通过。
 */
'use strict';
const fs = require('fs');

const scriptPath = process.argv[2];
if (!scriptPath) { console.error('usage: node splash_js_smoke.js <script.js>'); process.exit(2); }
const script = fs.readFileSync(scriptPath, 'utf8');

let failures = 0;
function check(name, cond, detail) {
  if (cond) { console.log('  \u2713 ' + name); }
  else { failures += 1; console.log('  \u2717 ' + name + (detail ? '  [' + detail + ']' : '')); }
}

// ---- 极简 DOM 桩 -------------------------------------------------------- //
function classList() {
  const set = new Set();
  return {
    toggle(c, force) {
      const on = (force === undefined) ? !set.has(c) : !!force;
      if (on) { set.add(c); } else { set.delete(c); }
      return on;
    },
    contains(c) { return set.has(c); },
    add(c) { set.add(c); },
    remove(c) { set.delete(c); },
  };
}
function makeEl(id) {
  const listeners = {};
  return {
    id, style: {}, innerHTML: '', textContent: '', title: '', disabled: false,
    classList: classList(),
    addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
    async click() {
      (listeners.click || []).forEach((fn) => { try { fn({}); } catch (e) { /* ignore */ } });
      await new Promise((r) => setImmediate(r));
      await new Promise((r) => setImmediate(r));
    },
  };
}
const els = {};
['status', 'detail', 'btnrow', 'bar', 'login', 'hint',
 'btnLogin', 'btnBrowser', 'btnQuit', 'btnPlatform'].forEach((id) => { els[id] = makeEl(id); });

const docClass = classList();
global.document = {
  getElementById: (id) => els[id] || null,
  documentElement: { classList: docClass },
  addEventListener: () => {},
  removeEventListener: () => {},
  querySelectorAll: () => [],
};
global.window = { addEventListener: () => {}, innerWidth: 1280, innerHeight: 860 };

// ---- pywebview 桥桩 ----------------------------------------------------- //
let statusResponse = { phase: 'boot', text: '正在启动本地服务…', detail: '' };
let authResponse = { logged_in: false, in_progress: false, configured: false };
let loginResult = { ok: true };
const calls = { login: 0, platform: 0 };
global.pywebview = {
  api: {
    status: async () => statusResponse,
    auth_status: async () => authResponse,
    login: async () => { calls.login += 1; return loginResult; },
    open_platform: async () => { calls.platform += 1; return { ok: true }; },
    open_browser: async () => ({}),
    quit: async () => ({}),
  },
};

// ---- 执行页面脚本（语法错误在此即炸）------------------------------------ //
// 严格模式下 eval 有独立作用域，显式把状态机函数导出；页面脚本的顶层语句
// （事件绑定、定时器）在 eval 时照常执行。
const splashApi = eval(script + "\n;({ setPhase, poll, renderAuth });");
const setPhase = splashApi.setPhase;
const poll = splashApi.poll;
const renderAuth = splashApi.renderAuth;

// 脚本里 setInterval 会挂住事件循环，断言完手动退出
(async () => {
  // 1) 门控相位：needlogin 开、进度条隐藏、按钮行不显示（非错误）
  setPhase('login', '登录后开始使用', '');
  check('setPhase(login) 开启 needlogin 门控态', docClass.contains('needlogin'));
  check('setPhase(login) 隐藏启动进度条', els.bar.style.display === 'none');
  check('setPhase(login) 非错误不显示操作按钮行', !els.btnrow.classList.contains('show'));

  // 2) 门控失败文案：红字 + 操作按钮行显示
  setPhase('login', '登录未完成：平台拒绝授权', '');
  check('失败文案渲染到状态行', els.status.innerHTML.includes('登录未完成'));
  check('失败时显示操作按钮行', els.btnrow.classList.contains('show'));

  // 3) poll() 消费 status：相位与门控同步
  statusResponse = { phase: 'login', text: '登录后开始使用', detail: '' };
  await poll();
  check('poll() 同步 login 相位（needlogin）', docClass.contains('needlogin'));

  // 4) renderAuth：按钮态跟随 in_progress
  authResponse = { logged_in: false, in_progress: false, configured: false };
  await renderAuth();
  check('空闲时登录按钮可用且文案为「登录」',
        els.btnLogin.textContent === '登录' && els.btnLogin.disabled === false);
  authResponse = { logged_in: false, in_progress: true, configured: false };
  await renderAuth();
  check('登录进行中按钮禁用并显示「等待浏览器…」',
        els.btnLogin.textContent === '等待浏览器…' && els.btnLogin.disabled === true);

  // 5) 点击登录按钮 → 调 login 桥 → 进入打开浏览器态
  els.btnLogin.disabled = false;
  els.btnLogin.textContent = '登录';
  await els.btnLogin.click();
  check('点击登录触发 pywebview.api.login', calls.login === 1);
  check('点击后按钮进入「正在打开浏览器…」',
        els.btnLogin.disabled === true && els.btnLogin.textContent === '正在打开浏览器…');

  // 6) 平台注册入口点击
  await els.btnPlatform.click();
  check('点击平台注册入口调用 open_platform', calls.platform === 1);

  console.log(failures === 0 ? 'SPLASH_JS_SMOKE_OK' : 'SPLASH_JS_SMOKE_FAILED');
  process.exit(failures === 0 ? 0 : 1);
})().catch((e) => { console.error('runner error:', e); process.exit(1); });
