"""Windows 自定义标题栏 + 原生菜单（EduBuddy 桌面壳）。

设计目标（对齐 WorkBuddy 桌面端观感）：
* 菜单栏位于窗口**第一排**（顶到窗口最上沿），不再挤在系统标题栏下方；
* 第一排右侧内嵌 最小化 / 最大化 / 关闭 三个窗口按钮（自绘，悬停高亮，
  关闭钮悬停红色）；
* 保留系统级体验：边缘拖拽缩放（WS_THICKFRAME）、Aero Snap（Win+方向键、
  拖到屏幕边缘）、最小化/还主动画、Alt+F4、任务栏预览；
* 菜单条空白处：按下拖动窗口、双击最大化/还原、右键弹系统菜单。

实现要点：
* 用 ctypes 给 BrowserForm 装轻量 WndProc 钩子，仅拦截 ``WM_NCCALCSIZE``：
  把客户区扩展到整个窗口矩形 → 系统标题栏在窗口可见**之前**就消失
  （无闪烁）。窗口样式里的 WS_CAPTION / WS_THICKFRAME / WS_MAXIMIZEBOX
  全部保留，因此缩放、贴靠、动画这些系统行为原封不动。
* 菜单走 pywebview 的 ``Menu`` / ``MenuAction`` / ``MenuSeparator`` 通道，
  渲染成 ``MenuStrip``（Dock=Top）——与 pywebview 原版 ``set_window_menu``
  同一条久经验证的路径；客户区扩大后菜单条自然顶到第一排。
* 菜单条自身的 WndProc 钩子只处理 ``WM_NCHITTEST``：顶部/左右 8px 边框
  区域返回 HT*，让被菜单条盖住的窗口边缘仍可拖拽缩放（按钮区除外——
  按钮区返回 HTCLIENT，保证整块按钮可点，与系统原生一致）。
* 窗口按钮**不是** ToolStripMenuItem：右对齐项的视觉序与集合序相反
  （曾出现 ✕□─ 颠倒），尺寸/贴边也不受控。按钮矩形按 DPI 换算
  （46×36 逻辑像素，对齐 VS Code / Chrome / Win11 惯例）直接绘制在
  菜单条画布右上角，鼠标事件手动命中测试，悬停高亮、关闭钮红色。

历史教训：此前用 ``Form.Menu = MainMenu()``（经典菜单）+ 在 loaded 事件里
做 ctypes 调用清理图标，冻结版中会引发 Python 工作线程集体冻结
（引导/尾随线程静默死亡，见 2026-09-23 日志）——本模块不再触碰这两条路径。
"""
from __future__ import annotations

import ctypes
import logging
import os
import threading

from webview.menu import Menu, MenuAction, MenuSeparator

log = logging.getLogger("dt.native_menu")

# 延迟绑定的 WinForms / System.Drawing 类型（install_windows_shell_menu 里赋值）。
# 不能在模块顶部 import —— pythonnet 的 System 命名空间要先 import clr，
# 而顶层 clr 导入会破坏非 Windows 平台兼容性。
_WF = None
_Color = _Font = _Pen = _SolidBrush = _Size = _SmoothingMode = None
_Func = _Type = None

# --------------------------------------------------------------------------- #
# Win32 常量与 ctypes 原型                                                      #
# --------------------------------------------------------------------------- #
GWL_WNDPROC = -4
WM_DESTROY = 0x0002
WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
WM_NCLBUTTONDOWN = 0x00A1
WM_NCLBUTTONDBLCLK = 0x00A3
WM_SYSCOMMAND = 0x0112
HTCAPTION = 2
HTCLIENT = 1
HTLEFT = 10
HTRIGHT = 11
HTTOP = 12
HTTOPLEFT = 13
HTTOPRIGHT = 14
SM_CXSIZEFRAME = 32
SM_CXPADDEDBORDER = 92
TPM_RETURNCMD = 0x0100
TPM_RIGHTBUTTON = 0x0002
# SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED
SWP_FRAME_RECALC = 0x0001 | 0x0002 | 0x0004 | 0x0020


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
    ctypes.c_size_t, ctypes.c_ssize_t,
)

