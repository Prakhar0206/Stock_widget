# Stock Alert Widget

[![Platform](https://img.shields.io/badge/Platform-Windows%2010%20%7C%2011-0078D6?logo=windows&logoColor=white)](https://microsoft.com/windows)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://python.org)
[![Tests](https://img.shields.io/badge/Tests-280%20Passed-brightgreen?logo=pytest&logoColor=white)](https://pytest.org)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Zero Cloud](https://img.shields.io/badge/Privacy-100%25%20Local%20%26%20Redacted-success)](README.md#privacy--security)

An ultra-responsive, privacy-focused desktop Heads-Up Display (HUD) and alert engine for US equities. It lives unobtrusively on your Windows desktop wallpaper, monitors regular and extended market sessions, rotates across a pool of up to 4 Finnhub API keys every 15 seconds, and alerts you the moment prices enter your target buy/sell zones or custom price triggers.

> **Disclaimer**: This is an **alert-only** notification tool. It does **not** connect to any brokerage, never places orders, and does not provide financial or trading advice.

---

## Highlights & Features

### 🪟 Floating Mini Tracker HUD (Always-on-Top)
* **Compact Floating Companion**: A sleek, bottom-right floating widget showing real-time quotes (`Symbol`, `Price`, `Arrow`, `Change %`) without taking desktop space.
* **Selective Click-Through Pinning**: When pinned (📌), the stock rows become click-through so you can interact with apps behind them, while the header stays interactive for unpinning, settings, and controls.
* **Dynamic Alert Pill**: The center header turns into an animated alert pill (`[ 🔔 1 Alert ]` / `[ 🚨 1 Alert ]`) that lets you acknowledge active alerts and silence chimes with a single click.
* **Category & Ticker Filters**: Filter on the fly with quick category buttons (`[All]`, `[SELL]`, `[HOLD]`, `[BUY]`, `[WATCH]`) and ticker checkboxes (limit 2–5).
* **Instant Toggle**: Toggle the mini tracker via the `[◱ Mini]` tag button on the main wallpaper status bar or from the Edit Mode header.

### 🚀 High-Frequency Finnhub Key Pool (15s Updates)
* **4-Key Round-Robin Rotation**: Distributes quote fetches across up to 4 Finnhub free-tier keys, unlocking **15-second live updates** during regular market hours ($4 \times 48 = 192$ calls/min budget, safely within the 30-symbol allowance).
* **In-Slot Partial Failover**: If a key throttles partway through a batch (e.g., after 6 of 10 symbols), the received quotes are retained and the remaining 4 symbols immediately fail over to the next healthy key in the pool.
* **Per-Key Progressive Backoff**: Exponential rate-limit handling (60s $\to$ 120s $\to$ 240s) per key on HTTP 429 without stalling the entire feed.
* **Rejected Key Isolation**: Distinguishes between rate limits (429) and invalid/expired keys (401/403), quarantining bad keys and automatically recovering when credentials are corrected.

### 🖥️ Native Windows Desktop Integration
* **Wallpaper Layer Pinning**: Resides on the Windows desktop layer (`WorkerW`/`Progman`) beneath active apps.
* **Win+D Immune**: Stays in place when minimizing all windows with `Win+D` or switching virtual desktops.
* **Non-Intrusive Normal Mode**: Configurable click-through or hover-enabled normal mode (`click_through: false`), allowing rich interactive tooltips without stealing focus from your work.
* **High-DPI & Multi-Monitor**: Scales cleanly across 100%, 125%, 150%, and multi-monitor setups.

### 📊 Dynamic Status Bar & Visual Feedback
* **Market Session Indicator**: Real-time ☀️ Regular Session vs. 🌙 After-Hours/Weekend/Holiday status with automatic opening bell wake-up.
* **Expanding Sonar Pulse Heartbeat**: 30 FPS multi-frame ripple animation visually confirms live scheduler ticks.
* **Rich Hover Tooltips**: Hover over status badges in normal mode to inspect market hours, schedule intervals, and active provider health.
* **Problem Badges**: Dedicated mini-icons indicate live network disconnections (🔌), rate-limiting (⚡), invalid API keys (🔑), and stale feeds (⏳).

### 🎯 Smart Target Zones & Portfolio Tracking
* **SELL Stocks**: Tracks unrealized P/L factoring in estimated buy/sell commissions and SEC/FINRA regulatory fees. Dynamically categorizes price action into 7 color-coded zones:
  - `DEEP LOSS` / `LOSS`
  - `BELOW BREAKEVEN` / `BREAKEVEN` / `ABOVE BREAKEVEN`
  - `SELL` / `HIGH SELL`
* **Automatic Slipping Protection**: Automatically arms a medium-priority alert if a stock in your sell target range reverses and slips below your threshold.
* **BUY Stocks**: Monitors watchlist entry opportunities with 5 zones (`DEEP DROP`, `ENTRY HIT`, `NEAR ENTRY`, `ABOVE ENTRY`, `FAR ABOVE`).
* **Hysteresis Filtering**: Prevents alert noise and jitter at zone boundaries using configurable price bands (`standard: 0.5%`, `noisy: 1.0%`).

### 🔔 Multi-Tier Notifications
* **Desktop Pop-up HUD**: Clean overlay card for HIGH priority alerts that appears above all windows and auto-dismisses after 10s (re-escalates after 2 minutes if unacknowledged).
* **Native Windows Toasts**: Action Center integration with persistent alarm scenario for urgent alerts.
* **Non-Blocking Audio**: Distinct sound chimes played from background threads so toasts and sounds never freeze the UI.
* **IST Quiet Hours**: Optional mute schedule configured for after-hours focus.

### 🔒 100% Private, Local-Only & Masked
* **Zero Telemetry**: No cloud database, no tracking, and no external connections other than Finnhub quotes.
* **Strict Redaction**: API keys, share quantities, cost bases, and actual dollar profits are automatically scrubbed from `app.log` and `alerts_log.jsonl`.
* **Local Data**: All configuration and state files reside strictly in `%APPDATA%\StockAlertWidget`.

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
| **`Alt + Shift + W`** | **Toggle Edit Mode** | Brings the widget above other windows and opens the interactive watchlist manager. Pressing `Esc` or 60s of inactivity exits edit mode. |
| **`Alt + A`** | **Acknowledge Alert** | Acknowledges the latest active alert and cancels repeating pop-ups. |
| **`Alt + M`** | **Toggle Mute** | Instantly mutes/unmutes audio chimes and notifications. |
| **`Alt + W` (Hold)** | **Peek Widget** | Temporarily brings the desktop widget above all open windows while held. |
| **`Ctrl + Alt + Shift + Q`** | **Force Quit** | Closes the widget and cleanly releases all system hooks and mutexes. |

---

## Batch Launcher Commands

The `run_stock_alert_widget.bat` script provides a full suite of management commands:

| Command | Description |
|---|---|
| `run_stock_alert_widget.bat` | Starts the widget in the background via `pythonw.exe` (no console window). |
| `run_stock_alert_widget.bat demo` | Starts in demo mode (`--mock`) with simulated price swings through all zones. |
| `run_stock_alert_widget.bat debug` | Starts in foreground console mode to display live logs and tracebacks. |
| `run_stock_alert_widget.bat test` | Runs the full 280-test automated `pytest` suite. |
| `run_stock_alert_widget.bat startup status` | Checks if the Windows Startup shortcut is configured. |
| `run_stock_alert_widget.bat startup create` | Configures the widget to automatically launch on Windows login. |
| `run_stock_alert_widget.bat startup remove` | Removes the Windows Startup shortcut. |
| `run_stock_alert_widget.bat logs` | Opens the `%APPDATA%\StockAlertWidget` data folder in File Explorer. |
| `run_stock_alert_widget.bat update` | Re-checks and updates all dependencies from `requirements.txt`. |

---

## Architecture & Codebase Overview

```text
stock_alert_widget/
├── main.py                  # Entry point, runtime builder, Win32 single-instance mutex
├── app_core.py              # Main application controller, state machine, poll events
├── desktop_layer.py         # Win32 desktop pinning, Progman/WorkerW hooks, z-order locking
├── scheduler.py             # Polling scheduler, clock alignment, off-hours sleep
├── market_clock.py          # US market hours, calendar sessions, opening bell calculation
├── config.py                # Config parser, validation, 4-key budget calculator
├── engine.py                # Zone evaluation, hysteresis filter, alert trigger logic
├── watchlist.py             # Watchlist model, atomic read/write, backup recovery
├── positions.py             # Position calculations, buy/sell fees, net P/L estimates
├── notifier.py              # Windows toast integration, sound player, pop-up dispatcher
├── store.py                 # File storage, atomic JSON writes, privacy log filter
├── providers/
│   ├── finnhub.py           # Multi-key pool rotation, in-slot failover, 429 backoff
│   └── mock.py              # Demo/mock provider with scripted price trajectories
└── ui/
    ├── widget.py            # Canvas HUD view, sonar pulse animation, tooltips
    ├── mini_tracker.py      # Floating always-on-top HUD with selective click-through
    ├── editor.py            # Edit mode GUI, stock list, inline row expansion
    ├── dialogs.py           # Add stock & custom alert creation dialogs
    ├── popup.py             # Top-level desktop alert card
    └── common.py            # Reusable Tk timer abstractions & UI helpers
```

---

## Data & Configuration

All runtime and persistent user data is kept isolated in `%APPDATA%\StockAlertWidget`:

| File | Purpose |
|---|---|
| `config.json` | General settings, look & feel tokens, fee structures, and zone defaults. |
| `watchlist.json` | Current stock watchlist, positions, and active alert rules. |
| `watchlist.last_good.json` | Automatic fallback copy preserved across atomic writes. |
| `state.json` | Runtime state, last seen prices, and daily close benchmarks. |
| `alerts_log.jsonl` | Append-only alert log (redacted: no shares, cost basis, or profits). |
| `app.log` | Rotating operational log (1 MB $\times$ 3 files, strict key/PII redaction). |

---

## Testing & Quality Assurance

The codebase includes an extensive automated test suite covering all business logic, multi-key scheduling, backoff recovery, and UI smoke tests under fake desktop layers.

To run the full suite:
```powershell
python -m pytest
```

```text
============================= test session starts =============================
platform win32 -- Python 3.14.x, pytest-9.x.x
collected 249 items

tests/test_app_core.py .......................                           [  9%]
tests/test_config_multikey.py ..................                         [ 16%]
tests/test_config_store.py ...................                           [ 24%]
tests/test_data_checks.py .......                                        [ 26%]
tests/test_desktop_layer_stub.py .........                               [ 30%]
tests/test_engine.py .......................                             [ 39%]
tests/test_finnhub_pool.py ...........................                   [ 50%]
tests/test_finnhub_scheduler.py ...................                      [ 58%]
tests/test_main_demo.py ........                                         [ 61%]
tests/test_main_multikey.py .........                                    [ 65%]
tests/test_market_clock.py ......                                        [ 67%]
tests/test_market_clock_rules.py .......                                 [ 70%]
tests/test_notifier.py ................                                  [ 76%]
tests/test_pipeline.py .....                                             [ 78%]
tests/test_positions.py .......                                          [ 81%]
tests/test_privacy.py ......                                             [ 83%]
tests/test_replay_soak.py ..                                             [ 84%]
tests/test_startup_shortcut.py ..                                        [ 85%]
tests/test_ui_smoke.py ....................                              [ 93%]
tests/test_zones.py ................                                     [100%]

============================ 249 passed in 10.2s ==============================
```

---

## License

This project is licensed under the [MIT License](LICENSE) - see the LICENSE file for details.
