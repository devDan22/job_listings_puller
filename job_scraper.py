#!/usr/bin/env python3
"""Daily SWE job scraper.

Pulls postings from the public Greenhouse, Lever and Ashby job-board APIs for
every company in companies.json, filters them against config.json (title,
level, CA/NY location, salary floor, skills), and reports only postings that
have not been reported before (tracked in data/seen_jobs.json).

Usage:
    python3 job_scraper.py            # fetch, write reports/<date>.md, update seen state
    python3 job_scraper.py --dry-run  # fetch and print, don't touch seen state or reports
"""

import argparse
import concurrent.futures as cf
import datetime as dt
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
COMPANIES_PATH = ROOT / "companies.json"
SEEN_PATH = ROOT / "data" / "seen_jobs.json"
REPORTS_DIR = ROOT / "reports"

USER_AGENT = "job-listings-puller/1.0 (personal job search)"
NOW = dt.datetime.now(dt.timezone.utc)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def fetch_json(url, retries=3):
    return json.loads(fetch_text(url, retries))


def fetch_text(url, retries=3):
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            last_err = e
        except Exception as e:  # network errors, timeouts, bad JSON
            last_err = e
        time.sleep(2 ** (attempt + 1))
    raise last_err


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def html_to_text(s):
    if not s:
        return ""
    # Greenhouse content is HTML-escaped HTML, so unescape before and after stripping tags.
    s = html.unescape(html.unescape(s))
    return WS_RE.sub(" ", TAG_RE.sub(" ", s)).strip()


def parse_date(value):
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):  # Lever uses epoch millis
            return dt.datetime.fromtimestamp(value / 1000, tz=dt.timezone.utc)
        d = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


_AMT = r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{5,7}(?:\.\d+)?|\d+(?:\.\d+)?\s?[kK])"
SALARY_RANGE_RE = re.compile(
    r"\$\s?" + _AMT + r"\s*(?:USD)?\s*(?:-|–|—|to)\s*(?:USD\s*)?\$?\s?" + _AMT
)


def _to_number(tok):
    tok = tok.replace(",", "").replace(" ", "")
    if tok[-1] in "kK":
        return float(tok[:-1]) * 1000
    return float(tok)


def salaries_from_text(text):
    """Return (min, max) of annual USD salary ranges mentioned in text, or None."""
    lows, highs = [], []
    for m in SALARY_RANGE_RE.finditer(text):
        lo, hi = _to_number(m.group(1)), _to_number(m.group(2))
        if 30_000 <= lo <= hi <= 2_000_000:  # skips hourly rates and funding amounts
            lows.append(lo)
            highs.append(hi)
    if not highs:
        return None
    return min(lows), max(highs)


YEARS_RE = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:(?:-|–|to)\s*\d{1,2}\s*\+?\s*)?years?\s+(?:of\s+)?"
    r"(?:[a-z-]+\s+){0,3}(?:experience|exp\b)",
    re.I,
)


def required_years(text):
    nums = [int(m.group(1)) for m in YEARS_RE.finditer(text)]
    nums = [n for n in nums if n <= 20]
    return max(nums) if nums else None


# --------------------------------------------------------------------------- #
# Board adapters -> normalized job dicts
# --------------------------------------------------------------------------- #

def job(company, ats, jid, title, locations, url, posted, text, salary):
    return {
        "key": f"{ats}:{company['slug']}:{jid}",
        "company": company["name"],
        "title": (title or "").strip(),
        "locations": [l for l in locations if l],
        "url": url,
        "posted": posted.isoformat() if posted else None,
        "text": text,
        "salary": salary,
    }


def fetch_greenhouse(company, matcher):
    data = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{company['slug']}/jobs?content=true")
    out = []
    for j in data.get("jobs", []):
        text = html_to_text(j.get("content"))
        locs = [(j.get("location") or {}).get("name")] + [o.get("name") for o in j.get("offices") or []]
        out.append(job(company, "greenhouse", j["id"], j.get("title"), locs, j.get("absolute_url"),
                       parse_date(j.get("first_published") or j.get("updated_at")), text,
                       salaries_from_text(text)))
    return out


def fetch_lever(company, matcher):
    data = fetch_json(f"https://api.lever.co/v0/postings/{company['slug']}?mode=json")
    out = []
    for j in data:
        cats = j.get("categories") or {}
        locs = [cats.get("location")] + list(cats.get("allLocations") or [])
        parts = [j.get("descriptionPlain") or "", j.get("additionalPlain") or ""]
        for lst in j.get("lists") or []:
            parts.append(lst.get("text", ""))
            parts.append(html_to_text(lst.get("content")))
        text = WS_RE.sub(" ", " ".join(parts))
        salary = None
        sr = j.get("salaryRange") or {}
        if sr.get("max") and sr.get("currency", "USD") == "USD" and "year" in (sr.get("interval") or "year"):
            salary = (float(sr.get("min") or sr["max"]), float(sr["max"]))
        salary = salary or salaries_from_text(text)
        out.append(job(company, "lever", j["id"], j.get("text"), locs, j.get("hostedUrl"),
                       parse_date(j.get("createdAt")), text, salary))
    return out


