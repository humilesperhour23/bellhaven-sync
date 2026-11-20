"""Daily job: scrape the website, then run the matcher. Never writes to the CRM.

    python run_pipeline.py            short summary only
    python run_pipeline.py --verbose  also show the scraper's and matcher's full output

Exit codes:
    0  success
    2  scrape found fewer than MIN_EXPECTED_LOCATIONS communities (matcher NOT run,
       previous website data kept)
    1  any other error (missing token, CRM unreachable, ...)
"""
import contextlib
import io
import os
import sys
from collections import Counter

from dotenv import load_dotenv

import config
import match
import scraper
import store


def quietly(func, *args, verbose=False):
    """Run func, showing its printed output only in verbose mode."""
    if verbose:
        return func(*args)
    with contextlib.redirect_stdout(io.StringIO()):
        return func(*args)


def main():
    verbose = "--verbose" in sys.argv
    load_dotenv()
    if not os.getenv("CRM_TOKEN"):
        print("ERROR: CRM_TOKEN is not set (add it to .env, or as a secret in CI).")
        return 1

    # ---- 1. Scrape ------------------------------------------------------------------
    print("1/2 Scraping the Bellhaven website...")
    locations, problems = quietly(scraper.scrape, verbose=verbose)
    print(f"    {len(locations)} communities found, {len(problems)} with missing fields")
    if len(locations) < config.MIN_EXPECTED_LOCATIONS:
        print(f"ERROR: expected at least {config.MIN_EXPECTED_LOCATIONS} communities "
              f"(MIN_EXPECTED_LOCATIONS in config.py). The website may be down or its layout changed.")
        print("       Stopping before the matcher, so accounts are not wrongly flagged 'not on website'.")
        print(f"       Previous data in {scraper.OUTPUT_FILE} was left as it was.")
        return 2
    quietly(scraper.save, locations, verbose=verbose)

    # ---- 2. Match ---------------------------------------------------------------------
    print("2/2 Matching against the CRM (read-only)...")
    run_started = store.now()
    quietly(match.main, verbose=verbose)

    # ---- Summary ------------------------------------------------------------------------
    db = store.connect()
    rows = store.list_all(db)
    new = [p for p in rows if p["created_at"] >= run_started and p["type"] != "NO_CHANGE"]
    pending = [p for p in rows if p["status"] == "pending"]
    by_type = Counter(p["type"] for p in new)
    print("\nSUMMARY")
    print(f"  New proposals this run: {len(new)}"
          + (f"  ({', '.join(f'{t}: {n}' for t, n in sorted(by_type.items()))})" if new else ""))
    print(f"  Waiting for review:     {len(pending)} pending")
    failed = [p for p in rows if p["status"] == "approved" and p["last_error"]]
    if failed:
        print(f"  Approved but failed:    {len(failed)} (open the review app to retry)")
    print("  No changes were sent to the CRM.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
