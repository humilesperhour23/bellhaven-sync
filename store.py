"""Local SQLite storage for proposals and the API audit log (data/pipeline.db)."""
import json
import os
import sqlite3
from datetime import datetime, timezone

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    key                 TEXT PRIMARY KEY,
    type                TEXT NOT NULL,     -- NO_CHANGE | UPDATE | CREATE | DUPLICATE | NOT_ON_WEBSITE
    action              TEXT NOT NULL,
    status              TEXT NOT NULL,     -- pending | approved | rejected | applied | stale | info
    account_ids         TEXT NOT NULL,     -- JSON list: [account being changed, related ids...]
    crm_name            TEXT,
    website_slug        TEXT,
    website_url         TEXT,
    website_name        TEXT,
    current_values      TEXT NOT NULL,     -- JSON: the account as the matcher saw it
    proposed_changes    TEXT NOT NULL,     -- JSON
    evidence            TEXT NOT NULL,     -- JSON list
    confidence          TEXT NOT NULL,     -- high | medium | low
    summary             TEXT,
    created_at          TEXT NOT NULL,
    decided_at          TEXT,
    decision_reason     TEXT,
    applied_at          TEXT,
    apply_result        TEXT,              -- JSON
    created_account_ids TEXT,              -- JSON list of accounts created by POST
    last_error          TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    proposal_key TEXT,
    method       TEXT NOT NULL,
    endpoint     TEXT NOT NULL,
    payload      TEXT,
    status_code  INTEGER,                  -- NULL for dry-run or network failure
    response     TEXT,
    dry_run      INTEGER NOT NULL DEFAULT 0
);
"""
JSON_FIELDS = ["account_ids", "current_values", "proposed_changes", "evidence",
               "apply_result", "created_account_ids"]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    db = sqlite3.connect(config.DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def _decode(row):
    p = dict(row)
    for f in JSON_FIELDS:
        p[f] = json.loads(p[f]) if p.get(f) else ([] if f in ("account_ids", "evidence", "created_account_ids") else {})
    return p


def get(db, key):
    row = db.execute("SELECT * FROM proposals WHERE key = ?", (key,)).fetchone()
    return _decode(row) if row else None


def list_all(db):
    return [_decode(r) for r in db.execute("SELECT * FROM proposals ORDER BY type, website_name, crm_name")]


def update(db, key, **fields):
    for f in JSON_FIELDS:
        if f in fields and not isinstance(fields[f], str) and fields[f] is not None:
            fields[f] = json.dumps(fields[f])
    sets = ", ".join(f"{k} = ?" for k in fields)
    db.execute(f"UPDATE proposals SET {sets} WHERE key = ?", (*fields.values(), key))
    db.commit()


def approve(db, key):
    update(db, key, status="approved", decided_at=now(), last_error=None)


def reject(db, key, reason=""):
    update(db, key, status="rejected", decided_at=now(), decision_reason=reason or None)


def save_proposals(proposals):
    """Insert new proposals. A key already in the database is skipped, so decided
    items are never re-proposed. Exception: a 'stale' proposal (the CRM changed
    under it before it could be applied) is replaced by the fresh version."""
    db = connect()
    result = {}
    for p in proposals:
        row = db.execute("SELECT status, created_account_ids FROM proposals WHERE key = ?",
                         (p["key"],)).fetchone()
        refresh = row is not None and row["status"] == "stale" and not json.loads(row["created_account_ids"] or "[]")
        if row is not None and not refresh:
            result[p["key"]] = f"already stored ({row['status']})"
            continue
        status = "info" if p["type"] == "NO_CHANGE" else "pending"
        db.execute(
            "INSERT OR REPLACE INTO proposals (key, type, action, status, account_ids, crm_name,"
            " website_slug, website_url, website_name, current_values, proposed_changes, evidence,"
            " confidence, summary, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p["key"], p["type"], p["action"], status, json.dumps(p["account_ids"]), p["crm_name"],
             p["website_slug"], p["website_url"], p["website_name"], json.dumps(p["current_values"]),
             json.dumps(p["proposed_changes"]), json.dumps(p["evidence"]), p["confidence"],
             p["summary"], now()))
        result[p["key"]] = "REFRESHED (was stale)" if refresh else "NEW"
    db.commit()
    db.close()
    return result


def log_call(db, proposal_key, method, endpoint, payload, status_code, response, dry_run):
    db.execute(
        "INSERT INTO audit_log (ts, proposal_key, method, endpoint, payload, status_code, response, dry_run)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (now(), proposal_key, method, endpoint, json.dumps(payload) if payload is not None else None,
         status_code, (response or "")[:2000], int(dry_run)))
    db.commit()


def audit_rows(db, proposal_key=None, limit=500):
    if proposal_key:
        q = db.execute("SELECT * FROM audit_log WHERE proposal_key = ? ORDER BY id DESC LIMIT ?", (proposal_key, limit))
    else:
        q = db.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in q]
