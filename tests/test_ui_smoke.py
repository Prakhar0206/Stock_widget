"""Drives the real Tk widgets (needs a display: run under xvfb-run on Linux, or on Windows).
The Win32 layer is replaced by a fake that records what the UI asks of it."""
import json
import os
import tkinter as tk
from datetime import datetime, timezone
from decimal import Decimal as Dc

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("DISPLAY") and os.name != "nt", reason="needs a display (xvfb-run)")

from app_core import AppController
from config import Config
from market_clock import IST
from models import Alert
from notifier import Notifier, NullBackends
from providers.mock import MockProvider
from store import Store
from test_config_store import EXAMPLE, write
from conftest import tick
from ui.common import TkTimers
from ui.editor import AddStockDialog, AlertDialog, ChoiceDialog, StockEditor
from ui.widget import EditPanel, WidgetView

T0 = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)


class FakeLayer:
    def __init__(self):
        self.fits, self.interactive_calls = [], []

    def fit(self, w, h): self.fits.append((w, h))
    def set_interactive(self, on): self.interactive_calls.append(on)


@pytest.fixture
def app(tmp_path):
    root = None
    for _ in range(5):
        try:
            root = tk.Tk()
            break
        except tk.TclError:
            import time
            time.sleep(0.05)
    if root is None:
        root = tk.Tk()
    root.withdraw()
    st = Store(tmp_path)
    write(st.watchlist_path, EXAMPLE)
    notifier = Notifier(Config(), NullBackends(), TkTimers(root), lambda: T0)
    mp = MockProvider({"MSFT": [410]})
    ctrl = AppController(Config(), st, mp, notifier, now_fn=lambda: T0)
    ctrl.on_ticks([tick("207.50", "AAPL", T0, 200), tick("201.90", "TTWO", T0, 206)], T0)
    layer = FakeLayer()
    quits = []
    view = WidgetView(root, ctrl, layer, {}, now_fn=lambda: T0, request_quit=lambda: quits.append(1))
    view.render()
    yield root, ctrl, view, layer, st, quits
    try:
        root.destroy()
    except tk.TclError:
        pass


def texts(canvas):
    return [canvas.itemcget(i, "text") for i in canvas.find_all() if canvas.type(i) == "text"]


def test_normal_view_draws_header_rows_and_fits(app):
    root, ctrl, view, layer, st, quits = app
    t = texts(view.canvas)
    assert any(icon in t for icon in ("☀️", "🌙"))
    assert any("IST" in s for s in t)
    assert "AAPL" in t and "TTWO" in t and "207.50" in t
    assert layer.fits and layer.fits[-1][0] > 300 and layer.fits[-1][1] > 60


def test_flash_highlights_row_then_clears(app):
    root, ctrl, view, layer, st, quits = app
    view.flash("AAPL", 80)
    assert "AAPL" in view.flashing()
    rects = [i for i in view.canvas.find_all() if view.canvas.type(i) == "rectangle"]
    assert len(rects) == 1
    root.update(); root.after(200, root.quit); root.mainloop()
    assert "AAPL" not in view.flashing()


def test_edit_mode_enter_exit_esc_and_idle(app):
    root, ctrl, view, layer, st, quits = app
    view.enter_edit()
    assert view.editing and layer.interactive_calls == [True]
    root.update()
    ev = tk.Event(); ev.widget = root
    view._escape(ev)
    assert not view.editing and layer.interactive_calls == [True, False]
    view.enter_edit()
    import ui.widget as w
    view._last_activity -= w.IDLE_EDIT_S + 1
    root.after(1300, root.quit); root.mainloop()
    assert not view.editing                                   # idle timeout left edit mode
    view.toggle_edit(); assert view.editing
    view.panel.winfo_children()[0].winfo_children()[-1].invoke()        # "Exit edit mode"
    assert not view.editing


def test_quit_button_calls_request_quit(app):
    root, ctrl, view, layer, st, quits = app
    view.enter_edit()
    top = view.panel.winfo_children()[0]
    quit_btn = [b for b in top.winfo_children() if b.cget("text") == "Quit"][0]
    quit_btn.invoke()
    assert quits == [1]


def open_editor(view, sym):
    view.enter_edit()
    view.panel.toggle(sym)
    view.root.update()
    return view.panel.editor


def test_expanded_section_shows_trade_summary_and_live_line(app):
    root, ctrl, view, layer, st, quits = app
    ed = open_editor(view, "AAPL")
    assert "Current price" in ed.lbl_live.cget("text") and "207.50" in ed.lbl_live.cget("text")
    assert Dc(ed.v_buy.get()) == Dc("200.00")
    assert Dc(ed.v_shares.get()) == Dc("10")
    assert ed.lbl_info.cget("text")


