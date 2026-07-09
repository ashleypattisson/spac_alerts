#!/usr/bin/env python3
"""
SPAC Deal Alert Monitor — GitHub Actions / cloud version
--------------------------------------------------------
Polls SEC EDGAR for new Form 425 filings (business combination
communications) and SPAC-related 8-Ks, dedupes by accession number,
and pushes alerts via Pushover.

All configuration comes from environment variables (set as GitHub
Secrets) so no credentials ever live in this file:
    PUSHOVER_TOKEN   - your Pushover application/API token
    PUSHOVER_USER    - your Pushover user key
    SEC_EMAIL        - your email (SEC requires a contact in the User-Agent)

State (which filings have been seen) is stored in seen_filings.json,
which the GitHub Actions workflow commits back to the repo after each
run so the memory persists between runs.
"""

import argparse
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests

# --- Config from environment ------------------------------------------------
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "")
SEC_EMAIL = os.environ.get("SEC_EMAIL", "anonymous@example.com")
SEC_USER_AGENT = f"SPACAlertMonitor/1.0 ({SEC_EMAIL})"

STATE_FILE = Path(__file__).parent / "seen_filings.json"
MAX_SEEN = 5000

EDGAR_FEEDS = {
    "425": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=425&company=&dateb=&owner=include&count=100&output=atom",
    "8-K": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb=&owner=include&count=100&output=atom",
}

DEAL_KEYWORDS = [
    r"business combination", r"merger agreement", r"definitive agreement",
    r"acquisition corp", r"acquisition co", r"spac",
]
KEYWORD_RE = re.compile("|".join(DEAL_KEYWORDS), re.IGNORECASE)
SPAC_NAME_RE = re.compile(
    r"acquisition\s+(corp|co|company|holdings?)|capital\s+corp|blank\s+check",
    re.IGNORECASE,
)
ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}Z] {msg}", flush=True)


def load_seen() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            log("WARNING: state file unreadable, starting fresh")
    return {"seen": [], "first_run_done": False}


def save_seen(state: dict) -> None:
    state["seen"] = state["seen"][-MAX_SEEN:]
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE_FILE)


def accession_from_entry(entry_id: str, link: str) -> str:
    m = re.search(r"(\d{10}-\d{2}-\d{6})", entry_id) or re.search(r"(\d{10}-\d{2}-\d{6})", link)
    return m.group(1) if m else entry_id


def fetch_feed(url: str, session: requests.Session, retries: int = 3) -> list:
    resp = None
    for attempt in range(1, retries + 1):
        resp = session.get(url, timeout=30)
        if resp.status_code == 200:
            break
        # SEC occasionally throttles cloud IPs with 403/429; back off and retry
        wait = attempt * 5
        log(f"HTTP {resp.status_code} on {url.split('type=')[-1][:4]} feed, "
            f"retry {attempt}/{retries} in {wait}s")
        time.sleep(wait)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    entries = []
    for e in root.findall("a:entry", ATOM_NS):
        title = (e.findtext("a:title", default="", namespaces=ATOM_NS) or "").strip()
        entry_id = (e.findtext("a:id", default="", namespaces=ATOM_NS) or "").strip()
        updated = (e.findtext("a:updated", default="", namespaces=ATOM_NS) or "").strip()
        link_el = e.find("a:link", ATOM_NS)
        link = link_el.get("href", "") if link_el is not None else ""
        entries.append({
            "title": title, "id": entry_id, "link": link, "updated": updated,
            "accession": accession_from_entry(entry_id, link),
        })
    return entries


def is_alert_worthy(form_type: str, entry: dict) -> bool:
    if form_type == "425":
        return True
    title = entry["title"]
    return bool(KEYWORD_RE.search(title) or SPAC_NAME_RE.search(title))


def send_pushover(title: str, message: str, url: str, url_title: str) -> bool:
    if not PUSHOVER_TOKEN or not PUSHOVER_USER:
        log("ERROR: Pushover credentials missing from environment")
        log(f"ALERT (not sent): {title} | {message} | {url}")
        return False
    try:
        r = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": PUSHOVER_TOKEN, "user": PUSHOVER_USER,
                "title": title, "message": message,
                "url": url, "url_title": url_title, "priority": 0,
            },
            timeout=15,
        )
        if r.status_code == 200:
            return True
        log(f"Pushover error {r.status_code}: {r.text[:200]}")
        return False
    except requests.RequestException as exc:
        log(f"Pushover request failed: {exc}")
        return False


def clean_title(raw_title: str) -> str:
    t = re.sub(r"^\S+\s+-\s+", "", raw_title)
    t = re.sub(r"\s*\(\d{7,10}\)\s*", " ", t)
    return t.strip()


def poll_once(state: dict, session: requests.Session, alerting: bool) -> int:
    seen = set(state["seen"])
    new_alerts = 0
    for form_type, url in EDGAR_FEEDS.items():
        try:
            entries = fetch_feed(url, session)
        except (requests.RequestException, ET.ParseError) as exc:
            log(f"Feed fetch failed for {form_type}: {exc}")
            continue
        log(f"{form_type}: {len(entries)} entries in feed")
        for entry in entries:
            key = f"{form_type}:{entry['accession']}"
            if key in seen:
                continue
            seen.add(key)
            state["seen"].append(key)
            if not is_alert_worthy(form_type, entry):
                continue
            if alerting:
                company = clean_title(entry["title"])
                if send_pushover(f"SPAC {form_type} Filing",
                                 f"{company}\nFiled: {entry['updated']}",
                                 entry["link"], "Open filing on EDGAR"):
                    new_alerts += 1
                    log(f"Alerted: {company}")
                time.sleep(0.5)
            else:
                log(f"Seeded (no alert): {clean_title(entry['title'])}")
    save_seen(state)
    return new_alerts


def main() -> None:
    parser = argparse.ArgumentParser(description="SPAC deal alert monitor (cloud)")
    parser.add_argument("--test-push", action="store_true")
    parser.add_argument("--alert-on-first-run", action="store_true")
    args = parser.parse_args()

    if args.test_push:
        ok = send_pushover("SPAC Alerts — Test",
                           "Cloud runner is wired up correctly.",
                           "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent",
                           "EDGAR latest filings")
        sys.exit(0 if ok else 1)

    session = requests.Session()
    session.headers.update({
        "User-Agent": SEC_USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
        "Accept": "application/atom+xml,text/xml,*/*",
        "Host": "www.sec.gov",
    })

    state = load_seen()
    first_run = not state.get("first_run_done", False)
    alerting = (not first_run) or args.alert_on_first_run
    if first_run:
        log("First run: seeding cache" + ("" if alerting else " (no alerts this pass)"))

    n = poll_once(state, session, alerting)
    if first_run:
        state["first_run_done"] = True
        save_seen(state)
    log(f"Done, {n} alert(s) sent.")


if __name__ == "__main__":
    main()
