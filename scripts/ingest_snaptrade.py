"""Pull accounts, positions and transactions from SnapTrade (read-only GETs, Personal
API key) into portfolio.duckdb; raw responses are saved before parsing and the database
is backed up before writing. Usage: python scripts/ingest_snaptrade.py
"""
import os
import sys
import json
import time
import shutil
import logging
import pathlib
import datetime
from collections import defaultdict

from dotenv import load_dotenv

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
load_dotenv(ENV_PATH)

RUN_TS = datetime.datetime.now()
RUN_STAMP = RUN_TS.strftime("%Y%m%d_%H%M%S")
RUN_DATE = RUN_TS.date()

RAW_DIR = PROJECT_ROOT / "data" / "raw" / "snaptrade" / RUN_TS.strftime("%Y-%m-%d")
BACKUP_DIR = PROJECT_ROOT / "data" / "backups"
LOG_DIR = PROJECT_ROOT / "logs"
LOG_PATH = LOG_DIR / ("ingest_snaptrade_%s.log" % RUN_STAMP)

REQUIRED_ENV = ("SNAPTRADE_CLIENT_ID", "SNAPTRADE_CONSUMER_KEY")
PLACEHOLDER_HINTS = ("your_", "your-", "_here", "changeme")

PAGE_SIZE = 100
MAX_RETRIES = 3
RETRYABLE = {429, 500, 502, 503, 504}

# An account returning fewer than this fraction of its previous snapshot's
# positions is treated as a bad response and not written, so an empty or
# truncated reply cannot replace good data. A genuine large sell-off trips it
# too; SNAPTRADE_ALLOW_POSITION_DROP=1 accepts the change for one run.
POSITION_DROP_RATIO = 0.5
ALLOW_POSITION_DROP = (os.getenv("SNAPTRADE_ALLOW_POSITION_DROP", "")
                       .strip() in ("1", "true", "yes"))

# Fetch and save raw responses but write nothing, to test the fetch path safely.
DRY_RUN = os.getenv("SNAPTRADE_DRY_RUN", "").strip() in ("1", "true", "yes")

# Only read-only GETs are used. Never called: refresh_brokerage_authorization
# (may be billable), disable_brokerage_authorization and delete_connection
# (destructive), sync_brokerage_authorization_transactions (triggers a write).

log = logging.getLogger("ingest")


def setup_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    log.addHandler(fh)


def make_scrubber(secrets):
    """Return a function that redacts credential values from text.

    SnapTrade's 401 response echoes the client id, so all error text is
    scrubbed before it is printed or logged.
    """
    real = [s for s in secrets if s and len(s) >= 6]

    def scrub(text):
        out = str(text)
        for s in real:
            out = out.replace(s, "<redacted>")
        return out
    return scrub


def f(value):
    """Parse a number that may arrive as a string, or None."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_date(value):
    """'2025-09-20T13:41:32Z' -> date(2025, 9, 20). None-safe."""
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.datetime.fromisoformat(s).date()
    except ValueError:
        try:
            return datetime.date.fromisoformat(s[:10])
        except ValueError:
            return None


def to_timestamp(value):
    """ISO timestamp -> naive datetime (DuckDB TIMESTAMP is tz-naive)."""
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        d = to_date(value)
        return datetime.datetime(d.year, d.month, d.day) if d else None
    if dt.tzinfo is not None:
        dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return dt


def plain(resp):
    """Plain-Python view of a response, preferring untouched HTTP bytes."""
    raw = getattr(getattr(resp, "response", None), "data", None)
    if isinstance(raw, (bytes, bytearray)):
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            pass
    return json.loads(json.dumps(getattr(resp, "body", resp), default=str))


def save_raw(endpoint, account_id, page, payload):
    """Write a raw response to disk before parsing, so a parser bug cannot lose data."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    name = "%s_%s_%s_%s.json" % (endpoint, account_id or "all", page,
                                 RUN_TS.strftime("%H%M%S"))
    path = RAW_DIR / name
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    log.info("raw saved %s (%d bytes)", name, path.stat().st_size)
    return path


