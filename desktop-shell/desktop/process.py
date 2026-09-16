"""Manage the `deeptutor start` subprocess (backend :8001 + frontend :3782)."""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen

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
    """Find the PID listening on a TCP port (Windows netstat)."""
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True,
            creationflags=CREATE_NO_WINDOW, check=False,
        ).stdout
        for line in out.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                return int(line.split()[-1])
    except Exception:  # noqa: BLE001
        pass
    return None


def _http_probe(url: str, timeout: float = 2.0) -> tuple[bool, int | None]:
    """Return (reachable, http_status). Any response means the server is up."""
    req = Request(url, method="GET", headers={"User-Agent": "EduBuddy-Desktop/0.1"})
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 (127.0.0.1 only)
            return True, resp.status
    except Exception as exc:  # noqa: BLE001 - any failure means "not ready yet"
        code = getattr(exc, "code", None)
        if code is not None:
            # Some servers return a 4xx/5xx page while still booting; treat a
            # response (even an error page) as "reachable" and let the caller
            # decide via a second probe.
            return True, code
        return False, None


class DeepTutorProcess:
    """Owns the lifetime of the `deeptutor start` child process."""

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

    # ------------------------------------------------------------------ #
    @property
    def frontend_url(self) -> str:
        return f"http://127.0.0.1:{self.frontend_port}"

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Spawn `deeptutor start --home <ws> --no-browser` as a hidden child."""
        self.home.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        if self.node_dir is not None:
            env["PATH"] = str(self.node_dir) + os.pathsep + env.get("PATH", "")
        # DeepTutor respects DEEPTUTOR_HOME too; pass --home explicitly.
        env["DEEPTUTOR_HOME"] = str(self.home)

        cmd = self.deeptutor_cmd + [
            "start",
            "--home", str(self.home),
            "--no-browser",
        ]
        log.info("spawn: %s", " ".join(cmd))
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(self.home),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            bufsize=0,  # PIPE is binary here; bufsize=1 is invalid (py>=3.13 warns)
            creationflags=CREATE_NO_WINDOW,
        )
        self._reader = threading.Thread(
            target=self._read_output, args=(self.proc,), daemon=True, name="dt-output"
        )
        self._reader.start()
        log.info("deeptutor pid=%s", self.proc.pid)

    def wait_ready(self, timeout: float = 180.0) -> tuple[str, int]:
        """Block until the frontend answers, or raise TimeoutError/RuntimeError."""
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if self.proc and self.proc.poll() is not None:
                raise RuntimeError(
                    f"deeptutor exited early (rc={self.proc.returncode}). "
                    "See the app log for deeptutor output."
                )
            ok, status = _http_probe(self.frontend_url)
            if ok:
                log.info("frontend ready at %s (status=%s)", self.frontend_url, status)
                return self.frontend_url, status or 0
            time.sleep(0.5)
        raise TimeoutError(
            f"frontend {self.frontend_url} did not become ready within {timeout:.0f}s. "
            "See the app log for deeptutor output."
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

    # ------------------------------------------------------------------ #
    def _read_output(self, proc: subprocess.Popen) -> None:  # instance method
        assert proc.stdout is not None
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = _decode_line(raw)
                log.info("deeptutor: %s", line)
                if self.on_line:
                    try:
                        self.on_line(line)
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            pass
