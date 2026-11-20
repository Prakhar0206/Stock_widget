"""desktop_layer.py is Windows-only. Here ctypes is stubbed so its message handling and z-order
locking LOGIC can run on any machine. This proves the code imports and branches correctly; it does
NOT prove Windows behaves as expected (that is the manual Windows checklist)."""
import ctypes
import importlib
import sys
from unittest import mock

import pytest


@pytest.fixture
def dl(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: mock.MagicMock(), raising=False)
    monkeypatch.setattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 0, raising=False)
    sys.modules.pop("desktop_layer", None)
    mod = importlib.import_module("desktop_layer")
    yield mod
    sys.modules.pop("desktop_layer", None)


class Root:
    def __init__(self):
        self.geo = []
        self.withdrawn = 0
        self.deiconified = 0
        self.attrs = []

    def geometry(self, g): self.geo.append(g)
    def update_idletasks(self): pass
    def withdraw(self): self.withdrawn += 1
    def deiconify(self): self.deiconified += 1
    def attributes(self, *a): self.attrs.append(a)
    def focus_force(self): pass


def make(dl, look=None):
    root = Root()
    layer = dl.DesktopLayer(root, look or {"anchor": "top-right", "margin_x": 24, "margin_y": 24})
    layer.hwnd = 1234
    layer._old_wndproc = 1
    return layer, root


def test_import_and_new_constants(dl):
    assert dl.WM_HOTKEY == 0x0312 and dl.MOD_ALT | dl.MOD_SHIFT == 5 and dl.PEEK_KEY == "W" and dl.QUIT_KEY == "Q"


def test_anchor_positions_use_primary_work_area(dl, monkeypatch):
    monkeypatch.setattr(dl, "work_area", lambda: (0, 0, 1920, 1040))
    layer, root = make(dl)
    layer.width, layer.height = 500, 200
    assert layer._target_position() == (1920 - 500 - 24, 24)
    layer.look["anchor"] = "bottom-left"
    assert layer._target_position() == (24, 1040 - 200 - 24)
    layer.look.update(pos_x=10, pos_y=20)
    assert layer._target_position() == (10, 20)


def test_fit_before_attach_only_remembers_and_after_attach_allows_geometry(dl, monkeypatch):
    monkeypatch.setattr(dl, "work_area", lambda: (0, 0, 1920, 1040))
    layer, root = make(dl)
    layer.hwnd = None
    layer.fit(300, 100)
    assert (layer.width, layer.height) == (300, 100) and root.geo == []
    layer.hwnd = 5
    layer.allow_geometry = False
    seen = []
    root.geometry = lambda g: seen.append((g, layer.allow_geometry))      # geometry must be allowed during fit
    layer.fit(400, 150)
    assert seen == [("400x150+1496+24", True)] and layer.allow_geometry is False


def test_wndproc_hotkey_wake_and_display_messages(dl, monkeypatch):
    monkeypatch.setattr(dl, "CallWindowProcW", lambda *a: 99)
    layer, _ = make(dl)
    assert layer._wndproc(1, dl.WM_HOTKEY, 1, 0) == 0 and layer.pop_hotkeys() == [1] and layer.pop_hotkeys() == []
    layer._wndproc(1, dl.WM_WTSSESSION_CHANGE, dl.WTS_SESSION_UNLOCK, 0)
    assert layer.wake_pending and layer.reposition_pending
    layer.wake_pending = layer.reposition_pending = False
    layer._wndproc(1, dl.WM_POWERBROADCAST, dl.PBT_APMRESUMEAUTOMATIC, 0)
    assert layer.wake_pending
    layer.wake_pending = layer.reposition_pending = False
    layer._wndproc(1, dl.WM_WTSSESSION_CHANGE, 7, 0)                      # session lock itself: nothing to do
    assert not layer.wake_pending
    layer._wndproc(1, dl.WM_DISPLAYCHANGE, 0, 0)
    assert layer.reposition_pending
    assert layer._wndproc(1, dl.WM_CLOSE, 0, 0) == 0                      # close requests are ignored
    assert layer._wndproc(1, dl.WM_SYSCOMMAND, dl.SC_MOVE, 0) == 0
    assert layer._wndproc(1, dl.WM_NCHITTEST, 0, 0) == dl.HTCLIENT