def call_with_retry(fn, scrub, what, **kwargs):
    """Call a SnapTrade endpoint, retrying 429/5xx with backoff."""
    delay = 2.0
    last = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(**kwargs)
        except Exception as e:
            status = getattr(e, "status", None)
            last = e
            if status in RETRYABLE and attempt < MAX_RETRIES:
                log.warning("%s: HTTP %s, retry %d/%d in %.0fs",
                            what, status, attempt, MAX_RETRIES, delay)
                time.sleep(delay)
                delay *= 2
                continue
            raise
    raise last


def fetch_authorizations(client, scrub):
    """Return brokerage authorizations (GET /authorizations), saving the raw response."""
    resp = call_with_retry(client.connections.list_brokerage_authorizations,
                           scrub, "list_brokerage_authorizations")
    payload = plain(resp)
    save_raw("authorizations", None, "1", payload)       # raw first
    return payload or []


def check_connection_health(auths):
    """Return plain-English problems for disabled connections.

    A disabled connection has expired and must be re-authorised in SnapTrade;
    ingesting against it would silently produce stale or empty data.
    """
    problems = []
    for a in auths:
        broker = ((a.get("brokerage") or {}).get("name")
                  or a.get("name") or "unknown broker")
        if a.get("disabled"):
            when = a.get("disabled_date") or "an unknown date"
            problems.append(
                "The connection to %s is DISABLED (since %s). Log in to "
                "SnapTrade and re-authorise it - until then this broker's "
                "data cannot refresh." % (broker, when))
    return problems


def prior_counts(db_path, run_date):
    """Return ({account: prior snapshot position count}, {account: stored transactions})."""
    import duckdb as _d
    pos, tx = {}, {}
    try:
        c = _d.connect(str(db_path), read_only=True)
    except Exception:
        return pos, tx
    try:
        for aid, n in c.execute("""
                SELECT account_id, count(*) FROM positions
                WHERE as_of_date = (SELECT max(as_of_date) FROM positions
                                    WHERE as_of_date < ?)
                GROUP BY 1""", [run_date]).fetchall():
            pos[aid] = n
        for aid, n in c.execute(
                "SELECT account_id, count(*) FROM transactions "
                "GROUP BY 1").fetchall():
            tx[aid] = n
    except Exception:
        pass
    finally:
        c.close()
    return pos, tx


def position_guard(n_now, n_prior):
    """Return None if the position count is plausible, else the reason to skip the account."""
    if n_prior is None or n_prior == 0:
        return None                      # nothing to compare against yet
    if n_now == 0:
        return ("returned ZERO positions but the previous snapshot had %d. "
                "Treating this as a bad response and keeping the old data."
                % n_prior)
    if n_now < n_prior * POSITION_DROP_RATIO:
        return ("returned %d positions, down from %d - a drop of more than "
                "%.0f%%. Treating this as a suspect response. If you really "
                "did sell most of this account, re-run once with "
                "SNAPTRADE_ALLOW_POSITION_DROP=1."
                % (n_now, n_prior, (1 - POSITION_DROP_RATIO) * 100))
    return None


def fetch_positions(client, scrub, account_id):
    resp = call_with_retry(
        client.account_information.get_all_account_positions, scrub,
        "positions(%s)" % account_id[:8], account_id=account_id)
    payload = plain(resp)
    save_raw("positions", account_id, "1", payload)      # raw first
    rows = payload.get("results", payload) if isinstance(payload, dict) else payload
    return rows or []


def fetch_activities(client, scrub, account_id):
    """Return the account's full activity history, paging until exhausted."""
    out = []
    offset, page = 0, 1
    while True:
        resp = call_with_retry(
            client.account_information.get_account_activities, scrub,
            "activities(%s p%d)" % (account_id[:8], page),
            account_id=account_id, limit=PAGE_SIZE, offset=offset)
        payload = plain(resp)
        save_raw("activities", account_id, str(page), payload)   # raw first

        if isinstance(payload, dict):
            rows = payload.get("data") or []
            pg = payload.get("pagination") or {}
            total = pg.get("total")
        else:
            rows = payload or []
            total = None

        out.extend(rows)
        log.info("account %s activities page %d: %d rows (running %d of %s)",
                 account_id[:8], page, len(rows), len(out), total)

        if not rows:
            break
        if total is not None and len(out) >= int(total):
            break
        if len(rows) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        page += 1
        if page > 500:                       # hard stop, should never trigger
            log.warning("account %s: pagination guard hit", account_id[:8])
            break
    return out


