"""Bellhaven sync review app.

Start it with:  .venv\\Scripts\\streamlit run review_app.py

Shows every proposal from data/pipeline.db. Approve sends the change to the CRM
(or, while DRY_RUN is on in config.py, only shows the requests it would send).
"""
import json
import os

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import applier
import config
import store

load_dotenv()
TOKEN = os.getenv("CRM_TOKEN", "")

st.set_page_config(page_title="Bellhaven Sync Review", layout="wide")


# =============================================================================
# Small helpers
# =============================================================================
def md(text):
    """Escape '$' so Streamlit doesn't read dollar amounts as math formulas."""
    return str(text).replace("$", "\\$")


def crm_link(account_id):
    return f"{config.SITE_URL}/crm/{TOKEN}/accounts/{account_id}"


def show(value):
    return "(empty)" if value in (None, "") else str(value)


def parent_label(parent_id, current):
    if not parent_id:
        return "(none)"
    if parent_id == config.BELLHAVEN_PARENT_ID:
        return f"{config.BELLHAVEN_PARENT_NAME} [{parent_id}]"
    if parent_id == current.get("parent_id") and current.get("parent_name"):
        return f"{current['parent_name']} [{parent_id}]"
    return parent_id


def change_tables(p):
    """(title, rows) tables showing before -> after for every field that changes."""
    changes, cur = p["proposed_changes"], p["current_values"]
    tables = []

    if "create" in changes:
        payload = applier.new_account_payload(p)
        rows = [{"Field": k, "Before": "-", "After": parent_label(v, cur) if k == "parent_id" else show(v)}
                for k, v in payload.items()]
        tables.append(("New account - POST /accounts", rows))

    if "then_set_on_old" in changes:
        old = changes["then_set_on_old"]
        rows = [{"Field": k, "Before": show(cur.get(k)),
                 "After": "id of the new account above" if v == applier.NEW_ID_PLACEHOLDER else show(v)}
                for k, v in old["set"].items()]
        tables.append((f"Old account {old['account_id']} - PATCH (nothing else on it changes, no note)", rows))

    if "set" in changes:
        rows = []
        for k, v in changes["set"].items():
            before, after = show(cur.get(k)), show(v)
            if k == "parent_id":
                before, after = parent_label(cur.get(k), cur), parent_label(v, cur)
            elif k == "note":
                after = applier.append_note(cur.get("note"), v)
            rows.append({"Field": k, "Before": before, "After": after})
        tables.append((f"Account {changes['account_id']} - PATCH", rows))
    return tables


def show_apply_result(result):
    outcome = result["outcome"]
    if outcome == "dry_run":
        st.info("DRY RUN: the checks passed. These are the exact requests Approve would send (nothing was sent):")
    elif outcome == "stale":
        st.warning("Stale: the CRM changed since this was proposed, so nothing would be written.")
        for prob in result["problems"]:
            st.markdown(f"- {md(prob)}")
    elif outcome == "failed":
        st.error(f"Failed: {result['error']}")
    elif outcome == "applied":
        st.success(f"Applied. Created account ids: {result['created_account_ids'] or 'none'}")
    for r in result.get("requests", []):
        st.code(f"{r['method']} {r['url']}\n{json.dumps(r['json'], indent=2)}", language="json")


def render_card(p):
    """Everything about one proposal except the action buttons."""
    st.markdown(f"#### {p['type']} &nbsp;·&nbsp; `{p['action']}` &nbsp;·&nbsp; confidence **{p['confidence']}**"
                f" &nbsp;·&nbsp; status `{p['status']}`")
    st.markdown(f"CRM: **{md(p['crm_name'] or '(new account)')}** &nbsp;→&nbsp; "
                f"Website: **{md(p['website_name'] or '(not on website)')}**")
    st.caption(md(p["summary"]))

    for title, rows in change_tables(p):
        st.markdown(f"*{title}*")
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    with st.expander("Evidence", expanded=True):
        for e in p["evidence"]:
            st.markdown(f"- {md(e)}")

    links = []
    if p["website_url"]:
        links.append(f"[Website page]({p['website_url']})")
    for i, aid in enumerate(p["account_ids"]):
        label = "CRM account" if i == 0 else "Related CRM account (survivor / new owner)"
        links.append(f"[{label} {aid}]({crm_link(aid)})")
    for aid in p["created_account_ids"]:
        links.append(f"[Created account {aid}]({crm_link(aid)})")
    st.markdown(" &nbsp;|&nbsp; ".join(links))


# =============================================================================
# Page
# =============================================================================
db = store.connect()
proposals = store.list_all(db)

st.title("Bellhaven Sync - Review")
if config.DRY_RUN:
    st.info("**DRY RUN is ON** (config.py). Approve only previews the API requests. "
            "Nothing is sent to the CRM and no status changes.")
else:
    st.warning("**LIVE MODE.** Approve writes to the CRM.")

if "flash" in st.session_state:
    kind, text = st.session_state.pop("flash")
    getattr(st, kind)(text)

if not proposals:
    st.warning("No proposals yet. Run `match.py` first.")
    st.stop()

# ---- Counts -------------------------------------------------------------------
counts = pd.crosstab(pd.Series([p["type"] for p in proposals], name="type"),
                     pd.Series([p["status"] for p in proposals], name="status"),
                     margins=True, margins_name="Total")
st.dataframe(counts, use_container_width=True)

# ---- Filters ------------------------------------------------------------------
st.sidebar.header("Filters")
all_types = sorted({p["type"] for p in proposals if p["type"] != "NO_CHANGE"})
types = st.sidebar.multiselect("Type", all_types, default=all_types)
confs = st.sidebar.multiselect("Confidence", ["high", "medium", "low"], default=["high", "medium", "low"])
search = st.sidebar.text_input("Search name or id").strip().lower()


