"""Game-only window handling. Never changes the desktop video mode or focus."""

from __future__ import annotations

import ctypes
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


def _write_options(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent,
                                         prefix="dingding-options-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def _values(text: str) -> dict[str, str]:
    return {line.partition(":")[0]: line.partition(":")[2]
            for line in text.splitlines() if ":" in line}


def _replace_values(text: str, changes: dict[str, str | None]) -> str:
    newline = "\r\n" if "\r\n" in text else "\n"
    result, seen = [], set()
    for line in text.splitlines(keepends=True):
        key = line.partition(":")[0]
        if key not in changes:
            result.append(line)
        elif key not in seen and changes[key] is not None:
            result.append(f"{key}:{changes[key]}{newline}")
        seen.add(key)
    for key, value in changes.items():
        if key not in seen and value is not None:
            if result and not result[-1].endswith(("\n", "\r")):
                result.append(newline)
            result.append(f"{key}:{value}{newline}")
    return "".join(result)


@dataclass
class GameOptionsLease:
    path: Path
    previous: dict[str, str | None]
    applied: dict[str, str]

    def restore(self) -> None:
        if not self.applied or not self.path.is_file():
            return
        text = self.path.read_bytes().decode("utf-8")
        current = _values(text)
        # Merge only unchanged launcher overrides; retain game/mod/user edits.
        restore = {key: self.previous[key] for key, value in self.applied.items()
                   if current.get(key) == value}
        if restore:
            _write_options(self.path, _replace_values(text, restore))
        self.applied.clear()


def prepare_window_options(game_dir: Path) -> GameOptionsLease:
    """Prevent saved exclusive-fullscreen/size settings overriding launch flags."""
    path = Path(game_dir) / "options.txt"
    text = path.read_bytes().decode("utf-8") if path.is_file() else ""
    values = _values(text)
    desired = {"fullscreen": "false"}
    for key in ("overrideWidth", "overrideHeight"):
        if key in values:
            desired[key] = "0"
    applied = {key: value for key, value in desired.items() if values.get(key) != value}
    previous = {key: values.get(key) for key in applied}
    if applied:
        _write_options(path, _replace_values(text, applied))
    return GameOptionsLease(path, previous, applied)


class WindowsWindowSystem:
    """Small typed Win32 interface restricted to windows owned by the game PID."""

    def __init__(self):
        if os.name != "nt":
            raise OSError("Win32 window handling is unavailable on this platform")
        from ctypes import wintypes
        self.types = wintypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user32.EnumWindows.argtypes = [self.callback_type, wintypes.LPARAM]
        self.user32.EnumWindows.restype = wintypes.BOOL
        self.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user32.IsWindowVisible.restype = wintypes.BOOL
        self.user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user32.GetWindow.restype = wintypes.HWND
        for name in ("GetClassNameW", "GetWindowTextW"):
            function = getattr(self.user32, name)
            function.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            function.restype = ctypes.c_int
        self.user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user32.GetWindowRect.restype = wintypes.BOOL
        self.user32.IsIconic.argtypes = [wintypes.HWND]
        self.user32.IsIconic.restype = wintypes.BOOL
        self.user32.MonitorFromRect.argtypes = [ctypes.POINTER(wintypes.RECT), wintypes.DWORD]
        self.user32.MonitorFromRect.restype = wintypes.HANDLE
        self.user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        self.user32.MonitorFromWindow.restype = wintypes.HANDLE
        self.user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        self.user32.GetMonitorInfoW.restype = wintypes.BOOL
        pointer_suffix = "PtrW" if ctypes.sizeof(ctypes.c_void_p) == 8 else "W"
        self.get_style = getattr(self.user32, "GetWindowLong" + pointer_suffix)
        self.get_style.argtypes = [wintypes.HWND, ctypes.c_int]
        self.get_style.restype = ctypes.c_ssize_t
        self.set_style = getattr(self.user32, "SetWindowLong" + pointer_suffix)
        self.set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        self.set_style.restype = ctypes.c_ssize_t
        self.user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self.user32.SetWindowPos.restype = wintypes.BOOL
        # Thread-local only: never change the launcher's or game's process DPI.
        self.set_dpi_context = getattr(self.user32, "SetThreadDpiAwarenessContext", None)
        self.get_dpi_context = getattr(self.user32, "GetWindowDpiAwarenessContext", None)
        if self.set_dpi_context and self.get_dpi_context:
            self.set_dpi_context.argtypes = [ctypes.c_void_p]
            self.set_dpi_context.restype = ctypes.c_void_p
            self.get_dpi_context.argtypes = [wintypes.HWND]
            self.get_dpi_context.restype = ctypes.c_void_p

    @contextmanager
    def dpi_context(self, context):
        setter = self.set_dpi_context
        previous = setter(context) if setter and context else None
        if setter and context and not previous:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield
        finally:
            if previous:
                setter(previous)

    def window_pid(self, handle: int) -> int:
        pid = self.types.DWORD()
        self.user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        return pid.value

    def find_game_window(self, pid: int) -> int | None:
        candidates = []

        @self.callback_type
        def visit(handle, _):
            if (self.window_pid(handle) != pid or not self.user32.IsWindowVisible(handle)
                    or self.user32.IsIconic(handle)):
                return True
            if self.user32.GetWindow(handle, 4):  # GW_OWNER: skip dialogs and owned popups.
                return True
            name, title = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(512)
            self.user32.GetClassNameW(handle, name, len(name))
            self.user32.GetWindowTextW(handle, title, len(title))
            if not name.value.casefold().startswith(("glfw", "lwjgl")):
                return True
            if any(marker in title.value.lower() for marker in ("early loading", "early progress", "fml early")):
                return True
            rect = self.types.RECT()
            if self.user32.GetWindowRect(handle, ctypes.byref(rect)):
                width, height = rect.right - rect.left, rect.bottom - rect.top
                if width >= 320 and height >= 240:
                    candidates.append((width * height, handle))
            return True

        self.user32.EnumWindows(visit, 0)
        return max(candidates, default=(0, None))[1]

    def monitor_bounds(self, handle: int) -> tuple[int, int, int, int]:
        # Qt and Java can have different awareness. LaunchPlan always stores
        # physical monitor coordinates, independently of this calling thread.
        with self.dpi_context(-4):  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            monitor = self.user32.MonitorFromWindow(handle, 2)
            return self._monitor_bounds(monitor)

    def _monitor_bounds(self, monitor) -> tuple[int, int, int, int]:
        types = self.types

        class MonitorInfo(ctypes.Structure):
            _fields_ = [("size", types.DWORD), ("monitor", types.RECT),
                        ("work", types.RECT), ("flags", types.DWORD)]

        info = MonitorInfo()
        info.size = ctypes.sizeof(info)
        if not monitor or not self.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        rect = info.monitor
        return rect.left, rect.top, rect.right, rect.bottom

    def window_state(self, handle: int, pid: int):
        if self.window_pid(handle) != pid or self.user32.IsIconic(handle):
            return None
        with self.dpi_context(-4):
            rect = self.types.RECT()
            if not self.user32.GetWindowRect(handle, ctypes.byref(rect)):
                return None
            return (rect.left, rect.top, rect.right, rect.bottom), self.get_style(handle, -16) & 0xFFFFFFFF

    def is_borderless(self, handle: int, pid: int, bounds=None) -> bool:
        state = self.window_state(handle, pid)
        if not state:
            return False
        rect, style = state
        target = bounds or self.monitor_bounds(handle)
        # Rounding virtualized coordinates at 150% can differ by one pixel.
        return (not style & (0x00CF0000 | 0x21000000)
                and all(abs(actual - expected) <= 1 for actual, expected in zip(rect, target)))

    def make_borderless(self, handle: int, pid: int, bounds=None) -> None:
        # Re-check ownership immediately before changing the window.
        if self.window_pid(handle) != pid:
            raise OSError("游戏窗口已退出或发生变化。")
        with self.dpi_context(-4):
            if bounds:
                rect = self.types.RECT(*bounds)
                monitor = self.user32.MonitorFromRect(ctypes.byref(rect), 2)
            else:
                monitor = self.user32.MonitorFromWindow(handle, 2)
        # Read monitor coordinates AND apply them in the target window's context.
        # Do not divide by an assumed scale factor: mixed-DPI/negative-origin
        # monitors have different coordinate origins, not just different sizes.
        context = self.get_dpi_context(handle) if self.get_dpi_context else None
        with self.dpi_context(context):
            self._make_borderless(handle, pid, monitor)

    def _make_borderless(self, handle, pid, monitor):
        if self.window_pid(handle) != pid:
            raise OSError("游戏窗口已退出或发生变化。")
        left, top, right, bottom = self._monitor_bounds(monitor)
        if right <= left or bottom <= top:
            raise OSError("屏幕尺寸无效。")
        original = self.get_style(handle, -16)  # GWL_STYLE
        # Remove caption, resize frame, system menu and min/max borders.
        style = (original & ~0x00CF0000 & ~0x21000000) | 0x80000000  # WS_POPUP
        ctypes.set_last_error(0)
        result = self.set_style(handle, -16, style)
        if not result and ctypes.get_last_error():
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            # Disable only this window's DWM transition, not system animations.
            dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
            dwm.DwmSetWindowAttribute.argtypes = [self.types.HWND, self.types.DWORD, ctypes.c_void_p, self.types.DWORD]
            dwm.DwmSetWindowAttribute.restype = ctypes.c_long
            disabled = self.types.BOOL(True)
            dwm.DwmSetWindowAttribute(handle, 3, ctypes.byref(disabled), ctypes.sizeof(disabled))
        except OSError:
            pass
        # Change style and bounds in one operation; never raise, focus or topmost.
        flags = 0x0020 | 0x0004 | 0x0010 | 0x0100 | 0x4000
        if not self.user32.SetWindowPos(handle, None, left, top, right - left, bottom - top, flags):
            self.set_style(handle, -16, original)
            raise ctypes.WinError(ctypes.get_last_error())


def apply_borderless_when_ready(process, bounds=None, *, system=None, timeout=180,
                                clock=time.monotonic, sleep=time.sleep) -> bool:
    """Settle, adjust, then verify asynchronously; stop touching the game after startup."""
    system = system or WindowsWindowSystem()
    deadline = clock() + timeout
    previous, stable_since, verified_since = None, None, None
    attempts, last_adjustment = 0, None
    while process.poll() is None and clock() < deadline:
        now = clock()
        handle = system.find_game_window(process.pid)
        if handle:
            state = system.window_state(handle, process.pid)
            if not state:
                sleep(0.1)
                continue
            snapshot = handle, state
            if snapshot != previous:
                previous, stable_since, verified_since = snapshot, now, None
            if system.is_borderless(handle, process.pid, bounds):
                if verified_since is None:
                    verified_since = now
                if now - verified_since >= 1.0:
                    return True
            else:
                verified_since = None
                # LWJGL initializes resizable styles after creating its HWND.
                # Also give SWP_ASYNCWINDOWPOS time to reach the render thread.
                if now - stable_since >= 0.3 and (last_adjustment is None or now - last_adjustment >= 1.0):
                    if attempts >= 3:
                        return False
                    try:
                        system.make_borderless(handle, process.pid, bounds)
                    except OSError:
                        if system.window_pid(handle) == process.pid:
                            raise
                        # Game may replace its initial HWND during setup.
                    attempts += 1
                    last_adjustment = now
                    deadline = min(deadline, now + 10)
        else:
            previous, stable_since, verified_since = None, None, None
        sleep(0.1)
    return False


def launcher_monitor_bounds(handle: int):
    try:
        return WindowsWindowSystem().monitor_bounds(handle)
    except OSError:
        return None
