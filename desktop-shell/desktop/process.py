"""Manage the `deeptutor start` subprocess (backend :8001 + frontend :3782)."""
from __future__ import annotations

import logging
import os
import socket
import subprocess
import threading
import time
import ctypes
import struct
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("dt.process")

# Invocation for a deeptutor that has no console entry-point .exe (e.g. an
# embedded-runtime python). `deeptutor` may be a bare path or this list.
EMBED_INVOKE = (
    "from deeptutor_cli.main import main; raise SystemExit(main())"
)

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DEFAULT_BACKEND_PORT = 8001
DEFAULT_FRONTEND_PORT = 3782
FRONTEND_PATH = "/"  # health-probe path on the frontend server


def _as_cmd(deeptutor) -> list[str]:
    """Normalize a deeptutor executable into an argv prefix."""
    if isinstance(deeptutor, (str, Path)):
        return [str(deeptutor)]
    return [str(arg) for arg in deeptutor]


def _decode_line(raw: bytes) -> str:
    """Decode a stdout chunk; deeptutor emits GBK on zh-CN Windows consoles."""
    try:
        return raw.decode("utf-8").rstrip("\r\n")
    except UnicodeDecodeError:
        try:
            return raw.decode("gbk", errors="replace").rstrip("\r\n")
        except Exception:  # noqa: BLE001
            return raw.decode("latin-1", errors="replace").rstrip("\r\n")


def _listener_pid(port: int) -> int | None:
    """Find the PID listening on a TCP port using the Windows TCP table."""
    if os.name != "nt":
        return None
    AF_INET = 2
    TCP_TABLE_OWNER_PID_LISTENER = 3
    MIB_TCP_STATE_LISTEN = 2
    size = ctypes.c_ulong(0)
    try:
        api = ctypes.windll.iphlpapi
        api.GetExtendedTcpTable(
            None, ctypes.byref(size), False, AF_INET,
            TCP_TABLE_OWNER_PID_LISTENER, 0,
        )
        table = ctypes.create_string_buffer(size.value)
        if api.GetExtendedTcpTable(
            table, ctypes.byref(size), False, AF_INET,
            TCP_TABLE_OWNER_PID_LISTENER, 0,
        ) != 0:
            return None
        count = struct.unpack_from("I", table, 0)[0]
        row_size = 24
        for index in range(count):
            row = struct.unpack_from("IIIIII", table, 4 + index * row_size)
            state, _, local_port, _, _, pid = row
            if state == MIB_TCP_STATE_LISTEN and socket.ntohs(local_port) == port:
                return pid
    except Exception:  # noqa: BLE001
        return None
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True,
            timeout=2,
            creationflags=CREATE_NO_WINDOW, check=False,
        ).stdout
        for line in out.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                return int(line.split()[-1])
    except Exception:  # noqa: BLE001
        pass
    return None


def _http_probe(url: str, timeout: float = 2.0) -> tuple[bool, int | None]:
    """Return (reachable, http_status) for the frontend TCP listener."""
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, 200
    except Exception:  # noqa: BLE001 - any failure means "not ready yet"
        return False, None


