#!/usr/bin/env python3
"""
Task Tree Planner — local server with multi-user login

User management:
    python server.py --setup          # add first user (alias for --add-user)
    python server.py --add-user       # add another user
    python server.py --update-user    # change a user's password
    python server.py --remove-user    # remove a user
    python server.py --list-users     # show all usernames

API tokens (for the /api/v1 API, sent as "Authorization: Bearer <token>"):
    python server.py --add-token NAME     # create a token (printed once)
    python server.py --remove-token NAME  # revoke a token
    python server.py --list-tokens        # show token names

Run:
    python server.py [port]           # default port: 8000
"""

import collections
import datetime
import hashlib
import http.cookies
import ipaddress
import json
import os
import re
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import unquote, urlparse, parse_qs
from urllib.request import urlopen, Request

_MGMT_FLAGS        = {"--setup", "--add-user", "--remove-user", "--list-users", "--update-user",
                      "--add-token", "--remove-token", "--list-tokens"}
BASE_DIR           = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE        = os.path.join(BASE_DIR, "config.json")
IMAGES_DIR         = os.path.join(BASE_DIR, "images")
DATA_FILE          = os.path.join(BASE_DIR, "data.json")
WEATHER_HISTORY_FILE = os.path.join(BASE_DIR, "weather_history.json")
ALLOWLIST_FILE     = os.path.join(BASE_DIR, "allowlist.txt")

# Read config early so PORT and INSTANCE_NAME are available at module level
_STARTUP_CFG: dict = {}
try:
    with open(CONFIG_FILE) as _f:
        _STARTUP_CFG = json.load(_f)
except Exception:
    pass

PORT          = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() \
                else _STARTUP_CFG.get("port", 8000)
INSTANCE_NAME = _STARTUP_CFG.get("instance_name", "Task Tree Planner")

SESSION_TTL        = 7 * 24 * 3600
_sessions: dict    = {}
_safe_instance     = re.sub(r'[^a-zA-Z0-9]', '_', INSTANCE_NAME)
COOKIE_NAME        = f"session_{_safe_instance}_{PORT}"  # instance+port scoped so multi-instance deployments don't clobber each other's cookies

# ── IP allowlist ──────────────────────────────────────────────────────────────

def load_allowlist() -> list:
    """
    Load allowed CIDRs/IPs from allowlist.txt.
    Lines starting with '#' and blank lines are ignored.
    Returns a list of ip_network objects; empty list = allow all.
    """
    if not os.path.isfile(ALLOWLIST_FILE):
        return []
    nets = []
    with open(ALLOWLIST_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                nets.append(ipaddress.ip_network(line, strict=False))
            except ValueError:
                print(f"  [allowlist] Invalid entry ignored: {line!r}")
    print(f"  [allowlist] Loaded {len(nets)} networks from {ALLOWLIST_FILE}")
    return nets

_allowlist: list = []   # populated at startup; empty = disabled

def is_allowed(ip: str) -> bool:
    """Return True if allowlist is empty (disabled) or ip matches an entry."""
    if not _allowlist:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _allowlist)


def load_config() -> dict:
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def get_users() -> dict:
    """Return {username_lower: password_hash}. Migrates old single-user format."""
    cfg = load_config()
    if "users" in cfg:
        return cfg["users"]
    # Migrate old format
    if cfg.get("username") and cfg.get("password_hash"):
        return {cfg["username"].lower(): cfg["password_hash"]}
    return {}


def save_users(users: dict):
    cfg = load_config()
    cfg["users"] = users
    # Remove legacy keys if present
    cfg.pop("username", None)
    cfg.pop("password_hash", None)
    save_config(cfg)


def check_credentials(username: str, password: str) -> bool:
    users = get_users()
    key = username.strip().lower()
    stored_hash = users.get(key)
    if not stored_hash:
        # Still run a dummy comparison to avoid timing attacks
        secrets.compare_digest("x", "y")
        return False
    return secrets.compare_digest(stored_hash, hash_password(password))


def has_any_user() -> bool:
    return bool(get_users())


def setup_user(update: bool = False):
    """Add or update a user interactively."""
    import getpass
    action = "Update" if update else "Add"
    print(f"\n── Task Tree Planner — {action} User ──────────────────")
    users = get_users()

    username = input("Username: ").strip().lower()
    if not username:
        print("Username cannot be empty.")
        return

    if update and username not in users:
        print(f"User '{username}' not found.")
        return

    while True:
        pw  = getpass.getpass("Password: ")
        pw2 = getpass.getpass("Confirm:  ")
        if not pw:
            print("Password cannot be empty.")
            continue
        if pw != pw2:
            print("Passwords do not match. Try again.\n")
            continue
        break

    users[username] = hash_password(pw)
    save_users(users)
    verb = "updated" if update and username in users else "added"
    print(f"✓ User '{username}' {verb}.\n")


def remove_user():
    """Remove a user interactively."""
    users = get_users()
    if not users:
        print("No users configured.")
        return
    print("\n── Task Tree Planner — Remove User ─────────────────────")
    print("Current users:", ", ".join(sorted(users)))
    username = input("Username to remove: ").strip().lower()
    if username not in users:
        print(f"User '{username}' not found.")
        return
    if len(users) == 1:
        confirm = input("This is the last user — removing it will disable auth. Continue? [y/N] ")
        if confirm.lower() != "y":
            print("Cancelled.")
            return
    del users[username]
    save_users(users)
    print(f"✓ User '{username}' removed.\n")


def list_users():
    users = get_users()
    if not users:
        print("No users configured.")
    else:
        print("\nConfigured users:")
        for u in sorted(users):
            print(f"  • {u}")
        print()


def create_session(username: str) -> str:
    token = secrets.token_hex(32)
    _sessions[token] = {"expires": time.time() + SESSION_TTL, "username": username}
    return token


def validate_session(token) -> bool:
    if not token:
        return False
    entry = _sessions.get(token)
    if not entry:
        return False
    if time.time() > entry["expires"]:
        del _sessions[token]
        return False
    return True


def get_session_username(token) -> str:
    entry = _sessions.get(token)
    return entry["username"] if entry else ""


def get_session_token(headers):
    raw = headers.get("Cookie", "")
    if not raw:
        return None
    c = http.cookies.SimpleCookie()
    c.load(raw)
    m = c.get(COOKIE_NAME)
    return m.value if m else None


# ── API tokens ────────────────────────────────────────────────────────────────
# Stored as SHA-256 hashes in config.json under "api_tokens": {name: hash}.
# Tokens are 32 random bytes, so an unsalted fast hash is sufficient.