def fetch_ashby(company, matcher):
    data = fetch_json(
        f"https://api.ashbyhq.com/posting-api/job-board/{company['slug']}?includeCompensation=true")
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        addr = ((j.get("address") or {}).get("postalAddress") or {})
        locs = [j.get("location"), ", ".join(filter(None, [addr.get("addressLocality"), addr.get("addressRegion")]))]
        locs += [s.get("location") for s in j.get("secondaryLocations") or []]
        text = j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml"))
        salary = None
        comp = j.get("compensation") or {}
        for c in comp.get("summaryComponents") or []:
            if (c.get("compensationType") == "Salary" and c.get("currencyCode") == "USD"
                    and c.get("interval") == "1 YEAR" and c.get("maxValue")):
                salary = (float(c.get("minValue") or c["maxValue"]), float(c["maxValue"]))
        if not salary:
            salary = salaries_from_text(" ".join(filter(None, [comp.get("compensationTierSummary"), text])))
        out.append(job(company, "ashby", j["id"], j.get("title"), locs, j.get("jobUrl"),
                       parse_date(j.get("publishedAt")), text, salary))
    return out


# Big-tech career sites. These search APIs are paginated, and Amazon/Microsoft
# only expose pay ranges on each job's detail page, so details are fetched only
# for postings that already pass the title/location prefilter.

def _too_old(posted, matcher):
    return posted and (NOW - posted).days > matcher.cfg["max_posting_age_days"]


AMAZON_PAY_RE = re.compile(r"([^<>\n]{0,80}?)\b(\d{1,3}(?:,\d{3})+)(?:\.\d+)? - (\d{1,3}(?:,\d{3})+)(?:\.\d+)? USD annually")


def fetch_amazon(company, matcher):
    base = ("https://www.amazon.jobs/en/search.json?base_query=software%20development%20engineer"
            "&country=USA&normalized_state_name%5B%5D=California&normalized_state_name%5B%5D=New%20York"
            "&result_limit=100&sort=recent")
    candidates = []
    for offset in range(0, 1500, 100):
        page = fetch_json(f"{base}&offset={offset}").get("jobs") or []
        for j in page:
            posted = None
            try:
                posted = dt.datetime.strptime(WS_RE.sub(" ", j["posted_date"]), "%B %d, %Y").replace(
                    tzinfo=dt.timezone.utc)
            except (KeyError, ValueError):
                pass
            locs = [j.get("location")]
            for raw in j.get("locations") or []:
                try:
                    locs.append(json.loads(raw).get("location"))
                except (TypeError, ValueError):
                    pass
            if _too_old(posted, matcher) or not matcher.prefilter(j.get("title", ""), locs):
                continue
            text = html_to_text(" ".join(j.get(k) or "" for k in
                                         ("description", "basic_qualifications", "preferred_qualifications")))
            candidates.append((j, posted, locs, text))
        if len(page) < 100:
            break

    def detail(c):
        j, posted, locs, text = c
        url = "https://www.amazon.jobs" + j["job_path"]
        salary = None
        try:
            ranges = [(where, float(lo.replace(",", "")), float(hi.replace(",", "")))
                      for where, lo, hi in AMAZON_PAY_RE.findall(fetch_text(url))]
            # Prefer the ranges listed for CA/NY locations when several markets are listed.
            local = [r for r in ranges if matcher.location_matches(r[0])] or ranges
            if local:
                salary = (min(r[1] for r in local), max(r[2] for r in local))
        except Exception:
            pass
        return job(company, "amazon", j["id_icims"], j.get("title"), locs, url, posted, text, salary)

    with cf.ThreadPoolExecutor(6) as ex:
        return list(ex.map(detail, candidates))


GOOGLE_DATA_RE = re.compile(r"AF_initDataCallback\(\{key: 'ds:1'.*?data:(.*?), sideChannel: \{\}\}\);</script>", re.S)


