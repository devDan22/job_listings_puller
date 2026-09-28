# job_listings_puller

Daily scraper for software engineering roles that fit my search:

- **Level:** mid-level SWE (Senior/Staff/Principal/Lead/Manager, intern/new grad, and roles requiring more than 6 years are filtered out)
- **Location:** California or New York only (no remote-only roles)
- **Salary:** posted range must reach **$200k+** (CA/NY pay-transparency laws mean nearly every posting lists one)
- **Stack:** ranked by overlap with full-stack TypeScript/React/Node and backend/infra skills (Python, Go, APIs, Postgres, AWS/GCP, Kubernetes, distributed systems)
- **Only new postings:** anything already reported is kept in `data/seen_jobs.json` and never reported again

It uses the public job-board APIs of **Greenhouse, Lever and Ashby**, covering 240 companies (see `companies.json`). It needs only the Python 3.11+ standard library.

## Run

```bash
python3 job_scraper.py            # writes reports/<date>.md + reports/latest.md, updates data/seen_jobs.json
python3 job_scraper.py --dry-run  # just print what would be reported
```

## Customize

- `config.json`: salary floor, max posting age, max years of experience, title include/exclude regexes, location regexes, and skill keywords used for ranking.
- `companies.json`: add a company with `{"name": "...", "ats": "greenhouse|lever|ashby", "slug": "..."}`. The slug is the company's board name in its job-board URL, e.g. `boards.greenhouse.io/<slug>`, `jobs.lever.co/<slug>`, `jobs.ashbyhq.com/<slug>`.

## Schedule

A Claude Code Routine runs this every morning at ~7am Pacific, commits the updated seen-state and report, and sends the new postings as a notification.
