"""Delivery rules (blueprint section 7). Backends and timers are injected, so the rules are testable
without Windows. Mute silences sounds and toasts; a HIGH pop-up still appears (silently), and rows
and alert log keep updating. Quiet hours (IST): pop-ups still appear but make no sound."""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Callable, Optional

from config import Config, in_quiet_hours
from messages import format_notification

log = logging.getLogger("saw.notifier")
FLASH_MS = 2000
BEEP_EVERY_MS = 1000


class NullBackends:
    """Used on non-Windows machines and in tests."""
    def toast(self, text: str, stay: bool, silent: bool): ...
    def beep(self, kind: str): ...
    def popup_show(self, text: str, priority: str, seconds: int, on_ack: Callable): ...
    def popup_close(self): ...
    def flash(self, symbol: str, ms: int): ...


class WindowsBackends(NullBackends):
    """Toast via windows-toasts (winotify as fallback), sound via winsound. Every call is guarded:
    a broken backend must never stop alert evaluation. Toasts are sent from a background thread
    because winotify can block for about a second, which would freeze the UI and delay alerts."""

    def __init__(self, popup=None, flash_cb=None):
        self._popup, self._flash = popup, flash_cb
        self._toaster = None
        self._lock = threading.Lock()        # one toast at a time
        self._mode = None
        try:
            import windows_toasts  # noqa: F401
            self._mode = "wt"
        except Exception:
            try:
                import winotify  # noqa: F401
                self._mode = "wn"
            except Exception:
                log.warning("no toast library available; toasts disabled")

    def toast(self, text, stay, silent):
        if self._mode is None:
            return
        threading.Thread(target=self._toast_blocking, args=(text, stay), daemon=True, name="toast").start()

    def _toast_blocking(self, text, stay):
        with self._lock:
            try:
                if self._mode == "wt":
                    from windows_toasts import Toast, ToastScenario, WindowsToaster
                    from windows_toasts.toast_audio import ToastAudio
                    if self._toaster is None:
                        self._toaster = WindowsToaster("Stock Alert Widget")
                    lines = text.split("\n")
                    fields = lines if len(lines) <= 3 else lines[:2] + ["\n".join(lines[2:])]
                    kw = {"audio": ToastAudio(silent=True)}         # sound is played by us, not the toast
                    if stay:
                        kw["scenario"] = ToastScenario.Alarm        # stays until dismissed
                    self._toaster.show_toast(Toast(fields, **kw))
                elif self._mode == "wn":
                    from winotify import Notification
                    lines = text.split("\n")
                    Notification("Stock Alert Widget", lines[0], "\n".join(lines[1:]),
                                 duration="long" if stay else "short").show()
            except Exception as exc:
                log.warning("toast failed: %s", type(exc).__name__)

    def beep(self, kind):
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONHAND if kind == "high" else winsound.MB_ICONASTERISK)
        except Exception as exc:
            log.warning("sound failed: %s", type(exc).__name__)

    def popup_show(self, text, priority, seconds, on_ack):
        if self._popup:
            self._popup.show(text, priority, seconds, on_ack)

    def popup_close(self):
        if self._popup:
            self._popup.close()

    def flash(self, symbol, ms):
        if self._flash:
            self._flash(symbol, ms)


class Notifier:
    def __init__(self, cfg: Config, backends, timers, now_fn: Callable[[], datetime]):
        """timers: object with schedule(ms, fn) -> handle and cancel(handle)."""
        self.cfg, self.b, self.t, self.now_fn = cfg, backends, timers, now_fn
        self.muted = False
        self._latest: Optional[dict] = None      # the most recent alert, for Alt+A
        self._beep_h = self._end_h = self._esc_h = None

    # ---- public ----------------------------------------------------------------------------
    def toggle_mute(self) -> bool:
        """Muting stops sound at once. A pop-up that is already showing stays (and still closes on
        its normal timer); it is just silent."""
        self.muted = not self.muted
        if self.muted:
            self._stop_beep()
        return self.muted

    def notify(self, note) -> None:
        """`note` is an engine Notification (events already ordered by priority)."""
        now = self.now_fn()
        for ev in note.events:                       # every symbol's row flashes for 2 s
            self.b.flash(ev.symbol, FLASH_MS)
        prio = note.priority
        if prio == "LOW":
            return
        text = format_notification(note, now)
        self._latest = {"text": text, "priority": prio, "acked": False, "escalated": False}
        quiet = self.muted or in_quiet_hours(self.cfg, now) or not self.cfg.sound
        if self.cfg.toast and not self.muted:
            self.b.toast(text, stay=(prio == "HIGH"), silent=True)
        if prio == "MEDIUM":
            if not quiet:
                self.b.beep("medium")
            return
        self._show_high(self._latest, quiet)

    def acknowledge_latest(self) -> bool:
        cur = self._latest
        if not cur or cur["acked"]:
            return False
        cur["acked"] = True
        self._stop_sound()
        self.b.popup_close()
        if self._esc_h is not None:
            self.t.cancel(self._esc_h)
            self._esc_h = None
        return True

    # ---- HIGH ------------------------------------------------------------------------------
    def _show_high(self, cur, quiet: bool):
        self._stop_sound()
        self.b.popup_show(cur["text"], "HIGH", self.cfg.pop_seconds, self.acknowledge_latest)
        if not quiet:
            self._beep_loop("high")
        self._end_h = self.t.schedule(self.cfg.pop_seconds * 1000, self._end_popup)
        if not cur["escalated"]:                     # a newer HIGH alert replaces the older one's timer
            if self._esc_h is not None:
                self.t.cancel(self._esc_h)
            self._esc_h = self.t.schedule(self.cfg.escalation_seconds * 1000, lambda: self._escalate(cur))

    def _beep_loop(self, kind):
        if self.muted:
            return
        self.b.beep(kind)
        self._beep_h = self.t.schedule(BEEP_EVERY_MS, lambda: self._beep_loop(kind))

    def _end_popup(self):
        self._end_h = None
        self._stop_sound()
        self.b.popup_close()                         # the toast stays; the widget is back on the desktop

    def _stop_beep(self):
        if self._beep_h is not None:
            self.t.cancel(self._beep_h)
            self._beep_h = None

    def _stop_sound(self):
        for name in ("_beep_h", "_end_h"):
            h = getattr(self, name)
            if h is not None:
                self.t.cancel(h)
                setattr(self, name, None)

    def _escalate(self, cur):
        """Not acknowledged: the pop-up repeats once after 2 minutes, then stops."""
        self._esc_h = None
        if cur["acked"]:
            return
        cur["escalated"] = True                      # repeats once, even while muted (silently)
        self._show_high(cur, self.muted or in_quiet_hours(self.cfg, self.now_fn()) or not self.cfg.sound)
