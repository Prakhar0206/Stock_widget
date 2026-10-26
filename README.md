# Stock Alert Widget (Steps A + B)

Alert-only tool. It never places orders and never tells you to buy or sell. Not financial advice.
Windows 10/11, one PC, one user, no cloud.

## Setup (PowerShell)
    py -m venv .venv
    .venv\Scripts\activate
    pip install -r requirements.txt
    python -m pytest -q                      # logic + (on Windows) UI tests
    setx FINNHUB_API_KEY "your-key"          # then open a NEW terminal; never paste the key into a chat
    pythonw main.py                          # starts with no console window

Without the key the widget still starts, shows "FINNHUB_API_KEY is not set" and does not poll.
Data lives in `%APPDATA%\StockAlertWidget` (config.json, watchlist.json, watchlist.last_good.json,
state.json, alerts_log.jsonl, app.log, summaries\). The key is read from the environment only and is
sent in the `X-Finnhub-Token` header; it is never written to any file or log.

Autostart (not enabled by default): `python tools\startup_shortcut.py create` / `remove` / `status`.

## Demo mode (no API key, nothing real is touched)
    pythonw main.py --mock          (or  python main.py --mock  to see errors in the window)
Scripted prices swing through every zone, so alerts, pop-ups and sounds all trigger within a few
minutes. It uses its own folder (`%APPDATA%\StockAlertWidget_demo`) with a ready-made demo watchlist
(AAPL, TTWO, MSFT, NVDA, VST), forces the market "open" at any hour, writes no daily summary, and
shows a "DEMO MODE" line on the widget. Close the real widget first (both register the same hotkeys).
`--data-dir PATH` runs the normal app against another data folder.

## Using it
| Key | What it does |
|---|---|
| Alt+Shift+W | Toggle edit mode. Esc or 60 s without a click/key leaves it. |
| Alt+A | Acknowledge the latest alert (stops pop-up repeats). |
| Alt+M | Mute / unmute. |
| Alt+W (hold) | Peek above all windows (from your clock). |
| Ctrl+Alt+Shift+Q | Quit (from your clock). Edit mode also has Quit and Exit buttons. |

Edit mode: **+ Add stock** (ticker checked with Finnhub, zero price / duplicates rejected), click a row
to open its expanded section (category, position, zones with price equivalents, custom alerts,
Remove stock). Defaults for zones, fees, priorities, polling etc. are in config.json (see the
blueprint, section 10.1). Fee rates in config.json are percents, e.g.
`{"fees": {"buy_rate_pct": 0.33, "buy_min": 0.02, "sell_rate_pct": 0.37, "sell_min": 0.05}}`.
Look settings go under `"look"` (anchor, margin_x, margin_y, pos_x, pos_y, font, size, text_color,
shadow_color, shadow_offset, background_mode, panel_color, opacity, padding); defaults are your clock's.

## What was tested where
* **Tested here (Linux, 145 tests):** all money/zone/alert logic, storage, market clock, Finnhub
  response handling (fake HTTP session), scheduler/backoff/budget, notifier rules, controller,
  daily summary, and the real Tk widget, editor and dialogs under a virtual display with a fake
  desktop layer. `desktop_layer.py` has a stubbed-ctypes test of its message handling and z-order
  locking logic (it caught one real bug).
* **NOT tested (needs your PC):** anything that talks to Windows or Finnhub for real: the desktop
  layer itself, click-through, Win+D, hotkey registration, toasts, sounds, the pop-up above other
  windows, lock/unlock, sleep/resume, remote desktop, multi-monitor/DPI, and live quotes. Run the
  manual checklist below. The 8-hour wall-clock soak with handle counts is also yours to run; the
  pytest soak only simulates 40 hours of readings and checks memory.

## Manual Windows checklist
Win+D, three-finger swipe, Alt+Tab (widget absent), fullscreen video, two monitors, 125% and 150%
scaling, lock/unlock, sleep/wake, remote desktop, unplug/replug network, Esc and idle exit from edit
mode, a HIGH pop-up over a browser, and: Alt+Shift+W from inside another app (does the widget get
focus?), click-through in normal mode, widget returns to the desktop layer after edit mode and after
a pop-up, toasts appear and HIGH toasts stay, sounds play, outside US hours the widget is silent.

