"""Stock Alert Widget: entry point. Start with:  pythonw main.py   (no console window).
Alert-only tool: it never places orders and never tells you to buy or sell.

Options:
  --mock            demo mode: scripted prices, no API key, separate data folder (nothing real is touched)
  --data-dir PATH   use another data folder instead of %APPDATA%\\StockAlertWidget
"""
import argparse
import json
import logging
import os
import queue
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from config import (BudgetError, Config, check_budget, data_dir, get_api_key,  # noqa: F401 (get_api_key kept for tests)
                    get_api_keys, load_config, min_poll_seconds)
from market_clock import Session

log = logging.getLogger("saw.main")
HK_EDIT, HK_ACK, HK_MUTE = 1, 2, 3            # Alt+Shift+W, Alt+A, Alt+M
PUMP_MS, DRAIN_MS, HOUSEKEEP_MS = 40, 250, 5000
WAKE_GAP_S = 30                                # a timer that fires 30 s late means the PC slept
DEMO_POLL_S = 15                               # demo prices move quickly; 15 s keeps the call budget valid
DEMO_BANNER = "DEMO MODE: scripted prices, not real market data. Nothing here is real."


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="main.py", description="Stock Alert Widget (alert-only, not financial advice)")
    p.add_argument("--mock", action="store_true",
                   help="demo mode: scripted prices, no API key, separate demo data folder")
    p.add_argument("--data-dir", default=None, metavar="PATH",
                   help="data folder (default: %%APPDATA%%\\StockAlertWidget, or ...\\StockAlertWidget_demo with --mock)")
    return p.parse_args(argv)


def resolve_data_dir(args, environ=os.environ) -> Path:
    """--data-dir wins. Demo mode gets its own folder so it can never touch your real watchlist."""
    if args.data_dir:
        return data_dir(Path(args.data_dir))
    if args.mock:
        base = environ.get("APPDATA")
        root = Path(base) if base else Path.home()
        return data_dir(root / ("StockAlertWidget_demo" if base else ".StockAlertWidget_demo"))
    return data_dir()


@dataclass
class Runtime:
    cfg: Config
    folder: Path
    provider: object
    polling: bool
    session_override: Optional[Session]
    warnings: list
    mutex_name: str
    demo: bool
    num_keys: int = 0                             # size of the Finnhub key pool (0 = none / demo)