def test_mouse_never_activates_in_normal_mode_but_may_in_edit_mode(dl, monkeypatch):
    monkeypatch.setattr(dl, "CallWindowProcW", lambda *a: 99)
    layer, _ = make(dl)
    assert layer._wndproc(1, dl.WM_MOUSEACTIVATE, 0, 0) == dl.MA_NOACTIVATE
    layer.interactive = True
    assert layer._wndproc(1, dl.WM_MOUSEACTIVATE, 0, 0) == 99


def _wp(dl, flags=0):
    wp = dl.WINDOWPOS(hwnd=1234, hwndInsertAfter=0, x=1, y=2, cx=3, cy=4, flags=flags)
    return wp, ctypes.addressof(wp)


def test_geometry_is_locked_and_z_order_rewritten_to_desktop_layer(dl, monkeypatch):
    layer, _ = make(dl)
    monkeypatch.setattr(layer, "_zorder_target", lambda: 777)
    wp, addr = _wp(dl)
    layer.allow_geometry = False
    layer._on_windowpos_changing(addr)
    assert wp.flags & dl.SWP_NOMOVE and wp.flags & dl.SWP_NOSIZE and wp.hwndInsertAfter == 777
    wp, addr = _wp(dl, dl.SWP_HIDEWINDOW)
    layer._on_windowpos_changing(addr)
    assert not wp.flags & dl.SWP_HIDEWINDOW                               # nothing may hide the widget
    wp, addr = _wp(dl)
    layer.allow_geometry = True
    layer._on_windowpos_changing(addr)
    assert not wp.flags & dl.SWP_NOMOVE                                   # our own fit() may move/resize


def test_edit_mode_and_peek_bypass_z_order_rewriting(dl, monkeypatch):
    layer, _ = make(dl)
    monkeypatch.setattr(layer, "_zorder_target", lambda: 777)
    for attr in ("interactive", "peeking", "raw_zorder"):
        setattr(layer, attr, True)
        wp, addr = _wp(dl)
        layer._on_windowpos_changing(addr)
        assert wp.hwndInsertAfter == 0, attr
        setattr(layer, attr, False)


def test_set_interactive_toggles_styles_and_returns_to_desktop_layer(dl, monkeypatch):
    layer, _ = make(dl)
    calls = []
    monkeypatch.setattr(layer, "_apply_window_styles", lambda: calls.append(("styles", layer.interactive)))
    monkeypatch.setattr(layer, "enforce_zorder", lambda: calls.append("enforce"))
    monkeypatch.setattr(dl, "SetWindowPos", lambda *a: calls.append(("pos", a[1])))
    monkeypatch.setattr(dl, "SetForegroundWindow", lambda h: calls.append("fg"))
    layer.root.focus_force = lambda: calls.append("focus")
    layer.set_interactive(True)
    assert ("styles", True) in calls and ("pos", dl.HWND_TOPMOST) in calls and "fg" in calls and "focus" in calls
    calls.clear()
    layer.set_interactive(False)
    assert calls == [("styles", False), "enforce"]                        # leaving: back to the desktop layer
    calls.clear(); layer.set_interactive(False); assert calls == []


def test_register_hotkeys_reports_failures_and_uses_norepeat(dl, monkeypatch):
    layer, _ = make(dl)
    got = []
    monkeypatch.setattr(dl, "RegisterHotKey", lambda h, i, m, vk: got.append((i, m, vk)) or i != 2)
    failed = layer.register_hotkeys({1: (dl.MOD_ALT | dl.MOD_SHIFT, "W"), 2: (dl.MOD_ALT, "A"),
                                     4: (dl.MOD_ALT | dl.MOD_SHIFT, "H")})
    assert failed == [2]
    assert got[0] == (1, dl.MOD_ALT | dl.MOD_SHIFT | dl.MOD_NOREPEAT, 0x57)
    assert dl.VK_BY_NAME["H"] == 0x48
    assert got[2] == (4, dl.MOD_ALT | dl.MOD_SHIFT | dl.MOD_NOREPEAT, 0x48)


