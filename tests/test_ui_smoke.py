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
    assert "AAPL" in t and "TTWO" in t and "207.50" in t and "Sell zone" in t
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


def test_expanded_section_shows_breakeven_zones_and_live_line(app):
    root, ctrl, view, layer, st, quits = app
    ed = open_editor(view, "AAPL")
    assert "Breakeven 201.41" in ed.lbl_info.cget("text") and "+$60.72" in ed.lbl_info.cget("text")
    assert "Current price 207.50" in ed.lbl_live.cget("text")
    assert ed._zlabels["sell"][1].cget("text") == "207.46 – 210.47"
    assert ed._zlabels["loss"][1].cget("text") == "≤ 195.36"
    assert ed._zlabels["deep_loss"][0].cget("text").startswith("P ≤")           # listed even though off
    assert not ed._zv["deep_loss"]["on"].get() and ed._zv["sell"]["on"].get()


def test_editing_threshold_updates_displayed_price_and_saves(app):
    root, ctrl, view, layer, st, quits = app
    ed = open_editor(view, "AAPL")
    ed._zv["sell"]["pct"].set("2.5")
    ed._update_live()
    assert ed._zlabels["sell"][1].cget("text").startswith("206.45")             # 201.41 x 1.025 = 206.4453 -> 206.45
    ed._save()
    assert ctrl.stock("AAPL").zones["sell"]["pct"] == Dc("2.5")
    data = json.loads(st.watchlist_path.read_text())
    assert data["symbols"][0]["zones"]["sell"]["pct"] == 2.5
    assert "loss" not in data["symbols"][0]["zones"]                            # defaults are not stored


def test_out_of_order_threshold_blocked_with_message(app):
    root, ctrl, view, layer, st, quits = app
    ed = open_editor(view, "AAPL")
    ed._zv["sell"]["pct"].set("5")                                              # above high_sell 4.5
    ed._save()
    assert "lower than High sell" in ed.err.cget("text") and ctrl.stock("AAPL").zones["sell"]["pct"] == Dc("3.0")


def test_bad_numbers_blocked(app):
    root, ctrl, view, layer, st, quits = app
    ed = open_editor(view, "AAPL")
    ed.v_shares.set("0"); ed._save()
    assert "above 0" in ed.err.cget("text")
    ed.v_shares.set("abc"); ed._save()
    assert "not a number" in ed.err.cget("text")
    assert ctrl.stock("AAPL").position.shares == 10


def test_category_change_keeps_numbers_and_hides_fields(app):
    root, ctrl, view, layer, st, quits = app
    ed = open_editor(view, "AAPL")
    ed.v_cat.set("BUY"); ed._on_category(); root.update()
    assert Dc(ed.v_shares.get()) == 10 and Dc(ed.v_avg.get()) == 200
    ed.v_ep.set("190"); ed._save()
    s = ctrl.stock("AAPL")
    assert s.category == "BUY" and s.planned_entry.price == 190 and s.position.shares == 10
    ed2 = view.panel.editor
    ed2.v_cat.set("WATCH"); ed2._on_category(); ed2._save()
    assert ctrl.stock("AAPL").category == "WATCH" and ctrl.stock("AAPL").planned_entry.price == 190


def test_add_alert_dialog_creation_check_fire_now(app):
    root, ctrl, view, layer, st, quits = app
    ed = open_editor(view, "TTWO")                                              # live price 201.90
    got = []
    ctx = ed._context()
    d = AlertDialog(root, ctx, None, lambda a, c: got.append((a, c)))
    d.v_type.set("Price at or below"); d._layout()
    d.v_val.set("202.73"); d.v_mode.set("once"); d.v_prio.set("HIGH"); d.v_label.set("RISK")
    d._save(); root.update()
    choice = [w for w in d.winfo_children() if isinstance(w, ChoiceDialog)]
    assert len(choice) == 1                                                     # "current 201.90, this would fire now"
    choice[0]._finish("wait_cross")
    assert got[0][0].price == Dc("202.73") and got[0][1] == "wait_cross" and got[0][0].priority == "HIGH"
    ed._open_alert  # exercised through the editor below
    ed.stock.alerts.append(got[0][0]); ed.choices[got[0][0].id] = "wait_cross"
    ed._save()
    assert ctrl.engine.alert_state("TTWO", got[0][0].id)["state"] == "WAIT_CROSS"


def test_alert_dialog_percent_and_zone_exit_and_validation(app):
    root, ctrl, view, layer, st, quits = app
    ed = open_editor(view, "AAPL")
    got = []
    d = AlertDialog(root, ed._context(), None, lambda a, c: got.append((a, c)))
    d.v_by.set("percent"); d.v_val.set("4"); d.v_type.set("Price at or above"); d._layout()
    assert "$209.47" in d.lbl_eq.cget("text") or "$209.46" in d.lbl_eq.cget("text")
    d.v_val.set("900"); d._save()
    assert "-90 and +500" in d.err.cget("text") and not got
    d.v_val.set("4"); d._save(); root.update()
    assert got and got[0][0].pct == 4 and got[0][1] is None                      # 209.46 not yet reached at 207.50
    d2 = AlertDialog(root, ed._context(), None, lambda a, c: got.append((a, c)))
    d2.v_type.set("Leaves a zone"); d2._layout()
    assert "Sell range" in d2.v_zone.get()
    d2._save()
    assert got[1][0].type == "zone_exit" and got[1][0].zone == "sell"


def test_watch_percent_alert_blocked(app):
    root, ctrl, view, layer, st, quits = app
    ed = open_editor(view, "AAPL")
    ed.v_cat.set("WATCH"); ed._on_category(); ed._save()
    ed = view.panel.editor
    d = AlertDialog(root, ed._context(), None, lambda a, c: None)
    assert str(d.rb_pct.cget("state")) == "disabled"                            # no base for percent on WATCH


