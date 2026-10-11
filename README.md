# Task Tree Planner

A self-hosted, local-first task management tool — **one HTML file + one Python file**. No framework, no database, no cloud. Your data stays on your machine as plain JSON.

![Task Tree Planner screenshot](example/example.png)

---

## Features

### 🌲 Hierarchical tree
- **Space → Project → Task → Subtask** hierarchy
- Compact view: rectangle-in-rectangle nested layout showing the whole structure at a glance
- Expand/compact individual nodes with dedicated buttons
- Click any compact card to see a full-detail popup

### 📅 Schedule overview
- **Overdue / Today / Tomorrow** lanes always visible
- **Monthly calendar heatmap** — colour-coded by workload (light green → red)
- Click a day to pin its popup open — click a task inside it to jump straight to Edit
- **Postpone 1 day** / **Bring back 1 day** per task, or **Postpone All** / **Bring Back All** to shift every scheduled item at once (itemized confirm prompt, skips Fixed-date items)
- **Fixed (date)** checkbox locks a task's date against those shifts — only a manual edit can move it
- **Reserved days** — mark a day as unavailable (e.g. a work day); postpone/bring-back route around it automatically
- Weather forecast overlay on calendar days (requires `server.py`)
- Expandable calendar for a full-width view

### ⏱ Time tracking
- Time estimate per task (in hours, supports decimals like `2.5`)
- Automatic rollup through the tree — parent nodes show the sum of their children
- Clock pill colour: 🟢 green = scheduled, 🔴 red = no target date set
- Date tag visible on every card (e.g. `31. Mar`)

### 🎨 Visual design
- Priority-coloured card borders: 1-10 scale (1 = Low, 4 = Medium, 7 = High, 10 = Very High), color gradient from grey through green and orange to red
- Status pills: open, in_progress, blocked, done
- Image thumbnails with hover popup zoom
- Done tasks shown greyed-out with strikethrough and a × watermark

### ⌨️ Productivity
- Press **N** while hovering a card to instantly open "Add child" dialog
- Burger menu (☰) on every card: Edit, Complete, Add child, Delete
- Peek popup on click for compact cards — full card info without expanding
- Bidirectional relations between tasks
- **Reorganize from the Edit dialog**: move an item under any other parent (or to the top level), change its type (Space/Project/Task/Subtask — optionally cascading to nested items), and reorder it up/down among same-type siblings
- Searchable "Move to" and "Relates to" pickers — type to filter; in "Move to", use ↑ / ↓ / Enter to navigate and pick a match without the mouse

