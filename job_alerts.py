#!/usr/bin/env python3
"""
All-India Govt Job Bot (v3): scrapes sources -> jobs.json (for the website)
+ pushes NEW jobs to Telegram.

  pip install requests beautifulsoup4
  export TG_TOKEN="..." TG_CHAT_ID="..."      (optional, for phone alerts)

  python job_alerts.py --check   # test every URL
  python job_alerts.py --dry     # print new matches only, save nothing
  python job_alerts.py           # normal run: update jobs.json + Telegram
"""
import json, os, re, sys, time, hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from sources import SOURCES

MODE = "all"            # "all" = every govt job | "ece" = technical/ECE-type only
KEEP_DAYS = 14          # drop a job if it hasn't been seen on its page for this long
ENRICH_CAP = 80         # max detail pages opened per run (to find apply/PDF/last date)

JOB_WORDS = ["recruit", "vacanc", "notification", "advertisement", "advt", "apply",
             "online form", "bharti", "walk-in", "walk in", "opening", "invites",
             "engagement", "post of", "posts of", "examination", "exam 20", "agniveer",
             "agnipath", "career", "deputation"]
BLOCK_WORDS = ["result", "admit card", "answer key", "syllabus", "cut off", "cutoff",
               "previous paper", "login", "contact us", "tender", "scorecard",
               "hall ticket", "merit list", "tutorial", "privacy", "corrigendum"]
ECE_WORDS = ["engineer", "technical", "electronics", "ece", "communication", "b.tech",
             "btech", "be/", "scientist", "technician", "je ", "junior engineer", "gate",
             "trainee", "apprentice", "diploma", "graduate", "cgl", "chsl", "assistant",
             "officer", "all india", "telecom", "instrument"]

HERE = Path(__file__).parent
DB = HERE / "jobs.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
           "Accept-Language": "en-IN,en;q=0.9"}
TG_TOKEN, TG_CHAT_ID = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT_ID")
NOW = datetime.now(timezone.utc)


def get(url):
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.text


def uid(title, link):
    return hashlib.md5((title + link).encode()).hexdigest()[:16]


def extract(sector, name, url):
    soup = BeautifulSoup(get(url), "html.parser")
    out, links = [], set()
    for a in soup.find_all("a", href=True):
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        if len(text) < 12:
            continue
        t = text.lower()
        if not any(w in t for w in JOB_WORDS) or any(w in t for w in BLOCK_WORDS):
            continue
        ece = any(w in t for w in ECE_WORDS)
        if MODE == "ece" and not ece:
            continue
        link = urljoin(url, a["href"])
        if not link.startswith("http") or link in links:
            continue
        links.add(link)
        is_pdf = link.lower().split("?")[0].endswith(".pdf")
        out.append({
            "id": uid(text[:170], link), "sector": sector, "source": name,
            "title": text[:170], "website": url, "link": link,
            "notification": link if is_pdf else None,
            "apply": link if "apply" in t else None,
            "last_date": None, "ece": ece,
        })
    return out


DATE_RE = re.compile(r"last\s*date[^0-9A-Za-z]{0,40}(\d{1,2}[\s./-]+[A-Za-z0-9]{1,9}[\s./-]+\d{2,4})", re.I)


MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_date(s):
    """'30 Oct 2026' / '30-10-2026' / '30.10.26' -> '2026-10-30' (or None)."""
    if not s:
        return None
    m = re.match(r"(\d{1,2})(?:st|nd|rd|th)?[\s./,-]+([A-Za-z]{3,9}|\d{1,2})[\s./,-]+(\d{2,4})", s.strip())
    if not m:
        return None
    d, mo, y = m.groups()
    mo = int(mo) if mo.isdigit() else MONTHS.get(mo[:3].lower())
    y = int(y)
    y = y + 2000 if y < 100 else y
    try:
        return datetime(y, mo, int(d)).date().isoformat()
    except Exception:
        return None


QUAL_RULES = [
    ("10th Pass", r"\b(10th|matric\w*|hslc|sslc|class x)\b"),
    ("12th Pass", r"\b(12th|10\+2|intermediate|higher secondary|hsc|class xii)\b"),
    ("ITI", r"\biti\b"),
    ("Diploma", r"\bdiploma\b"),
    ("B.Tech / B.E.", r"\b(b\.?tech|b\.e\.?|engineer\w*|junior engineer|je)\b"),
    ("Graduate (Any)", r"\b(graduate|graduation|bachelor\w*|degree|cgl)\b"),
    ("Post Graduate", r"\b(post[- ]?graduate|m\.?tech|m\.?sc|mba|masters?|pg)\b"),
    ("Medical / Nursing", r"\b(mbbs|nurs\w*|medical officer|pharmac\w*|bds)\b"),
    ("Law", r"\b(llb|law officer|legal)\b"),
    ("PhD / Research", r"\b(ph\.?d|research|scientist|jrf)\b"),
]