_user32 = ctypes.windll.user32
_user32.CallWindowProcW.restype = ctypes.c_ssize_t
_user32.CallWindowProcW.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
    ctypes.c_size_t, ctypes.c_ssize_t,
]
_user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
_user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
_user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
_user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
if not hasattr(_user32, "SetWindowLongPtrW"):  # 32 位 Python 兜底
    _user32.SetWindowLongPtrW = _user32.SetWindowLongW
    _user32.GetWindowLongPtrW = _user32.GetWindowLongW

# hwnd → (原窗口过程, 回调引用)；全局持有，防止 ctypes 回调被 GC
_HOOKS: dict[int, tuple] = {}


def _frame_thickness() -> int:
    """可缩放窗口的边框厚度（含扩展边），用于贴边命中测试。"""
    try:
        return _user32.GetSystemMetrics(SM_CXSIZEFRAME) + _user32.GetSystemMetrics(
            SM_CXPADDEDBORDER
        )
    except Exception:  # noqa: BLE001
        return 8


def _install_hook(hwnd: int, handler) -> None:
    """替换 hwnd 的窗口过程；handler(msg, wp, lp) 返回非 None 表示已处理。"""
    original = _user32.GetWindowLongPtrW(hwnd, GWL_WNDPROC)

    def _cb(h, msg, wp, lp):
        try:
            result = handler(msg, wp, lp)
        except Exception:  # noqa: BLE001  钩子绝不能把异常抛回消息循环
            log.exception("wndproc handler error (msg=0x%X)", msg)
            result = None
        if result is not None:
            return result
        return _user32.CallWindowProcW(original, h, msg, wp, lp)

    keepalive = _WNDPROC(_cb)
    _HOOKS[hwnd] = (original, keepalive)
    _user32.SetWindowLongPtrW(
        hwnd, GWL_WNDPROC, ctypes.cast(keepalive, ctypes.c_void_p)
    )


def _unhook(hwnd: int) -> None:
    entry = _HOOKS.pop(hwnd, None)
    if entry:
        _user32.SetWindowLongPtrW(hwnd, GWL_WNDPROC, entry[0])


# --------------------------------------------------------------------------- #
# 窗体钩子：WM_NCCALCSIZE 抹掉标题栏                                             #
# --------------------------------------------------------------------------- #
def _make_form_handler(hwnd: int):
    def handler(msg, wp, lp):
        if msg == WM_DESTROY:
            _unhook(hwnd)
            return None
        if msg != WM_NCCALCSIZE or not wp:
            return None
        # 客户区 = 整个窗口矩形；最大化时按边框厚度内缩，避免内容溢出屏幕
        if _user32.IsZoomed(hwnd):
            ins = _frame_thickness()
            if ins > 0 and lp:
                rect = ctypes.cast(lp, ctypes.POINTER(_RECT)).contents
                rect.left += ins
                rect.top += ins
                rect.right -= ins
                rect.bottom -= ins
        return 0

    return handler


# --------------------------------------------------------------------------- #
# 菜单条钩子：让被菜单条盖住的窗口边缘仍可缩放                                    #
# --------------------------------------------------------------------------- #
def _make_strip_handler(hwnd: int, caption_hit=None):
    def handler(msg, wp, lp):
        if msg == WM_DESTROY:
            _unhook(hwnd)
            return None
        if msg != WM_NCHITTEST:
            return None
        cursor = _POINT()
        if not _user32.GetCursorPos(ctypes.byref(cursor)):
            return None
        rect = _RECT()
        if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        m = _frame_thickness()
        x = cursor.x - rect.left
        y = cursor.y - rect.top
        width = rect.right - rect.left
        # 标题栏按钮区优先于缩放带：整块按钮可点（原生标题栏同样如此）
        if caption_hit is not None and caption_hit(x, y):
            return HTCLIENT
        if y < m:
            if x < m:
                return HTTOPLEFT
            if x >= width - m:
                return HTTOPRIGHT
            return HTTOP
        if x < m:
            return HTLEFT
        if x >= width - m:
            return HTRIGHT
        return None

    return handler