### 💾 Data
- All data saved to `data.json` alongside the HTML; every change is saved instantly through the [API](#api)
- **Live sync**: edits from other users, tabs and scripts appear within a second, with no reload and no overwriting each other
- Images uploaded directly to `images/` on the server
- Import / Export JSON at any time

---

## Getting started

### Requirements
- Python 3.8+
- A modern browser (Chrome / Firefox / Safari)

### First run — add your first user

```bash
git clone https://github.com/yourname/task-tree-planner
cd task-tree-planner
python server.py --setup
```

You will be prompted for a username and password. Credentials are stored in `config.json` as SHA-256 hashes — plaintext passwords are never saved.

### Run

```bash
python server.py
```

Open **http://localhost:8000** in your browser. You'll see a login page.

The server binds to `0.0.0.0`, so it's reachable from other devices on your LAN (phone, tablet, other machines).

### User management

```bash
python server.py --setup           # add first user
python server.py --add-user        # add another user
python server.py --update-user     # change a user's password
python server.py --remove-user     # remove a user
python server.py --list-users      # show all usernames
```

### Optional: custom port

```bash
python server.py 9000
```

Port can also be set in `config.json` (CLI argument takes priority):

```json
{ "port": 9000 }
```

---

## File structure

```
task-tree-planner/
├── task_tree_planner.html   # The entire frontend
├── server.py                # Minimal Python HTTP server
├── backup_tasks.sh          # Hourly backup script (optional)
├── config.json.example      # Config template — copy to config.json and edit
├── data.json                # Your task data (created on first save; written only by the server)
└── images/                  # Uploaded images (created automatically)
```

Backups are stored one directory up (path is configurable via `config.json`):

```
../backup_ttp_personal/
  2026/
    04-April/
      2026-04-01_14-00.json
      2026-04-01_18-00.json
```

---

## Multi-instance & config.json

Copy `config.json.example` to `config.json` and edit to taste. All keys are optional — defaults are used for anything omitted.

```json
{
  "instance_name": "Personal",
  "port": 8000,
  "backup_dir": "../backup_ttp_personal"
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `instance_name` | `"Task Tree Planner"` | Shown in the browser tab title |
| `port` | `8000` | Port the server listens on (CLI arg overrides this) |
| `backup_dir` | `../backup_ttp_<instance>` | Where `backup_tasks.sh` writes backups (relative to the instance directory, or absolute) |

To run two instances side by side, clone the repo twice into separate directories, give each a different `instance_name` and `port` in its `config.json`, and start both servers:

```bash
# Terminal 1 — Personal
cd ~/task-tree-planner-personal && python server.py

# Terminal 2 — Work
cd ~/task-tree-planner-work && python server.py
```

Each instance has its own `data.json`, `config.json`, `images/`, and backup directory.

---

## Backup (optional)

Place `backup_tasks.sh` next to `task_tree_planner.html`, then add a cron job:

```bash
chmod +x backup_tasks.sh
crontab -e
# Add:
0 * * * * /full/path/to/backup_tasks.sh >> /full/path/to/backup_tasks.log 2>&1
```

The script uses `md5sum` to detect changes — a backup is only written when the file has actually changed, keeping storage usage minimal.

---

## Weather (optional)

The calendar supports weather overlays via [Open-Meteo](https://open-meteo.com/) — no API key required. Configure your coordinates in `server.py`:

```python
WEATHER_LAT = 42.70   # Sofia, Bulgaria
WEATHER_LON = 23.32
```

The server fetches a 7-day forecast and caches historical weather in `weather_history.json`.

---

## Login & security

The server uses a **session cookie** authentication system with multi-user support:

- Users are stored in `config.json` as `{ "users": { "alice": "<sha256>", "bob": "<sha256>" } }`
- On successful login a random 64-character session token is set as an `HttpOnly` cookie
- Sessions expire after **7 days**
- All routes except `/login` require a valid session (or an [API token](#api)). Unauthenticated page requests redirect to the login page, and `/api/*` requests get a `401`
- Passwords are validated with `secrets.compare_digest` (constant-time, safe against timing attacks)
- Failed login attempts are logged with the IP address but never the attempted password
- **Add `config.json` to `.gitignore`** if you push to a public repo

```
config.json  ← contains usernames + password/token hashes, never commit this
```

Only `task_tree_planner.html` and files under `images/` (image types only) are served as static files. Data goes through the API. `config.json`, `data.json`, the server source, logs and the allowlist are never served directly.

---

## API

Everything goes through a JSON API under `/api/`: the web UI itself, scripts and integrations. You can read and change the tree, create whole nested subtrees in one call, and follow changes live.

### Auth

API clients use bearer tokens. Browser sessions also work.

```bash
python server.py --add-token my-script     # prints the token once
python server.py --list-tokens
python server.py --remove-token my-script
```

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/nodes
```

Tokens are stored as SHA-256 hashes in `config.json` under `api_tokens`. The IP allowlist still applies. When a token resolves a task, its name is recorded as `completed_by`.

### Endpoints

**Nodes**

| Method | Path | Body | Effect |
|---|---|---|---|
| GET | `/api/nodes` | — | `{revision, root_ids, nodes}` |
| GET | `/api/nodes/{id}?depth=N` | — | The node with `children` expanded into nested objects. Depth defaults to unlimited. At the depth limit, `children` stays a list of ids and the node gets `children_truncated: true` |
| POST | `/api/nodes` | `{parent_id, position?, node}` | Creates a node or a nested subtree. `parent_id: null` creates it at the top level |
| PATCH | `/api/nodes/{id}` | fields, `type?`, `cascade?` | Partial update. Changing `type` also shifts descendants unless `cascade: false` |
| POST | `/api/nodes/{id}/move` | `{parent_id, position?}` | Reparents or reorders. Moving a node under itself or one of its descendants is rejected |
| DELETE | `/api/nodes/{id}` | — | Deletes the node and all descendants, and removes them from every `relates_to` |

**Whole document, batches and sync**

| Method | Path | Body | Effect |
|---|---|---|---|
| GET | `/api/data` | — | The full document: `{version, revision, settings, nodes}` |
| PUT | `/api/data` | full document | Replaces everything (Import). Dangling references are repaired and legacy fields migrated |
| POST | `/api/batch` | `{ops: [...]}` | Applies a list of operations atomically, all or none (see below) |
| GET | `/api/changes?since=N&wait=S` | — | What changed since revision `N`. With `wait`, it long-polls for up to `S` seconds (max 30) until something changes |
| POST | `/api/images` | multipart `file` | Uploads an image to `images/`. Returns `{path}` |

Batch operations, each with an `op` field:
- `{"op": "create", "parent_id", "position"?, "node"}`
- `{"op": "update", "id", "set": {fields}, "type"?, "cascade"?}`
- `{"op": "move", "id", "parent_id", "position"?}`
- `{"op": "delete", "id"}`
- `{"op": "reserve_date", "date", "reserved": true|false}`

The response has one entry per op in `results`, in order. An error names the op that failed, e.g. `ops[2] (move): ...`.

Editable fields:
- `name`, `description`, `image`
- `priority` (1–10)
- `status` (`open`, `in_progress`, `blocked`, `done`, `wont_do`)
- `target_date` (`YYYY-MM-DD` or null), `date_fixed`
- `time_estimate` (hours)
- `relates_to` (list of ids)
- `collapsed`, `archived`

Setting a resolved status fills `completed_date` and `completed_by`, exactly as the UI does. `id`, `parent_id` and `children` are managed by the server.

`position` is optional on create and move. It is a 0-based index among the new siblings, and leaving it out appends.

### Creating nested tasks

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  http://localhost:8000/api/nodes -d '{
  "parent_id": "project_1",
  "position": 0,
  "node": {
    "name": "Launch", "priority": 7, "ref": "launch",
    "children": [
      {"name": "Write copy", "children": [{"name": "Draft", "relates_to": ["launch"]}]},
      {"name": "Review", "target_date": "2026-11-01"}
    ]
  }
}'
# → {"ok": true, "revision": 12, "id": "task_7",
#    "ids": {"0": "task_7", "0.0": "subtask_4", "0.0.0": "subtask_5", "0.1": "subtask_6"},
#    "refs": {"launch": "task_7"}}
```

- **Types follow the hierarchy.** A child of a project becomes a task, a child of a task becomes a subtask, and subtasks nest indefinitely. You can pass an explicit `type` only at the default level or deeper.
- **The whole subtree is created atomically.** If any node is invalid, nothing is written and the error names its path, such as `0.1`.
- **New IDs come back in the response.** `ids` maps payload paths to new IDs. Any node can carry a `ref` label, which other nodes in the same payload can use in `relates_to`. The `refs` map in the response translates labels to IDs.
- **Limits:** 1000 nodes and 32 levels per request.

### Revisions, sync and concurrency

The server is the single source of truth. It keeps the data in memory and writes `data.json` atomically (temp file plus rename) on every change. Writes are serialized, and each one bumps a `revision` counter.

Every write response looks like `{ok, since, revision, changes: {nodes, deleted, settings?}, ...}`:
- `changes` holds the latest state of every node the write touched.
- `since` and `revision` are the revisions before and after the write.

To stay in sync, a client keeps the revision it has and calls `GET /api/changes?since=<revision>&wait=25` in a loop.
- **Normal case:** the response holds the latest state of every node touched since then, plus `settings` if they changed. Applying it is idempotent.
- **Reset:** when the server can't cover the gap (after a restart, an import, or someone editing `data.json` by hand), it answers `{reset: true, data}` with the full document instead.

**The web UI uses exactly this:**
- Each change is applied on screen immediately.
- The matching operations are sent through an in-order queue, and the server's result is then merged in.
- Edits from other users, tabs and scripts stream in through the long-poll.
- The Edit dialog only sends fields you actually changed, so a change someone else made to another field while the dialog was open isn't overwritten.
- If a write is rejected, the UI reloads the data from the server.

**Optimistic concurrency for scripts:** add `"if_revision": N` to a write body, or `?if_revision=N` on DELETE. The server answers `409 {"error":"conflict","revision":current}` if anything changed since revision `N`.

## Running as a systemd service

Create `/etc/systemd/system/task-planner.service`:

```ini
[Unit]
Description=Task Tree Planner
After=network.target

[Service]
ExecStart=/usr/bin/python3 /path/to/server.py
WorkingDirectory=/path/to/
Restart=always
User=youruser

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now task-planner
```

---

## Keyboard shortcuts

| Key | Action |
|-----|--------|
| `N` | Add child to hovered card |
| `Esc` | Close peek popup |

---

## Data format

`data.json` is plain, human-readable JSON — easy to back up, version with git, or process with scripts:

```json
{
  "version": 1,
  "revision": 42,
  "settings": { "root_ids": ["space_1"], "reserved_dates": [] },
  "nodes": {
    "space_1": {
      "id": "space_1",
      "type": "space",
      "name": "My Space",
      "status": "open",
      "priority": 7,
      "target_date": "2026-04-15",
      "date_fixed": false,
      "time_estimate": null,
      "children": ["project_1"],
      "relates_to": [],
      "collapsed": false,
      "archived": false
    }
  }
}
```

Node types: `space` → `project` → `task` → `subtask`

---

## Why not use an existing tool?

Most task managers are either cloud-dependent, too heavy, or don't offer a visual hierarchy tree with time rollup and a calendar heatmap in one place. This tool is:

- **Zero dependencies** — no npm, no pip packages, no Docker
- **Yours** — data is a plain JSON file you can read, grep, and version
- **Fast** — loads instantly, saves instantly
- **Hackable** — everything is in two files, easy to modify

---

## License

MIT — do whatever you want with it.