def parse_positions(raw_rows, account_id):
    """Return (rows, rows without a symbol, symbol collisions) for the positions table."""
    good, no_symbol, collisions = [], [], []
    seen = {}
    for r in raw_rows:
        ins = r.get("instrument") or {}
        symbol = ins.get("symbol") or ins.get("raw_symbol")
        if not symbol:
            no_symbol.append({"account_id": account_id,
                              "description": ins.get("description"),
                              "units": r.get("units")})
            continue
        units = f(r.get("units"))
        price = f(r.get("price"))
        market_value = None
        if units is not None and price is not None:
            market_value = units * price     # derived: no market_value field
        row = {
            "account_id": account_id,
            "symbol": str(symbol),
            "as_of_date": RUN_DATE,
            "quantity": units,
            "avg_cost": f(r.get("cost_basis")),   # per-unit average price
            "market_value": market_value,
            "currency": r.get("currency"),
        }
        key = (account_id, row["symbol"], RUN_DATE)
        if key in seen:
            collisions.append({"account_id": account_id, "symbol": row["symbol"],
                               "kept_units": seen[key]["quantity"],
                               "dropped_units": units})
            continue
        seen[key] = row
        good.append(row)
    return good, no_symbol, collisions


def parse_account(raw_account):
    """Map a SnapTrade account record onto the accounts table."""
    meta = raw_account.get("meta") or {}
    balance = (raw_account.get("balance") or {}).get("total") or {}
    sync = (raw_account.get("sync_status") or {}).get("holdings") or {}
    return {
        "account_id": raw_account.get("id"),
        "brokerage": raw_account.get("institution_name"),
        "account_name": raw_account.get("name"),
        "account_type": raw_account.get("raw_type") or meta.get("type"),
        "currency": meta.get("currency") or balance.get("currency"),
        "last_synced": to_timestamp(sync.get("last_successful_sync")),
    }


def parse_activities(raw_rows, account_id):
    """Return (transaction rows, count of activities without an id)."""
    rows, missing = [], 0
    for a in raw_rows:
        tid = a.get("id")
        if not tid:
            missing += 1
            continue
        sym = a.get("symbol")
        symbol = None
        if isinstance(sym, dict):
            symbol = sym.get("symbol") or sym.get("raw_symbol")
        elif isinstance(sym, str):
            symbol = sym
        cur = a.get("currency")
        currency = cur.get("code") if isinstance(cur, dict) else cur
        rows.append({
            "transaction_id": str(tid),
            "account_id": account_id,
            "symbol": symbol,
            "txn_type": a.get("type"),
            "quantity": f(a.get("units")),
            "price": f(a.get("price")),
            "amount": f(a.get("amount")),
            "fee": f(a.get("fee")),
            "currency": currency,
            "trade_date": to_date(a.get("trade_date")),
            "settle_date": to_date(a.get("settlement_date")),
        })
    return rows, missing


