"""One-off diagnostic: dump the live feeds and a dry-run replay. Sends nothing."""
import io, json, contextlib, os
import spac_alerts as sa

os.makedirs("debug_out", exist_ok=True)
feeds = {}
for name, url in sa.EDGAR_FEEDS.items():
    try:
        raw = sa.sec_get(url)
        entries = sa.parse_feed(raw)
        feeds[name] = [{k: e[k] for k in ("form", "company", "cik", "accession", "link")} for e in entries]
        open(f"debug_out/feed_{name}.xml", "w").write(raw[:6000])
    except Exception as exc:
        feeds[name] = f"ERROR {exc!r}"
json.dump(feeds, open("debug_out/feeds.json", "w"), indent=1)

orig_extract, orig_text, orig_company = sa.extract_details, sa.fetch_prospectus_text, sa.fetch_company
def text_spy(url):
    t = orig_text(url)
    print(f"    [debug] prospectus {url} -> {len(t)} chars")
    return t
def extract_spy(text):
    r = orig_extract(text)
    print(f"    [debug] details: {json.dumps(r)}")
    return r
def company_spy(cik):
    c = orig_company(cik)
    if c:
        print(f"    [debug] cik {cik}: {c['name']} | sic {c['sic']} | tickers {c['tickers']} | forms {[f['form'] for f in c['filings']][:12]}")
    return c
sa.extract_details, sa.fetch_prospectus_text, sa.fetch_company = extract_spy, text_spy, company_spy
sa.STATE_FILE = sa.Path("does_not_exist.json")   # blank state, so nothing is skipped
print("api key present:", bool(sa.ANTHROPIC_API_KEY))
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    try:
        sa.run(replay=True, dry_run=True)
    except Exception as exc:
        print("CRASH", repr(exc))
open("debug_out/replay.txt", "w").write(buf.getvalue())
print(buf.getvalue()[-3000:])
