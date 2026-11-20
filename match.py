"""Match website locations to CRM accounts and record proposed changes.

READ-ONLY with respect to the CRM: this script only makes GET requests.
Every proposed change is saved to a local SQLite database (data/pipeline.db)
for a human to approve or reject in the review app.

Safe to re-run, including after changes have been applied:
  * a proposal whose key is already stored is skipped (decided items never return);
  * accounts with chow_current_account set are preserved billing records and are
    left out of everything;
  * accounts already marked duplicate_of_account + Inactive are treated as resolved;
  * an account that already matches its proposed end state is recorded as NO_CHANGE.
"""
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher

import requests
from dotenv import load_dotenv

import config
import store

# Match strength, strongest first
EXACT_ADDRESS, CLOSE_ADDRESS, NAME_ONLY = 3, 2, 1
CONFIDENCE = {EXACT_ADDRESS: "high", CLOSE_ADDRESS: "medium", NAME_ONLY: "medium"}


# =============================================================================
# CRM access (GET only)
# =============================================================================
load_dotenv()
_session = requests.Session()
_session.headers["Authorization"] = f"Bearer {os.getenv('CRM_TOKEN')}"


def crm_get_all(path, **filters):
    """Read every page of a list endpoint and return all rows."""
    rows, page = [], 1
    while True:
        resp = _session.get(f"{config.API_BASE}{path}",
                            params={**filters, "page": page, "page_size": 50}, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        batch = body.get("data", [])
        rows.extend(batch)
        if not batch or len(rows) >= body.get("total", 0):
            return rows
        page += 1


# =============================================================================
# Normalization
# =============================================================================
def _words(text):
    text = (text or "").lower().replace("&", " and ")
    return re.sub(r"[^\w\s]", " ", text).split()


def norm_street(street):
    return " ".join(config.ADDRESS_ABBREVIATIONS.get(w, w) for w in _words(street))


def norm_zip(zip_code):
    return re.sub(r"\D", "", zip_code or "")[:5]


def norm_name(name):
    words = []
    for w in _words(name):
        words.extend(config.NAME_WORD_MAP.get(w, w).split())
    return " ".join(words)


def norm_place(text):
    return " ".join(_words(text))


def similarity(a, b):
    return round(SequenceMatcher(None, a, b).ratio(), 2)


# =============================================================================
# Account state helpers
# =============================================================================
def is_preserved(acct):
    """Old account kept for billing after a change of ownership. Never touched."""
    return bool(acct.get("chow_current_account"))


def is_resolved_duplicate(acct):
    """Already marked as a duplicate and deactivated. Nothing left to do."""
    return bool(acct.get("duplicate_of_account")) and acct.get("status") == "Inactive"


def is_retired(acct):
    """Linked elsewhere (even if not fully resolved), so it cannot be a survivor."""
    return bool(acct.get("duplicate_of_account") or acct.get("chow_current_account"))


def has_chow_risk(acct):
    return (acct.get("lifetime_revenue") or 0) > 0 and (acct.get("outstanding_ar") or 0) > 0


def still_to_do(acct, desired):
    """The subset of desired field values the account does not have yet."""
    return {k: v for k, v in desired.items() if (acct.get(k) or "") != (v or "")}


# =============================================================================
# Matching
# =============================================================================
def compare(loc, acct):
    """Return (strength, evidence list, name similarity) for one location vs one account."""
    evidence = []
    strength = 0
    same_zip = norm_zip(loc["zip"]) == norm_zip(acct["billing_zip"]) != ""
    loc_st, acct_st = norm_street(loc["street"]), norm_street(acct["billing_street"])

    if same_zip and loc_st == acct_st:
        strength = EXACT_ADDRESS
        evidence.append(f"Same street + zip: '{acct['billing_street']}, {acct['billing_zip']}' "
                        f"= website '{loc['street']}, {loc['zip']}'")
    elif same_zip and loc_st and acct_st:
        loc_num, _, loc_rest = loc_st.partition(" ")
        acct_num, _, acct_rest = acct_st.partition(" ")
        street_sim = similarity(loc_rest, acct_rest)
        if loc_num == acct_num and street_sim >= config.STREET_SIMILARITY_THRESHOLD:
            strength = CLOSE_ADDRESS
            evidence.append(f"Near-identical address: '{acct['billing_street']}' vs website "
                            f"'{loc['street']}' (same number + zip, street {street_sim:.0%} similar)")

    same_place = (norm_place(loc["city"]) == norm_place(acct["billing_city"])
                  and loc["state"] == acct["billing_state"])
    name_sim = similarity(norm_name(loc["name"]), norm_name(acct["name"]))
    if same_place and name_sim >= config.NAME_SIMILARITY_THRESHOLD:
        strength = max(strength, NAME_ONLY)
        evidence.append(f"Same city/state ({loc['city']}, {loc['state']}) and name "
                        f"{name_sim:.0%} similar: '{acct['name']}' vs '{loc['name']}'")
    return strength, evidence, name_sim


def find_matches(locations, accounts):
    """For each website slug, return the CRM accounts that match it.

    If one account resembles two locations, it goes to the stronger match only.
    """
    best_for_account = {}
    for loc in locations:
        for acct in accounts:
            strength, evidence, name_sim = compare(loc, acct)
            if not strength:
                continue
            score = (strength, name_sim)
            current = best_for_account.get(acct["account_id"])
            if current is None or score > current["score"]:
                best_for_account[acct["account_id"]] = {
                    "slug": loc["slug"], "acct": acct, "strength": strength,
                    "evidence": evidence, "score": score}
    by_slug = defaultdict(list)
    for m in best_for_account.values():
        by_slug[m["slug"]].append(m)
    return by_slug


def admin_evidence(loc, acct, contacts_by_account):
    """Say whether the website's administrator is one of this account's CRM contacts."""
    admin = loc.get("administrator")
    contacts = contacts_by_account.get(acct["account_id"], [])
    if not admin:
        return "Contacts: website lists no administrator to check."
    found = [c for c in contacts if norm_place(c["name"]) == norm_place(admin)]
    if found:
        c = found[0]
        return (f"Contacts: website administrator '{admin}' IS a contact on this account "
                f"({c.get('title') or 'no title'}{'' if c.get('is_active') else ', inactive'}).")
    if contacts:
        names = ", ".join(f"{c['name']} ({c.get('title') or 'no title'})" for c in contacts)
        return f"Contacts: website administrator '{admin}' is NOT among this account's contacts: {names}."
    return f"Contacts: account has no contacts, so administrator '{admin}' cannot be confirmed."


def choose_survivor(loc, matches, contacts_by_account):
    """Pick the account to keep. Returns (match, reason)."""
    contact_counts = {k: sum(1 for c in v if c.get("is_active")) for k, v in contacts_by_account.items()}
    target = norm_name(loc["name"])

    def rank(m):
        a = m["acct"]
        right_name = norm_name(a["name"]) == target
        right_parent = a["parent_id"] == config.BELLHAVEN_PARENT_ID
        return (right_name and right_parent, right_parent, (a["lifetime_revenue"] or 0) > 0,
                contact_counts.get(a["account_id"], 0), a["status"] == "Active",
                m["strength"], _reverse_id(a["account_id"]))

    ordered = sorted(matches, key=rank, reverse=True)
    win = ordered[0]["acct"]
    labels = ["correct name and parent", "under Bellhaven", "has billing history",
              "contacts", "Active", "address match strength"]
    win_rank, runner_rank = rank(ordered[0]), rank(ordered[1])
    deciding = next((i for i in range(len(labels)) if win_rank[i] != runner_rank[i]), None)
    facts = (f"name/parent correct={win_rank[0]}, under Bellhaven={win_rank[1]}, "
             f"revenue=${win['lifetime_revenue'] or 0:,}, "
             f"contacts={contact_counts.get(win['account_id'], 0)}")
    if deciding is None:
        reason = f"All copies tie on every rule; kept the lowest account id for consistency ({facts})."
    elif labels[deciding] == "contacts":
        reason = f"Tie on name, parent and billing; it has the most contacts ({facts})."
    else:
        reason = f"Decided by rule '{labels[deciding]}' ({facts})."
    return ordered[0], reason


def _reverse_id(account_id):
    # Sorting is descending, so invert the id to make the LOWEST id win a tie.
    return tuple(-ord(c) for c in account_id)


# =============================================================================
# Building proposals
# =============================================================================
def make_proposal(ptype, action, *, account=None, loc=None, changes, evidence,
                  confidence, summary, current=None, related_ids=()):
    subject = account["account_id"] if account else f"web:{loc['slug']}"
    fingerprint = hashlib.sha1(json.dumps(changes, sort_keys=True).encode()).hexdigest()[:12]
    return {
        "key": f"{ptype}|{subject}|{fingerprint}",
        "type": ptype,
        "action": action,
        # First id is the account being changed; any others are related (e.g. the survivor).
        "account_ids": ([account["account_id"]] if account else []) + list(related_ids),
        "crm_name": account["name"] if account else "",
        "website_slug": loc["slug"] if loc else "",
        "website_url": loc["url"] if loc else "",
        "website_name": loc["name"] if loc else "",
        "current_values": current if current is not None else (account or {}),
        "proposed_changes": changes,
        "evidence": evidence,
        "confidence": confidence,
        "summary": summary,
    }


def no_change(account, loc, evidence, confidence, summary):
    return make_proposal("NO_CHANGE", "none", account=account, loc=loc, changes={},
                         evidence=evidence, confidence=confidence, summary=summary)


def new_account_fields(loc, note):
    care = [config.CARE_TYPE_MAP[c] for c in loc["care_offerings"] if c in config.CARE_TYPE_MAP]
    return {
        "name": loc["name"], "parent_id": config.BELLHAVEN_PARENT_ID,
        "billing_street": loc["street"], "billing_city": loc["city"],
        "billing_state": loc["state"], "billing_zip": loc["zip"],
        "care_type": care[0] if care else "", "phone": loc["phone"],
        "status": "Active", "note": note,
    }


def survivor_fixes(loc, acct):
    """Fields on the survivor that differ from the website."""
    fixes = {}
    if acct["name"] != loc["name"]:
        fixes["name"] = loc["name"]
    if acct["parent_id"] != config.BELLHAVEN_PARENT_ID:
        fixes["parent_id"] = config.BELLHAVEN_PARENT_ID
    if config.FIX_ADDRESS:
        if norm_street(acct["billing_street"]) != norm_street(loc["street"]):
            fixes["billing_street"] = loc["street"]
        if norm_place(acct["billing_city"]) != norm_place(loc["city"]):
            fixes["billing_city"] = loc["city"]
        if acct["billing_state"] != loc["state"]:
            fixes["billing_state"] = loc["state"]
        if norm_zip(acct["billing_zip"]) != norm_zip(loc["zip"]):
            fixes["billing_zip"] = loc["zip"]
    if config.FIX_CARE_TYPE:
        offered = [config.CARE_TYPE_MAP[c] for c in loc["care_offerings"] if c in config.CARE_TYPE_MAP]
        if offered and acct["care_type"] not in offered:
            fixes["care_type"] = offered[0]
    if acct["status"] != "Active":
        fixes["status"] = "Active"
    return fixes


def describe(fixes, acct, parent_names):
    parts = []
    for field, new in fixes.items():
        old = acct.get(field, "")
        if field == "parent_id":
            old = parent_names.get(old, "(none)") if old else "(none)"
            new = parent_names.get(new, new)
            field = "parent"
        parts.append(f"{field}: '{old}' -> '{new}'")
    return "; ".join(parts)


def proposals_for_location(loc, matches, contacts_by_account, parent_names, all_accounts):
    out = []

    # ---- No CRM account at all -> CREATE --------------------------------------
    if not matches:
        evidence = ["No CRM account shares this street + zip, and none in "
                    f"{loc['city']}, {loc['state']} has a similar name."]
        near = []
        for a in all_accounts:
            sim = similarity(norm_name(loc["name"]), norm_name(a["name"]))
            same_place = norm_place(a["billing_city"]) == norm_place(loc["city"]) and a["billing_state"] == loc["state"]
            if same_place:
                near.append(f"Checked and rejected - same city but different address, name only "
                            f"{sim:.0%} similar: '{a['name']}' at {a['billing_street']} "
                            f"({a['account_id']}, {parent_names.get(a['parent_id'], 'no parent')}). "
                            + admin_evidence(loc, a, contacts_by_account))
            elif sim >= config.NAME_SIMILARITY_THRESHOLD:
                near.append(f"Similar name in a different place, ignored: '{a['name']}' in "
                            f"{a['billing_city']}, {a['billing_state']} ({a['account_id']})")
        note = f"Created from the Bellhaven website listing: {loc['url']}"
        out.append(make_proposal(
            "CREATE", "create_account", loc=loc, current={},
            changes={"create": new_account_fields(loc, note)},
            evidence=evidence + near, confidence="medium" if near else "high",
            summary=f"Create '{loc['name']}' under Bellhaven ({loc['street']}, {loc['city']}, {loc['state']})"))
        return out

    # ---- Pick the survivor -------------------------------------------------------
    candidates = [m for m in matches if not is_retired(m["acct"])] or matches
    if len(candidates) > 1:
        survivor_m, survivor_reason = choose_survivor(loc, candidates, contacts_by_account)
    else:
        survivor_m, survivor_reason = candidates[0], "Only active match for this location."
    survivor = survivor_m["acct"]
    sid = survivor["account_id"]

    # ---- Losing copies -> DUPLICATE (or CHOW link if it has billing exposure) -
    for m in matches:
        a = m["acct"]
        if a is survivor:
            continue
        ev = m["evidence"] + [admin_evidence(loc, a, contacts_by_account),
                              f"Survivor: '{survivor['name']}' ({sid}). {survivor_reason}"]
        if has_chow_risk(a):
            ev.append(f"Has lifetime_revenue ${a['lifetime_revenue']:,} AND outstanding_ar "
                      f"${a['outstanding_ar']:,}, so it is NOT marked duplicate; CHOW link only.")
            action, desired = "chow_link", {"chow_current_account": sid}
            summary = f"Set chow_current_account -> {sid} (no other change)"
        else:
            ev.append(f"Billing check passed: lifetime_revenue ${a['lifetime_revenue'] or 0:,}, "
                      f"outstanding_ar ${a['outstanding_ar'] or 0:,}.")
            action, desired = "mark_duplicate", {"duplicate_of_account": sid, "status": "Inactive"}
            summary = f"Duplicate of {sid} ('{survivor['name']}'); status -> Inactive"
        todo = still_to_do(a, desired)
        if not todo:
            out.append(no_change(a, loc, ev, CONFIDENCE[m["strength"]], "Already linked to the survivor"))
            continue
        out.append(make_proposal(
            "DUPLICATE", action, account=a, loc=loc,
            changes={"account_id": a["account_id"], "set": todo},
            evidence=ev, confidence=CONFIDENCE[m["strength"]], related_ids=[sid], summary=summary))

    # ---- The survivor itself -> NO_CHANGE / UPDATE / UPDATE via CHOW -----------
    ev = list(survivor_m["evidence"]) + [admin_evidence(loc, survivor, contacts_by_account)]
    if len(matches) > 1:
        ev.append(f"Chosen as survivor of {len(matches)} copies. {survivor_reason}")
    confidence = CONFIDENCE[survivor_m["strength"]]
    fixes = survivor_fixes(loc, survivor)

    if not fixes:
        out.append(no_change(survivor, loc, ev, confidence,
                             "Name, parent and address already match the website"))
    elif "parent_id" in fixes and has_chow_risk(survivor):
        old_parent = parent_names.get(survivor["parent_id"], "(none)")
        ev.append(f"Parent change needed ({old_parent} -> Bellhaven) but account has lifetime_revenue "
                  f"${survivor['lifetime_revenue']:,} AND outstanding_ar ${survivor['outstanding_ar']:,}: "
                  "CHOW rule applies. Old account is left exactly as is.")
        note = (f"Change of ownership: replaces {sid} ('{survivor['name']}', {old_parent}). "
                f"Source: {loc['url']}")
        out.append(make_proposal(
            "UPDATE", "chow_new_account", account=survivor, loc=loc,
            changes={"create": new_account_fields(loc, note),
                     "then_set_on_old": {"account_id": sid,
                                         "set": {"chow_current_account": "<NEW_ACCOUNT_ID>"}}},
            evidence=ev, confidence=confidence,
            summary=f"CHOW: create new '{loc['name']}' under Bellhaven; link old account to it"))
    else:
        if "parent_id" in fixes:
            ev.append(f"Parent change allowed: lifetime_revenue ${survivor['lifetime_revenue'] or 0:,}, "
                      f"outstanding_ar ${survivor['outstanding_ar'] or 0:,} (not both > 0).")
        action = "reparent" if "parent_id" in fixes else "fix_fields"
        out.append(make_proposal(
            "UPDATE", action, account=survivor, loc=loc,
            changes={"account_id": sid, "set": fixes},
            evidence=ev, confidence=confidence,
            summary=describe(fixes, survivor, parent_names)))
    return out


def proposals_not_on_website(accounts, matched_ids, parent_names):
    out = []
    for a in accounts:
        if a["parent_id"] != config.BELLHAVEN_PARENT_ID or a["account_id"] in matched_ids:
            continue
        ev = [f"Filed under Bellhaven, but no website location matches its address "
              f"({a['billing_street']}, {a['billing_city']}, {a['billing_state']} {a['billing_zip']}) or name."]
        billing = (f"Billing: lifetime_revenue ${a['lifetime_revenue'] or 0:,}, "
                   f"outstanding_ar ${a['outstanding_ar'] or 0:,}.")

        # Same normalized address under another parent = facility was sold.
        street, zip5 = norm_street(a["billing_street"]), norm_zip(a["billing_zip"])
        new_owner = [o for o in accounts
                     if o is not a and street and o["parent_id"] != a["parent_id"]
                     and not is_retired(o)
                     and norm_street(o["billing_street"]) == street and norm_zip(o["billing_zip"]) == zip5]
        if new_owner:
            # Prefer an Active account, then the lowest id, so the choice is stable.
            target = sorted(new_owner, key=lambda o: (o["status"] != "Active", o["account_id"]))[0]
            tid = target["account_id"]
            owner = parent_names.get(target["parent_id"], "no parent")
            ev.append(f"Same address as '{target['name']}' ({tid}, {owner}): "
                      f"'{target['billing_street']}, {target['billing_zip']}' = "
                      f"'{a['billing_street']}, {a['billing_zip']}'. Facility appears to have been sold to {owner}.")
            if len(new_owner) > 1:
                ev.append("Other accounts at this address: " + ", ".join(
                    f"'{o['name']}' ({o['account_id']})" for o in new_owner if o is not target))
            if has_chow_risk(a):
                ev += [billing, "Has revenue AND outstanding AR, so it is NOT marked duplicate; CHOW link only."]
                action, desired = "chow_link", {"chow_current_account": tid}
                summary = f"Sold to {owner}: set chow_current_account -> {tid} ('{target['name']}'), no other change"
            else:
                ev += [billing, "Billing check passed (not both > 0)."]
                action, desired = "mark_duplicate", {"duplicate_of_account": tid, "status": "Inactive"}
                summary = f"Sold to {owner}: duplicate of {tid} ('{target['name']}'); status -> Inactive"
            todo = still_to_do(a, desired)
            if todo:
                out.append(make_proposal(
                    "DUPLICATE", action, account=a, changes={"account_id": a["account_id"], "set": todo},
                    evidence=ev, confidence="high", related_ids=[tid], summary=summary))
            else:
                out.append(no_change(a, None, ev, "high", "Already linked to the new owner's account"))
            continue

        ev.append("No other account shares this address.")
        if a["lifetime_revenue"] or a["outstanding_ar"]:
            ev.append(billing)
        if a["status"] != "Active":
            out.append(no_change(a, None, ev, "medium", f"Already '{a['status']}'; nothing to change"))
            continue
        note = "No longer listed on the Bellhaven website (community directory); please review."
        out.append(make_proposal(
            "NOT_ON_WEBSITE", "needs_review", account=a,
            changes={"account_id": a["account_id"], "set": {"status": "Needs Review", "note": note}},
            evidence=ev, confidence="medium",
            summary="status -> Needs Review, add note"))
    return out


# =============================================================================
# Main
# =============================================================================
def main():
    print("Reading CRM (GET only)...")
    accounts = crm_get_all("/accounts")
    contacts = crm_get_all("/contacts")
    print(f"  {len(accounts)} accounts, {len(contacts)} contacts")

    by_id = {a["account_id"]: a for a in accounts}
    parent = by_id.get(config.BELLHAVEN_PARENT_ID)
    if not parent or parent["name"] != config.BELLHAVEN_PARENT_NAME:
        raise SystemExit(f"BELLHAVEN_PARENT_ID in config.py does not point to "
                         f"'{config.BELLHAVEN_PARENT_NAME}'. Fix it and re-run.")
    parent_names = {a["account_id"]: a["name"].replace(" (Parent Account)", "") for a in accounts}
    contacts_by_account = defaultdict(list)
    for c in contacts:
        contacts_by_account[c["account_id"]].append(c)

    with open(config.WEBSITE_FILE, encoding="utf-8") as f:
        locations = json.load(f)
    for loc in locations:
        loc["slug"] = loc["url"].rstrip("/").rsplit("/", 1)[-1]
        for label in loc["care_offerings"]:
            if label not in config.CARE_TYPE_MAP:
                print(f"  ! Unmapped care label '{label}' on {loc['name']} - add it to CARE_TYPE_MAP")
    print(f"  {len(locations)} website locations")

    # Leave out parent accounts (no address), preserved CHOW records and resolved duplicates.
    with_address = [a for a in accounts if a["billing_street"] or a["billing_city"]]
    preserved = [a for a in with_address if is_preserved(a)]
    resolved = [a for a in with_address if not is_preserved(a) and is_resolved_duplicate(a)]
    facilities = [a for a in with_address if not is_preserved(a) and not is_resolved_duplicate(a)]
    print(f"  Skipping {len(preserved)} preserved CHOW record(s) and {len(resolved)} resolved duplicate(s)")

    matches = find_matches(locations, facilities)
    matched_ids = {m["acct"]["account_id"] for ms in matches.values() for m in ms}

    proposals = []
    for loc in locations:
        proposals += proposals_for_location(loc, matches.get(loc["slug"], []),
                                            contacts_by_account, parent_names, facilities)
    proposals += proposals_not_on_website(facilities, matched_ids, parent_names)

    saved = store.save_proposals(proposals)

    # ---- Summary ------------------------------------------------------------------
    order = ["NO_CHANGE", "UPDATE", "CREATE", "DUPLICATE", "NOT_ON_WEBSITE"]
    counts = Counter(p["type"] for p in proposals)
    new_count = sum(1 for s in saved.values() if s == "NEW" or s.startswith("REFRESHED"))
    print(f"\n=== {len(proposals)} results ({new_count} new, "
          f"{len(proposals) - new_count} already in the database) ===")
    for t in order:
        actions = Counter(p["action"] for p in proposals if p["type"] == t)
        detail = ", ".join(f"{k}: {v}" for k, v in actions.items())
        print(f"  {t:<15} {counts.get(t, 0):>3}   ({detail})")

    print()
    for t in order:
        for p in sorted((p for p in proposals if p["type"] == t), key=lambda p: p["website_name"] or p["crm_name"]):
            acct = f"{p['crm_name']} [{p['account_ids'][0]}]" if p["account_ids"] else "(no CRM account)"
            web = p["website_name"] or "(not on website)"
            flag = "" if saved[p["key"]] == "NEW" else f"  <{saved[p['key']]}>"
            print(f"{t:<14} | {acct:<62} | web: {web:<48} | {p['summary']} | {p['confidence']}{flag}")
    print(f"\nSaved to {config.DB_PATH}. No changes were sent to the CRM.")


if __name__ == "__main__":
    main()
