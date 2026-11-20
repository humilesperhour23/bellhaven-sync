# Bellhaven Sync

Keeps the CRM in line with the Bellhaven Senior Living website, the source of truth for which communities Bellhaven runs.

Every day the pipeline:
1. **Scrapes** every community from the website (name, address, care offerings, administrator, phone).
2. **Matches** each one to CRM accounts and **proposes** fixes: renames, re-parents, new accounts, duplicates to retire, and accounts no longer on the website.
3. **Waits for a person.** Nothing is written to the CRM until someone approves it in the review app.

## Files

| File | What it does |
|---|---|
| `config.py` | **All settings** (see below) |
| `scraper.py` | Reads the website → `data/website_locations.json` |
| `match.py` | Compares website vs CRM (read-only) → proposals in `data/pipeline.db` |
| `run_pipeline.py` | Daily job: scraper + matcher + short summary |
| `review_app.py` | Web page to approve / reject proposals |
| `applier.py` | Sends an approved change to the CRM |
| `store.py` | The local database (proposals + audit log) |
| `schedule/crontab.txt`, `.github/workflows/daily-sync.yml` | Daily schedule |

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate            # Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Create a file called `.env` in this folder:

```
CRM_TOKEN=your-token-here
```

`.env` is git-ignored. Never commit the token. For GitHub Actions, add it as a repository secret named `CRM_TOKEN` (Settings → Secrets and variables → Actions).

## Running it

```bash
python run_pipeline.py              # scrape + match, short summary
python run_pipeline.py --verbose    # same, with every proposal listed
streamlit run review_app.py         # open the review app (http://localhost:8501)
```

In the review app, each proposal shows a before/after table, the evidence, and links to the website page and the CRM account. **Approve** writes it to the CRM. **Reject** takes an optional reason.

While `DRY_RUN = True` in `config.py` (the default), Approve only *shows* the exact API requests. Set it to `False` and restart the app to write for real.

## Why re-runs are safe

- **Decisions stick.** Each proposal has a fixed key built from the account (or website page), the type of change, and the proposed values. A proposal whose key is already stored is never added again, so a rejected change never comes back.
- **Finished work is left alone.** Accounts with `chow_current_account` set (kept for billing history) are ignored completely. Accounts already marked duplicate and Inactive are treated as done. An account that already looks the way a proposal wants is recorded as "no change".
- **Check before writing.** On Approve, the app re-reads the account. If anything changed since the proposal was made, it marks the proposal **stale**, writes nothing, and the next run proposes it fresh.
- **Failures can be retried.** If a write fails, the proposal stays approved with a Retry button. A retry never creates a second account.
- **A broken website can't cause damage.** If the scrape finds fewer than `MIN_EXPECTED_LOCATIONS` communities, the pipeline stops with an error before matching. Otherwise every account would look "not on website".
- **Everything is logged.** Every API call goes into the `audit_log` table in `data/pipeline.db`.

## Matching rules

Addresses are cleaned before comparing: lowercase, no punctuation, standard abbreviations (Street → St, West → W, Northwest → NW), and 5-digit zip codes.

A website community matches a CRM account when:
1. **Same street + zip.** High confidence.
2. **Near-identical address:** same house number and zip, street name at least 85% similar (e.g. "Wilmington Pk" vs "Pike"). Medium confidence.
3. **Same city + state and a very similar name.** Medium confidence. A matching name in a *different* city or state never counts.

| Result | What's proposed |
|---|---|
| Matches, already correct | Nothing (listed under "Confirmed matches") |
| Matches, wrong name / parent / address | **UPDATE** the fields that differ |
| No CRM account | **CREATE** one under Bellhaven |
| Several accounts match one community | Keep one (the **survivor**). Mark the others **DUPLICATE** (`duplicate_of_account` = survivor, status Inactive). |
| Under Bellhaven, not on website, and another owner's account is at the same address | It was sold: **DUPLICATE** of that account |
| Under Bellhaven, not on website, nothing else there | **Needs Review** plus a note |

The survivor is chosen in this order: correct name and parent, then has billing history, then has more contacts, then Active. The evidence also says whether the website's administrator is one of the account's CRM contacts.

## Change of ownership (CHOW)

Before moving an account to a new parent, or marking it duplicate, the pipeline checks its billing. If it has **both** `lifetime_revenue > 0` **and** `outstanding_ar > 0`, the old account is kept exactly as it is, so billing history and money owed stay with the old owner. Instead:
- **Re-parent case:** a **new** account is created under Bellhaven, and the old account's `chow_current_account` is set to the new id. Nothing else changes on the old account, and it gets no note.
- **Duplicate case:** only `chow_current_account` is set, pointing at the account that replaces it.

Otherwise, the account is re-parented or marked duplicate directly.

## Settings (`config.py`)

| Setting | Meaning |
|---|---|
| `DRY_RUN` | `True` = the review app only previews requests |
| `BELLHAVEN_PARENT_ID` | The Bellhaven parent account (checked on every run) |
| `NAME_SIMILARITY_THRESHOLD`, `STREET_SIMILARITY_THRESHOLD` | How close names / streets must be to match |
| `ADDRESS_ABBREVIATIONS`, `NAME_WORD_MAP` | Word replacements used when comparing |
| `CARE_TYPE_MAP` | Website care label → CRM `care_type` |
| `FIX_ADDRESS`, `FIX_CARE_TYPE` | Whether UPDATE also fixes address / care type |
| `MIN_EXPECTED_LOCATIONS` | Scrape minimum before the pipeline stops |
| `NOTE_PREFIX` | Start of every note the sync writes |

## Schedule

- **Cron (primary):** see `schedule/crontab.txt` (daily, 6 AM). This is the main schedule because it runs on the same machine as the review app and **shares its `pipeline.db`**, so every approve/reject decision is seen by the next run.
- **GitHub Actions (cloud option):** `.github/workflows/daily-sync.yml` runs daily (11:00 UTC ≈ 6 AM Eastern) and has a manual "Run workflow" button. It keeps its *own* `pipeline.db`, restored from the previous run and saved at the end, and uploads it as a downloadable artifact. Decisions made in a local review app don't reach it.

> **In production**, the decisions database would live in a shared hosted database (e.g. Postgres) instead of a local SQLite file, so scheduled runs and reviews always stay in sync.
