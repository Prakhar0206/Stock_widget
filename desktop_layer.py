"""desktop_layer.py - the desktop-layer behaviour of the existing clock, moved here unchanged and
wrapped in a reusable class (blueprint 9.1). Windows only.

The Win32 bindings, z-order logic and the Alt+W / Ctrl+Alt+Shift+Q low-level hook below are copied
from the clock. What is new: the window can change size, an interactive mode (edit mode and pop-ups
need clicks and focus), WM_HOTKEY handling for the new hotkeys (RegisterHotKey, no hook), and
notifications for display / DPI changes, lock-unlock, remote-desktop reconnect and resume.
"""
import ctypes
import sys
import threading
from ctypes import wintypes

if sys.platform != "win32":
    raise ImportError("desktop_layer only runs on Windows")

PEEK_KEY = "W"              # held together with Alt (carried over from the clock; it works)
QUIT_KEY = "Q"              # Ctrl+Alt+Shift+<key> exits (carried over from the clock)
KEY_POLL_MS = 40            # how often peek state is checked (clock used 25)
ZORDER_CHECK_MS = 100       # how often desktop-layer position is verified


# ----------------------------------------------------------------------------
# Win32 bindings (explicit prototypes so 64-bit handles are never truncated)
# ----------------------------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
HWND = wintypes.HWND
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


def _proto(func, restype, *argtypes):
    func.restype = restype
    func.argtypes = argtypes
    return func


FindWindowW = _proto(user32.FindWindowW, HWND, wintypes.LPCWSTR, wintypes.LPCWSTR)
FindWindowExW = _proto(user32.FindWindowExW, HWND, HWND, HWND, wintypes.LPCWSTR, wintypes.LPCWSTR)
GetWindow = _proto(user32.GetWindow, HWND, HWND, wintypes.UINT)
GetParent = _proto(user32.GetParent, HWND, HWND)
IsWindowVisible = _proto(user32.IsWindowVisible, wintypes.BOOL, HWND)
GetWindowLongW = _proto(user32.GetWindowLongW, wintypes.LONG, HWND, ctypes.c_int)
SetWindowLongW = _proto(user32.SetWindowLongW, wintypes.LONG, HWND, ctypes.c_int, wintypes.LONG)
# user32[...] returns a fresh function object, so prototypes don't clash.
SetWindowLongPtrW = _proto(
    user32["SetWindowLongPtrW" if ctypes.sizeof(ctypes.c_void_p) == 8 else "SetWindowLongW"],
    ctypes.c_void_p, HWND, ctypes.c_int, ctypes.c_void_p)
