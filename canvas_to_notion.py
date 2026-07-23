#!/usr/bin/env python3
"""
canvas_to_notion.py
Sync upcoming Canvas assignments into Notion, linked to a Courses database.

For each active Canvas course it upserts a page in the Courses database, then
for each upcoming assignment it upserts a row in the Canvas Assignments
database and links it (via the "Course" relation) to its course page. Both
upserts are idempotent, matched on Canvas IDs, so re-runs never duplicate.

Required environment variables:
  CANVAS_BASE_URL             e.g. https://canvas.yourschool.edu  (no trailing slash)
  CANVAS_TOKEN                Canvas API access token
  NOTION_TOKEN                Notion internal integration secret
  NOTION_DATABASE_ID          Canvas Assignments database ID
  NOTION_COURSES_DATABASE_ID  Courses database ID

Optional:
  DAYS_AHEAD                  How many days of upcoming items to sync (default 60)
"""

import os
import sys
import time
import requests
from datetime import datetime, timezone, timedelta

# ---------- config ----------
CANVAS_BASE_URL = os.environ.get("CANVAS_BASE_URL", "").rstrip("/")
CANVAS_TOKEN = os.environ.get("CANVAS_TOKEN", "")
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")
NOTION_COURSES_DATABASE_ID = os.environ.get("NOTION_COURSES_DATABASE_ID", "")
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", "60"))
DAYS_BEHIND = int(os.environ.get("DAYS_BEHIND", "21"))

NOTION_VERSION = "2026-03-11"
NOTION_API = "https://api.notion.com/v1"

# Canvas Assignments column names (case-sensitive, must match the database)
PROP_TITLE = "Name"
PROP_COURSE = "Course"          # relation -> Courses database
PROP_DUE = "Due"
PROP_TYPE = "Type"
PROP_URL = "Canvas URL"
PROP_CANVAS_ID = "Canvas ID"
PROP_DONE = "Complete"

# Courses column names
COURSE_TITLE = "Name"
COURSE_CANVAS_ID = "Canvas Course ID"

TYPE_LABELS = {
    "assignment": "Assignment",
    "quiz": "Quiz",
    "discussion_topic": "Discussion",
    "wiki_page": "Page",
    "sub_assignment": "Assignment",
    "missing": "Missing",
}


def require_env():
    missing = [k for k, v in {
        "CANVAS_BASE_URL": CANVAS_BASE_URL,
        "CANVAS_TOKEN": CANVAS_TOKEN,
        "NOTION_TOKEN": NOTION_TOKEN,
        "NOTION_DATABASE_ID": NOTION_DATABASE_ID,
        "NOTION_COURSES_DATABASE_ID": NOTION_COURSES_DATABASE_ID,
    }.items() if not v]
    if missing:
        sys.exit(f"Missing required env vars: {', '.join(missing)}")


# ---------- Canvas ----------
def canvas_get(path, params=None):
    """GET a Canvas endpoint, following Link-header pagination. Returns a list."""
    url = f"{CANVAS_BASE_URL}/api/v1/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {CANVAS_TOKEN}"}
    params = dict(params or {})
    params.setdefault("per_page", 100)
    results = []
    while url:
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        results.extend(data if isinstance(data, list) else [data])
        url, params = None, None
        for part in r.headers.get("Link", "").split(","):
            seg = part.split(";")
            if len(seg) >= 2 and 'rel="next"' in seg[1]:
                url = seg[0].strip().strip("<>")
    return results


def get_course_names():
    courses = canvas_get("courses", {"enrollment_state": "active"})
    return {c["id"]: c.get("name", f"Course {c['id']}") for c in courses if "id" in c}


def get_upcoming_items():
    # Look a bit into the past too (DAYS_BEHIND), not just forward — otherwise
    # anything already overdue silently falls outside the search window and
    # never gets pulled in at all.
    start = datetime.now(timezone.utc) - timedelta(days=DAYS_BEHIND)
    end = datetime.now(timezone.utc) + timedelta(days=DAYS_AHEAD)
    items = canvas_get("planner/items", {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    })
    keep = {"assignment", "quiz", "discussion_topic", "wiki_page", "sub_assignment"}
    out = {}
    for it in items:
        ptype = it.get("plannable_type", "")
        if ptype not in keep:
            continue
        p = it.get("plannable", {}) or {}
        due = p.get("due_at") or p.get("todo_date") or it.get("plannable_date")
        html_url = it.get("html_url", "")
        if html_url.startswith("/"):
            html_url = CANVAS_BASE_URL + html_url
        canvas_id = str(it.get("plannable_id") or p.get("id") or "")
        if not canvas_id:
            continue
        subs = it.get("submissions")
        out[canvas_id] = {
            "canvas_id": canvas_id,
            "title": (p.get("title") or p.get("name") or "(untitled)")[:2000],
            "course_id": it.get("course_id"),
            "due": due,
            "type": ptype,
            "url": html_url,
            "done": bool(subs.get("submitted")) if isinstance(subs, dict) else False,
        }
    return out


def get_missing_submissions():
    """Canvas's dedicated 'what have I missed' endpoint. Belt-and-suspenders
    against the planner window ever excluding something overdue — this pulls
    truly missing work regardless of how old it is."""
    items = canvas_get("users/self/missing_submissions", {
        "include[]": "planner_overrides",
        "filter[]": "submittable",
    })
    out = {}
    for a in items:
        canvas_id = str(a.get("id", ""))
        if not canvas_id:
            continue
        html_url = a.get("html_url", "")
        if html_url.startswith("/"):
            html_url = CANVAS_BASE_URL + html_url
        out[canvas_id] = {
            "canvas_id": canvas_id,
            "title": (a.get("name") or "(untitled)")[:2000],
            "course_id": a.get("course_id"),
            "due": a.get("due_at"),
            "type": "missing",
            "url": html_url,
            "done": False,   # by definition: Canvas only lists it here if it's missing
        }
    return out


