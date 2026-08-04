#!/usr/bin/env python3
"""
SPAC Deal Alert Monitor - v2 (sector-aware)
-------------------------------------------
Polls SEC EDGAR for new Form 425 and SPAC-flagged 8-K filings, then:

  1. Skips any SPAC it has already alerted on in the last COOLDOWN_DAYS
     (kills the follow-up roadshow/promo flood - one alert per deal).
  2. Downloads the filing text into memory (nothing written to disk) and
     asks Claude to identify the target's INDUSTRY SECTOR.
  3. Only pushes the alert if that sector is on your watchlist.

Environment variables (set as GitHub Secrets):
    PUSHOVER_TOKEN     - Pushover application/API token
    PUSHOVER_USER      - Pushover user key
    SEC_EMAIL          - your email (SEC requires a contact in User-Agent)
    ANTHROPIC_API_KEY  - key from console.anthropic.com

State lives in seen_filings.json, committed back by the workflow.
"""

import argparse
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

import requests

# ============================================================================
# YOUR SETTINGS - edit these two things and nothing else
# ============================================================================

# Only alert on these sectors. Leave the list EMPTY to get every sector.
# Must match one of the ALLOWED_SECTORS names further down.
SECTOR_WATCHLIST = [
    # "Aerospace & Defense",
    # "Semiconductors",
    # "Energy & Power",
]

# Don't alert on the same SPAC twice within this many days.
COOLDOWN_DAYS = 30

# ============================================================================
# Config
# ============================================================================

PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "")
SEC_EMAIL = os.environ.get("SEC_EMAIL", "anonymous@example.com")
SEC_USER_AGENT = f"SPACAlertMonitor/2.0 ({SEC_EMAIL})"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

STATE_FILE = Path(__file__).parent / "seen_filings.json"
MAX_SEEN = 5000

# How much of the filing to send for classification (characters).
DOC_CHARS = 6000

ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}

EDGAR_FEEDS = {
    "425": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=425"
           "&company=&dateb=&owner=include&count=100&output=atom",
    "8-K": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K"
           "&company=&dateb=&owner=include&count=100&output=atom",
}

DEAL_KEYWORDS = [
    r"business combination", r"merger agreement", r"definitive agreement",
    r"acquisition corp", r"acquisition co", r"spac",
]
KEYWORD_RE = re.compile("|".join(DEAL_KEYWORDS), re.IGNORECASE)

SPAC_NAME_RE = re.compile(
    r"(acquisition\s+(corp|co|company|holdings?|partners)"
    r"|capital\s+corp"
    r"|\bSPAC\b"
    r"|blank\s+check)",
    re.IGNORECASE,
)

ALLOWED_SECTORS = [
    "Aerospace & Defense", "Semiconductors", "Software & Internet",
    "Fintech & Financial Services", "Healthcare & Biotech", "Energy & Power",
    "Mining & Materials", "Industrials & Manufacturing", "Consumer & Retail",
    "Media & Entertainment", "Transport & Logistics", "Real Estate",
    "Crypto & Digital Assets", "Agriculture & Food", "Telecoms",
    "Other", "Unknown",
]

# ============================================================================
# State
# ============================================================================


def load_state():
    if not STATE_FILE.exists():
        return {"seen": [], "cik_alerts": {}}
    try:
        data = json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {"seen": [], "cik_alerts": {}}
    # Tolerate the v1 format (a bare list of accession numbers)
    if isinstance(data, list):
        return {"seen": data, "cik_alerts": {}}
    data.setdefault("seen", [])
    data.setdefault("cik_alerts", {})
    return data


def save_state(state):
    state["seen"] = state["seen"][-MAX_SEEN:]
    cutoff = time.time() - (COOLDOWN_DAYS * 86400)
    state["cik_alerts"] = {
        k: v for k, v in state["cik_alerts"].items() if v > cutoff
    }
    STATE_FILE.write_text(json.dumps(state, indent=1))


def in_cooldown(state, cik):
    last = state["cik_alerts"].get(cik)
    if last is None:
        return False
    return (time.time() - last) < (COOLDOWN_DAYS * 86400)


# ============================================================================
# EDGAR
# ============================================================================


def sec_get(url, timeout=30):
    r = requests.get(
        url,
        headers={"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"},
        timeout=timeout,
    )
    r.raise_for_status()
    time.sleep(0.15)  # stay well inside SEC's 10 requests/second limit
    return r.text


