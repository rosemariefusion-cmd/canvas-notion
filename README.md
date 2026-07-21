# Canvas → Notion assignment sync

Pulls your upcoming Canvas assignments (and quizzes/discussions) into a Notion
database and keeps them updated. Runs itself on GitHub Actions — no server, no
Railway, no cost.

## 1. Make a Notion database

Create a database (full-page) with these columns — **names are case-sensitive
and must match exactly**:

| Column       | Type       |
|--------------|------------|
| `Name`       | Title      |
| `Course`     | Text       |
| `Due`        | Date       |
| `Type`       | Select     |
| `Canvas URL` | URL        |
| `Canvas ID`  | Text       |
| `Done`       | Checkbox   |

## 2. Create a Notion integration

1. Go to https://www.notion.so/my-integrations → **New integration** (internal).
2. Copy the **Internal Integration Secret** → this is `NOTION_TOKEN`.
3. Open your database → **•••** menu → **Connections** → add your integration.
   (This is the step everyone forgets — the API can't see the DB without it.)
4. Grab the **database ID**: the 32-char hex chunk in the database page URL,
   e.g. `notion.so/yourspace/`**`1a2b3c...`**`?v=...` → `NOTION_DATABASE_ID`.

## 3. Get a Canvas API token

Canvas → **Account → Settings → Approved Integrations → + New Access Token**.
Copy it → `CANVAS_TOKEN`. Your `CANVAS_BASE_URL` is your school's Canvas domain,
e.g. `https://canvas.yourschool.edu` (no trailing slash).

## 4. Test it locally (optional but smart)

```bash
pip install -r requirements.txt
export CANVAS_BASE_URL="https://canvas.yourschool.edu"
export CANVAS_TOKEN="..."
export NOTION_TOKEN="..."
export NOTION_DATABASE_ID="..."
python canvas_to_notion.py
```

You should see rows appear in Notion.

## 5. Put it on autopilot (GitHub Actions)

1. Push this folder to a **private** GitHub repo.
2. Repo → **Settings → Secrets and variables → Actions → New repository secret**,
   and add: `CANVAS_BASE_URL`, `CANVAS_TOKEN`, `NOTION_TOKEN`, `NOTION_DATABASE_ID`.
3. The workflow runs every 6 hours. Trigger it by hand anytime from the
   **Actions** tab → *Sync Canvas to Notion* → **Run workflow**.

## Notes

- The sync is idempotent: it matches on `Canvas ID`, so re-runs update rows
  instead of duplicating them.
- Change how far ahead it looks with the `DAYS_AHEAD` env var (default 60).
- Keep your tokens secret. If one ever leaks, revoke and regenerate it.