def watchlist_size(folder: Path) -> Optional[int]:
    """Best-effort symbol count of watchlist.json. None when the file is missing or its layout is not
    recognised: the caller then simply skips the extra budget check."""
    try:
        data = json.loads((Path(folder) / "watchlist.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for name in ("symbols", "watchlist", "items", "stocks"):
            if isinstance(data.get(name), (list, dict)):
                return len(data[name])
    return None


def enforce_budget(cfg: Config, folder: Path, num_keys: int, warnings: list):
    """Check the real watchlist against the key pool's call budget. config.json is validated for
    max_symbols already; this catches a hand-edited watchlist that is longer. Over budget never
    crashes the app: polling is slowed to the fastest safe interval and a warning is shown."""
    n = watchlist_size(folder)
    if not n:
        return cfg, warnings
    try:
        check_budget(n, cfg.poll_seconds, num_keys=num_keys)
        return cfg, warnings
    except BudgetError as exc:
        safe = min_poll_seconds(n, num_keys)
        log.warning("call budget exceeded; polling slowed from %s s to %s s", cfg.poll_seconds, safe)
        return replace(cfg, poll_seconds=safe), warnings + [f"{exc} Polling slowed to every {safe} s."]


def forward_pool_notices(provider, warnings: list) -> None:
    """Move key-pool events ("key #3 was rejected ...") from the provider onto the widget's warnings."""
    pop = getattr(provider, "pop_notices", None)
    if callable(pop):
        for msg in pop():
            if msg not in warnings:
                warnings.append(msg)


def build_runtime(args, environ=os.environ) -> Runtime:
    """Everything decided before any window exists (kept separate so it can be tested)."""
    from providers.finnhub import FinnhubProvider
    from providers.mock import DEMO_WATCHLIST, DemoProvider, MockProvider
    from store import atomic_write_json

    folder = resolve_data_dir(args, environ)
    env_keys = [] if args.mock else get_api_keys(environ)    # environment only; never logged or written
    cfg, warnings = load_config(folder / "config.json", num_env_keys=len(env_keys))
    if args.mock:
        if not (folder / "watchlist.json").exists():
            atomic_write_json(folder / "watchlist.json", DEMO_WATCHLIST)
        return Runtime(replace(cfg, poll_seconds=DEMO_POLL_S), folder, DemoProvider(), True, Session.REGULAR,
                       [DEMO_BANNER] + warnings, "Local\\StockAlertWidget_SingleInstance_DEMO", True)
    keys = env_keys or list(cfg.api_keys)                    # env wins; config.json "api_keys" is the fallback
    if keys:
        from store import register_keys_for_redaction
        register_keys_for_redaction(keys)
        if len(keys) >= 4 and cfg.poll_seconds == 60:
            cfg = replace(cfg, poll_seconds=15)
        cfg, warnings = enforce_budget(cfg, folder, len(keys), warnings)
    provider = FinnhubProvider(keys) if keys else MockProvider({})
    return Runtime(cfg, folder, provider, bool(keys), None, warnings, "Local\\StockAlertWidget_SingleInstance", False,
                   len(keys))


class MutexGuard:
    """Owns the Win32 single-instance mutex handle and releases it with CloseHandle().
    close() is idempotent and never raises, so it is safe in shutdown and error paths."""

    def __init__(self, handle):
        self._h = handle

    def close(self) -> None:
        h, self._h = self._h, None
        if not h:
            return
        try:
            closer = getattr(h, "close", None)              # already a wrapper object
            if callable(closer):
                closer()
                return
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            k32.CloseHandle.restype = wintypes.BOOL
            k32.CloseHandle(h)
        except Exception as exc:
            log.info("mutex close failed: %s", type(exc).__name__)

    def __del__(self):
        self.close()


def _fix_pythonw_streams() -> None:
    """Under pythonw there is no console: sys.stdout/stderr are None and argparse would crash."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w"))


def run(argv=None) -> None:
    _fix_pythonw_streams()
    args = parse_args(argv)
    if sys.platform != "win32":
        sys.exit("The Stock Alert Widget only runs on Windows.")
    import tkinter as tk

    import desktop_layer as dl
    from app_core import AppController
    from notifier import Notifier, WindowsBackends
    from scheduler import Scheduler
    from store import Store, setup_logging
    from ui.common import TkTimers
    from ui.popup import AlertPopup
    from ui.widget import WidgetView, merged_look
    from market_clock import utcnow

    dl.set_dpi_aware()
    rt = build_runtime(args)
    raw_mutex = dl.single_instance(rt.mutex_name)
    if raw_mutex is None:
        return                                  # a second copy exits silently
    res = {"mutex": MutexGuard(raw_mutex), "sched": None, "layer": None, "ctrl": None, "root": None,
           "provider": rt.provider, "done": False}

    def step(label, fn):                        # one failing teardown step must not skip the rest
        try:
            fn()
        except Exception as exc:
            log.warning("shutdown step '%s' failed: %s", label, type(exc).__name__)

    def shutdown():                             # idempotent: quit_app() and the finally block both call it
        if res["done"]:
            return
        res["done"] = True
        if res["sched"] is not None:
            step("scheduler", res["sched"].close)          # stops polling and closes the provider session
        else:
            closer = getattr(res["provider"], "close", None)
            if callable(closer):
                step("provider", closer)
        if res["layer"] is not None:
            step("layer", res["layer"].quit)
        if res["ctrl"] is not None:
            step("save state", res["ctrl"]._save_state)
        if res["root"] is not None:
            step("window", res["root"].destroy)
        step("mutex", res["mutex"].close)

    try:
        setup_logging(rt.folder)
        cfg = rt.cfg
        store = Store(rt.folder)

        root = tk.Tk()
        res["root"] = root
        root.report_callback_exception = lambda *a: log.error("UI error", exc_info=a)
        layer = dl.DesktopLayer(root, merged_look(cfg.look))
        res["layer"] = layer
        popup = AlertPopup(root, make_window=dl.make_popup_window)
        holder = {}
        backends = WindowsBackends(popup=popup, flash_cb=lambda s, ms: holder["view"].flash(s, ms))
        notifier = Notifier(cfg, backends, TkTimers(root), utcnow)
        q = queue.Queue()
        ctrl = AppController(cfg, store, rt.provider, notifier, warnings=rt.warnings, polling_enabled=rt.polling,
                             session_override=rt.session_override)
        res["ctrl"] = ctrl
        sched = Scheduler(rt.provider, ctrl.symbols, q, cfg)
        res["sched"] = sched
        sched.provider_session = rt.session_override
        ctrl.run_call, ctrl.poll_now = sched.run_call, sched.request_now

        def quit_app():
            shutdown()

        view = WidgetView(root, ctrl, layer, cfg.look, request_quit=quit_app)
        holder["view"] = view
        w, h = view.render()
        layer.attach(w, h, hook=cfg.hotkeys_enabled)
        if cfg.hotkeys_enabled:
            failed = layer.register_hotkeys({HK_EDIT: (dl.MOD_ALT | dl.MOD_SHIFT, "W"), HK_ACK: (dl.MOD_ALT, "A"),
                                             HK_MUTE: (dl.MOD_ALT, "M")})
            names = {HK_EDIT: "Alt+Shift+W", HK_ACK: "Alt+A", HK_MUTE: "Alt+M"}
            for hid in failed:
                ctrl.warnings.append(f"Hotkey {names[hid]} is used by another program and was not registered.")
        ctrl.housekeeping(utcnow())                 # catch-up summary if the PC was off at the close
        view.start_clock()
        if rt.polling:
            sched.start()

        last_beat = [time.time()]

        def rearm(ms, fn):                          # a failed tick must never end its own timer chain
            if not res["done"]:
                root.after(ms, fn)

        def pump():                                 # hotkeys, peek, z-order
            try:
                layer.poll()
                for hid in layer.pop_hotkeys():
                    if hid == HK_EDIT:
                        view.toggle_edit()
                    elif hid == HK_ACK:
                        notifier.acknowledge_latest()
                    elif hid == HK_MUTE:
                        notifier.toggle_mute()
                        view.refresh()
                if layer.quit_requested:
                    quit_app()
                    return
            except Exception:
                log.exception("pump failure")
            rearm(PUMP_MS, pump)

        def drain():                                # everything from the poller thread
            try:
                if not q.empty():
                    ctrl.drain(q)
                    view.refresh()
            except Exception:
                log.exception("drain failure")
            rearm(DRAIN_MS, drain)

        def housekeeping():
            try:
                now = time.time()
                if layer.wake_pending or now - last_beat[0] > WAKE_GAP_S:
                    layer.wake_pending = False
                    ctrl.on_wake(utcnow())
                last_beat[0] = now
                ctrl.housekeeping(utcnow())
                forward_pool_notices(rt.provider, ctrl.warnings)
                view.refresh()
            except Exception:
                log.exception("housekeeping failure")
                last_beat[0] = time.time()
            rearm(HOUSEKEEP_MS, housekeeping)

        pump()
        drain()
        root.after(HOUSEKEEP_MS, housekeeping)
        root.mainloop()
    finally:
        shutdown()                              # also runs after a crash or an early exit


if __name__ == "__main__":
    run()