def accession_from_entry(entry_id, link):
    m = re.search(r"accession-number=(\S+)", entry_id or "")
    if m:
        return m.group(1)
    m = re.search(r"(\d{10}-\d{2}-\d{6})", link or "")
    return m.group(1) if m else (entry_id or link)


def cik_from_link(link):
    m = re.search(r"/data/(\d+)/", link or "")
    return m.group(1) if m else None


def clean_title(title):
    t = re.sub(r"^\S+\s+-\s+", "", title or "")
    t = re.sub(r"\s*\(\d{10}\)\s*", " ", t)
    t = re.sub(r"\s*\((Filer|Subject|Issuer|Reporting)\)\s*", "", t)
    return re.sub(r"\s+", " ", t).strip()


def parse_feed(xml_text):
    root = ET.fromstring(xml_text)
    out = []
    for e in root.findall("a:entry", ATOM_NS):
        title = (e.findtext("a:title", default="", namespaces=ATOM_NS) or "").strip()
        entry_id = (e.findtext("a:id", default="", namespaces=ATOM_NS) or "").strip()
        link_el = e.find("a:link", ATOM_NS)
        link = link_el.get("href", "") if link_el is not None else ""
        out.append({
            "title": title,
            "link": link,
            "company": clean_title(title),
            "accession": accession_from_entry(entry_id, link),
            "cik": cik_from_link(link),
        })
    return out


def is_candidate(form_type, entry):
    """425s always qualify. 8-Ks must look SPAC-related."""
    if form_type == "425":
        return True
    title = entry["title"]
    return bool(SPAC_NAME_RE.search(title) or KEYWORD_RE.search(title))


# ============================================================================
# Document fetch (in memory only - nothing saved to disk)
# ============================================================================

TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)


def strip_html(html):
    text = SCRIPT_RE.sub(" ", html)
    text = TAG_RE.sub(" ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_filing_text(index_url, limit=DOC_CHARS):
    """Pull the primary document's text from a filing index page."""
    try:
        index_html = sec_get(index_url)
    except Exception as exc:
        print(f"    ! could not fetch index: {exc}")
        return ""

    hrefs = re.findall(r'href="(/Archives/edgar/data/[^"]+)"', index_html)
    doc = None
    for h in hrefs:
        low = h.lower()
        if low.endswith("-index.htm") or low.endswith("-index.html"):
            continue
        if low.endswith((".htm", ".html", ".txt")):
            doc = h
            break
    if not doc:
        return ""

    try:
        raw = sec_get("https://www.sec.gov" + doc)
    except Exception as exc:
        print(f"    ! could not fetch document: {exc}")
        return ""

    return strip_html(raw)[:limit]


# ============================================================================
# Sector classification
# ============================================================================

CLASSIFY_PROMPT = """You are reading an SEC filing from a SPAC (blank-check company).

Answer three things: (1) does this involve a SPAC / blank-check company, \
(2) does it announce or discuss a business combination with an identified \
target, and (3) what industry does the TARGET operate in.

Respond with ONLY a JSON object, no markdown, no preamble:
{{"is_spac": true/false, "is_deal": true/false, "target": "target company name or null", \
"sector": "one of the allowed sectors", "summary": "max 12 words on what the target does"}}

Allowed sectors: {sectors}

CRITICAL - is_spac: Form 425 is filed by ANY company in a stock-based merger, \
not just SPACs. Set is_spac to true ONLY if one party is a special purpose \
acquisition company / blank-check shell taking a private company public. \
Ordinary corporate M&A between two operating businesses is is_spac FALSE, \
however large the deal. Signals of a real SPAC: a trust account, redemption \
rights, a shell with no operations, "blank check" language, a deadline to \
complete a combination, units/warrants from a recent IPO.

Use "Unknown" for sector if no target is identifiable. Set is_deal to false for \
routine filings (extensions, trust redemptions, IPO closings, auditor changes) \
that do not concern a specific merger target.

FILING TEXT:
{text}"""


def classify(text):
    """Ask Claude for the target's sector. Returns a dict."""
    fallback = {"is_spac": True, "is_deal": True, "target": None,
                "sector": "Unknown", "summary": "", "degraded": True}
    if not ANTHROPIC_API_KEY or not text:
        return fallback

    prompt = CLASSIFY_PROMPT.format(
        sectors=", ".join(ALLOWED_SECTORS), text=text
    )
    try:
        r = requests.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        r.raise_for_status()
        body = "".join(
            b.get("text", "") for b in r.json().get("content", [])
            if b.get("type") == "text"
        )
        body = re.sub(r"^```(?:json)?|```$", "", body.strip(), flags=re.M).strip()
        result = json.loads(body)
        result.setdefault("is_spac", True)
        result.setdefault("is_deal", True)
        result.setdefault("sector", "Unknown")
        result.setdefault("target", None)
        result.setdefault("summary", "")
        result["degraded"] = False
        return result
    except Exception as exc:
        print(f"    ! classification failed ({exc}) - alerting anyway")
        return fallback


def sector_wanted(sector, degraded):
    if not SECTOR_WATCHLIST:
        return True
    if degraded or sector == "Unknown":
        return True  # fail open: never silently lose a real deal
    return sector in SECTOR_WATCHLIST


# ============================================================================
# Pushover
# ============================================================================


def push(title, message, url, url_title="Open filing on EDGAR"):
    if not PUSHOVER_TOKEN or not PUSHOVER_USER:
        print("    ! Pushover credentials missing - not sending")
        return False
    try:
        r = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": PUSHOVER_TOKEN, "user": PUSHOVER_USER,
                "title": title[:250], "message": message[:1024],
                "url": url, "url_title": url_title,
            },
            timeout=20,
        )
        r.raise_for_status()
        return True
    except Exception as exc:
        print(f"    ! Pushover error: {exc}")
        return False


