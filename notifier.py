"""Delivery rules for shifted Priority tiers (LOW/MEDIUM/HIGH/CUSTOM).
Backends and timers are injected so rules are testable without Windows.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Callable, Optional

from config import Config, in_quiet_hours
from messages import format_notification
from models import CustomNotification, resolve_notification_spec

log = logging.getLogger("saw.notifier")
FLASH_MS = 2000
BEEP_EVERY_MS = 1200

_TONE_RANK = {"none": 0, "chime": 1, "beep": 2, "bell": 3, "alarm": 4}


class NullBackends:
    """Used on non-Windows machines and in tests."""
    def toast(self, text: str, stay: bool, silent: bool): ...
    def beep(self, kind: str): ...
    def popup_show(self, text: str, priority: str, seconds: int, on_ack: Callable): ...
    def popup_close(self): ...
    def flash(self, symbol: str, ms: int): ...


class WindowsBackends(NullBackends):
    """Toast via windows-toasts (winotify as fallback), sound via winsound."""

    def __init__(self, popup=None, flash_cb=None):
        self._popup, self._flash = popup, flash_cb
        self._toaster = None
        self._lock = threading.Lock()
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
                    kw = {"audio": ToastAudio(silent=True)}
                    if stay:
                        kw["scenario"] = ToastScenario.Alarm
                    self._toaster.show_toast(Toast(fields, **kw))
                elif self._mode == "wn":
                    from winotify import Notification
                    lines = text.split("\n")
                    Notification("Stock Alert Widget", lines[0], "\n".join(lines[1:]),
                                 duration="long" if stay else "short").show()
            except Exception as exc:
                log.warning("toast failed: %s", type(exc).__name__)

    def beep(self, kind):
        if kind == "none":
            return
        try:
            import winsound
            sound_map = {
                "alarm": winsound.MB_ICONHAND,
                "high": winsound.MB_ICONHAND,
                "beep": winsound.MB_ICONEXCLAMATION,
                "medium": winsound.MB_ICONEXCLAMATION,
                "bell": winsound.MB_ICONASTERISK,
                "chime": winsound.MB_ICONASTERISK,
                "low": winsound.MB_ICONASTERISK,
            }
            winsound.MessageBeep(sound_map.get(kind, winsound.MB_ICONASTERISK))
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


def _merge_specs(events: list) -> CustomNotification:
    specs = [ev.notification or resolve_notification_spec(ev.priority) for ev in events]
    popup = any(s.popup for s in specs)
    toast = any(s.toast for s in specs)
    repeat = any(s.repeat_until_ack for s in specs)
    sound_seconds = max(s.sound_seconds for s in specs)
    best_tone = max((s.sound_tone for s in specs), key=lambda t: _TONE_RANK.get(t, 0))
    return CustomNotification(
        popup=popup,
        toast=toast,
        sound_tone=best_tone,
        sound_seconds=sound_seconds,
        repeat_until_ack=repeat,
    )


class Notifier:
    def __init__(self, cfg: Config, backends, timers, now_fn: Callable[[], datetime]):
        """timers: object with schedule(ms, fn) -> handle and cancel(handle)."""
        self.cfg, self.b, self.t, self.now_fn = cfg, backends, timers, now_fn
        self.muted = False
        self._latest: Optional[dict] = None      # most recent unacked alert batch, for Alt+A & Mini pill
        self._pending_high: Optional[dict] = None
        self._listeners: list = []
        self._beep_h = self._beep_stop_h = self._end_h = self._esc_h = None

    def add_listener(self, fn: Callable[[], None]) -> None:
        if fn not in self._listeners:
            self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[], None]) -> None:
        try:
            self._listeners.remove(fn)
        except ValueError:
            pass

    def _notify_listeners(self) -> None:
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:
                log.exception("notifier listener failed")

    def toggle_mute(self) -> bool:
        self.muted = not self.muted
        if self.muted:
            self._stop_beep()
        return self.muted

    def notify(self, note, now: Optional[datetime] = None) -> None:
        if now is None:
            now = self.now_fn()
        spec = _merge_specs(note.events)
        eff_prio = "HIGH" if spec.repeat_until_ack else ("MEDIUM" if spec.popup else "LOW")
        text = format_notification(note, now)

        prev = self._latest
        unacked_prev = bool(prev and not prev.get("acked"))
        new_count = len(note.events)
        if unacked_prev and prev.get("priority") == "HIGH" and eff_prio != "HIGH":
            total_count = prev.get("count", 1) + new_count
            self._latest = {
                "text": text,
                "priority": "HIGH",
                "acked": False,
                "count": total_count,
                "spec": spec,
            }
            if self._pending_high is None:
                self._pending_high = prev
        else:
            total_count = (prev.get("count", 1) + new_count) if unacked_prev else new_count
            self._latest = {
                "text": text,
                "priority": eff_prio,
                "acked": False,
                "count": total_count,
                "spec": spec,
            }
            if eff_prio == "HIGH":
                if self._pending_high is not None and self._pending_high is not prev:
                    self._pending_high["acked"] = True
                self._pending_high = self._latest
            else:
                self._pending_high = None

        for ev in note.events:
            self.b.flash(ev.symbol, FLASH_MS)
        self._notify_listeners()

        quiet = self.muted or in_quiet_hours(self.cfg, now) or not self.cfg.sound
        if spec.toast and self.cfg.toast and not self.muted:
            self.b.toast(text, stay=spec.repeat_until_ack, silent=True)

        self._deliver_active(self._latest, spec, quiet, schedule_repeat=True)

    def _deliver_active(self, cur: dict, spec: CustomNotification, quiet: bool, schedule_repeat: bool) -> None:
        self._stop_sound()
        if spec.popup:
            self.b.popup_show(cur["text"], cur["priority"], self.cfg.pop_seconds, self.acknowledge_latest)
            self._end_h = self.t.schedule(self.cfg.pop_seconds * 1000, self._end_popup)
        if not quiet and spec.sound_tone != "none":
            if spec.sound_seconds <= 0:
                self.b.beep(spec.sound_tone)
            else:
                self._beep_loop(spec.sound_tone)
                self._beep_stop_h = self.t.schedule(spec.sound_seconds * 1000, self._stop_beep)
        if schedule_repeat and spec.repeat_until_ack:
            if self._esc_h is not None:
                self.t.cancel(self._esc_h)
            self._esc_h = self.t.schedule(
                self.cfg.escalation_seconds * 1000,
                lambda: self._escalate(cur, spec),
            )

    def acknowledge_latest(self) -> bool:
        cur = self._latest
        pending = self._pending_high
        if (not cur or cur.get("acked")) and (not pending or pending.get("acked")):
            return False
        if cur:
            cur["acked"] = True
        if pending:
            pending["acked"] = True
        self._pending_high = None
        self._stop_sound()
        self.b.popup_close()
        if self._esc_h is not None:
            self.t.cancel(self._esc_h)
            self._esc_h = None
        self._notify_listeners()
        return True

    def _beep_loop(self, kind: str) -> None:
        if self.muted:
            return
        self.b.beep(kind)
        self._beep_h = self.t.schedule(BEEP_EVERY_MS, lambda: self._beep_loop(kind))

    def _end_popup(self) -> None:
        self._end_h = None
        self.b.popup_close()

    def _stop_beep(self) -> None:
        for name in ("_beep_h", "_beep_stop_h"):
            h = getattr(self, name, None)
            if h is not None:
                self.t.cancel(h)
                setattr(self, name, None)

    def _stop_sound(self) -> None:
        self._stop_beep()
        if self._end_h is not None:
            self.t.cancel(self._end_h)
            self._end_h = None

    def _escalate(self, cur: dict, spec: CustomNotification) -> None:
        """Auto-repeat persistent alarms every `escalation_seconds` (2 min) until acknowledged."""
        self._esc_h = None
        if cur.get("acked"):
            return
        quiet = self.muted or in_quiet_hours(self.cfg, self.now_fn()) or not self.cfg.sound
        self._deliver_active(cur, spec, quiet, schedule_repeat=True)
