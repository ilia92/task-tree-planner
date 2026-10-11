"""End-to-end tests for the /api/nodes write API and /save revision checks.

Run:  python3 -m unittest discover tests -v
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
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
        server._cache.update(data=None, stamp=None)
        server._change_log.clear()
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
        with urllib.request.urlopen(urllib.request.Request(self.base + "/", headers=auth)) as r:
            self.assertEqual(r.status, 200)
        for blocked in ("/data.json", "/config.json", "/server.py", "/README.md"):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(urllib.request.Request(self.base + blocked, headers=auth))
            self.assertEqual(cm.exception.code, 404)

    def test_legacy_save_gone(self):
        self.assertEqual(self.call("POST", "/save", self.stored())[0], 410)

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

    # ── batch ─────────────────────────────────────────────────────────────────
    def test_batch_atomic(self):
        status, res = self.call("POST", "/api/batch", {"ops": [
            {"op": "update", "id": "task_1", "set": {"name": "A"}},
            {"op": "move", "id": "task_2", "parent_id": "project_1", "position": 0},
            {"op": "reserve_date", "date": "2026-12-24", "reserved": True},
            {"op": "create", "parent_id": "task_1", "node": {"name": "Sub"}},
        ]})
        self.assertEqual(status, 200, res)
        d = self.stored()
        self.assertEqual(d["nodes"]["task_1"]["name"], "A")
        self.assertEqual(d["nodes"]["project_1"]["children"][0], "task_2")
        self.assertIn("2026-12-24", d["settings"]["reserved_dates"])
        new_id = res["results"][3]["id"]
        self.assertEqual(d["nodes"][new_id]["parent_id"], "task_1")
        # write responses carry the change set
        self.assertEqual(res["since"] + 1, res["revision"])
        self.assertEqual(set(res["changes"]["nodes"]), {"task_1", "project_1", new_id})
        self.assertIn("settings", res["changes"])

        before = self.stored()
        status, res = self.call("POST", "/api/batch", {"ops": [
            {"op": "update", "id": "task_1", "set": {"name": "B"}},
            {"op": "delete", "id": "nope"},
        ]})
        self.assertEqual(status, 404)
        self.assertIn("ops[1]", res["error"])
        self.assertEqual(self.stored(), before)
        # the in-memory copy was not left half-modified either
        self.assertEqual(self.call("GET", "/api/data")[1]["nodes"]["task_1"]["name"], "A")

    def test_reserve_date_unreserve(self):
        self.call("POST", "/api/batch", {"ops": [{"op": "reserve_date", "date": "2026-12-24", "reserved": True}]})
        self.call("POST", "/api/batch", {"ops": [{"op": "reserve_date", "date": "2026-12-24", "reserved": False}]})
        self.assertNotIn("2026-12-24", self.stored()["settings"]["reserved_dates"])
        self.assertEqual(self.call("POST", "/api/batch", {"ops": [
            {"op": "reserve_date", "date": "bad", "reserved": True}]})[0], 400)

    # ── sync ──────────────────────────────────────────────────────────────────
    def test_changes_incremental(self):
        rev = self.call("GET", "/api/data")[1]["revision"]
        self.call("PATCH", "/api/nodes/task_1", {"name": "X"})
        self.call("DELETE", "/api/nodes/task_2")
        status, res = self.call("GET", f"/api/changes?since={rev}")
        self.assertEqual(status, 200)
        self.assertFalse(res.get("reset"))
        self.assertEqual((res["since"], res["revision"]), (rev, rev + 2))
        self.assertEqual(res["changes"]["nodes"]["task_1"]["name"], "X")
        self.assertEqual(set(res["changes"]["deleted"]), {"task_2", "subtask_1"})
        self.assertIn("project_1", res["changes"]["nodes"])
        # up to date → empty
        res = self.call("GET", f"/api/changes?since={rev + 2}")[1]
        self.assertEqual(res["changes"], {"nodes": {}, "deleted": []})

    def test_changes_reset_when_gap_unknown(self):
        res = self.call("GET", "/api/changes?since=999")[1]
        self.assertTrue(res["reset"])
        self.assertIn("task_1", res["data"]["nodes"])

    def test_long_poll_wakes_on_write(self):
        rev = self.call("GET", "/api/data")[1]["revision"]
        threading.Timer(0.3, lambda: self.call("PATCH", "/api/nodes/task_1", {"name": "live"})).start()
        t0 = time.monotonic()
        res = self.call("GET", f"/api/changes?since={rev}&wait=10")[1]
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(res["changes"]["nodes"]["task_1"]["name"], "live")

    def test_long_poll_times_out(self):
        rev = self.call("GET", "/api/data")[1]["revision"]
        t0 = time.monotonic()
        res = self.call("GET", f"/api/changes?since={rev}&wait=1")[1]
        self.assertGreaterEqual(time.monotonic() - t0, 0.9)
        self.assertEqual(res["revision"], rev)

    def test_external_edit_detected(self):
        rev = self.call("GET", "/api/data")[1]["revision"]
        doc = self.stored()
        doc["nodes"]["task_1"]["name"] = "edited by hand"
        with open(server.DATA_FILE, "w") as f:
            json.dump(doc, f)
        res = self.call("GET", f"/api/changes?since={rev}")[1]
        self.assertTrue(res["reset"])
        self.assertGreater(res["revision"], rev)
        self.assertEqual(res["data"]["nodes"]["task_1"]["name"], "edited by hand")

    # ── import ────────────────────────────────────────────────────────────────
    def test_import_replaces_and_repairs(self):
        rev = self.call("GET", "/api/data")[1]["revision"]
        doc = {"settings": {"root_ids": ["s1", "ghost"]}, "nodes": {
            "s1": {"type": "space", "name": "S", "parent_id": None, "children": ["t1", "ghost"],
                   "priority": "high", "relates_to": ["ghost"]},
            "t1": {"type": "task", "name": "T", "parent_id": "s1"}}}
        status, res = self.call("PUT", "/api/data", doc)
        self.assertEqual(status, 200, res)
        self.assertTrue(res["reset"])
        d = self.stored()
        self.assertEqual(d["settings"]["root_ids"], ["s1"])
        self.assertEqual(d["nodes"]["s1"]["children"], ["t1"])
        self.assertEqual(d["nodes"]["s1"]["priority"], 7)
        self.assertEqual(d["nodes"]["s1"]["relates_to"], [])
        self.assertEqual(d["revision"], rev + 1)
        self.assertTrue(self.call("GET", f"/api/changes?since={rev}")[1]["reset"])
        self.assertEqual(self.call("PUT", "/api/data", {"nodes": []})[0], 400)

    # ── misc ──────────────────────────────────────────────────────────────────
    def test_image_upload_rejects_non_images(self):
        boundary = "XyZ"
        def upload(name):
            body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
                    f"Content-Type: application/octet-stream\r\n\r\nDATA\r\n--{boundary}--\r\n").encode()
            req = urllib.request.Request(self.base + "/api/images", data=body, method="POST")
            req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
            req.add_header("Authorization", f"Bearer {TOKEN}")
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code
        self.assertEqual(upload("evil.html"), 400)
        self.assertEqual(upload("pic.png"), 200)
        self.assertTrue(os.path.isfile(os.path.join(server.IMAGES_DIR, "pic.png")))


if __name__ == "__main__":
    unittest.main()
