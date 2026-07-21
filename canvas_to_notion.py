#!/usr/bin/env python3
"""
canvas_to_notion.py
Sync upcoming Canvas assignments into a Notion database.

This is a one-shot, idempotent sync (an "upsert"): run it as often as you
like and it will create rows for new assignments and update existing ones
in place, never duplicating. Schedule it with cron or GitHub Actions to keep
Notion current automatically.

Required environment variables:
  CANVAS_BASE_URL      e.g. https://canvas.yourschool.edu   (no trailing slash)
  CANVAS_TOKEN         Canvas API access token
  NOTION_TOKEN         Notion internal integration secret
  NOTION_DATABASE_ID   Notion database ID (32 hex chars, from the DB page URL)

Optional:
  NOTION_DATA_SOURCE_ID  Use this data source directly, skipping auto-resolution
  DAYS_AHEAD             How many days of upcoming items to sync (default 60)
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
NOTION_DATA_SOURCE_ID = os.environ.get("NOTION_DATA_SOURCE_ID", "")
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", "60"))

NOTION_VERSION = "2026-03-11"        # current Notion API version (data-sources era)
NOTION_API = "https://api.notion.com/v1"

# Notion property names -- these MUST match your database columns exactly
# (case-sensitive). If you rename a column in Notion, change it here too.
PROP_TITLE = "Name"
PROP_COURSE = "Course"
PROP_DUE = "Due"
PROP_TYPE = "Type"
PROP_URL = "Canvas URL"
PROP_CANVAS_ID = "Canvas ID"
PROP_DONE = "Done"

TYPE_LABELS = {
    "assignment": "Assignment",
    "quiz": "Quiz",
    "discussion_topic": "Discussion",
    "wiki_page": "Page",
    "sub_assignment": "Assignment",
}


def require_env():
    missing = [k for k, v in {
        "CANVAS_BASE_URL": CANVAS_BASE_URL,
        "CANVAS_TOKEN": CANVAS_TOKEN,
        "NOTION_TOKEN": NOTION_TOKEN,
        "NOTION_DATABASE_ID": NOTION_DATABASE_ID,
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
        # Canvas paginates via the Link header; follow rel="next" if present.
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
    start = datetime.now(timezone.utc)
    end = start + timedelta(days=DAYS_AHEAD)
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
    return list(out.values())


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
        if r.status_code == 429:                       # rate limited -> back off
            time.sleep(float(r.headers.get("Retry-After", "1")))
            continue
        if not r.ok:
            sys.exit(f"Notion {method} {path} failed ({r.status_code}): {r.text}")
        return r.json()
    sys.exit(f"Notion {method} {path} kept getting rate limited.")


def resolve_data_source_id():
    """A Notion database is a container for one or more data sources; the API
    reads/writes rows at the data-source level. Auto-resolve it from the DB ID."""
    if NOTION_DATA_SOURCE_ID:
        return NOTION_DATA_SOURCE_ID
    db = notion_request("GET", f"databases/{NOTION_DATABASE_ID}")
    sources = db.get("data_sources", [])
    if not sources:
        sys.exit("No data sources on that database. Set NOTION_DATA_SOURCE_ID manually.")
    return sources[0]["id"]


def fetch_existing(ds_id):
    """Return {canvas_id: notion_page_id} for rows already in the database."""
    existing, cursor = {}, None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = notion_request("POST", f"data_sources/{ds_id}/query", json=body)
        for page in data.get("results", []):
            rt = page.get("properties", {}).get(PROP_CANVAS_ID, {}).get("rich_text", [])
            cid = rt[0].get("plain_text", "").strip() if rt else ""
            if cid:
                existing[cid] = page["id"]
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return existing


def build_properties(item, course_names):
    course = course_names.get(item["course_id"], "") if item["course_id"] else ""
    props = {
        PROP_TITLE: {"title": [{"text": {"content": item["title"]}}]},
        PROP_COURSE: {"rich_text": [{"text": {"content": course[:2000]}}]},
        PROP_TYPE: {"select": {"name": TYPE_LABELS.get(item["type"], item["type"])}},
        PROP_CANVAS_ID: {"rich_text": [{"text": {"content": item["canvas_id"]}}]},
        PROP_DONE: {"checkbox": item["done"]},
        PROP_DUE: {"date": {"start": item["due"]} if item["due"] else None},
    }
    if item["url"]:
        props[PROP_URL] = {"url": item["url"]}
    return props


def main():
    require_env()

    print("Fetching active Canvas courses...")
    course_names = get_course_names()
    print(f"  {len(course_names)} courses")

    print(f"Fetching Canvas items due in the next {DAYS_AHEAD} days...")
    items = get_upcoming_items()
    print(f"  {len(items)} items")

    ds_id = resolve_data_source_id()
    existing = fetch_existing(ds_id)
    print(f"  {len(existing)} rows already in Notion")

    created = updated = 0
    for it in items:
        props = build_properties(it, course_names)
        page_id = existing.get(it["canvas_id"])
        if page_id:
            notion_request("PATCH", f"pages/{page_id}", json={"properties": props})
            updated += 1
        else:
            notion_request("POST", "pages", json={
                "parent": {"type": "data_source_id", "data_source_id": ds_id},
                "properties": props,
            })
            created += 1
        time.sleep(0.34)   # stay under Notion's ~3 requests/second limit

    print(f"Done. Created {created}, updated {updated}.")


if __name__ == "__main__":
    main()
