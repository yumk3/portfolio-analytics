"""Read-only SnapTrade connectivity check: lists linked accounts with masked
numbers via GET /accounts, using the Personal API-key flow.
Usage: python scripts/snaptrade_check.py
"""
import os
import sys
import pathlib

from dotenv import load_dotenv

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

load_dotenv(ENV_PATH)

REQUIRED = ("SNAPTRADE_CLIENT_ID", "SNAPTRADE_CONSUMER_KEY")

# Values still carrying the scaffold placeholders count as "not set".
PLACEHOLDER_HINTS = ("your_", "your-", "_here", "changeme", "xxx")


def _looks_like_placeholder(value):
    low = value.strip().lower()
    return any(h in low for h in PLACEHOLDER_HINTS)


def _make_scrubber(secrets):
    """Return a function that redacts credential values from text.

    SDK/HTTP exceptions can echo request headers or the client id, so error
    text is scrubbed before display.
    """
    real = [s for s in secrets if s and len(s) >= 6]

    def scrub(text):
        out = str(text)
        for s in real:
            out = out.replace(s, "<redacted>")
        return out

    return scrub


def _first(obj, *names, default=None):
    """Return the first non-empty field among names (SDK field names vary by broker)."""
    for n in names:
        if isinstance(obj, dict):
            if n in obj and obj[n] not in (None, ""):
                return obj[n]
        else:
            v = getattr(obj, n, None)
            if v not in (None, ""):
                return v
    return default


def _mask(number):
    """Show only the last 4 characters of an account number."""
    if number is None:
        return "(no number)"
    s = str(number).strip()
    if not s:
        return "(no number)"
    if len(s) <= 4:
        # Too short to partially mask without revealing all of it.
        return "*" * len(s)
    return "****" + s[-4:]


def main():
    if not ENV_PATH.exists():
        print("ERROR: no .env file found at %s" % ENV_PATH)
        return 2

    creds = {k: (os.getenv(k) or "").strip() for k in REQUIRED}

    missing = [k for k, v in creds.items() if not v]
    placeholder = [k for k, v in creds.items()
                   if v and _looks_like_placeholder(v)]

    if missing or placeholder:
        print("ERROR: SnapTrade credentials are not ready.\n")
        for k in missing:
            print("  %-24s missing or empty" % k)
        for k in placeholder:
            print("  %-24s still set to the scaffold placeholder" % k)
        print("\nEdit %s and set real values for those keys." % ENV_PATH)
        print("(Values are never displayed by this script.)")
        return 2

    scrub = _make_scrubber(creds.values())

    try:
        from snaptrade_client import SnapTrade, SnapTradeAuth
    except ImportError:
        print("ERROR: SDK not installed. Run:")
        print("  pip install -r requirements.txt")
        return 2

    print("Authenticating with SnapTrade (Personal API key flow)...")
    try:
        client = SnapTrade(
            auth=SnapTradeAuth.personal_api_key(
                client_id=creds["SNAPTRADE_CLIENT_ID"],
                consumer_key=creds["SNAPTRADE_CONSUMER_KEY"],
            )
        )
    except Exception as e:
        print("ERROR: could not build the SnapTrade client.")
        print("  %s: %s" % (type(e).__name__, scrub(e)))
        return 1

    try:
        # Personal flow: the key identifies the user, so user_id and
        # user_secret are deliberately omitted.
        resp = client.account_information.list_user_accounts()
    except Exception as e:
        print("ERROR: the SnapTrade API request failed.")
        print("  %s: %s" % (type(e).__name__, scrub(e)))
        print("\nCommon causes: wrong or revoked key, no brokerage connected,")
        print("or a Commercial key used against the Personal flow.")
        return 1

    accounts = getattr(resp, "body", resp)
    if accounts is None:
        accounts = []

    try:
        n = len(accounts)
    except TypeError:
        accounts = list(accounts)
        n = len(accounts)

    if n == 0:
        print("\nConnected, but no brokerage accounts are linked to this key.")
        return 0

    # Only broker, name and masked number are ever displayed.
    print("\nConnected accounts (%d):\n" % n)
    print("  %-26s %-30s %s" % ("BROKER", "ACCOUNT NAME", "NUMBER"))
    print("  %-26s %-30s %s" % ("-" * 26, "-" * 30, "-" * 10))
    for acct in accounts:
        broker = _first(acct, "institution_name", "brokerage_name",
                        "institution", default="(unknown)")
        name = _first(acct, "name", "account_name", default="(unnamed)")
        number = _first(acct, "number", "account_number", "masked_number")
        print("  %-26s %-30s %s"
              % (str(broker)[:26], str(name)[:30], _mask(number)))

    print("\nRead-only check complete. Nothing was created or modified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
