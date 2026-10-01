from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QObject, QRunnable, QSettings, Qt, QThreadPool, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QCloseEvent, QDesktopServices, QFont, QIcon, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStatusBar,
    QStyleFactory,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "ECOMTECH Boot Manager"
APP_VERSION = "1.1.1"
ORG_NAME = "ECOMTECH"
IS_WINDOWS = sys.platform == "win32"
IMMEDIATE_POWER_DELAY = "0"
ONE_TIME_SAFE_DESCRIPTION = "ECOMTECH One-Time Safe Mode"


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def app_data_dir() -> Path:
    if IS_WINDOWS:
        root = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        root = Path.home() / ".local" / "share"
    path = root / "ECOMTECH" / "WindowsBootManager"
    path.mkdir(parents=True, exist_ok=True)
    return path


LOG_FILE = app_data_dir() / "boot_manager.log"


def is_admin() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        if getattr(sys, "frozen", False):
            executable = sys.executable
            params = " ".join(f'"{a}"' for a in sys.argv[1:])
        else:
            executable = sys.executable
            script = str(Path(__file__).resolve())
            params = " ".join([f'"{script}"', *[f'"{a}"' for a in sys.argv[1:]]])
        result = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, params, None, 1)
        return int(result) > 32
    except Exception:
        return False


def firmware_type() -> str:
    if not IS_WINDOWS:
        return "Unavailable"
    try:
        value = ctypes.c_uint(0)
        if ctypes.windll.kernel32.GetFirmwareType(ctypes.byref(value)):
            return {1: "Legacy BIOS", 2: "UEFI"}.get(value.value, "Unknown")
    except Exception:
        pass
    return "Unknown"


def read_reg_value(root: int, path: str, name: str) -> tuple[bool, object | None]:
    """Return whether a registry value exists and its value."""
    if not IS_WINDOWS:
        return False, None
    try:
        import winreg

        with winreg.OpenKey(root, path) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return True, value
    except FileNotFoundError:
        return False, None
    except Exception:
        return False, None


def read_reg_dword(root: int, path: str, name: str) -> int | None:
    exists, value = read_reg_value(root, path, name)
    if not exists:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def run_capture(command: list[str], *, timeout: int = 15) -> tuple[int, str]:
    kwargs: dict = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
    }
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(command, **kwargs)
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode, output.strip()
    except Exception as exc:
        return 1, str(exc)


def current_boot_state() -> tuple[str, str]:
    """Return the Windows session currently running, not the next configured boot."""
    if not IS_WINDOWS:
        return "Unavailable", "unknown"
    try:
        # SM_CLEANBOOT: 0=normal, 1=safe mode, 2=safe mode with networking.
        clean_boot = int(ctypes.windll.user32.GetSystemMetrics(67))
        if clean_boot == 1:
            return "Safe Mode (Minimal)", "warning"
        if clean_boot == 2:
            return "Safe Mode (Networking)", "warning"
        if clean_boot == 0:
            return "Normal Mode", "ok"
    except Exception:
        pass
    return "Unknown", "unknown"


def _safe_mode_from_bcd(output: str) -> str:
    lowered = output.lower()
    if "safebootalternateshell" in lowered and "yes" in lowered:
        return "Safe Mode (Command Prompt)"
    for line in lowered.splitlines():
        if "safeboot" in line:
            if "network" in line:
                return "Safe Mode (Networking)"
            if "minimal" in line:
                return "Safe Mode (Minimal)"
    return "Normal Mode"


def safe_boot_status() -> str:
    """Return the effective mode for the next restart."""
    if not IS_WINDOWS:
        return "Unavailable"

    # A bootsequence entry is consumed by Windows Boot Manager after one boot.
    code, manager_output = run_capture(["bcdedit", "/enum", "{bootmgr}"])
    if code == 0:
        for line in manager_output.splitlines():
            if "bootsequence" not in line.lower():
                continue
            match = re.search(r"\{[0-9a-fA-F-]{36}\}", line)
            if match:
                entry_code, entry_output = run_capture(["bcdedit", "/enum", match.group(0)])
                if entry_code == 0:
                    mode = _safe_mode_from_bcd(entry_output)
                    if mode.startswith("Safe Mode"):
                        return f"One-Time {mode}"

    code, current_output = run_capture(["bcdedit", "/enum", "{current}"])
    if code != 0:
        return "Unknown"
    if ONE_TIME_SAFE_DESCRIPTION.lower() in current_output.lower():
        return "Normal Mode (next restart)"
    return _safe_mode_from_bcd(current_output)


def one_time_safe_boot_command(mode: str, *, alternate_shell: bool = False) -> list[str]:
    """Build a PowerShell command that schedules Safe Mode for one boot only."""
    if mode not in {"minimal", "network"}:
        raise ValueError(f"Unsupported Safe Mode type: {mode}")

    alternate_shell_step = (
        "$setOutput = & bcdedit.exe /set $entry safebootalternateshell yes 2>&1; "
        "if ($LASTEXITCODE -ne 0) { throw (($setOutput | Out-String).Trim()) }; "
        if alternate_shell
        else "& bcdedit.exe /deletevalue $entry safebootalternateshell 2>$null | Out-Null; "
    )

    script = (
        "$ErrorActionPreference='Stop'; "
        f"$description='{ONE_TIME_SAFE_DESCRIPTION}'; "
        # Repair persistent Safe Boot flags left by older versions before cloning.
        "& bcdedit.exe /deletevalue '{current}' safeboot 2>$null | Out-Null; "
        "& bcdedit.exe /deletevalue '{current}' safebootalternateshell 2>$null | Out-Null; "
        "$copyOutput = & bcdedit.exe /copy '{current}' /d $description 2>&1; "
        "if ($LASTEXITCODE -ne 0) { throw (($copyOutput | Out-String).Trim()) }; "
        "$match = [regex]::Match(($copyOutput | Out-String), '\\{[0-9a-fA-F-]{36}\\}'); "
        "if (-not $match.Success) { throw 'Windows created the boot entry but did not return its identifier.' }; "
        "$entry = $match.Value; "
        "try { "
        # A copied loader can be appended to the normal boot menu; keep it hidden.
        "& bcdedit.exe /displayorder $entry /remove 2>$null | Out-Null; "
        f"$setOutput = & bcdedit.exe /set $entry safeboot {mode} 2>&1; "
        "if ($LASTEXITCODE -ne 0) { throw (($setOutput | Out-String).Trim()) }; "
        + alternate_shell_step
        + "$sequenceOutput = & bcdedit.exe /bootsequence $entry 2>&1; "
        "if ($LASTEXITCODE -ne 0) { throw (($sequenceOutput | Out-String).Trim()) }; "
        # Remove stale temporary entries from previous runs, but keep the new one.
        "$enumOutput = & bcdedit.exe /enum all /v 2>$null; "
        "$blocks = (($enumOutput | Out-String) -split '(?:\\r?\\n){2,}'); "
        "foreach ($block in $blocks) { "
        "if ($block.Contains($description)) { "
        "$oldMatch = [regex]::Match($block, '\\{[0-9a-fA-F-]{36}\\}'); "
        "if ($oldMatch.Success -and $oldMatch.Value -ne $entry) { "
        "& bcdedit.exe /delete $oldMatch.Value /cleanup 2>$null | Out-Null "
        "} } }; "
        "Write-Output ('One-time Safe Mode entry prepared: ' + $entry); "
        "} catch { "
        "& bcdedit.exe /delete $entry /cleanup 2>$null | Out-Null; "
        "throw "
        "}"
    )
    return [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
    ]


def get_restore_points(limit: int = 10) -> tuple[list[dict[str, object]], str]:
    """Return recent Windows restore points and an optional readable error."""
    if not IS_WINDOWS:
        return [], "Unavailable on this operating system."

    script = (
        "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); "
        "$ErrorActionPreference='Stop'; "
        "$points=Get-ComputerRestorePoint | Sort-Object SequenceNumber -Descending | "
        f"Select-Object -First {max(1, min(limit, 50))}; "
        "$items=@($points | ForEach-Object { "
        "$created=$_.CreationTime; "
        "try {$created=[System.Management.ManagementDateTimeConverter]::ToDateTime($_.CreationTime).ToString('yyyy-MM-dd HH:mm:ss')} catch {}; "
        "[PSCustomObject]@{SequenceNumber=[int]$_.SequenceNumber; Description=[string]$_.Description; "
        "Created=[string]$created; RestorePointType=[int]$_.RestorePointType} }); "
        "$items | ConvertTo-Json -Compress"
    )
    code, output = run_capture([
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
    ], timeout=30)
    if code != 0:
        detail = output.strip() or "Windows could not read restore points."
        return [], detail.splitlines()[-1]
    if not output.strip():
        return [], ""
    try:
        data = json.loads(output)
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return [], "Unexpected restore-point data."
        return [item for item in data if isinstance(item, dict)], ""
    except json.JSONDecodeError:
        return [], "Windows returned unreadable restore-point data."


def restore_point_type_name(value: object) -> str:
    names = {
        0: "Application install",
        1: "Application uninstall",
        10: "Device driver install",
        12: "Modify settings",
        13: "Cancelled operation",
    }
    try:
        return names.get(int(value), f"Type {int(value)}")
    except (TypeError, ValueError):
        return "Unknown type"


