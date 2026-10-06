#!/usr/bin/env python3
"""
SPAC Alert Monitor - v3 (new listings only)
-------------------------------------------
Alerts ONLY when a brand-new SPAC is about to start trading. Merger / deal
news is no longer watched at all.

How it works:

  1. Polls SEC EDGAR for the two forms a SPAC files as it lists:
       8-A12B  - "listing paperwork", filed just before trading starts
       424B4   - final prospectus, filed once the IPO is priced
       (424B1 is also watched - a few SPACs use it instead of 424B4)
  2. Keeps only blank-check companies (SEC industry code 6770) that have
     never filed a quarterly/annual report - i.e. genuinely new SPACs.
  3. Reads the prospectus cover (in memory, nothing saved) and asks Claude
     for the ticker, size and cash in trust per unit.
  4. Sends ONE alert per SPAC.

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
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

import requests

try:
    from zoneinfo import ZoneInfo
    NEW_YORK = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - tz database missing
    NEW_YORK = timezone(timedelta(hours=-5))

# ============================================================================
# YOUR SETTINGS
# ============================================================================

# False = one alert per SPAC (normally the day before it starts trading).
# True  = also send a second "priced" alert when the final prospectus lands.
ALSO_ALERT_ON_PRICING = False

# ============================================================================
# Config
# ============================================================================

PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "")
SEC_EMAIL = os.environ.get("SEC_EMAIL", "anonymous@example.com")
SEC_USER_AGENT = f"SPACAlertMonitor/3.0 ({SEC_EMAIL})"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

STATE_FILE = Path(__file__).parent / "seen_filings.json"
MAX_SEEN = 5000
REMEMBER_DAYS = 400          # how long we remember a SPAC we've alerted on

DOC_BYTES = 900_000          # read at most this much of a prospectus
DOC_CHARS = 40_000           # ...and send this much text to the classifier

ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}

_FEED = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type={}"
         "&company=&dateb=&owner=include&count=100&output=atom")
EDGAR_FEEDS = {
    "8-A12B": _FEED.format("8-A12B"),
    "424B4": _FEED.format("424B4"),
    "424B1": _FEED.format("424B1"),
}
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{:010d}.json"

BLANK_CHECK_SIC = "6770"

SPAC_NAME_RE = re.compile(
    r"(acquisition\s+(corp|co\b|company|holdings?|partners|inc|ltd|limited)"
    r"|\bSPAC\b"
    r"|blank\s+check)",
    re.IGNORECASE,
)

# A company still waiting to IPO has one of these on file...
IPO_FORMS = {"S-1", "S-1/A", "F-1", "F-1/A", "424B4", "424B1"}
# ...and none of these. Any of them means it is already public or is the
# shell for a merger, so an 8-A from it is NOT a new SPAC listing.
SEASONED_FORMS = {
    "10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "10-KT",
    "S-4", "S-4/A", "F-4", "F-4/A", "425", "DEFM14A", "PREM14A",
    "25", "25-NSE", "15-12B", "15-12G",
}
# Best document to read details from, in order of preference.
PROSPECTUS_FORMS = ["424B4", "424B1", "S-1/A", "F-1/A", "S-1", "F-1"]

# ============================================================================
# State
# ============================================================================


def load_state():
    blank = {"seen": [], "ipo_alerts": {}, "priced_alerts": {}, "seeded_feeds": []}
    if not STATE_FILE.exists():
        return blank
    try:
        data = json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return blank
    if isinstance(data, list):          # very old format: bare list
        data = {"seen": data}
    data.pop("cik_alerts", None)        # v2 merger-alert cooldowns: obsolete
    data.pop("first_run_done", None)
    for key, default in blank.items():
        data.setdefault(key, default)
    return data


def save_state(state):
    state["seen"] = state["seen"][-MAX_SEEN:]
    cutoff = time.time() - (REMEMBER_DAYS * 86400)
    for key in ("ipo_alerts", "priced_alerts"):
        state[key] = {k: v for k, v in state[key].items() if v > cutoff}
    STATE_FILE.write_text(json.dumps(state, indent=1))


# ============================================================================
# EDGAR
# ============================================================================

SEC_HEADERS = {"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}


def sec_get(url, timeout=30, max_bytes=None):
    """GET from the SEC. With max_bytes, stop reading after that much."""
    r = requests.get(url, headers=SEC_HEADERS, timeout=timeout,
                     stream=max_bytes is not None)
    r.raise_for_status()
    if max_bytes is None:
        text = r.text
    else:
        buf = b""
        for chunk in r.iter_content(chunk_size=65536):
            buf += chunk
            if len(buf) >= max_bytes:
                break
        r.close()
        text = buf.decode(r.encoding or "utf-8", errors="replace")
    time.sleep(0.15)  # stay well inside SEC's 10 requests/second limit
    return text


def accession_from_entry(entry_id, link):
    m = re.search(r"accession-number=(\S+)", entry_id or "")
    if m:
        return m.group(1)
    m = re.search(r"(\d{10}-\d{2}-\d{6})", link or "")
    return m.group(1) if m else (entry_id or link)


def cik_from_link(link):
    m = re.search(r"/data/(\d+)/", link or "")
    return m.group(1) if m else None


def form_from_title(title):
    m = re.match(r"\s*(\S+)\s+-\s+", title or "")
    return m.group(1).upper() if m else ""


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
            "form": form_from_title(title),
            "company": clean_title(title),
            "accession": accession_from_entry(entry_id, link),
            "cik": cik_from_link(link),
        })
    return out


def fetch_company(cik):
    """Company record from the SEC: industry code, tickers, filing history.

    Returns None if it can't be read.
    """
    try:
        data = json.loads(sec_get(SUBMISSIONS_URL.format(int(cik))))
    except Exception as exc:
        print(f"    ! could not fetch company record: {exc}")
        return None
    recent = (data.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    accs = recent.get("accessionNumber") or []
    docs = recent.get("primaryDocument") or []
    filings = [
        {"form": (f or "").upper(), "accession": a, "doc": d}
        for f, a, d in zip(forms, accs, docs)
    ]
    return {
        "name": data.get("name") or "",
        "sic": str(data.get("sic") or "").strip(),
        "tickers": data.get("tickers") or [],
        "exchanges": [x for x in (data.get("exchanges") or []) if x],
        "filings": filings,   # newest first
    }


def new_spac_verdict(company, fallback_name):
    """Decide if this filer is a brand-new SPAC.

    Returns (verdict, reason). verdict is "yes", "maybe" or "no".
    "maybe" = looks like a SPAC by name but the SEC hasn't coded it 6770 yet;
    the prospectus text gets the final say.
    """
    if company is None:
        # Couldn't read the record. Don't lose a real one: go on the name.
        if SPAC_NAME_RE.search(fallback_name or ""):
            return "maybe", "company record unavailable, SPAC-like name"
        return "no", "company record unavailable"

    name = company["name"] or fallback_name or ""
    forms = {f["form"] for f in company["filings"]}

    if forms & SEASONED_FORMS:
        return "no", "already public / merger shell"
    if not forms & IPO_FORMS:
        return "no", "no IPO registration on file"
    if company["sic"] == BLANK_CHECK_SIC:
        return "yes", "blank-check company (SIC 6770)"
    if SPAC_NAME_RE.search(name):
        return "maybe", f"SPAC-like name, SIC {company['sic'] or 'not yet assigned'}"
    return "no", f"not a blank-check company (SIC {company['sic'] or 'none'})"


def prospectus_url(cik, company, entry):
    """Best available prospectus for this SPAC (final if filed, else latest draft)."""
    if company:
        for form in PROSPECTUS_FORMS:
            for f in company["filings"]:
                if f["form"] == form and f["doc"]:
                    acc = f["accession"].replace("-", "")
                    return (f"https://www.sec.gov/Archives/edgar/data/"
                            f"{int(cik)}/{acc}/{f['doc']}")
    return None


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


def fetch_prospectus_text(url):
    if not url:
        return ""
    try:
        return strip_html(sec_get(url, timeout=60, max_bytes=DOC_BYTES))[:DOC_CHARS]
    except Exception as exc:
        print(f"    ! could not fetch prospectus: {exc}")
        return ""


# ============================================================================
# Reading the prospectus
# ============================================================================

EXTRACT_PROMPT = """You are reading the opening pages of an IPO prospectus filed \
with the SEC. Extract the facts below.

