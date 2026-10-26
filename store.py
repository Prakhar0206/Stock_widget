"""Local JSON storage: atomic writes, last-good fallback, state, alert log (blueprint section 10)."""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from config import Config
from models import AlertEvent, Watchlist, ValidationError
from watchlist import dump_watchlist, parse_watchlist

log = logging.getLogger("saw.store")


def _default(o):
    if isinstance(o, Decimal):
        return float(o)
    raise TypeError(f"not JSON serializable: {type(o)}")


def _fsync_dir(folder: Path) -> None:
    """Best effort: make the rename itself durable (POSIX only; silently skipped elsewhere)."""
    try:
        fd = os.open(str(folder), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _parse_log_time(value) -> datetime:
    """ISO timestamp from a log record -> aware datetime (naive is taken as UTC)."""
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def atomic_write_text(path: Path, text: str) -> None:
    """Temp file in the same folder, flush + fsync, then replace. A crash never leaves half a file."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                import time
                time.sleep(0.02)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, default=_default, ensure_ascii=False) + "\n")


class Store:
    def __init__(self, folder: Path):
        self.dir = Path(folder)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "summaries").mkdir(exist_ok=True)
        self.watchlist_path = self.dir / "watchlist.json"
        self.last_good_path = self.dir / "watchlist.last_good.json"
        self.state_path = self.dir / "state.json"
        self.alert_log_path = self.dir / "alerts_log.jsonl"
        self.alert_archive_dir = self.dir / "alerts_archive"
        self._log_lock = threading.RLock()      # append vs. prune must not interleave (this process)

    # ---- watchlist -------------------------------------------------------------------------
    def load_watchlist(self, cfg: Config):
        """Returns (Watchlist, warnings). Invalid file -> last good copy + warning; never raises."""
        warnings = []
        if not self.watchlist_path.exists():
            if not self.last_good_path.exists():
                return Watchlist(), warnings
            warnings.append("watchlist.json is missing; loaded the last good copy.")
        else:
            try:
                wl = self._parse_file(self.watchlist_path, cfg)
                self._refresh_last_good(wl)
                return wl, warnings
            except Exception as exc:
                warnings.append(f"watchlist.json is invalid ({exc}); loaded the last good copy.")
                log.warning("watchlist.json invalid: %s", exc)
        try:
            return self._parse_file(self.last_good_path, cfg), warnings
        except Exception as exc:
            warnings.append(f"No usable last good watchlist either ({exc}); starting empty.")
            return Watchlist(), warnings

    @staticmethod
    def _parse_file(path: Path, cfg: Config) -> Watchlist:
        data = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
        return parse_watchlist(data, cfg)

    def _refresh_last_good(self, wl: Watchlist):
        try:
            text = json.dumps(dump_watchlist(wl), indent=2, default=_default, ensure_ascii=False) + "\n"
            try:
                if self.last_good_path.read_text(encoding="utf-8") == text:
                    return                       # unchanged: skip a pointless write + fsync
            except OSError:
                pass
            atomic_write_text(self.last_good_path, text)
        except OSError as exc:
            log.warning("could not refresh last good copy: %s", exc)

    def save_watchlist(self, wl: Watchlist, cfg: Config) -> None:
        """Validate then write atomically. On failure nothing is written and the error propagates."""
        data = dump_watchlist(wl)
        parse_watchlist(json.loads(json.dumps(data)), cfg)
        atomic_write_json(self.watchlist_path, data)
        atomic_write_json(self.last_good_path, data)

    # ---- state -----------------------------------------------------------------------------
    def load_state(self) -> dict:
        try:
            st = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(st, dict) and isinstance(st.get("symbols"), dict):
                return st
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("state.json unreadable (%s); starting fresh", exc)
        return {"version": 1, "symbols": {}}

    def save_state(self, state: dict) -> None:
        atomic_write_json(self.state_path, state)

    # ---- alert log (no shares, prices paid, fees or P/L) -------------------------------------
    def log_alert(self, ev: AlertEvent) -> None:
        rec = {"time": ev.time.astimezone(timezone.utc).isoformat(timespec="seconds"),
               "symbol": ev.symbol, "alert_id": ev.alert_id, "label": ev.label,
               "priority": ev.priority, "price": str(ev.price),
               "condition": ev.condition or ev.zone or ev.kind, "source": ev.source}
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with self._log_lock:
            with open(self.alert_log_path, "a", encoding="utf-8", newline="\n") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())

    def read_alert_log(self, since: datetime | None = None) -> list:
        """Parsed records, oldest first. Streams the file line by line and skips corrupt lines
        (e.g. one torn by a crash) instead of failing. `since` keeps only records at/after it."""
        if not self.alert_log_path.exists():
            return []
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        out = []
        with self._log_lock, open(self.alert_log_path, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    if since is not None and _parse_log_time(rec["time"]) < since:
                        continue
                except (ValueError, KeyError, TypeError):
                    log.warning("alerts_log.jsonl line %d unreadable; skipped", n)
                    continue
                out.append(rec)
        return out

    def prune_alert_log(self, max_age_days: int = 60, now: datetime | None = None,
                        archive: bool = False) -> int:
        """Drop alert records older than `max_age_days` (default 60); returns how many were removed.
        Kept lines are copied byte-for-byte, so the JSONL schema is untouched. The file is replaced
        atomically. With archive=True the removed lines are first appended to
        alerts_archive/alerts_log_YYYY-MM.jsonl (same fields only; nothing sensitive is in the log).
        Unreadable lines are discarded. Call at startup and/or after the daily summary."""
        if max_age_days < 0:
            raise ValueError("max_age_days must be >= 0")
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        cutoff = now - timedelta(days=max_age_days)
        with self._log_lock:
            if not self.alert_log_path.exists():
                return 0
            keep, old, bad = [], [], 0
            with open(self.alert_log_path, encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip("\r\n")
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                        t = _parse_log_time(rec["time"])
                    except (ValueError, KeyError, TypeError):
                        bad += 1
                        continue
                    (old if t < cutoff else keep).append((t, line))
            if not old and not bad:
                return 0
            if archive and old:
                self._archive_alerts(old)        # archive first: a crash can duplicate, never lose
            atomic_write_text(self.alert_log_path, "".join(l + "\n" for _, l in keep))
            if bad:
                log.warning("alerts_log.jsonl: discarded %d unreadable line(s) while pruning", bad)
            log.info("alerts_log.jsonl: pruned %d record(s) older than %d days", len(old), max_age_days)
            return len(old)

    def _archive_alerts(self, old: list) -> None:
        self.alert_archive_dir.mkdir(exist_ok=True)
        by_month: dict = {}
        for t, line in old:
            by_month.setdefault(t.strftime("%Y-%m"), []).append(line)
        for month, lines in by_month.items():
            with open(self.alert_archive_dir / f"alerts_log_{month}.jsonl", "a",
                      encoding="utf-8", newline="\n") as f:
                f.write("".join(l + "\n" for l in lines))
                f.flush()
                os.fsync(f.fileno())

    # ---- summaries ---------------------------------------------------------------------------
    def save_summary(self, date_str: str, card: dict) -> Path:
        if not date_str or any(c in date_str for c in "/\\") or ".." in date_str:
            raise ValueError(f"invalid summary date: {date_str!r}")
        p = self.dir / "summaries" / f"{date_str}.json"
        atomic_write_json(p, card)
        return p


_SENSITIVE_FIELD = re.compile(
    r"""(["']?)\b(avg_price|actual_buy_fee|total_cost_basis|cost_basis|shares)\1(\s*[:=]\s*)[^\s,;}\]]+""",
    re.IGNORECASE)
_TOKEN_HEADER = re.compile(r"(X-Finnhub-Token\W+)[^\s,;}'\"]+", re.IGNORECASE)
_TOKEN_QUERY = re.compile(r"([?&]token=)[^&\s'\"]+", re.IGNORECASE)


_EXTRA_KEYS_TO_REDACT: set[str] = set()


def register_keys_for_redaction(keys: list[str]) -> None:
    """Register keys (e.g. from config.json) so they are masked in app.log."""
    if not keys:
        return
    for k in keys:
        if isinstance(k, str):
            k_clean = k.strip().strip('"\'').strip()
            if len(k_clean) >= 4:
                _EXTRA_KEYS_TO_REDACT.add(k_clean)


def redact(text: str) -> str:
    """Second line of defence: the code never logs these, but if one ever slips into a message
    (or an exception text) it is masked before it reaches app.log."""
    if not text:
        return text
    all_keys = set(_EXTRA_KEYS_TO_REDACT)
    try:
        from config import get_api_keys
        all_keys.update(get_api_keys())
    except Exception:
        pass
    for env_var in ("FINNHUB_API_KEY", "FINNHUB_API_KEYS"):
        raw = os.environ.get(env_var, "")
        if raw:
            for part in re.split(r"[,;\s]+", raw):
                clean_k = part.strip().strip('"\'').strip()
                if len(clean_k) >= 4:
                    all_keys.add(clean_k)

    for key in sorted(all_keys, key=len, reverse=True):
        if key in text:
            text = text.replace(key, "[REDACTED_KEY]")
    text = _TOKEN_HEADER.sub(r"\1[REDACTED_KEY]", text)
    text = _TOKEN_QUERY.sub(r"\1[REDACTED_KEY]", text)
    return _SENSITIVE_FIELD.sub(lambda m: f"{m.group(2)}{m.group(3)}[REDACTED]", text)


class PrivacyLogFilter(logging.Filter):
    """Masks the API key and position fields in every record, including formatted tracebacks."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg, record.args = redact(record.getMessage()), ()
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = redact(record.exc_text)
            if record.stack_info:
                record.stack_info = redact(record.stack_info)
        except Exception:               # a logging filter must never raise
            pass
        return True


def setup_logging(folder: Path) -> None:
    """Rotating app.log, 1 MB x 3 files. Never log the API key (and the filter masks it anyway)."""
    h = logging.handlers.RotatingFileHandler(Path(folder) / "app.log", maxBytes=1_000_000,
                                             backupCount=2, encoding="utf-8")
    h.addFilter(PrivacyLogFilter())
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("saw")
    root.setLevel(logging.INFO)
    if not any(isinstance(x, logging.handlers.RotatingFileHandler) for x in root.handlers):
        root.addHandler(h)
