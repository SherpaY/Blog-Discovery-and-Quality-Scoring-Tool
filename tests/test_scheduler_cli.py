import plistlib

from scanner import scheduler
from scanner.cli import main


def test_launchd_plist_is_valid():
    data = plistlib.loads(scheduler.plist_content(6, 5, python="/Users/me/scanner/.venv/bin/python").encode())
    assert data["Label"] == scheduler.LABEL
    assert data["StartCalendarInterval"] == {"Hour": 6, "Minute": 5}
    assert data["ProgramArguments"][:4] == ["/Users/me/scanner/.venv/bin/python", "-m", "scanner", "scan"]
    assert "--auto-resume" in data["ProgramArguments"]  # resumes an interrupted scan
    assert data["StandardErrorPath"].endswith("launchd.err.log")


def test_cli_expand_labels_generated_queries(capsys):
    assert main(["expand", "technical seo audit"]) == 0
    out = capsys.readouterr().out
    assert "user keyword" in out and "generated: template variation" in out