# --------------------------------------------------------------------------- #
# 菜单条行为：拖动 / 双击 / 右键系统菜单                                          #
# --------------------------------------------------------------------------- #
def _show_system_menu(form) -> None:
    try:
        hwnd = int(form.Handle.ToInt64())
        hmenu = _user32.GetSystemMenu(hwnd, False)
        if not hmenu:
            return
        pt = _POINT()
        if not _user32.GetCursorPos(ctypes.byref(pt)):
            return
        cmd = _user32.TrackPopupMenu(
            hmenu, TPM_RETURNCMD | TPM_RIGHTBUTTON,
            pt.x, pt.y, 0, hwnd, None,
        )
        if cmd:
            _user32.PostMessageW(hwnd, WM_SYSCOMMAND, cmd, 0)
    except Exception:  # noqa: BLE001
        log.exception("system menu failed")


def _attach_strip_behaviors(form, strip, caption_hit=None) -> None:
    mb = _WF.MouseButtons

    def on_mouse_down(sender, e):
        try:
            on_button = caption_hit is not None and caption_hit(e.X, e.Y)
            hit = strip.GetItemAt(e.X, e.Y)
            if e.Button == mb.Right:
                if hit is None and not on_button:
                    _show_system_menu(form)
                return
            if e.Button != mb.Left:
                return
            if hit is not None or on_button:
                return  # 菜单项 / 标题栏按钮：交给各自的处理逻辑
            hwnd = int(form.Handle.ToInt64())
            _user32.ReleaseCapture()
            msg = WM_NCLBUTTONDBLCLK if e.Clicks == 2 else WM_NCLBUTTONDOWN
            _user32.SendMessageW(hwnd, msg, HTCAPTION, 0)
        except Exception:  # noqa: BLE001
            log.exception("strip mouse handling failed")

    strip.MouseDown += on_mouse_down


# --------------------------------------------------------------------------- #
# 标题栏窗口按钮：直接绘制在菜单条右上角画布上                                    #
# --------------------------------------------------------------------------- #
# 第一性原理：窗口按钮是窗口 chrome，不是菜单项。ToolStripMenuItem 的右对齐
# 布局视觉序与集合序相反（曾出现 ✕□─ 颠倒）、尺寸/贴边不受布局器摆布，
# 因此改为：按钮矩形按 DPI 换算（46×36 逻辑像素，对齐 VS Code / Chrome /
# Win11 惯例，全高贴右上角），手动命中测试 + 自绘悬停态。
_CAPTION_H_DIP = 36     # 标题栏高度（逻辑像素，96 DPI 基准）
_BTN_W_DIP = 46         # 单个按钮宽度（逻辑像素）
_GLYPH_BOX_DIP = 10.0   # 符号盒边长（─ □ ✕ 共用）

_BTN_ORDER = ("min", "max", "close")


def _dpi_scale(form) -> float:
    """窗口当前 DPI / 96。进程为 system-DPI-aware，该值会话内恒定。"""
    try:
        hwnd = int(form.Handle.ToInt64())
        getter = getattr(_user32, "GetDpiForWindow", None)
        dpi = int(getter(hwnd)) if getter else 0
        if not dpi:
            dpi = int(_user32.GetDpiForSystem()) if hasattr(
                _user32, "GetDpiForSystem"
            ) else 96
        return max(dpi, 96) / 96.0
    except Exception:  # noqa: BLE001
        return 1.0


