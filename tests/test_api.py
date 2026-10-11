"""End-to-end tests for the /api/nodes write API and /save revision checks.

Run:  python3 -m unittest discover tests -v
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402

TOKEN = "test-token-123"
EXAMPLE = os.path.join(os.path.dirname(server.__file__), "example", "data.json")


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        server.DATA_FILE = os.path.join(self.tmp, "data.json")
        server.CONFIG_FILE = os.path.join(self.tmp, "config.json")
        server.IMAGES_DIR = os.path.join(self.tmp, "images")
        shutil.copy(EXAMPLE, server.DATA_FILE)
        with open(server.CONFIG_FILE, "w") as f:
            json.dump({"users": {"alice": server.hash_password("pw")},
                       "api_tokens": {"bot": server.hash_password(TOKEN)}}, f)
        self.httpd = server.ThreadedHTTPServer(("127.0.0.1", 0), server.Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp)

    def call(self, method, path, body=None, token=TOKEN):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def stored(self):
        with open(server.DATA_FILE) as f:
            return json.load(f)

    # ── auth ──────────────────────────────────────────────────────────────────
    def test_requires_auth(self):
        self.assertEqual(self.call("GET", "/api/nodes", token=None)[0], 401)
        self.assertEqual(self.call("GET", "/api/nodes", token="wrong")[0], 401)
        self.assertEqual(self.call("DELETE", "/api/nodes/task_1", token=None)[0], 401)
        self.assertEqual(self.call("GET", "/api/nodes")[0], 200)

    def test_static_allowlist(self):
        auth = {"Authorization": f"Bearer {TOKEN}"}
        for ok in ("/", "/data.json"):
            with urllib.request.urlopen(urllib.request.Request(self.base + ok, headers=auth)) as r:
                self.assertEqual(r.status, 200)
        for blocked in ("/config.json", "/server.py", "/README.md"):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(urllib.request.Request(self.base + blocked, headers=auth))
            self.assertEqual(cm.exception.code, 404)

    # ── create ────────────────────────────────────────────────────────────────
    def test_nested_create(self):
        status, res = self.call("POST", "/api/nodes", {
            "parent_id": "project_1", "position": 0,
            "node": {"name": "Launch", "priority": 7, "ref": "launch", "children": [
                {"name": "Write copy", "children": [{"name": "Draft", "relates_to": ["launch"]}]},
                {"name": "Review", "target_date": "2026-11-01", "status": "done"},
            ]}})
        self.assertEqual(status, 201, res)
        d = self.stored()
        root = d["nodes"][res["id"]]
        self.assertEqual(root["type"], "task")
        self.assertEqual(d["nodes"]["project_1"]["children"][0], res["id"])
        copy_id, review_id = root["children"]
        self.assertEqual(d["nodes"][copy_id]["type"], "subtask")
        draft = d["nodes"][res["ids"]["0.0.0"]]
        self.assertEqual(draft["type"], "subtask")
        self.assertEqual(draft["parent_id"], copy_id)
        self.assertEqual(draft["relates_to"], [res["id"]])
        review = d["nodes"][review_id]
        self.assertEqual(review["completed_date"], "2026-11-01")
        self.assertEqual(review["completed_by"], "bot")
        self.assertEqual(d["revision"], res["revision"])

    def test_create_top_level_is_space(self):
        status, res = self.call("POST", "/api/nodes", {"parent_id": None, "node": {"name": "New"}})
        self.assertEqual(status, 201)
        d = self.stored()
        self.assertEqual(d["nodes"][res["id"]]["type"], "space")
        self.assertEqual(d["settings"]["root_ids"][-1], res["id"])

    def test_create_is_atomic(self):
        before = self.stored()
        status, res = self.call("POST", "/api/nodes", {"parent_id": "project_1", "node": {
            "name": "Ok", "children": [{"name": "Fine"}, {"name": "Bad", "priority": 99}]}})
        self.assertEqual(status, 400)
        self.assertIn("priority", res["error"])
        self.assertEqual(self.stored(), before)

    def test_create_rejects_type_above_level(self):
        status, _ = self.call("POST", "/api/nodes",
                              {"parent_id": "project_1", "node": {"name": "X", "type": "space"}})
        self.assertEqual(status, 400)

    def test_create_unknown_parent(self):
        self.assertEqual(self.call("POST", "/api/nodes", {"parent_id": "nope", "node": {"name": "X"}})[0], 404)

    # ── read ──────────────────────────────────────────────────────────────────
    def test_get_subtree_depth(self):
        status, res = self.call("GET", "/api/nodes/project_1?depth=1")
        self.assertEqual(status, 200)
        children = res["node"]["children"]
        self.assertEqual([c["id"] for c in children], ["task_1", "task_2"])
        self.assertTrue(children[1]["children_truncated"])

    # ── patch ─────────────────────────────────────────────────────────────────
    def test_patch_and_status(self):
        status, res = self.call("PATCH", "/api/nodes/task_1", {"name": " Renamed ", "status": "done"})
        self.assertEqual(status, 200, res)
        node = self.stored()["nodes"]["task_1"]
        self.assertEqual(node["name"], "Renamed")
        self.assertEqual(node["completed_date"], node["target_date"])
        self.call("PATCH", "/api/nodes/task_1", {"status": "open"})
        self.assertIsNone(self.stored()["nodes"]["task_1"]["completed_date"])

    def test_patch_validation(self):
        self.assertEqual(self.call("PATCH", "/api/nodes/task_1", {"parent_id": "x"})[0], 400)
        self.assertEqual(self.call("PATCH", "/api/nodes/task_1", {"target_date": "2026-02-30"})[0], 400)
        self.assertEqual(self.call("PATCH", "/api/nodes/task_1", {"relates_to": ["nope"]})[0], 400)
        self.assertEqual(self.call("PATCH", "/api/nodes/nope", {"name": "x"})[0], 404)

    def test_patch_type_cascades(self):
        self.call("PATCH", "/api/nodes/task_2", {"type": "project"})
        d = self.stored()
        self.assertEqual(d["nodes"]["task_2"]["type"], "project")
        self.assertEqual(d["nodes"]["subtask_1"]["type"], "task")

    # ── move ──────────────────────────────────────────────────────────────────
    def test_move(self):
        status, _ = self.call("POST", "/api/nodes/task_2/move", {"parent_id": "project_1", "position": 0})
        self.assertEqual(status, 200)
        self.assertEqual(self.stored()["nodes"]["project_1"]["children"][:2], ["task_2", "task_1"])
        status, _ = self.call("POST", "/api/nodes/task_1/move", {"parent_id": None})
        d = self.stored()
        self.assertIsNone(d["nodes"]["task_1"]["parent_id"])
        self.assertIn("task_1", d["settings"]["root_ids"])
        self.assertNotIn("task_1", d["nodes"]["project_1"]["children"])

    def test_move_rejects_cycle(self):
        self.assertEqual(self.call("POST", "/api/nodes/task_2/move", {"parent_id": "subtask_1"})[0], 400)
        self.assertEqual(self.call("POST", "/api/nodes/task_2/move", {"parent_id": "task_2"})[0], 400)

    # ── delete ────────────────────────────────────────────────────────────────
    def test_delete_cascades(self):
        self.call("PATCH", "/api/nodes/task_1", {"relates_to": ["subtask_1"]})
        status, res = self.call("DELETE", "/api/nodes/task_2")
        self.assertEqual(status, 200)
        self.assertEqual(set(res["deleted"]), {"task_2", "subtask_1"})
        d = self.stored()
        self.assertNotIn("subtask_1", d["nodes"])
        self.assertNotIn("task_2", d["nodes"]["project_1"]["children"])
        self.assertEqual(d["nodes"]["task_1"]["relates_to"], [])

    # ── concurrency ───────────────────────────────────────────────────────────
    def test_if_revision(self):
        rev = self.call("GET", "/api/nodes")[1]["revision"]
        self.assertEqual(self.call("PATCH", "/api/nodes/task_1", {"name": "a", "if_revision": rev})[0], 200)
        status, res = self.call("PATCH", "/api/nodes/task_1", {"name": "b", "if_revision": rev})
        self.assertEqual(status, 409)
        self.assertEqual(res["revision"], rev + 1)

    def test_stale_save_rejected(self):
        doc = self.stored()
        doc["revision"] = 0
        status, res = self.call("POST", "/save", doc)
        self.assertEqual((status, res["revision"]), (200, 1))
        self.call("PATCH", "/api/nodes/task_1", {"name": "from api"})   # revision → 2
        doc["revision"] = 1
        self.assertEqual(self.call("POST", "/save", doc)[0], 409)
        self.assertEqual(self.stored()["nodes"]["task_1"]["name"], "from api")

    def test_legacy_save_without_revision(self):
        doc = self.stored()
        doc.pop("revision", None)
        status, res = self.call("POST", "/save", doc)
        self.assertEqual(status, 200)
        self.assertEqual(self.stored()["revision"], res["revision"])


if __name__ == "__main__":
    unittest.main()