def get_api_tokens() -> dict:
    return load_config().get("api_tokens", {})


def add_api_token(name: str):
    name = name.strip()
    if not name:
        print("Token name cannot be empty."); return
    cfg = load_config()
    tokens = cfg.setdefault("api_tokens", {})
    if name in tokens:
        print(f"Token '{name}' already exists — remove it first to rotate."); return
    token = secrets.token_urlsafe(32)
    tokens[name] = hash_password(token)
    save_config(cfg)
    print(f"✓ API token '{name}' created. Store it now — it will not be shown again:\n\n  {token}\n")


def remove_api_token(name: str):
    cfg = load_config()
    if name not in cfg.get("api_tokens", {}):
        print(f"Token '{name}' not found."); return
    del cfg["api_tokens"][name]
    save_config(cfg)
    print(f"✓ API token '{name}' removed.")


def check_bearer(headers):
    """Return the token's name if the Authorization header carries a valid token, else None."""
    auth = headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    digest = hash_password(auth[7:].strip())
    for name, stored in get_api_tokens().items():
        if secrets.compare_digest(stored, digest):
            return name
    return None


# ── Data store ────────────────────────────────────────────────────────────────
# The server is the single source of truth. data.json is kept in memory (reloaded
# if the file changes on disk, e.g. a restored backup) and every write goes through
# DATA_LOCK and an atomic replace. Each write bumps data["revision"] and records
# which nodes it touched in a change log, so clients can sync incrementally
# (GET /api/changes) and long-poll for live updates.

DATA_LOCK        = threading.Lock()
DATA_CHANGED     = threading.Condition(DATA_LOCK)
MAX_BODY         = 20 * 1024 * 1024
MAX_CREATE_NODES = 1000
MAX_CREATE_DEPTH = 32
MAX_BATCH_OPS    = 1000
MAX_POLL_WAIT    = 30
CHANGE_LOG_SIZE  = 1000

TYPE_CHILDREN = {"space": "project", "project": "task", "task": "subtask", "subtask": "subtask"}
TYPE_ORDER    = ["space", "project", "task", "subtask"]
STATUSES      = {"open", "in_progress", "blocked", "done", "wont_do"}
RESOLVED      = {"done", "wont_do"}
EDITABLE      = {"name", "description", "image", "priority", "status", "target_date",
                 "date_fixed", "time_estimate", "relates_to", "collapsed", "archived"}
LEGACY_PRIORITY = {"very_high": 10, "high": 7, "medium": 4, "low": 1}

_cache: dict = {"data": None, "stamp": None}
# (revision, touched node ids, settings changed, reset) — reset means "resync everything"
_change_log: collections.deque = collections.deque(maxlen=CHANGE_LOG_SIZE)


class ApiError(Exception):
    def __init__(self, status: int, error: str, **extra):
        super().__init__(error)
        self.status, self.error, self.extra = status, error, extra


class RawJson:
    """Pre-serialized JSON body (serialized under DATA_LOCK so it can't change mid-send)."""
    def __init__(self, payload):
        self.body = json.dumps(payload, ensure_ascii=False).encode()


def empty_data() -> dict:
    return {"version": 1, "revision": 0,
            "settings": {"images_dir": "images/", "root_ids": [], "reserved_dates": []},
            "nodes": {}}


def migrate_data(d: dict):
    """Normalise legacy/missing fields in place (server-side port of the UI's migrateData)."""
    settings = d.setdefault("settings", {})
    if not isinstance(settings.get("root_ids"), list):
        settings["root_ids"] = []
    if not isinstance(settings.get("reserved_dates"), list):
        settings["reserved_dates"] = []
    d.setdefault("nodes", {})
    if not isinstance(d.get("revision"), int) or isinstance(d.get("revision"), bool):
        d["revision"] = 0
    for nid, node in d["nodes"].items():
        node["id"] = nid
        if isinstance(node.get("depends_on"), list) and not isinstance(node.get("relates_to"), list):
            node["relates_to"] = list(node["depends_on"])
        node.pop("depends_on", None)
        if not isinstance(node.get("relates_to"), list):
            node["relates_to"] = []
        node.setdefault("time_estimate", None)
        node.setdefault("completed_date", None)
        node.setdefault("date_fixed", False)
        p = node.get("priority")
        if isinstance(p, str) and p in LEGACY_PRIORITY:
            p = LEGACY_PRIORITY[p]
        node["priority"] = p if isinstance(p, int) and not isinstance(p, bool) and 1 <= p <= 10 else 4


def read_data() -> dict:
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return empty_data()
    except ValueError as e:
        raise ApiError(500, f"data.json is not valid JSON: {e}")
    if not isinstance(d, dict):
        raise ApiError(500, "data.json must contain a JSON object")
    migrate_data(d)
    return d


def write_data(d: dict):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, DATA_FILE)


