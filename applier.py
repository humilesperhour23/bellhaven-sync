"""Apply one approved proposal to the CRM.

Steps for every proposal:
  1. Re-read the account(s) from the CRM and check nothing changed since the
     matcher looked. If something did, mark the proposal "stale" and write nothing.
  2. Send the writes (PATCH / POST) for that proposal type.
  3. Log every API call to the audit_log table, store any created account ids,
     and mark the proposal "applied". If a write fails, the proposal stays
     "approved" with the error saved, so it can be retried.

With DRY_RUN (config.py, or dry_run=True) step 1 still reads the CRM, but step 2
only records the exact requests it would send. Nothing is written and the
proposal's status is not changed.
"""
import json
import os
from datetime import date

import requests
from dotenv import load_dotenv

import config
import store
from match import norm_street, norm_zip

load_dotenv()

NEW_ID_PLACEHOLDER = "<NEW_ACCOUNT_ID>"
# Fields that may change without making a proposal stale
IGNORE_FOR_STALE = {"updated_at", "parent_name"}


class ApiError(Exception):
    pass


# =============================================================================
# CRM client that logs every call
# =============================================================================
class CrmClient:
    def __init__(self, db, proposal_key, dry_run):
        self.db, self.key, self.dry_run = db, proposal_key, dry_run
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {os.getenv('CRM_TOKEN')}"
        self.planned = []  # requests a dry run would have sent

    def _send(self, method, path, payload=None, params=None):
        url = config.API_BASE + path
        try:
            resp = self.session.request(method, url, json=payload, params=params, timeout=30)
        except requests.RequestException as e:
            store.log_call(self.db, self.key, method, path, payload, None, f"network error: {e}", False)
            raise ApiError(f"{method} {path} failed: {e}") from e
        endpoint = path + (f"?{requests.compat.urlencode(params)}" if params else "")
        store.log_call(self.db, self.key, method, endpoint, payload, resp.status_code, resp.text, False)
        return resp

    def get(self, path, **params):
        """Reads always happen, even in dry-run mode (they change nothing)."""
        return self._send("GET", path, params=params or None)

    def get_all(self, path, **filters):
        rows, page = [], 1
        while True:
            resp = self.get(path, **filters, page=page, page_size=50)
            if resp.status_code != 200:
                raise ApiError(f"GET {path} failed: HTTP {resp.status_code}: {resp.text[:300]}")
            body = resp.json()
            rows += body.get("data", [])
            if not body.get("data") or len(rows) >= body.get("total", 0):
                return rows
            page += 1

    def write(self, method, path, payload):
        if self.dry_run:
            self.planned.append({"method": method, "url": config.API_BASE + path, "json": payload})
            store.log_call(self.db, self.key, method, path, payload, None, "DRY RUN - not sent", True)
            return {"account_id": NEW_ID_PLACEHOLDER} if method == "POST" else {}
        resp = self._send(method, path, payload)
        if resp.status_code >= 400:
            raise ApiError(f"{method} {path} failed: HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError:
            return {}


# =============================================================================
# Notes
# =============================================================================
def note_line(text, today=None):
    return f"[{(today or date.today()).isoformat()}] {config.NOTE_PREFIX}: {text}"


def append_note(existing, text, today=None):
    """Add a dated line to the note, keeping whatever was already there."""
    line = note_line(text, today)
    existing = (existing or "").rstrip()
    return f"{existing}\n{line}" if existing else line


# =============================================================================
# Step 1: is the proposal still valid?
# =============================================================================
def new_account_payload(proposal):
    """The POST body for proposals that create an account, or None."""
    create = proposal["proposed_changes"].get("create")
    if not create:
        return None
    payload = dict(create)
    payload["note"] = note_line(payload["note"]) if payload.get("note") else ""
    return payload


def find_stale_problems(crm, p):
    """Return (live subject account or None, list of reasons the proposal is stale)."""
    problems, live = [], None
    ids = p["account_ids"]

    if ids:  # the account being changed: every field must match what the matcher saw
        resp = crm.get(f"/accounts/{ids[0]}")
        if resp.status_code != 200:
            problems.append(f"Account {ids[0]} could not be re-read (HTTP {resp.status_code}).")
        else:
            live = resp.json()
            for field, seen in p["current_values"].items():
                if field not in IGNORE_FOR_STALE and live.get(field) != seen:
                    problems.append(f"{field} changed: proposal saw {seen!r}, CRM now has {live.get(field)!r}.")

    for rid in ids[1:]:  # survivor / new owner must still exist and not be retired
        resp = crm.get(f"/accounts/{rid}")
        if resp.status_code != 200:
            problems.append(f"Related account {rid} could not be read (HTTP {resp.status_code}).")
        elif resp.json().get("duplicate_of_account") or resp.json().get("chow_current_account"):
            problems.append(f"Related account {rid} is itself now marked duplicate/CHOW.")

    new_account = new_account_payload(p)
    if new_account and not p["created_account_ids"]:  # don't create a second account at an address
        street = norm_street(new_account["billing_street"])
        for a in crm.get_all("/accounts", zip=norm_zip(new_account["billing_zip"])):
            if (a["account_id"] not in ids and norm_street(a["billing_street"]) == street
                    and not a.get("chow_current_account")
                    and not (a.get("duplicate_of_account") and a.get("status") == "Inactive")):
                problems.append(f"An account now exists at this address: '{a['name']}' ({a['account_id']}).")
    return live, problems


# =============================================================================
# Step 2: the writes
# =============================================================================
def _created_id(body):
    new_id = body.get("account_id") or (body.get("data") or {}).get("account_id")
    if not new_id:
        raise ApiError(f"POST /accounts succeeded but the response had no account_id: {json.dumps(body)[:300]}")
    return new_id


def run_writes(crm, p, live):
    """Send the writes for this proposal. Returns the list of created account ids."""
    changes = p["proposed_changes"]
    created = list(p["created_account_ids"])

    def create_once():
        # On a retry, an account created by an earlier attempt is reused, never re-created.
        if not created:
            created.append(_created_id(crm.write("POST", "/accounts", new_account_payload(p))))
            if not crm.dry_run:
                store.update(crm.db, p["key"], created_account_ids=created)
        return created[0]

    if p["type"] == "CREATE":
        create_once()

    elif p["action"] == "chow_new_account":
        new_id = create_once()
        old_id = changes["then_set_on_old"]["account_id"]
        crm.write("PATCH", f"/accounts/{old_id}", {"chow_current_account": new_id})  # nothing else, no note

    elif p["type"] == "NOT_ON_WEBSITE":
        wanted = changes["set"]
        payload = {k: v for k, v in wanted.items() if k != "note" and live.get(k) != v}
        payload["note"] = append_note(live.get("note"), wanted["note"])
        crm.write("PATCH", f"/accounts/{changes['account_id']}", payload)

    elif p["type"] in ("UPDATE", "DUPLICATE"):
        payload = {k: v for k, v in changes["set"].items() if live.get(k) != v}  # changed fields only
        if payload:
            crm.write("PATCH", f"/accounts/{changes['account_id']}", payload)

    else:
        raise ValueError(f"Nothing to apply for proposal type {p['type']}")
    return created


# =============================================================================
# Entry point used by the review app
# =============================================================================
def apply_proposal(key, dry_run=None, db=None):
    """Apply (or, in dry-run, preview) one proposal. Returns a result dict:
    outcome = applied | dry_run | stale | failed, plus details."""
    dry_run = config.DRY_RUN if dry_run is None else dry_run
    db = db or store.connect()
    p = store.get(db, key)
    if p is None:
        raise ValueError(f"No proposal with key {key}")
    if p["type"] == "NO_CHANGE":
        return {"outcome": "nothing_to_do", "requests": []}
    if not dry_run and p["status"] != "approved":
        raise ValueError(f"Proposal must be 'approved' before applying (it is '{p['status']}').")

    crm = CrmClient(db, key, dry_run)
    try:
        live, problems = find_stale_problems(crm, p)
        if problems:
            if not dry_run:
                store.update(db, key, status="stale", last_error=" ".join(problems))
            return {"outcome": "stale", "problems": problems, "requests": crm.planned}
        created = run_writes(crm, p, live)
    except (ApiError, requests.RequestException, ValueError) as e:
        if not dry_run:
            store.update(db, key, last_error=str(e))  # stays 'approved' so it can be retried
        return {"outcome": "failed", "error": str(e), "requests": crm.planned}

    if dry_run:
        return {"outcome": "dry_run", "requests": crm.planned}
    store.update(db, key, status="applied", applied_at=store.now(), last_error=None,
                 created_account_ids=created, apply_result={"created_account_ids": created})
    return {"outcome": "applied", "created_account_ids": created, "requests": []}
