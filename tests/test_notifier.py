from dataclasses import replace
from datetime import datetime, timezone

from config import Config
from engine import Notification
from models import AlertEvent
from decimal import Decimal as Dc
from notifier import Notifier, NullBackends

T = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)       # 20:30 IST


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

    def toast(self, text, stay, silent): self.log.append(("toast", stay))
    def beep(self, kind): self.log.append(("beep", kind))
    def popup_show(self, text, priority, seconds, on_ack): self.log.append(("popup", seconds))
    def popup_close(self): self.log.append(("close",))
    def flash(self, symbol, ms): self.log.append(("flash", symbol, ms))

    def count(self, name): return sum(1 for x in self.log if x[0] == name)


def ev(prio, sym="AAPL"):
    return AlertEvent(sym, "a", "level", "SELL", prio, Dc("207.50"), T, "mock", "SELL", "at or above 207.50",
                      pct_vs_base=Dc("3.02"), base_name="breakeven", net_estimate=Dc("60.72"), data_age_s=4)


def make(cfg=None):
    b, t = Rec(), Timers()
    return Notifier(cfg or Config(), b, t, lambda: T), b, t


def test_low_only_flashes():
    n, b, t = make()
    n.notify(Notification([ev("LOW")]))
    assert b.log == [("flash", "AAPL", 2000)]


def test_medium_toast_and_short_sound():
    n, b, t = make()
    n.notify(Notification([ev("MEDIUM")]))
    assert b.log == [("flash", "AAPL", 2000), ("toast", False), ("beep", "medium")]


def test_high_popup_repeating_sound_for_10_seconds_then_closes_toast_stays():
    n, b, t = make()
    n.notify(Notification([ev("HIGH")]))
    assert ("toast", True) in b.log and ("popup", 10) in b.log
    t.advance(9_500)
    beeps = b.count("beep")
    assert 9 <= beeps <= 11
    t.advance(1_000)                                          # past 10 s
    assert b.count("close") == 1
    after = b.count("beep"); t.advance(5_000)
    assert b.count("beep") == after                           # sound stopped


def test_high_escalates_once_after_2_minutes_if_not_acknowledged():
    n, b, t = make()
    n.notify(Notification([ev("HIGH")]))
    t.advance(119_000); assert b.count("popup") == 1
    t.advance(2_000);   assert b.count("popup") == 2          # repeated once
    t.advance(600_000); assert b.count("popup") == 2          # then stops


def test_acknowledge_stops_repeat_and_sound():
    n, b, t = make()
    n.notify(Notification([ev("HIGH")]))
    t.advance(3_000)
    assert n.acknowledge_latest() is True
    beeps = b.count("beep")
    t.advance(600_000)
    assert b.count("popup") == 1 and b.count("beep") == beeps
    assert n.acknowledge_latest() is False


def test_new_high_alert_gets_its_own_escalation():
    n, b, t = make()
    n.notify(Notification([ev("HIGH", "AAPL")]))
    t.advance(60_000)
    n.notify(Notification([ev("HIGH", "TTWO")]))
    t.advance(121_000)
    assert b.count("popup") == 3                              # AAPL, TTWO, TTWO again


def test_mute_silences_sound_and_toast_but_high_popup_still_appears_silently():
    n, b, t = make()
    n.toggle_mute()
    n.notify(Notification([ev("HIGH")]))
    assert ("flash", "AAPL", 2000) in b.log                  # rows keep updating
    assert b.count("toast") == 0 and b.count("beep") == 0    # no toast, no sound
    assert ("popup", 10) in b.log                            # but the pop-up is shown
    t.advance(11_000)
    assert b.count("close") == 1 and b.count("beep") == 0    # and closes on its normal 10 s timer, silently


def test_mute_medium_alert_flashes_only():
    n, b, t = make()
    n.toggle_mute()
    n.notify(Notification([ev("MEDIUM")]))
    assert b.log == [("flash", "AAPL", 2000)]                # no toast, no beep, no pop-up for MEDIUM


