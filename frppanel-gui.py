#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FRP Panel GUI — frp-panel 客户端图形管理器 (Windows)

给普通用户使用的 frp-panel 客户端管理界面:
- 粘贴启动命令即可完成配置(支持完整安装命令或 frp-panel client 命令)
- 实时显示客户端状态 / 隧道列表 / 运行日志
- 开机自启动(注册表 HKCU Run, 无需管理员权限), 自启动时最小化到托盘
- 自动下载并管理 frp-panel 客户端二进制

依赖: Python 3.9+, PySide6
"""

import sys
import os
import re
import json
import base64
import shutil
import argparse
import urllib.request
from pathlib import Path
from datetime import datetime

from PySide6.QtCore import (
    Qt, QObject, Signal, QProcess, QTimer, QThread, QSize,
)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtGui import (
    QAction, QColor, QFont, QIcon, QPainter, QPixmap, QPen, QBrush,
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget, QLabel, QPushButton,
    QVBoxLayout, QHBoxLayout, QGridLayout, QPlainTextEdit, QCheckBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QStackedWidget, QFrame,
    QProgressBar, QMenu, QSystemTrayIcon, QMessageBox, QAbstractItemView,
    QSizePolicy, QSpacerItem,
)

# ----------------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------------

APP_NAME = "FRPPanelGUI"
APP_TITLE = "FRP Panel 客户端"
SINGLE_INSTANCE_KEY = "FRPPanelGUI-SingleInstance"
AUTOSTART_RUN_VALUE = "FRPPanelGUI"

DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / APP_NAME
CONFIG_FILE = DATA_DIR / "config.json"
CLIENT_EXE = DATA_DIR / "frp-panel-client.exe"
CLIENT_LOG = DATA_DIR / "client.log"
CLIENT_LOG_OLD = DATA_DIR / "client.log.old"
CA_CACHE_FILE = DATA_DIR / "master_ca.pem"
LOG_ROTATE_SIZE = 1024 * 1024  # 1MB 后轮转

DOWNLOAD_URLS = [
    "https://ghfast.top/https://github.com/VaalaCat/frp-panel/releases/latest/download/frp-panel-client-windows-amd64.exe",
    "https://github.com/VaalaCat/frp-panel/releases/latest/download/frp-panel-client-windows-amd64.exe",
]

PRIMARY = "#2E7CF6"   # 淡蓝主色
GREEN = "#22B573"
RED = "#E5484D"
YELLOW = "#F5A623"
GRAY = "#9AA5B1"

# 程序图标: 打包形态从 exe 资源取; 源码形态从 docs/icons/frp.png 取;
# 都不可用时回退到程序内绘制
ICON_FILE = Path(__file__).parent / "docs" / "icons" / "frp.png"

# 客户端状态
ST_UNCONFIGURED = "unconfigured"
ST_STOPPED = "stopped"
ST_STARTING = "starting"
ST_CONNECTED = "connected"
ST_ERROR = "error"


# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def mask_secret(text: str, secret: str) -> str:
    """日志脱敏: 隐藏客户端密钥"""
    if secret and len(secret) > 4:
        text = text.replace(secret, "****" + secret[-4:])
        text = text.replace(secret.upper(), "****" + secret[-4:])
    return text


def mask_url(url: str) -> str:
    """地址脱敏: 保留协议与端口, 隐藏主机名"""
    if not url or "://" not in url:
        return url or "—"
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*://)([^/]+)(.*)$", url)
    if not m:
        return "•" * min(len(url), 12)
    scheme, host, rest = m.groups()
    if ":" in host:
        h, p = host.rsplit(":", 1)
        if p.isdigit():
            return f"{scheme}••••••••:{p}{rest}"
    return f"{scheme}••••••••{rest}"


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n:.1f}GB"


# ----------------------------------------------------------------------------
# 启动命令解析
# ----------------------------------------------------------------------------

class CommandParser:
    """从粘贴的文本中提取 frp-panel 客户端参数"""

    KEYS = {
        "-s": "secret", "--secret": "secret",
        "-i": "client_id", "--id": "client_id",
        "--api-url": "api_url",
        "--rpc-url": "rpc_url",
    }

    @classmethod
    def parse(cls, text: str):
        if not text or not text.strip():
            return None
        # 归一化空白; 兼容 PowerShell 命令中的换行
        tokens = re.sub(r"[\r\n\t]+", " ", text.strip()).split()
        params = {}
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            key = tok.lower()
            # --key=value 形式
            if key.startswith("--") and "=" in tok:
                k, v = tok.split("=", 1)
                name = cls.KEYS.get(k.lower())
                if name:
                    params[name] = v.strip('\'"')
                    i += 1
                    continue
            # -key value 形式
            name = cls.KEYS.get(key)
            if name and i + 1 < len(tokens):
                val = tokens[i + 1].strip('\'",;')
                params[name] = val
                i += 2
                continue
            i += 1
        # 必要参数校验
        if not (params.get("secret") and params.get("client_id") and params.get("rpc_url")):
            return None
        if not params["rpc_url"].startswith(("grpc://", "grpcs://", "http://", "https://", "ws://", "wss://")):
            return None
        params.setdefault("api_url", "")
        return params


# ----------------------------------------------------------------------------
# 客户端日志解析
# ----------------------------------------------------------------------------

# frp-panel logrus 日志: 2026-09-21 10:20:29.911 [info] [file.go:line]  msg
LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\s+\[(\w+)\]\s+\[([^\]]*)\]\s*(.*)$"
)
# frp 库内嵌日志(stdout): 2026-09-21 12:30:03.947 [I] [file.go:172] [run_id] [proxy] start proxy success
FRP_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\s+\[(\w)\]\s+\[([^\]]*)\]\s*(.*)$"
)
# frp 库: [run_id] proxy added: [proxy_name]
FRP_PROXY_ADDED_RE = re.compile(r"proxy added:\s*\[([\w.-]+)\]")
# frp 库: [run_id] [proxy_name] start proxy success
FRP_PROXY_OK_RE = re.compile(r"\[([\w.-]+)\]\s+start proxy success")
# frp 库: [run_id] [proxy_name] start proxy error: ...
FRP_PROXY_FAIL_RE = re.compile(r"\[([\w.-]+)\]\s+start proxy error:\s*(.*)")
# 兼容旧格式
PROXY_OK_RE = re.compile(r"\[([\w.-]+)\] start proxy success")
PROXY_FAIL_RE = re.compile(r"\[([\w.-]+)\] (?:start error|start proxy failed|proxy failed)")


class LogParser:
    """把 frp-panel 客户端日志行解析为结构化事件"""

    @staticmethod
    def parse(line: str):
        clean = strip_ansi(line).rstrip()
        # 先试 frp-panel logrus 格式
        m = LINE_RE.match(clean)
        if not m:
            # 再试 frp 库 stdout 格式: [I] [file.go:line] msg
            m = FRP_LINE_RE.match(clean)
        if not m:
            # 无时间戳前缀的行(如多行版本信息块)
            vm = re.search(r"BinVersion:\s*(\S+)", clean)
            if vm:
                return {"type": "version", "version": vm.group(1), "level": "-", "msg": clean}, clean
            return {"type": "raw", "level": "-", "msg": clean}, clean
        ts, level, loc, msg = m.groups()
        ev = {"type": "log", "level": level, "msg": msg, "ts": ts}

        # frp-panel 事件
        if "client get server register envent success" in msg:
            ev["type"] = "registered"
        elif "config is empty" in msg and "wait for server init" in msg:
            ev["type"] = "config_empty"
        elif "EVENT_PING" in msg or "client resp received" in msg:
            ev["type"] = "heartbeat"
        elif "EVENT_UPDATE_FRPC" in msg:
            ev["type"] = "config_update"
        elif "EVENT_START_FRPC" in msg:
            ev["type"] = "config_refresh"
        elif "BinVersion:" in msg:
            v = msg.split("BinVersion:")[1].strip().split()[0]
            ev.update(type="version", version=v)
        elif "has no workers" in msg:
            ev["type"] = "heartbeat"
        elif "pull client config success" in msg:
            ev["type"] = "config_pulled"
        else:
            # frp 库代理事件(stdout)
            pm = FRP_PROXY_ADDED_RE.search(msg)
            if pm:
                ev.update(type="proxy_new", name=pm.group(1), ptype="", local_ip="", local_port="")
                return ev, clean
            pm = FRP_PROXY_OK_RE.search(msg)
            if pm:
                ev.update(type="proxy_ok", name=pm.group(1))
                return ev, clean
            pm = FRP_PROXY_FAIL_RE.search(msg)
            if pm:
                ev.update(type="proxy_fail", name=pm.group(1), error=pm.group(2))
                return ev, clean
            if level in ("error", "fatal", "panic", "E"):
                ev["type"] = "error"
        return ev, clean


# ----------------------------------------------------------------------------
# 配置存储
# ----------------------------------------------------------------------------

class ConfigStore:
    def __init__(self):
        self.data = {
            "client": {},            # secret / client_id / api_url / rpc_url
            "autostart_on_launch": True,
        }
        self.load()

    def load(self):
        try:
            if CONFIG_FILE.exists():
                self.data.update(json.loads(CONFIG_FILE.read_text("utf-8")))
        except Exception:
            pass

    def save(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), "utf-8"
        )

    @property
    def client(self):
        return self.data.get("client") or {}

    @property
    def configured(self):
        c = self.client
        return bool(c.get("secret") and c.get("client_id") and c.get("rpc_url"))


# ----------------------------------------------------------------------------
# 开机自启动 (注册表 HKCU\...\Run)
# ----------------------------------------------------------------------------

class AutoStart:
    RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

    @staticmethod
    def _command():
        if getattr(sys, "frozen", False):
            # PyInstaller 打包形态
            return f'"{sys.executable}" --tray'
        # 源码形态: 优先用 pythonw 避免弹出控制台
        exe = sys.executable
        pw = Path(exe).with_name("pythonw.exe")
        if pw.exists():
            exe = str(pw)
        script = os.path.abspath(sys.argv[0])
        return f'"{exe}" "{script}" --tray'

    @staticmethod
    def enabled():
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AutoStart.RUN_KEY) as k:
                winreg.QueryValueEx(k, AUTOSTART_RUN_VALUE)
                return True
        except OSError:
            return False
        except Exception:
            return False

    @staticmethod
    def set_enabled(on: bool):
        try:
            import winreg
            if on:
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, AutoStart.RUN_KEY) as k:
                    winreg.SetValueEx(k, AUTOSTART_RUN_VALUE, 0, winreg.REG_SZ, AutoStart._command())
            else:
                try:
                    # 注意: 必须带 KEY_SET_VALUE 权限, 默认 KEY_READ 无法删除
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AutoStart.RUN_KEY,
                                        0, winreg.KEY_SET_VALUE) as k:
                        winreg.DeleteValue(k, AUTOSTART_RUN_VALUE)
                except FileNotFoundError:
                    pass
            return True
        except Exception:
            return False


# ----------------------------------------------------------------------------
# 单实例锁 (QLocalServer)
# ----------------------------------------------------------------------------

class SingleInstance(QObject):
    showRequested = Signal()

    def __init__(self):
        super().__init__()
        self.server = None
        self._sock = None

    def acquire(self) -> bool:
        """返回 True 表示本进程是第一个实例"""
        probe = QLocalSocket()
        probe.connectToServer(SINGLE_INSTANCE_KEY)
        if probe.waitForConnected(300):
            probe.write(b"show")
            probe.flush()
            probe.waitForBytesWritten(300)
            probe.disconnectFromServer()
            return False
        QLocalServer.removeServer(SINGLE_INSTANCE_KEY)
        self.server = QLocalServer()
        self.server.listen(SINGLE_INSTANCE_KEY)
        self.server.newConnection.connect(self._on_conn)
        return True

    def _on_conn(self):
        sock = self.server.nextPendingConnection()
        if sock:
            sock.readyRead.connect(lambda: self._read(sock))
            sock.disconnected.connect(sock.deleteLater)
            # 竞态防护: 数据可能在连接信号之前就已到达, 此时 readyRead 不会发射
            if sock.bytesAvailable() > 0:
                self._read(sock)

    def _read(self, sock):
        try:
            data = bytes(sock.readAll())
        except RuntimeError:
            return  # socket 已销毁
        if b"show" in data:
            self.showRequested.emit()


# ----------------------------------------------------------------------------
# 客户端二进制下载器
# ----------------------------------------------------------------------------

class Downloader(QThread):
    progress = Signal(int, int)          # received, total
    finishedOk = Signal(str)             # 保存路径
    finishedErr = Signal(str)            # 错误信息

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        last_err = None
        for url in DOWNLOAD_URLS:
            if self._cancelled:
                return
            try:
                self._download(url)
                return
            except Exception as e:
                last_err = f"{e}"
        if not self._cancelled:
            self.finishedErr.emit(last_err or "下载失败")

    def _download(self, url):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CLIENT_EXE.with_suffix(".downloading")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            received = 0
            with open(tmp, "wb") as f:
                while True:
                    if self._cancelled:
                        f.close()
                        tmp.unlink(missing_ok=True)
                        return
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    received += len(chunk)
                    self.progress.emit(received, total)
        if self._cancelled:
            tmp.unlink(missing_ok=True)
            return
        if tmp.stat().st_size < 1024 * 1024:
            tmp.unlink(missing_ok=True)
            raise RuntimeError("下载的文件异常(过小), 可能镜像不可用")
        os.replace(tmp, CLIENT_EXE)
        self.finishedOk.emit(str(CLIENT_EXE))

    def is_cancelled(self):
        return self._cancelled


# ----------------------------------------------------------------------------
# 客户端子进程管理
# ----------------------------------------------------------------------------

class ClientProcess(QObject):
    logLine = Signal(str)          # 已脱敏的原始日志行
    event = Signal(dict)           # 解析后的事件
    procStarted = Signal()
    procFinished = Signal(int)     # exit code
    procError = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc = None
        self._buf = ""
        self._user_stop = False
        self._log_fh = None
        self._log_size = 0
        self._secret = ""
        self._restart_count = 0
        self._last_params = None
        self._toml_buf = []
        self._in_update = False

    # -- 生命周期 ------------------------------------------------------------

    def running(self) -> bool:
        return self.proc is not None and self.proc.state() != QProcess.ProcessState.NotRunning

    def start(self, params: dict):
        if self.running():
            return
        if not CLIENT_EXE.exists():
            self.procError.emit("客户端程序不存在, 请先下载")
            return
        if self._detect_legacy_service():
            self.procError.emit(
                "检测到官方方式安装的 frp-panel 服务正在运行 (C:\\frpp\\frpp.exe),\n"
                "同一客户端 ID 双开会冲突。\n请先停止并卸载旧服务:\n"
                "  C:\\frpp\\frpp.exe stop\n  C:\\frpp\\frpp.exe uninstall"
            )
            return
        self._user_stop = False
        self._secret = params.get("secret", "")
        self._buf = ""
        self.reset_restart_count()
        try:
            self._open_log()
            self.proc = QProcess(self)
            self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
            self.proc.setWorkingDirectory(str(DATA_DIR))
            self.proc.readyRead.connect(self._on_ready)
            self.proc.finished.connect(self._on_finished)
            self.proc.errorOccurred.connect(self._on_proc_error)
            args = [
                "client",
                "-s", params.get("secret", ""),
                "-i", params.get("client_id", ""),
            ]
            if params.get("api_url"):
                args += ["--api-url", params["api_url"]]
            args += ["--rpc-url", params["rpc_url"]]
            self.proc.start(str(CLIENT_EXE), args)
        except Exception as e:
            self._close_log()
            self.procError.emit(f"启动客户端失败: {e}")
            return
        self._last_params = params
        self.procStarted.emit()

    def stop(self):
        if not self.running():
            return
        self._user_stop = True
        self.proc.terminate()

        def _force():
            if self.running():
                self.proc.kill()

        QTimer.singleShot(3000, _force)

    def stop_sync(self, timeout_ms: int = 3000):
        """同步停止(退出程序时使用), 确保子进程不残留"""
        if not self.running():
            return
        self._user_stop = True
        self.proc.terminate()
        if not self.proc.waitForFinished(1500):
            self.proc.kill()
            self.proc.waitForFinished(timeout_ms)

    # -- 输出处理 ------------------------------------------------------------

    def _on_ready(self):
        if not self.proc:
            return
        data = bytes(self.proc.readAll())
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            text = data.decode("gbk", errors="replace")
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._handle_line(line)

    def _handle_line(self, line: str):
        ev, clean = LogParser.parse(line)

        # 多行 TOML 配置缓冲: update frpc 日志含 TOML 配置(跨多行)
        # 首行有时间戳, 后续行是裸 TOML, 直到下一行时间戳出现
        if self._in_update:
            if re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", clean):
                # 新的带时间戳行 -> TOML 块结束
                self._parse_toml_buf()
                self._in_update = False
                self._toml_buf = []
            else:
                self._toml_buf.append(clean)

        if "update frpc" in clean and "req:" in clean:
            self._in_update = True
            self._toml_buf = [clean]

        shown = mask_secret(clean, self._secret)
        self._write_log(shown)
        self.logLine.emit(shown)
        if ev and ev["type"] != "raw":
            self.event.emit(ev)

    def _parse_toml_buf(self):
        """从 update frpc 的多行日志中解析 TOML proxy 配置"""
        if not self._toml_buf:
            return
        text = "\n".join(self._toml_buf)
        # 匹配 [[proxies]] 块中的 name/type/localIP/localPort
        proxy_blocks = re.findall(
            r"\[\[proxies\]\]\s*\n(.*?)(?=\n\[\[|\n\[\s*$|$)",
            text, re.DOTALL
        )
        for block in proxy_blocks:
            name_m = re.search(r'name\s*=\s*"([^"]+)"', block)
            type_m = re.search(r'type\s*=\s*"([^"]+)"', block)
            ip_m = re.search(r'localIP\s*=\s*"([^"]*)"', block)
            port_m = re.search(r'localPort\s*=\s*(\d+)', block)
            if name_m:
                ev = {
                    "type": "proxy_new",
                    "name": name_m.group(1),
                    "ptype": type_m.group(1) if type_m else "",
                    "local_ip": ip_m.group(1) if ip_m else "",
                    "local_port": port_m.group(1) if port_m else "",
                }
                self.event.emit(ev)
                self.logLine.emit(
                    f"[GUI] 解析到隧道: {ev['name']} ({ev['ptype'] or '?'}) -> {ev['local_ip'] or '?'}:{ev['local_port'] or '?'}"
                )

    # -- 进程结束 ------------------------------------------------------------

    def _on_finished(self, code, status):
        if self._buf:
            self._handle_line(self._buf)
            self._buf = ""
        self._close_log()
        self.procFinished.emit(int(code))
        # 崩溃自动重启(非用户主动停止 + 异常退出码)
        if not self._user_stop and code != 0 and self._last_params:
            self._restart_count += 1
            if self._restart_count <= 5:
                delay = min(self._restart_count * 3, 15)  # 3s, 6s, 9s, 12s, 15s
                self.logLine.emit(
                    f"[GUI] 客户端异常退出(码 {code}), {delay} 秒后自动重试 (第 {self._restart_count} 次)…"
                )
                QTimer.singleShot(delay * 1000, lambda: self._auto_restart())
            else:
                self.logLine.emit("[GUI] 客户端多次重启失败, 已停止自动重试, 请检查日志或手动重启")
                self._restart_count = 0

    def _auto_restart(self):
        if self._user_stop:
            self._restart_count = 0
            return
        if self._last_params and not self.running():
            self.logLine.emit(f"[GUI] 正在自动重启客户端…")
            self.start(self._last_params)

    def reset_restart_count(self):
        """用户主动启动时重置重启计数"""
        self._restart_count = 0

    def _on_proc_error(self, err):
        msg = {
            QProcess.ProcessError.FailedToStart: "客户端启动失败: 文件不存在或无执行权限",
            QProcess.ProcessError.Crashed: "客户端进程崩溃",
            QProcess.ProcessError.Timedout: "客户端进程等待超时",
        }.get(err, f"客户端进程错误: {err}")
        if err == QProcess.ProcessError.FailedToStart:
            self._close_log()
        self.procError.emit(msg)

    # -- 日志文件 ------------------------------------------------------------

    def _open_log(self):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            if CLIENT_LOG.exists() and CLIENT_LOG.stat().st_size > LOG_ROTATE_SIZE:
                CLIENT_LOG_OLD.unlink(missing_ok=True)
                CLIENT_LOG.replace(CLIENT_LOG_OLD)
            self._log_fh = open(CLIENT_LOG, "a", encoding="utf-8", errors="replace")
            self._log_fh.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} client start =====\n")
            self._log_fh.flush()
        except Exception:
            self._log_fh = None

    def _write_log(self, line: str):
        if self._log_fh is None:
            return
        try:
            self._log_fh.write(line + "\n")
            self._log_size += 1
            if self._log_size % 50 == 0:
                self._log_fh.flush()
        except Exception:
            pass

    def _close_log(self):
        if self._log_fh:
            try:
                self._log_fh.flush()
                self._log_fh.close()
            except Exception:
                pass
            self._log_fh = None
        self._log_size = 0

    # -- 冲突检测 ------------------------------------------------------------

    @staticmethod
    def _detect_legacy_service() -> bool:
        """检测官方 install.ps1 安装的服务是否在运行"""
        try:
            import subprocess
            r = subprocess.run(
                ["sc", "query", "frpp"],
                capture_output=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
                text=True, encoding="utf-8", errors="replace",
            )
            return "RUNNING" in (r.stdout or "")
        except Exception:
            return False

# ----------------------------------------------------------------------------
# 管理服务器 RPC (gRPC 直连拉取隧道配置, 无需面板账号)
# ----------------------------------------------------------------------------
# 链路: POST /api/v1/auth/cert (clientID+secret) 换 master CA 证书
#       -> gRPC TLS 直连 master.Master/PullClientConfig
#       -> shadow 机制: 配置在子 client (原ID@N), 递归拉取合并
# 配置是 frp v1 JSON 格式, protobuf 字段编号来自 frp-panel pb 定义:
#   PullClientConfigReq{255: base};  ClientBase{1: clientId, 2: clientSecret}
#   PullClientConfigResp{1: status, 2: client};  Client{3: config, 8: clientIds}

def _pb_varint(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            out += bytes([b])
            return out


def _pb_enc_msg(field: int, data: bytes) -> bytes:
    return _pb_varint((field << 3) | 2) + _pb_varint(len(data)) + data


def _pb_dec_varint(data: bytes, i: int):
    r = 0
    s = 0
    while True:
        b = data[i]
        i += 1
        r |= (b & 0x7F) << s
        if not (b & 0x80):
            return r, i
        s += 7


def _pb_dec_msg(data: bytes) -> dict:
    """protobuf wire bytes -> {field_num: [values]}"""
    out = {}
    i = 0
    n = len(data)
    while i < n:
        tag, i = _pb_dec_varint(data, i)
        field, wire = tag >> 3, tag & 7
        if wire == 0:
            v, i = _pb_dec_varint(data, i)
        elif wire == 2:
            ln, i = _pb_dec_varint(data, i)
            v = data[i:i + ln]
            i += ln
        elif wire == 5:
            v = data[i:i + 4]
            i += 4
        elif wire == 1:
            v = data[i:i + 8]
            i += 8
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        out.setdefault(field, []).append(v)
    return out


def _rpc_fetch_ca(api_base: str, client_id: str, secret: str) -> str:
    """用客户端凭据换取 master CA 证书 PEM"""
    url = api_base.rstrip("/") + "/api/v1/auth/cert"
    body = json.dumps({"clientId": client_id, "clientSecret": secret,
                       "clientType": 1}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    # gin 统一响应: {code, msg, body:{status, cert}}
    inner = data.get("body") if isinstance(data.get("body"), dict) else data
    cert = inner.get("cert") or data.get("cert") or ""
    if not cert:
        raise RuntimeError("cert 接口未返回证书")
    if "BEGIN CERTIFICATE" not in cert:
        cert = base64.b64decode(cert).decode("ascii", errors="replace")
    if "BEGIN CERTIFICATE" not in cert:
        raise RuntimeError("cert 接口返回内容无法识别")
    return cert


def _rpc_grpc_pull(host_port: str, ca_pem: str, client_id: str, secret: str) -> bytes:
    """gRPC 直连 master 调 PullClientConfig, 返回原始响应 bytes"""
    import grpc  # 延迟导入: 源码运行时缺少则给出友好提示
    creds = grpc.ssl_channel_credentials(root_certificates=ca_pem.encode())
    # frp-panel 默认证书签给 127.0.0.1(SAN), 客户端官方实现同样跳过主机名校验,
    # 这里用 SAN 值做 override 保住证书链校验
    req = _pb_enc_msg(255, _pb_enc_msg(1, client_id.encode()) + _pb_enc_msg(2, secret.encode()))
    last = None
    for override in ("127.0.0.1", None):
        opts = [("grpc.ssl_target_name_override", override)] if override else []
        ch = grpc.secure_channel(host_port, creds, options=opts)
        try:
            stub = ch.unary_unary("/master.Master/PullClientConfig")
            return stub(req, timeout=15)
        except Exception as e:
            last = e
        finally:
            ch.close()
    raise last


def _rpc_parse_client(resp: bytes) -> dict:
    """解析 PullClientConfigResp 里的 Client 消息"""
    fields = _pb_dec_msg(resp)
    client = {}
    if 2 in fields:
        cl = _pb_dec_msg(fields[2][0])
        if 3 in cl:
            client["config"] = cl[3][0].decode("utf-8", errors="replace")
        if 8 in cl:
            client["clientIds"] = [c.decode("utf-8", errors="replace") for c in cl[8]]
    return client


def _rpc_parse_config(cfg_text: str):
    """解析 frpc 配置(frp v1 JSON)为 (隧道列表, 用户名); 兼容 TOML 兕底
    frp 运行时 proxy 全名是 user.name, 需要用户名来对齐日志事件"""
    try:
        data = json.loads(cfg_text)
    except Exception:
        return _rpc_parse_config_toml(cfg_text), ""
    user = data.get("user") or ""
    out = []
    for p in data.get("proxies") or []:
        name = p.get("name") or "?"
        ptype = p.get("type") or "?"
        lip = p.get("localIP") or "127.0.0.1"
        lport = p.get("localPort")
        local = f"{lip}:{lport}" if lport not in (None, "") else str(lip)
        remote = str(p.get("remotePort") or "")
        if not remote:
            doms = p.get("customDomains") or []
            remote = (doms[0] if doms else "") or p.get("subdomain") or ""
        if not remote and ptype in ("stcp", "xtcp", "sudp"):
            remote = "P2P"
        out.append({"name": name, "type": ptype, "local": local,
                    "remote": remote or "—"})
    for v in data.get("visitors") or []:
        name = v.get("name") or "?"
        vtype = (v.get("type") or "?") + " 访问端"
        bind = f"{v.get('bindAddr') or ''}:{v.get('bindPort') or ''}"
        out.append({"name": name, "type": vtype, "local": bind, "remote": "—"})
    return out, user


def _rpc_parse_config_toml(cfg_text: str) -> list:
    """TOML 兕底: [[proxies]] 块正则提取"""
    out = []
    for blk in re.split(r"(?m)^\s*\[\[proxies\]\]", cfg_text)[1:]:
        def _m(k, cast=str):
            m = re.search(rf"(?m)^\s*{k}\s*=\s*['\"]?([^'\"\n#]*)", blk)
            return cast(m.group(1).strip()) if m else ""
        name = _m("name")
        if not name:
            continue
        ptype = _m("type") or "?"
        lip = _m("localIP") or "127.0.0.1"
        lport = _m("localPort")
        remote = _m("remotePort")
        if not remote and ptype in ("stcp", "xtcp", "sudp"):
            remote = "P2P"
        out.append({"name": name, "type": ptype,
                    "local": f"{lip}:{lport}" if lport else lip,
                    "remote": remote or "—"})
    return out


def master_pull_proxies(params: dict) -> dict:
    """拉取该客户端在管理服务器上的全部隧道配置(含 shadow 子 client)
    返回 {proxies: [...], user: str}(frp 运行时 proxy 全名为 user.name)"""
    secret = params.get("secret", "")
    client_id = params.get("client_id", "")
    rpc_url = params.get("rpc_url", "")
    api_url = params.get("api_url", "")
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^/]+)", rpc_url)
    host_port = (m.group(1) if m else rpc_url).strip()
    if not secret or not client_id or not host_port:
        raise RuntimeError("缺少客户端凭据配置")
    # api-url 未填时从 rpc 主机推导默认端口
    api_base = api_url or ("http://" + host_port.rsplit(":", 1)[0] + ":9000")

    # CA: 优先缓存, 失败时重新换取
    ca = None
    try:
        if CA_CACHE_FILE.exists():
            ca = CA_CACHE_FILE.read_text("ascii", errors="replace")
            if "BEGIN CERTIFICATE" not in ca:
                ca = None
    except Exception:
        ca = None
    if not ca:
        ca = _rpc_fetch_ca(api_base, client_id, secret)
        CA_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CA_CACHE_FILE.write_text(ca, "ascii")

    try:
        resp = _rpc_grpc_pull(host_port, ca, client_id, secret)
    except Exception:
        # 证书可能已更换, 强刷 CA 后重试一次
        ca = _rpc_fetch_ca(api_base, client_id, secret)
        CA_CACHE_FILE.write_text(ca, "ascii")
        resp = _rpc_grpc_pull(host_port, ca, client_id, secret)

    client = _rpc_parse_client(resp)
    configs = []
    if client.get("config"):
        configs.append(client["config"])
    # shadow 机制: 配置在子 client (原ID@N), 用同样凭据拉取
    for child_id in client.get("clientIds") or []:
        try:
            r2 = _rpc_grpc_pull(host_port, ca, child_id, secret)
            c2 = _rpc_parse_client(r2)
            if c2.get("config"):
                configs.append(c2["config"])
        except Exception:
            continue

    proxies = []
    user = ""
    for cfg in configs:
        plist, u = _rpc_parse_config(cfg)
        proxies.extend(plist)
        user = user or u
    # 按 name 去重 (同名以先出现的为准)
    seen = {}
    for p in proxies:
        seen.setdefault(p["name"], p)
    return {"proxies": list(seen.values()), "user": user}


class MasterPoller(QThread):
    """后台线程执行 master_pull_proxies, 避免网络阻塞 UI"""
    ok = Signal(dict)
    err = Signal(str)

    def __init__(self, params: dict, parent=None):
        super().__init__(parent)
        self.params = params

    def run(self):
        try:
            proxies = master_pull_proxies(self.params)
            self.ok.emit(proxies)
        except ImportError as e:
            self.err.emit(f"隧道同步需要 grpcio 库: {e}")
        except Exception as e:
            self.err.emit(str(e))


# ----------------------------------------------------------------------------
# 界面辅助
# ----------------------------------------------------------------------------

def make_icon(status_color: str = None) -> QIcon:
    """程序/托盘图标: frp 图标(base64 内嵌) + 右下角状态色点"""
    base = QPixmap(64, 64)
    base.fill(Qt.GlobalColor.transparent)
    # 从内嵌 base64 加载 frp 图标, 不依赖外部文件
    try:
        from frp_icon import FRP_ICON_B64
        src = QPixmap()
        if src.loadFromData(base64.b64decode(FRP_ICON_B64)):
            base = src.scaled(64, 64, Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
    except Exception:
        pass
    if base.isNull() or base.size().isEmpty():
        # 回退: 自绘圆点
        base = QPixmap(64, 64)
        base.fill(Qt.GlobalColor.transparent)
        p = QPainter(base)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(PRIMARY)))
        p.drawEllipse(4, 4, 56, 56)
        p.end()
    if not status_color:
        return QIcon(base)
    # 叠加右下角状态色点
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.drawPixmap(0, 0, base)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor("#FFFFFF"), 3))
    p.setBrush(QBrush(QColor(status_color)))
    p.drawEllipse(38, 38, 22, 22)
    p.end()
    return QIcon(pm)


def make_eye_icon() -> QIcon:
    """眼睛图标(显示/隐藏切换), 自绘"""
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QPen(QColor("#7A8699"), 5))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    # 眼眶
    p.drawEllipse(10, 22, 44, 22)
    # 瞳孔
    p.setBrush(QBrush(QColor("#7A8699")))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(26, 26, 12, 12)
    p.end()
    return QIcon(pm)


STATUS_TEXT = {
    ST_UNCONFIGURED: ("未配置", GRAY, "请粘贴启动命令完成配置"),
    ST_STOPPED: ("已停止", GRAY, "客户端未在运行"),
    ST_STARTING: ("连接中…", YELLOW, "正在连接管理服务器"),
    ST_CONNECTED: ("已连接", GREEN, "隧道服务正常运行中"),
    ST_ERROR: ("异常", RED, "请查看日志了解详情"),
}


def build_stylesheet() -> str:
    return f"""
    QMainWindow, QWidget {{ background: #ffffff; font-size: 13px; color: #2A3547; }}
    QLabel#titleLabel {{ font-size: 18px; font-weight: 600; }}
    QLabel#subTitleLabel {{ color: #7A8699; font-size: 12px; }}
    QLabel#statusText {{ font-size: 22px; font-weight: 700; }}
    QLabel#statusSub {{ color: #7A8699; font-size: 12px; }}
    QLabel#infoKey {{ color: #7A8699; font-size: 12px; }}
    QLabel#infoVal {{ font-size: 12px; }}
    QPushButton {{
        background: {PRIMARY}; color: #ffffff; border: none;
        border-radius: 4px; padding: 7px 18px; font-weight: 500;
    }}
    QPushButton:hover {{ background: #1F6AE0; }}
    QPushButton:disabled {{ background: #C3CFDF; }}
    QPushButton[text="停止"] {{ background: #EEF1F6; color: #5B6B7F; border: 1px solid #D7DEE8; }}
    QPushButton[text="停止"]:hover {{ background: #E3E9F2; }}
    QPlainTextEdit, QLineEdit {{
        border: 1px solid #D7DEE8; border-radius: 4px; padding: 6px;
        font-family: Consolas, 'Courier New', monospace; font-size: 12px;
        background: #FCFDFE; selection-background-color: #CFE0FC;
    }}
    QPlainTextEdit:focus, QLineEdit:focus {{ border-color: {PRIMARY}; }}
    QTabWidget::pane {{ border: 1px solid #E4EAF2; border-radius: 4px; top: -1px; }}
    QTabBar::tab {{
        padding: 8px 22px; color: #7A8699; background: transparent;
        border-bottom: 2px solid transparent; font-weight: 500;
    }}
    QTabBar::tab:selected {{ color: {PRIMARY}; border-bottom: 2px solid {PRIMARY}; }}
    QTableWidget {{
        border: none; gridline-color: #F0F3F8;
        alternate-background-color: #FAFBFD;
    }}
    QHeaderView::section {{
        background: #F7FAFD; border: none; border-bottom: 1px solid #E4EAF2;
        padding: 7px 8px; color: #7A8699; font-weight: 500;
    }}
    QTableWidget::item {{ padding: 5px 8px; }}
    QProgressBar {{
        border: 1px solid #D7DEE8; border-radius: 3px;
        text-align: center; background: #F4F7FB; height: 16px;
    }}
    QProgressBar::chunk {{ background: {PRIMARY}; border-radius: 2px; }}
    QCheckBox {{ spacing: 8px; }}
    QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 3px; border: 1px solid #C3CFDF; }}
    QCheckBox::indicator:checked {{ background: {PRIMARY}; border-color: {PRIMARY}; }}
    QFrame#card {{ background: #F8FAFD; border: 1px solid #E4EAF2; border-radius: 6px; }}
    /* 卡片内的文字/复选框不要继承白色背景, 否则会盖住卡片底色 */
    QLabel, QCheckBox, QRadioButton {{ background: transparent; }}
    QPushButton#eyeBtn {{
        border: none; background: transparent;
        padding: 0; margin: 0;
    }}
    QPushButton#eyeBtn:hover {{ background: #EDF3FC; border-radius: 4px; }}
    """


# ----------------------------------------------------------------------------
# 首次配置页
# ----------------------------------------------------------------------------

class ConfigPage(QWidget):
    """粘贴启动命令 -> 解析 -> 保存并启动"""
    parsed = Signal(dict)   # 参数解析成功
    def __init__(self, existing: str = ""):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(40, 30, 40, 30)
        lay.setSpacing(10)

        title = QLabel("欢迎使用 FRP Panel 客户端")
        title.setObjectName("titleLabel")
        sub = QLabel("请粘贴管理面板提供的客户端启动命令（支持完整安装命令，或 frp-panel client 命令）")
        sub.setObjectName("subTitleLabel")
        sub.setWordWrap(True)
        lay.addWidget(title)
        lay.addWidget(sub)

        self.edit = QPlainTextEdit()
        self.edit.setPlaceholderText(
            "frp-panel client -s xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx -i your.client.id "
            "--api-url http://master:9000 --rpc-url grpc://master:9001"
        )
        self.edit.setPlainText(existing)
        self.edit.setFixedHeight(110)
        lay.addWidget(self.edit)

        self.preview = QLabel("")
        self.preview.setWordWrap(True)
        self.preview.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.preview)

        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_save = QPushButton("保存并启动")
        self.btn_save.setEnabled(False)
        self.btn_save.setMinimumWidth(140)
        row.addWidget(self.btn_save)
        lay.addLayout(row)

        tip = QLabel("配置保存在本机，密钥不会上传到任何第三方")
        tip.setObjectName("subTitleLabel")
        lay.addWidget(tip)
        lay.addStretch(1)

        self.edit.textChanged.connect(self._reparse)
        self.btn_save.clicked.connect(self._on_save)
        self._reparse()

    def _reparse(self):
        params = CommandParser.parse(self.edit.toPlainText())
        if params:
            cid = params["client_id"]
            self.preview.setText(
                f'<span style="color:{GREEN};">✓ 解析成功</span>'
                f'<span style="color:#7A8699;"> · 客户端 ID: {cid} · 服务器: {params["rpc_url"]}</span>'
            )
            self.btn_save.setEnabled(True)
        else:
            txt = self.edit.toPlainText().strip()
            if txt:
                self.preview.setText(f'<span style="color:{RED};">未识别到客户端参数，需要包含 -s、-i 和 --rpc-url</span>')
            else:
                self.preview.setText("")
            self.btn_save.setEnabled(False)

    def _on_save(self):
        params = CommandParser.parse(self.edit.toPlainText())
        if params:
            self.parsed.emit(params)


# ----------------------------------------------------------------------------
# 主窗口
# ----------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self, cfg: ConfigStore, start_minimized: bool = False):
        super().__init__()
        self.cfg = cfg
        self.status = ST_UNCONFIGURED
        self.last_error = ""
        self.last_hb = None          # datetime
        self.client_version = ""
        self.proxies = {}            # name -> {type, local, remote, state}
        self._proxy_user = ""         # frp 运行时 proxy 全名前缀(user.)
        self._first_hide_hint = False
        self.downloader = None
        self.poller = None           # 隧道配置拉取线程

        self.setWindowTitle(APP_TITLE)
        self.setWindowIcon(make_icon(GRAY))
        self.resize(720, 540)
        self.setMinimumSize(620, 480)

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        # -- 页面 0: 首次配置 --------------------------------------------------
        self.config_page = ConfigPage()
        self.config_page.parsed.connect(self._on_config_saved)
        self.stack.addWidget(self.config_page)

        # -- 页面 1: 主界面 ----------------------------------------------------
        main = QWidget()
        self.stack.addWidget(main)
        ml = QVBoxLayout(main)
        ml.setContentsMargins(16, 12, 16, 12)
        self.tabs = QTabWidget()
        ml.addWidget(self.tabs)
        self.tabs.addTab(self._build_status_tab(), "状态")
        self.tabs.addTab(self._build_proxy_tab(), "隧道服务")
        self.tabs.addTab(self._build_log_tab(), "日志")

        # -- 客户端进程 --------------------------------------------------------
        self.client = ClientProcess(self)
        self.client.logLine.connect(self._append_log)
        self.client.event.connect(self._on_client_event)
        self.client.procStarted.connect(self._on_proc_started)
        self.client.procFinished.connect(self._on_proc_finished)
        self.client.procError.connect(self._on_proc_error)

        # -- 心跳超时检查 ------------------------------------------------------
        self.hb_timer = QTimer(self)
        self.hb_timer.setInterval(30000)
        self.hb_timer.timeout.connect(self._check_heartbeat)

        # -- 隧道配置轮询 (连接期间每 60s 从 master 同步一次) ----------------
        self.rpc_timer = QTimer(self)
        self.rpc_timer.setInterval(60000)
        self.rpc_timer.timeout.connect(lambda: self.refresh_proxies_rpc(silent=True))

        # -- 托盘 ---------------------------------------------------------------
        self._build_tray()

        # -- 初始化状态 ---------------------------------------------------------
        self._refresh_config_view()
        if self.cfg.configured:
            if start_minimized:
                # 托盘模式: 强制创建原生窗口但不上屏, 修复从未 show 过的窗口
                # 后续 show() 时 Qt 状态机与平台层不一致的问题
                self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
                self.show()
                self.hide()
                self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, False)
            else:
                self.show()
            if self.cfg.data.get("autostart_on_launch", True):
                QTimer.singleShot(100, self.start_client)
        else:
            if start_minimized:
                # 未配置 + 托盘模式: 同样先创建再隐藏
                self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
                self.show()
                self.hide()
                self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, False)
            else:
                self.show()

    # ==================== 界面构建 ============================================

    def _build_status_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 14, 8, 8)
        v.setSpacing(12)

        # 状态卡
        card = QFrame()
        card.setObjectName("card")
        cv = QVBoxLayout(card)
        cv.setContentsMargins(20, 16, 20, 16)
        head = QHBoxLayout()
        self.dot = QLabel("●")
        self.dot.setFixedWidth(30)
        f = QFont()
        f.setPointSize(20)
        self.dot.setFont(f)
        tv = QVBoxLayout()
        self.status_label = QLabel("已停止")
        self.status_label.setObjectName("statusText")
        self.status_sub = QLabel("")
        self.status_sub.setObjectName("statusSub")
        self.status_sub.setWordWrap(True)
        tv.addWidget(self.status_label)
        tv.addWidget(self.status_sub)
        head.addWidget(self.dot)
        head.addLayout(tv)
        head.addStretch(1)
        self.btn_toggle = QPushButton("启动")
        self.btn_toggle.setMinimumWidth(110)
        head.addWidget(self.btn_toggle)
        cv.addLayout(head)
        v.addWidget(card)

        # 信息卡(全宽, 与状态卡对齐; 仅限制左列标签宽度)
        info = QFrame()
        info.setObjectName("card")
        g = QGridLayout(info)
        g.setContentsMargins(20, 14, 20, 14)
        g.setHorizontalSpacing(18)
        g.setVerticalSpacing(9)
        g.setColumnStretch(0, 0)   # 标签列固定宽
        g.setColumnStretch(1, 1)   # 值列可伸展
        self.lbl_cid = self._info_row(g, 0, "客户端 ID", "—", 130)
        self.lbl_ver = self._info_row(g, 1, "客户端版本", "—", 130)
        self.lbl_rpc = self._info_row(g, 2, "管理服务器 (RPC)", "—", 130, maskable=True)
        self.lbl_api = self._info_row(g, 3, "面板地址 (API)", "—", 130, maskable=True)
        self.lbl_hb = self._info_row(g, 4, "最近心跳", "—", 130)
        self.lbl_err = self._info_row(g, 5, "最近错误", "—", 130)
        # 错误信息可能很长: 允许折行 + 悬停看全文
        self.lbl_err.setWordWrap(True)
        self.lbl_err.setTextFormat(Qt.TextFormat.PlainText)
        self.lbl_err.setToolTip("")
        v.addWidget(info)

        # 设置行
        row = QHBoxLayout()
        self.chk_autostart = QCheckBox("开机自动启动（启动后最小化到托盘）")
        self.chk_autostart.setChecked(AutoStart.enabled())
        self.chk_autostart.toggled.connect(self._on_autostart_toggled)
        row.addWidget(self.chk_autostart)
        row.addStretch(1)
        self.btn_reconfig = QPushButton("修改配置")
        self.btn_reconfig.clicked.connect(self._show_reconfig_dialog)
        row.addWidget(self.btn_reconfig)
        self.btn_update = QPushButton("更新客户端程序")
        self.btn_update.clicked.connect(self.download_client)
        row.addWidget(self.btn_update)
        v.addLayout(row)

        # 下载进度
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setFormat("下载中… %p% (%v / %m)")
        v.addWidget(self.progress)
        v.addStretch(1)

        self.btn_toggle.clicked.connect(self._on_toggle)
        return w

    def _info_row(self, g: QGridLayout, row: int, key: str, val: str,
                  key_width: int = None, maskable: bool = False) -> QLabel:
        k = QLabel(key)
        k.setObjectName("infoKey")
        if key_width:
            k.setFixedWidth(key_width)
        v = QLabel(val)
        v.setObjectName("infoVal")
        v.setWordWrap(True)
        g.addWidget(k, row, 0)
        if maskable:
            # 可脱敏行: 值 + 眼睛按钮(默认脱敏, 点亮眼睛显示明文)
            wrap = QHBoxLayout()
            wrap.setContentsMargins(0, 0, 0, 0)
            wrap.setSpacing(6)
            wrap.addWidget(v, 1)
            btn = QPushButton()
            btn.setObjectName("eyeBtn")
            btn.setFixedSize(24, 24)
            btn.setIcon(make_eye_icon())
            btn.setIconSize(QSize(16, 16))
            btn.setFlat(True)
            btn.setToolTip("显示/隐藏完整地址")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setCheckable(True)
            state = {"plain": False}

            def _toggle(on, lbl=v, st=state):
                st["plain"] = on
                if not lbl._plain_text or lbl._plain_text == "—":
                    return
                lbl.setText(lbl._plain_text if on else lbl._masked_text)

            btn.toggled.connect(_toggle)
            v._show_plain = state
            wrap.addWidget(btn)
            g.addLayout(wrap, row, 1)
        else:
            g.addWidget(v, row, 1)
        v._plain_text = val
        v._masked_text = self._mask_url(val)
        if maskable and val and val != "—":
            v.setText(v._masked_text)
        return v

    @staticmethod
    def _mask_url(url: str) -> str:
        """grpc://panel.example.com:9001 -> grpc://pane********:9001
        非地址文本(—/未配置等)原样返回"""
        if not url or url == "—" or "://" not in url:
            return url
        m = re.match(r"([a-zA-Z]+://)([^:/]+)(:\d+)?(.*)", url)
        if not m:
            return url
        scheme, host, port, rest = m.groups()
        if len(host) <= 3:
            masked_host = "*" * len(host)
        else:
            keep = max(2, len(host) // 3)
            masked_host = host[:keep] + "*" * max(4, len(host) - keep)
        return f"{scheme}{masked_host}{port or ''}{rest or ''}"

    def _build_proxy_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 14, 8, 8)
        v.setSpacing(8)
        head = QHBoxLayout()
        tip = QLabel("隧道在管理面板上配置，此处实时显示；类型与本地地址自动从管理服务器同步")
        tip.setObjectName("subTitleLabel")
        head.addWidget(tip)
        head.addStretch(1)
        btn = QPushButton("刷新")
        btn.clicked.connect(lambda: self.refresh_proxies_rpc(silent=False))
        head.addWidget(btn)
        v.addLayout(head)

        self.proxy_table = QTableWidget(0, 5)
        self.proxy_table.setHorizontalHeaderLabels(["名称", "类型", "本地地址", "远程端口", "状态"])
        self.proxy_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.proxy_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.proxy_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.proxy_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.proxy_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.proxy_table.verticalHeader().setVisible(False)
        self.proxy_table.setAlternatingRowColors(True)
        self.proxy_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.proxy_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        v.addWidget(self.proxy_table)

        self.proxy_empty = QLabel(
            "暂无隧道。该客户端已就绪，请在 frp-panel 管理面板上为它添加隧道，添加后会自动同步到这里。"
        )
        self.proxy_empty.setObjectName("subTitleLabel")
        self.proxy_empty.setWordWrap(True)
        self.proxy_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.proxy_empty)
        return w

    def _build_log_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 14, 8, 8)
        v.setSpacing(8)
        row = QHBoxLayout()
        self.chk_autoscroll = QCheckBox("自动滚动")
        self.chk_autoscroll.setChecked(True)
        row.addWidget(self.chk_autoscroll)
        row.addStretch(1)
        btn_copy = QPushButton("复制")
        btn_copy.clicked.connect(self._copy_log)
        btn_clear = QPushButton("清空")
        btn_clear.clicked.connect(lambda: self.log_view.clear())
        row.addWidget(btn_copy)
        row.addWidget(btn_clear)
        v.addLayout(row)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(3000)
        f = QFont("Consolas")
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setPointSize(9)
        self.log_view.setFont(f)
        v.addWidget(self.log_view)
        return w

    def _build_tray(self):
        self.tray_menu = QMenu()
        act_show = QAction("显示主界面", self)
        act_show.triggered.connect(self.show_and_raise)
        act_start = QAction("启动客户端", self)
        act_start.triggered.connect(self.start_client)
        act_stop = QAction("停止客户端", self)
        act_stop.triggered.connect(self.stop_client)
        self.act_autostart = QAction("开机自动启动", self)
        self.act_autostart.setCheckable(True)
        self.act_autostart.setChecked(AutoStart.enabled())
        self.act_autostart.toggled.connect(self._on_autostart_toggled)
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self.quit_app)

        self.tray_menu.addAction(act_show)
        self.tray_menu.addSeparator()
        self.tray_menu.addAction(act_start)
        self.tray_menu.addAction(act_stop)
        self.tray_menu.addSeparator()
        self.tray_menu.addAction(self.act_autostart)
        self.tray_menu.addSeparator()
        self.tray_menu.addAction(act_quit)

        self.tray = QSystemTrayIcon(self)
        self.tray.setContextMenu(self.tray_menu)
        self.tray.setIcon(make_icon(GRAY))
        self.tray.setToolTip(APP_TITLE)
        self.tray.activated.connect(
            lambda reason: self.show_and_raise()
            if reason == QSystemTrayIcon.ActivationReason.DoubleClick
            else None
        )
        self.tray.show()

    # ==================== 状态管理 ============================================

    def _set_status(self, st: str, sub: str = None):
        prev = self.status
        self.status = st
        text, color, default_sub = STATUS_TEXT[st]
        self.status_label.setText(text)
        self.status_sub.setText(sub if sub is not None else default_sub)
        self.dot.setStyleSheet(f"color: {color}; font-size: 20px;")
        self.status_sub.setStyleSheet(f"color: {RED if st == ST_ERROR else '#7A8699'};")
        self.btn_toggle.setText("停止" if self.client.running() else "启动")
        icon_color = {"unconfigured": GRAY, "stopped": GRAY, "starting": YELLOW,
                      "connected": GREEN, "error": RED}.get(st, GRAY)
        self.tray.setIcon(make_icon(icon_color))
        self.tray.setToolTip(f"{APP_TITLE} - {text}")
        self.setWindowIcon(make_icon(icon_color))
        if prev != st and st == ST_CONNECTED and prev in (ST_STARTING, ST_ERROR):
            self.tray.showMessage(APP_TITLE, "已连接到管理服务器", QSystemTrayIcon.MessageIcon.Information, 3000)
        # 已连接时开启隧道配置轮询, 其余状态停止
        if st == ST_CONNECTED:
            if not self.rpc_timer.isActive():
                self.rpc_timer.start()
        else:
            self.rpc_timer.stop()

    def _refresh_config_view(self):
        if not self.cfg.configured:
            self.stack.setCurrentIndex(0)
            self._set_status(ST_UNCONFIGURED)
            return
        self.stack.setCurrentIndex(1)
        c = self.cfg.client
        self.lbl_cid.setText(c.get("client_id", "—"))
        # RPC/API 是可脱敏行: 更新明文缓存, 按眼睛当前状态显示
        for lbl, key in ((self.lbl_rpc, "rpc_url"), (self.lbl_api, "api_url")):
            text = c.get(key) or "未配置"
            lbl._plain_text = text
            lbl._masked_text = self._mask_url(text)
            show_plain = bool(lbl._show_plain and lbl._show_plain.get("plain"))
            lbl.setText(text if (show_plain and "://" in text) else lbl._masked_text)
        self.lbl_ver.setText(self.client_version or "—")
        self._set_status(ST_STOPPED)

    # ==================== 客户端控制 ==========================================

    def start_client(self):
        if not self.cfg.configured:
            return
        if self.client.running():
            return
        if not CLIENT_EXE.exists():
            self._append_log(f"[GUI] 未找到客户端程序, 开始自动下载…")
            self.download_client(autostart_after=True)
            return
        self.proxies.clear()
        self._render_proxies()
        self.last_hb = None
        self.lbl_hb.setText("—")
        self.client.start(self.cfg.client)
        self._set_status(ST_STARTING)
        self.hb_timer.start()

    def stop_client(self):
        self.hb_timer.stop()
        self.client.stop()
        self._set_status(ST_STOPPED)

    def restart_client_silent(self):
        if self.client.running():
            self.client.stop()
            QTimer.singleShot(1500, self.start_client)
        else:
            self.start_client()

    def _on_toggle(self):
        if self.client.running():
            self.stop_client()
        else:
            self.start_client()

    def download_client(self, autostart_after: bool = False):
        if self.downloader and self.downloader.isRunning():
            return
        self.tabs.setCurrentIndex(0)  # 切到状态页让用户看到下载进度
        self.progress.setVisible(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.btn_update.setEnabled(False)
        self._append_log(f"[GUI] 正在下载 frp-panel 客户端 ({len(DOWNLOAD_URLS)} 个镜像依次尝试)…")
        self.downloader = Downloader(self)
        self.downloader.progress.connect(self._on_dl_progress)
        self.downloader.finishedOk.connect(lambda path: self._on_dl_ok(path, autostart_after))
        self.downloader.finishedErr.connect(self._on_dl_err)
        self.downloader.start()

    def _on_dl_progress(self, received, total):
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(received)
            self.progress.setFormat(f"下载中… {human_size(received)} / {human_size(total)}")
        else:
            self.progress.setRange(0, 0)

    def _on_dl_ok(self, path: str, autostart_after: bool):
        self.progress.setVisible(False)
        self.btn_update.setEnabled(True)
        self._append_log(f"[GUI] 客户端下载完成: {path}")
        if autostart_after:
            QTimer.singleShot(200, self.start_client)

    def _on_dl_err(self, err: str):
        self.progress.setVisible(False)
        self.btn_update.setEnabled(True)
        self._append_log(f"[GUI] 客户端下载失败: {err}")
        self._set_status(ST_ERROR, "客户端程序下载失败, 请检查网络后重试")

    # ==================== 事件处理 ============================================

    def _on_config_saved(self, params: dict):
        self.cfg.data["client"] = params
        self.cfg.save()
        self._refresh_config_view()
        self._append_log(
            f"[GUI] 配置已保存: 客户端 {params['client_id']} -> {params['rpc_url']}"
        )
        self.start_client()

    def _show_reconfig_dialog(self):
        """弹出对话框, 重新粘贴启动命令修改配置"""
        from PySide6.QtWidgets import QDialog, QDialogButtonBox
        dlg = QDialog(self)
        dlg.setWindowTitle("修改配置")
        dlg.setMinimumWidth(520)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(20, 20, 20, 16)
        tip = QLabel("粘贴新的客户端启动命令，保存后将停止当前客户端并用新配置重启：")
        tip.setWordWrap(True)
        tip.setObjectName("subTitleLabel")
        lay.addWidget(tip)
        edit = QPlainTextEdit()
        # 预填当前配置对应的命令
        c = self.cfg.client
        if c.get("secret"):
            parts = [f"frp-panel client -s {c['secret']} -i {c['client_id']}"]
            if c.get("api_url"):
                parts.append(f"--api-url {c['api_url']}")
            parts.append(f"--rpc-url {c['rpc_url']}")
            edit.setPlainText(" ".join(parts))
        edit.setFixedHeight(100)
        lay.addWidget(edit)
        preview = QLabel("")
        preview.setWordWrap(True)
        preview.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(preview)

        def reparse():
            p = CommandParser.parse(edit.toPlainText())
            if p:
                preview.setText(
                    f'<span style="color:{GREEN};">✓ 解析成功</span>'
                    f'<span style="color:#7A8699;"> · 客户端 ID: {p["client_id"]} · 服务器: {p["rpc_url"]}</span>'
                )
            elif edit.toPlainText().strip():
                preview.setText(f'<span style="color:{RED};">未识别到完整参数（需要 -s、-i 和 --rpc-url）</span>')
            else:
                preview.setText("")
        edit.textChanged.connect(reparse)
        reparse()

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QDialogButtonBox.StandardButton.Save).setText("保存并重启")
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            params = CommandParser.parse(edit.toPlainText())
            if not params:
                return
            # 停掉当前客户端, 保存新配置, 重启
            if self.client.running():
                self.stop_client()
            self.cfg.data["client"] = params
            self.cfg.save()
            self._refresh_config_view()
            self._append_log(
                f"[GUI] 配置已修改: 客户端 {params['client_id']} -> {params['rpc_url']}"
            )
            self.start_client()

    def _on_proc_started(self):
        self.btn_toggle.setText("停止")
        self._set_status(ST_STARTING)

    def _on_proc_finished(self, code: int):
        self.btn_toggle.setText("启动")
        self.last_hb = None
        if self._user_stopped():
            self._set_status(ST_STOPPED)
        elif code != 0:
            self._set_status(ST_ERROR, f"客户端异常退出 (退出码 {code})")
            self.tray.showMessage(APP_TITLE, f"客户端异常退出 (退出码 {code})",
                                  QSystemTrayIcon.MessageIcon.Critical, 5000)
        else:
            self._set_status(ST_STOPPED)

    def _user_stopped(self):
        return bool(self.client._user_stop)

    def _on_proc_error(self, msg: str):
        self._append_log(f"[GUI] {msg}")
        self._set_status(ST_ERROR, msg.splitlines()[0] if msg else "错误")

    def _on_client_event(self, ev: dict):
        t = ev["type"]
        if t == "registered":
            self._set_status(ST_CONNECTED)
            self.refresh_proxies_rpc(silent=True)
        elif t == "config_empty":
            if self.status != ST_CONNECTED:
                self._set_status(ST_CONNECTED, "已连接，面板尚未配置隧道")
            else:
                self.status_sub.setText("已连接 · 面板尚未配置隧道")
            self.refresh_proxies_rpc(silent=True)
        elif t in ("config_pulled", "config_update", "config_refresh"):
            # master 侧配置变化或客户端拉取成功 -> 从 master 同步最新隧道信息
            self.refresh_proxies_rpc(silent=True)
        elif t == "heartbeat":
            self.last_hb = datetime.now()
            self.lbl_hb.setText(self.last_hb.strftime("%H:%M:%S"))
        elif t == "version":
            self.client_version = ev.get("version", "")
            self.lbl_ver.setText(self.client_version)
        elif t == "proxy_new":
            # 日志只可靠提供 name; 类型/地址由 RPC 同步补充
            name = self._strip_proxy_user(ev["name"])
            cur = self.proxies.get(name)
            if cur is None:
                cur = {"type": "—", "local": "—", "remote": "—", "state": "—"}
                self.proxies[name] = cur
            cur["state"] = "已注册"
            self._render_proxies()
        elif t == "proxy_ok":
            name = self._strip_proxy_user(ev["name"])
            cur = self.proxies.get(name)
            if cur is None:
                cur = {"type": "—", "local": "—", "remote": "—", "state": "—"}
                self.proxies[name] = cur
            cur["state"] = "运行中"
            self._render_proxies()
        elif t == "proxy_fail":
            name = self._strip_proxy_user(ev["name"])
            if name in self.proxies:
                self.proxies[name]["state"] = "失败"
                self._render_proxies()
        elif t == "error":
            self.last_error = ev.get("msg", "")
            # 全文显示(已开启自动换行), 悬停气泡兕底
            self.lbl_err.setText(self.last_error)
            self.lbl_err.setToolTip(self.last_error)
            if self.status != ST_CONNECTED:
                self._set_status(ST_ERROR)

    def _check_heartbeat(self):
        if self.status == ST_CONNECTED and self.client.running():
            if self.last_hb and (datetime.now() - self.last_hb).total_seconds() > 120:
                self._set_status(ST_STARTING, "连接似乎中断，正在等待自动重连…")
        elif self.status == ST_STARTING and self.client.running():
            if self.last_hb and (datetime.now() - self.last_hb).total_seconds() < 30:
                self._set_status(ST_CONNECTED)

    # ==================== 隧道配置 RPC 同步 ===================================

    def refresh_proxies_rpc(self, silent: bool = False):
        """从管理服务器拉取隧道配置(后台线程)"""
        if not self.cfg.configured:
            return
        if self.poller and self.poller.isRunning():
            return
        if not silent:
            self._append_log("[GUI] 正在从管理服务器同步隧道配置…")
        self.poller = MasterPoller(self.cfg.client, self)
        self.poller.ok.connect(self._on_rpc_ok)
        self.poller.err.connect(self._on_rpc_err)
        self.poller.start()

    def _on_rpc_ok(self, payload: dict):
        """RPC 拉取成功: 配置信息以 master 为准, 运行状态保留日志观测
        frp 日志里 proxy 全名是 user.name, 记住前缀用于剥比对"""
        proxies = payload.get("proxies") or []
        self._proxy_user = payload.get("user") or ""
        merged = {}
        for p in proxies:
            prev = self.proxies.get(p["name"])
            # 日志事件先于 RPC 到达时 user 前缀未知, 条目以 "user.name" 存在, 归并其状态
            if prev is None and self._proxy_user:
                prev = self.proxies.get(f"{self._proxy_user}.{p['name']}")
            merged[p["name"]] = {
                "type": p["type"], "local": p["local"], "remote": p["remote"],
                "state": prev["state"] if prev else "待运行",
            }
        self.proxies = merged
        self._render_proxies()
        self._append_log(f"[GUI] 已同步隧道配置: {len(proxies)} 条" if proxies
                         else "[GUI] 管理服务器上暂无该客户端的隧道配置")
        if proxies and self.status == ST_CONNECTED and "尚未配置" in self.status_sub.text():
            self.status_sub.setText(STATUS_TEXT[ST_CONNECTED][2])

    def _on_rpc_err(self, msg: str):
        self._append_log(f"[GUI] 隧道配置同步失败: {msg}")

    def _strip_proxy_user(self, raw_name: str) -> str:
        """frp 日志里 proxy 全名是 user.name, 剥去前缀与配置中的 name 对齐"""
        u = self._proxy_user
        if u and raw_name.startswith(u + "."):
            return raw_name[len(u) + 1:]
        return raw_name

    # ==================== 界面更新 ============================================

    def _render_proxies(self):
        names = sorted(self.proxies.keys())
        self.proxy_table.setRowCount(len(names))
        for i, name in enumerate(names):
            p = self.proxies[name]
            self.proxy_table.setItem(i, 0, QTableWidgetItem(name))
            self.proxy_table.setItem(i, 1, QTableWidgetItem(p.get("type", "—")))
            self.proxy_table.setItem(i, 2, QTableWidgetItem(p.get("local", "—")))
            self.proxy_table.setItem(i, 3, QTableWidgetItem(p.get("remote", "—")))
            state_item = QTableWidgetItem(p.get("state", "—"))
            state = p.get("state", "")
            state_item.setForeground(
                QColor(GREEN if state == "运行中" else (RED if state == "失败" else YELLOW))
            )
            self.proxy_table.setItem(i, 4, state_item)
        self.proxy_empty.setVisible(len(names) == 0)
        self.proxy_table.setVisible(len(names) > 0)

    def _append_log(self, line: str):
        if not line:
            return
        sb = self.log_view.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 8
        self.log_view.appendPlainText(line)
        if self.chk_autoscroll.isChecked() and at_bottom:
            sb.setValue(sb.maximum())

    def _copy_log(self):
        QApplication.clipboard().setText(self.log_view.toPlainText())

    # ==================== 自启动 / 窗口管理 ====================================

    def _on_autostart_toggled(self, on: bool):
        ok = AutoStart.set_enabled(on)
        if not ok:
            QMessageBox.warning(self, "提示", "设置开机自启动失败，请手动配置")
            self.chk_autostart.setChecked(AutoStart.enabled())
            self.act_autostart.setChecked(AutoStart.enabled())
            return
        self.chk_autostart.blockSignals(True)
        self.act_autostart.blockSignals(True)
        self.chk_autostart.setChecked(on)
        self.act_autostart.setChecked(on)
        self.chk_autostart.blockSignals(False)
        self.act_autostart.blockSignals(False)
        if on:
            self.tray.showMessage(
                APP_TITLE, "已开启开机自启动，下次开机将自动连接并最小化到托盘",
                QSystemTrayIcon.MessageIcon.Information, 4000,
            )

    def show_and_raise(self):
        # 重置可见性状态机: 先归位窗口状态, 再完整走一次显示流程
        self.setWindowState(Qt.WindowState.WindowNoState)
        self.show()
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()
        # show() 是异步的, 延迟到事件循环处理完显示事件后再补一次激活;
        # 跨进程唤醒场景下 Windows 有前台锁定限制, requestActivate 是更底层的激活途径
        QTimer.singleShot(0, self.activateWindow)
        if self.windowHandle() is not None:
            self.windowHandle().requestActivate()

    def closeEvent(self, event):
        # 关闭按钮 → 隐藏到托盘
        event.ignore()
        self.hide()
        if not self._first_hide_hint:
            self._first_hide_hint = True
            self.tray.showMessage(
                APP_TITLE, "程序已最小化到托盘，右键托盘图标可选择退出",
                QSystemTrayIcon.MessageIcon.Information, 4000,
            )

    def quit_app(self):
        if self.client.running():
            self.client.stop_sync()
        self.tray.hide()
        QApplication.quit()


# ----------------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="FRP Panel 客户端图形管理器")
    parser.add_argument("--tray", action="store_true", help="启动时最小化到托盘(开机自启动使用)")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    app.setStyleSheet(build_stylesheet())

    # 单实例: 若已有实例则唤醒它后退出
    single = SingleInstance()
    if not single.acquire():
        return 0

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cfg = ConfigStore()
    win = MainWindow(cfg, start_minimized=args.tray)
    # 防御: 挂到 app 上确保窗口对象在整个 exec() 期间存活,
    # 避免信号槽对窗口的弱引用在极端情况下失效
    app._main_window = win
    app._single_instance = single
    single.showRequested.connect(win.show_and_raise)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