def _file_stamp():
    try:
        st = os.stat(DATA_FILE)
        return (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        return None


def current_data() -> dict:
    """The in-memory data, reloaded if data.json changed on disk. Caller holds DATA_LOCK."""
    stamp = _file_stamp()
    if _cache["data"] is not None and stamp == _cache["stamp"]:
        return _cache["data"]
    d = read_data()
    prev = _cache["data"]
    if prev is not None:
        # Changed outside the server (hand edit, restored backup): make sure clients
        # see a newer revision and resync from scratch.
        if d["revision"] <= prev["revision"]:
            d["revision"] = prev["revision"] + 1
        _change_log.append((d["revision"], frozenset(), False, True))
        DATA_CHANGED.notify_all()
    _cache.update(data=d, stamp=stamp)
    return d


def _snapshot(d):
    return ({k: json.dumps(v, sort_keys=True) for k, v in d["nodes"].items()},
            json.dumps(d["settings"], sort_keys=True))


def mutate(fn, if_revision=None, reset=False) -> dict:
    """Run fn(data) under the lock, persist, and log what changed.

    If fn raises, nothing is written (and the possibly half-modified in-memory copy is
    dropped). Returns {ok, since, revision, changes: {nodes, deleted, settings?}, ...fn result}.
    reset=True marks a whole-document replace: clients resync instead of patching.
    """
    with DATA_LOCK:
        d = current_data()
        since = d["revision"]
        if if_revision is not None and if_revision != since:
            raise ApiError(409, "conflict", revision=since)
        before_nodes, before_settings = _snapshot(d)
        try:
            result = fn(d) or {}
            d["revision"] = since + 1
            write_data(d)
        except BaseException:
            _cache.update(data=None, stamp=None)
            raise
        _cache["stamp"] = _file_stamp()

        after_nodes, after_settings = _snapshot(d)
        changed = [k for k, v in after_nodes.items() if before_nodes.get(k) != v]
        deleted = [k for k in before_nodes if k not in after_nodes]
        settings_changed = after_settings != before_settings
        _change_log.append((d["revision"], frozenset(changed) | frozenset(deleted), settings_changed, reset))
        DATA_CHANGED.notify_all()

        out = {"ok": True, "since": since, "revision": d["revision"], **result}
        if reset:
            out["reset"] = True
        else:
            changes = {"nodes": {k: d["nodes"][k] for k in changed}, "deleted": deleted}
            if settings_changed:
                changes["settings"] = d["settings"]
            out["changes"] = changes
        return RawJson(out)


def changes_since(since: int) -> dict:
    """Everything a client at revision `since` needs to catch up. Caller holds DATA_LOCK.

    Returns the latest state of every node touched since then (so applying it is
    idempotent), or {reset: true, data} when the log can't cover the gap.
    """
    d = current_data()
    cur = d["revision"]
    if since == cur:
        return {"ok": True, "since": since, "revision": cur, "changes": {"nodes": {}, "deleted": []}}
    entries = [e for e in _change_log if since < e[0] <= cur]
    if since > cur or len(entries) != cur - since or any(e[3] for e in entries):
        return {"ok": True, "reset": True, "revision": cur, "data": d}
    ids = set().union(*(e[1] for e in entries))
    changes = {"nodes": {i: d["nodes"][i] for i in ids if i in d["nodes"]},
               "deleted": sorted(i for i in ids if i not in d["nodes"])}
    if any(e[2] for e in entries):
        changes["settings"] = d["settings"]
    return {"ok": True, "since": since, "revision": cur, "changes": changes}


def wait_for_changes(since: int, wait: float) -> RawJson:
    """Long-poll: block up to `wait` seconds until the revision moves past `since`."""
    deadline = time.monotonic() + wait
    with DATA_LOCK:
        while current_data()["revision"] == since:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            DATA_CHANGED.wait(min(remaining, 5))   # wake periodically to notice on-disk edits
        return RawJson(changes_since(since))


def full_data() -> RawJson:
    with DATA_LOCK:
        return RawJson({"ok": True, **current_data()})


# ── Tree helpers (Python ports of the UI's logic) ─────────────────────────────

def make_id(d, node_type):
    idx = 1
    while f"{node_type}_{idx}" in d["nodes"]:
        idx += 1
    return f"{node_type}_{idx}"


def collect_descendants(d, node_id):
    """node_id plus all descendants, depth-first. Guards against cycles in corrupt data."""
    out, seen, stack = [], set(), [node_id]
    while stack:
        nid = stack.pop()
        if nid in seen or nid not in d["nodes"]:
            continue
        seen.add(nid)
        out.append(nid)
        stack.extend(reversed(d["nodes"][nid].get("children") or []))
    return out


def siblings_of(d, node):
    """The ordered array a node lives in — its parent's children, or settings.root_ids."""
    pid = node.get("parent_id")
    if pid and pid in d["nodes"]:
        return d["nodes"][pid].setdefault("children", [])
    return d["settings"].setdefault("root_ids", [])


def detach(d, node):
    sib = siblings_of(d, node)
    sib[:] = [x for x in sib if x != node["id"]]


def attach(d, node, parent_id, position=None):
    if parent_id is None:
        node["parent_id"] = None
        sib = d["settings"].setdefault("root_ids", [])
    else:
        parent = d["nodes"][parent_id]
        parent["collapsed"] = False
        node["parent_id"] = parent_id
        sib = parent.setdefault("children", [])
    if position is None or position >= len(sib):
        sib.append(node["id"])
    else:
        sib.insert(position, node["id"])


def change_type(d, node, new_type, cascade=True):
    delta = TYPE_ORDER.index(new_type) - TYPE_ORDER.index(node.get("type", "subtask"))
    node["type"] = new_type
    if not cascade or not delta:
        return
    for nid in collect_descendants(d, node["id"])[1:]:
        desc = d["nodes"][nid]
        cur = TYPE_ORDER.index(desc["type"]) if desc.get("type") in TYPE_ORDER else len(TYPE_ORDER) - 1
        desc["type"] = TYPE_ORDER[min(len(TYPE_ORDER) - 1, max(0, cur + delta))]


def apply_status(node, status, user):
    """Set status and keep completed_date/completed_by in sync, as the Edit dialog does."""
    prev = node.get("status")
    node["status"] = status
    if status in RESOLVED and prev not in RESOLVED:
        node["completed_date"] = node.get("target_date") or time.strftime("%Y-%m-%d")
        node["completed_by"] = user or None
    elif status not in RESOLVED:
        node["completed_date"] = None
        node["completed_by"] = None


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def clean_fields(fields: dict, where: str = "") -> dict:
    """Validate editable fields. relates_to is only shape-checked; callers resolve ids."""
    p = f"{where}: " if where else ""
    out = {}
    for k, v in fields.items():
        if k not in EDITABLE:
            raise ApiError(400, f"{p}unknown or read-only field '{k}'")
        if k == "name":
            if not isinstance(v, str) or not v.strip():
                raise ApiError(400, f"{p}name must be a non-empty string")
            v = v.strip()
        elif k == "description":
            if not isinstance(v, str):
                raise ApiError(400, f"{p}description must be a string")
            v = v.strip()
        elif k == "image":
            if v is not None and not isinstance(v, str):
                raise ApiError(400, f"{p}image must be a string or null")
            v = (v or "").strip() or None
        elif k == "priority":
            if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 10:
                raise ApiError(400, f"{p}priority must be an integer 1-10")
        elif k == "status":
            if v not in STATUSES:
                raise ApiError(400, f"{p}status must be one of {sorted(STATUSES)}")
        elif k == "target_date":
            if v is not None:
                if not isinstance(v, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                    raise ApiError(400, f"{p}target_date must be YYYY-MM-DD or null")
                try:
                    datetime.date.fromisoformat(v)
                except ValueError:
                    raise ApiError(400, f"{p}target_date is not a valid date")
        elif k in ("date_fixed", "collapsed", "archived"):
            if not isinstance(v, bool):
                raise ApiError(400, f"{p}{k} must be a boolean")
        elif k == "time_estimate":
            if v is not None and (not _is_number(v) or v < 0):
                raise ApiError(400, f"{p}time_estimate must be a non-negative number or null")
        elif k == "relates_to":
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise ApiError(400, f"{p}relates_to must be a list of ids")
        out[k] = v
    return out


def resolve_relates(d, node_id, ids, refs=None):
    out = []
    for x in ids:
        target = (refs or {}).get(x, x)
        if target not in d["nodes"]:
            raise ApiError(400, f"relates_to: unknown id or ref '{x}'")
        if target != node_id and target not in out:
            out.append(target)
    return out


def _parse_position(v):
    if v is None:
        return None
    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
        raise ApiError(400, "position must be a non-negative integer")
    return v


def create_subtree(d, parent_id, position, spec, user):
    """Create spec (a node with optional nested "children") under parent_id.

    Types follow the hierarchy (space → project → task → subtask). Returns the
    new root id plus maps of payload path ("0", "0.1", ...) and "ref" labels to ids.
    """
    if parent_id is not None and parent_id not in d["nodes"]:
        raise ApiError(404, f"parent '{parent_id}' not found")
    ids, refs, pending_relates = {}, {}, []
    count = 0

    def build(spec, parent_id, position, path, depth):
        nonlocal count
        if not isinstance(spec, dict):
            raise ApiError(400, f"{path}: node must be an object")
        if depth > MAX_CREATE_DEPTH:
            raise ApiError(400, f"nesting deeper than {MAX_CREATE_DEPTH} levels")
        count += 1
        if count > MAX_CREATE_NODES:
            raise ApiError(400, f"more than {MAX_CREATE_NODES} nodes in one request")

        spec = dict(spec)
        children = spec.pop("children", [])
        ref = spec.pop("ref", None)
        explicit_type = spec.pop("type", None)
        relates = spec.pop("relates_to", [])
        if not isinstance(children, list):
            raise ApiError(400, f"{path}: children must be a list")
        if "name" not in spec:
            raise ApiError(400, f"{path}: name is required")
        fields = clean_fields({**spec, "relates_to": relates}, path)
        fields.pop("relates_to")

        parent_type = d["nodes"][parent_id].get("type") if parent_id else None
        default_type = TYPE_CHILDREN.get(parent_type, "subtask") if parent_id else "space"
        if explicit_type is not None:
            if explicit_type not in TYPE_ORDER or TYPE_ORDER.index(explicit_type) < TYPE_ORDER.index(default_type):
                raise ApiError(400, f"{path}: type '{explicit_type}' not allowed here (expected {default_type} or lower)")
        node_type = explicit_type or default_type

        nid = make_id(d, node_type)
        status = fields.pop("status", "open")
        node = {"id": nid, "type": node_type, "parent_id": None, "name": "", "description": "",
                "image": None, "children": [], "collapsed": False, "priority": 4, "status": "open",
                "target_date": None, "date_fixed": False, "time_estimate": None, "relates_to": [],
                "archived": False, "completed_date": None}
        node.update(fields)
        apply_status(node, status, user)
        d["nodes"][nid] = node
        attach(d, node, parent_id, position)

        ids[path] = nid
        if ref is not None:
            if not isinstance(ref, str) or ref in refs:
                raise ApiError(400, f"{path}: ref must be a unique string")
            refs[ref] = nid
        pending_relates.append((node, relates))
        for i, child in enumerate(children):
            build(child, nid, None, f"{path}.{i}", depth + 1)

    build(spec, parent_id, position, "0", 0)
    for node, relates in pending_relates:
        node["relates_to"] = resolve_relates(d, node["id"], relates, refs)
    return {"id": ids["0"], "ids": ids, "refs": refs}


def subtree_view(d, node_id, depth):
    """Node copy with "children" expanded to objects down to depth (None = unlimited)."""
    node = dict(d["nodes"][node_id])
    child_ids = [c for c in node.get("children") or [] if c in d["nodes"]]
    if depth is not None and depth <= 0:
        node["children"] = child_ids
        if child_ids:
            node["children_truncated"] = True
    else:
        nxt = None if depth is None else depth - 1
        node["children"] = [subtree_view(d, c, nxt) for c in child_ids]
    return node


# ── Write operations ──────────────────────────────────────────────────────────
# Each op validates its arguments, mutates d in place and returns extra result
# fields. They run inside mutate(), so a failing op leaves data.json untouched.
# The REST endpoints and POST /api/batch share them.

def _check_keys(args, allowed):
    unknown = set(args) - set(allowed) - {"op"}
    if unknown:
        raise ApiError(400, f"unknown argument(s): {', '.join(sorted(unknown))}")


def _require_node(d, node_id):
    if not isinstance(node_id, str) or node_id not in d["nodes"]:
        raise ApiError(404, f"node '{node_id}' not found")
    return d["nodes"][node_id]


def _parent_arg(d, args):
    if "parent_id" not in args:
        raise ApiError(400, "parent_id is required (use null for top level)")
    parent_id = args["parent_id"]
    if parent_id is not None and (not isinstance(parent_id, str) or parent_id not in d["nodes"]):
        raise ApiError(404, f"parent '{parent_id}' not found")
    return parent_id


def op_create(d, args, user):
    """{parent_id, position?, node: {..., children: [...]}}"""
    _check_keys(args, {"parent_id", "position", "node"})
    parent_id = _parent_arg(d, args)
    if not isinstance(args.get("node"), dict):
        raise ApiError(400, "node must be an object")
    return create_subtree(d, parent_id, _parse_position(args.get("position")), args["node"], user)


def op_update(d, args, user):
    """{id, set: {fields}, type?, cascade?}"""
    _check_keys(args, {"id", "set", "type", "cascade"})
    node = _require_node(d, args.get("id"))
    fields = args.get("set", {})
    if not isinstance(fields, dict):
        raise ApiError(400, "set must be an object")
    fields = clean_fields(fields)
    new_type, cascade = args.get("type"), args.get("cascade", True)
    if new_type is not None and new_type not in TYPE_ORDER:
        raise ApiError(400, f"type must be one of {TYPE_ORDER}")
    if not isinstance(cascade, bool):
        raise ApiError(400, "cascade must be a boolean")
    if not fields and new_type is None:
        raise ApiError(400, "nothing to update")
    status = fields.pop("status", None)
    if "relates_to" in fields:
        fields["relates_to"] = resolve_relates(d, node["id"], fields["relates_to"])
    node.update(fields)
    if status is not None and status != node.get("status"):
        apply_status(node, status, user)
    if new_type is not None and new_type != node.get("type"):
        change_type(d, node, new_type, cascade)
    return {"id": node["id"]}


def op_move(d, args, user):
    """{id, parent_id, position?} — reparent and/or reorder."""
    _check_keys(args, {"id", "parent_id", "position"})
    node = _require_node(d, args.get("id"))
    parent_id = _parent_arg(d, args)
    position = _parse_position(args.get("position"))
    if parent_id is not None and parent_id in collect_descendants(d, node["id"]):
        raise ApiError(400, "cannot move a node under itself or its descendants")
    detach(d, node)
    attach(d, node, parent_id, position)
    return {"id": node["id"]}


def op_delete(d, args, user):
    """{id} — cascades to descendants and strips relates_to references."""
    _check_keys(args, {"id"})
    node = _require_node(d, args.get("id"))
    doomed = collect_descendants(d, node["id"])
    detach(d, node)
    for nid in doomed:
        del d["nodes"][nid]
    gone = set(doomed)
    for n in d["nodes"].values():
        if any(x in gone for x in n.get("relates_to") or []):
            n["relates_to"] = [x for x in n["relates_to"] if x not in gone]
    return {"deleted": doomed}


def op_reserve_date(d, args, user):
    """{date, reserved} — mark/unmark a day as reserved (postpone skips it)."""
    _check_keys(args, {"date", "reserved"})
    date = args.get("date")
    clean_fields({"target_date": date})           # same YYYY-MM-DD validation
    if date is None:
        raise ApiError(400, "date is required")
    if not isinstance(args.get("reserved"), bool):
        raise ApiError(400, "reserved must be a boolean")
    dates = d["settings"].setdefault("reserved_dates", [])
    if args["reserved"] and date not in dates:
        dates.append(date)
    elif not args["reserved"]:
        dates[:] = [x for x in dates if x != date]
    return {}


OPS = {"create": op_create, "update": op_update, "move": op_move,
       "delete": op_delete, "reserve_date": op_reserve_date}


def op_batch(d, ops, user):
    """Apply a list of ops atomically — all of them or none."""
    if not isinstance(ops, list) or not ops:
        raise ApiError(400, "ops must be a non-empty list")
    if len(ops) > MAX_BATCH_OPS:
        raise ApiError(400, f"more than {MAX_BATCH_OPS} ops in one batch")
    results = []
    for i, op in enumerate(ops):
        if not isinstance(op, dict) or op.get("op") not in OPS:
            raise ApiError(400, f"ops[{i}]: op must be one of {sorted(OPS)}")
        try:
            results.append(OPS[op["op"]](d, op, user))
        except ApiError as e:
            raise ApiError(e.status, f"ops[{i}] ({op['op']}): {e.error}", **e.extra)
    return {"results": results}


def prepare_import(doc) -> dict:
    """Validate a whole document (Import JSON) and repair dangling references."""
    if not isinstance(doc, dict) or not isinstance(doc.get("nodes"), dict) \
            or not isinstance(doc.get("settings"), dict) \
            or not isinstance(doc["settings"].get("root_ids"), list):
        raise ApiError(400, "Invalid data format: expected {settings: {root_ids: []}, nodes: {}}")
    if not all(isinstance(n, dict) for n in doc["nodes"].values()):
        raise ApiError(400, "Invalid data format: every node must be an object")
    doc = json.loads(json.dumps(doc))
    doc.pop("ok", None)
    migrate_data(doc)
    nodes = doc["nodes"]
    for n in nodes.values():
        n["children"] = [c for c in n.get("children") or [] if c in nodes]
        n["relates_to"] = [r for r in n["relates_to"] if r in nodes]
        if n.get("parent_id") not in nodes:
            n["parent_id"] = None
        if n.get("type") not in TYPE_ORDER:
            n["type"] = "subtask"
    roots = []
    for r in doc["settings"]["root_ids"]:
        if r in nodes and r not in roots:
            roots.append(r)
    doc["settings"]["root_ids"] = roots
    return doc


def op_replace(d, doc, user):
    """Replace the whole document (keeps the revision counter moving forward)."""
    revision = d["revision"]
    d.clear()
    d.update(doc)
    d["revision"] = revision
    return {}


LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Task Tree Planner — Login</title>
  <style>
    :root { --green: #10b981; --red: #ef4444; --line: #d1d5db; }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      background: #f3f4f6;
      font-family: Inter, Arial, sans-serif;
    }
    .card {
      background: white;
      border: 1px solid var(--line);
      border-radius: 24px;
      box-shadow: 0 6px 24px rgba(0,0,0,0.08);
      padding: 40px 36px;
      width: min(380px, 92vw);
    }
    h1 { font-size: 22px; margin-bottom: 6px; }
    .sub { font-size: 13px; color: #6b7280; margin-bottom: 28px; }
    .form-field { margin-bottom: 14px; }
    label { font-size: 13px; font-weight: 600; display: block; margin-bottom: 6px; }
    input[type=text], input[type=password] {
      width: 100%;
      padding: 11px 14px;
      border: 1px solid var(--line);
      border-radius: 12px;
      font: inherit;
      font-size: 15px;
      outline: none;
      transition: border-color 0.15s;
    }
    input[type=text]:focus, input[type=password]:focus { border-color: var(--green); }
    .error {
      margin-top: 4px;
      padding: 10px 14px;
      background: #fef2f2;
      border: 1px solid #fca5a5;
      border-radius: 10px;
      font-size: 13px;
      color: var(--red);
      display: none;
    }
    .error.show { display: block; }
    button {
      margin-top: 6px;
      width: 100%;
      padding: 12px;
      background: #111827;
      color: white;
      border: none;
      border-radius: 12px;
      font: inherit;
      font-size: 15px;
      font-weight: 600;
      cursor: pointer;
      transition: opacity 0.15s;
    }
    button:hover { opacity: 0.88; }
    .tree-icon { font-size: 36px; margin-bottom: 16px; }
  </style>
</head>
<body>
  <div class="card">
    <div class="tree-icon">🌲</div>
    <h1>Task Tree Planner</h1>
    <p class="sub">Sign in to continue.</p>
    <div class="form-field">
      <label for="un">Username</label>
      <input type="text" id="un" autofocus placeholder="username"
             onkeydown="if(event.key==='Enter')document.getElementById('pw').focus()"/>
    </div>
    <div class="form-field">
      <label for="pw">Password</label>
      <input type="password" id="pw" placeholder="password"
             onkeydown="if(event.key==='Enter')login()"/>
    </div>
    <div class="error" id="err">Incorrect username or password.</div>
    <button onclick="login()">Sign in</button>
  </div>
  <script>
    async function login() {
      const un = document.getElementById('un').value.trim();
      const pw = document.getElementById('pw').value;
      const err = document.getElementById('err');
      if (!un || !pw) { err.classList.add('show'); return; }
      err.classList.remove('show');
      const resp = await fetch('/do-login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: un, password: pw })
      });
      if (resp.ok) {
        location.href = '/';
      } else {
        err.classList.add('show');
        document.getElementById('pw').value = '';
        document.getElementById('un').focus();
      }
    }
  </script>
</body>
</html>"""

_weather_cache = {}
CACHE_TTL = 3600

def fetch_url(url, timeout=8):
    req = Request(url, headers={"User-Agent": "TaskTreePlanner/1.0"})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def geolocate_by_ip():
    try:
        d = fetch_url("http://ip-api.com/json/?fields=lat,lon,city,country")
        if d.get("lat"):
            return {"lat": d["lat"], "lon": d["lon"],
                    "city": d.get("city",""), "country": d.get("country","")}
    except Exception as e:
        print(f"  [geo] IP lookup failed: {e}")
    return None

WMO_CODES = {
    0:("☀️","Clear sky"),1:("🌤️","Mainly clear"),2:("⛅","Partly cloudy"),
    3:("☁️","Overcast"),45:("🌫️","Fog"),48:("🌫️","Icy fog"),
    51:("🌦️","Light drizzle"),53:("🌦️","Drizzle"),55:("🌧️","Heavy drizzle"),
    61:("🌧️","Light rain"),63:("🌧️","Rain"),65:("🌧️","Heavy rain"),
    71:("🌨️","Light snow"),73:("❄️","Snow"),75:("❄️","Heavy snow"),
    77:("🌨️","Snow grains"),80:("🌦️","Light showers"),81:("🌧️","Showers"),
    82:("⛈️","Heavy showers"),85:("🌨️","Snow showers"),
    86:("❄️","Heavy snow showers"),95:("⛈️","Thunderstorm"),
    96:("⛈️","Thunderstorm + hail"),99:("⛈️","Thunderstorm + heavy hail"),
}

def fetch_weather(lat, lon):
    url = (f"https://api.open-meteo.com/v1/forecast"
           f"?latitude={lat}&longitude={lon}"
           f"&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum"
           f"&timezone=auto&forecast_days=14")
    raw=fetch_url(url); daily=raw.get("daily",{})
    dates=daily.get("time",[]); codes=daily.get("weathercode",[])
    temp_max=daily.get("temperature_2m_max",[]); temp_min=daily.get("temperature_2m_min",[])
    precip=daily.get("precipitation_sum",[])
    result=[]
    for i,date in enumerate(dates):
        code=codes[i] if i<len(codes) else 0
        emoji,label=WMO_CODES.get(code,("🌡️",f"Code {code}"))
        result.append({"date":date,"code":code,"emoji":emoji,"label":label,
            "temp_max":round(temp_max[i],1) if i<len(temp_max) and temp_max[i] is not None else None,
            "temp_min":round(temp_min[i],1) if i<len(temp_min) and temp_min[i] is not None else None,
            "precip":round(precip[i],1) if i<len(precip) and precip[i] is not None else None})
    return result

def load_weather_history():
    try:
        with open(WEATHER_HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_weather_history(history):
    with open(WEATHER_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)

def merge_forecast_into_history(forecast):
    history=load_weather_history(); today=time.strftime("%Y-%m-%d")
    for day in forecast:
        d=day["date"]
        if d<=today: history[d]={k:v for k,v in day.items() if k!="date"}
    save_weather_history(history)

def parse_multipart(data: bytes, boundary: bytes):
    parts={}
    for part in data.split(b"--"+boundary):
        if not part or part in (b"",b"--\r\n",b"--"): continue
        if b"\r\n\r\n" not in part: continue
        headers_raw,body=part.split(b"\r\n\r\n",1); body=body.rstrip(b"\r\n")
        headers=headers_raw.decode(errors="replace"); name=filename=None
        for line in headers.splitlines():
            if "Content-Disposition" in line:
                for token in line.split(";"):
                    token=token.strip()
                    if token.startswith('name="'): name=token[6:-1]
                    elif token.startswith('filename="'): filename=token[10:-1]
        if name: parts[name]=(filename,body)
    return parts


# The API is versioned: /api/v1/... is the stable contract for scripts and integrations.
# Within v1, endpoints and response fields may be added but never renamed or removed — a breaking
# change would go into /api/v2 while v1 keeps working. Unversioned /api/... paths are not served.
API_VERSION = "v1"
API_ENDPOINTS = [
    "GET    /api/v1                    this description",
    "GET    /api/v1/data               the full document",
    "PUT    /api/v1/data               replace everything (import)",
    "GET    /api/v1/changes?since=N&wait=S   changes since revision N (long-poll)",
    "POST   /api/v1/batch              atomic list of ops: create, update, move, delete, reserve_date",
    "GET    /api/v1/nodes              all nodes",
    "GET    /api/v1/nodes/{id}?depth=N one node with its subtree",
    "POST   /api/v1/nodes              create a node or nested subtree",
    "PATCH  /api/v1/nodes/{id}         update fields / type",
    "POST   /api/v1/nodes/{id}/move    reparent / reorder",
    "DELETE /api/v1/nodes/{id}         delete with descendants",
    "POST   /api/v1/images             upload an image",
    "GET    /api/v1/me | instance | weather | geolocate",
]
API_CHANGES_RE = re.compile(r"^/api/(v\d+/)?changes")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
MIME_TYPES = {".html": "text/html; charset=utf-8", ".png": "image/png", ".jpg": "image/jpeg",
              ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml"}


class Handler(BaseHTTPRequestHandler):
    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass

    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} {fmt % args}")

    def log_request(self, code="-", size="-"):
        # Long-poll requests arrive every ~25 s per open tab — don't flood the log with them.
        if API_CHANGES_RE.match(self.path) and str(code) == "200":
            return
        super().log_request(code, size)

    def _ip_allowed(self) -> bool:
        return is_allowed(self.client_address[0])

    def _identity(self):
        """Username (session) or token name (bearer) of the caller; None if unauthenticated.
        Returns "" when no users are configured (open access) and no credential was sent."""
        token_name = check_bearer(self.headers)
        if token_name:
            return token_name
        token = get_session_token(self.headers)
        if validate_session(token):
            return get_session_username(token)
        return "" if not has_any_user() else None

    def _authed(self) -> bool:
        return self._identity() is not None

    def _redirect_login(self):
        self.send_response(302)
        self.send_header("Location", "/login")
        self.end_headers()

    def _serve_login(self):
        self._send(200, LOGIN_HTML.encode(), "text/html; charset=utf-8")

    def _send(self, status, body: bytes, content_type, extra_headers=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra_headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, payload):
        body = payload.body if isinstance(payload, RawJson) else json.dumps(payload, ensure_ascii=False).encode()
        self._send(status, body, "application/json; charset=utf-8", [("Cache-Control", "no-store")])

    def _read_body(self):
        """Read the request body, or reply 413/400 and return None."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._json(400, {"ok": False, "error": "Bad Content-Length"}); return None
        if length > MAX_BODY:
            self._json(413, {"ok": False, "error": "Request body too large"}); return None
        return self.rfile.read(length) if length > 0 else b""

    # ── Method entry points ──────────────────────────────────────────────────

    def do_GET(self):
        if not self._ip_allowed():
            self.send_error(403, "Forbidden"); return
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path == "/login":
            self._serve_login(); return
        if not self._authed():
            if path.startswith("/api/"):
                self._json(401, {"ok": False, "error": "Unauthorized"})
            else:
                self._redirect_login()
            return
        if path.startswith("/api/"):
            self._handle_api("GET", path, parse_qs(parsed.query), b""); return
        self._serve_static(path)

    def do_POST(self):
        if not self._ip_allowed():
            self.send_error(403, "Forbidden"); return
        body = self._read_body()
        if body is None:
            return
        parsed = urlparse(self.path)
        if parsed.path == "/do-login":
            self._login(body); return
        if not self._authed():
            self._json(401, {"ok": False, "error": "Unauthorized"}); return
        if parsed.path == "/save":
            # The pre-API UI saved the whole document here. Tell stale tabs to reload.
            self._json(410, {"ok": False, "error": "This page is out of date — reload it"}); return
        if parsed.path.startswith("/api/"):
            self._handle_api("POST", parsed.path, parse_qs(parsed.query), body); return
        self.send_error(404)

    def _api_method(self, method):
        if not self._ip_allowed():
            self.send_error(403, "Forbidden"); return
        body = self._read_body()
        if body is None:
            return
        if not self._authed():
            self._json(401, {"ok": False, "error": "Unauthorized"}); return
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self.send_error(405); return
        self._handle_api(method, parsed.path, parse_qs(parsed.query), body)

    def do_PUT(self):    self._api_method("PUT")
    def do_PATCH(self):  self._api_method("PATCH")
    def do_DELETE(self): self._api_method("DELETE")

    # ── Static files ─────────────────────────────────────────────────────────

    def _serve_static(self, path):
        # Only the app and uploaded images are served — never data.json (use the API),
        # config.json (password/token hashes), server source, logs or the allowlist.
        if path in ("/", "/index.html", "/task_tree_planner.html"):
            filepath = os.path.join(BASE_DIR, "task_tree_planner.html")
        elif path.startswith("/images/"):
            filepath = os.path.normpath(os.path.join(IMAGES_DIR, path[len("/images/"):]))
            if not filepath.startswith(IMAGES_DIR + os.sep):
                self.send_error(403); return
        else:
            self.send_error(404, f"Not found: {path}"); return
        if not os.path.isfile(filepath):
            self.send_error(404, f"Not found: {path}"); return
        ext = os.path.splitext(filepath)[1].lower()
        with open(filepath, "rb") as f:
            body = f.read()
        headers = [("Cache-Control", "no-cache")] if ext == ".html" else []
        if ext == ".svg":
            headers.append(("Content-Security-Policy", "script-src 'none'"))
        self._send(200, body, MIME_TYPES.get(ext, "application/octet-stream"), headers)

    # ── Login ────────────────────────────────────────────────────────────────

    def _login(self, body):
        try:
            payload  = json.loads(body)
            username = payload.get("username", "")
            pw       = payload.get("password", "")
        except Exception:
            self._json(400, {"ok": False, "error": "Bad request"}); return

        if check_credentials(username, pw):
            token  = create_session(username.strip().lower())
            cookie = http.cookies.SimpleCookie()
            cookie[COOKIE_NAME] = token
            cookie[COOKIE_NAME]["httponly"] = True
            cookie[COOKIE_NAME]["samesite"] = "Strict"
            cookie[COOKIE_NAME]["max-age"]  = SESSION_TTL
            cookie[COOKIE_NAME]["path"]     = "/"
            self._send(200, b'{"ok":true}', "application/json",
                       [("Set-Cookie", cookie[COOKIE_NAME].OutputString())])
            print(f"  [auth] Login successful: '{username}' from {self.address_string()}")
        else:
            print(f"  [auth] Failed login for '{username}' from {self.address_string()}")
            self._json(401, {"ok": False, "error": "Incorrect username or password"})

    # ── API ──────────────────────────────────────────────────────────────────

    def _handle_api(self, method, path, qs, body):
        try:
            status, payload = self._route_api(method, path, qs, body)
        except ApiError as e:
            status, payload = e.status, {"ok": False, "error": e.error, **e.extra}
        except Exception as e:
            print(f"  [api] {method} {path} failed: {e!r}")
            status, payload = 500, {"ok": False, "error": "Internal error"}
        self._json(status, payload)

    def _route_api(self, method, path, qs, body):
        parts = [unquote(p) for p in path.strip("/").split("/")][1:]   # drop "api"
        if not parts or not re.fullmatch(r"v\d+", parts[0]):
            raise ApiError(404, f"The API is versioned - use /api/{API_VERSION}/...")
        if parts[0] != API_VERSION:
            raise ApiError(404, f"API version {parts[0]} is not supported - use /api/{API_VERSION}")
        parts = [p for p in parts[1:] if p != ""]
        route = parts[0] if parts else ""
        if not parts and method == "GET":
            return 200, {"ok": True, "app": "task-tree-planner", "version": API_VERSION,
                         "supported": [API_VERSION], "base": f"/api/{API_VERSION}",
                         "endpoints": API_ENDPOINTS}

        # Non-JSON / non-data endpoints
        if (method, route) == ("POST", "images") and len(parts) == 1:
            return self._upload_image(body)
        if method == "GET" and len(parts) == 1:
            if route == "instance":
                return 200, {"instance_name": INSTANCE_NAME}
            if route == "me":
                return 200, {"username": self._identity() or ""}
            if route == "geolocate":
                geo = geolocate_by_ip()
                return (200, {"ok": True, **geo}) if geo else (500, {"ok": False, "error": "Geolocation failed"})
            if route == "weather":
                return self._weather(qs)

        payload = {}
        if body:
            try:
                payload = json.loads(body)
            except ValueError:
                raise ApiError(400, "Body must be valid JSON")
            if not isinstance(payload, dict):
                raise ApiError(400, "Body must be a JSON object")
        if_revision = payload.pop("if_revision", None)
        if if_revision is None and "if_revision" in qs:
            if_revision = qs["if_revision"][0]
        if if_revision is not None:
            try:
                if_revision = int(if_revision)
            except (TypeError, ValueError):
                raise ApiError(400, "if_revision must be an integer")
        user = self._identity() or None

        def int_param(name, default):
            try:
                return int(qs[name][0]) if name in qs else default
            except ValueError:
                raise ApiError(400, f"{name} must be an integer")

        # Whole document
        if route == "data" and len(parts) == 1:
            if method == "GET":
                return 200, full_data()
            if method == "PUT":
                doc = prepare_import(payload)
                return 200, mutate(lambda d: op_replace(d, doc, user), if_revision, reset=True)

        # Incremental sync / long-poll:  GET /api/changes?since=N&wait=S
        if (method, route) == ("GET", "changes") and len(parts) == 1:
            if "since" not in qs:
                raise ApiError(400, "since is required")
            since = int_param("since", 0)
            wait = max(0, min(int_param("wait", 0), MAX_POLL_WAIT))
            return 200, wait_for_changes(since, wait)

        # Atomic list of operations:  POST /api/batch {ops: [...]}
        if (method, route) == ("POST", "batch") and len(parts) == 1:
            ops = payload.get("ops")
            return 200, mutate(lambda d: op_batch(d, ops, user), if_revision)

        # Nodes (REST)
        if route == "nodes" and len(parts) <= 3:
            node_id = parts[1] if len(parts) >= 2 else None
            action  = parts[2] if len(parts) == 3 else None

            if method == "GET" and not action:
                with DATA_LOCK:
                    d = current_data()
                    if node_id is None:
                        return 200, RawJson({"ok": True, "revision": d["revision"],
                                             "root_ids": d["settings"]["root_ids"], "nodes": d["nodes"]})
                    _require_node(d, node_id)
                    depth = int_param("depth", None)
                    return 200, RawJson({"ok": True, "revision": d["revision"],
                                         "node": subtree_view(d, node_id, depth)})
            if method == "POST" and node_id is None:
                return 201, mutate(lambda d: op_create(d, payload, user), if_revision)
            if method == "PATCH" and node_id and not action:
                args = {"id": node_id, "set": {k: v for k, v in payload.items() if k not in ("type", "cascade")}}
                for k in ("type", "cascade"):
                    if k in payload:
                        args[k] = payload[k]
                return 200, mutate(lambda d: op_update(d, args, user), if_revision)
            if method == "POST" and node_id and action == "move":
                return 200, mutate(lambda d: op_move(d, {**payload, "id": node_id}, user), if_revision)
            if method == "DELETE" and node_id and not action:
                return 200, mutate(lambda d: op_delete(d, {"id": node_id}, user), if_revision)

        raise ApiError(404, f"no API route for {method} {path}")

    def _upload_image(self, body):
        ct = self.headers.get("Content-Type", "")
        if "boundary=" not in ct:
            raise ApiError(400, "Missing boundary")
        parts = parse_multipart(body, ct.split("boundary=")[-1].strip().encode())
        if "file" not in parts:
            raise ApiError(400, "No file field")
        filename, file_data = parts["file"]
        filename = os.path.basename(filename or "")
        if not filename:
            raise ApiError(400, "No filename")
        if os.path.splitext(filename)[1].lower() not in IMAGE_EXTS:
            raise ApiError(400, f"Only image files are allowed ({', '.join(sorted(IMAGE_EXTS))})")
        os.makedirs(IMAGES_DIR, exist_ok=True)
        with open(os.path.join(IMAGES_DIR, filename), "wb") as f:
            f.write(file_data)
        return 200, {"ok": True, "path": f"images/{filename}"}

    def _weather(self, qs):
        global _weather_cache
        try:
            lat, lon = float(qs["lat"][0]), float(qs["lon"][0])
        except (KeyError, ValueError):
            raise ApiError(400, "lat/lon required")
        now = time.time()
        cache_hit = (
            _weather_cache.get("fetched_at", 0) + CACHE_TTL > now
            and abs(_weather_cache.get("lat", 0) - lat) < 0.1
            and abs(_weather_cache.get("lon", 0) - lon) < 0.1
        )
        if not cache_hit:
            try:
                forecast = fetch_weather(lat, lon)
                _weather_cache = {"fetched_at": now, "lat": lat, "lon": lon, "forecast": forecast}
                merge_forecast_into_history(forecast)
                print(f"  [weather] Fetched {len(forecast)} days for {lat:.2f},{lon:.2f}")
            except Exception as e:
                print(f"  [weather] Fetch failed: {e}")
                if "forecast" not in _weather_cache:
                    return 500, {"ok": False, "error": str(e)}
        return 200, {"ok": True, "forecast": _weather_cache["forecast"],
                     "history": load_weather_history(), "cached": cache_hit}


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--setup" in args or "--add-user" in args:
        setup_user(update=False)
        sys.exit(0)

    if "--update-user" in args:
        setup_user(update=True)
        sys.exit(0)

    if "--remove-user" in args:
        remove_user()
        sys.exit(0)

    if "--list-users" in args:
        list_users()
        sys.exit(0)

    for flag, fn in (("--add-token", add_api_token), ("--remove-token", remove_api_token)):
        if flag in args:
            i = args.index(flag)
            if i + 1 >= len(args):
                print(f"Usage: python server.py {flag} NAME"); sys.exit(1)
            fn(args[i + 1])
            sys.exit(0)

    if "--list-tokens" in args:
        names = sorted(get_api_tokens())
        print("\n".join(f"  • {n}" for n in names) if names else "No API tokens configured.")
        sys.exit(0)

    if not has_any_user():
        print("⚠  No users configured.")
        print("   Run `python server.py --setup` to add the first user.")
        print("   Starting anyway — all requests will be allowed.\n")

    os.makedirs(IMAGES_DIR, exist_ok=True)
    _allowlist = load_allowlist()
    if _allowlist:
        print(f"  [allowlist] Active — {len(_allowlist)} networks")
    else:
        print("  [allowlist] Disabled (no allowlist.txt found — all IPs allowed)")
    server = ThreadedHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Task Tree Planner [{INSTANCE_NAME}]  ->  http://0.0.0.0:{PORT}")
    print(f"  data.json   : {DATA_FILE}")
    print(f"  config.json : {CONFIG_FILE}")
    print(f"  images/     : {IMAGES_DIR}")
    print("Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