def detect_quals(text):
    t = (text or "").lower()
    return [q for q, rx in QUAL_RULES if re.search(rx, t)]


STATE_RULES = [
    (r"uttarakhand|ukpsc|uksss|\bupcl\b|ptcul|ujvnl|\butc\b", "Uttarakhand"),
    (r"uppsc|upsssc|\bup (police|forest|metro|power)|uppcl|upsrtc|allahabad|lucknow|noida", "Uttar Pradesh"),
    (r"bihar|bpsc|bssc|patna", "Bihar"),
    (r"rajasthan|rpsc|rsmssb|jaipur", "Rajasthan"),
    (r"mppsc|mpesb|\bmp (forest|metro)|bhopal|indore|madhya pradesh", "Madhya Pradesh"),
    (r"haryana|\bhpsc\b|hssc|dhbvn|uhbvn", "Haryana"),
    (r"punjab|\bppsc\b|psssb", "Punjab"),
    (r"himachal|hppsc|hprca", "Himachal Pradesh"),
    (r"j&k|jkpsc|jkssb", "Jammu & Kashmir"),
    (r"gujarat|\bgpsc \(gujarat|gsssb", "Gujarat"),
    (r"maharashtra|\bmpsc \(maha|mumbai|mahametro|msedcl|\bbmc\b|pune|nagpur", "Maharashtra"),
    (r"delhi|dsssb|\bmcd\b|dmrc", "Delhi"),
    (r"tamil|tnpsc|tangedco|tnpdcl|chennai|cmrl", "Tamil Nadu"),
    (r"karnataka|kpsc|bescom|bengaluru|bmrcl|aranya", "Karnataka"),
    (r"kerala|kseb|kochi", "Kerala"),
    (r"andhra|appsc \(andhra", "Andhra Pradesh"),
    (r"telangana|tgpsc|hyderabad|ghmc", "Telangana"),
    (r"odisha|opsc", "Odisha"),
    (r"west bengal|wbpsc|wbssc|kolkata", "West Bengal"),
    (r"jharkhand|jpsc|jssc", "Jharkhand"),
    (r"chhattisgarh|cgpsc|cg vyapam", "Chhattisgarh"),
    (r"assam|\bapsc \(assam|slprb", "Assam"),
    (r"\bgoa\b", "Goa"),
    (r"tripura", "Tripura"), (r"manipur", "Manipur"), (r"meghalaya", "Meghalaya"),
    (r"mizoram", "Mizoram"), (r"nagaland", "Nagaland"), (r"arunachal", "Arunachal Pradesh"),
    (r"sikkim", "Sikkim"),
]


def state_of(source):
    for rx, st in STATE_RULES:
        if re.search(rx, source, re.I):
            return st
    return "All India"


def decorate(j):
    """Add filter fields used by the website."""
    j["quals"] = sorted(set(j.get("quals", [])) | set(detect_quals(j["title"])))
    j["deadline"] = parse_date(j.get("last_date"))
    j["state"] = state_of(j["source"])
    return j


def enrich(job):
    """Open the job's own page to find Apply link, notification PDF, last date."""
    if (job["link"].lower().split("?")[0].endswith(".pdf")):
        return job
    try:
        html = get(job["link"])
    except Exception:
        return job
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        txt = a.get_text(" ", strip=True).lower()
        href = urljoin(job["link"], a["href"])
        if not href.startswith("http"):
            continue
        if not job["apply"] and ("apply" in txt or "online application" in txt):
            job["apply"] = href
        if not job["notification"] and (href.lower().split("?")[0].endswith(".pdf")
                                        or "notification" in txt or "advertisement" in txt):
            job["notification"] = href
    text = soup.get_text(" ", strip=True)
    m = DATE_RE.search(text)
    if m:
        job["last_date"] = m.group(1).strip()
    low = text.lower()
    for kw in ("educational qualification", "qualification", "eligibility"):
        k = low.find(kw)
        if k != -1:
            job["quals"] = detect_quals(text[k:k + 400])
            break
    return job


