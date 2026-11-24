#!/usr/bin/env python3
"""Data Transfer: local web UI for rsync + SSH transfers (PRD v3).

Python standard library only. Listens on 127.0.0.1 only.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

VERSION = "3"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(APP_DIR, "web")
DEFAULT_PORT = 8765
PORT_TRIES = 8  # 8765 + up to seven more
MAX_LOG_LINES = 3000

DIRECTIONS = ("local_to_server", "server_to_local")

ST_READY = "就绪"
ST_RUNNING = "传输中"
ST_STOPPING = "正在停止"
ST_STOPPED = "已停止"
ST_DONE = "完成"
ST_PARTIAL = "部分失败"
ST_FAILED = "失败"


class UserError(Exception):
    """Error whose message is safe and readable for the UI (Chinese)."""


# ---------------------------------------------------------------- settings
def config_path() -> str:
    return os.environ.get("DT_CONFIG_PATH") or os.path.join(APP_DIR, "transfer_tool_config.json")


def ssh_config_path() -> str:
    return os.environ.get("DT_SSH_CONFIG") or os.path.expanduser("~/.ssh/config")


# ---------------------------------------------------------------- ssh config
_HOST_RE = re.compile(r"^\s*host\s*(?:=\s*|\s+)(.*)$", re.IGNORECASE)
_INCLUDE_RE = re.compile(r"^\s*include\s*(?:=\s*|\s+)(.*)$", re.IGNORECASE)


def _split_tokens(value: str) -> list[str]:
    value = value.split("#", 1)[0] if "#" in value else value
    try:
        return shlex.split(value)
    except ValueError:
        return value.split()


def parse_ssh_hosts(path: str | None = None) -> list[str]:
    """Return concrete Host aliases in file order, following Include, deduped.

    Read-only. Wildcards (*, ?) and negations (!) are skipped.
    """
    path = path or ssh_config_path()
    hosts: list[str] = []
    seen_hosts: set[str] = set()
    visited: set[str] = set()

    def visit(file_path: str) -> None:
        real = os.path.realpath(file_path)
        if real in visited or not os.path.isfile(real):
            return
        visited.add(real)
        try:
            with open(real, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            return
        base_dir = os.path.dirname(real)
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m = _INCLUDE_RE.match(line)
            if m:
                for pattern in _split_tokens(m.group(1)):
                    pattern = os.path.expanduser(os.path.expandvars(pattern))
                    if not os.path.isabs(pattern):
                        # relative to the including file's directory (PRD 6.1)
                        pattern = os.path.join(base_dir, pattern)
                    for inc in sorted(glob.glob(pattern)):
                        visit(inc)
                continue
            m = _HOST_RE.match(line)
            if m:
                for token in _split_tokens(m.group(1)):
                    if any(c in token for c in "*?!"):
                        continue
                    if token not in seen_hosts:
                        seen_hosts.add(token)
                        hosts.append(token)

    visit(path)
    return hosts


# ---------------------------------------------------------------- persistence
DEFAULT_CONFIG = {"local": "", "servers": [], "remote_path": "", "direction": "local_to_server"}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    path = config_path()
    if not os.path.exists(path):
        return cfg
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise UserError(f"读取配置文件失败：{path}（{exc.__class__.__name__}）") from exc
    if isinstance(data, dict):
        for key in cfg:
            if key in data:
                cfg[key] = data[key]
    return cfg


def save_config(update: dict) -> dict:
    try:
        cfg = load_config()
    except UserError:
        cfg = dict(DEFAULT_CONFIG)
    for key in DEFAULT_CONFIG:
        if key in update:
            cfg[key] = update[key]
    path = config_path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError as exc:
        raise UserError(f"写入配置文件失败：{path}（{exc.strerror or exc}）") from exc
    return cfg


# ---------------------------------------------------------------- paths & validation
def normalize_local(path: str) -> str:
    path = (path or "").strip()
    if not path:
        return ""
    return os.path.abspath(os.path.expanduser(os.path.expandvars(path)))


_REMOTE_PREFIX_RE = re.compile(r"^[A-Za-z0-9_.\-]+(@[A-Za-z0-9_.\-\[\]:]+)?:")


def validate_remote_path(remote_path: str, servers: list[str]) -> str:
    rp = (remote_path or "").strip()
    if not rp:
        raise UserError("远程路径不能为空。")
    lower = rp.lower()
    if "://" in lower:
        raise UserError("远程路径只填写服务器上的路径，不要包含 ssh:// 等协议前缀。")
    for alias in servers:
        if rp.startswith(alias + ":"):
            raise UserError(f"远程路径不要重复填写服务器别名（{alias}:）。")
    if "@" in rp.split("/", 1)[0] or _REMOTE_PREFIX_RE.match(rp):
        raise UserError("远程路径不要包含 user@host: 或 别名: 前缀，只填写服务器上的路径。")
    return rp


def validate_start(payload: dict, known_hosts: list[str] | None) -> dict:
    direction = payload.get("direction") or "local_to_server"
    if direction not in DIRECTIONS:
        raise UserError("传输方向无效。")
    servers = payload.get("servers") or []
    if isinstance(servers, str):
        servers = [servers]
    servers = [str(s).strip() for s in servers if str(s).strip()]
    # keep order, dedupe
    servers = list(dict.fromkeys(servers))
    if not servers:
        raise UserError("请至少选择一台服务器。")
    if known_hosts is not None:
        unknown = [s for s in servers if s not in known_hosts]
        if unknown:
            raise UserError("服务器必须来自 ~/.ssh/config：" + " / ".join(unknown))
    remote_path = validate_remote_path(payload.get("remote_path", ""), servers)
    raw_local = payload.get("local", "")
    local = normalize_local(raw_local)
    if not local:
        raise UserError("本地地址不能为空。")
    if direction == "local_to_server" and not os.path.exists(local):
        raise UserError(f"本地源路径不存在：{local}")
    return {"local": local, "raw_local": str(raw_local).strip(), "servers": servers,
            "remote_path": remote_path, "direction": direction}


# ---------------------------------------------------------------- rsync
def find_executable(names: list[str]) -> str | None:
    for name in names:
        if os.path.isabs(name):
            if os.path.isfile(name) and os.access(name, os.X_OK):
                return name
        else:
            found = shutil.which(name)
            if found:
                return found
    return None


def find_rsync() -> str | None:
    env = os.environ.get("DT_RSYNC")
    candidates = ([env] if env else []) + ["/opt/homebrew/bin/rsync", "/usr/local/bin/rsync", "rsync", "/usr/bin/rsync"]
    return find_executable(candidates)


def rsync_supports_progress2(rsync: str) -> bool:
    try:
        out = subprocess.run([rsync, "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    m = re.search(r"rsync\s+version\s+v?(\d+)\.(\d+)", out)
    if not m or "openrsync" in out:
        return False
    return (int(m.group(1)), int(m.group(2))) >= (3, 1)


def ssh_command() -> str:
    program = os.environ.get("DT_SSH_PROGRAM") or "ssh"
    return f"{shlex.quote(program)} -T -o Compression=no"


def build_rsync_args(rsync: str, source: str, dest: str, progress2: bool = True,
                     extra: list[str] | None = None) -> list[str]:
    if progress2:
        args = [rsync, "-rlt", "--no-owner", "--no-group", "--no-perms", "--whole-file",
                "--partial", "--info=progress2", "--human-readable", "--stats"]
    else:  # macOS openrsync fallback (no --info / --no-owner support)
        args = [rsync, "-rlt", "--whole-file", "--partial", "--progress", "--stats"]
    args += list(extra or [])
    args += ["-e", ssh_command(), source, dest]
    return args


def plan_jobs(spec: dict) -> list[dict]:
    """Return [{server, source, dest, local_target}] in selection order."""
    jobs = []
    local, rp, servers = spec["local"], spec["remote_path"], spec["servers"]
    multi = len(servers) > 1
    for alias in servers:
        remote = f"{alias}:{rp}"
        if spec["direction"] == "local_to_server":
            src = local
            if os.path.isdir(local) and not src.endswith("/"):
                src += "/"  # sync directory contents into remote path
            jobs.append({"server": alias, "source": src, "dest": remote, "local_target": local})
        else:
            target = os.path.join(local, alias) if multi else local
            jobs.append({"server": alias, "source": remote, "dest": target.rstrip("/") + "/",
                         "local_target": target})
    return jobs


_PROGRESS_RE = re.compile(
    r"^\s*([\d.,]+\s*[KMGTP]?i?B?)\s+(\d{1,3})%\s+([\d.,]+\s*[KMGTP]?i?B/s)\s+(\d+:\d{2}:\d{2}|\d+:\d{2})")


def parse_progress(line: str):
    m = _PROGRESS_RE.match(line)
    if not m:
        return None
    return {"bytes": m.group(1).strip(), "percent": int(m.group(2)),
            "speed": m.group(3).replace(" ", ""), "eta": m.group(4)}


def fmt_eta(eta: str) -> str:
    parts = eta.split(":")
    if len(parts) == 2:
        return "0:" + eta
    return eta


# ---------------------------------------------------------------- transfer manager
class TransferManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.stop_requested = False
        self.state = self._blank()

    @staticmethod
    def _blank() -> dict:
        return {"running": False, "status": ST_READY, "percent": 0, "speed": "0 B/s", "eta": "--",
                "direction": "local_to_server", "local": "", "servers": [], "remote_path": "",
                "current_server": "", "return_code": None, "results": [], "logs": [], "log_total": 0}

    def status(self) -> dict:
        with self.lock:
            s = dict(self.state)
            s["results"] = [dict(r) for r in self.state["results"]]
            s["logs"] = list(self.state["logs"])
            return s

    def log(self, msg: str) -> None:
        with self.lock:
            logs = self.state["logs"]
            self.state["log_total"] = self.state.get("log_total", 0) + 1
            logs.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
            if len(logs) > MAX_LOG_LINES:
                del logs[: len(logs) - MAX_LOG_LINES]

    def start(self, spec: dict) -> dict:
        with self.lock:
            if self.state["running"]:
                raise UserError("已有传输任务正在进行，请等待完成或先停止。")
            rsync = find_rsync()
            if not rsync:
                raise UserError("找不到 rsync，请先安装 rsync（例如 brew install rsync）。")
            ssh_prog = os.environ.get("DT_SSH_PROGRAM") or "ssh"
            if not find_executable([ssh_prog]):
                raise UserError("找不到 ssh，请确认系统已安装 OpenSSH 客户端。")
            if spec["direction"] == "server_to_local":
                try:
                    os.makedirs(spec["local"], exist_ok=True)
                except OSError as exc:
                    raise UserError(f"无法创建本地目标目录：{spec['local']}（{exc.strerror}）") from exc
            progress2 = rsync_supports_progress2(rsync)
            jobs = plan_jobs(spec)
            self.stop_requested = False
            self.state = self._blank()
            self.state.update({"running": True, "status": ST_RUNNING, "direction": spec["direction"],
                               "local": spec["local"], "servers": list(spec["servers"]),
                               "remote_path": spec["remote_path"],
                               "results": [{"server": j["server"], "status": "等待", "return_code": None,
                                            "source": j["source"], "dest": j["dest"]} for j in jobs]})
            self.log(f"任务开始：{len(jobs)} 台服务器，方向 {'本地 → 服务器' if spec['direction']=='local_to_server' else '服务器 → 本地'}")
            self.log(f"rsync: {rsync}" + ("" if progress2 else "（openrsync 兼容模式：无 --info=progress2）"))
            extra = shlex.split(os.environ.get("DT_EXTRA_RSYNC_ARGS", ""))
            self.thread = threading.Thread(target=self._run, args=(rsync, progress2, jobs, extra), daemon=True)
            self.thread.start()
            return self.status()

    def _run(self, rsync: str, progress2: bool, jobs: list[dict], extra: list[str]) -> None:
        n = len(jobs)
        for idx, job in enumerate(jobs):
            with self.lock:
                if self.stop_requested:
                    self.state["results"][idx]["status"] = "未执行"
                    continue
                self.state["current_server"] = job["server"]
                self.state["results"][idx]["status"] = "传输中"
                self.state["speed"], self.state["eta"] = "0 B/s", "--"
            args = build_rsync_args(rsync, job["source"], job["dest"], progress2, extra)
            self.log(f"[{job['server']}] 开始：{job['source']} → {job['dest']}")
            self.log(f"[{job['server']}] $ " + " ".join(shlex.quote(a) for a in args))
            rc = self._run_one(args, idx, n, job["server"])
            with self.lock:
                res = self.state["results"][idx]
                res["return_code"] = rc
                if self.stop_requested:
                    res["status"] = "已停止"
                    self.log(f"[{job['server']}] 已停止（返回码 {rc}），部分文件已保留，再次开始将续传。")
                elif rc == 0:
                    res["status"] = "完成"
                    self.log(f"[{job['server']}] 完成")
                else:
                    res["status"] = "失败"
                    self.log(f"[{job['server']}] 失败，返回码 {rc}，继续处理后续服务器。")
                self.state["percent"] = round((idx + 1) * 100 / n) if not self.stop_requested else self.state["percent"]
        with self.lock:
            results = self.state["results"]
            ok = [r for r in results if r["status"] == "完成"]
            failed = [r for r in results if r["status"] == "失败"]
            if self.stop_requested:
                status = ST_STOPPED
            elif len(ok) == n:
                status = ST_DONE
            elif ok:
                status = ST_PARTIAL
            else:
                status = ST_FAILED
            codes = [r["return_code"] for r in results if r["return_code"] is not None]
            self.state["return_code"] = next((c for c in codes if c != 0), 0 if codes else None)
            self.state["status"] = status
            self.state["running"] = False
            self.state["current_server"] = ""
            if status == ST_DONE:
                self.state["percent"], self.state["eta"] = 100, "0:00:00"
            else:
                self.state["eta"] = "--"
            self.state["speed"] = "0 B/s"
            self.proc = None
            self.log(f"任务结束：{status}（成功 {len(ok)}，失败 {len(failed)}，共 {n}）")

    def _run_one(self, args: list[str], idx: int, n: int, server: str) -> int:
        env = dict(os.environ)
        env.setdefault("LC_ALL", "C")
        try:
            proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    bufsize=0, start_new_session=True, env=env)
        except OSError as exc:
            self.log(f"[{server}] 无法启动 rsync：{exc}")
            return 127
        with self.lock:
            self.proc = proc
            if self.stop_requested:  # stop clicked during spawn
                self._signal(proc, signal.SIGINT)
        buf = b""
        last_logged = -10
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for raw in parts:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line.strip():
                    continue
                prog = parse_progress(line)
                if prog:
                    with self.lock:
                        self.state["percent"] = round((idx * 100 + prog["percent"]) / n)
                        self.state["speed"] = prog["speed"]
                        self.state["eta"] = fmt_eta(prog["eta"])
                    if prog["percent"] >= last_logged + 10 or prog["percent"] == 100:
                        last_logged = prog["percent"]
                        self.log(f"[{server}] {line.strip()}")
                else:
                    self.log(f"[{server}] {line}")
        if buf.strip():
            self.log(f"[{server}] {buf.decode('utf-8', errors='replace').strip()}")
        proc.stdout.close()
        return proc.wait()

    @staticmethod
    def _signal(proc: subprocess.Popen, sig: int) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def stop(self) -> dict:
        with self.lock:
            if not self.state["running"]:
                return self.status()
            self.stop_requested = True
            self.state["status"] = ST_STOPPING
            self.log("收到停止请求，向当前 rsync 进程组发送 SIGINT……")
            proc = self.proc
        if proc is not None and proc.poll() is None:
            self._signal(proc, signal.SIGINT)
            # escalate if rsync ignores SIGINT
            def escalate():
                time.sleep(8)
                if proc.poll() is None:
                    self._signal(proc, signal.SIGTERM)
            threading.Thread(target=escalate, daemon=True).start()
        return self.status()


# ---------------------------------------------------------------- folder chooser
def choose_folder(initial: str = "") -> dict:
    """Open the native folder chooser. Returns {ok, path, cancelled}."""
    initial = normalize_local(initial)
    if sys.platform == "darwin":
        osa = "/usr/bin/osascript"
        if not os.path.exists(osa):
            raise UserError("找不到系统文件夹选择器（/usr/bin/osascript）。")
        script = [
            "on run argv",
            "set p to \"选择本地文件夹\"",
            "activate",
            "if (count of argv) > 0 and item 1 of argv is not \"\" then",
            "  try",
            "    set f to choose folder with prompt p default location (POSIX file (item 1 of argv) as alias)",
            "  on error number n",
            "    if n is -128 then error number -128",
            "    set f to choose folder with prompt p",
            "  end try",
            "else",
            "  set f to choose folder with prompt p",
            "end if",
            "return POSIX path of f",
            "end run",
        ]
        cmd = [osa]
        for line in script:
            cmd += ["-e", line]
        cmd.append(initial if initial and os.path.isdir(initial) else "")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            return {"ok": True, "path": proc.stdout.strip(), "cancelled": False}
        if "-128" in proc.stderr or "User canceled" in proc.stderr or "用户已取消" in proc.stderr:
            return {"ok": True, "path": None, "cancelled": True}
        raise UserError("系统文件夹选择器出错：" + (proc.stderr.strip() or f"返回码 {proc.returncode}"))
    for cmd in (["zenity", "--file-selection", "--directory"], ["kdialog", "--getexistingdirectory", initial or os.path.expanduser("~")]):
        if shutil.which(cmd[0]):
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode == 0 and proc.stdout.strip():
                return {"ok": True, "path": proc.stdout.strip(), "cancelled": False}
            return {"ok": True, "path": None, "cancelled": True}
    raise UserError("找不到可用的系统文件夹选择器（macOS 需要 osascript，Linux 需要 zenity 或 kdialog）。")


# ---------------------------------------------------------------- HTTP
MANAGER = TransferManager()
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml",
                 ".png": "image/png", ".ico": "image/x-icon"}


class Handler(BaseHTTPRequestHandler):
    server_version = "DataTransfer/" + VERSION

    def log_message(self, fmt, *args):  # quiet access log
        if os.environ.get("DT_ACCESS_LOG"):
            super().log_message(fmt, *args)

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, msg, status=400):
        self._json({"ok": False, "error": msg}, status)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise UserError("请求内容不是有效的 JSON。") from exc
        if not isinstance(data, dict):
            raise UserError("请求内容格式错误。")
        return data

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/status":
                return self._json(MANAGER.status())
            if path == "/api/hosts":
                return self._json({"hosts": parse_ssh_hosts()})
            if path == "/api/config":
                return self._json(load_config())
            if path == "/api/health":
                return self._json({"ok": True, "version": VERSION, "pid": os.getpid(), "app_dir": APP_DIR,
                                   "test_mode": bool(os.environ.get("DT_SSH_PROGRAM"))})
            return self._static(path)
        except UserError as exc:
            return self._error(str(exc))
        except Exception as exc:  # never leak a traceback to the page
            sys.stderr.write(f"GET {path} failed: {exc!r}\n")
            return self._error("后台处理请求时出错，请查看服务日志。", 500)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/start":
                spec = validate_start(body, parse_ssh_hosts())
                save_config({"local": spec["raw_local"] or spec["local"], "servers": spec["servers"],
                             "remote_path": spec["remote_path"], "direction": spec["direction"]})
                status = MANAGER.start(spec)
                return self._json({"ok": True, "status": status})
            if path == "/api/stop":
                return self._json({"ok": True, "status": MANAGER.stop()})
            if path == "/api/choose-folder":
                if MANAGER.status()["running"]:
                    raise UserError("传输进行中，暂不能更换本地文件夹。")
                result = choose_folder(body.get("current", ""))
                if result.get("path"):
                    save_config({"local": result["path"]})
                return self._json(result)
            if path == "/api/config":
                cfg = save_config({k: v for k, v in body.items() if k in DEFAULT_CONFIG})
                return self._json({"ok": True, "config": cfg})
            return self._error("接口不存在。", 404)
        except UserError as exc:
            return self._error(str(exc))
        except Exception as exc:
            sys.stderr.write(f"POST {path} failed: {exc!r}\n")
            return self._error("后台处理请求时出错，请查看服务日志。", 500)

    def _static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        full = os.path.realpath(os.path.join(WEB_DIR, path.lstrip("/")))
        if not full.startswith(os.path.realpath(WEB_DIR) + os.sep) or not os.path.isfile(full):
            return self._error("页面不存在。", 404)
        with open(full, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(os.path.splitext(full)[1], "application/octet-stream"))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class LocalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # skip HTTPServer's socket.getfqdn() reverse lookup, which can stall for seconds on macOS
        import socketserver
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


def make_server(port: int = DEFAULT_PORT, tries: int = PORT_TRIES) -> ThreadingHTTPServer:
    last = None
    for p in range(port, port + tries):
        try:
            return LocalHTTPServer(("127.0.0.1", p), Handler)
        except OSError as exc:
            last = exc
    raise SystemExit(f"端口 {port}-{port + tries - 1} 均被占用：{last}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Data Transfer local web tool")
    ap.add_argument("--port", type=int, default=int(os.environ.get("DT_PORT", DEFAULT_PORT)))
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--url-file", help="write the final URL to this file")
    args = ap.parse_args(argv)
    httpd = make_server(args.port)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"Data Transfer v{VERSION} running at {url}", flush=True)
    if args.url_file:
        with open(args.url_file, "w") as fh:
            fh.write(url + "\n")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        MANAGER.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