def _attach_window_buttons(form, strip):
    """在菜单条上绘制并接管 最小化/最大化/关闭 按钮；返回命中测试函数。"""
    state = {"scale": _dpi_scale(form), "hover": None}

    def rects():
        """右上角三个按钮矩形（strip 客户区坐标，顺序即视觉序 ─ □ ✕）。"""
        s = state["scale"]
        h = max(1, round(_CAPTION_H_DIP * s))
        w = max(1, round(_BTN_W_DIP * s))
        right = strip.Width
        return {
            "min": (right - 3 * w, 0, right - 2 * w, h),
            "max": (right - 2 * w, 0, right - w, h),
            "close": (right - w, 0, right, h),
        }

    def hit(x, y):
        for name, (l, t, r, b) in rects().items():
            if l <= x < r and t <= y < b:
                return name
        return None

    def on_paint(sender, e):
        try:
            g = e.Graphics
            s = state["scale"]
            smax = form.WindowState == _WF.FormWindowState.Maximized
            for kind in _BTN_ORDER:
                l, t, r, b = rects()[kind]
                clip = e.ClipRectangle
                if (clip.Right <= l or clip.Left >= r
                        or clip.Bottom <= t or clip.Top >= b):
                    continue
                hot = state["hover"] == kind
                bg = None
                if kind == "close" and hot:
                    bg = _Color.FromArgb(0xE8, 0x11, 0x23)
                elif hot:
                    bg = _Color.FromArgb(0xE9, 0xE9, 0xE9)
                if bg is not None:
                    g.FillRectangle(_SolidBrush(bg), l, t, r - l, b - t)
                g.SmoothingMode = _SmoothingMode.AntiAlias
                fg = (
                    _Color.White
                    if (kind == "close" and hot)
                    else _Color.FromArgb(32, 32, 32)
                )
                pen = _Pen(fg, max(1.0, round(s * 2) / 2.0))
                cx, cy = (l + r) / 2.0, (t + b) / 2.0
                half = _GLYPH_BOX_DIP * s / 2.0
                if kind == "min":
                    g.DrawLine(pen, cx - half, cy, cx + half, cy)
                elif kind == "max":
                    side = half * 0.9
                    if smax:  # 还原态：前矩形 + 右上偏移的后矩形
                        off = max(2.0, 2.0 * s)
                        back_l, back_t = cx - side + off / 2, cy - side - off / 2
                        front_l, front_t = cx - side - off / 2, cy - side + off / 2
                        g.DrawRectangle(pen, int(back_l), int(back_t),
                                        int(side * 2), int(side * 2))
                        under = bg if bg is not None else _Color.White
                        g.FillRectangle(_SolidBrush(under), int(front_l),
                                        int(front_t), int(side * 2), int(side * 2))
                        g.DrawRectangle(pen, int(front_l), int(front_t),
                                        int(side * 2), int(side * 2))
                    else:
                        g.DrawRectangle(pen, int(cx - side), int(cy - side),
                                        int(side * 2), int(side * 2))
                else:  # close
                    g.DrawLine(pen, cx - half, cy - half, cx + half, cy + half)
                    g.DrawLine(pen, cx + half, cy - half, cx - half, cy + half)
        except Exception:  # noqa: BLE001
            log.exception("caption buttons paint failed")

    def on_click(sender, e):
        try:
            if e.Button != _WF.MouseButtons.Left:
                return
            kind = hit(e.X, e.Y)
            if kind == "min":
                form.WindowState = _WF.FormWindowState.Minimized
            elif kind == "max":
                form.WindowState = (
                    _WF.FormWindowState.Normal
                    if form.WindowState == _WF.FormWindowState.Maximized
                    else _WF.FormWindowState.Maximized
                )
            elif kind == "close":
                form.Close()
        except Exception:  # noqa: BLE001
            log.exception("caption button action failed")

    def on_move(sender, e):
        try:
            kind = hit(e.X, e.Y)
            if kind != state["hover"]:
                state["hover"] = kind
                strip.Invalidate()  # MenuStrip 默认双缓冲，整条重绘无闪烁
        except Exception:  # noqa: BLE001
            pass

    def on_leave(sender, e):
        try:
            if state["hover"] is not None:
                state["hover"] = None
                strip.Invalidate()
        except Exception:  # noqa: BLE001
            pass

    strip.Paint += on_paint
    strip.MouseClick += on_click
    strip.MouseMove += on_move
    strip.MouseLeave += on_leave

    def sync(_s=None, _e=None):
        """窗口尺寸/状态变化时：按钮区随宽度重算 + 重绘（含最大化图标切换）。"""
        try:
            h = max(1, round(_CAPTION_H_DIP * state["scale"]))
            w = max(1, round(_BTN_W_DIP * state["scale"]))
            if abs(strip.Height - h) > 1:
                strip.Height = h
            strip.Padding = _WF.Padding(
                round(6 * state["scale"]), 0, 3 * w, 0
            )
            strip.Invalidate()
        except Exception:  # noqa: BLE001
            pass

    sync()
    strip.Resize += sync
    return hit