def write_account(con, account_id, account_row, positions, transactions):
    """Write one account atomically; return (pos_inserted, tx_inserted, tx_updated).

    Upserts the account row, replaces today's position snapshot and upserts
    transactions on SnapTrade's activity id, all in one transaction so a
    failed account leaves nothing behind.
    """
    tx_ids = [t["transaction_id"] for t in transactions]
    existing = set()
    if tx_ids:
        marks = ",".join("?" * len(tx_ids))
        existing = {r[0] for r in con.execute(
            "SELECT transaction_id FROM transactions WHERE transaction_id IN (%s)"
            % marks, tx_ids).fetchall()}

    con.execute("BEGIN TRANSACTION")
    try:
        con.execute(
            "INSERT INTO accounts (account_id, brokerage, account_name,"
            " account_type, currency, last_synced) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT (account_id) DO UPDATE SET"
            "   brokerage=excluded.brokerage,"
            "   account_name=excluded.account_name,"
            "   account_type=excluded.account_type,"
            "   currency=excluded.currency,"
            "   last_synced=excluded.last_synced",
            [account_row["account_id"], account_row["brokerage"],
             account_row["account_name"], account_row["account_type"],
             account_row["currency"], account_row["last_synced"]])

        con.execute("DELETE FROM positions WHERE account_id = ? AND as_of_date = ?",
                    [account_id, RUN_DATE])
        for p in positions:
            con.execute(
                "INSERT INTO positions (account_id, symbol, as_of_date, quantity,"
                " avg_cost, market_value, currency) VALUES (?,?,?,?,?,?,?)",
                [p["account_id"], p["symbol"], p["as_of_date"], p["quantity"],
                 p["avg_cost"], p["market_value"], p["currency"]])

        for t in transactions:
            con.execute(
                "INSERT INTO transactions (transaction_id, account_id, symbol,"
                " txn_type, quantity, price, amount, fee, currency, trade_date,"
                " settle_date) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT (transaction_id) DO UPDATE SET"
                "   account_id=excluded.account_id, symbol=excluded.symbol,"
                "   txn_type=excluded.txn_type, quantity=excluded.quantity,"
                "   price=excluded.price, amount=excluded.amount,"
                "   fee=excluded.fee, currency=excluded.currency,"
                "   trade_date=excluded.trade_date, settle_date=excluded.settle_date",
                [t["transaction_id"], t["account_id"], t["symbol"], t["txn_type"],
                 t["quantity"], t["price"], t["amount"], t["fee"], t["currency"],
                 t["trade_date"], t["settle_date"]])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

    updated = len(existing)
    inserted = len(transactions) - updated
    return len(positions), inserted, updated


