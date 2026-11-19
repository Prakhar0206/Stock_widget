from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as Dc

from config import Config
from engine import Notification
from models import AlertEvent, CustomNotification
from notifier import Notifier, NullBackends

T = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)  # 20:30 IST


class Timers:
    def __init__(self):
        self.t, self.q, self.n = 0, {}, 0

    def schedule(self, ms, fn):
        self.n += 1
        self.q[self.n] = (self.t + ms, fn)
        return self.n

    def cancel(self, h):
        self.q.pop(h, None)

    def advance(self, ms):
        end = self.t + ms
        while True:
            due = sorted((v[0], k) for k, v in self.q.items() if v[0] <= end)
            if not due:
                break
            when, k = due[0]
            self.t = when
            _, fn = self.q.pop(k)
            fn()
        self.t = end


class Rec(NullBackends):
    def __init__(self):
        self.log = []

    def toast(self, text, stay, silent):
        self.log.append(("toast", stay, silent))

    def beep(self, kind):
        self.log.append(("beep", kind))

    def popup_show(self, text, priority, seconds, on_ack):
        self.log.append(("popup", priority, seconds))

    def popup_close(self):
        self.log.append(("close",))

    def flash(self, symbol, ms):
        self.log.append(("flash", symbol, ms))

    def count(self, name):
        return sum(1 for x in self.log if x[0] == name)


def ev(prio, sym="AAPL", custom=None):
    return AlertEvent(
        sym,
        "a",
        "crossing_up",
        "SELL",
        prio,
        Dc("207.50"),
        T,
        "mock",
        "SELL",
        "Crossing Up $207.50",
        session="REGULAR",
        data_age_s=4,
        notification=custom,
    )


def make(cfg=None):
    b, t = Rec(), Timers()
    return Notifier(cfg or Config(), b, t, lambda: T), b, t


def test_low_flashes_toasts_and_plays_single_chime_without_popup():
    n, b, t = make()
    n.notify(Notification([ev("LOW")]))
    assert ("flash", "AAPL", 2000) in b.log
    assert any(x[0] == "toast" and x[1] is False for x in b.log)
    assert b.count("beep") == 1
    assert b.count("popup") == 0


def test_medium_shows_popup_toast_and_5s_sound():
    n, b, t = make()
    n.notify(Notification([ev("MEDIUM")]))
    assert ("flash", "AAPL", 2000) in b.log
    assert any(x[0] == "toast" and x[1] is False for x in b.log)
    assert ("popup", "MEDIUM", 10) in b.log
    t.advance(4_500)
    assert b.count("beep") >= 4
    t.advance(2_000)  # t = 6.5s -> sound stopped, popup still visible
    beeps_at_6s5 = b.count("beep")
    assert b.count("close") == 0
    t.advance(3_000)  # t = 9.5s -> still no extra beeps, popup still visible
    assert b.count("beep") == beeps_at_6s5
    assert b.count("close") == 0
    t.advance(1_000)  # t = 10.5s -> closes at 10s
    assert b.count("close") == 1


def test_high_plays_10s_alarm_persistent_toast_and_repeats_until_acknowledged():
    n, b, t = make()
    n.notify(Notification([ev("HIGH")]))
    assert any(x[0] == "toast" and x[1] is True for x in b.log)
    assert ("popup", "HIGH", 10) in b.log
    t.advance(9_500)
    beeps = b.count("beep")
    assert 7 <= beeps <= 12
    t.advance(1_000)
    assert b.count("close") == 1
    after = b.count("beep")
    t.advance(5_000)
    assert b.count("beep") == after

    # Repeats every 2 minutes continuously until acknowledged
    t.advance(120_000)  # t = 135.5s -> popup 2 already opened at 120s and closed at 130s
    assert b.count("popup") == 2
    t.advance(105_000)  # t = 240.5s -> popup 3 just opened at 240s and is still active
    assert b.count("popup") == 3
    assert n.acknowledge_latest() is True
    assert b.count("close") == 3
    t.advance(300_000)
    assert b.count("popup") == 3


def test_acknowledge_stops_repeat_and_sound():
    n, b, t = make()
    n.notify(Notification([ev("HIGH")]))
    t.advance(3_000)
    assert n.acknowledge_latest() is True
    beeps = b.count("beep")
    t.advance(600_000)
    assert b.count("popup") == 1 and b.count("beep") == beeps
    assert n.acknowledge_latest() is False