def telegram(msg):
    if not (TG_TOKEN and TG_CHAT_ID):
        print("[!] Telegram not configured; skipping push.")
        return
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                  data={"chat_id": TG_CHAT_ID, "text": msg,
                        "disable_web_page_preview": True}, timeout=25)


def flat():
    return [(s, n, u) for s, d in SOURCES.items() for n, u in d.items()]


def check():
    def t(x):
        _, n, u = x
        try:
            requests.get(u, headers=HEADERS, timeout=20).raise_for_status()
            return n, u, "OK"
        except Exception as e:
            return n, u, f"FAIL {str(e)[:60]}"
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(t, flat()))
    bad = [r for r in res if r[2] != "OK"]
    print(f"\n{len(res)-len(bad)} OK / {len(bad)} failing\n")
    for n, u, s in bad:
        print(f"- {n}: {u}\n    {s}")


def scrape(x):
    s, n, u = x
    try:
        return n, extract(s, n, u), None
    except Exception as e:
        return n, [], str(e)[:80]


def main():
    if "--check" in sys.argv:
        return check()
    dry = "--dry" in sys.argv

    db = json.loads(DB.read_text()) if DB.exists() else {"jobs": {}}
    store = db["jobs"]
    first_run = not store

    with ThreadPoolExecutor(6) as ex:
        results = list(ex.map(scrape, flat()))
    failed = [n for n, _, err in results if err]

    current = {}
    for _, items, _ in results:
        for j in items:
            current[j["id"]] = j

    new_ids = [i for i in current if i not in store]
    print(f"Matched now: {len(current)} | New: {len(new_ids)} | Failed sources: {len(failed)}")

    # find apply / notification / last date for NEW jobs only
    to_enrich = [current[i] for i in new_ids][:ENRICH_CAP]
    with ThreadPoolExecutor(6) as ex:
        list(ex.map(enrich, to_enrich))

    if dry:
        for i in new_ids:
            j = current[i]
            print(f"{'*' if j['ece'] else '-'} [{j['sector']}/{j['source']}] {j['title']}\n"
                  f"   page: {j['link']}\n   apply: {j['apply']}\n   notif: {j['notification']}\n   last: {j['last_date']}")
        return

    stamp = NOW.isoformat()
    for i, j in current.items():
        if i in store:
            store[i].update({k: j[k] for k in ("apply", "notification", "last_date", "quals") if j.get(k) and not store[i].get(k)})
            store[i]["last_seen"] = stamp
        else:
            j["first_seen"] = j["last_seen"] = stamp
            store[i] = j
    cutoff = (NOW - timedelta(days=KEEP_DAYS)).isoformat()
    for i in [k for k, v in store.items() if v["last_seen"] < cutoff]:
        del store[i]

    db = {"updated": stamp, "failed_sources": failed, "jobs": store}
    DB.write_text(json.dumps(db, ensure_ascii=False))
    # website reads a plain list sorted newest first
    jobs_list = sorted((decorate(v) for v in store.values()), key=lambda v: v["first_seen"], reverse=True)
    (HERE / "jobs_public.json").write_text(json.dumps(
        {"updated": stamp, "count": len(jobs_list), "jobs": jobs_list}, ensure_ascii=False))

    # ---- Telegram push for NEW only
    if first_run and len(new_ids) > 20:
        telegram(f"Job bot live. Indexed {len(new_ids)} existing posts. Only NEW ones will be pushed from now.")
        return
    new = sorted((current[i] for i in new_ids), key=lambda j: (not j["ece"], j["sector"]))
    chunk, size = [], 0
    for j in new:
        line = (f"{'⭐' if j['ece'] else '•'} [{j['sector']}] {j['source']}\n{j['title']}\n"
                f"Page: {j['link']}\n"
                + (f"Apply: {j['apply']}\n" if j['apply'] else "")
                + (f"Notification: {j['notification']}\n" if j['notification'] else "")
                + (f"Last date: {j['last_date']}\n" if j['last_date'] else ""))
        if size + len(line) > 3500:
            telegram("NEW GOVT JOBS\n\n" + "\n".join(chunk)); chunk, size = [], 0; time.sleep(1)
        chunk.append(line); size += len(line)
    if chunk:
        telegram("NEW GOVT JOBS\n\n" + "\n".join(chunk))


if __name__ == "__main__":
    main()