def keep(p):
    text = " ".join([p["crm_name"] or "", p["website_name"] or "", *p["account_ids"]]).lower()
    return p["confidence"] in confs and (not search or search in text)


changes = [p for p in proposals if p["type"] in types and keep(p)]
pending = [p for p in changes if p["status"] == "pending"]
done = [p for p in changes if p["status"] in ("approved", "applied", "stale")]
rejected = [p for p in changes if p["status"] == "rejected"]
confirmed = [p for p in proposals if p["type"] == "NO_CHANGE" and keep(p)]

failed = [p for p in proposals if p["status"] == "approved" and p["last_error"]]
if failed:
    st.error(f"{len(failed)} approved change(s) failed to apply. See the Approved / Applied tab to retry.")

tab_pending, tab_done, tab_rejected, tab_confirmed, tab_audit = st.tabs([
    f"Pending ({len(pending)})", f"Approved / Applied ({len(done)})", f"Rejected ({len(rejected)})",
    f"Confirmed matches ({len(confirmed)})", "Audit log"])

# ---- Pending -----------------------------------------------------------------
with tab_pending:
    if not pending:
        st.write("Nothing pending.")
    for p in pending:
        k = p["key"]
        with st.container(border=True):
            render_card(p)
            c1, c2, c3 = st.columns([1.2, 3, 1])
            approve_label = "Approve (dry-run preview)" if config.DRY_RUN else "Approve & apply"
            if c1.button(approve_label, key=f"approve-{k}", type="primary"):
                if config.DRY_RUN:
                    st.session_state[f"preview-{k}"] = applier.apply_proposal(k, dry_run=True, db=db)
                else:
                    store.approve(db, k)
                    result = applier.apply_proposal(k, db=db)
                    kind = {"applied": "success", "stale": "warning"}.get(result["outcome"], "error")
                    st.session_state["flash"] = (kind, f"{p['type']} for {p['crm_name'] or p['website_name']}: "
                                                       f"{result['outcome']}. {result.get('error', '')}")
                    st.rerun()
            reason = c2.text_input("Reject reason (optional)", key=f"reason-{k}",
                                   label_visibility="collapsed", placeholder="Reject reason (optional)")
            if c3.button("Reject", key=f"reject-{k}"):
                store.reject(db, k, reason)
                st.session_state["flash"] = ("info", f"Rejected: {p['crm_name'] or p['website_name']}")
                st.rerun()
            if f"preview-{k}" in st.session_state:
                show_apply_result(st.session_state[f"preview-{k}"])

# ---- Approved / Applied -------------------------------------------------------
with tab_done:
    if not done:
        st.write("Nothing approved yet.")
    for p in done:
        k = p["key"]
        with st.container(border=True):
            render_card(p)
            if p["status"] == "applied":
                st.success(f"Applied {p['applied_at']}. Created account ids: {p['created_account_ids'] or 'none'}")
            elif p["status"] == "stale":
                st.warning(f"Stale - nothing was written. {md(p['last_error'] or '')} "
                           "Re-run match.py to get a fresh proposal for review.")
            elif p["status"] == "approved":
                if p["last_error"]:
                    st.error(f"Write failed: {p['last_error']}")
                label = "Retry (dry-run preview)" if config.DRY_RUN else "Retry"
                if st.button(label, key=f"retry-{k}", type="primary"):
                    if config.DRY_RUN:
                        st.session_state[f"preview-{k}"] = applier.apply_proposal(k, dry_run=True, db=db)
                    else:
                        result = applier.apply_proposal(k, db=db)
                        st.session_state["flash"] = ("success" if result["outcome"] == "applied" else "error",
                                                     f"Retry: {result['outcome']}. {result.get('error', '')}")
                        st.rerun()
                if f"preview-{k}" in st.session_state:
                    show_apply_result(st.session_state[f"preview-{k}"])
            calls = [r for r in store.audit_rows(db, k) if not r["dry_run"]]
            if calls:
                with st.expander(f"API calls for this proposal ({len(calls)})"):
                    st.dataframe(pd.DataFrame(calls)[["ts", "method", "endpoint", "status_code", "payload"]],
                                 hide_index=True, use_container_width=True)

# ---- Rejected -------------------------------------------------------------------
with tab_rejected:
    if not rejected:
        st.write("Nothing rejected.")
    for p in rejected:
        with st.container(border=True):
            render_card(p)
            st.info(f"Rejected {p['decided_at']}. Reason: {p['decision_reason'] or '(none given)'}")

# ---- Confirmed matches ------------------------------------------------------------
with tab_confirmed:
    st.caption("These CRM accounts already match the website. Nothing to approve.")
    st.dataframe(pd.DataFrame([{
        "CRM account": p["crm_name"], "Account id": p["account_ids"][0] if p["account_ids"] else "",
        "Website": p["website_name"] or "(not on website)", "Confidence": p["confidence"],
        "Why": p["summary"], "Evidence": " | ".join(p["evidence"]),
    } for p in confirmed]), hide_index=True, use_container_width=True)

# ---- Audit log --------------------------------------------------------------------
with tab_audit:
    show_dry = st.checkbox("Include dry-run previews", value=False)
    rows = [r for r in store.audit_rows(db) if show_dry or not r["dry_run"]]
    if rows:
        st.dataframe(pd.DataFrame(rows)[["ts", "proposal_key", "method", "endpoint", "status_code",
                                         "dry_run", "payload", "response"]],
                     hide_index=True, use_container_width=True)
    else:
        st.write("No API calls logged yet.")
