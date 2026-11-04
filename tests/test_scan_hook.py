"""The pre-commit hook and tools/scan_diff.py, in a throwaway git repo with fabricated data.

A commit goes through only on a CLEAN scan; findings, anything the scanner
cannot read, and a missing Python or scanner all refuse the commit.
"""
import json
import pathlib
import shutil
import subprocess
import sys

import duckdb
import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
DENYLIST = {"identity": ["Jane Example", "C:\\Users\\jexample"], "brokers": ["ExampleBroker"],
            "figures": ["123.45"], "tickers": ["EXTRA"]}
SECRET = "fakesecret987654"
ACCOUNT_ID = "aaaa1111-2222-3333-4444-555566667777"
ACCOUNT_NUMBER = "99887766"


def git(repo, *args, check=True):
    r = subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        *args], cwd=repo, capture_output=True, text=True)
    if check:
        assert r.returncode == 0, r.stdout + r.stderr
    return r


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "tools").mkdir(parents=True)
    (root / "githooks").mkdir()
    shutil.copy2(REPO / "tools" / "scan_diff.py", root / "tools" / "scan_diff.py")
    shutil.copy2(REPO / "githooks" / "pre-commit", root / "githooks" / "pre-commit")
    (root / ".gitignore").write_text("_private/\ndata/\n.env\narchiver/config.ini\n",
                                     encoding="utf-8")
    (root / "_private").mkdir()
    (root / "_private" / "scan_denylist.json").write_text(json.dumps(DENYLIST), encoding="utf-8")
    (root / ".env").write_text("SNAPTRADE_CONSUMER_KEY=%s\nDB_PATH=data/portfolio.duckdb\n"
                               % SECRET, encoding="utf-8")
    (root / "data" / "raw" / "snaptrade").mkdir(parents=True)
    (root / "data" / "raw" / "snaptrade" / "accounts.json").write_text(
        json.dumps([{"id": ACCOUNT_ID, "number": ACCOUNT_NUMBER}]), encoding="utf-8")
    con = duckdb.connect(str(root / "data" / "portfolio.duckdb"))
    con.execute("CREATE TABLE transactions (symbol VARCHAR)")
    con.execute("INSERT INTO transactions VALUES ('Q'), ('QQZ'), ('BTC')")
    con.execute("CREATE TABLE opening_balances (symbol VARCHAR)")
    con.execute("CREATE TABLE accounts (account_id VARCHAR)")
    con.execute("INSERT INTO accounts VALUES (?)", [ACCOUNT_ID])
    con.close()
    (root / "archiver").mkdir()
    (root / "archiver" / "config.ini").write_text("[fund FNDZ]\nissuer = manual\n",
                                                  encoding="utf-8")
    git(root, "init", "-q")
    git(root, "add", ".gitignore", "tools/scan_diff.py", "githooks/pre-commit")
    git(root, "commit", "-q", "-m", "setup")          # before the hook is switched on
    git(root, "config", "core.hooksPath", "githooks")
    git(root, "config", "hooks.python", sys.executable)
    return root


def commit(repo, text, name="notes.txt"):
    (repo / name).write_text(text + "\n", encoding="utf-8")
    git(repo, "add", name)
    r = git(repo, "commit", "-m", "change", check=False)
    return r.returncode == 0, r.stdout + r.stderr


def scan(repo):
    return subprocess.run([sys.executable, str(repo / "tools" / "scan_diff.py"), "--cached"],
                          cwd=repo, capture_output=True, text=True)


# ------------------------------------------------------------ verdicts
def test_clean_change_commits(repo):
    ok, out = commit(repo, "An ordinary line about prices and returns.")
    assert ok and "RESULT: CLEAN" in out


@pytest.mark.parametrize("text, finding", [
    ("Hello from Jane Example", "personal paths / identity"),
    ("saved under C:\\Users\\jexample\\data", "personal paths / identity"),
    ("statement from ExampleBroker", "broker names"),
    ("value was 123.45 today", "real figures"),
    ("key=" + SECRET, "secrets (.env values)"),
    ("account " + ACCOUNT_ID, "account ids"),
    ("account " + ACCOUNT_ID[:8], "account ids"),
    ("number " + ACCOUNT_NUMBER, "account numbers"),
    ("bought QQZ last week", "real tickers"),
    ("fund FNDZ holdings", "real tickers"),
    ("extra EXTRA ticker", "real tickers"),
])
def test_findings_refuse_the_commit(repo, text, finding):
    ok, out = commit(repo, text)
    assert not ok and "FINDINGS" in out and "commit refused (scan exit code 1)" in out
    assert next(l for l in out.splitlines() if finding in l).split(finding)[1].strip() != "none"


def test_secrets_and_ids_are_printed_truncated(repo):
    _, out = commit(repo, "key=%s account %s" % (SECRET, ACCOUNT_ID))
    assert SECRET not in out and ACCOUNT_ID not in out and SECRET[:4] + "..." in out