def append_log(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")


@dataclass(slots=True)
class CommandStep:
    command: list[str]
    ignore_error: bool = False


@dataclass(slots=True)
class ActionSpec:
    name: str
    description: str
    steps: list[CommandStep]
    requires_admin: bool = True
    destructive: bool = False
    success_message: str = "Operation completed successfully."
    show_success_dialog: bool = True


class WorkerSignals(QObject):
    finished = pyqtSignal(bool, str, str)
    output = pyqtSignal(str)


class StatusSignals(QObject):
    finished = pyqtSignal(dict)


class StatusWorker(QRunnable):
    def __init__(self):
        super().__init__()
        self.signals = StatusSignals()

    def run(self) -> None:
        result: dict[str, object] = {
            "firmware": "Unknown",
            "current_state": "Unknown",
            "current_level": "unknown",
            "next_boot": "Unknown",
            "restore_points": [],
            "restore_error": "",
            "hibernate": "Unavailable",
            "tweaks": {
                "hibernate": "Unavailable",
                "fast_startup": "Unavailable",
                "bsod_restart": "Unavailable",
                "clear_pagefile": "Unavailable",
                "auto_end_tasks": "Unavailable",
                "startup_delay": "Unavailable",
            },
        }
        try:
            current_state, current_level = current_boot_state()
            points, restore_error = get_restore_points()
            result.update({
                "firmware": firmware_type(),
                "current_state": current_state,
                "current_level": current_level,
                "next_boot": safe_boot_status(),
                "restore_points": points,
                "restore_error": restore_error,
            })
            if IS_WINDOWS:
                try:
                    import winreg

                    hibernate = read_reg_dword(
                        winreg.HKEY_LOCAL_MACHINE,
                        r"SYSTEM\CurrentControlSet\Control\Power",
                        "HibernateEnabled",
                    )
                    hibernate_text = (
                        "Enabled" if hibernate == 1 else "Disabled" if hibernate == 0 else "Unknown"
                    )
                    result["hibernate"] = hibernate_text

                    fast_startup = read_reg_dword(
                        winreg.HKEY_LOCAL_MACHINE,
                        r"SYSTEM\CurrentControlSet\Control\Session Manager\Power",
                        "HiberbootEnabled",
                    )
                    bsod_restart = read_reg_dword(
                        winreg.HKEY_LOCAL_MACHINE,
                        r"SYSTEM\CurrentControlSet\Control\CrashControl",
                        "AutoReboot",
                    )
                    clear_pagefile = read_reg_dword(
                        winreg.HKEY_LOCAL_MACHINE,
                        r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management",
                        "ClearPageFileAtShutdown",
                    )
                    auto_end_exists, auto_end_value = read_reg_value(
                        winreg.HKEY_CURRENT_USER,
                        r"Control Panel\Desktop",
                        "AutoEndTasks",
                    )
                    delay_exists, delay_value = read_reg_value(
                        winreg.HKEY_LOCAL_MACHINE,
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Serialize",
                        "StartupDelayInMSec",
                    )

                    if hibernate == 0:
                        fast_startup_text = "Unavailable (Hibernate off)"
                    elif fast_startup == 1:
                        fast_startup_text = "Enabled"
                    elif fast_startup == 0:
                        fast_startup_text = "Disabled"
                    else:
                        fast_startup_text = "Windows default"

                    if not auto_end_exists:
                        auto_end_text = "Windows default"
                    elif str(auto_end_value).strip() == "1":
                        auto_end_text = "Enabled"
                    elif str(auto_end_value).strip() == "0":
                        auto_end_text = "Disabled"
                    else:
                        auto_end_text = f"Custom ({auto_end_value})"

                    if not delay_exists:
                        delay_text = "Windows default"
                    else:
                        try:
                            delay_number = int(delay_value)
                            delay_text = "Delay removed" if delay_number == 0 else f"Custom ({delay_number} ms)"
                        except (TypeError, ValueError):
                            delay_text = "Custom value"

                    result["tweaks"] = {
                        "hibernate": hibernate_text,
                        "fast_startup": fast_startup_text,
                        "bsod_restart": "Enabled" if bsod_restart == 1 else "Disabled" if bsod_restart == 0 else "Windows default",
                        "clear_pagefile": "Enabled" if clear_pagefile == 1 else "Disabled" if clear_pagefile == 0 else "Windows default",
                        "auto_end_tasks": auto_end_text,
                        "startup_delay": delay_text,
                    }
                except Exception:
                    result["hibernate"] = "Unknown"
                    result["tweaks"] = {key: "Unknown" for key in result["tweaks"]}
        except Exception as exc:
            result["restore_error"] = f"Status refresh failed: {exc}"
            append_log(f"STATUS ERROR: {traceback.format_exc()}")
        self.signals.finished.emit(result)


class CommandWorker(QRunnable):
    def __init__(self, spec: ActionSpec):
        super().__init__()
        self.spec = spec
        self.signals = WorkerSignals()

    def run(self) -> None:
        all_output: list[str] = []
        try:
            for step in self.spec.steps:
                display = subprocess.list2cmdline(step.command)
                self.signals.output.emit(f"> {display}")
                append_log(f"RUN {display}")
                kwargs: dict = {
                    "capture_output": True,
                    "text": True,
                    "encoding": "utf-8",
                    "errors": "replace",
                    "timeout": 90,
                }
                if IS_WINDOWS:
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                result = subprocess.run(step.command, **kwargs)
                text = "\n".join(x for x in [(result.stdout or "").strip(), (result.stderr or "").strip()] if x)
                if text:
                    all_output.append(text)
                    self.signals.output.emit(text)
                if result.returncode != 0 and not step.ignore_error:
                    detail = text or f"Command exited with code {result.returncode}."
                    append_log(f"FAILED {self.spec.name}: {detail}")
                    self.signals.finished.emit(False, self.spec.name, detail)
                    return
            append_log(f"SUCCESS {self.spec.name}")
            self.signals.finished.emit(True, self.spec.name, self.spec.success_message)
        except subprocess.TimeoutExpired:
            detail = "The command timed out."
            append_log(f"TIMEOUT {self.spec.name}")
            self.signals.finished.emit(False, self.spec.name, detail)
        except Exception:
            detail = traceback.format_exc()
            append_log(f"ERROR {self.spec.name}: {detail}")
            self.signals.finished.emit(False, self.spec.name, detail)


class StatusCard(QFrame):
    def __init__(self, title: str, value: str = "Checking…"):
        super().__init__()
        self.setObjectName("statusCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(2)
        title_label = QLabel(title.upper())
        title_label.setObjectName("mutedLabel")
        title_label.setWordWrap(True)
        self.value_label = QLabel(value)
        self.value_label.setObjectName("statusValue")
        self.value_label.setWordWrap(True)
        self.value_label.setProperty("state", "neutral")
        layout.addWidget(title_label)
        layout.addWidget(self.value_label)

    def set_value(self, value: str, state: str = "neutral", *, indicator: bool = False) -> None:
        self.value_label.setText(f"● {value}" if indicator else value)
        self.value_label.setToolTip(value)
        self.value_label.setProperty("state", state)
        self.value_label.style().unpolish(self.value_label)
        self.value_label.style().polish(self.value_label)


class AboutDialog(QDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        self.setWindowIcon(QIcon(str(resource_path("assets/power.svg"))))
        self.setFixedSize(430, 410)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 10, 18, 12)
        layout.setSpacing(4)

        icon = QLabel("⏻")
        icon.setObjectName("aboutPower")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon)

        title = QLabel(APP_NAME)
        title.setObjectName("aboutTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        version = QLabel(f"Version {APP_VERSION}")
        version.setObjectName("mutedLabel")
        version.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(version)

        content = QLabel()
        content.setObjectName("aboutContent")
        content.setTextFormat(Qt.TextFormat.RichText)
        content.setOpenExternalLinks(True)
        content.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        content.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        content.setWordWrap(True)
        content.setText(
            """
            <div align='center' style='font-size:8.5pt; line-height:1.14;'>
              Created by:<br>
              <b>rahfie27</b><br>
              <b>E-COMPUTER</b><br>
              SERVICE KOMPUTER PANGGILAN BOGOR<br>
              Copyright © ECOMTECH 2026 - All Right Reserved<br><br>

              <a style='color:#60a5fa;' href='mailto:e-comtech@mail.com'><b>Contact:</b></a><br>
              <a style='color:#60a5fa;' href='mailto:e-comtech@mail.com'>e-comtech@mail.com</a> /
              <a style='color:#60a5fa;' href='mailto:rahfie27@gmail.com'>rahfie27@gmail.com</a><br><br>

              <a style='color:#60a5fa;' href='https://paypal.me/rahfie'><b>Donation:</b><br>
              paypal.me/rahfie</a><br><br>

              <b style='color:#ef4444;'>WARNING!</b><br>
              This software is provided as-is without warranty.<br><br>

              <a style='color:#60a5fa;' href='https://nsaneforums.com'><b>THANKS TO:</b><br>
              NSANE FORUM ADMIN, STAFF, MOD, MEMBER, AND VISITOR.<br>
              https://nsaneforums.com</a>
            </div>
            """
        )
        layout.addWidget(content, 1)

        close_button = QPushButton("Close")
        close_button.setDefault(True)
        close_button.setFixedWidth(90)
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button, alignment=Qt.AlignmentFlag.AlignCenter)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings(ORG_NAME, APP_NAME)
        self.thread_pool = QThreadPool.globalInstance()
        self.active_workers: set[CommandWorker] = set()
        self.status_worker: StatusWorker | None = None
        self.status_refresh_pending = False
        self.pending_power_spec: ActionSpec | None = None
        self.pending_power_seconds = 0
        self.power_countdown_timer = QTimer(self)
        self.power_countdown_timer.setInterval(1000)
        self.power_countdown_timer.timeout.connect(self._power_countdown_tick)
        stored_theme = self.settings.value("theme", "", type=str).strip().lower()
        if stored_theme not in {"dark", "light", "classic"}:
            # Migrate the theme preference used by version 1.0.0.
            old_dark_mode = self.settings.value("dark_mode", True, type=bool)
            stored_theme = "dark" if old_dark_mode else "light"
        self.theme_name = stored_theme

        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        self.setWindowIcon(QIcon(str(resource_path("assets/power.svg"))))
        self.resize(930, 650)
        self.setMinimumSize(760, 540)

        self._build_menu()
        self._build_ui()
        self._apply_theme()
        self.refresh_status()

        QTimer.singleShot(400, self._first_run_checks)

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        refresh_action = QAction("Refresh Status", self)
        refresh_action.setShortcut("F5")
        refresh_action.triggered.connect(self.refresh_status)
        file_menu.addAction(refresh_action)

        open_logs = QAction("Open Log Folder", self)
        open_logs.triggered.connect(self.open_log_folder)
        file_menu.addAction(open_logs)
        file_menu.addSeparator()

        exit_action = QAction("Exit", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        tools_menu = self.menuBar().addMenu("&Tools")
        elevate_action = QAction("Restart as Administrator", self)
        elevate_action.triggered.connect(self.restart_elevated)
        tools_menu.addAction(elevate_action)

        theme_menu = tools_menu.addMenu("Theme")
        self.theme_group = QActionGroup(self)
        self.theme_group.setExclusive(True)
        self.theme_actions: dict[str, QAction] = {}
        for theme_name, label in (
            ("dark", "Dark"),
            ("light", "Light"),
            ("classic", "Classic"),
        ):
            action = QAction(label, self)
            action.setCheckable(True)
            action.triggered.connect(
                lambda checked, selected=theme_name: self.set_theme(selected) if checked else None
            )
            self.theme_group.addAction(action)
            theme_menu.addAction(action)
            self.theme_actions[theme_name] = action

        help_menu = self.menuBar().addMenu("&Help")
        about_action = QAction("About", self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(9)

        header = QHBoxLayout()
        power = QLabel("⏻")
        power.setObjectName("headerPower")
        header.addWidget(power)

        titles = QVBoxLayout()
        titles.setSpacing(0)
        title = QLabel("ECOMTECH Boot Manager")
        title.setObjectName("mainTitle")
        subtitle = QLabel("Windows boot, recovery, power, and startup controls")
        subtitle.setObjectName("mutedLabel")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        header.addLayout(titles, 1)

        self.admin_badge = QLabel()
        self.admin_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.admin_badge.setMinimumWidth(128)
        header.addWidget(self.admin_badge)

        refresh = QToolButton()
        refresh.setText("↻")
        refresh.setToolTip("Refresh system status (F5)")
        refresh.setObjectName("refreshButton")
        refresh.clicked.connect(self.refresh_status)
        header.addWidget(refresh)
        root.addLayout(header)

        status_grid = QGridLayout()
        status_grid.setHorizontalSpacing(7)
        status_grid.setVerticalSpacing(7)
        self.status_current = StatusCard("CURRENT STATE")
        self.status_safeboot = StatusCard("NEXT BOOT MODE")
        self.status_admin = StatusCard("PRIVILEGE")
        self.status_firmware = StatusCard("FIRMWARE")
        self.status_hibernate = StatusCard("HIBERNATE")
        self.status_restore = StatusCard("LATEST RESTORE POINT")
        status_grid.addWidget(self.status_current, 0, 0)
        status_grid.addWidget(self.status_safeboot, 0, 1)
        status_grid.addWidget(self.status_admin, 0, 2)
        status_grid.addWidget(self.status_firmware, 1, 0)
        status_grid.addWidget(self.status_hibernate, 1, 1)
        status_grid.addWidget(self.status_restore, 1, 2)
        for column in range(3):
            status_grid.setColumnStretch(column, 1)
        root.addLayout(status_grid)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_boot_tab(), "BOOT && RECOVERY")
        self.tabs.addTab(self._build_power_tab(), "POWER")
        self.tabs.addTab(self._build_tweaks_tab(), "STARTUP TWEAKS")
        self.tabs.addTab(self._build_tools_tab(), "TOOLS")
        self.tabs.addTab(self._build_log_tab(), "LOGS")
        root.addWidget(self.tabs, 1)

        self.setCentralWidget(central)
        status_bar = QStatusBar()
        self.setStatusBar(status_bar)
        self.statusBar().showMessage("Ready")

    def _scroll_tab(self, content: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setWidget(content)
        return area

    def _action_button(
        self,
        title: str,
        description: str,
        callback: Callable[[], None],
        *,
        danger: bool = False,
        power_icon: bool = False,
    ) -> QWidget:
        card = QFrame()
        card.setObjectName("actionCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)
        button = QPushButton(("⏻  " if power_icon else "") + title)
        button.setMinimumHeight(34)
        if danger:
            button.setProperty("danger", True)
        button.clicked.connect(callback)
        label = QLabel(description)
        label.setObjectName("mutedLabel")
        label.setWordWrap(True)
        layout.addWidget(button)
        layout.addWidget(label)
        return card

    def _build_boot_tab(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(6, 9, 6, 6)
        layout.setSpacing(8)

        notice = QLabel(
            "Safe Mode uses a temporary one-time boot entry. The following restart automatically "
            "returns to the normal Windows boot entry; no manual Safe Boot cleanup is required. "
            "The timer and Force options on the POWER tab also apply to every restart action here."
        )
        notice.setObjectName("notice")
        notice.setWordWrap(True)
        layout.addWidget(notice)

        grid = QGridLayout()
        grid.setSpacing(8)
        grid.addWidget(self._action_button(
            "Restart to Normal Mode (Repair)",
            "Clear legacy Safe Boot flags and restart in Normal Mode after the selected timer.",
            self.boot_normal,
            danger=True,
            power_icon=True,
        ), 0, 0)
        grid.addWidget(self._action_button(
            "One-Time Safe Mode",
            "Use minimal Safe Mode for one boot only, then return to Normal Mode.",
            self.boot_safe_minimal,
            danger=True,
        ), 0, 1)
        grid.addWidget(self._action_button(
            "One-Time Safe Mode with Networking",
            "Use Safe Mode with networking for one boot only.",
            self.boot_safe_network,
            danger=True,
        ), 0, 2)
        grid.addWidget(self._action_button(
            "One-Time Safe Mode Command Prompt",
            "Use Command Prompt Safe Mode for one boot only.",
            self.boot_safe_cmd,
            danger=True,
        ), 1, 0)
        grid.addWidget(self._action_button(
            "Advanced Startup",
            "Restart into Windows Recovery Environment and troubleshooting options.",
            self.boot_advanced,
            danger=True,
        ), 1, 1)
        grid.addWidget(self._action_button(
            "UEFI Firmware Settings",
            "Restart directly to firmware setup when supported by the PC.",
            self.boot_firmware,
            danger=True,
        ), 1, 2)
        layout.addLayout(grid)

        policy_group = QGroupBox("Boot menu policy")
        policy_layout = QHBoxLayout(policy_group)
        policy_layout.addWidget(QLabel("Boot menu timeout (seconds):"))
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setObjectName("iconSpinBox")
        self.timeout_spin.setRange(0, 999)
        self.timeout_spin.setValue(10)
        policy_layout.addWidget(self.timeout_spin)
        set_timeout = QPushButton("Apply Timeout")
        set_timeout.clicked.connect(self.set_boot_timeout)
        policy_layout.addWidget(set_timeout)
        legacy = QPushButton("Use Legacy F8 Menu")
        legacy.clicked.connect(lambda: self.set_boot_policy("legacy"))
        policy_layout.addWidget(legacy)
        standard = QPushButton("Use Standard Menu")
        standard.clicked.connect(lambda: self.set_boot_policy("standard"))
        policy_layout.addWidget(standard)
        policy_layout.addStretch(1)
        layout.addWidget(policy_group)
        layout.addStretch(1)
        return self._scroll_tab(content)

    def _build_power_tab(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(6, 9, 6, 6)
        layout.setSpacing(8)

        options_group = QGroupBox("POWER ACTION OPTIONS")
        options_layout = QGridLayout(options_group)
        options_layout.setHorizontalSpacing(12)
        options_layout.setVerticalSpacing(7)

        timer_icon = QLabel("⏱")
        timer_icon.setObjectName("powerOptionIcon")
        timer_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        options_layout.addWidget(timer_icon, 0, 0, 2, 1)

        timer_label = QLabel("ACTION TIMER")
        timer_label.setObjectName("powerTimerCaption")
        options_layout.addWidget(timer_label, 0, 1)

        self.power_timer_spin = QSpinBox()
        self.power_timer_spin.setObjectName("iconSpinBox")
        self.power_timer_spin.setRange(0, 86400)
        self.power_timer_spin.setSingleStep(5)
        self.power_timer_spin.setSuffix(" seconds")
        self.power_timer_spin.setSpecialValueText("Immediate (0 seconds)")
        self.power_timer_spin.setValue(self.settings.value("power_timer_seconds", 0, type=int))
        self.power_timer_spin.setToolTip(
            "Delay every POWER action and every restart action on the BOOT & RECOVERY tab."
        )
        self.power_timer_spin.valueChanged.connect(
            lambda value: self.settings.setValue("power_timer_seconds", value)
        )
        options_layout.addWidget(self.power_timer_spin, 1, 1)

        self.confirm_checkbox = QCheckBox(
            "⚠  REQUIRE CONFIRMATION FOR RESTART / SHUT DOWN ACTIONS"
        )
        self.confirm_checkbox.setObjectName("criticalPowerCheck")
        self.confirm_checkbox.setChecked(self.settings.value("confirm_power", True, type=bool))
        self.confirm_checkbox.setToolTip(
            "Show a final confirmation before a restart, shutdown, sign-out, sleep, or hibernate action."
        )
        self.confirm_checkbox.toggled.connect(
            lambda value: self.settings.setValue("confirm_power", value)
        )
        options_layout.addWidget(self.confirm_checkbox, 0, 2, 2, 1)

        self.force_checkbox = QCheckBox("⚡  USE FORCE PARAMETER (/f)")
        self.force_checkbox.setObjectName("forcePowerCheck")
        self.force_checkbox.setChecked(self.settings.value("force_power", True, type=bool))
        self.force_checkbox.setToolTip(
            "Force running applications to close without warning where Windows supports it. "
            "This option does not apply to Lock."
        )
        self.force_checkbox.toggled.connect(
            lambda value: self.settings.setValue("force_power", value)
        )
        options_layout.addWidget(self.force_checkbox, 2, 2, 1, 1)

        self.power_countdown_label = QLabel("● No power action is scheduled.")
        self.power_countdown_label.setObjectName("powerCountdown")
        self.power_countdown_label.setWordWrap(True)
        options_layout.addWidget(self.power_countdown_label, 2, 0, 1, 2)
        options_layout.setColumnStretch(2, 1)
        layout.addWidget(options_group)

        grid = QGridLayout()
        grid.setSpacing(8)
        items = [
            ("Restart", "Restart Windows after the selected timer.", self.power_restart, True),
            ("Shut Down", "Power off the computer after the selected timer.", self.power_shutdown, True),
            ("Sign Out", "Sign out the current Windows user after the selected timer.", self.power_signout, True),
            ("Lock", "Lock the current Windows session after the selected timer.", self.power_lock, False),
            ("Hibernate", "Save the session to disk after the selected timer.", self.power_hibernate, False),
            ("Sleep", "Enter low-power sleep after the selected timer.", self.power_sleep, False),
        ]
        for index, (title, desc, callback, danger) in enumerate(items):
            grid.addWidget(self._action_button(title, desc, callback, danger=danger, power_icon=True), index // 3, index % 3)
        layout.addLayout(grid)
        self.cancel_power_button = QPushButton("✖  CANCEL PENDING POWER ACTION / SHUTDOWN / RESTART")
        self.cancel_power_button.setObjectName("cancelPowerButton")
        self.cancel_power_button.clicked.connect(self.cancel_shutdown)
        layout.addWidget(self.cancel_power_button)
        layout.addStretch(1)
        return self._scroll_tab(content)

    def _build_tweaks_tab(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(6, 9, 6, 6)
        layout.setSpacing(7)

        warning = QLabel("These settings change Windows power or registry configuration. A restart may be required.")
        warning.setObjectName("notice")
        warning.setWordWrap(True)
        layout.addWidget(warning)

        self.tweak_state_labels: dict[str, QLabel] = {}
        rows = [
            ("hibernate", "Hibernate", "Enable or disable hibernation support.", self.enable_hibernate, self.disable_hibernate),
            ("fast_startup", "Fast Startup", "Enable or disable Windows Fast Startup.", self.enable_fast_startup, self.disable_fast_startup),
            ("bsod_restart", "BSOD Automatic Restart", "Automatically restart after a system crash.", self.enable_bsod_restart, self.disable_bsod_restart),
            ("clear_pagefile", "Clear Page File at Shutdown", "Clear virtual memory page file during shutdown (slower shutdown).", self.enable_clear_pagefile, self.disable_clear_pagefile),
            ("auto_end_tasks", "AutoEndTasks", "Automatically close unresponsive apps during sign-out or shutdown.", self.enable_auto_end_tasks, self.disable_auto_end_tasks),
            ("startup_delay", "Startup App Delay", "Remove the default startup app delay or restore Windows default behavior.", self.remove_startup_delay, self.restore_startup_delay),
        ]
        for key, title, desc, enable_cb, disable_cb in rows:
            group = QGroupBox(title)
            row = QHBoxLayout(group)
            text = QLabel(desc)
            text.setWordWrap(True)
            row.addWidget(text, 1)

            state_label = QLabel("● Checking…")
            state_label.setObjectName("tweakState")
            state_label.setProperty("state", "unknown")
            state_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            state_label.setMinimumWidth(155)
            state_label.setToolTip(f"Current {title} state")
            row.addWidget(state_label)
            self.tweak_state_labels[key] = state_label

            enable = QPushButton("Enable" if title != "Startup App Delay" else "Remove Delay")
            enable.clicked.connect(enable_cb)
            disable = QPushButton("Disable" if title != "Startup App Delay" else "Restore Default")
            disable.clicked.connect(disable_cb)
            row.addWidget(enable)
            row.addWidget(disable)
            layout.addWidget(group)
        layout.addStretch(1)
        return self._scroll_tab(content)

    def _build_tools_tab(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(6, 9, 6, 6)
        layout.setSpacing(8)

        hint = QLabel("Maintenance, recovery, and Windows administration shortcuts.")
        hint.setObjectName("notice")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        grid = QGridLayout()
        grid.setSpacing(8)
        tools = [
            ("Backup BCD", self.backup_bcd),
            ("Restore BCD", self.restore_bcd),
            ("Create Restore Point", self.create_restore_point),
            ("System Configuration", lambda: self.launch_tool("msconfig.exe")),
            ("System Information", lambda: self.launch_tool("msinfo32.exe")),
            ("Event Viewer", lambda: self.launch_tool("eventvwr.msc")),
            ("System Restore", lambda: self.launch_tool("rstrui.exe")),
            ("System Protection", lambda: self.launch_tool("SystemPropertiesProtection.exe")),
            ("Recovery Options", lambda: self.launch_uri("ms-settings:recovery")),
            ("Startup Apps", lambda: self.launch_uri("ms-settings:startupapps")),
            ("Device Manager", lambda: self.launch_tool("devmgmt.msc")),
            ("Disk Management", lambda: self.launch_tool("diskmgmt.msc")),
        ]
        for index, (label, callback) in enumerate(tools):
            button = QPushButton(label)
            button.setMinimumHeight(36)
            button.clicked.connect(callback)
            grid.addWidget(button, index // 3, index % 3)
        for column in range(3):
            grid.setColumnStretch(column, 1)
        layout.addLayout(grid)
        layout.addStretch(1)
        return content

    def _build_log_tab(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(6, 9, 6, 6)
        layout.setSpacing(8)

        restore_group = QGroupBox("Recent System Restore Points")
        restore_layout = QVBoxLayout(restore_group)
        restore_layout.setContentsMargins(10, 10, 10, 8)
        restore_layout.setSpacing(6)
        restore_header = QHBoxLayout()
        restore_hint = QLabel("Up to 10 restore points reported by Windows System Protection.")
        restore_hint.setObjectName("mutedLabel")
        restore_hint.setWordWrap(True)
        restore_header.addWidget(restore_hint, 1)
        refresh_restore = QPushButton("Refresh")
        refresh_restore.clicked.connect(self.refresh_status)
        restore_header.addWidget(refresh_restore)
        restore_layout.addLayout(restore_header)
        self.restore_points_view = QPlainTextEdit()
        self.restore_points_view.setReadOnly(True)
        self.restore_points_view.setMaximumHeight(150)
        self.restore_points_view.setPlaceholderText("Checking Windows restore points…")
        restore_layout.addWidget(self.restore_points_view)
        layout.addWidget(restore_group)

        log_group = QGroupBox("Application and Command Log")
        log_layout = QVBoxLayout(log_group)
        log_layout.setContentsMargins(10, 10, 10, 8)
        log_layout.setSpacing(6)
        log_actions = QHBoxLayout()
        log_hint = QLabel("Recent operations, command output, warnings, and errors.")
        log_hint.setObjectName("mutedLabel")
        log_actions.addWidget(log_hint, 1)
        reload_log_button = QPushButton("Reload")
        reload_log_button.clicked.connect(self.load_log)
        log_actions.addWidget(reload_log_button)
        open_log_button = QPushButton("Open Folder")
        open_log_button.clicked.connect(self.open_log_folder)
        log_actions.addWidget(open_log_button)
        clear_log_button = QPushButton("Clear")
        clear_log_button.clicked.connect(self.clear_log)
        log_actions.addWidget(clear_log_button)
        log_layout.addLayout(log_actions)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("Command output and operation history will appear here.")
        log_layout.addWidget(self.log_view, 1)
        layout.addWidget(log_group, 1)
        self.load_log()
        return content

    def _apply_theme(self) -> None:
        app = QApplication.instance()
        if app is None:
            return

        if self.theme_name == "light":
            qt_style = "Fusion"
            style_sheet = LIGHT_STYLE
        elif self.theme_name == "classic":
            available_styles = {name.lower(): name for name in QStyleFactory.keys()}
            qt_style = available_styles.get("windows", "Fusion")
            style_sheet = CLASSIC_STYLE
        else:
            self.theme_name = "dark"
            qt_style = "Fusion"
            style_sheet = DARK_STYLE

        check_icon_path = resource_path("assets/check.svg").as_posix()
        spin_plus_icon_path = resource_path("assets/spin-plus.svg").as_posix()
        spin_minus_icon_path = resource_path("assets/spin-minus.svg").as_posix()
        style_sheet = style_sheet.replace("__CHECK_ICON__", check_icon_path)
        style_sheet = style_sheet.replace("__SPIN_PLUS_ICON__", spin_plus_icon_path)
        style_sheet = style_sheet.replace("__SPIN_MINUS_ICON__", spin_minus_icon_path)
        app.setStyle(qt_style)
        app.setStyleSheet(style_sheet)
        selected_action = self.theme_actions.get(self.theme_name)
        if selected_action is not None:
            selected_action.setChecked(True)
        self.statusBar().showMessage(f"{self.theme_name.title()} theme enabled", 2500)

    def set_theme(self, theme_name: str) -> None:
        if theme_name not in {"dark", "light", "classic"}:
            return
        self.theme_name = theme_name
        self.settings.setValue("theme", theme_name)
        self._apply_theme()

    def _first_run_checks(self) -> None:
        if not IS_WINDOWS:
            QMessageBox.information(
                self,
                "Windows Required",
                "This application is designed for Windows 10 and Windows 11. The interface can open here, but system actions are disabled.",
            )
        elif not is_admin():
            answer = QMessageBox.question(
                self,
                "Administrator Access Recommended",
                "Most boot and system actions require administrator privileges. Restart this application as Administrator now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.restart_elevated()

    def _set_tweak_state(self, key: str, value: str) -> None:
        label = self.tweak_state_labels.get(key)
        if label is None:
            return
        lowered = value.lower()
        if value in {"Enabled", "Delay removed"}:
            state = "ok"
        elif value == "Disabled" or lowered.startswith("unavailable"):
            state = "warning"
        elif value in {"Windows default", "Checking…"}:
            state = "neutral"
        else:
            state = "unknown"
        label.setText(f"● {value}")
        label.setToolTip(value)
        label.setProperty("state", state)
        label.style().unpolish(label)
        label.style().polish(label)

    def refresh_status(self) -> None:
        admin = is_admin()
        self.status_admin.set_value(
            "Administrator" if admin else "Standard user",
            "ok" if admin else "warning",
            indicator=True,
        )
        self.admin_badge.setText("● ADMINISTRATOR" if admin else "● NOT ELEVATED")
        self.admin_badge.setProperty("admin", admin)
        self.admin_badge.style().unpolish(self.admin_badge)
        self.admin_badge.style().polish(self.admin_badge)

        if self.status_worker is not None:
            self.status_refresh_pending = True
            self.statusBar().showMessage("Status refresh queued", 2500)
            return

        for card in (self.status_current, self.status_firmware, self.status_hibernate, self.status_safeboot, self.status_restore):
            card.set_value("Checking…")
        for key in self.tweak_state_labels:
            self._set_tweak_state(key, "Checking…")
        self.restore_points_view.setPlainText("Checking Windows restore points…")
        self.statusBar().showMessage("Refreshing system status…")
        worker = StatusWorker()
        self.status_worker = worker
        worker.signals.finished.connect(self._status_refresh_finished)
        self.thread_pool.start(worker)

    def _status_refresh_finished(self, result: dict) -> None:
        self.status_worker = None
        current_state = str(result.get("current_state", "Unknown"))
        current_level = str(result.get("current_level", "unknown"))
        self.status_current.set_value(current_state, current_level, indicator=True)

        next_boot = str(result.get("next_boot", "Unknown"))
        next_state = (
            "ok" if next_boot.startswith("Normal Mode")
            else "warning" if "Safe Mode" in next_boot
            else "unknown"
        )
        self.status_safeboot.set_value(next_boot, next_state, indicator=True)
        self.status_firmware.set_value(str(result.get("firmware", "Unknown")))

        hibernate = str(result.get("hibernate", "Unknown"))
        hibernate_state = "ok" if hibernate == "Enabled" else "warning" if hibernate == "Disabled" else "unknown"
        self.status_hibernate.set_value(hibernate, hibernate_state, indicator=True)

        tweaks_value = result.get("tweaks", {})
        tweaks = tweaks_value if isinstance(tweaks_value, dict) else {}
        for key in self.tweak_state_labels:
            self._set_tweak_state(key, str(tweaks.get(key, "Unknown")))

        points_value = result.get("restore_points", [])
        points = points_value if isinstance(points_value, list) else []
        restore_error = str(result.get("restore_error", "")).strip()
        self._display_restore_points(points, restore_error)
        self.statusBar().showMessage("Status refreshed", 3000)
        if self.status_refresh_pending:
            self.status_refresh_pending = False
            QTimer.singleShot(0, self.refresh_status)

    def _display_restore_points(self, points: list[dict[str, object]], error: str) -> None:
        if points:
            latest = points[0]
            created = str(latest.get("Created", "Unknown date"))
            description = str(latest.get("Description", "Restore point"))
            short_description = description if len(description) <= 32 else description[:29] + "…"
            self.status_restore.set_value(f"{created} — {short_description}", "ok", indicator=True)

            rows: list[str] = []
            for point in points:
                sequence = point.get("SequenceNumber", "?")
                created = str(point.get("Created", "Unknown date"))
                description = str(point.get("Description", "Restore point"))
                point_type = restore_point_type_name(point.get("RestorePointType"))
                rows.append(f"#{sequence}  {created}  |  {description}  |  {point_type}")
            self.restore_points_view.setPlainText("\n".join(rows))
            return

        if error:
            unavailable = "Unavailable" if not IS_WINDOWS else "Could not read"
            self.status_restore.set_value(unavailable, "unknown", indicator=True)
            self.restore_points_view.setPlainText(
                "Restore points could not be read. System Protection may be disabled, "
                "or Windows may require Administrator access.\n\n" + error
            )
        else:
            self.status_restore.set_value("None found", "warning", indicator=True)
            self.restore_points_view.setPlainText(
                "No restore points were reported. Use Create Restore Point or open System Protection to configure it."
            )

    def ensure_windows(self) -> bool:
        if IS_WINDOWS:
            return True
        QMessageBox.warning(self, "Windows Required", "This operation is only available on Windows.")
        return False

    def ensure_admin(self) -> bool:
        if is_admin():
            return True
        answer = QMessageBox.question(
            self,
            "Administrator Required",
            "This action requires administrator privileges. Restart the application as Administrator?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.restart_elevated()
        return False

    def confirm(self, title: str, message: str, *, power: bool = False) -> bool:
        if power and not self.confirm_checkbox.isChecked():
            return True
        result = QMessageBox.warning(
            self,
            title,
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return result == QMessageBox.StandardButton.Yes

    def _force_args(self) -> list[str]:
        return ["/f"] if self.force_checkbox.isChecked() else []

    def _shutdown_command(self, *arguments: str) -> list[str]:
        return ["shutdown", *arguments, *self._force_args()]

    def _set_power_options_enabled(self, enabled: bool) -> None:
        self.power_timer_spin.setEnabled(enabled)
        self.confirm_checkbox.setEnabled(enabled)
        self.force_checkbox.setEnabled(enabled)

    def _update_power_countdown_label(self) -> None:
        if self.pending_power_spec is None:
            self.power_countdown_label.setText("● No power action is scheduled.")
            self.power_countdown_label.setProperty("active", False)
        else:
            unit = "second" if self.pending_power_seconds == 1 else "seconds"
            self.power_countdown_label.setText(
                f"⏱ {self.pending_power_spec.name} in {self.pending_power_seconds} {unit}. "
                "Use the Cancel button to stop it."
            )
            self.power_countdown_label.setProperty("active", True)
        self.power_countdown_label.style().unpolish(self.power_countdown_label)
        self.power_countdown_label.style().polish(self.power_countdown_label)

    def _schedule_power_action(self, spec: ActionSpec) -> bool:
        delay = self.power_timer_spin.value()
        if delay <= 0:
            return False
        if self.pending_power_spec is not None:
            QMessageBox.warning(
                self,
                "Power Action Already Scheduled",
                f"{self.pending_power_spec.name} is already counting down. Cancel it before scheduling another action.",
            )
            return True

        self.pending_power_spec = spec
        self.pending_power_seconds = delay
        self._set_power_options_enabled(False)
        self._update_power_countdown_label()
        self.power_countdown_timer.start()
        self.statusBar().showMessage(f"Scheduled: {spec.name} in {delay} seconds")
        self.log_view.appendPlainText(
            f"\n[{datetime.now():%H:%M:%S}] SCHEDULED {spec.name} in {delay} seconds"
        )
        append_log(f"SCHEDULED {spec.name} in {delay} seconds")
        return True

    def _power_countdown_tick(self) -> None:
        if self.pending_power_spec is None:
            self.power_countdown_timer.stop()
            return

        self.pending_power_seconds -= 1
        if self.pending_power_seconds > 0:
            self._update_power_countdown_label()
            self.statusBar().showMessage(
                f"{self.pending_power_spec.name} in {self.pending_power_seconds} seconds"
            )
            return

        spec = self.pending_power_spec
        self.power_countdown_timer.stop()
        self.pending_power_spec = None
        self.pending_power_seconds = 0
        self._set_power_options_enabled(True)
        self._update_power_countdown_label()
        append_log(f"COUNTDOWN COMPLETE {spec.name}")
        self._start_action(spec)

    def _cancel_pending_power_action(self) -> bool:
        if self.pending_power_spec is None:
            return False
        name = self.pending_power_spec.name
        self.power_countdown_timer.stop()
        self.pending_power_spec = None
        self.pending_power_seconds = 0
        self._set_power_options_enabled(True)
        self._update_power_countdown_label()
        self.statusBar().showMessage(f"Cancelled: {name}", 5000)
        self.log_view.appendPlainText(f"[{datetime.now():%H:%M:%S}] CANCELLED {name}")
        append_log(f"CANCELLED {name}")
        return True

    def _start_action(self, spec: ActionSpec) -> None:
        self.statusBar().showMessage(f"Running: {spec.name}")
        self.log_view.appendPlainText(f"\n[{datetime.now():%H:%M:%S}] {spec.name}")
        worker = CommandWorker(spec)
        self.active_workers.add(worker)
        worker.signals.output.connect(self.log_view.appendPlainText)
        worker.signals.finished.connect(
            lambda ok, name, detail, w=worker: self._operation_finished(w, ok, name, detail)
        )
        self.thread_pool.start(worker)

    def execute(self, spec: ActionSpec, *, confirmation: str | None = None, power: bool = False) -> None:
        if not self.ensure_windows():
            return
        if spec.requires_admin and not self.ensure_admin():
            return
        if confirmation and not self.confirm(spec.name, confirmation, power=power):
            return
        if power and self._schedule_power_action(spec):
            return
        self._start_action(spec)

    def _operation_finished(self, worker: CommandWorker, success: bool, name: str, detail: str) -> None:
        self.active_workers.discard(worker)
        self.statusBar().showMessage(f"Completed: {name}" if success else f"Failed: {name}", 6000)
        if success:
            if worker.spec.show_success_dialog:
                QMessageBox.information(self, name, detail)
        else:
            QMessageBox.critical(self, f"{name} Failed", detail)
        self.refresh_status()

    def restart_elevated(self) -> None:
        if is_admin():
            QMessageBox.information(self, "Administrator", "The application is already running as Administrator.")
            return
        if relaunch_as_admin():
            QApplication.quit()
        else:
            QMessageBox.critical(self, "Elevation Failed", "Windows did not start the elevated application.")

    def _restart_steps(self, setup: list[CommandStep]) -> list[CommandStep]:
        return [
            *setup,
            CommandStep(self._shutdown_command("/r", "/t", IMMEDIATE_POWER_DELAY)),
        ]

    def boot_normal(self) -> None:
        spec = ActionSpec(
            "Restart to Normal Mode (Repair)",
            "Clear legacy Safe Boot flags and restart after the selected timer.",
            self._restart_steps([
                CommandStep(["bcdedit", "/deletevalue", "{current}", "safeboot"], ignore_error=True),
                CommandStep(["bcdedit", "/deletevalue", "{current}", "safebootalternateshell"], ignore_error=True),
                CommandStep(["bcdedit", "/deletevalue", "{bootmgr}", "bootsequence"], ignore_error=True),
            ]),
            destructive=True,
            success_message="Safe Boot flags were cleared and Windows is restarting in Normal Mode.",
            show_success_dialog=False,
        )
        self.execute(spec, confirmation="Windows will restart in Normal Mode after the selected timer. Save your work first. Continue?", power=True)

    def _boot_safe_once(self, mode: str, title: str, *, alternate_shell: bool = False) -> None:
        spec = ActionSpec(
            title,
            "Create a temporary one-time Safe Mode boot entry.",
            [
                CommandStep(one_time_safe_boot_command(mode, alternate_shell=alternate_shell)),
                CommandStep(self._shutdown_command("/r", "/t", IMMEDIATE_POWER_DELAY)),
            ],
            destructive=True,
            success_message=(
                "One-time Safe Mode was prepared. This restart enters Safe Mode once; "
                "the following restart uses Normal Mode automatically."
            ),
            show_success_dialog=False,
        )
        self.execute(
            spec,
            confirmation=(
                f"Windows will restart into {title} after the selected timer. It will use Normal Mode on the "
                "following restart. Save your work first. Continue?"
            ),
            power=True,
        )

    def boot_safe_minimal(self) -> None:
        self._boot_safe_once("minimal", "One-Time Safe Mode")

    def boot_safe_network(self) -> None:
        self._boot_safe_once("network", "One-Time Safe Mode with Networking")

    def boot_safe_cmd(self) -> None:
        self._boot_safe_once("minimal", "One-Time Safe Mode Command Prompt", alternate_shell=True)

    def boot_advanced(self) -> None:
        spec = ActionSpec(
            "Advanced Startup",
            "Restart into Windows Recovery Environment.",
            [CommandStep(self._shutdown_command("/r", "/o", "/t", IMMEDIATE_POWER_DELAY))],
            destructive=True,
            success_message="Advanced Startup restart was sent.",
            show_success_dialog=False,
        )
        self.execute(spec, confirmation="Windows will restart into Advanced Startup after the selected timer. Save your work first. Continue?", power=True)

    def boot_firmware(self) -> None:
        if firmware_type() != "UEFI":
            proceed = self.confirm(
                "UEFI Firmware Settings",
                "UEFI firmware was not detected. This command may fail on a Legacy BIOS system. Try anyway?",
            )
            if not proceed:
                return
        spec = ActionSpec(
            "UEFI Firmware Settings",
            "Restart into firmware setup.",
            [CommandStep(self._shutdown_command("/r", "/fw", "/t", IMMEDIATE_POWER_DELAY))],
            destructive=True,
            success_message="Firmware setup restart was sent.",
            show_success_dialog=False,
        )
        self.execute(spec, confirmation="Windows will restart into UEFI firmware settings after the selected timer. Save your work first. Continue?", power=True)

    def set_boot_timeout(self) -> None:
        seconds = str(self.timeout_spin.value())
        spec = ActionSpec(
            "Set Boot Menu Timeout",
            "Set BCD timeout.",
            [CommandStep(["bcdedit", "/timeout", seconds])],
            success_message=f"Boot menu timeout was set to {seconds} seconds.",
        )
        self.execute(spec)

    def set_boot_policy(self, policy: str) -> None:
        label = "Legacy F8" if policy == "legacy" else "Standard"
        spec = ActionSpec(
            f"Set {label} Boot Menu",
            "Change boot menu policy.",
            [CommandStep(["bcdedit", "/set", "{default}", "bootmenupolicy", policy])],
            success_message=f"Boot menu policy was changed to {label}.",
        )
        self.execute(spec, confirmation=f"Change the Windows boot menu policy to {label}?")

    def power_restart(self) -> None:
        self.execute(
            ActionSpec(
                "Restart Windows",
                "Restart Windows after the selected timer.",
                [CommandStep(self._shutdown_command("/r", "/t", IMMEDIATE_POWER_DELAY))],
                destructive=True,
                success_message="Restart command was sent.",
                show_success_dialog=False,
            ),
            confirmation="Windows will restart after the selected timer. Save your work first. Continue?",
            power=True,
        )

    def power_shutdown(self) -> None:
        self.execute(
            ActionSpec(
                "Shut Down Windows",
                "Shut down Windows after the selected timer.",
                [CommandStep(self._shutdown_command("/s", "/t", IMMEDIATE_POWER_DELAY))],
                destructive=True,
                success_message="Shutdown command was sent.",
                show_success_dialog=False,
            ),
            confirmation="Windows will shut down after the selected timer. Save your work first. Continue?",
            power=True,
        )

    def power_signout(self) -> None:
        self.execute(ActionSpec("Sign Out", "Sign out user after the selected timer.", [CommandStep(self._shutdown_command("/l"))], requires_admin=False, destructive=True, success_message="Sign-out command was sent."), confirmation="You will be signed out after the selected timer and unsaved work may be lost. Continue?", power=True)

    def power_lock(self) -> None:
        self.execute(ActionSpec("Lock Windows", "Lock workstation after the selected timer.", [CommandStep(["rundll32.exe", "user32.dll,LockWorkStation"])], requires_admin=False, success_message="Windows was locked."), confirmation="Windows will lock after the selected timer. Continue?", power=True)

    def power_hibernate(self) -> None:
        self.execute(ActionSpec("Hibernate", "Hibernate Windows after the selected timer.", [CommandStep(self._shutdown_command("/h"))], requires_admin=False, destructive=True, success_message="Hibernate command was sent."), confirmation="Windows will hibernate after the selected timer. Continue?", power=True)

    def power_sleep(self) -> None:
        command = [
            "powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command",
            "Add-Type -AssemblyName System.Windows.Forms; "
            "[System.Windows.Forms.Application]::SetSuspendState("
            f"[System.Windows.Forms.PowerState]::Suspend, "
            f"{'$true' if self.force_checkbox.isChecked() else '$false'}, $false)",
        ]
        self.execute(ActionSpec("Sleep", "Sleep Windows after the selected timer.", [CommandStep(command)], requires_admin=False, destructive=True, success_message="Windows resumed from sleep."), confirmation="Windows will enter sleep mode after the selected timer. Continue?", power=True)

    def cancel_shutdown(self) -> None:
        cancelled_local = self._cancel_pending_power_action()
        message = (
            "The scheduled app countdown was cancelled. Windows was also asked to abort any native pending shutdown or restart."
            if cancelled_local
            else "Windows was asked to abort any native pending shutdown or restart."
        )
        self.execute(ActionSpec("Cancel Power Action", "Abort pending power action.", [CommandStep(["shutdown", "/a"], ignore_error=True)], requires_admin=False, success_message=message))

    def registry_action(self, name: str, steps: list[list[str]], success: str) -> None:
        self.execute(ActionSpec(name, name, [CommandStep(x) for x in steps], success_message=success), confirmation=f"Apply this setting: {name}?")

    def enable_hibernate(self) -> None:
        self.registry_action("Enable Hibernate", [["powercfg", "/h", "on"]], "Hibernate was enabled.")

    def disable_hibernate(self) -> None:
        self.registry_action("Disable Hibernate", [["powercfg", "/h", "off"]], "Hibernate was disabled. Fast Startup is also unavailable while hibernation is disabled.")

    def enable_fast_startup(self) -> None:
        self.registry_action("Enable Fast Startup", [
            ["powercfg", "/h", "on"],
            ["reg", "add", r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Power", "/v", "HiberbootEnabled", "/t", "REG_DWORD", "/d", "1", "/f"],
        ], "Fast Startup was enabled.")

    def disable_fast_startup(self) -> None:
        self.registry_action("Disable Fast Startup", [["reg", "add", r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Power", "/v", "HiberbootEnabled", "/t", "REG_DWORD", "/d", "0", "/f"]], "Fast Startup was disabled.")

    def enable_bsod_restart(self) -> None:
        self.registry_action("Enable BSOD Automatic Restart", [["reg", "add", r"HKLM\SYSTEM\CurrentControlSet\Control\CrashControl", "/v", "AutoReboot", "/t", "REG_DWORD", "/d", "1", "/f"]], "BSOD automatic restart was enabled.")

    def disable_bsod_restart(self) -> None:
        self.registry_action("Disable BSOD Automatic Restart", [["reg", "add", r"HKLM\SYSTEM\CurrentControlSet\Control\CrashControl", "/v", "AutoReboot", "/t", "REG_DWORD", "/d", "0", "/f"]], "BSOD automatic restart was disabled.")

    def enable_clear_pagefile(self) -> None:
        self.registry_action("Enable Clear Page File at Shutdown", [["reg", "add", r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management", "/v", "ClearPageFileAtShutdown", "/t", "REG_DWORD", "/d", "1", "/f"]], "Page-file clearing at shutdown was enabled.")

    def disable_clear_pagefile(self) -> None:
        self.registry_action("Disable Clear Page File at Shutdown", [["reg", "add", r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management", "/v", "ClearPageFileAtShutdown", "/t", "REG_DWORD", "/d", "0", "/f"]], "Page-file clearing at shutdown was disabled.")

    def enable_auto_end_tasks(self) -> None:
        self.registry_action("Enable AutoEndTasks", [["reg", "add", r"HKCU\Control Panel\Desktop", "/v", "AutoEndTasks", "/t", "REG_SZ", "/d", "1", "/f"]], "AutoEndTasks was enabled for the current user.")

    def disable_auto_end_tasks(self) -> None:
        self.registry_action("Disable AutoEndTasks", [["reg", "add", r"HKCU\Control Panel\Desktop", "/v", "AutoEndTasks", "/t", "REG_SZ", "/d", "0", "/f"]], "AutoEndTasks was disabled for the current user.")

    def remove_startup_delay(self) -> None:
        self.registry_action("Remove Startup App Delay", [["reg", "add", r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Serialize", "/v", "StartupDelayInMSec", "/t", "REG_DWORD", "/d", "0", "/f"]], "Startup app delay was removed.")

    def restore_startup_delay(self) -> None:
        spec = ActionSpec(
            "Restore Startup App Delay",
            "Remove the custom StartupDelayInMSec value.",
            [CommandStep(["reg", "delete", r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Serialize", "/v", "StartupDelayInMSec", "/f"], ignore_error=True)],
            success_message="Windows default startup delay behavior was restored.",
        )
        self.execute(spec, confirmation="Restore Windows default startup app delay behavior?")

    def backup_bcd(self) -> None:
        if not self.ensure_windows() or not self.ensure_admin():
            return
        default_name = f"BCD_Backup_{datetime.now():%Y%m%d_%H%M%S}.bcd"
        path, _ = QFileDialog.getSaveFileName(self, "Save BCD Backup", str(Path.home() / "Desktop" / default_name), "BCD Backup (*.bcd);;All Files (*.*)")
        if not path:
            return
        spec = ActionSpec("Backup BCD", "Export BCD store.", [CommandStep(["bcdedit", "/export", path])], success_message=f"BCD backup saved to:\n{path}")
        self.execute(spec)

    def restore_bcd(self) -> None:
        if not self.ensure_windows() or not self.ensure_admin():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Select BCD Backup", str(Path.home()), "BCD Backup (*.bcd);;All Files (*.*)")
        if not path:
            return
        spec = ActionSpec("Restore BCD", "Import BCD store.", [CommandStep(["bcdedit", "/import", path])], destructive=True, success_message="BCD store was restored. Restart Windows to apply it.")
        self.execute(spec, confirmation="Restoring an incorrect BCD backup can make Windows unbootable. Restore the selected backup?")

    def create_restore_point(self) -> None:
        command = [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
            "Checkpoint-Computer -Description 'Boot Manager' -RestorePointType 'MODIFY_SETTINGS'",
        ]
        spec = ActionSpec("Create Restore Point", "Create Windows restore point.", [CommandStep(command)], success_message="A Windows restore point was requested. Windows may limit restore-point creation frequency.")
        self.execute(spec)

    def launch_tool(self, executable: str) -> None:
        if not self.ensure_windows():
            return
        try:
            os.startfile(executable)  # type: ignore[attr-defined]
            append_log(f"OPEN {executable}")
        except Exception as exc:
            QMessageBox.critical(self, "Unable to Open Tool", str(exc))

    def launch_uri(self, uri: str) -> None:
        if not self.ensure_windows():
            return
        try:
            os.startfile(uri)  # type: ignore[attr-defined]
            append_log(f"OPEN {uri}")
        except Exception as exc:
            QMessageBox.critical(self, "Unable to Open Settings", str(exc))

    def open_log_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(app_data_dir())))

    def load_log(self) -> None:
        if LOG_FILE.exists():
            try:
                lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
                self.log_view.setPlainText("\n".join(lines[-500:]))
                self.log_view.moveCursor(QTextCursor.MoveOperation.End)
            except Exception:
                pass

    def clear_log(self) -> None:
        if not self.confirm("Clear Log", "Delete the application log file?"):
            return
        try:
            LOG_FILE.unlink(missing_ok=True)
            self.log_view.clear()
            self.statusBar().showMessage("Log cleared", 3000)
        except Exception as exc:
            QMessageBox.critical(self, "Clear Log Failed", str(exc))

    def show_about(self) -> None:
        AboutDialog(self).exec()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.pending_power_spec is not None:
            answer = QMessageBox.question(
                self,
                "Power Action Scheduled",
                f"{self.pending_power_spec.name} is still counting down. Exit and cancel the scheduled action?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._cancel_pending_power_action()
        if self.active_workers:
            answer = QMessageBox.question(
                self,
                "Operations Running",
                "One or more operations are still running. Exit anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.accept()


DARK_STYLE = """
QWidget { background:#0b1220; color:#e5e7eb; font-family:'Segoe UI'; font-size:10pt; }
QMainWindow, QMenuBar, QMenu, QStatusBar { background:#0b1220; }
QMenuBar::item:selected, QMenu::item:selected { background:#1d4ed8; }
QLabel#mainTitle { font-size:19pt; font-weight:700; color:#f8fafc; }
QLabel#headerPower { font-size:30pt; color:#60a5fa; padding-right:4px; }
QLabel#aboutPower { font-size:28pt; color:#60a5fa; }
QLabel#aboutTitle { font-size:13pt; font-weight:700; }
QLabel#aboutContent { font-size:8.5pt; }
QLabel#mutedLabel { color:#94a3b8; font-size:9pt; }
QLabel#statusValue { font-size:10.5pt; font-weight:650; color:#f8fafc; }
QLabel#statusValue[state="ok"] { color:#4ade80; }
QLabel#statusValue[state="warning"] { color:#fbbf24; }
QLabel#statusValue[state="unknown"] { color:#94a3b8; }
QLabel#tweakState { font-size:9pt; font-weight:650; color:#cbd5e1; }
QLabel#tweakState[state="ok"] { color:#4ade80; }
QLabel#tweakState[state="warning"] { color:#fbbf24; }
QLabel#tweakState[state="unknown"] { color:#94a3b8; }
QFrame#statusCard, QFrame#actionCard, QGroupBox { background:#111c2f; border:1px solid #26354d; border-radius:8px; }
QFrame#actionCard:hover { border-color:#3b82f6; }
QGroupBox { margin-top:9px; padding-top:10px; font-weight:600; }
QGroupBox::title { subcontrol-origin:margin; left:12px; padding:0 5px; color:#bfdbfe; }
QLabel#notice { background:#172554; border:1px solid #1d4ed8; border-radius:7px; padding:7px; color:#dbeafe; }
QLabel[admin="true"] { background:#14532d; color:#bbf7d0; border:1px solid #22c55e; border-radius:10px; padding:4px 8px; font-weight:700; }
QLabel[admin="false"] { background:#4c1d1d; color:#fecaca; border:1px solid #ef4444; border-radius:10px; padding:4px 8px; font-weight:700; }
QPushButton, QToolButton, QSpinBox { background:#172033; border:1px solid #334155; border-radius:6px; padding:6px 9px; }
QSpinBox#iconSpinBox { padding-right:42px; min-height:32px; font-weight:800; }
QSpinBox#iconSpinBox::up-button { subcontrol-origin:border; subcontrol-position:top right; width:38px; background:#1d4ed8; border-left:1px solid #60a5fa; border-bottom:1px solid #0b1220; border-top-right-radius:5px; }
QSpinBox#iconSpinBox::down-button { subcontrol-origin:border; subcontrol-position:bottom right; width:38px; background:#1d4ed8; border-left:1px solid #60a5fa; border-top:1px solid #0b1220; border-bottom-right-radius:5px; }
QSpinBox#iconSpinBox::up-button:hover, QSpinBox#iconSpinBox::down-button:hover { background:#2563eb; }
QSpinBox#iconSpinBox::up-button:pressed, QSpinBox#iconSpinBox::down-button:pressed { background:#1e40af; }
QSpinBox#iconSpinBox::up-arrow { image:url("__SPIN_PLUS_ICON__"); width:16px; height:16px; }
QSpinBox#iconSpinBox::down-arrow { image:url("__SPIN_MINUS_ICON__"); width:16px; height:16px; }
QPushButton:hover, QToolButton:hover { background:#1e3a5f; border-color:#60a5fa; }
QPushButton:pressed { background:#1d4ed8; }
QPushButton[danger="true"] { background:#7f1d1d; border-color:#dc2626; }
QPushButton[danger="true"]:hover { background:#991b1b; }
QToolButton#refreshButton { font-size:15pt; padding:2px 8px; }
QTabWidget::pane { border:1px solid #26354d; border-radius:8px; background:#0f172a; }
QTabBar::tab { background:#111827; color:#94a3b8; padding:7px 12px; border:1px solid #26354d; border-bottom:none; }
QTabBar::tab:selected { background:#1d4ed8; color:white; }
QPlainTextEdit { background:#070d18; border:1px solid #26354d; border-radius:8px; font-family:Consolas; color:#d1d5db; }
QScrollArea { background:transparent; }
QCheckBox::indicator { width:17px; height:17px; }
QLabel#powerOptionIcon { font-size:25pt; color:#60a5fa; min-width:42px; }
QLabel#powerTimerCaption { color:#e2e8f0; font-size:10.5pt; font-weight:800; }
QLabel#powerCountdown { color:#94a3b8; font-weight:700; padding:5px; }
QLabel#powerCountdown[active="true"] { color:#fbbf24; background:#422006; border:1px solid #f59e0b; border-radius:5px; }
QCheckBox#criticalPowerCheck, QCheckBox#forcePowerCheck { spacing:11px; padding:4px 6px; font-weight:900; }
QCheckBox#criticalPowerCheck { color:#ff4d5e; font-size:11pt; }
QCheckBox#forcePowerCheck { color:#fbbf24; font-size:10.5pt; }
QCheckBox#criticalPowerCheck::indicator, QCheckBox#forcePowerCheck::indicator {
    width:28px; height:28px; border:3px solid #f8fafc; border-radius:5px; background:#020617;
}
QCheckBox#criticalPowerCheck::indicator:checked {
    image:url("__CHECK_ICON__"); background:#dc2626; border-color:#ffffff;
}
QCheckBox#forcePowerCheck::indicator:checked {
    image:url("__CHECK_ICON__"); background:#ca8a04; border-color:#ffffff;
}
QCheckBox#criticalPowerCheck::indicator:disabled, QCheckBox#forcePowerCheck::indicator:disabled { background:#475569; border-color:#94a3b8; }
QPushButton#cancelPowerButton { font-weight:850; border-width:2px; }
"""

LIGHT_STYLE = """
QWidget { background:#f3f6fb; color:#172033; font-family:'Segoe UI'; font-size:10pt; }
QMainWindow, QMenuBar, QMenu, QStatusBar { background:#f8fafc; }
QMenuBar::item:selected, QMenu::item:selected { background:#dbeafe; }
QLabel#mainTitle { font-size:19pt; font-weight:700; color:#0f172a; }
QLabel#headerPower { font-size:30pt; color:#2563eb; padding-right:4px; }
QLabel#aboutPower { font-size:28pt; color:#2563eb; }
QLabel#aboutTitle { font-size:13pt; font-weight:700; }
QLabel#aboutContent { font-size:8.5pt; }
QLabel#mutedLabel { color:#64748b; font-size:9pt; }
QLabel#statusValue { font-size:10.5pt; font-weight:650; color:#0f172a; }
QLabel#statusValue[state="ok"] { color:#15803d; }
QLabel#statusValue[state="warning"] { color:#b45309; }
QLabel#statusValue[state="unknown"] { color:#64748b; }
QLabel#tweakState { font-size:9pt; font-weight:650; color:#334155; }
QLabel#tweakState[state="ok"] { color:#15803d; }
QLabel#tweakState[state="warning"] { color:#b45309; }
QLabel#tweakState[state="unknown"] { color:#64748b; }
QFrame#statusCard, QFrame#actionCard, QGroupBox { background:white; border:1px solid #cbd5e1; border-radius:8px; }
QFrame#actionCard:hover { border-color:#2563eb; }
QGroupBox { margin-top:9px; padding-top:10px; font-weight:600; }
QGroupBox::title { subcontrol-origin:margin; left:12px; padding:0 5px; color:#1e3a8a; }
QLabel#notice { background:#eff6ff; border:1px solid #60a5fa; border-radius:7px; padding:7px; color:#1e3a8a; }
QLabel[admin="true"] { background:#dcfce7; color:#166534; border:1px solid #22c55e; border-radius:10px; padding:4px 8px; font-weight:700; }
QLabel[admin="false"] { background:#fee2e2; color:#991b1b; border:1px solid #ef4444; border-radius:10px; padding:4px 8px; font-weight:700; }
QPushButton, QToolButton, QSpinBox { background:white; border:1px solid #cbd5e1; border-radius:6px; padding:6px 9px; }
QSpinBox#iconSpinBox { padding-right:42px; min-height:32px; font-weight:800; }
QSpinBox#iconSpinBox::up-button { subcontrol-origin:border; subcontrol-position:top right; width:38px; background:#2563eb; border-left:1px solid #1d4ed8; border-bottom:1px solid #ffffff; border-top-right-radius:5px; }
QSpinBox#iconSpinBox::down-button { subcontrol-origin:border; subcontrol-position:bottom right; width:38px; background:#2563eb; border-left:1px solid #1d4ed8; border-top:1px solid #ffffff; border-bottom-right-radius:5px; }
QSpinBox#iconSpinBox::up-button:hover, QSpinBox#iconSpinBox::down-button:hover { background:#1d4ed8; }
QSpinBox#iconSpinBox::up-button:pressed, QSpinBox#iconSpinBox::down-button:pressed { background:#1e40af; }
QSpinBox#iconSpinBox::up-arrow { image:url("__SPIN_PLUS_ICON__"); width:16px; height:16px; }
QSpinBox#iconSpinBox::down-arrow { image:url("__SPIN_MINUS_ICON__"); width:16px; height:16px; }
QPushButton:hover, QToolButton:hover { background:#eff6ff; border-color:#2563eb; }
QPushButton:pressed { background:#dbeafe; }
QPushButton[danger="true"] { background:#fee2e2; border-color:#ef4444; color:#991b1b; }
QPushButton[danger="true"]:hover { background:#fecaca; }
QToolButton#refreshButton { font-size:15pt; padding:2px 8px; }
QTabWidget::pane { border:1px solid #cbd5e1; border-radius:8px; background:white; }
QTabBar::tab { background:#e2e8f0; color:#475569; padding:7px 12px; border:1px solid #cbd5e1; border-bottom:none; }
QTabBar::tab:selected { background:#2563eb; color:white; }
QPlainTextEdit { background:#f8fafc; border:1px solid #cbd5e1; border-radius:8px; font-family:Consolas; color:#172033; }
QScrollArea { background:transparent; }
QCheckBox::indicator { width:17px; height:17px; }
QLabel#powerOptionIcon { font-size:25pt; color:#2563eb; min-width:42px; }
QLabel#powerTimerCaption { color:#0f172a; font-size:10.5pt; font-weight:800; }
QLabel#powerCountdown { color:#64748b; font-weight:700; padding:5px; }
QLabel#powerCountdown[active="true"] { color:#92400e; background:#fef3c7; border:1px solid #f59e0b; border-radius:5px; }
QCheckBox#criticalPowerCheck, QCheckBox#forcePowerCheck { spacing:11px; padding:4px 6px; font-weight:900; }
QCheckBox#criticalPowerCheck { color:#c1121f; font-size:11pt; }
QCheckBox#forcePowerCheck { color:#854d0e; font-size:10.5pt; }
QCheckBox#criticalPowerCheck::indicator, QCheckBox#forcePowerCheck::indicator {
    width:28px; height:28px; border:3px solid #0f172a; border-radius:5px; background:#ffffff;
}
QCheckBox#criticalPowerCheck::indicator:checked {
    image:url("__CHECK_ICON__"); background:#dc2626; border-color:#111827;
}
QCheckBox#forcePowerCheck::indicator:checked {
    image:url("__CHECK_ICON__"); background:#ca8a04; border-color:#111827;
}
QCheckBox#criticalPowerCheck::indicator:disabled, QCheckBox#forcePowerCheck::indicator:disabled { background:#cbd5e1; border-color:#64748b; }
QPushButton#cancelPowerButton { font-weight:850; border-width:2px; }
"""


CLASSIC_STYLE = """
QWidget {
    background:#d4d0c8;
    color:#000000;
    font-family:'Tahoma';
    font-size:9pt;
}
QMainWindow, QMenuBar, QMenu, QStatusBar { background:#d4d0c8; }
QMenuBar { border-bottom:1px solid #808080; }
QMenuBar::item { padding:4px 8px; background:transparent; }
QMenuBar::item:selected, QMenu::item:selected { background:#0a246a; color:#ffffff; }
QMenu { border:1px solid #404040; }
QLabel#mainTitle { font-size:17pt; font-weight:700; color:#000080; }
QLabel#headerPower { font-size:28pt; color:#000080; padding-right:4px; }
QLabel#aboutPower { font-size:27pt; color:#000080; }
QLabel#aboutTitle { font-size:12.5pt; font-weight:700; color:#000080; }
QLabel#aboutContent { font-size:8pt; }
QLabel#mutedLabel { color:#404040; font-size:8.5pt; }
QLabel#statusValue { font-size:10pt; font-weight:700; color:#000000; }
QLabel#statusValue[state="ok"] { color:#006400; }
QLabel#statusValue[state="warning"] { color:#9a5b00; }
QLabel#statusValue[state="unknown"] { color:#606060; }
QLabel#tweakState { font-size:8.5pt; font-weight:700; color:#000000; }
QLabel#tweakState[state="ok"] { color:#006400; }
QLabel#tweakState[state="warning"] { color:#9a5b00; }
QLabel#tweakState[state="unknown"] { color:#606060; }
QFrame#statusCard, QFrame#actionCard, QGroupBox {
    background:#d4d0c8;
    border:2px groove #ffffff;
    border-radius:0px;
}
QFrame#actionCard:hover { border:2px groove #000080; }
QGroupBox { margin-top:10px; padding-top:12px; font-weight:700; }
QGroupBox::title { subcontrol-origin:margin; left:8px; padding:0 4px; color:#000000; }
QLabel#notice {
    background:#ffffe1;
    border:1px solid #808000;
    border-radius:0px;
    padding:8px;
    color:#000000;
}
QLabel[admin="true"] {
    background:#d4d0c8;
    color:#006400;
    border:2px inset #ffffff;
    border-radius:0px;
    padding:5px 8px;
    font-weight:700;
}
QLabel[admin="false"] {
    background:#d4d0c8;
    color:#800000;
    border:2px inset #ffffff;
    border-radius:0px;
    padding:5px 8px;
    font-weight:700;
}
QPushButton, QToolButton, QSpinBox {
    background:#d4d0c8;
    color:#000000;
    border:2px outset #ffffff;
    border-radius:0px;
    padding:6px 10px;
}
QSpinBox#iconSpinBox { padding-right:42px; min-height:30px; font-weight:700; }
QSpinBox#iconSpinBox::up-button { subcontrol-origin:border; subcontrol-position:top right; width:38px; background:#000080; border:2px outset #ffffff; }
QSpinBox#iconSpinBox::down-button { subcontrol-origin:border; subcontrol-position:bottom right; width:38px; background:#000080; border:2px outset #ffffff; }
QSpinBox#iconSpinBox::up-button:pressed, QSpinBox#iconSpinBox::down-button:pressed { background:#0a246a; border:2px inset #ffffff; }
QSpinBox#iconSpinBox::up-arrow { image:url("__SPIN_PLUS_ICON__"); width:15px; height:15px; }
QSpinBox#iconSpinBox::down-arrow { image:url("__SPIN_MINUS_ICON__"); width:15px; height:15px; }
QPushButton:hover, QToolButton:hover { background:#e5e2dc; }
QPushButton:pressed, QToolButton:pressed { border:2px inset #ffffff; padding-top:7px; padding-left:11px; }
QPushButton:focus, QToolButton:focus:focus, QSpinBox:focus { outline:1px dotted #000000; }
QPushButton[danger="true"] { background:#d4d0c8; color:#800000; font-weight:700; }
QPushButton[danger="true"]:hover { background:#ffd7d7; }
QToolButton#refreshButton { font-size:16pt; padding:2px 8px; }
QTabWidget::pane { border:2px inset #ffffff; background:#d4d0c8; }
QTabBar::tab {
    background:#d4d0c8;
    color:#000000;
    padding:6px 10px;
    border:2px outset #ffffff;
    border-bottom:none;
    border-radius:0px;
}
QTabBar::tab:selected { background:#ffffff; color:#000000; }
QPlainTextEdit {
    background:#ffffff;
    color:#000000;
    border:2px inset #ffffff;
    border-radius:0px;
    font-family:'Courier New';
}
QScrollArea { background:transparent; }
QCheckBox::indicator { width:13px; height:13px; }
QLabel#powerOptionIcon { font-size:23pt; color:#000080; min-width:42px; }
QLabel#powerTimerCaption { color:#000000; font-size:10pt; font-weight:700; }
QLabel#powerCountdown { color:#404040; font-weight:700; padding:5px; }
QLabel#powerCountdown[active="true"] { color:#800000; background:#ffffe1; border:2px inset #ffffff; }
QCheckBox#criticalPowerCheck, QCheckBox#forcePowerCheck { spacing:10px; padding:4px 6px; font-weight:700; }
QCheckBox#criticalPowerCheck { color:#c00000; font-size:10pt; }
QCheckBox#forcePowerCheck { color:#804000; font-size:9.5pt; }
QCheckBox#criticalPowerCheck::indicator, QCheckBox#forcePowerCheck::indicator {
    width:26px; height:26px; border:2px solid #000000; border-radius:0px; background:#ffffff;
}
QCheckBox#criticalPowerCheck::indicator:checked { image:url("__CHECK_ICON__"); background:#c00000; }
QCheckBox#forcePowerCheck::indicator:checked { image:url("__CHECK_ICON__"); background:#9a6700; }
QCheckBox#criticalPowerCheck::indicator:disabled, QCheckBox#forcePowerCheck::indicator:disabled { background:#a0a0a0; border-color:#606060; }
QPushButton#cancelPowerButton { font-weight:700; border-width:2px; }
QStatusBar { border-top:2px inset #ffffff; }
"""


def main() -> int:
    QApplication.setOrganizationName(ORG_NAME)
    QApplication.setApplicationName(APP_NAME)
    QApplication.setApplicationVersion(APP_VERSION)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    font = QFont("Segoe UI", 10)
    app.setFont(font)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