def main():
    """Run the ingest. Exit codes: 0 ok, 1 an account failed, 2 configuration
    problem, 3 activities without ids (nothing written), 4 disabled connection.
    """
    setup_logging()
    log.info("run start %s", RUN_TS.isoformat(timespec="seconds"))

    creds = {k: (os.getenv(k) or "").strip() for k in REQUIRED_ENV}
    bad = [k for k, v in creds.items()
           if not v or any(h in v.lower() for h in PLACEHOLDER_HINTS)]
    if bad:
        print("ERROR: credentials not ready: %s" % ", ".join(bad))
        print("Set them in %s (values are never displayed)." % ENV_PATH)
        log.error("missing/placeholder credentials: %s", ", ".join(bad))
        return 2
    scrub = make_scrubber(creds.values())

    db_path = PROJECT_ROOT / (os.getenv("DB_PATH") or "data/portfolio.duckdb")
    if not db_path.exists():
        print("ERROR: database not found at %s" % db_path)
        print("Run scripts/create_db.py first.")
        return 2

    try:
        import duckdb
        from snaptrade_client import SnapTrade, SnapTradeAuth
    except ImportError as e:
        print("ERROR: missing dependency (%s). Run: pip install -r requirements.txt" % e)
        return 2

    client = SnapTrade(auth=SnapTradeAuth.personal_api_key(
        client_id=creds["SNAPTRADE_CLIENT_ID"],
        consumer_key=creds["SNAPTRADE_CONSUMER_KEY"]))

    # Fetch everything before writing anything; a dead broker link makes the
    # rest moot, so it is checked first.
    try:
        auths = fetch_authorizations(client, scrub)
        health = check_connection_health(auths)
        log.info("brokerage connections checked: %d, problems: %d",
                 len(auths), len(health))
    except Exception as e:
        auths, health = [], []
        log.warning("could not check connection health: %s: %s",
                    type(e).__name__, scrub(e))
        print("WARNING: could not check brokerage connection health (%s). "
              "Continuing." % type(e).__name__)

    if health:
        print("\nSTOPPED - a brokerage connection needs re-authorising.\n")
        for h in health:
            print("  %s" % h)
            log.error("connection health: %s", h)
        print("\nNothing was written. Fix the connection, then re-run.")
        return 4

    try:
        resp = call_with_retry(client.account_information.list_user_accounts,
                               scrub, "list_user_accounts")
        accounts = plain(resp)
        save_raw("accounts", None, "1", accounts)
    except Exception as e:
        print("ERROR: could not list accounts: %s: %s" % (type(e).__name__, scrub(e)))
        log.error("list_user_accounts failed: %s: %s", type(e).__name__, scrub(e))
        return 1

    if not accounts:
        print("No connected accounts. Nothing to do.")
        log.info("no accounts returned")
        return 0

    prior_pos, prior_tx = prior_counts(db_path, RUN_DATE)
    log.info("prior snapshot position counts: %s", prior_pos)

    fetched, failed, warnings = {}, {}, []
    for a in accounts:
        aid = a.get("id")
        label = "%s / %s" % (a.get("institution_name") or "?", a.get("name") or "?")
        if not aid:
            failed["(no id)"] = ("accounts", "account record has no id")
            continue
        try:
            raw_pos = fetch_positions(client, scrub, aid)
            raw_act = fetch_activities(client, scrub, aid)

            reason = position_guard(len(raw_pos), prior_pos.get(aid))
            if reason and not ALLOW_POSITION_DROP:
                failed[aid] = ("positions-guard", "%s %s" % (label, reason))
                log.error("account %s (%s) POSITION GUARD: %s",
                          aid[:8], label, reason)
                continue          # raw file is already saved; skip the write
            if reason and ALLOW_POSITION_DROP:
                warnings.append("%s: position drop allowed by override (%s)"
                                % (label, reason))
                log.warning("position drop overridden for %s", label)

            # History is fetched in full, so fewer activities than are stored
            # suggests a truncated response (a warning only: writes are upserts).
            stored_tx = prior_tx.get(aid)
            if stored_tx and len(raw_act) < stored_tx:
                warnings.append(
                    "%s: SnapTrade returned %d activities but %d are already "
                    "stored - possible truncated response. Nothing is deleted "
                    "(upsert only), but worth checking."
                    % (label, len(raw_act), stored_tx))
                log.warning("account %s activities %d < stored %d",
                            aid[:8], len(raw_act), stored_tx)

            fetched[aid] = {"label": label, "raw_account": a,
                            "raw_pos": raw_pos, "raw_act": raw_act}
            log.info("account %s (%s): fetched %d positions, %d activities",
                     aid[:8], label, len(raw_pos), len(raw_act))
        except Exception as e:
            endpoint = "positions/activities"
            failed[aid] = (endpoint, "%s: %s" % (type(e).__name__, scrub(e)))
            log.error("account %s (%s) fetch failed [%s]: %s",
                      aid[:8], label, endpoint, scrub(e))

    # Activities are upserted on their id, so any missing id stops the run.
    parsed = {}
    missing_total = 0
    for aid, d in fetched.items():
        tx, missing = parse_activities(d["raw_act"], aid)
        pos, no_sym, collisions = parse_positions(d["raw_pos"], aid)
        missing_total += missing
        parsed[aid] = {"label": d["label"], "positions": pos, "transactions": tx,
                       "account_row": parse_account(d["raw_account"]),
                       "no_symbol": no_sym, "collisions": collisions,
                       "missing_id": missing,
                       "n_raw_pos": len(d["raw_pos"]),
                       "n_raw_act": len(d["raw_act"])}

    if missing_total:
        print("\nSTOPPED - nothing was written.\n")
        print("%d activities have no id, so upserting on it would be unsafe."
              % missing_total)
        for aid, p in parsed.items():
            if p["missing_id"]:
                print("   %s (%s): %d without id" % (aid[:8], p["label"],
                                                     p["missing_id"]))
        print("\nDecide what the transaction key should be, then re-run.")
        log.error("halted: %d activities without id", missing_total)
        return 3

    if DRY_RUN:
        print()
        print("DRY RUN - nothing was written to the database.")
        print("  accounts fetched : %d" % len(accounts))
        for aid, p in parsed.items():
            print("  %-22s would write %d positions, %d transactions"
                  % (p["label"], len(p["positions"]), len(p["transactions"])))
        if warnings:
            print("  warnings:")
            for w in warnings:
                print("     - %s" % w)
        print("  raw files saved  : %d  (%s)"
              % (len(list(RAW_DIR.glob("*.json"))) if RAW_DIR.exists() else 0,
                 RAW_DIR))
        log.info("dry run complete: no database writes")
        return 1 if failed else 0

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = BACKUP_DIR / ("portfolio_%s.duckdb" % RUN_STAMP)
    shutil.copy2(db_path, backup_path)
    log.info("backup written: %s", backup_path.name)
    print("Backup: %s" % backup_path.relative_to(PROJECT_ROOT))

    # One transaction per account: a failure rolls back that account only.
    con = duckdb.connect(str(db_path))
    results = {}
    try:
        for aid, p in parsed.items():
            try:
                n_pos, n_ins, n_upd = write_account(
                    con, aid, p["account_row"], p["positions"], p["transactions"])
                results[aid] = {"pos": n_pos, "tx_ins": n_ins, "tx_upd": n_upd}
                log.info("account %s (%s): positions fetched=%d inserted=%d "
                         "skipped=%d | tx fetched=%d inserted=%d updated=%d",
                         aid[:8], p["label"], p["n_raw_pos"], n_pos,
                         len(p["no_symbol"]) + len(p["collisions"]),
                         p["n_raw_act"], n_ins, n_upd)
            except Exception as e:
                failed[aid] = ("write", "%s: %s" % (type(e).__name__, scrub(e)))
                log.error("account %s (%s) write failed, rolled back: %s",
                          aid[:8], p["label"], scrub(e))

        raw_count = len(list(RAW_DIR.glob("*.json"))) if RAW_DIR.exists() else 0
        print("\n" + "=" * 70)
        print("SUMMARY  (%s)" % RUN_TS.strftime("%Y-%m-%d %H:%M:%S"))
        print("=" * 70)
        for aid, p in parsed.items():
            if aid not in results:
                continue
            r = results[aid]
            print("\n%s   [%s]" % (p["label"], aid[:8]))
            print("   positions      : %d" % r["pos"])
            mv = defaultdict(float)
            for row in p["positions"]:
                if row["market_value"] is not None:
                    mv[row["currency"] or "(unknown)"] += row["market_value"]
            if mv:
                for cur, tot in sorted(mv.items()):
                    # never summed across currencies - one line per currency
                    print("   market value   : {:,.2f} {}".format(tot, cur))
            else:
                print("   market value   : n/a")
            print("   transactions   : %d  (new %d, updated %d)"
                  % (len(p["transactions"]), r["tx_ins"], r["tx_upd"]))
            dates = [t["trade_date"] for t in p["transactions"] if t["trade_date"]]
            print("   earliest txn   : %s" % (min(dates) if dates else "n/a"))
            if p["no_symbol"]:
                print("   SKIPPED (no symbol): %d" % len(p["no_symbol"]))
                for x in p["no_symbol"]:
                    print("      - %s units=%s" % (x["description"], x["units"]))
            if p["collisions"]:
                print("   SKIPPED (duplicate key): %d" % len(p["collisions"]))
                for x in p["collisions"]:
                    print("      - %s kept=%s dropped=%s"
                          % (x["symbol"], x["kept_units"], x["dropped_units"]))

        if warnings:
            print()
            print("WARNINGS (data was still written):")
            for w in warnings:
                print("   - %s" % w)

        print("\n" + "-" * 70)
        print("accounts succeeded : %d" % len(results))
        print("accounts failed    : %d" % len(failed))
        for aid, (endpoint, msg) in failed.items():
            print("   %s [%s] %s" % (aid[:8], endpoint, msg[:110]))
        print("raw files saved    : %d  (%s)"
              % (raw_count, RAW_DIR.relative_to(PROJECT_ROOT)))
        print("log                : %s" % LOG_PATH.relative_to(PROJECT_ROOT))
    finally:
        con.close()

    log.info("run end: %d succeeded, %d failed", len(results), len(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