# --------------------------------------------------------------------------- #
# 菜单条构建                                                                    #
# --------------------------------------------------------------------------- #
def _convert_entry(entry):
    """pywebview Menu 项 → WinForms ToolStripMenuItem（递归）。"""
    if entry is None or isinstance(entry, MenuSeparator):
        return _WF.ToolStripSeparator()
    if isinstance(entry, MenuAction):
        item = _WF.ToolStripMenuItem(entry.title)
        item.Click += (
            lambda _s, _e, fn=entry.function: threading.Thread(
                target=fn, daemon=True
            ).start()
        )
        return item
    if isinstance(entry, Menu):
        item = _WF.ToolStripMenuItem(entry.title)
        for child in entry.items:
            converted = _convert_entry(child)
            if converted is not None:
                item.DropDownItems.Add(converted)
        return item
    return _WF.ToolStripMenuItem(str(entry))


def _build_menu_strip(form, menu_list):
    scale = _dpi_scale(form)
    cap_h = max(1, round(_CAPTION_H_DIP * scale))
    btn_w = max(1, round(_BTN_W_DIP * scale))

    strip = _WF.MenuStrip()
    strip.Dock = _WF.DockStyle.Top
    strip.GripStyle = _WF.ToolStripGripStyle.Hidden
    # AutoSize=True 时条带高度被字体行高决定（约 30 物理像素），视觉过窄；
    # 显式按 DPI 设为标准标题栏高度（Win11 风 36 逻辑像素）。
    strip.AutoSize = False
    strip.Height = cap_h
    strip.ShowItemToolTips = False
    strip.BackColor = _Color.White
    strip.Font = _Font("Microsoft YaHei UI", 9.0)
    # 右侧让出 3 个按钮宽度，菜单项永远不会钻到按钮下面
    strip.Padding = _WF.Padding(round(6 * scale), 0, 3 * btn_w, 0)

    for entry in menu_list or []:
        converted = _convert_entry(entry)
        if converted is not None:
            # 锚定上下边缘 → 条带加高后菜单文字垂直居中，而不是贴顶
            try:
                converted.Anchor = (
                    _WF.AnchorStyles.Top | _WF.AnchorStyles.Bottom
                )
            except Exception:  # noqa: BLE001  布局兜底不拦构建
                pass
            strip.Items.Add(converted)

    # 绘制顺序 = 订阅顺序：白底 → 窗口按钮 → 底部分隔线（最后覆盖保证连贯）
    brush = _SolidBrush(_Color.White)

    def on_strip_paint(sender, e):
        try:
            e.Graphics.FillRectangle(brush, e.ClipRectangle)
        except Exception:  # noqa: BLE001
            pass

    strip.Paint += on_strip_paint

    caption_hit = _attach_window_buttons(form, strip)
    _attach_strip_behaviors(form, strip, caption_hit)

    line_brush = _SolidBrush(_Color.FromArgb(0xE0, 0xE0, 0xE0))

    def on_border_paint(sender, e):
        try:
            g = e.Graphics
            y = strip.Height - 1
            g.FillRectangle(
                line_brush, e.ClipRectangle.Left, y, e.ClipRectangle.Width, 1
            )
        except Exception:  # noqa: BLE001
            pass

    strip.Paint += on_border_paint

    form.Controls.Add(strip)

    # 菜单条自己的句柄钩子：顶边/左右边缘保持系统缩放（按钮区除外）
    try:
        hwnd_strip = int(strip.Handle.ToInt64())
        _install_hook(
            hwnd_strip, _make_strip_handler(hwnd_strip, caption_hit)
        )
    except Exception:  # noqa: BLE001
        log.exception("strip hook failed (top-edge resize may be limited)")

    return strip