CallWindowProcW = _proto(user32.CallWindowProcW, LRESULT, ctypes.c_void_p, HWND,
                         wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
SetWindowPos = _proto(user32.SetWindowPos, wintypes.BOOL, HWND, ctypes.c_ssize_t,
                      ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT)
SystemParametersInfoW = _proto(user32.SystemParametersInfoW, wintypes.BOOL,
                               wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT)
SetLayeredWindowAttributes = _proto(user32.SetLayeredWindowAttributes, wintypes.BOOL,
                                    HWND, wintypes.DWORD, wintypes.BYTE, wintypes.DWORD)
# Multi-monitor APIs. HMONITOR is a pointer-sized handle: declare it as c_void_p so it is never
# truncated to 32 bits on 64-bit Python (the ctypes default return type is c_int).
HMONITOR = ctypes.c_void_p


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, HMONITOR, wintypes.HDC,
                                     ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
MonitorFromWindow = _proto(user32.MonitorFromWindow, HMONITOR, HWND, wintypes.DWORD)
MonitorFromPoint = _proto(user32.MonitorFromPoint, HMONITOR, wintypes.POINT, wintypes.DWORD)
GetMonitorInfoW = _proto(user32.GetMonitorInfoW, wintypes.BOOL, HMONITOR, ctypes.POINTER(MONITORINFO))
EnumDisplayMonitors = _proto(user32.EnumDisplayMonitors, wintypes.BOOL, wintypes.HDC,
                             ctypes.POINTER(wintypes.RECT), MONITORENUMPROC, wintypes.LPARAM)
GetSystemMetrics = _proto(user32.GetSystemMetrics, ctypes.c_int, ctypes.c_int)
GetAsyncKeyState = _proto(user32.GetAsyncKeyState, wintypes.SHORT, ctypes.c_int)
keybd_event = _proto(user32.keybd_event, None, wintypes.BYTE, wintypes.BYTE,
                     wintypes.DWORD, ctypes.c_size_t)
SetWindowsHookExW = _proto(user32.SetWindowsHookExW, ctypes.c_void_p, ctypes.c_int,
                           HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
CallNextHookEx = _proto(user32.CallNextHookEx, LRESULT, ctypes.c_void_p, ctypes.c_int,
                        wintypes.WPARAM, wintypes.LPARAM)
UnhookWindowsHookEx = _proto(user32.UnhookWindowsHookEx, wintypes.BOOL, ctypes.c_void_p)
GetMessageW = _proto(user32.GetMessageW, wintypes.BOOL, ctypes.POINTER(wintypes.MSG),
                     HWND, wintypes.UINT, wintypes.UINT)
TranslateMessage = _proto(user32.TranslateMessage, wintypes.BOOL, ctypes.POINTER(wintypes.MSG))
DispatchMessageW = _proto(user32.DispatchMessageW, LRESULT, ctypes.POINTER(wintypes.MSG))
PostThreadMessageW = _proto(user32.PostThreadMessageW, wintypes.BOOL, wintypes.DWORD,
                            wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
GetModuleHandleW = _proto(kernel32.GetModuleHandleW, wintypes.HMODULE, wintypes.LPCWSTR)
GetCurrentThreadId = _proto(kernel32.GetCurrentThreadId, wintypes.DWORD)
CreateMutexW = _proto(kernel32.CreateMutexW, wintypes.HANDLE, ctypes.c_void_p,
                      wintypes.BOOL, wintypes.LPCWSTR)

# Constants
HWND_TOP, HWND_BOTTOM, HWND_TOPMOST, HWND_NOTOPMOST = 0, 1, -1, -2
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER = 0x0001, 0x0002, 0x0004
SWP_NOACTIVATE, SWP_FRAMECHANGED, SWP_SHOWWINDOW = 0x0010, 0x0020, 0x0040
SWP_HIDEWINDOW, SWP_NOOWNERZORDER = 0x0080, 0x0200
Z_FLAGS = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER | SWP_SHOWWINDOW

GWL_EXSTYLE, GWLP_WNDPROC = -20, -4
WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = 0x8, 0x20, 0x80
WS_EX_APPWINDOW, WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x40000, 0x80000, 0x08000000
LWA_ALPHA = 0x2
GW_HWNDPREV = 3

WM_CLOSE, WM_MOUSEACTIVATE, WM_WINDOWPOSCHANGING = 0x0010, 0x0021, 0x0046
WM_SETTINGCHANGE, WM_DISPLAYCHANGE, WM_NCHITTEST = 0x001A, 0x007E, 0x0084
WM_SYSCOMMAND, WM_QUIT = 0x0112, 0x0012
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
MA_NOACTIVATE, HTCLIENT = 3, 1
SC_SIZE, SC_MOVE, SC_MINIMIZE, SC_MAXIMIZE = 0xF000, 0xF010, 0xF020, 0xF030
SC_CLOSE, SC_RESTORE = 0xF060, 0xF120
BLOCKED_SYSCOMMANDS = {SC_SIZE, SC_MOVE, SC_MINIMIZE, SC_MAXIMIZE, SC_CLOSE, SC_RESTORE}
SPI_SETWORKAREA, SPI_GETWORKAREA = 0x002F, 0x0030
MONITOR_DEFAULTTONULL, MONITOR_DEFAULTTOPRIMARY, MONITOR_DEFAULTTONEAREST = 0, 1, 2
MONITORINFOF_PRIMARY = 0x1
SM_CXSCREEN, SM_CYSCREEN = 0, 1

WH_KEYBOARD_LL, HC_ACTION, LLKHF_INJECTED = 13, 0, 0x10
VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
VK_MASK = 0xE8              # unassigned key, used to stop the Alt-menu popping up
KEYEVENTF_KEYUP = 0x2
ERROR_ALREADY_EXISTS = 183


class WINDOWPOS(ctypes.Structure):
    # HWND fields declared as signed pointer-size ints so we can write -1/-2.
    _fields_ = [("hwnd", ctypes.c_ssize_t), ("hwndInsertAfter", ctypes.c_ssize_t),
                ("x", ctypes.c_int), ("y", ctypes.c_int),
                ("cx", ctypes.c_int), ("cy", ctypes.c_int), ("flags", wintypes.UINT)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


def key_down(vk):
    return bool(GetAsyncKeyState(vk) & 0x8000)


def is_topmost(hwnd):
    return bool(GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)


def find_desktop_window():
    """Return the top-level window that hosts the desktop icons (SHELLDLL_DefView).
    That's Progman normally, or a WorkerW (e.g. during Show Desktop).
    Searched every time, so it keeps working if Explorer restarts."""
    progman = FindWindowW("Progman", None)
    if progman and FindWindowExW(progman, None, "SHELLDLL_DefView", None):
        return progman
    worker = None
    while True:
        worker = FindWindowExW(None, worker, "WorkerW", None)
        if not worker:
            break
        if FindWindowExW(worker, None, "SHELLDLL_DefView", None):
            return worker
    return progman


# ----------------------------------------------------------------------------
# Global hotkey listener (low-level keyboard hook on its own thread)
# ----------------------------------------------------------------------------
def peek_starts(down: bool, alt: bool, ctrl: bool, shift: bool, swallowing: bool) -> bool:
    """Alt+W starts a peek and its W is swallowed. Alt+Shift+W is NOT a peek: it is the edit-mode
    hotkey, so the W must reach Windows and RegisterHotKey must fire."""
    return down and (swallowing or (alt and not ctrl and not shift))


class HotkeyListener(threading.Thread):
    """Only looks at the peek key and quit key; nothing else is stored or used.
    While Alt is held, the peek key is swallowed so the focused app
    (e.g. Office's Alt+W "View" ribbon) never sees it."""

    def __init__(self):
        super().__init__(daemon=True)
        self.peek_key_down = False
        self.quit_requested = False
        self._swallowing = False
        self._peek_vk = ord(PEEK_KEY.upper())
        self._quit_vk = ord(QUIT_KEY.upper()) if QUIT_KEY else None
        self._proc = HOOKPROC(self._hook)   # keep a reference so it isn't GC'd
        self._thread_id = None
        self._ready = threading.Event()     # set once _thread_id is valid

    def run(self):
        self._thread_id = GetCurrentThreadId()
        self._ready.set()
        hook = SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, GetModuleHandleW(None), 0)
        msg = wintypes.MSG()
        # A message loop is required for low-level hooks to be called.
        while GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            TranslateMessage(ctypes.byref(msg))
            DispatchMessageW(ctypes.byref(msg))
        if hook:
            UnhookWindowsHookEx(hook)

    def stop(self):
        if self.is_alive():
            self._ready.wait(1.0)           # stop() right after start() must not be lost
        if self._thread_id:
            PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    @staticmethod
    def _mask_alt():
        # Injecting a dummy key stops "Alt tapped alone" from opening app menus
        # after we swallowed the W. (Same technique AutoHotkey uses.)
        keybd_event(VK_MASK, 0, 0, 0)
        keybd_event(VK_MASK, 0, KEYEVENTF_KEYUP, 0)

    def _hook(self, n_code, w_param, l_param):
        try:
            if n_code == HC_ACTION:
                kb = KBDLLHOOKSTRUCT.from_address(l_param)
                if not (kb.flags & LLKHF_INJECTED):
                    down = w_param in (WM_KEYDOWN, WM_SYSKEYDOWN)
                    alt, ctrl = key_down(VK_MENU), key_down(VK_CONTROL)
                    shift = key_down(VK_SHIFT)
                    if kb.vkCode == self._peek_vk:
                        if peek_starts(down, alt, ctrl, shift, self._swallowing):
                            if not self._swallowing:
                                self._mask_alt()
                            self._swallowing = True
                            self.peek_key_down = True
                            return 1                       # swallow
                        if not down:
                            self.peek_key_down = False
                            if self._swallowing:
                                self._swallowing = False
                                return 1                   # swallow matching key-up
                    elif (self._quit_vk and kb.vkCode == self._quit_vk and down
                          and alt and ctrl and shift):
                        self.quit_requested = True
                        self._mask_alt()
                        return 1
        except Exception:
            pass
        return CallNextHookEx(None, n_code, w_param, l_param)




# ----------------------------------------------------------------------------
# Extra bindings / constants for the widget
# ----------------------------------------------------------------------------
SetForegroundWindow = _proto(user32.SetForegroundWindow, wintypes.BOOL, HWND)
RegisterHotKey = _proto(user32.RegisterHotKey, wintypes.BOOL, HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
UnregisterHotKey = _proto(user32.UnregisterHotKey, wintypes.BOOL, HWND, ctypes.c_int)
try:
    _wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
    WTSRegisterSessionNotification = _proto(_wts.WTSRegisterSessionNotification, wintypes.BOOL, HWND, wintypes.DWORD)
    WTSUnRegisterSessionNotification = _proto(_wts.WTSUnRegisterSessionNotification, wintypes.BOOL, HWND)
except OSError:                                    # very old / stripped Windows: skip lock notifications
    WTSRegisterSessionNotification = WTSUnRegisterSessionNotification = None

WM_POWERBROADCAST, WM_HOTKEY, WM_WTSSESSION_CHANGE, WM_DPICHANGED = 0x0218, 0x0312, 0x02B1, 0x02E0
PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC = 0x0007, 0x0012
WTS_CONSOLE_CONNECT, WTS_REMOTE_CONNECT, WTS_SESSION_UNLOCK = 0x1, 0x3, 0x8
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
VK_BY_NAME = {"W": 0x57, "A": 0x41, "M": 0x4D}


def set_dpi_aware():
    """Crisp text and correct pixel positions on high-DPI screens (call before creating Tk)."""
    try:
        hr = ctypes.windll.shcore.SetProcessDpiAwareness(1)
        if hr not in (0, -2147024891):             # S_OK, or E_ACCESSDENIED (already set)
            raise OSError(hr)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def single_instance(name: str = "Local\\StockAlertWidget_SingleInstance"):
    """Returns a handle to keep alive, or None when another copy is already running."""
    handle = CreateMutexW(None, False, name)
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        return None
    return handle


def make_popup_window(toplevel):
    """Pop-ups: topmost, never take focus, not in the taskbar or Alt+Tab. Returns the HWND."""
    hwnd = GetParent(toplevel.winfo_id())
    ex = GetWindowLongW(hwnd, GWL_EXSTYLE)
    ex |= WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST
    ex &= ~WS_EX_APPWINDOW
    SetWindowLongW(hwnd, GWL_EXSTYLE, ex)
    SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, Z_FLAGS | SWP_FRAMECHANGED)
    return hwnd


def _valid_rect(left, top, right, bottom):
    return right > left and bottom > top


def _primary_work_area():
    """Fallback: primary-monitor work area from SPI_GETWORKAREA, then the raw screen size."""
    r = wintypes.RECT()
    if SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0) and \
            _valid_rect(r.left, r.top, r.right, r.bottom):
        return r.left, r.top, r.right, r.bottom
    return 0, 0, max(1, GetSystemMetrics(SM_CXSCREEN)), max(1, GetSystemMetrics(SM_CYSCREEN))


def _monitor_info(hmon):
    """MONITORINFO for an HMONITOR, or None."""
    if not hmon:
        return None
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)       # required, or GetMonitorInfoW fails
    if not GetMonitorInfoW(hmon, ctypes.byref(info)):
        return None
    w = info.rcWork
    return info if _valid_rect(w.left, w.top, w.right, w.bottom) else None


def monitor_work_areas():
    """Work rectangles (left, top, right, bottom) of all attached monitors. Index 0 is the primary
    monitor, the rest follow sorted left-to-right, then top-to-bottom. Empty list on failure."""
    found = []

    def _cb(hmon, hdc, rect, lparam):
        try:
            info = _monitor_info(hmon)
            if info:
                w, m = info.rcWork, info.rcMonitor
                found.append((not (info.dwFlags & MONITORINFOF_PRIMARY), m.left, m.top,
                              (w.left, w.top, w.right, w.bottom)))
        except Exception:
            pass
        return True                                # keep enumerating

    try:
        EnumDisplayMonitors(None, None, MONITORENUMPROC(_cb), 0)
    except Exception:
        return []
    found.sort(key=lambda t: t[:3])
    return [t[3] for t in found]


def work_area(hwnd=None, point=None, index=None):
    """Work area (left, top, right, bottom) of one monitor, excluding the taskbar.
    Chosen by, in order: index (0 = primary, see monitor_work_areas), the monitor holding hwnd,
    the monitor holding point (x, y) (nearest monitor if the point is off-screen), else the primary.
    Any failure falls back to the primary work area from SPI_GETWORKAREA."""
    try:
        if index is not None:
            areas = monitor_work_areas()
            if 0 <= index < len(areas):
                return areas[index]
        if hwnd:
            hmon = MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        elif point is not None:
            hmon = MonitorFromPoint(wintypes.POINT(int(point[0]), int(point[1])), MONITOR_DEFAULTTONEAREST)
        else:
            hmon = MonitorFromPoint(wintypes.POINT(0, 0), MONITOR_DEFAULTTOPRIMARY)
        info = _monitor_info(hmon)
        if info:
            w = info.rcWork
            return w.left, w.top, w.right, w.bottom
    except Exception:
        pass
    return _primary_work_area()


def fit_in_rect(x, y, width, height, rect):
    """Move (x, y) so a width x height window lies inside rect; if it is bigger than rect, pin its
    top-left corner to the rect's top-left."""
    left, top, right, bottom = rect
    return max(left, min(x, right - width)), max(top, min(y, bottom - height))


# ----------------------------------------------------------------------------
# The widget window: desktop layer in normal mode, topmost + focusable in edit mode
# ----------------------------------------------------------------------------
class DesktopLayer:
    """Wraps a Tk root. Call attach() once the first layout size is known, then poll() every
    KEY_POLL_MS from the Tk loop. look: dict with anchor, margin_x, margin_y, pos_x, pos_y,
    opacity, background_mode (transparent | panel)."""

    def __init__(self, root, look: dict):
        self.root, self.look = root, look
        self.peeking = False          # True while Alt+W is held
        self.interactive = False      # True in edit mode: clicks + keyboard focus
        self.allow_geometry = True    # only our own code may move/resize
        self.raw_zorder = False       # bypass z-order rewriting (internal use)
        self.reposition_pending = False
        self.wake_pending = False     # lock/unlock, resume, remote-desktop reconnect
        self.pending_hotkeys = []
        self.hwnd = None
        self.width = self.height = 1
        self._poll_elapsed = 0
        self._old_wndproc = self._wndproc_ref = None
        self._registered = []
        self.hotkeys = HotkeyListener()
        self.click_through = bool(self.look.get("click_through", True))

    def _opacity(self) -> float:
        try:
            return max(0.0, min(1.0, float(self.look.get("opacity", 0.92))))
        except (TypeError, ValueError):
            return 0.92

    # ---------- start / stop ------------------------------------------------
    def attach(self, width: int, height: int, hook: bool = True) -> None:
        root = self.root
        root.overrideredirect(True)                 # no title bar / borders
        root.protocol("WM_DELETE_WINDOW", lambda: None)
        root.attributes("-alpha", self._opacity())
        self.width, self.height = width, height
        x, y = self._target_position()
        root.geometry(f"{width}x{height}+{x}+{y}")
        root.update_idletasks()
        self.hwnd = GetParent(root.winfo_id())      # Tk's real top-level HWND
        self._apply_window_styles()
        self._install_subclass()
        if WTSRegisterSessionNotification:
            WTSRegisterSessionNotification(self.hwnd, 0)   # this session only
        self.allow_geometry = False                 # locked from now on
        self.enforce_zorder()
        if hook:
            self.hotkeys.start()                    # Alt+W peek and Ctrl+Alt+Shift+Q, as in the clock

    def quit(self) -> None:
        self.hotkeys.stop()
        if not self.hwnd:
            return
        for hid in self._registered:
            UnregisterHotKey(self.hwnd, hid)
        self._registered = []
        if WTSUnRegisterSessionNotification:
            WTSUnRegisterSessionNotification(self.hwnd)
        if self._old_wndproc:
            SetWindowLongPtrW(self.hwnd, GWLP_WNDPROC, self._old_wndproc)
            self._old_wndproc = None                # safe to call quit() twice

    @property
    def quit_requested(self) -> bool:
        return self.hotkeys.quit_requested

    # ---------- new hotkeys: RegisterHotKey, never a hook --------------------
    def register_hotkeys(self, spec: dict) -> list:
        """spec: {id: (modifiers, 'W')}. Returns the ids that could not be registered."""
        failed = []
        for hid, (mods, key) in spec.items():
            name = str(key).upper()
            vk = VK_BY_NAME.get(name) or (ord(name) if len(name) == 1 else None)
            if not self.hwnd or vk is None:
                failed.append(hid)
            elif RegisterHotKey(self.hwnd, hid, mods | MOD_NOREPEAT, vk):
                self._registered.append(hid)
            else:
                failed.append(hid)
        return failed

    def pop_hotkeys(self) -> list:
        out, self.pending_hotkeys = self.pending_hotkeys, []
        return out

    # ---------- geometry -----------------------------------------------------
    def _saved_position(self):
        """pos_x / pos_y as ints (virtual-screen coordinates, may be negative), or None."""
        px, py = self.look.get("pos_x"), self.look.get("pos_y")
        if px is None or py is None:
            return None
        try:
            return int(px), int(py)
        except (TypeError, ValueError):
            return None

    def _monitor_index(self):
        """Optional look["monitor"]: "primary" or a 0-based index (0 = primary). None = automatic."""
        m = self.look.get("monitor")
        if m is None or isinstance(m, bool):
            return None
        if isinstance(m, str) and m.strip().lower() == "primary":
            return 0
        try:
            return int(m) if int(m) >= 0 else None
        except (TypeError, ValueError):
            return None

    def target_work_area(self):
        """Work area of the monitor this widget belongs on: the one holding pos_x/pos_y if set,
        else look["monitor"] if set, else the one the window is on now (primary before attach)."""
        try:
            pos = self._saved_position()
            if pos is not None:
                return work_area(point=pos)
            idx = self._monitor_index()
            if idx is not None:
                return work_area(index=idx)
            if self.hwnd:
                return work_area(hwnd=self.hwnd)
            return work_area()
        except TypeError:
            return work_area()

    def work_area_size(self):
        left, top, right, bottom = self.target_work_area()
        return right - left, bottom - top

    def _target_position(self):
        left, top, right, bottom = area = self.target_work_area()
        pos = self._saved_position()
        if pos is not None:                        # explicit position, kept inside that monitor
            return fit_in_rect(pos[0], pos[1], self.width, self.height, area)
        anchor = str(self.look.get("anchor", "top-right"))
        try:
            mx, my = int(self.look.get("margin_x", 24)), int(self.look.get("margin_y", 24))
        except (TypeError, ValueError):
            mx = my = 24
        x = right - self.width - mx if "right" in anchor else left + mx
        y = bottom - self.height - my if "bottom" in anchor else top + my
        return fit_in_rect(x, y, self.width, self.height, area)

    def fit(self, width: int, height: int) -> None:
        """Resize (rows added/removed, edit mode) keeping the anchor corner."""
        width, height = int(width), int(height)
        if (width, height) == (self.width, self.height):
            return
        if self.hwnd is None:                        # before attach(): just remember the size
            self.width, self.height = width, height
            return
        self.width, self.height = width, height
        x, y = self._target_position()
        self.allow_geometry = True
        try:
            self.root.geometry(f"{width}x{height}+{x}+{y}")
            self.root.update_idletasks()
        finally:
            self.allow_geometry = False

    def _reposition(self):
        """Re-anchor after resolution / DPI / taskbar changes."""
        x, y = self._target_position()
        self.allow_geometry = True
        try:
            SetWindowPos(self.hwnd, 0, x, y, self.width, self.height, SWP_NOZORDER | SWP_NOACTIVATE)
        finally:
            self.allow_geometry = False

    # ---------- window styles & subclass ------------------------------------
    def _apply_window_styles(self):
        ex = GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        was_layered = ex & WS_EX_LAYERED
        ex |= WS_EX_TOOLWINDOW                         # no taskbar / Alt+Tab
        ex &= ~WS_EX_APPWINDOW
        if self.interactive:
            ex &= ~(WS_EX_NOACTIVATE | WS_EX_TRANSPARENT)    # edit mode: clicks and focus
        else:
            ex |= WS_EX_NOACTIVATE                           # never focused
            if self.click_through:
                ex |= WS_EX_TRANSPARENT | WS_EX_LAYERED      # mouse passes through
            else:
                ex &= ~WS_EX_TRANSPARENT                     # don't keep a stale click-through flag
        SetWindowLongW(self.hwnd, GWL_EXSTYLE, ex)
        if not was_layered and (ex & WS_EX_LAYERED):
            # Tk drops WS_EX_LAYERED when alpha == 1.0 and no transparent colour;
            # a layered window needs attributes set or it is invisible.
            SetLayeredWindowAttributes(self.hwnd, 0, int(self._opacity() * 255), LWA_ALPHA)
        SetWindowPos(self.hwnd, 0, 0, 0, 0, 0,
                     SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)

    def _install_subclass(self):
        self._wndproc_ref = WNDPROC(self._wndproc)  # must stay referenced
        new_ptr = ctypes.cast(self._wndproc_ref, ctypes.c_void_p).value
        self._old_wndproc = SetWindowLongPtrW(self.hwnd, GWLP_WNDPROC, new_ptr)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_WINDOWPOSCHANGING:
                self._on_windowpos_changing(lparam)
            elif msg == WM_MOUSEACTIVATE:
                if not self.interactive:
                    return MA_NOACTIVATE             # clicking never activates/raises
            elif msg == WM_SYSCOMMAND and (wparam & 0xFFF0) in BLOCKED_SYSCOMMANDS:
                return 0                             # no move/size/min/max/close
            elif msg == WM_CLOSE:
                return 0                             # ignore close requests
            elif msg == WM_NCHITTEST:
                return HTCLIENT                      # no draggable/resizable edges
            elif msg == WM_HOTKEY:
                self.pending_hotkeys.append(int(wparam))
                return 0
            elif msg in (WM_DISPLAYCHANGE, WM_DPICHANGED) or (msg == WM_SETTINGCHANGE and wparam == SPI_SETWORKAREA):
                self.reposition_pending = True
            elif msg == WM_WTSSESSION_CHANGE and wparam in (WTS_SESSION_UNLOCK, WTS_REMOTE_CONNECT, WTS_CONSOLE_CONNECT):
                self.wake_pending = True
                self.reposition_pending = True
            elif msg == WM_POWERBROADCAST and wparam in (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
                self.wake_pending = True
                self.reposition_pending = True
        except Exception:
            pass
        return CallWindowProcW(self._old_wndproc, hwnd, msg, wparam, lparam)

    def _on_windowpos_changing(self, lparam):
        """Every move/resize/z-order change passes through here first."""
        wp = WINDOWPOS.from_address(lparam)
        if not self.allow_geometry:
            wp.flags |= SWP_NOMOVE | SWP_NOSIZE      # locked in place
            wp.flags &= ~SWP_HIDEWINDOW              # nothing may hide us
        if self.peeking or self.interactive or self.raw_zorder or (wp.flags & SWP_NOZORDER):
            return
        target = self._zorder_target()
        if target is None:
            wp.flags |= SWP_NOZORDER                 # already in the right spot
        else:
            wp.hwndInsertAfter = target              # force onto the desktop layer

    # ---------- z-order logic (from the clock) ----------------------------------
    def _zorder_target(self):
        """The 'insert after' value that puts us directly above the desktop window."""
        desk = find_desktop_window()
        if not desk:
            return None
        prev = GetWindow(desk, GW_HWNDPREV)          # window just above the desktop
        if prev == self.hwnd:
            return None
        desk_top = is_topmost(desk)
        me_top = is_topmost(self.hwnd)
        if not prev or (is_topmost(prev) and not desk_top):
            # Desktop is the highest non-topmost window (Show Desktop active).
            if desk_top:
                return HWND_TOPMOST
            return HWND_NOTOPMOST if me_top else HWND_TOP
        return prev

    def _zorder_ok(self, desk):
        if is_topmost(self.hwnd) != is_topmost(desk):
            return False
        w = GetWindow(desk, GW_HWNDPREV)
        for _ in range(500):                         # skip invisible windows
            if not w or w == self.hwnd or IsWindowVisible(w):
                break
            w = GetWindow(w, GW_HWNDPREV)
        return w == self.hwnd

    def enforce_zorder(self):
        if self.interactive:
            return
        if self.peeking:
            if not is_topmost(self.hwnd):
                SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, Z_FLAGS)
            return
        desk = find_desktop_window()
        if not desk or self._zorder_ok(desk):
            return
        # The subclass rewrites HWND_TOP into "directly above the desktop".
        SetWindowPos(self.hwnd, HWND_TOP, 0, 0, 0, 0, Z_FLAGS)
        if is_topmost(self.hwnd) and not is_topmost(desk):
            # Fallback: HWND_BOTTOM always clears the topmost flag.
            self.raw_zorder = True
            try:
                SetWindowPos(self.hwnd, HWND_BOTTOM, 0, 0, 0, 0, Z_FLAGS)
            finally:
                self.raw_zorder = False
            SetWindowPos(self.hwnd, HWND_TOP, 0, 0, 0, 0, Z_FLAGS)

    def set_peek(self, on):
        if self.interactive:
            return
        self.peeking = on
        if on:
            SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, Z_FLAGS)  # above everything
        else:
            self.enforce_zorder()                                        # back to desktop

    # ---------- edit mode -----------------------------------------------------------
    def set_interactive(self, on: bool):
        """Edit mode: accept clicks and keyboard focus, above other windows.
        Leaving returns the widget to the desktop layer."""
        if on == self.interactive:
            return
        if on and self.peeking:
            self.peeking = False
        self.interactive = on
        self._apply_window_styles()
        if on:
            SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, Z_FLAGS & ~SWP_NOACTIVATE)
            SetForegroundWindow(self.hwnd)
            self.root.focus_force()
        else:
            self.enforce_zorder()

    # ---------- periodic work -----------------------------------------------------------
    def poll(self):
        """Call every KEY_POLL_MS from the Tk loop."""
        if not self.interactive:
            want_peek = (self.hotkeys.peek_key_down and key_down(VK_MENU)
                         and not key_down(VK_CONTROL) and not key_down(VK_SHIFT))
            if want_peek != self.peeking:
                self.set_peek(want_peek)
        self._poll_elapsed += KEY_POLL_MS
        if self._poll_elapsed >= ZORDER_CHECK_MS:
            self._poll_elapsed = 0
            if self.reposition_pending:
                self.reposition_pending = False
                self._reposition()
            self.enforce_zorder()