def fetch_google(company, matcher):
    out, seen_ids = [], set()
    for loc in ("California, USA", "New York, NY, USA"):
        for page in range(1, 60):
            url = ("https://www.google.com/about/careers/applications/jobs/results/"
                   f"?q=software%20engineer&location={urllib.parse.quote(loc)}&sort_by=date&page={page}")
            m = GOOGLE_DATA_RE.search(fetch_text(url))
            rows = (json.loads(m.group(1))[0] or []) if m else []
            any_recent = False
            for j in rows:
                posted = parse_date(j[12][0] * 1000) if j[12] else None
                if not _too_old(posted, matcher):
                    any_recent = True
                if j[0] in seen_ids:
                    continue
                seen_ids.add(j[0])
                locs = [l[0] for l in j[9] or []]
                text = html_to_text(" ".join((x[1] or "") for x in (j[3], j[4], j[10], j[19]) if x))
                out.append(job(company, "google", j[0], j[1], locs,
                               f"https://www.google.com/about/careers/applications/jobs/results/{j[0]}",
                               posted, text, salaries_from_text(text)))
            if len(rows) < 20 or not any_recent:  # results are newest-first
                break
    return out


def fetch_microsoft(company, matcher):
    api = "https://apply.careers.microsoft.com/api/pcsx"
    positions = {}
    for loc in ("California", "New York"):
        start = 0
        while start < 500:
            data = fetch_json(f"{api}/search?domain=microsoft.com&query=software%20engineer"
                              f"&location={urllib.parse.quote(loc)}&start={start}")["data"]
            page = data.get("positions") or []
            for p in page:
                positions[p["id"]] = p
            start += len(page)
            if not page or start >= data.get("count", 0):
                break

    candidates = []
    for p in positions.values():
        posted = parse_date(p["postedTs"] * 1000) if p.get("postedTs") else None
        locs = p.get("standardizedLocations") or []
        if not _too_old(posted, matcher) and matcher.prefilter(p.get("name", ""), locs):
            candidates.append((p, posted, locs))

    def detail(c):
        p, posted, locs = c
        d = fetch_json(f"{api}/position_details?position_id={p['id']}&domain=microsoft.com&hl=en")["data"]
        text = html_to_text(d.get("jobDescription"))
        url = d.get("publicUrl") or f"https://apply.careers.microsoft.com/careers/job/{p['id']}"
        return job(company, "microsoft", p["id"], p.get("name"), locs, url, posted, text,
                   salaries_from_text(text))

    with cf.ThreadPoolExecutor(6) as ex:
        return list(ex.map(detail, candidates))


ADAPTERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
            "amazon": fetch_amazon, "google": fetch_google, "microsoft": fetch_microsoft}


def fetch_all(companies, matcher):
    jobs, failures = [], []

    def run(c):
        return c, ADAPTERS[c["ats"]](c, matcher)

    # Ashby rate-limits bursts, so give it a small pool of its own.
    ashby = [c for c in companies if c["ats"] == "ashby"]
    others = [c for c in companies if c["ats"] != "ashby"]
    with cf.ThreadPoolExecutor(16) as ex_other, cf.ThreadPoolExecutor(3) as ex_ashby:
        futures = {ex_other.submit(run, c): c for c in others}
        futures.update({ex_ashby.submit(run, c): c for c in ashby})
        for fut in cf.as_completed(futures):
            c = futures[fut]
            try:
                _, js = fut.result()
                jobs.extend(js)
            except Exception as e:
                failures.append(f"{c['name']} ({c['ats']}/{c['slug']}): {e}")
    return jobs, failures


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #

class Matcher:
    def __init__(self, cfg):
        comp = lambda pats: [re.compile(p, re.I) for p in pats]
        self.cfg = cfg
        self.title_inc = comp(cfg["title_include"])
        self.title_exc = comp(cfg["title_exclude"])
        # Location patterns are case-sensitive so ", CA" doesn't match "Ca" etc.
        self.loc_inc = [re.compile(p) for p in cfg["location_patterns"]]
        self.loc_exc = [re.compile(p) for p in cfg["location_exclude_patterns"]]
        self.skills = {name: re.compile(p, re.I) for name, p in cfg["skill_keywords"].items()}

    def title_matches(self, title):
        return (any(p.search(title) for p in self.title_inc)
                and not any(p.search(title) for p in self.title_exc))

    def location_matches(self, loc):
        return (bool(loc) and any(p.search(loc) for p in self.loc_inc)
                and not any(p.search(loc) for p in self.loc_exc))

    def prefilter(self, title, locations):
        """Cheap title + location check, used before fetching per-job detail pages."""
        return self.title_matches(title) and any(self.location_matches(l) for l in locations)

    def evaluate(self, j):
        """Return None if the job is rejected, else the job enriched with match info."""
        title = j["title"]
        if not self.title_matches(title):
            return None

        matched_locs = [l for l in j["locations"] if self.location_matches(l)]
        if not matched_locs:
            return None

        if not j["salary"] or j["salary"][1] < self.cfg["min_salary_usd"]:
            return None

        if j["posted"]:
            age = (NOW - dt.datetime.fromisoformat(j["posted"])).days
            if age > self.cfg["max_posting_age_days"]:
                return None

        yrs = required_years(j["text"])
        if yrs is not None and yrs > self.cfg["max_years_experience_required"]:
            return None

        haystack = f"{title} {j['text']}"
        skills = [name for name, p in self.skills.items() if p.search(haystack)]
        if len(skills) < self.cfg["min_skill_matches"]:
            return None

        return {**j, "matched_locations": sorted(set(matched_locs)), "skills": skills,
                "years_required": yrs}


