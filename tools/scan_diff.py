"""Scan the added lines of a git diff for data that must never be published.

Usage:
    python tools/scan_diff.py --cached          (staged changes; the pre-commit hook)
    python tools/scan_diff.py origin/main..HEAD (commits not yet pushed)

Nothing personal is stored here. At run time it reads, from the repository:
  _private/scan_denylist.json   identity and paths, broker names, real figures,
                                extra tickers (required; gitignored)
  _private/scan_allowlist.txt   words exempt from ticker matching (optional)
  .env                          SNAPTRADE_* secrets
  the database                  account ids and every symbol ever held
  data/raw/snaptrade/           account numbers in raw API responses
  archiver/config.ini           the fund list
Tickers match as whole words, case-sensitive ('_' counts as part of a word).

Exit codes: 0 CLEAN, 1 findings, 2 cannot scan. Anything that cannot be read
is a refusal, never a pass. Secrets and account identifiers are printed
truncated to their first 4 characters.
"""
import configparser
import json
import pathlib
import re
import subprocess
import sys

GENERIC_CRYPTO = {"BTC", "ETH", "XRP", "SOL", "DOGE"}     # standard coin list in code
DENYLIST_KEYS = ("identity", "brokers", "figures", "tickers")


class CannotScan(Exception):
    pass


def repo_root():
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if r.returncode != 0:
        raise CannotScan("not inside a git repository")
    return pathlib.Path(r.stdout.strip())


def added_lines(root, args):
    r = subprocess.run(["git", "diff", "-U0", *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=root)
    if r.returncode != 0:
        raise CannotScan("git diff failed: %s" % r.stderr.strip())
    files, added, cur = [], [], None
    for l in r.stdout.splitlines():
        if l.startswith("+++ "):
            cur = l[6:] if l.startswith("+++ b/") else l[4:]
            files.append(cur)
        elif l.startswith("+") and not l.startswith("+++"):
            added.append((cur, l[1:]))
    return files, added


def load_denylist(root):
    path = root / "_private" / "scan_denylist.json"
    if not path.exists():
        raise CannotScan("denylist not found: %s" % path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CannotScan("denylist unreadable (%s): %s" % (path, e))
    if not isinstance(data, dict) or not any(data.get(k) for k in DENYLIST_KEYS):
        raise CannotScan("denylist is empty: %s" % path)
    return {k: [str(x) for x in data.get(k) or []] for k in DENYLIST_KEYS}, data


def load_allowlist(root):
    path = root / "_private" / "scan_allowlist.txt"
    if not path.exists():
        return set()
    return {l.strip() for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.lstrip().startswith("#")}


def load_database(root, cfg):
    import duckdb
    path = root / cfg.get("database", "data/portfolio.duckdb")
    if not path.exists():
        raise CannotScan("database not found: %s" % path)
    try:
        con = duckdb.connect(str(path), read_only=True)
    except duckdb.IOException as e:
        if "lock" in str(e).lower():
            raise CannotScan("database busy, retry after the nightly run")
        raise CannotScan("database unreadable: %s" % e)
    try:
        held = {r[0] for r in con.execute(
            "SELECT DISTINCT symbol FROM transactions WHERE symbol IS NOT NULL "
            "UNION SELECT symbol FROM opening_balances").fetchall()}
        ids = [r[0] for r in con.execute("SELECT account_id FROM accounts").fetchall()]
    finally:
        con.close()
    return held, ids


def load_env_secrets(root):
    path = root / ".env"
    if not path.exists():
        raise CannotScan(".env not found: %s" % path)
    env = dict(l.split("=", 1) for l in path.read_text(encoding="utf-8").splitlines()
               if "=" in l and not l.startswith("#"))
    return [v.strip() for k, v in env.items()
            if k.strip().startswith("SNAPTRADE_") and len(v.strip()) >= 6]


def load_account_numbers(root):
    folder = root / "data" / "raw" / "snaptrade"
    if not folder.is_dir():
        raise CannotScan("raw SnapTrade folder not found: %s" % folder)
    nums = set()

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "number" and v:
                    nums.add(str(v))
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    for f in folder.rglob("*.json"):
        try:
            walk(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            raise CannotScan("raw file unreadable (%s): %s" % (f, e))
    return nums


def load_funds(root):
    path = root / "archiver" / "config.ini"
    if not path.exists():
        raise CannotScan("archiver config not found: %s" % path)
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(path, encoding="utf-8")
    return {s[5:] for s in cp.sections() if s.startswith("fund ")}


def scan(args):
    root = repo_root()
    deny, cfg = load_denylist(root)
    allow = load_allowlist(root)
    held, ids = load_database(root, cfg)
    secrets = load_env_secrets(root)
    nums = load_account_numbers(root)
    funds = load_funds(root)
    files, added = added_lines(root, args)

    literal = {
        "secrets (.env values)": secrets,
        "account ids": ids + [i[:8] for i in ids],
        "account numbers": sorted(nums),
        "personal paths / identity": deny["identity"],
        "broker names": deny["brokers"],
        "real figures": deny["figures"],
    }
    masked = {"secrets (.env values)", "account ids", "account numbers"}
    tickers = sorted((held | funds | set(deny["tickers"])) - GENERIC_CRYPTO - allow)
    tick_re = re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(map(re.escape, tickers))
                         + r")(?![A-Za-z0-9_])") if tickers else None

    print("files in diff: %d  (%s)" % (len(files), ", ".join(files) or "none"))
    print("added lines scanned: %d" % len(added))
    findings = 0
    for label, lits in literal.items():
        hits = sorted({(f, x) for f, line in added for x in lits if x and x in line})
        findings += len(hits)
        shown = ["%s: %s" % (f, x[:4] + "..." if label in masked else x) for f, x in hits]
        print("  %-28s %s" % (label, "none" if not hits else shown))
    th = sorted({(f, m) for f, line in added for m in tick_re.findall(line)}) if tick_re else []
    findings += len(th)
    print("  %-28s %s" % ("real tickers", "none" if not th else ["%s: %s" % h for h in th]))
    print("RESULT:", "CLEAN" if not findings else "FINDINGS (%d) - do not commit/push" % findings)
    return 0 if not findings else 1


def main(args):
    try:
        return scan(args or ["--cached"])
    except CannotScan as e:
        print("RESULT: CANNOT SCAN - %s" % e)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