Respond with ONLY a JSON object, no markdown, no preamble:
{{"is_blank_check": true/false, "units_ticker": "ticker or null", \
"exchange": "Nasdaq / NYSE / NYSE American / other, or null", \
"size_usd_millions": number or null, "unit_price": number or null, \
"trust_per_unit": number or null, \
"unit_terms": "what one unit contains, max 10 words, or null", \
"industry": "see rules, max 6 words"}}

Rules:
- is_blank_check: true ONLY if this is a special purpose acquisition company / \
blank-check shell raising cash to buy an as-yet-unidentified business, with a \
trust account. An ordinary operating company's IPO is false.
- units_ticker: the symbol the UNITS will trade under (usually ends in U or .U).
- size_usd_millions: base offering size, excluding the over-allotment option.
- trust_per_unit: dollars per unit placed in the trust account \
(e.g. 10.00, 10.05, 10.10). Use null if not stated - do not guess.
- industry: the industry or sector the SPAC says it intends to target for its \
merger (e.g. "Fintech", "AI infrastructure", "Energy transition", "Healthcare"). \
If it says it may pursue any industry but ALSO names a sector it intends to focus \
on or that its management team specialises in, give that sector followed by \
" (not limited)". If it names no sector at all, answer exactly "Any industry". \
If the text does not cover this, use null.
- Use null for anything the text does not state.