def test_editing_trade_fields_updates_summary_and_saves(app):
    root, ctrl, view, layer, st, quits = app
    ed = open_editor(view, "AAPL")
    ed.v_buy.set("201.00")
    ed.v_fees.set("4.00")
    ed._save()
    assert ctrl.stock("AAPL").trade.buy_price == Dc("201.00")
    assert ctrl.stock("AAPL").trade.fees == Dc("4.00")
    data = json.loads(st.watchlist_path.read_text())
    assert data["symbols"][0]["trade"]["buy_price"] == 201.0


def test_bad_trade_numbers_blocked(app):
    root, ctrl, view, layer, st, quits = app
    ed = open_editor(view, "AAPL")
    ed.v_shares.set("0")
    ed._save()
    assert ed.err.cget("text")
    ed.v_shares.set("abc")
    ed._save()
    assert ed.err.cget("text")
    assert ctrl.stock("AAPL").trade.shares == 10


def test_category_change_keeps_trade_numbers(app):
    root, ctrl, view, layer, st, quits = app
    ed = open_editor(view, "AAPL")
    ed.v_cat.set("BUY")
    ed._save()
    s = ctrl.stock("AAPL")
    assert s.category == "BUY" and s.trade.shares == 10 and s.trade.buy_price == Dc("200.00")
    ed2 = view.panel.editor
    ed2.v_cat.set("WATCH")
    ed2._save()
    assert ctrl.stock("AAPL").category == "WATCH" and ctrl.stock("AAPL").trade.shares == 10


def test_alert_dialog_create_persists_immediately_even_when_closing_edit_mode(app):
    root, ctrl, view, layer, st, quits = app
    ed = open_editor(view, "AAPL")
    ed._open_alert(None)
    d = ed._alert_dlg
    assert isinstance(d, AlertDialog) and d.winfo_exists()
    d.v_op.set("Greater Than")
    d._on_op_changed()
    d.v_val.set("220.00")
    d._on_value_edited()
    d.v_prio.set("HIGH (Popup + 2m repeat alarm)")
    d.v_label.set("SELL")
    d._save()
    root.update()
    # Alert must already be persisted in controller & watchlist.json WITHOUT clicking StockEditor._save()
    assert any(a.value == Dc("220.00") for a in ctrl.stock("AAPL").alerts)
    disk_alerts = json.loads(st.watchlist_path.read_text())["symbols"][0]["alerts"]
    assert any(a["value"] == 220.0 for a in disk_alerts)

    # Exit edit mode without clicking StockEditor.Save -> alert must still survive!
    view.exit_edit()
    assert any(a.value == Dc("220.00") for a in ctrl.stock("AAPL").alerts)
    disk_alerts_after = json.loads(st.watchlist_path.read_text())["symbols"][0]["alerts"]
    assert any(a["value"] == 220.0 for a in disk_alerts_after)


def test_alert_dialog_subviews_pct_preview_and_watch_fallback(app):
    root, ctrl, view, layer, st, quits = app
    ed = open_editor(view, "AAPL")
    got = []
    d = AlertDialog(root, ctrl, "AAPL", None, lambda sym, a, c: got.append((sym, a, c)))
    d.v_cmode.set("Percent (%)")
    d._on_cmode_changed()
    d.v_val.set("4")
    d._on_value_edited()
    root.update()
    assert "208" in d.lbl_preview.cget("text") or "207" in d.lbl_preview.cget("text")

    # Show more operators toggle
    d._toggle_more_ops()
    assert d._show_more is True

    # < Edit message sub-view + placeholder insertion
    d._open_msg_subview()
    d.v_name.set("Breakout")
    d._insert_placeholder("{{ticker}}")
    d._apply_msg_subview()

    # < Notifications custom sub-view
    d.v_prio.set("CUSTOM...")
    d._open_notif_subview()
    d.v_notif_popup.set(False)
    d.v_notif_toast.set(True)
    d.v_notif_tone.set("bell")
    d._apply_notif_subview()

    # Invalid extreme % blocked
    d.v_val.set("900")
    d._save()
    assert d.err.cget("text") and not got

    d.v_val.set("4")
    d._save()
    root.update()
    assert got and got[0][1].condition_mode == "pct" and got[0][1].priority == "CUSTOM"
    assert got[0][1].custom_notification is not None and got[0][1].custom_notification.popup is False

    # WATCH stock without Buy/Sell price still supports Percent (%) cleanly via Previous Close fallback
    ed.v_cat.set("WATCH")
    ed.v_buy.set("")
    ed.v_shares.set("")
    ed._save()
    d2 = AlertDialog(root, ctrl, "AAPL", None, lambda sym, a, c: got.append((sym, a, c)))
    d2.v_cmode.set("Percent (%)")
    d2._on_cmode_changed()
    d2.v_val.set("5")
    d2._on_value_edited()
    root.update()
    assert "210" in d2.lbl_preview.cget("text") or "close" in d2.lbl_preview.cget("text").lower()
    d2._save()
    root.update()
    assert len(got) == 2


