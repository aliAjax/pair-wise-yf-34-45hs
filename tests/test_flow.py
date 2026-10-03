import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, DroneAirspaceService, iso, utcnow


class DroneFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = DroneAirspaceService(Path(self.tmp.name) / "test.db"); self.start = utcnow() + timedelta(hours=2)

    def tearDown(self): self.tmp.cleanup()

    def plan(self, callsign="D100", route=None, risk=1, altitude=100):
        return self.svc.create_plan("op-user", "operator", "OP1", {"callsign": callsign, "drone_model": "M400", "payload_kg": 5, "route": route or [[116.1, 39.8], [116.3, 39.9]], "starts_at": iso(self.start), "ends_at": iso(self.start + timedelta(hours=1)), "max_altitude": altitude, "population_risk": risk, "emergency_plan": "返回起降点", "region": "BJ"})

    def test_full_approval_change_and_offline_reconciliation(self):
        plan = self.plan(); submitted = self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})["plan"]
        check = self.svc.check_conflicts(plan["id"], "airspace_reviewer", "")
        self.assertTrue(check["approvable"])
        approved = self.svc.approve(plan["id"], "reviewer", "airspace_reviewer", {"expected_revision": submitted["revision"], "offline_id": "offline-1", "reason": "路线和应急方案满足要求"})
        self.assertEqual(approved["plan"]["status"], "approved")
        duplicate = self.svc.approve(plan["id"], "reviewer", "airspace_reviewer", {"expected_revision": submitted["revision"], "offline_id": "offline-1", "reason": "补传"})
        self.assertTrue(duplicate["idempotent"])
        changed = self.svc.change(plan["id"], "op-user", "operator", "OP1", {"expected_revision": submitted["revision"], "route": [[116.12, 39.82], [116.32, 39.92]]})
        self.assertEqual(changed["status"], "draft"); self.assertEqual(changed["revision"], 2)
        notifications = self.svc.notifications("op-user", "operator", "OP1")["notifications"]
        self.assertEqual(notifications[0]["kind"], "approval_invalidated")

    def test_restriction_emergency_override_and_conflicts(self):
        self.svc.create_restriction("reviewer", "airspace_reviewer", {"name": "临时禁飞", "kind": "no_fly", "min_lon": 116.0, "min_lat": 39.7, "max_lon": 116.2, "max_lat": 40.0, "min_altitude": 0, "max_altitude": 150, "starts_at": iso(self.start - timedelta(minutes=30)), "ends_at": iso(self.start + timedelta(hours=2)), "reason": "活动"})
        plan = self.plan("D101"); self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})
        check = self.svc.check_conflicts(plan["id"], "airspace_reviewer", "")
        self.assertFalse(check["approvable"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.approve(plan["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1, "offline_id": "offline-2", "reason": "常规审核"})
        self.assertEqual(ctx.exception.code, "airspace_conflict")
        override = self.svc.approve(plan["id"], "commander", "commander", {"expected_revision": 1, "offline_id": "offline-3", "reason": "紧急任务", "override_reason": "应急救援授权"})
        self.assertEqual(override["plan"]["status"], "approved")
        conflicting = self.plan("D102", route=[[116.11, 39.81], [116.15, 39.84]])
        self.svc.submit(conflicting["id"], "op-user", "operator", "OP1", {})
        with self.assertRaises(ApiError) as ctx:
            self.svc.approve(conflicting["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1, "offline_id": "offline-4", "reason": "复核"})
        self.assertIn(ctx.exception.code, {"hard_constraint_violation", "airspace_conflict"})
        with self.assertRaises(ApiError) as ctx:
            self.svc.approve(conflicting["id"], "reviewer", "airspace_reviewer", {"expected_revision": 99, "offline_id": "offline-5", "reason": "过期审核"})
        self.assertEqual(ctx.exception.code, "revision_conflict")


if __name__ == "__main__": unittest.main()


class OfflineReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = DroneAirspaceService(Path(self.tmp.name) / "test.db")
        self.start = utcnow() + timedelta(hours=2)

    def tearDown(self): self.tmp.cleanup()

    def plan(self, callsign="D200", route=None):
        return self.svc.create_plan("op-user", "operator", "OP1", {"callsign": callsign, "drone_model": "M400", "payload_kg": 5,
            "route": route or [[116.1, 39.8], [116.3, 39.9]], "starts_at": iso(self.start),
            "ends_at": iso(self.start + timedelta(hours=1)), "max_altitude": 100, "population_risk": 1,
            "emergency_plan": "返回起降点", "region": "BJ"})

    def submit(self, p): return self.svc.submit(p["id"], "op-user", "operator", "OP1", {})["plan"]

    def approve(self, p, offline_id, rv, expected=1, reviewer="reviewer", role="airspace_reviewer", **kw):
        return self.svc.approve(p["id"], reviewer, role, {"expected_revision": expected, "restriction_version": rv,
            "offline_id": offline_id, "reason": "满足要求", **kw})

    def restriction(self, name="临时限制"):
        return self.svc.create_restriction("reviewer", "airspace_reviewer", {"name": name, "kind": "temporary_limit",
            "min_lon": 115.0, "min_lat": 39.0, "max_lon": 115.5, "max_lat": 39.5, "min_altitude": 0, "max_altitude": 150,
            "starts_at": iso(self.start - timedelta(minutes=30)), "ends_at": iso(self.start + timedelta(hours=2)), "reason": "活动"})

    def approval_row(self, offline_id):
        return self.svc.repo.conn.execute("SELECT * FROM approvals WHERE offline_id=?", (offline_id,)).fetchone()

    def test_stale_restriction_version_rejected_and_persisted(self):
        p = self.plan(); self.submit(p)
        self.assertEqual(self.svc._restriction_version(self.svc.repo.conn), 1)
        with self.assertRaises(ApiError) as ctx:
            self.approve(p, "off-stale", rv=99)
        self.assertEqual(ctx.exception.code, "restriction_version_conflict")
        row = self.approval_row("off-stale")
        self.assertIsNotNone(row); self.assertEqual(row["status"], "invalidated")
        self.assertEqual(row["restriction_version"], 99)
        self.assertEqual(self.svc.get_plan(p["id"], "airspace_reviewer", "")["status"], "submitted")
        # retry same offline_id: no duplicate write, still rejected
        with self.assertRaises(ApiError) as ctx2:
            self.approve(p, "off-stale", rv=99)
        self.assertEqual(ctx2.exception.code, "decision_invalidated")
        self.assertEqual(self.svc.repo.conn.execute("SELECT COUNT(*) FROM approvals WHERE offline_id='off-stale'").fetchone()[0], 1)

    def test_matching_versions_apply_and_retry_is_idempotent(self):
        p = self.plan(); self.submit(p)
        out = self.approve(p, "off-ok", rv=1)
        self.assertEqual(out["plan"]["status"], "approved"); self.assertFalse(out["idempotent"])
        again = self.approve(p, "off-ok", rv=1)
        self.assertTrue(again["idempotent"])
        self.assertEqual(self.svc.repo.conn.execute("SELECT COUNT(*) FROM approvals WHERE offline_id='off-ok'").fetchone()[0], 1)

    def test_two_reviewers_same_version_first_wins_second_pending_review(self):
        p = self.plan(); self.submit(p)
        first = self.approve(p, "off-a", rv=1, reviewer="rA")
        self.assertEqual(first["plan"]["status"], "approved")
        second = self.approve(p, "off-b", rv=1, reviewer="rB")
        self.assertTrue(second["pending_review"]); self.assertFalse(second["idempotent"])
        self.assertEqual(second["conflicts"][0]["code"], "already_decided")
        self.assertEqual(second["conflicts"][0]["decided_by"], "rA")
        # plan stays approved (first wins)
        self.assertEqual(self.svc.get_plan(p["id"], "airspace_reviewer", "")["status"], "approved")
        # pending review is listed and can be resolved
        listing = self.svc.pending_reviews("commander")
        self.assertEqual(len(listing["pending_reviews"]), 1)
        self.assertEqual(listing["pending_reviews"][0]["offline_id"], "off-b")
        resolved = self.svc.resolve_review(second["approval_id"], "cmd", "commander", {"resolution": "apply", "reason": "复核后同意"})
        self.assertEqual(resolved["status"], "applied")
        self.assertEqual(self.approval_row("off-a")["status"], "superseded")

    def test_plan_change_invalidates_decision(self):
        p = self.plan(); self.submit(p)
        self.approve(p, "off-p", rv=1)
        changed = self.svc.change(p["id"], "op-user", "operator", "OP1", {"expected_revision": 1, "route": [[117.5, 39.2], [117.6, 39.3]]})
        self.assertEqual(changed["revision"], 2); self.assertEqual(changed["status"], "draft")
        self.assertEqual(self.approval_row("off-p")["status"], "invalidated")
        # re-approve against the new revision applies
        self.submit(changed)
        again = self.approve(changed, "off-p2", rv=1, expected=2)
        self.assertEqual(again["plan"]["status"], "approved")

    def test_restriction_change_invalidates_decisions_and_requires_reapproval(self):
        p = self.plan(); self.submit(p)
        self.approve(p, "off-r", rv=1)
        self.assertEqual(self.svc.get_plan(p["id"], "airspace_reviewer", "")["status"], "approved")
        self.restriction()
        self.assertEqual(self.svc._restriction_version(self.svc.repo.conn), 2)
        self.assertEqual(self.approval_row("off-r")["status"], "invalidated")
        self.assertEqual(self.svc.get_plan(p["id"], "airspace_reviewer", "")["status"], "submitted")
        # a decision based on the old restriction version is rejected
        with self.assertRaises(ApiError) as ctx:
            self.approve(p, "off-r2", rv=1)
        self.assertEqual(ctx.exception.code, "restriction_version_conflict")
        # re-approve with the current restriction version applies (commander override)
        out = self.approve(p, "off-r3", rv=2, reviewer="commander", role="commander", override_reason="应急救援授权")
        self.assertEqual(out["plan"]["status"], "approved")

    def test_reject_also_reconciles_versions(self):
        p = self.plan(); self.submit(p)
        rj = self.svc.reject(p["id"], "r1", "airspace_reviewer", {"expected_revision": 1, "restriction_version": 1, "offline_id": "off-j1", "reason": "拒绝"})
        self.assertEqual(rj["plan"]["status"], "rejected")
        # second reject for the same version -> pending review, not applied
        rj2 = self.svc.reject(p["id"], "r2", "airspace_reviewer", {"expected_revision": 1, "restriction_version": 1, "offline_id": "off-j2", "reason": "我也拒绝"})
        self.assertTrue(rj2["pending_review"])
        self.assertEqual(rj2["conflicts"][0]["code"], "already_decided")


if __name__ == "__main__": unittest.main()
