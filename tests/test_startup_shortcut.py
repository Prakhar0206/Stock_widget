import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "startup_shortcut", Path(__file__).resolve().parent.parent / "tools" / "startup_shortcut.py")
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)


def test_status_remove_and_flag_aliases(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert sc.main(["status"]) == 0
    assert "NOT INSTALLED" in capsys.readouterr().out
    lnk = sc.shortcut_path()
    lnk.parent.mkdir(parents=True)
    lnk.write_text("x")
    assert sc.main(["--status"]) == 0                                    # the other tool's flag style also works
    out = capsys.readouterr().out
    assert "INSTALLED" in out and "NOT INSTALLED" not in out
    assert sc.main(["--remove"]) == 0
    assert "removed" in capsys.readouterr().out and not lnk.exists()
    assert sc.main(["remove"]) == 0
    assert "was not present" in capsys.readouterr().out


def test_bad_usage_returns_2():
    assert sc.main([]) == 2 and sc.main(["nonsense"]) == 2 and sc.main(["status", "extra"]) == 2
