#!/usr/bin/env python3
"""Job board: the running list of live postings, plus your own tracking columns.

board.md is the file you edit. Each daily run:
  * adds newly matched postings,
  * refreshes company/role/salary details from the job sites,
  * removes postings that were taken down, unless you gave them a Status, in
    which case they move to "Closed postings you acted on",
  * never changes your Status or Notes cells, or anything in "Your own entries".

The scraper owns every column except Status and Notes. data/board_state.json
holds the posting details the board is rendered from.

Usage:
    python3 board.py mark <id-or-search> <status> [--note TEXT]   # e.g. mark 3fa9c1d2 applied
    python3 board.py list [search]                              # find a posting's ID
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BOARD_PATH = ROOT / "board.md"
STATE_PATH = ROOT / "data" / "board_state.json"

COLUMNS = ["Status", "Notes", "Company", "Role", "Location", "Salary", "Posted", "ID"]
SKIP_STATUSES = {"skip", "skipped", "not interested", "pass", "no"}
STATUS_ORDER = ["offer", "interviewing", "interview", "phone screen", "oa", "applied",
                "referred", "to apply", "rejected", "withdrawn"]

OWN_HEADING = "## Your own entries"
ID_RE = re.compile(r"^`?([0-9a-f]{8})`?$")


def job_id(key):
    return hashlib.sha1(key.encode()).hexdigest()[:8]


def is_tracking(status):
    return bool(status) and status.strip().lower() not in SKIP_STATUSES


def is_skipped(status):
    return bool(status) and status.strip().lower() in SKIP_STATUSES


# --------------------------------------------------------------------------- #
# State file
# --------------------------------------------------------------------------- #

def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"jobs": {}}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")


# --------------------------------------------------------------------------- #
# Parsing board.md (only the parts you own)
# --------------------------------------------------------------------------- #

def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator(line):
    return bool(re.fullmatch(r"\|?[\s:|-]+\|?", line.strip())) and "-" in line


def parse_board(path=BOARD_PATH):
    """Return (fields, own_lines, job_row_count).

    fields: {id: {"status": str, "notes": str}} for every scraper row.
    own_lines: table rows from "Your own entries" plus any row whose ID the
    scraper doesn't recognise, kept verbatim.
    """
    fields, own_lines, rows = {}, [], 0
    if not path.exists():
        return fields, own_lines, rows
    section = None
    for line in path.read_text().splitlines():
        if line.startswith("## "):
            section = line.strip()
            continue
        if not line.strip().startswith("|") or _is_separator(line):
            continue
        cells = _cells(line)
        if cells and cells[0].lower() == "status" and cells[-1].upper() == "ID":
            continue  # a header row
        if section == OWN_HEADING:
            own_lines.append(line)
            continue
        m = ID_RE.match(cells[-1]) if cells else None
        if not m or len(cells) < len(COLUMNS):
            own_lines.append(line)  # not one of ours; keep it rather than drop it
            continue
        # A "|" typed inside Notes splits it into extra cells; join them back.
        extra = len(cells) - len(COLUMNS)
        fields[m.group(1)] = {"status": cells[0], "notes": " / ".join(cells[1:2 + extra])}
        rows += 1
    return fields, own_lines, rows


# --------------------------------------------------------------------------- #
# Merging a scrape into the board
# --------------------------------------------------------------------------- #

def update(state, matches, fetched_keys, failed_companies, today):
    """Merge today's scrape into state, honouring your edits in board.md.

    matches: jobs that pass every filter today.
    fetched_keys: keys of every posting still up on a job site (filters or not).
    failed_companies: names whose boards failed to load; their postings are
    left alone rather than treated as taken down.
    Returns (added_ids, closed_ids, removed_count, board_written).
    If board.md exists but has none of its job rows (e.g. it was accidentally
    mangled), it is left untouched so none of your edits are lost.
    """
    fields, own_lines, rows = parse_board()
    jobs = state["jobs"]
    # Only trust a missing row as "you deleted it" when the board parsed normally.
    board_ok = BOARD_PATH.exists() and (rows > 0 or not any(not j.get("hidden") for j in jobs.values()))
    if BOARD_PATH.exists() and not board_ok:
        # Without the board we can't tell which postings you've marked, so change nothing.
        return [], [], 0, False

    if board_ok:
        for jid, j in jobs.items():
            if jid not in fields and not j.get("hidden"):
                j["hidden"] = True  # you deleted this row; don't bring it back

    added = []
    for m in matches:
        jid = job_id(m["key"])
        prev = jobs.get(jid)
        if prev is None:
            added.append(jid)
        jobs[jid] = {
            "key": m["key"], "company": m["company"], "title": m["title"], "url": m["url"],
            "locations": m["matched_locations"][:3], "salary": list(m["salary"]),
            "posted": (m["posted"] or "")[:10], "skills": m["skills"],
            "added": prev["added"] if prev else today, "last_seen": today,
            "closed": None, "hidden": prev.get("hidden", False) if prev else False,
        }

    match_ids = {job_id(m["key"]) for m in matches}
    closed, removed = [], 0
    for jid in list(jobs):
        j = jobs[jid]
        if jid in match_ids or j.get("closed"):
            if j.get("closed") and board_ok and jid not in fields:
                del jobs[jid]  # you deleted a closed row
            continue
        if j["key"] in fetched_keys:
            j["last_seen"] = today  # still up, even if it no longer matches every filter
            continue
        if j["company"] in failed_companies:
            continue  # couldn't check today
        status = fields.get(jid, {}).get("status", "")
        if is_tracking(status):
            j["closed"] = today
            j["hidden"] = False
            closed.append(jid)
        else:
            del jobs[jid]
            removed += 1

    write_board(state, fields, own_lines, today)
    return added, closed, removed, True


# --------------------------------------------------------------------------- #
# Rendering board.md
# --------------------------------------------------------------------------- #

def _clean(s):
    return (s or "").replace("|", "/").replace("\n", " ").strip()


def _money(n):
    return f"${n / 1000:.0f}k"


def _row(jid, j, f, posted_label=None):
    lo, hi = j["salary"]
    cells = [
        _clean(f.get("status")), _clean(f.get("notes")), _clean(j["company"]),
        f"[{_clean(j['title'])}]({j['url']})", _clean("; ".join(j["locations"][:2])),
        f"{_money(lo)}–{_money(hi)}", posted_label or j.get("posted") or "?", f"`{jid}`",
    ]
    return "| " + " | ".join(cells) + " |"


def _table(rows):
    if not rows:
        return ["_None._"]
    return ["| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)] + rows


def _status_rank(status):
    s = status.strip().lower()
    return STATUS_ORDER.index(s) if s in STATUS_ORDER else len(STATUS_ORDER)


def write_board(state, fields, own_lines, today):
    jobs = state["jobs"]
    tracking, open_, skipped, closed = [], [], [], []
    for jid, j in jobs.items():
        f = fields.get(jid, {})
        status = f.get("status", "")
        if j.get("closed"):
            closed.append((jid, j, f))
        elif j.get("hidden"):
            continue
        elif is_skipped(status):
            skipped.append((jid, j, f))
        elif is_tracking(status):
            tracking.append((jid, j, f))
        else:
            open_.append((jid, j, f))

    tracking.sort(key=lambda t: (_status_rank(t[2]["status"]), t[1]["company"]))
    open_.sort(key=lambda t: (t[1].get("added", ""), len(t[1].get("skills", [])), t[1]["salary"][1]),
               reverse=True)
    skipped.sort(key=lambda t: t[1]["company"])
    closed.sort(key=lambda t: t[1]["closed"], reverse=True)

    out = [
        "# Job board",
        "",
        f"_Updated {today}. {len(open_)} open postings, {len(tracking)} you're tracking._",
        "",
        "**How to update:** edit this file on GitHub (pencil icon) and fill in the **Status** and "
        "**Notes** cells of a row. Use a status like `applied`, `interviewing`, `offer`, `rejected`, "
        "or `skip` to hide a posting. Rows move to the right section on the next run. "
        "Only Status and Notes are yours; the scraper rewrites the other columns and keeps the `ID`, "
        "so don't edit those. Deleting a row hides that posting for good. Add jobs you found elsewhere "
        "under **Your own entries**; the scraper never touches that section.",
        "",
        "Or from a terminal: `python3 board.py mark <ID or search words> applied --note \"referral from X\"`.",
        "",
        "Postings that get taken down are removed automatically, unless you've given them a status; "
        "those move to **Closed postings you acted on**.",
        "",
        f"## Tracking ({len(tracking)})",
        "",
        *_table([_row(*t) for t in tracking]),
        "",
        f"## Open postings ({len(open_)})",
        "",
        "_Newest first._",
        "",
        *_table([_row(*t) for t in open_]),
        "",
        f"## Skipped ({len(skipped)})",
        "",
        *_table([_row(*t) for t in skipped]),
        "",
        f"## Closed postings you acted on ({len(closed)})",
        "",
        *_table([_row(jid, j, f, posted_label=f"closed {j['closed']}") for jid, j, f in closed]),
        "",
        OWN_HEADING,
        "",
        "| Status | Notes | Company | Role | Location | Salary | Posted | ID |",
        "|---|---|---|---|---|---|---|---|",
        *own_lines,
        "",
    ]
    BOARD_PATH.write_text("\n".join(out))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _find(state, query):
    q = query.lower().strip("`")
    if q in state["jobs"]:
        return [q]
    words = q.split()
    return [jid for jid, j in state["jobs"].items()
            if all(w in f"{j['company']} {j['title']}".lower() for w in words)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    mk = sub.add_parser("mark", help="set a posting's Status (and optionally Notes)")
    mk.add_argument("query", help="the posting's ID, or words from its company and title")
    mk.add_argument("status", help='e.g. applied, interviewing, offer, rejected, skip; "" to clear')
    mk.add_argument("--note", help="replace the Notes cell")
    ls = sub.add_parser("list", help="list postings with their IDs")
    ls.add_argument("query", nargs="?", default="")
    args = ap.parse_args()

    state = load_state()
    fields, own_lines, rows = parse_board()
    if args.cmd == "mark" and BOARD_PATH.exists() and rows == 0 and state["jobs"]:
        print("board.md has no job rows it can read, so it was left alone. Restore it from git history first.")
        return 1
    hits = _find(state, args.query) if args.query else list(state["jobs"])

    if args.cmd == "list":
        for jid in hits:
            j = state["jobs"][jid]
            status = fields.get(jid, {}).get("status", "")
            print(f"{jid}  {status or '-':<12} {j['company']} — {j['title']}")
        return 0

    if len(hits) != 1:
        print(f"{len(hits)} postings match {args.query!r}; use the ID instead:" if hits
              else f"No posting matches {args.query!r}.")
        for jid in hits[:20]:
            j = state["jobs"][jid]
            print(f"  {jid}  {j['company']} — {j['title']}")
        return 1
    jid = hits[0]
    f = fields.setdefault(jid, {"status": "", "notes": ""})
    f["status"] = args.status
    if args.note is not None:
        f["notes"] = args.note
    state["jobs"][jid]["hidden"] = False
    save_state(state)
    today = next((l.split("Updated ")[1][:10] for l in BOARD_PATH.read_text().splitlines()
                  if l.startswith("_Updated ")), "") if BOARD_PATH.exists() else ""
    write_board(state, fields, own_lines, today)
    j = state["jobs"][jid]
    print(f"Marked {jid} ({j['company']} — {j['title']}) as {args.status!r}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