def test_add_stock_dialog_flow(app):
    root, ctrl, view, layer, st, quits = app
    view.enter_edit()
    added = []
    d = AddStockDialog(root, ctrl, added.append)
    d.v_sym.set("msft")
    d._check()
    root.update()
    assert d.btn_add.cget("state") == "normal" and "410.00" in d.lbl_status.cget("text")
    d.v_cat.set("HOLD")
    d.v_nick.set("Microsoft")
    d.v_shares.set("2")
    d.v_buy.set("400")
    d._add()
    root.update()
    assert added == ["MSFT"] and ctrl.symbols() == ["AAPL", "TTWO", "MSFT"]
    assert json.loads(st.watchlist_path.read_text())["symbols"][2]["trade"]["shares"] == 2


def test_add_stock_rejects_zero_price_and_duplicates_and_allows_optional_trade(app):
    root, ctrl, view, layer, st, quits = app
    view.enter_edit()
    d = AddStockDialog(root, ctrl, lambda s: None)
    d.v_sym.set("nope")
    d._check()
    root.update()
    assert d.btn_add.cget("state") == "disabled" and "No price data" in d.lbl_status.cget("text")
    d.v_sym.set("AAPL")
    d._check()
    assert "already" in d.lbl_status.cget("text")
    # Blank trade fields are now valid on SELL/HOLD/BUY
    d.v_sym.set("MSFT")
    d._check()
    d.v_cat.set("SELL")
    d.v_sym.set("MSFTX")
    d._invalidate()
    assert d.btn_add.cget("state") == "disabled"
    d.v_sym.set("MSFT")
    d._check()
    d.v_cat.set("SELL")
    d._add()
    root.update()
    assert "MSFT" in ctrl.symbols()


def test_remove_stock_with_confirmation(app, monkeypatch):
    root, ctrl, view, layer, st, quits = app
    ed = open_editor(view, "TTWO")
    from tkinter import messagebox
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: False)
    ed._remove()
    assert ctrl.symbols() == ["AAPL", "TTWO"]
    seen = []
    monkeypatch.setattr(messagebox, "askyesno", lambda title, msg, **k: seen.append(msg) or True)
    ed._remove()
    root.update()
    assert ctrl.symbols() == ["AAPL"] and seen
    assert "TTWO" not in ctrl.engine.state["symbols"]


def test_edit_panel_resizes_window_within_screen(app):
    root, ctrl, view, layer, st, quits = app
    view.enter_edit()
    root.update()
    view.panel._resize()
    w, h = layer.fits[-1]
    assert h <= root.winfo_screenheight() and w >= 660


def test_add_dialog_refuses_when_the_watchlist_is_full(app, monkeypatch):
    root, ctrl, view, layer, st, quits = app
    from dataclasses import replace
    from tkinter import messagebox
    import ui.editor as ed_mod
    ctrl.cfg = replace(ctrl.cfg, max_symbols=2)
    seen = []
    monkeypatch.setattr(messagebox, "showerror", lambda title, msg, **k: seen.append((title, msg)))
    view.enter_edit()
    d = AddStockDialog(root, ctrl, lambda s: None)
    assert d._closed and not d.winfo_exists()
    assert seen and seen[0][0] == "Watchlist full"
    assert "max_symbols = 2" in seen[0][1] and "48 calls per minute" in seen[0][1] and "config.json" in seen[0][1]
    assert ed_mod.watchlist_full_message(30, 30).startswith("The watchlist already has 30 stocks")


def test_hold_stock_alert_dialog_defaults_label_to_hold_and_saves(app):
    root, ctrl, view, layer, st, quits = app
    ed = open_editor(view, "TTWO")
    ed.v_cat.set("HOLD")
    ed._save()
    root.update()
    ed = view.panel.editor
    ed._open_alert(None)
    d = ed._alert_dlg
    assert d.v_label.get() == "HOLD"
    d.v_op.set("Greater Than")
    d._on_op_changed()
    d.v_val.set("215.00")
    d._on_value_edited()
    d._save()
    root.update()
    assert not d.winfo_exists()
    assert any(a.label == "HOLD" and a.value == Dc("215.00") for a in ctrl.stock("TTWO").alerts)
    disk_alerts = json.loads(st.watchlist_path.read_text())["symbols"][1]["alerts"]
    assert any(a["label"] == "HOLD" and a["value"] == 215.0 for a in disk_alerts)


def test_heartbeat_pulse_animation_runs_smoothly(app):
    root, ctrl, view, layer, st, quits = app
    # Canvas should have both core and ring items
    assert view._hb_core_id is not None
    assert view._hb_ring_id is not None
    # Trigger pulse animation
    view.pulse_heartbeat()
    assert view._pulse_job is not None
    # Step through animation frames
    for _ in range(12):
        root.after(40)
        root.update()
    # At completion of sequence, pulse job clears cleanly
    assert view._pulse_job is None