# ---------- Notion ----------
def notion_headers():
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def notion_request(method, path, **kwargs):
    url = f"{NOTION_API}/{path.lstrip('/')}"
    for _ in range(5):
        r = requests.request(method, url, headers=notion_headers(), timeout=30, **kwargs)
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After", "1")))
            continue
        if not r.ok:
            sys.exit(f"Notion {method} {path} failed ({r.status_code}): {r.text}")
        return r.json()
    sys.exit(f"Notion {method} {path} kept getting rate limited.")


def resolve_data_source_id(database_id):
    """A Notion database contains one or more data sources; rows live at the
    data-source level. Resolve it from the database ID."""
    db = notion_request("GET", f"databases/{database_id}")
    sources = db.get("data_sources", [])
    if not sources:
        sys.exit(f"No data sources found on database {database_id}.")
    return sources[0]["id"]


def query_all(ds_id):
    """Return every page in a data source (following pagination)."""
    results, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = notion_request("POST", f"data_sources/{ds_id}/query", json=body)
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return results


def _rich_text_value(page, prop_name):
    rt = page.get("properties", {}).get(prop_name, {}).get("rich_text", [])
    return rt[0].get("plain_text", "").strip() if rt else ""


def upsert_courses(courses_ds_id, course_names):
    """Ensure a page exists in Courses for each active course. Returns
    {canvas_course_id: notion_page_id}."""
    existing = {}
    for page in query_all(courses_ds_id):
        cid = _rich_text_value(page, COURSE_CANVAS_ID)
        if cid:
            existing[cid] = page["id"]

    course_pages = {}
    for course_id, name in course_names.items():
        key = str(course_id)
        if key in existing:
            course_pages[course_id] = existing[key]
        else:
            res = notion_request("POST", "pages", json={
                "parent": {"type": "data_source_id", "data_source_id": courses_ds_id},
                "properties": {
                    COURSE_TITLE: {"title": [{"text": {"content": name[:2000]}}]},
                    COURSE_CANVAS_ID: {"rich_text": [{"text": {"content": key}}]},
                },
            })
            course_pages[course_id] = res["id"]
            time.sleep(0.34)
    return course_pages


def fetch_existing_assignments(ds_id):
    """Return {canvas_id: (notion_page_id, currently_complete)} for rows
    already present, so the caller can avoid ever unchecking Complete."""
    existing = {}
    for page in query_all(ds_id):
        cid = _rich_text_value(page, PROP_CANVAS_ID)
        if cid:
            is_complete = page.get("properties", {}).get(PROP_DONE, {}).get("checkbox", False)
            existing[cid] = (page["id"], bool(is_complete))
    return existing


def build_properties(item, course_pages, include_done=True):
    props = {
        PROP_TITLE: {"title": [{"text": {"content": item["title"]}}]},
        PROP_TYPE: {"select": {"name": TYPE_LABELS.get(item["type"], item["type"])}},
        PROP_CANVAS_ID: {"rich_text": [{"text": {"content": item["canvas_id"]}}]},
        PROP_DUE: {"date": {"start": item["due"]} if item["due"] else None},
    }
    if include_done:
        props[PROP_DONE] = {"checkbox": item["done"]}
    if item["url"]:
        props[PROP_URL] = {"url": item["url"]}
    page_id = course_pages.get(item["course_id"])
    if page_id:
        props[PROP_COURSE] = {"relation": [{"id": page_id}]}
    return props


def main():
    require_env()

    print("Fetching active Canvas courses...")
    course_names = get_course_names()
    print(f"  {len(course_names)} courses")

    print(f"Fetching Canvas items from {DAYS_BEHIND} days ago through {DAYS_AHEAD} days ahead...")
    items_by_id = get_upcoming_items()
    print(f"  {len(items_by_id)} items")

    print("Fetching missing submissions...")
    missing_by_id = get_missing_submissions()
    print(f"  {len(missing_by_id)} missing")

    # Merge: missing_submissions is the source of truth for "this is actually
    # missing" (it wins), everything else comes from the planner window.
    items_by_id.update(missing_by_id)
    items = list(items_by_id.values())
    print(f"  {len(items)} total after merge")

    assignments_ds = resolve_data_source_id(NOTION_DATABASE_ID)
    courses_ds = resolve_data_source_id(NOTION_COURSES_DATABASE_ID)

    print("Syncing course pages...")
    course_pages = upsert_courses(courses_ds, course_names)
    print(f"  {len(course_pages)} course pages ready")

    existing = fetch_existing_assignments(assignments_ds)
    print(f"  {len(existing)} assignments already in Notion")

    created = updated = 0
    for it in items:
        existing_entry = existing.get(it["canvas_id"])
        if existing_entry:
            page_id, currently_complete = existing_entry
            # Never let a sync run un-check something already marked complete
            # (by a manual click or a prior run) — only ever flip false->true.
            include_done = not currently_complete
            props = build_properties(it, course_pages, include_done=include_done)
            notion_request("PATCH", f"pages/{page_id}", json={"properties": props})
            updated += 1
        else:
            props = build_properties(it, course_pages, include_done=True)
            notion_request("POST", "pages", json={
                "parent": {"type": "data_source_id", "data_source_id": assignments_ds},
                "properties": props,
            })
            created += 1
        time.sleep(0.34)

    print(f"Done. Created {created}, updated {updated}.")


if __name__ == "__main__":
    main()