def test_add_stock_dialog_flow(app):
    root, ctrl, view, layer, st, quits = app
    view.enter_edit()
    added = []
    d = AddStockDialog(root, ctrl, added.append)
    d.v_sym.set("msft"); d._check(); root.update()
    assert d.btn_add.cget("state") == "normal" and "410.00" in d.lbl_status.cget("text")
    d.v_cat.set("HOLD"); d._layout(); d.v_nick.set("Microsoft"); d.v_shares.set("2"); d.v_avg.set("400")
    d._add(); root.update()
    assert added == ["MSFT"] and ctrl.symbols() == ["AAPL", "TTWO", "MSFT"]
    assert json.loads(st.watchlist_path.read_text())["symbols"][2]["position"]["shares"] == 2


def test_add_stock_rejects_zero_price_duplicates_and_blank_position(app):
    root, ctrl, view, layer, st, quits = app
    view.enter_edit()
    d = AddStockDialog(root, ctrl, lambda s: None)
    d.v_sym.set("nope"); d._check(); root.update()
    assert d.btn_add.cget("state") == "disabled" and "No price data" in d.lbl_status.cget("text")
    d.v_sym.set("AAPL"); d._check()
    assert "already" in d.lbl_status.cget("text")
    d.v_sym.set("MSFT"); d._check(); d.v_cat.set("SELL"); d._layout(); d._add()
    assert "Shares" in d.err.cget("text")
    d.v_sym.set("MSFTX"); d._invalidate()
    assert d.btn_add.cget("state") == "disabled"


def test_remove_stock_with_confirmation(app, monkeypatch):
    root, ctrl, view, layer, st, quits = app
    ed = open_editor(view, "TTWO")
    from tkinter import messagebox
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: False)
    ed._remove(); assert ctrl.symbols() == ["AAPL", "TTWO"]
    seen = []
    monkeypatch.setattr(messagebox, "askyesno", lambda title, msg, **k: seen.append(msg) or True)
    ed._remove(); root.update()
    assert ctrl.symbols() == ["AAPL"] and "deletes its position and alerts" in seen[0] and "Realized profit is not saved" in seen[0]
    assert "TTWO" not in ctrl.engine.state["symbols"]


def test_edit_panel_resizes_window_within_screen(app):
    root, ctrl, view, layer, st, quits = app
    view.enter_edit(); root.update(); view.panel._resize()
    w, h = layer.fits[-1]
    assert h <= root.winfo_screenheight() and w >= 660


# ---- items from the review: auto Slipping in the UI flows, watchlist-full message ---------------------
def _slip_alerts(stock):
    return [a for a in stock.alerts if a.type == "zone_exit" and a.zone == "sell"]


def test_add_sell_stock_via_dialog_shows_slipping_in_its_editor(app):
    root, ctrl, view, layer, st, quits = app
    view.enter_edit()
    d = AddStockDialog(root, ctrl, lambda sym: view.panel._added(sym))
    d.v_sym.set("msft"); d._check(); root.update()
    d.v_cat.set("SELL"); d._layout(); d.v_shares.set("2"); d.v_avg.set("400")
    d._add(); root.update()
    assert [a.id for a in _slip_alerts(ctrl.stock("MSFT"))] == ["msft-slip"]
    ed = view.panel.editor                                               # the new stock's editor opened
    assert ed is not None and ed.symbol == "MSFT"
    texts_ = [w.cget("text") for w in ed.f_alerts.winfo_children()[0].winfo_children() if isinstance(w, tk.Label)]
    assert any("Leaves the sell range" in t for t in texts_) and any("RISK" in t and "MEDIUM" in t for t in texts_)


def test_switching_category_to_sell_in_the_editor_adds_slipping_and_delete_sticks(app):
    root, ctrl, view, layer, st, quits = app
    ed = open_editor(view, "TTWO")                                       # a BUY stock with one custom alert
    ed.v_cat.set("SELL"); ed._on_category()
    ed.v_shares.set("5"); ed.v_avg.set("200"); ed._save()
    assert ctrl.stock("TTWO").category == "SELL" and [a.id for a in _slip_alerts(ctrl.stock("TTWO"))] == ["ttwo-slip"]
    ed = view.panel.editor
    slip = [a for a in ed.stock.alerts if a.id == "ttwo-slip"][0]
    ed._delete_alert(slip); ed._save()                                   # the user deletes it
    assert _slip_alerts(ctrl.stock("TTWO")) == []
    view.panel.editor._save()                                            # saving again does not bring it back
    assert _slip_alerts(ctrl.stock("TTWO")) == []


def test_add_dialog_refuses_when_the_watchlist_is_full(app, monkeypatch):
    root, ctrl, view, layer, st, quits = app
    from dataclasses import replace
    from tkinter import messagebox
    import ui.editor as ed_mod
    ctrl.cfg = replace(ctrl.cfg, max_symbols=2)                          # AAPL + TTWO already fill it
    seen = []
    monkeypatch.setattr(messagebox, "showerror", lambda title, msg, **k: seen.append((title, msg)))
    view.enter_edit()
    d = AddStockDialog(root, ctrl, lambda s: None)
    assert d._closed and not d.winfo_exists()
    assert seen and seen[0][0] == "Watchlist full"
    assert "max_symbols = 2" in seen[0][1] and "48 calls per minute" in seen[0][1] and "config.json" in seen[0][1]
    assert ed_mod.watchlist_full_message(30, 30).startswith("The watchlist already has 30 stocks")


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