PROSPECTUS TEXT:
{text}"""

TICKER_RE = re.compile(
    r"under\s+the\s+symbols?\s+[“\"']?\s*([A-Z]{2,6}(?:\.U|\s?U)?)\b")


def extract_details(text):
    """Ask Claude for ticker / size / trust. Never raises; fails open."""
    blank = {"is_blank_check": None, "units_ticker": None, "exchange": None,
             "size_usd_millions": None, "unit_price": None,
             "trust_per_unit": None, "unit_terms": None, "industry": None,
             "degraded": True}
    if text:
        m = TICKER_RE.search(text)
        if m:
            blank["units_ticker"] = m.group(1).replace(" ", "")
        if re.search(r"blank\s+check", text, re.I):
            blank["is_blank_check"] = True
    if not ANTHROPIC_API_KEY or not text:
        return blank

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
                "messages": [{"role": "user",
                              "content": EXTRACT_PROMPT.format(text=text)}],
            },
            timeout=60,
        )
        r.raise_for_status()
        body = "".join(
            b.get("text", "") for b in r.json().get("content", [])
            if b.get("type") == "text"
        )
        body = re.sub(r"^```(?:json)?|```$", "", body.strip(), flags=re.M).strip()
        m = re.search(r"\{.*\}", body, re.S)
        result = json.loads(m.group(0) if m else body)
        if not isinstance(result, dict):
            raise ValueError("unexpected reply")
        out = dict(blank)
        for k in blank:
            if result.get(k) not in (None, "", "null"):
                out[k] = result[k]
        out["degraded"] = False
        return out
    except Exception as exc:
        print(f"    ! could not read prospectus details ({exc}) - alerting anyway")
        return blank


# ============================================================================
# Alert wording
# ============================================================================


def _num(x):
    try:
        return float(str(x).replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return None


def next_trading_day(now=None):
    """Next US weekday after today (New York time). Ignores market holidays."""
    d = (now or datetime.now(NEW_YORK)).date() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def build_alert(name, form, info, company, now=None):
    ticker = info.get("units_ticker")
    if not ticker and company and company["tickers"]:
        ticker = company["tickers"][0]
    exchange = info.get("exchange")
    if not exchange and company and company["exchanges"]:
        exchange = company["exchanges"][0]

    priced = form.startswith("424B")
    title = ("SPAC priced: " if priced else "New SPAC: ") + name
    if ticker:
        title = f"{ticker} - {title}"   # ticker first so it shows in the banner

    lines = [f"Industry: {info.get('industry') or 'not stated - check prospectus'}"]
    money = []
    size = _num(info.get("size_usd_millions"))
    if size:
        money.append(f"${size:,.0f}m")
    unit = _num(info.get("unit_price"))
    trust = _num(info.get("trust_per_unit"))
    if trust:
        money.append(f"${trust:.2f} in trust per unit")
        if unit and abs(unit - trust) > 0.001:
            money.append(f"units sold at ${unit:.2f}")
    else:
        money.append("trust per unit not found - check prospectus")
    lines.append(" · ".join(money))

    if priced:
        when = "Priced - trading now or from the next session"
    else:
        day = next_trading_day(now)
        when = f"Likely first trade {day.strftime('%a %-d %b')} (est.)"
    lines.append(f"{exchange} · {when}" if exchange else when)

    if info.get("unit_terms"):
        lines.append(f"Unit: {info['unit_terms']}")
    return title, "\n".join(lines)


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


def run(replay=False, dry_run=False):
    state = load_state()
    seen = set(state["seen"])
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    sent = not_spac = already = failed = 0
    verdicts = {}  # cik -> (verdict, reason, company), cached for this run

    for feed_name, url in EDGAR_FEEDS.items():
        try:
            entries = parse_feed(sec_get(url))
        except Exception as exc:
            print(f"[{stamp}] {feed_name}: feed error: {exc}")
            continue
        print(f"[{stamp}] {feed_name}: {len(entries)} entries in feed")

        # First time we see a feed: note what's there, don't alert on old news.
        seeding = feed_name not in state["seeded_feeds"] and not replay
        if feed_name not in state["seeded_feeds"]:
            state["seeded_feeds"].append(feed_name)

        # Oldest first, so the 8-A is handled before a later 424B4.
        for e in reversed(entries):
            acc = e["accession"]
            is_new = acc not in seen
            if is_new:
                seen.add(acc)
                state["seen"].append(acc)
            if seeding or not (is_new or replay):
                continue
            if e["form"].endswith("/A"):
                continue  # amendments to earlier paperwork
            cik = e["cik"]
            if not cik:
                continue

            priced = e["form"].startswith("424B")
            if cik in state["priced_alerts"]:
                already += 1
                continue
            if cik in state["ipo_alerts"] and not (priced and ALSO_ALERT_ON_PRICING):
                already += 1
                continue

            if cik not in verdicts:
                company = fetch_company(cik)
                verdicts[cik] = new_spac_verdict(company, e["company"]) + (company,)
            verdict, reason, company = verdicts[cik]
            if verdict == "no":
                not_spac += 1
                continue

            name = (company["name"] if company and company["name"] else e["company"])
            print(f"  → {e['form']}: {name} [{reason}]")

            doc_url = prospectus_url(cik, company, e)
            info = extract_details(fetch_prospectus_text(doc_url))

            if verdict == "maybe" and info["is_blank_check"] is False:
                not_spac += 1
                print("    · prospectus says not a blank-check company - skipped")
                continue

            title, message = build_alert(name, e["form"], info, company)
            link = doc_url or e["link"]
            link_title = "Open prospectus" if doc_url else "Open filing on EDGAR"

            if dry_run:
                print(f"    [dry run] {title}\n      " + message.replace("\n", "\n      "))
                ok = True
            else:
                ok = push(title, message, link, link_title)
            if ok:
                sent += 1
                if not dry_run:
                    state["ipo_alerts"].setdefault(cik, time.time())
                    if priced:
                        state["priced_alerts"][cik] = time.time()
            else:
                failed += 1
                # Let the next run try this filing again.
                if is_new:
                    seen.discard(acc)
                    state["seen"].remove(acc)

        if seeding:
            print(f"[{stamp}] {feed_name}: first run on this feed - "
                  f"noted {len(entries)} existing filings, no alerts.")

    if not dry_run:
        save_state(state)
    print(f"[{stamp}] Done. {sent} sent | {already} already alerted | "
          f"{not_spac} not new SPACs | {failed} failed to send.")


def main():
    p = argparse.ArgumentParser(description="New-SPAC listing alerts via EDGAR + Pushover")
    p.add_argument("--once", action="store_true", help="run a single poll (default)")
    p.add_argument("--replay", action="store_true",
                   help="re-check everything currently in the feeds (last few "
                        "days) and alert on any new SPAC not yet alerted on")
    p.add_argument("--dry-run", action="store_true",
                   help="print alerts instead of sending them")
    p.add_argument("--test-push", action="store_true",
                   help="send a test notification and exit")
    args = p.parse_args()

    if args.test_push:
        ok = push("SPAC Alerts", "Test notification — setup is working.",
                  "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-A12B")
        print("Sent." if ok else "Failed.")
        sys.exit(0 if ok else 1)

    run(replay=args.replay, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
