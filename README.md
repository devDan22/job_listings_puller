# job_listings_puller

Daily scraper for software engineering roles that fit my search:

- **Level:** mid-level SWE (Senior/Staff/Principal/Lead/Manager, intern/new grad, and roles requiring more than 6 years are filtered out)
- **Location:** California or New York only (no remote-only roles)
- **Salary:** posted range must reach **$200k+** (CA/NY pay-transparency laws mean nearly every posting lists one)
- **Stack:** ranked by overlap with C++, full-stack TypeScript/React/Node and backend/infra skills (Python, Go, APIs, Postgres, AWS/GCP, Kubernetes, distributed systems)
- **Only new postings:** anything already reported is kept in `data/seen_jobs.json` and never reported again

It uses the public job-board APIs of **Greenhouse, Lever and Ashby** (240 companies, including Anduril and Palantir) plus the career-site search APIs of **Amazon, Google and Microsoft** (see `companies.json`). For Amazon and Microsoft, pay ranges are only on each job's page, so those are fetched just for postings that already pass the title/location filters. It needs only the Python 3.11+ standard library.

## Files

| File | What it is |
|---|---|
| [`board.md`](board.md) | **Your job board.** Every live posting that matches, plus your Status and Notes. |
| [`new_postings.md`](new_postings.md) | Postings found for the first time on the latest run. |
| `reports/<date>.md` | History of each day's new postings. |
| `data/` | Scraper state (what's been reported, posting details). Don't edit by hand. |

## Tracking applications in board.md

Edit `board.md` directly on GitHub (pencil icon) and fill in a row's **Status** and **Notes** cells:

- `applied`, `interviewing`, `offer`, `rejected`, or anything else: the row moves to **Tracking**.
- `skip`: the row moves to **Skipped**.
- Delete a row to hide that posting permanently.
- Jobs found elsewhere (referrals etc.) go under **Your own entries**. The scraper never touches that section.

Or from a terminal (or ask Claude to run it):

```bash
python3 board.py list vercel                                   # find a posting's ID
python3 board.py mark e88c9dc2 applied --note "referral from Sam"
python3 board.py mark "vercel agentic" interviewing            # search words work too
```

**What the daily run changes:** it adds new matches, refreshes company, role, salary and location from the job sites, and prunes postings that were taken down. A taken-down posting you gave a Status isn't deleted; it moves to **Closed postings you acted on**. The run never changes Status, Notes or **Your own entries**. It also leaves alone postings from a company whose job site failed to load that day. If `board.md` is ever unreadable (no job rows), the run leaves it and the saved state untouched and warns you in `new_postings.md`.

## Run

```bash
python3 job_scraper.py            # updates board.md, new_postings.md, reports/<date>.md, data/
python3 job_scraper.py --dry-run  # just print today's new postings
```

## Customize

- `config.json`: salary floor, the "new posting" age window (`max_posting_age_days`), max years of experience, title include/exclude regexes, location regexes, and skill keywords used for ranking.
- `companies.json`: add a company with `{"name": "...", "ats": "greenhouse|lever|ashby", "slug": "..."}`. The slug is the company's board name in its job-board URL, e.g. `boards.greenhouse.io/<slug>`, `jobs.lever.co/<slug>`, `jobs.ashbyhq.com/<slug>`.

## Schedule

A Claude Code Routine runs this every morning at ~7am Pacific. It commits the updated board and state, and sends the new postings as a notification.