# ------------------------------------------------------------ ticker matching
def test_short_ticker_inside_an_ordinary_word_is_not_flagged(repo):
    ok, out = commit(repo, "QUIT the AQUA screen; QQZX and XQQZ are not tickers.")
    assert ok, out


def test_short_ticker_as_a_standalone_word_is_flagged(repo):
    ok, out = commit(repo, "position Q was sold")
    assert not ok and "notes.txt: Q" in out


def test_ticker_matching_is_case_sensitive(repo):
    ok, out = commit(repo, "q and qqz in lower case are ordinary text")
    assert ok, out


def test_underscore_joins_a_word(repo):
    ok, out = commit(repo, "QQZ_DAY = 1")
    assert ok, out


def test_generic_crypto_symbols_are_not_flagged(repo):
    ok, out = commit(repo, "crypto list: BTC")
    assert ok, out


def test_allowlisted_word_passes(repo):
    (repo / "_private" / "scan_allowlist.txt").write_text("# sample ticker\nQQZ\n",
                                                          encoding="utf-8")
    ok, out = commit(repo, "sample ticker QQZ in a fixture")
    assert ok, out


def test_allowlist_does_not_exempt_other_categories(repo):
    (repo / "_private" / "scan_allowlist.txt").write_text("123.45\nJane Example\n",
                                                          encoding="utf-8")
    ok, _ = commit(repo, "Jane Example paid 123.45")
    assert not ok


# ------------------------------------------------------------ fail closed
@pytest.mark.parametrize("break_it, message", [
    (lambda r: (r / "_private" / "scan_denylist.json").unlink(), "denylist not found"),
    (lambda r: (r / "_private" / "scan_denylist.json").write_text("{}", encoding="utf-8"),
     "denylist is empty"),
    (lambda r: (r / "_private" / "scan_denylist.json").write_text(
        json.dumps({k: [] for k in DENYLIST}), encoding="utf-8"), "denylist is empty"),
    (lambda r: (r / "_private" / "scan_denylist.json").write_text("{not json", encoding="utf-8"),
     "denylist unreadable"),
    (lambda r: (r / "data" / "portfolio.duckdb").unlink(), "database not found"),
    (lambda r: (r / ".env").unlink(), ".env not found"),
    (lambda r: shutil.rmtree(r / "data" / "raw" / "snaptrade"), "raw SnapTrade folder not found"),
    (lambda r: (r / "archiver" / "config.ini").unlink(), "archiver config not found"),
])
def test_unreadable_inputs_refuse_the_commit(repo, break_it, message):
    break_it(repo)
    ok, out = commit(repo, "An ordinary line.")
    assert not ok and "CANNOT SCAN - " + message in out
    assert "commit refused (scan exit code 2)" in out


def test_locked_database_is_reported_as_busy_not_as_a_finding(repo):
    (repo / "notes.txt").write_text("An ordinary line.\n", encoding="utf-8")
    git(repo, "add", "notes.txt")
    holder = duckdb.connect(str(repo / "data" / "portfolio.duckdb"))   # like a nightly run
    try:
        r = scan(repo)
        ok, out = commit(repo, "An ordinary line.")
    finally:
        holder.close()
    assert r.returncode == 2
    assert "RESULT: CANNOT SCAN - database busy, retry after the nightly run" in r.stdout
    assert "FINDINGS" not in r.stdout
    assert not ok and "database busy, retry after the nightly run" in out


def test_missing_python_setting_refuses_the_commit(repo):
    git(repo, "config", "--unset", "hooks.python")
    ok, out = commit(repo, "An ordinary line.")
    assert not ok and "hooks.python is not set" in out


def test_python_that_does_not_exist_refuses_the_commit(repo):
    git(repo, "config", "hooks.python", str(repo / "no_such_python.exe"))
    ok, out = commit(repo, "An ordinary line.")
    assert not ok and "commit refused" in out


def test_missing_scanner_refuses_the_commit(repo):
    (repo / "tools" / "scan_diff.py").unlink()
    ok, out = commit(repo, "An ordinary line.")
    assert not ok and "commit refused" in out


def test_crashing_scanner_refuses_the_commit(repo):
    (repo / "tools" / "scan_diff.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    ok, out = commit(repo, "An ordinary line.")
    assert not ok and "commit refused" in out


def test_scan_exit_codes(repo):
    (repo / "notes.txt").write_text("clean\n", encoding="utf-8")
    git(repo, "add", "notes.txt")
    assert scan(repo).returncode == 0
    (repo / "notes.txt").write_text("Jane Example\n", encoding="utf-8")
    git(repo, "add", "notes.txt")
    assert scan(repo).returncode == 1
    (repo / ".env").unlink()
    assert scan(repo).returncode == 2