def test_privacy_hide_and_restore_preserves_window_state(dl, monkeypatch):
    layer, root = make(dl)
    events = []
    swp_calls = []
    desk_calls = []
    reposition_calls = []
    peek_events = []
    monkeypatch.setattr(dl, "SetWindowPos", lambda *a: (swp_calls.append(a), events.append(("swp", a[1], a[6]))))
    monkeypatch.setattr(dl, "work_area", lambda: (0, 0, 1920, 1040))
    monkeypatch.setattr(dl, "find_desktop_window", lambda: (desk_calls.append(1), 999)[1])
    monkeypatch.setattr(dl, "is_topmost", lambda h: False)
    monkeypatch.setattr(layer, "_zorder_ok", lambda desk: False)
    monkeypatch.setattr(layer, "_apply_window_styles", lambda: None)
    monkeypatch.setattr(layer, "_zorder_target", lambda: 777)
    monkeypatch.setattr(layer, "_reposition", lambda: reposition_calls.append(1))
    orig_deiconify = root.deiconify
    monkeypatch.setattr(root, "deiconify", lambda: (orig_deiconify(), events.append(("deiconify",))))
    layer.on_peek_change = lambda on: peek_events.append(on)

    layer.width, layer.height = 400, 150
    layer.peeking = True
    layer.interactive = True
    layer.set_hidden(True)
    assert layer.hidden is True
    assert layer.peeking is False
    assert layer.interactive is False
    assert root.withdrawn == 1
    assert any(c[6] & dl.SWP_HIDEWINDOW for c in swp_calls)

    # While hidden & not peeking, _on_windowpos_changing strips SWP_SHOWWINDOW, and enforce/poll do nothing
    swp_calls.clear()
    desk_calls.clear()
    wp, addr = _wp(dl, dl.SWP_SHOWWINDOW)
    layer._on_windowpos_changing(addr)
    assert not (wp.flags & dl.SWP_SHOWWINDOW)
    layer.fit(520, 210)
    assert (layer.width, layer.height) == (520, 210)
    assert layer.reposition_pending is True
    layer.enforce_zorder()
    assert desk_calls == [] and swp_calls == []
    monkeypatch.setattr(dl, "key_down", lambda vk: False)
    layer._poll_elapsed = dl.ZORDER_CHECK_MS
    layer.poll()
    assert reposition_calls == [] and desk_calls == []

    # Holding Alt+W (peek) while hidden temporarily shows HWND_TOPMOST and hides again on release
    swp_calls.clear()
    events.clear()
    layer.hotkeys.peek_key_down = True
    monkeypatch.setattr(dl, "key_down", lambda vk: vk == dl.VK_MENU)
    layer._poll_elapsed = 0
    layer.poll()
    assert layer.hidden is True and layer.peeking is True
    assert peek_events == [True]
    assert any(c[1] == dl.HWND_TOPMOST and (c[6] & dl.SWP_SHOWWINDOW) for c in swp_calls)
    wp2, addr2 = _wp(dl, dl.SWP_SHOWWINDOW)
    layer._on_windowpos_changing(addr2)
    assert wp2.flags & dl.SWP_SHOWWINDOW
    layer.fit(540, 220)
    assert (layer.width, layer.height) == (540, 220)
    layer.enforce_zorder()
    assert any(c[1] == dl.HWND_TOPMOST for c in swp_calls)

    # Releasing Alt+W immediately hides the widget again while keeping layer.hidden == True
    swp_calls.clear()
    layer.hotkeys.peek_key_down = False
    monkeypatch.setattr(dl, "key_down", lambda vk: False)
    layer.poll()
    assert layer.hidden is True and layer.peeking is False
    assert peek_events == [True, False]
    assert root.withdrawn == 2
    assert any(c[6] & dl.SWP_HIDEWINDOW for c in swp_calls)

    # Unhiding positions onto target z-order (777) BEFORE deiconify (no flash on top of other windows)
    swp_calls.clear()
    events.clear()
    desk_calls.clear()
    layer.set_hidden(False)
    assert layer.hidden is False and layer.peeking is False
    assert layer.reposition_pending is False
    assert root.deiconified == 2
    show_idx = next(i for i, e in enumerate(events) if e[0] == "swp" and e[1] == 777 and (e[2] & dl.SWP_SHOWWINDOW))
    deic_idx = next(i for i, e in enumerate(events) if e[0] == "deiconify")
    assert show_idx < deic_idx
    assert len(desk_calls) >= 1

    # Entering interactive mode while hidden automatically unhides first
    layer.set_hidden(True)
    assert layer.hidden is True
    layer.set_interactive(True)
    assert layer.hidden is False
    assert layer.interactive is True
