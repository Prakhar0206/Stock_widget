# Stock Alert Widget

[![Platform](https://img.shields.io/badge/Platform-Windows%2010%20%7C%2011-0078D6?logo=windows&logoColor=white)](https://microsoft.com/windows)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://python.org)
[![Tests](https://img.shields.io/badge/Tests-285%20Passed-brightgreen?logo=pytest&logoColor=white)](https://pytest.org)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Zero Cloud](https://img.shields.io/badge/Privacy-100%25%20Local%20%26%20Redacted-success)](README.md#privacy--security)

An ultra-responsive, privacy-focused desktop Heads-Up Display (HUD) and alert engine for US equities. It lives unobtrusively on your Windows desktop wallpaper, monitors Pre-Market (`PH`), Regular Hours (`RH`), and After-Hours (`AH`) sessions across a 4-key Finnhub pool every 15 seconds, and delivers customizable TradingView-style price and percentage alerts with multi-tier desktop pop-ups, toasts, and audio alarms.

> **Disclaimer**: This is an **alert-only** notification tool. It does **not** connect to any brokerage, never places orders, and does not provide financial or trading advice.

---

## Highlights & Features

### 🕵️ Privacy Hide / Show Toggle (`Alt + Shift + H`)
* **Instant Screen Privacy**: Press **`Alt + Shift + H`** anywhere in Windows—or click **`Hide`** in Edit Mode—to immediately hide both the Main Widget and the Floating Mini Tracker when someone is looking at your screen.
* **Always Active in Background**: Quote polling, alert evaluation, audio alarms, Windows toasts, and alert pop-ups continue running normally while hidden.
* **Quick Glance While Hidden (`Alt + W`)**: Hold **`Alt + W`** at any time while hidden to temporarily peek the Main Widget on top with the latest refreshed prices; releasing **`Alt + W`** hides it immediately again (Mini Tracker stays hidden).
* **One-Key Restore**: Press **`Alt + Shift + H`** again (or `Alt + Shift + W` for Edit Mode) to restore the Main Widget and reopen the Mini Tracker if it was active before hiding.

### 🎯 TradingView-Style Alert System
* **Immediate Atomic Persistence**: Creating, editing, toggling, or deleting an alert saves immediately to `watchlist.json` and `state.json`—never lost if you close Edit Mode without a second save click.
* **`Price ($)` and `Percent (%)` Conditions**:
  * **`Price ($)`**: Trigger against explicit dollar thresholds or upper/lower channel bounds.
  * **`Percent (%)`**: Calculates targets against your manual **`Buy Price`** (with optional manual `(buy_price * shares + fees) / shares` adjustment for `HOLD`/`SELL`), **`Sell Price`**, or a clean fallback to **`Previous Close`** (e.g., on `WATCH` stocks) with live `$` target previews.
* **All 13 Operators**:
  * **Primary**: `Crossing`, `Crossing Up`, `Crossing Down`, `Greater Than`, `Less Than`.
  * **Expanded (`Show more ▾`)**: `Entering Channel`, `Exiting Channel`, `Inside Channel`, `Outside Channel`, `Moving Up ($)`, `Moving Down ($)`, `Moving Up %`, `Moving Down %` (over a configurable time window).
* **Trigger Modes, Expirations & Session Scopes**:
  * **Triggers**: `Once only` (auto-disarms when hit; 1-click re-enable), `Every time` (with configurable minute cooldown), or `Once per cross (Re-arm)` (re-arms automatically after hysteresis cross-back).
  * **Expirations**: `Open-ended`, `End of trading session`, `End of next trading session`, `1 week`, `1 month`, or `Custom date & time`.
  * **Session Scope**: Restrict any alert to `All Sessions (PH + RH + AH)`, `Regular Hours Only (RH)`, or `Extended Hours Only (PH + AH)`.
* **Custom Messages & Optional Manual Trade Fields**:
  * Customize optional alert names and templates under **`< Edit message`** with clickable placeholder chips (`{{ticker}}`, `{{price}}`, `{{target}}`, `{{change_pct}}`, `{{session}}`, `{{time}}`).
  * Tag alerts with `BUY`, `HOLD`, `SELL`, `WATCH`, or `RISK`.
  * Optional manual trade fields (`Buy Price`, `Shares`, `Sell Price`, `Fees`) on any category (`BUY`, `HOLD`, `SELL`, `WATCH`), plus an optional 2-reading confirmation (`Noisy stock`) filter to suppress 1-tick spikes.

### 🔔 Shifted Multi-Tier Notifications & Centered Alert Card
* **`LOW` (Standard)**: Row highlight flash + Mini Tracker alert pill + Windows Toast + single chime (no pop-up).
* **`MEDIUM` (Important)**: Row highlight flash + Mini Tracker alert pill + centered 10s dark-card Desktop Overlay Popup + Windows Toast + 5s alert sound.
* **`HIGH` (Persistent Alarm)**: Row highlight flash + Mini Tracker critical pill (`🚨`) + centered 10s dark-card Desktop Overlay Popup + persistent Windows Toast + 10s alarm sound + **auto-repeats every 2 minutes until acknowledged** (`Alt + A`, popup **Acknowledge** / `✕`, or clicking the Mini Tracker alert pill).
* **`CUSTOM...`**: Open **`< Notifications`** to pick bespoke combinations of Desktop Popup, Windows Toast, Sound Tone (`none`, `chime`, `beep`, `bell`, `alarm`), Sound Duration (`Once`, `5s`, `10s`, `30s`), and Repeat-until-acknowledged.
* **Centered Dark-Card Pop-Up & Diagnostics**: Styled priority card (`alert` strip, ticker/price hero row, condition summary, source/data age) centered on the active monitor work area without stealing focus, plus a **`Test Alert`** button in Edit Mode and optional **IST Quiet Hours**.

### 📥 Unacknowledged Alerts Inbox & Offline Catch-Up
* **Pure Unacknowledged Inbox**: Collects only missed alerts awaiting your attention—live alerts whose 10s pop-ups timed out while you were away plus alerts triggered while your laptop was closed or the widget was off (no "All History" UI clutter; `alerts_log.jsonl` remains on disk).
* **Async Background Offline Catch-Up**: On startup and laptop wake (`on_wake`), asynchronously fetches missed 1-minute Yahoo Finance OHLC candles across `PH`, `RH`, and `AH` (up to 5 trading days since `last_active_at`) on a background worker thread so the UI never freezes.
* **Single Summary Notification**: Offline catch-up lights up the `🔔 N` badge and fires one single summary pop-up/toast/chime (`offline_summary`). Dismissing that summary pop-up (`Alt + A` / **Acknowledge**) never clears individual unread offline cards in the inbox, and failed wake fetches automatically retry once live quotes reconnect.
* **Offline Peak vs Live Price & 1-Click Re-Arm**: Deduplicates repeated hits during an offline gap to one card per rule showing first trigger time/price, extreme **Peak** (when moved past trigger), and live **Now** price comparison. Any `Once only` alert hit while offline transitions to `DONE` and offers a 1-click **`↻ Re-arm`** button right on the inbox card.
* **Clean Single-Bar UI & Quick Controls**: Top bar with unacknowledged counter, **`✓ Acknowledge All`**, and **`Close`** (auto-hides scrollbar when short), plus **`↻` Restart** and **`Alerts (N)`** buttons in Edit Mode, Main Widget **`🔔 N`** header badge, and Mini Tracker **`🔔 N`** header button / right-click.

### 🪟 Floating Mini Tracker HUD (Always-on-Top)
* **Translucent Dark Panel**: Compact, always-on-top floating companion matching the main widget palette (`#202020`/`#2D2D2D` dark cards with `#444444` outlines and 92% opacity), freely draggable with saved monitor-clamped coordinates.
* **In-Place Updates & Overflow Indicators**: Shows `Symbol`, latest `Price`, directional arrow, and `% Change` with flicker-free in-place updates and clickable `▲` / `▼` overflow indicators on long watchlists.
* **Dynamic Alert Pill & Hover Summary**: Center header turns into a colored alert pill (`🔔` / `🚨`) displaying the live alert count, a hover tooltip with the latest alert details, and 1-click acknowledgment.
* **Double-Click to Edit**: Double-clicking any stock row opens Edit Mode directly expanded to that stock.
* **Category & Unlimited Ticker Checklist**: Filter on the fly with category pills (`ALL`, `SELL`, `HOLD`, `BUY`, `WATCH`) and an unlimited scrollable ticker checklist (empty selection shows all stocks).
* **Header Mini Switch & Settings Gear (`⚙`)**: Toggle the Mini Tracker directly from the `Mini` pill switch on the far right of the main widget header, or click the adjacent `⚙` icon to open Edit Mode.

### 🕒 Extended Hours Market Support (`PH` / `RH` / `AH`)
* **Separate Session Columns**: The main desktop HUD and Edit Mode display dedicated Pre-Market (`PH`), Regular Hours (`RH`), and After-Hours (`AH`) columns alongside the latest directional change (`▲`/`▼` `%`). Active session numbers highlight while closed markets switch to a calm, uniform silver/muted off-hours palette.
* **Yahoo Finance Supplementation**: When `extended_hours` is enabled in `Config` (on by default), Yahoo Finance extended-hours chart metadata and 1-minute extended-session bars supplement Finnhub quotes so `PH`, `RH`, and `AH` display live extended prices and retain the last trading day's session closes across `CLOSED`, `PRE`, `REGULAR`, and `AFTER` (with 15-minute cached backfill during `REGULAR`).

### 🚀 High-Frequency Finnhub Key Pool (Balanced 15s Updates)
* **Equal Round-Robin Rotation**: Distributes both quote batches and `/stock/market-status` checks evenly across up to 4 Finnhub free-tier keys, maintaining a steady post-fetch **15-second live cadence** ($4 \times 48 = 192$ calls/min budget).
* **In-Slot Partial Failover**: If a key throttles partway through a batch (e.g., after 6 of 10 symbols), partial quotes are kept and the remaining symbols immediately fail over to the next healthy key in the same slot.
* **Per-Key Progressive Backoff & Read-Only Status Modal**: Exponential 429 cooldowns (`60s` $\to$ `120s` $\to$ `240s`), 401/403 rejected key isolation with automatic recovery, and a read-only **API Keys & Status** viewer in Edit Mode showing key source, pool health, and per-key call counts.

### 🖥️ Native Windows Desktop & Status Integration
* **Wallpaper Layer Pinning & `Win+D` Immunity**: Lives on the Windows desktop layer (`WorkerW`/`Progman`) beneath active windows, scales across high-DPI & multi-monitor setups, and supports optional hover tooltips (`click_through: false` by default).
* **Dynamic Status Strip**: Session icons (`🌅` Pre-Market, `☀️` Regular Open, `🌇` After-Hours, `🌙` Closed), live IST/ET clocks, 30 FPS expanding sonar pulse heartbeat, rich hover tooltips, and specific problem badges (`🚫📶` offline, `⏳` rate-limited keys, `🔑❌` rejected keys, `☁️❌` upstream 5xx/timeout).

### 🔒 Privacy & Security
* **100% Local Data**: Zero telemetry or cloud storage. Connects only to Finnhub's quote/status API (plus optional public Yahoo Finance extended-hours chart metadata when `extended_hours` is enabled). All user state lives locally in `%APPDATA%\StockAlertWidget`.
* **Automatic Redaction & Log Archiving**: API keys (headers, query params, env/config keys) and trade financial figures (`shares`, cost fields) are scrubbed from `app.log`, excluded from `alerts_log.jsonl`, and alert logs older than 60 days automatically archive into `alerts_archive/alerts_log_YYYY-MM.jsonl`.

---

## Quick Start

### Option 1: One-Click Launcher (`run_stock_alert_widget.bat`)

The included batch launcher automatically configures Python, sets up a `.venv` virtual environment, installs dependencies, and runs the widget in the background:

```cmd
:: 1. Launch the widget in the background
run_stock_alert_widget.bat

:: Or try demo mode right away (scripted prices, no API keys needed)
run_stock_alert_widget.bat demo
```

### Option 2: Manual PowerShell Setup

```powershell
# 1. Clone repository & change directory
git clone https://github.com/Prakhar0206/Stock_widget.git
cd Stock_widget

# 2. Create and activate virtual environment
py -m venv .venv
.venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set your Finnhub API Key(s)
setx FINNHUB_API_KEY "your-finnhub-key"

# 5. Launch the widget in the background
pythonw main.py
```

---

## Finnhub API Key Pool Configuration

For a single key, the widget safely polls once every 60 seconds. To enable **15-second slot updates**, configure 4 Finnhub keys.

You can set them globally via PowerShell environment variables (recommended) or in your `config.json`:

### Via Windows Environment Variables (Recommended)
Open PowerShell and set your 4 keys either comma-separated (recommended) or individually:

```powershell
# Option A: Comma-separated (easiest)
setx FINNHUB_API_KEY "key1,key2,key3,key4"

# Option B: Individual numbered variables
setx FINNHUB_API_KEY_1 "your_finnhub_key_1"
setx FINNHUB_API_KEY_2 "your_finnhub_key_2"
setx FINNHUB_API_KEY_3 "your_finnhub_key_3"
setx FINNHUB_API_KEY_4 "your_finnhub_key_4"
```
*(After running `setx`, reopen your terminal or launcher so the new environment variables take effect).*

### Via `config.json`
Alternatively, specify the keys in `%APPDATA%\StockAlertWidget\config.json`:
```json
{
  "poll_seconds": 15,
  "api_keys": [
    "your_finnhub_key_1",
    "your_finnhub_key_2",
    "your_finnhub_key_3",
    "your_finnhub_key_4"
  ]
}
```

---

## Keyboard Shortcuts

| Shortcut | Action | Description |
|---|---|---|
| **`Alt + Shift + H`** | **Privacy Hide / Show** | Instantly hides or restores both the Main Widget and Mini Tracker while keeping background polling and alerts running. *(Also available via the **Hide** button in Edit Mode).* |
| **`Alt + Shift + W`** | **Toggle Edit Mode** | Brings the widget above other windows (unhiding if hidden) and opens the watchlist & alert manager. `Esc` or 60s of inactivity exits edit mode. |
| **`Alt + A`** | **Acknowledge Alert** | Acknowledges the latest active alert and cancels repeating `HIGH` alarm reminders. |
| **`Alt + M`** | **Toggle Mute** | Instantly mutes/unmutes audio chimes and Windows toasts. |
| **`Alt + W` (Hold)** | **Peek Widget** | Temporarily brings the Main Widget above all open windows with live prices while held (works even while privacy-hidden, hiding again on release). |
| **`Ctrl + Alt + Shift + Q`** | **Force Quit** | Closes the widget and cleanly releases all system hooks and mutexes. |

---

## Batch Launcher Commands

The `run_stock_alert_widget.bat` script provides a full suite of management commands:

| Command | Description |
|---|---|
| `run_stock_alert_widget.bat` | Starts the widget in the background via `pythonw.exe` (no console window). |
| `run_stock_alert_widget.bat demo` | Starts in demo mode (`--mock`) with scripted price swings across a safe isolated folder. |
| `run_stock_alert_widget.bat debug` | Starts in foreground console mode to display live logs and tracebacks. |
| `run_stock_alert_widget.bat test` | Runs the full 285-test automated `pytest` suite (installs `requirements-dev.txt` if needed). |
| `run_stock_alert_widget.bat startup status` | Checks if the Windows Startup shortcut is configured. |
| `run_stock_alert_widget.bat startup create` | Configures the widget to automatically launch on Windows login. |
| `run_stock_alert_widget.bat startup remove` | Removes the Windows Startup shortcut. |
| `run_stock_alert_widget.bat logs` | Opens the `%APPDATA%\StockAlertWidget` data folder in File Explorer. |
| `run_stock_alert_widget.bat update` | Re-checks and updates all dependencies from `requirements.txt`. |

---

## Architecture & Codebase Overview

```text
stock_alert_widget/
├── main.py                  # Entry point, runtime builder, global hotkeys, single-instance mutex
├── app_core.py              # Main application controller, alert/tick orchestration, status strip views
├── desktop_layer.py         # Win32 desktop pinning, Progman/WorkerW hooks, peek, hide/show, z-order
├── scheduler.py             # Polling scheduler, post-fetch cadence alignment, off-hours sleep
├── market_clock.py          # US market hours, NYSE rule calendar, session & expiration helpers
├── config.py                # Config parser, validation, multi-key discovery, API budget calculator
├── models.py                # Domain dataclasses & constants (Alert, TradeInfo, Notification, PriceTick)
├── engine.py                # TradingView-style 13-operator alert state machine & target resolver
├── watchlist.py             # Watchlist parser/serializer with legacy migration & budget validation
├── positions.py             # Decimal trade summary & percentage target math (no guessed fees)
├── data_checks.py           # Price sanity jump filter, feed health tracker, exponential backoff
├── messages.py              # Alert & system warning message formatting with template placeholders
├── notifier.py              # Shifted priority delivery (LOW/MEDIUM/HIGH/CUSTOM), sound, toast & popup
├── store.py                 # Crash-safe atomic JSON writes, alert log pruning/archiving, PII redaction
├── providers/
│   ├── base.py              # QuoteProvider abstract interface & provider error hierarchy
│   ├── finnhub.py           # Multi-key round-robin rotation, in-slot failover, 429 backoff, Yahoo PH/AH
│   └── mock.py              # Demo/mock provider with scripted price trajectories
├── tools/
│   ├── startup_shortcut.py  # Windows Startup folder shortcut status/create/remove CLI
│   └── make_sample_day.py   # Sample fixture generator for replay/soak verification
└── ui/
    ├── widget.py            # Dark-card Canvas HUD, PH/RH/AH columns, sonar pulse, Inbox & Mini switches, EditPanel
    ├── mini_tracker.py      # Floating always-on-top companion HUD, Inbox button, overflow indicators, Filter dialog
    ├── inbox.py             # Clean unacknowledged Alerts Inbox dialog (Peak vs Now, Re-arm, live ack sync)
    ├── editor.py            # Edit mode StockEditor, TradingView AlertDialog, AddStock & API Keys modals
    ├── popup.py             # Centered dark-card desktop alert overlay pop-up
    └── common.py            # Shared dark palette, Tkinter timers & reusable form widgets
```

---

## Data & Configuration

All runtime and persistent user data is kept isolated in `%APPDATA%\StockAlertWidget`:

| File | Purpose |
|---|---|
| `config.json` | General settings (`poll_seconds`, `extended_hours`, `default_priority`, `hotkeys_enabled`), `look` appearance tokens, optional `api_keys`, and `quiet_hours_ist`. |
| `watchlist.json` | Current stock watchlist, optional manual trade details (`buy_price`, `shares`, `sell_price`, `fees`), and alert rules. |
| `watchlist.last_good.json` | Automatic fallback copy preserved across atomic writes. |
| `state.json` | Runtime state, `PH`/`RH`/`AH` session prices, alert arming/cooldown states, `last_active_at`, `unacked_alerts`, and Mini Tracker layout preferences. |
| `alerts_log.jsonl` | Append-only alert audit log (redacted: no shares, cost basis, or profits). |
| `alerts_archive/` | Monthly archives (`alerts_log_YYYY-MM.jsonl`) created when logs older than 60 days are pruned on startup. |
| `app.log` | Rotating operational log (1 MB $\times$ 3 files, strict key/PII redaction). |

---

## Testing & Quality Assurance

The codebase includes an automated test suite covering all business logic, 13 alert operators, shifted notifications, multi-key rotation, privacy hiding, and Tkinter UI flows under fake Win32 layers.

To run the full suite (install dev dependencies from `requirements-dev.txt` first):
```powershell
python -m pytest
```

```text
============================= test session starts =============================
platform win32 -- Python 3.14.x, pytest-9.x.x
collected 285 items

tests/test_alert_inbox_and_catchup.py ..........                         [  3%]
tests/test_app_core.py ..................                                [  9%]
tests/test_audit_and_extended_hours.py ................................. [ 21%]
tests/test_config_multikey.py ..................                         [ 27%]
tests/test_config_store.py .....................                         [ 35%]
tests/test_data_checks.py .......                                        [ 37%]
tests/test_desktop_layer_stub.py ..........                              [ 41%]
tests/test_engine.py ...........                                         [ 44%]
tests/test_finnhub_pool.py ............................                  [ 54%]
tests/test_finnhub_scheduler.py ...................                      [ 61%]
tests/test_main_demo.py ........                                         [ 64%]
tests/test_main_multikey.py .........                                    [ 67%]
tests/test_market_clock.py ......                                        [ 69%]
tests/test_market_clock_rules.py ........                                [ 72%]
tests/test_mini_tracker.py .............................                 [ 82%]
tests/test_notifier.py ...........                                       [ 86%]
tests/test_pipeline.py .....                                             [ 88%]
tests/test_positions.py .....                                            [ 89%]
tests/test_privacy.py ......                                             [ 91%]
tests/test_replay_soak.py ..                                             [ 92%]
tests/test_startup_shortcut.py ..                                        [ 93%]
tests/test_ui_smoke.py ...................                               [100%]

============================ 285 passed =======================================
```

---

## License

This project is licensed under the [MIT License](LICENSE) - see the LICENSE file for details.