# --------------------------------------------------------------------------- #
# 进程归属判定（孤儿清理用）                                                     #
# --------------------------------------------------------------------------- #
def _exe_path_of(pid: int) -> str | None:
    """Return the executable path of a process, or None if unavailable."""
    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFO
        if not handle:
            return None
        try:
            size = ctypes.c_ulong(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value
            return None
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return None


def _parent_pid(pid: int) -> int | None:
    """Return the parent process id via NtQueryInformationProcess, or None."""
    if os.name != "nt":
        return None

    class _PBI(ctypes.Structure):
        _fields_ = [
            ("ExitStatus", ctypes.c_void_p),
            ("PebBaseAddress", ctypes.c_void_p),
            ("AffinityMask", ctypes.c_void_p),
            ("BasePriority", ctypes.c_void_p),
            ("UniqueProcessId", ctypes.c_void_p),
            ("InheritedFromUniqueProcessId", ctypes.c_void_p),
        ]

    try:
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            pbi = _PBI()
            ret_len = ctypes.c_ulong()
            status = ctypes.windll.ntdll.NtQueryInformationProcess(
                handle, 0, ctypes.byref(pbi),
                ctypes.sizeof(pbi), ctypes.byref(ret_len),
            )
            if status != 0:
                return None
            return int(pbi.InheritedFromUniqueProcessId or 0) or None
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return None


def _taskkill_tree(pid: int) -> None:
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, creationflags=CREATE_NO_WINDOW, check=False,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("taskkill failed: %s", exc)


class DeepTutorProcess:
    """Owns the lifetime of the `deeptutor start` child process.

    Frozen, windowed executables cannot reliably read child stdout from a
    pipe. The child therefore writes to a log file and this class tails that
    file with an independently opened handle.
    """

    def __init__(self, deeptutor, home: Path, node_dir: Path | None = None,
                 on_line=None):
        # `deeptutor` is either a path/str (a console entry-point exe) or an
        # argv list (e.g. ["python.exe", "run_deeptutor.py"] for the embedded
        # runtime). Normalize both to a leading argv prefix.
        self.deeptutor_cmd = _as_cmd(deeptutor)
        self.home = Path(home)
        self.node_dir = Path(node_dir) if node_dir else None
        self.on_line = on_line
        self.frontend_port = DEFAULT_FRONTEND_PORT
        self.backend_port = DEFAULT_BACKEND_PORT
        self.proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._child_out = None
        self._tail = None
        self.child_log_path: Path | None = None
        self.frontend_ready = threading.Event()
        self.frontend_launching = threading.Event()

    # ------------------------------------------------------------------ #
    @property
    def frontend_url(self) -> str:
        return f"http://127.0.0.1:{self.frontend_port}"

    # ------------------------------------------------------------------ #
    def _kill_stale_listeners(self) -> None:
        """清掉占用本应用端口的「本运行时」孤儿进程（越界进程一律不动）。

        背景：壳被强杀/崩溃时，deeptutor 的后端/前端孙进程会脱离进程树
        存活，占住 8001/3782；下次启动 deeptutor 检测到冲突会弹交互提示
        （stdin 非交互时直接退出）→ 表现为「正在启动本地服务」卡死。
        这里在 spawn 之前清场，只杀属于本应用运行时目录的 python/node。
        """
        if os.name != "nt":
            return
        roots: list[str] = []
        try:
            py = Path(self.deeptutor_cmd[0])
            if py.exists():
                py = py.resolve()
                # .../runtime/python/python.exe → .../runtime；venv/Scripts/x → venv
                roots.append(str(py.parent.parent))
                roots.append(str(py.parent))
        except Exception:  # noqa: BLE001
            pass
        if self.node_dir is not None:
            roots.append(str(self.node_dir))
        roots = [r.lower().rstrip("\\/") for r in roots if r]
        if not roots:
            return

        def owned(exe: str | None) -> bool:
            if not exe:
                return False
            exe_l = exe.lower()
            if exe_l.rsplit("\\", 1)[-1] not in ("python.exe", "node.exe", "deeptutor.exe"):
                return False
            return any(exe_l.startswith(r + "\\") for r in roots)

        for port, name in ((self.backend_port, "backend"), (self.frontend_port, "frontend")):
            pid = _listener_pid(port)
            if not pid:
                continue
            exe = _exe_path_of(pid)
            if not owned(exe):
                log.warning(
                    "port %s held by non-runtime process (pid=%s exe=%s); leaving it alone",
                    port, pid, exe,
                )
                continue
            # 若父进程也是本运行时的进程（残留编排器），连树一起杀
            ppid = _parent_pid(pid)
            if ppid and owned(_exe_path_of(ppid)):
                log.info("killing stale %s orchestrator tree (parent pid=%s)", name, ppid)
                _taskkill_tree(ppid)  # /T 连同端口持有者一起终止
            else:
                log.info(
                    "killing stale %s on :%s (pid=%s exe=%s)", name, port, pid, exe
                )
                _taskkill_tree(pid)

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Spawn `deeptutor start --home <ws> --no-browser` as a hidden child."""
        self.home.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        if self.node_dir is not None:
            env["PATH"] = str(self.node_dir) + os.pathsep + env.get("PATH", "")
        # DeepTutor respects DEEPTUTOR_HOME too; pass --home explicitly.
        env["DEEPTUTOR_HOME"] = str(self.home)

        # 启动前清场：别让上次异常退出留下的孤儿把端口冲突顶到子进程面前。
        self._kill_stale_listeners()

        cmd = self.deeptutor_cmd + [
            "start",
            "--home", str(self.home),
            "--no-browser",
        ]
        log.info("spawn: %s", " ".join(cmd))
        self.child_log_path = self.home / "logs" / "deeptutor-child.log"
        self.child_log_path.parent.mkdir(parents=True, exist_ok=True)
        self._child_out = self.child_log_path.open("wb")
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(self.home),
            env=env,
            stdout=self._child_out,
            stderr=subprocess.STDOUT,
            # stdin 用真实管道而非 DEVNULL：Windows 上 NUL 设备的
            # sys.stdin.isatty() 返回 True（FILE_TYPE_CHAR 怪癖），会让
            # deeptutor 的「非 TTY 自动退出」保护失效、走进交互 input()。
            stdin=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        self._reader = threading.Thread(
            target=self._read_output, daemon=True, name="dt-output"
        )
        self._reader.start()
        log.info("deeptutor pid=%s", self.proc.pid)

    def wait_ready(self, timeout: float = 180.0) -> tuple[str, int]:
        """Block until the frontend answers, or raise TimeoutError/RuntimeError.

        就绪判定只依赖前端端口探测（无条件、每轮一次），**不依赖子进程
        stdout 的任何文本**——文本通道（子进程缓冲/尾随线程/语言差异）任何
        一环出问题都不能拖累启动。
        """
        start = time.monotonic()
        last_beat = start
        while True:
            now = time.monotonic()
            if now - start >= timeout:
                raise TimeoutError(
                    f"frontend {self.frontend_url} did not become ready within "
                    f"{timeout:.0f}s. See the app log for deeptutor output."
                )
            if self.proc and self.proc.poll() is not None:
                raise RuntimeError(
                    f"deeptutor exited early (rc={self.proc.returncode}). "
                    "See the app log for deeptutor output."
                )
            if self.frontend_ready.wait(timeout=0.5):
                log.info("frontend ready (child signal) at %s", self.frontend_url)
                return self.frontend_url, 200
            reachable, _status = _http_probe(self.frontend_url, timeout=1.0)
            if reachable:
                log.info("frontend ready (port probe) at %s", self.frontend_url)
                return self.frontend_url, 200
            if now - last_beat >= 5:
                last_beat = now
                log.info(
                    "wait_ready: %.0fs elapsed; child alive, frontend %s not "
                    "answering yet", now - start, self.frontend_url,
                )

    # ------------------------------------------------------------------ #
    def stop(self, timeout: float = 10.0) -> None:
        """Terminate the whole process tree (backend + next.js server).

        DeepTutor detaches its backend/frontend children into their own process
        groups, so ``taskkill /T`` on the orchestrator PID can occasionally leave
        orphans behind. We therefore also re-scan the known ports and kill any
        listener there.
        """
        start_pid = None
        if self.proc is not None:
            start_pid = self.proc.pid
            log.info("stopping deeptutor pid=%s", start_pid)
        if os.name == "nt":
            if start_pid:
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(start_pid), "/T", "/F"],
                        capture_output=True,
                        creationflags=CREATE_NO_WINDOW,
                        check=False,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("taskkill failed: %s", exc)
            # port-based sweep (covers detached grandchildren)
            for port, name in ((self.backend_port, "backend"), (self.frontend_port, "frontend")):
                pid = _listener_pid(port)
                if pid:
                    try:
                        subprocess.run(
                            ["taskkill", "/PID", str(pid), "/T", "/F"],
                            capture_output=True,
                            creationflags=CREATE_NO_WINDOW,
                            check=False,
                        )
                        log.info("killed stale %s on :%s (pid=%s)", name, port, pid)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("port kill failed: %s", exc)
        else:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()

        deadline = time.monotonic() + timeout
        while self.proc and self.proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.2)
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass
        self._close_handles()

    # ------------------------------------------------------------------ #
    def _read_output(self) -> None:
        """Tail the child log without touching the frozen parent's PIPE.

        用 ``os.read`` 原始 fd 轮询（此前 ``BufferedReader.read(65536)`` 在
        冻结版里曾静默卡死，就绪文本永远进不了壳），并全程留痕方便排查。
        """
        log.info("tail thread: watching %s", self.child_log_path)
        try:
            fd = os.open(
                str(self.child_log_path),
                os.O_RDONLY | getattr(os, "O_BINARY", 0),
            )
        except OSError:
            log.exception("tail thread: open failed")
            return
        total = 0
        last_beat = time.monotonic()
        try:
            pending = bytearray()
            while True:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    chunk = b""
                if chunk:
                    total += len(chunk)
                    pending.extend(chunk)
                    *lines, pending_bytes = pending.split(b"\n")
                    pending.clear()
                    pending.extend(pending_bytes)
                    for raw in lines:
                        self._handle_line(raw)
                    continue
                if self.proc is not None and self.proc.poll() is not None:
                    if pending:
                        self._handle_line(bytes(pending) + b"\n")
                    break
                if time.monotonic() - last_beat >= 10:
                    last_beat = time.monotonic()
                    log.debug("tail alive: %d bytes so far", total)
                time.sleep(0.2)
        except Exception:  # noqa: BLE001
            log.exception("tail thread: failed to read deeptutor output")
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            log.info("tail thread: exit (%d bytes total)", total)

    def _handle_line(self, raw: bytes) -> None:
        line = _decode_line(raw)
        log.info("deeptutor: %s", line)
        lowered = line.lower()
        if "前端 已就绪" in line or "frontend ready" in lowered:
            self.frontend_ready.set()
        if "正在启动前端" in line or "starting frontend" in lowered:
            self.frontend_launching.set()
        if self.on_line:
            try:
                self.on_line(line)
            except Exception:  # noqa: BLE001
                pass

    def _close_handles(self) -> None:
        proc = self.proc
        if proc is not None and proc.stdin is not None:
            try:
                proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
        if self._child_out is not None:
            try:
                self._child_out.close()
            except Exception:  # noqa: BLE001
                pass
            self._child_out = None
        if self._tail is not None:
            try:
                self._tail.close()
            except Exception:  # noqa: BLE001
                pass
            self._tail = None