# ============================================================================
# Main
# ============================================================================


def run(alert_on_first_run=False, dry_run=False):
    state = load_state()
    first_run = not state["seen"]
    seen = set(state["seen"])
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    sent = skipped_cik = skipped_sector = skipped_notdeal = skipped_notspac = 0

    for form_type, url in EDGAR_FEEDS.items():
        try:
            entries = parse_feed(sec_get(url))
        except Exception as exc:
            print(f"[{stamp}] {form_type}: feed error: {exc}")
            continue
        print(f"[{stamp}] {form_type}: {len(entries)} entries in feed")

        for e in entries:
            acc = e["accession"]
            if acc in seen:
                continue
            seen.add(acc)
            state["seen"].append(acc)

            if first_run and not alert_on_first_run:
                continue
            if not is_candidate(form_type, e):
                continue

            cik = e["cik"] or e["company"]
            if in_cooldown(state, cik):
                skipped_cik += 1
                continue

            print(f"  → {form_type}: {e['company']}")
            text = fetch_filing_text(e["link"])
            info = classify(text)

            if not info["is_spac"]:
                skipped_notspac += 1
                print("    · ordinary M&A, not a SPAC - skipped")
                continue
            if not info["is_deal"]:
                skipped_notdeal += 1
                print("    · not a deal filing - skipped")
                continue
            if not sector_wanted(info["sector"], info["degraded"]):
                skipped_sector += 1
                print(f"    · sector {info['sector']} not on watchlist - skipped")
                continue

            target = info.get("target") or "target not yet named"
            title = f"{info['sector']} — {e['company']}"
            body_lines = [f"Target: {target}"]
            if info.get("summary"):
                body_lines.append(info["summary"])
            body_lines.append(f"Form {form_type} · {stamp}")
            message = "\n".join(body_lines)

            if dry_run:
                print(f"    [dry run] {title} | {message}")
            elif push(title, message, e["link"]):
                sent += 1
                state["cik_alerts"][cik] = time.time()

    save_state(state)
    if first_run and not alert_on_first_run:
        print(f"[{stamp}] First run - seeded {len(state['seen'])} filings, no alerts.")
    else:
        print(f"[{stamp}] Done. {sent} sent | {skipped_cik} in cooldown | "
              f"{skipped_notspac} not SPACs | {skipped_sector} off-sector | "
              f"{skipped_notdeal} not deals.")


def main():
    p = argparse.ArgumentParser(description="SPAC deal alerts via EDGAR + Pushover")
    p.add_argument("--once", action="store_true", help="run a single poll (default)")
    p.add_argument("--alert-on-first-run", action="store_true",
                   help="alert on the backlog instead of seeding silently")
    p.add_argument("--dry-run", action="store_true",
                   help="print alerts instead of sending them")
    p.add_argument("--test-push", action="store_true",
                   help="send a test notification and exit")
    args = p.parse_args()

    if args.test_push:
        ok = push("SPAC Alerts", "Test notification — setup is working.",
                  "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=425")
        print("Sent." if ok else "Failed.")
        sys.exit(0 if ok else 1)

    run(alert_on_first_run=args.alert_on_first_run, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