# --------------------------------------------------------------------------- #
# State + reporting
# --------------------------------------------------------------------------- #

def load_seen():
    if SEEN_PATH.exists():
        return json.loads(SEEN_PATH.read_text())
    return {}


def save_seen(seen, cfg):
    # A posting older than max_posting_age_days can never match again, so old
    # entries are safe to drop.
    cutoff = (NOW - dt.timedelta(days=cfg["max_posting_age_days"] + 30)).date().isoformat()
    seen = {k: v for k, v in seen.items() if v["first_seen"] >= cutoff}
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(seen, indent=1, sort_keys=True) + "\n")


def pacific_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/Los_Angeles")
    except Exception:  # no tzdata installed
        return dt.timezone(dt.timedelta(hours=-8))


def fmt_money(n):
    return f"${n / 1000:.0f}k"


def render_report(new_jobs, stats, failures, today):
    lines = [f"# New SWE jobs — {today}", ""]
    lines.append(
        f"**{len(new_jobs)} new** matching postings "
        f"(scanned {stats['jobs']:,} postings across {stats['companies']} companies; "
        f"{stats['matches']} total current matches).")
    lines.append("")
    lines.append("Filters: mid-level SWE titles · California or New York · salary range tops out "
                 f"at ≥ {fmt_money(stats['min_salary'])} · posted within {stats['max_age']} days · "
                 "ranked by overlap with C++, TypeScript/React/Node + backend/infra skills.")
    lines.append("")
    if not new_jobs:
        lines.append("_No new matching postings today._")
    else:
        lines.append("| # | Company | Role | Location | Salary | Posted | Skill match |")
        lines.append("|---|---|---|---|---|---|---|")
        for i, j in enumerate(new_jobs, 1):
            lo, hi = j["salary"]
            locs = "; ".join(j["matched_locations"][:3])
            posted = j["posted"][:10] if j["posted"] else "?"
            skills = ", ".join(j["skills"][:6])
            title = j["title"].replace("|", "/")
            lines.append(f"| {i} | {j['company']} | [{title}]({j['url']}) | {locs} | "
                         f"{fmt_money(lo)}–{fmt_money(hi)} | {posted} | {skills} |")
    if failures:
        lines += ["", f"<details><summary>{len(failures)} boards failed to load</summary>", ""]
        lines += [f"- {f}" for f in sorted(failures)]
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="don't update seen state or write a report")
    args = ap.parse_args()

    cfg = json.loads(CONFIG_PATH.read_text())
    companies = json.loads(COMPANIES_PATH.read_text())
    matcher = Matcher(cfg)

    jobs, failures = fetch_all(companies, matcher)
    matches, seen_keys = [], set()
    for j in jobs:
        if j["key"] in seen_keys:
            continue
        seen_keys.add(j["key"])
        m = matcher.evaluate(j)
        if m:
            matches.append(m)

    seen = load_seen()
    new_all = [m for m in matches if m["key"] not in seen]
    # Some companies post the same role several times under different IDs; list it once.
    new_jobs, shown = [], set()
    for m in new_all:
        ident = (m["company"], m["title"].lower(), tuple(m["matched_locations"]))
        if ident not in shown:
            shown.add(ident)
            new_jobs.append(m)
    new_jobs.sort(key=lambda m: (-len(m["skills"]), -m["salary"][1], m["company"]))

    today = NOW.astimezone(pacific_tz()).date().isoformat()
    stats = {"jobs": len(jobs), "companies": len(companies) - len(failures), "matches": len(matches),
             "min_salary": cfg["min_salary_usd"], "max_age": cfg["max_posting_age_days"]}
    report = render_report(new_jobs, stats, failures, today)
    print(report)

    if args.dry_run:
        return 0

    for m in new_all:
        seen[m["key"]] = {"first_seen": today, "company": m["company"], "title": m["title"]}
    save_seen(seen, cfg)
    REPORTS_DIR.mkdir(exist_ok=True)
    (REPORTS_DIR / f"{today}.md").write_text(report)
    (REPORTS_DIR / "latest.md").write_text(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