def install_windows_shell_menu() -> bool:
    """替换 pywebview 的菜单安装路径，并给窗体装自定义标题栏钩子。

    Returns whether the backend was patched. On non-Windows platforms this is
    an inert no-op; the shell currently targets Windows.
    """
    global _WF, _Color, _Font, _Pen, _SolidBrush, _Size, _SmoothingMode, _Func, _Type
    if os.name != "nt":
        return False

    try:
        import webview.platforms.winforms as winforms
    except Exception:  # noqa: BLE001 - shell menu remains optional
        log.warning("WinForms backend unavailable; using pywebview default menu")
        return False

    # pythonnet 的 System 命名空间此刻已由 winforms 模块激活，可安全导入
    from System import Func as _Func_tmp, Type as _Type_tmp
    from System.Drawing import (  # noqa: F401
        Color as _Color_tmp,
        Font as _Font_tmp,
        Pen as _Pen_tmp,
        SolidBrush as _SolidBrush_tmp,
        Size as _Size_tmp,
    )
    from System.Drawing.Drawing2D import SmoothingMode as _SmoothingMode_tmp

    _WF = winforms.WinForms
    _Func, _Type = _Func_tmp, _Type_tmp
    _Color, _Font = _Color_tmp, _Font_tmp
    _Pen, _SolidBrush, _Size = _Pen_tmp, _SolidBrush_tmp, _Size_tmp
    _SmoothingMode = _SmoothingMode_tmp

    def set_window_menu(self, menu_list):
        def _build():
            try:
                strip = _build_menu_strip(self, menu_list)
                log.info(
                    "chrome: menu strip ready (%d top items + window buttons)",
                    len(menu_list or []),
                )
            except Exception:  # noqa: BLE001
                log.exception("menu strip build failed")

        if self.InvokeRequired:
            self.Invoke(_Func[_Type](_build))
        else:
            _build()

    original_form_init = winforms.BrowserView.BrowserForm.__init__

    def browser_form_init(form, *args, **kwargs):
        original_form_init(form, *args, **kwargs)
        try:
            form.ShowIcon = False
            if not form.Text:
                form.Text = "EduBuddy"  # Alt-Tab / 任务栏悬停显示
            hwnd = int(form.Handle.ToInt64())
            _install_hook(hwnd, _make_form_handler(hwnd))
            # 窗口可见前重算非客户区 → 标题栏无声消失，无闪烁
            _user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_FRAME_RECALC)
            log.info("chrome: custom frame installed (hwnd=0x%X)", hwnd)
        except Exception:  # noqa: BLE001  失败则退回系统标题栏，不影响启动
            log.exception("custom frame install failed; system title bar kept")

    winforms.BrowserView.BrowserForm.__init__ = browser_form_init
    winforms.BrowserView.BrowserForm.set_window_menu = set_window_menu
    return True