def test_custom_notification_profile_flags():
    # Toast + no popup + bell for 0s (once) + no repeat
    spec = CustomNotification(popup=False, toast=True, sound_tone="bell", sound_seconds=0, repeat_until_ack=False)
    n, b, t = make()
    n.notify(Notification([ev("CUSTOM", custom=spec)]))
    assert b.count("popup") == 0
    assert b.count("toast") == 1
    assert ("beep", "bell") in b.log
    t.advance(10_000)
    assert b.count("beep") == 1

    # Silent custom notification with popup only
    spec2 = CustomNotification(popup=True, toast=False, sound_tone="none", sound_seconds=5, repeat_until_ack=False)
    n2, b2, t2 = make()
    n2.notify(Notification([ev("CUSTOM", custom=spec2)]))
    assert b2.count("popup") == 1
    assert b2.count("toast") == 0
    assert b2.count("beep") == 0


def test_mute_silences_sound_and_toast_but_high_popup_still_appears_silently():
    n, b, t = make()
    n.toggle_mute()
    n.notify(Notification([ev("HIGH")]))
    assert ("flash", "AAPL", 2000) in b.log
    assert b.count("toast") == 0 and b.count("beep") == 0
    assert ("popup", "HIGH", 10) in b.log
    t.advance(11_000)
    assert b.count("close") == 1 and b.count("beep") == 0


def test_mute_low_alert_flashes_only():
    n, b, t = make()
    n.toggle_mute()
    n.notify(Notification([ev("LOW")]))
    assert b.log == [("flash", "AAPL", 2000)]


def test_quiet_hours_popup_appears_without_sound():
    cfg = replace(
        Config(),
        quiet_hours_ist=(
            datetime.strptime("20:00", "%H:%M").time(),
            datetime.strptime("21:00", "%H:%M").time(),
        ),
    )
    n, b, t = make(cfg)
    n.notify(Notification([ev("HIGH")]))
    t.advance(3_000)
    assert b.count("popup") == 1 and b.count("beep") == 0
    m, b2, _ = make(cfg)
    m.notify(Notification([ev("MEDIUM")]))
    assert b2.count("beep") == 0 and b2.count("toast") == 1


def test_windows_toast_runs_on_a_background_thread_and_never_blocks_the_caller():
    import threading
    from notifier import WindowsBackends

    wb = WindowsBackends()
    wb._mode = "wt"
    started, release, seen = threading.Event(), threading.Event(), {}

    def slow_toast(text, stay):
        seen["thread"] = threading.current_thread()
        seen["args"] = (text, stay)
        started.set()
        release.wait(2)

    wb._toast_blocking = slow_toast
    wb.toast("line1\nline2", True, True)
    assert started.wait(2)
    assert seen["thread"] is not threading.main_thread() and seen["thread"].daemon
    assert seen["args"] == ("line1\nline2", True)
    release.set()


def test_windows_toast_disabled_without_a_library_and_errors_are_swallowed(monkeypatch):
    import sys
    import types
    from notifier import WindowsBackends

    monkeypatch.setitem(sys.modules, "windows_toasts", None)
    monkeypatch.setitem(sys.modules, "winotify", None)
    wb = WindowsBackends()
    assert wb._mode is None
    wb.toast("x", False, True)

    wt_mod = types.ModuleType("windows_toasts")
    wt_audio_mod = types.ModuleType("windows_toasts.toast_audio")

    class _FakeToaster:
        def __init__(self, _name):
            pass

        def show_toast(self, _toast):
            raise RuntimeError("boom")

    wt_mod.Toast = lambda *a, **kw: ("toast", a, kw)
    wt_mod.ToastScenario = type("ToastScenario", (), {"Alarm": "alarm"})
    wt_mod.WindowsToaster = _FakeToaster
    wt_audio_mod.ToastAudio = lambda silent=True: ("audio", silent)
    monkeypatch.setitem(sys.modules, "windows_toasts", wt_mod)
    monkeypatch.setitem(sys.modules, "windows_toasts.toast_audio", wt_audio_mod)

    wb._mode = "wt"
    wb._toaster = None
    wb._toast_blocking("x", False)

    wn_mod = types.ModuleType("winotify")

    class _FakeNotification:
        def __init__(self, *a, **kw):
            pass

        def show(self):
            raise RuntimeError("wn boom")

    wn_mod.Notification = _FakeNotification
    monkeypatch.setitem(sys.modules, "winotify", wn_mod)
    wb._mode = "wn"
    wb._toast_blocking("x", True)


def test_merged_notification_uses_highest_priority_and_text():
    from messages import format_notification

    note = Notification([ev("LOW", "AAPL"), ev("HIGH", "TTWO"), ev("MEDIUM", "MSFT")])
    assert note.priority == "HIGH"
    text = format_notification(note, T)
    assert text.splitlines()[0].startswith("[HIGH] TTWO")
    assert "Also [MEDIUM] MSFT" in text and "Also [LOW] AAPL" in text
    n, b, t = make()
    n.notify(note)
    assert b.count("flash") == 3
