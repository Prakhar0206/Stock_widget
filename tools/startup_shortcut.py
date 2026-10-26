"""Create or remove a Startup-folder shortcut that starts the widget at login (no console window).
    python tools\\startup_shortcut.py create     (alias: --install)
    python tools\\startup_shortcut.py remove     (alias: --remove)
    python tools\\startup_shortcut.py status     (alias: --status)
Not enabled by default; run it yourself if you want autostart."""
import os
import subprocess
import sys
from pathlib import Path

NAME = "Stock Alert Widget.lnk"
ROOT = Path(__file__).resolve().parent.parent
ALIASES = {"--install": "create", "--remove": "remove", "--status": "status"}


def startup_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def shortcut_path() -> Path:
    return startup_dir() / NAME


def create() -> Path:
    pyw = Path(sys.executable).with_name("pythonw.exe")
    if not pyw.exists():
        sys.exit(f"pythonw.exe not found next to {sys.executable}")
    lnk = shortcut_path()
    lnk.parent.mkdir(parents=True, exist_ok=True)
    # paths go through the environment, not into the PowerShell text, so apostrophes in a path are safe
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:SAW_LNK);"
          "$s.TargetPath=$env:SAW_TARGET;$s.Arguments=$env:SAW_ARGS;$s.WorkingDirectory=$env:SAW_DIR;"
          "$s.Description='Stock Alert Widget';$s.Save()")
    env = dict(os.environ, SAW_LNK=str(lnk), SAW_TARGET=str(pyw), SAW_ARGS=f'"{ROOT / "main.py"}"', SAW_DIR=str(ROOT))
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], check=True, env=env)
    return lnk


def remove() -> bool:
    lnk = shortcut_path()
    if lnk.exists():
        lnk.unlink()
        return True
    return False


def status() -> str:
    lnk = shortcut_path()
    return f"Startup shortcut ({lnk}): {'INSTALLED' if lnk.exists() else 'NOT INSTALLED'}"


def main(argv=None) -> int:
    argv = [ALIASES.get(a, a) for a in (sys.argv[1:] if argv is None else argv)]
    if len(argv) != 1 or argv[0] not in ("create", "remove", "status"):
        print("usage: python tools\\startup_shortcut.py create|remove|status")
        return 2
    cmd = argv[0]
    if cmd == "status":
        print(status())
    elif cmd == "remove":
        print(f"Startup shortcut {'removed' if remove() else 'was not present'}: {shortcut_path()}")
    else:
        if sys.platform != "win32":
            print("Creating the shortcut only works on Windows.")
            return 1
        print(f"Created {create()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