## Decisions I made where the blueprint was unclear (section 14 rule)
**Hysteresis (changed after review):** a zone is entered exactly at its stated price and left only
after price falls back by the band, on the exit edge only: Loss/Deep loss are kept on their upper
edge, Sell/High sell on their lower edge, Breakeven (and the HOLD no-tag zone) on both, Entry hit and
Near entry on their upper edge, Far above on its lower edge. Example: Sell starts at 207.46 and is
left below 206.44 (P +2.5). The earlier version widened the zone you were in on every side, which
delayed entering the next zone by up to half a point.

**Slipping alert:** every SELL stock gets `<ticker>-slip` (RISK, MEDIUM, "leaves the sell range")
automatically, but only when a SELL stock is added in the app or a stock is switched to SELL in the
editor. It is never created when watchlist.json is loaded, so if you delete it, it stays deleted
after a restart (and a stock that already has a sell-range exit alert does not get a second one).

Step A items 1-14 are unchanged (unrounded P for zone decisions, Slipping = leaving the whole sell
range, noisy confirmation, "first reading" rule, cross-alert and daily-move behaviour, jump filter,
stale clock, privacy-test scope, fee percents in config, holiday list). Step B additions:
1. **Mute silences sounds and toasts only.** A HIGH pop-up still appears (silently, closes after 10 s,
   repeats once after 2 minutes if not acknowledged). A MEDIUM alert while muted only flashes its
   row and is logged. Quiet hours behave the same way: pop-ups appear, silently.
2. **Edit mode puts the widget above other windows** (it must take clicks and focus); leaving it
   returns it to the desktop layer.
3. **`hotkeys_enabled: false` also disables Alt+Shift+W**, so edit mode is then unreachable. Edit
   watchlist.json by hand and restart, or set it back to true. It also turns off the Alt+W peek and
   quit hook.
4. **Alt+A and Alt+M are global**; if another program owns them, a warning appears on the widget.
5. **Provider market status** is used when it answered in the last 30 minutes (isOpen false = closed,
   e.g. an unexpected closure); otherwise the bundled calendar decides.
6. **One notification per poll**: all alerts from one poll (all stocks) are logged individually, then
   merged into one notification, highest priority first.
7. **Daily summary** uses the last regular-session reading per stock (kept in state.json), so after-hours
   polls, a restart or a wake cannot overwrite the close values. The card contains estimated net P/L
   by design; it lives in `summaries\`, not in the alert log. No card is built for a day with no
   regular-session reading.
8. **Wake / unlock / remote reconnect / a 30 s timer gap** poll immediately and restart the stale
   clock, so sleeping never produces a "feed stale" alert; alerts only run on readings that arrive
   afterwards.
9. **Add stock** prefills BUY entry with the live price; zone thresholds come from the defaults. Add
   alerts in the expanded section that opens after adding.
10. **The editor stores only what differs from the defaults** (thresholds, priorities, switches).
11. **Editing an alert resets its state** (back to ARMED, or the choice you make on the "would fire
    now" prompt). Unchanged alerts keep their state.
12. **Toasts**: HIGH uses the Windows "alarm" scenario (stays until dismissed); the toast itself is
    silent because the app plays the sound. winotify is used if windows-toasts fails. Toasts are sent
    from a background thread so a slow toast can never freeze the widget or delay alerts.
13. **Log redaction**: app.log masks the API key and any `avg_price`, `shares`, `actual_buy_fee` or
    cost-basis values (also inside tracebacks), as a second line of defence.

## Open issues / things to verify
* Everything in "NOT tested" above. If a Windows-specific piece misbehaves, send me the symptom and
  `%APPDATA%\StockAlertWidget\app.log` (it never contains the key or your positions).
* Verify the 2026 holiday list and Finnhub's free-tier limits on their sites before relying on it.
* Python 3.14.5rc1 is a release candidate; the blueprint targets 3.11/3.12. All pinned packages have
  3.14 Windows wheels, but if something odd appears, try stable 3.12 first.
* Not built (v2, per blueprint): combined rules, position closing/realized P&L, cross/percent-move
  alerts in the editor, tray icon, CSV export, WebSocket, second provider, extended hours, installer.