def test_muted_high_alert_still_escalates_once_silently():
    n, b, t = make()
    n.toggle_mute()
    n.notify(Notification([ev("HIGH")]))
    t.advance(125_000)
    assert b.count("popup") == 2 and b.count("beep") == 0 and b.count("toast") == 0
    t.advance(600_000)
    assert b.count("popup") == 2


def test_mute_mid_alert_stops_sound_but_keeps_the_popup_up():
    n, b, t = make()
    n.notify(Notification([ev("HIGH")]))
    t.advance(2_000); n.toggle_mute()
    beeps = b.count("beep"); t.advance(5_000)
    assert b.count("beep") == beeps                          # sound stopped
    assert b.count("close") == 0                             # pop-up still showing at 7 s
    t.advance(4_000)
    assert b.count("close") == 1                             # closes at 10 s as usual


def test_unmute_restores_toast_and_sound_for_the_next_alert():
    n, b, t = make()
    n.toggle_mute(); n.toggle_mute()
    n.notify(Notification([ev("MEDIUM")]))
    assert ("toast", False) in b.log and ("beep", "medium") in b.log


def test_acknowledge_closes_a_muted_popup_too():
    n, b, t = make()
    n.toggle_mute()
    n.notify(Notification([ev("HIGH")]))
    assert n.acknowledge_latest() is True and b.count("close") == 1
    t.advance(300_000)
    assert b.count("popup") == 1                             # no escalation after Got it


def test_windows_toast_runs_on_a_background_thread_and_never_blocks_the_caller():
    import threading
    from notifier import WindowsBackends
    wb = WindowsBackends()
    wb._mode = "wt"                                          # pretend a toast library is available
    started, release, seen = threading.Event(), threading.Event(), {}

    def slow_toast(text, stay):
        seen["thread"] = threading.current_thread()
        seen["args"] = (text, stay)
        started.set()
        release.wait(2)
    wb._toast_blocking = slow_toast
    wb.toast("line1\nline2", True, True)                    # returns immediately although the toast is "slow"
    assert started.wait(2)
    assert seen["thread"] is not threading.main_thread() and seen["thread"].daemon
    assert seen["args"] == ("line1\nline2", True)
    release.set()


def test_windows_toast_disabled_without_a_library_and_errors_are_swallowed(monkeypatch):
    from notifier import WindowsBackends
    wb = WindowsBackends()
    wb._mode = None
    wb.toast("x", False, True)                               # no library: silently does nothing
    wb._mode = "wt"                                          # library "present" but the real call fails here
    wb._toast_blocking("x", False)                           # must not raise (windows_toasts is not installed on Linux)


def test_quiet_hours_popup_appears_without_sound():
    cfg = replace(Config(), quiet_hours_ist=(datetime.strptime("20:00", "%H:%M").time(), datetime.strptime("21:00", "%H:%M").time()))
    n, b, t = make(cfg)
    n.notify(Notification([ev("HIGH")]))
    t.advance(3_000)
    assert b.count("popup") == 1 and b.count("beep") == 0
    m = make(cfg)[0]; 
    m.b.log.clear(); m.notify(Notification([ev("MEDIUM")]))
    assert m.b.count("beep") == 0 and m.b.count("toast") == 1


def test_merged_notification_uses_highest_priority_and_text():
    from messages import format_notification
    note = Notification([ev("LOW", "AAPL"), ev("HIGH", "TTWO"), ev("MEDIUM", "MSFT")])
    assert note.priority == "HIGH"
    text = format_notification(note, T)
    assert text.splitlines()[0].startswith("[HIGH] TTWO") and "Also [MEDIUM] MSFT" in text and "Also [LOW] AAPL" in text
    n, b, t = make(); n.notify(note)
    assert b.count("flash") == 3
