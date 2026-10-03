import threading
import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, DroneAirspaceService, iso, utcnow


class OfflineMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = DroneAirspaceService(Path(self.tmp.name) / "test.db"); self.start = utcnow() + timedelta(hours=2)

    def tearDown(self): self.tmp.cleanup()

    def plan(self, callsign="D200", route=None, risk=1, altitude=100):
        return self.svc.create_plan("op-user", "operator", "OP1", {"callsign": callsign, "drone_model": "M400", "payload_kg": 5, "route": route or [[116.1, 39.8], [116.3, 39.9]], "starts_at": iso(self.start), "ends_at": iso(self.start + timedelta(hours=1)), "max_altitude": altitude, "population_risk": risk, "emergency_plan": "返回起降点", "region": "BJ"})

    def restriction(self, name="临时禁飞", lon=116.0, lat=39.7, lon2=116.4, lat2=40.0):
        return self.svc.create_restriction("reviewer", "airspace_reviewer", {"name": name, "kind": "no_fly", "min_lon": lon, "min_lat": lat, "max_lon": lon2, "max_lat": lat2, "min_altitude": 0, "max_altitude": 150, "starts_at": iso(self.start - timedelta(minutes=30)), "ends_at": iso(self.start + timedelta(hours=2)), "reason": "活动"})

    def submit(self, callsign="D200", **kwargs):
        plan = self.plan(callsign, **kwargs); self.svc.submit(plan["id"], "op-user", "operator", "OP1", {}); return plan

    # --- 三维合并：计划版本 + 限制集合版本 + 离线决定 ---
    def test_conflict_report_carries_restrictions_version(self):
        plan = self.submit()
        check = self.svc.check_conflicts(plan["id"], "airspace_reviewer", "")
        self.assertEqual(check["restrictions_version"], 1)
        self.assertTrue(check["approvable"])

    def test_stale_restrictions_version_offline_approval_goes_pending(self):
        plan = self.submit()
        # 审核员在限制版本 1 下离线做了批准；回传前限制集合更新到版本 2
        created = self.restriction()
        self.assertEqual(created["restrictions_version"], 2)
        result = self.svc.approve(plan["id"], "reviewer", "airspace_reviewer",
                                  {"expected_revision": 1, "expected_restrictions_version": 1, "offline_id": "off-1", "reason": "离线批准"})
        self.assertEqual(result["decision_state"], "pending_review")
        self.assertFalse(result["idempotent"])
        # 计划状态不能被陈旧决定改成 approved
        self.assertEqual(result["plan"]["status"], "submitted")
        codes = {c["code"] for c in result["conflicts"]}
        self.assertIn("restrictions_version_stale", codes)
        stale = next(c for c in result["conflicts"] if c["code"] == "restrictions_version_stale")
        self.assertEqual(stale["expected_restrictions_version"], 1)
        self.assertEqual(stale["current_restrictions_version"], 2)
        # 当前真实空域冲突一并列出
        self.assertTrue(any(c["code"] == "airspace_restriction" for c in stale["blocking_conflicts"]))
        pending = self.svc.pending_reviews("airspace_reviewer", "")
        self.assertEqual(pending["count"], 1)
        self.assertEqual(pending["pending_reviews"][0]["offline_id"], "off-1")

    def test_matching_versions_approves_effectively(self):
        plan = self.submit()
        self.restriction(lon=100.0, lat=10.0, lon2=101.0, lat2=11.0)  # 版本变 2 但与计划不相交
        check = self.svc.check_conflicts(plan["id"], "airspace_reviewer", "")
        result = self.svc.approve(plan["id"], "reviewer", "airspace_reviewer",
                                  {"expected_revision": 1, "expected_restrictions_version": check["restrictions_version"],
                                   "offline_id": "off-2", "reason": "版本一致"})
        self.assertEqual(result["decision_state"], "effective")
        self.assertEqual(result["plan"]["status"], "approved")

    # --- 同版本并发：先入库者赢，后到者转待复核 ---
    def test_concurrent_same_version_first_writer_wins(self):
        plan = self.submit()
        outcomes: list[dict] = []
        barrier = threading.Barrier(2)

        def reviewer(user, offline_id, bucket):
            barrier.wait()
            bucket.append(self.svc.approve(plan["id"], user, "airspace_reviewer",
                                           {"expected_revision": 1, "expected_restrictions_version": 1,
                                            "offline_id": offline_id, "reason": f"并发-{offline_id}"}))

        t1 = threading.Thread(target=reviewer, args=("rev-a", "off-a", outcomes))
        t2 = threading.Thread(target=reviewer, args=("rev-b", "off-b", outcomes))
        t1.start(); t2.start(); t1.join(); t2.join()
        states = sorted(o["decision_state"] for o in outcomes)
        self.assertEqual(states, ["effective", "pending_review"])
        winner = next(o for o in outcomes if o["decision_state"] == "effective")
        loser = next(o for o in outcomes if o["decision_state"] == "pending_review")
        conflict = loser["conflicts"][0]
        self.assertEqual(conflict["code"], "concurrent_decision")
        self.assertEqual(conflict["existing_approval_id"], winner["approval_id"])
        self.assertEqual(conflict["existing_reviewer"], winner["plan"]["approvals"][0]["reviewer"])
        self.assertEqual(winner["plan"]["status"], "approved")
        # 待复核列表里能看到后到决定及其冲突
        pending = self.svc.pending_reviews("airspace_reviewer", "")["pending_reviews"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], loser["approval_id"])

    def test_concurrent_reject_second_goes_pending(self):
        plan = self.submit()
        first = self.svc.reject(plan["id"], "rev-a", "airspace_reviewer",
                                {"expected_revision": 1, "offline_id": "off-r1", "reason": "先到拒绝"})
        self.assertEqual(first["decision_state"], "effective")
        second = self.svc.reject(plan["id"], "rev-b", "airspace_reviewer",
                                 {"expected_revision": 1, "offline_id": "off-r2", "reason": "后到拒绝"})
        self.assertEqual(second["decision_state"], "pending_review")
        self.assertEqual(second["conflicts"][0]["code"], "concurrent_decision")
        self.assertEqual(second["conflicts"][0]["existing_decision"], "rejected")

    # --- 计划或限制变化，原决定立即失效 ---
    def test_plan_change_supersedes_effective_approval(self):
        plan = self.submit()
        self.svc.approve(plan["id"], "rev", "airspace_reviewer",
                         {"expected_revision": 1, "offline_id": "off-chg", "reason": "批准"})
        changed = self.svc.change(plan["id"], "op-user", "operator", "OP1",
                                  {"expected_revision": 1, "route": [[116.12, 39.82], [116.32, 39.92]]})
        self.assertEqual(changed["revision"], 2); self.assertEqual(changed["status"], "draft")
        detail = self.svc.get_plan(plan["id"], "airspace_reviewer", "")
        self.assertEqual(detail["approvals"][0]["state"], "superseded")
        self.assertEqual(detail["approvals"][0]["conflicts"][0]["code"], "plan_revision_changed")
        # 旧离线决定重传不能把新版本批掉
        with self.assertRaises(ApiError) as ctx:
            self.svc.approve(plan["id"], "rev", "airspace_reviewer",
                             {"expected_revision": 1, "offline_id": "off-chg-retry", "reason": "旧决定重传"})
        self.assertEqual(ctx.exception.code, "revision_conflict")

    def test_new_restriction_supersedes_overlapping_approval(self):
        plan = self.submit()
        approved = self.svc.approve(plan["id"], "rev", "airspace_reviewer",
                                    {"expected_revision": 1, "offline_id": "off-rv", "reason": "批准"})
        self.assertEqual(approved["plan"]["status"], "approved")
        created = self.restriction()
        self.assertEqual(created["invalidated_plans"], [{"plan_id": plan["id"], "callsign": "D200"}])
        detail = self.svc.get_plan(plan["id"], "auditor", "")
        self.assertEqual(detail["status"], "draft")
        self.assertEqual(detail["approvals"][0]["state"], "superseded")
        self.assertEqual(detail["approvals"][0]["conflicts"][0]["code"], "restriction_changed")
        # 重新提交后，旧离线决定（基于限制版本 1）仍只能进待复核
        self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})
        replay = self.svc.approve(plan["id"], "rev", "airspace_reviewer",
                                  {"expected_revision": 1, "expected_restrictions_version": 1,
                                   "offline_id": "off-rv-old", "reason": "拿旧依据重传"})
        self.assertEqual(replay["decision_state"], "pending_review")

    def test_unrelated_restriction_does_not_invalidate(self):
        plan = self.submit()
        self.svc.approve(plan["id"], "rev", "airspace_reviewer",
                         {"expected_revision": 1, "offline_id": "off-ok", "reason": "批准"})
        created = self.restriction(lon=100.0, lat=10.0, lon2=101.0, lat2=11.0)
        self.assertEqual(created["restrictions_version"], 2)
        self.assertEqual(created["invalidated_plans"], [])
        detail = self.svc.get_plan(plan["id"], "airspace_reviewer", "")
        self.assertEqual(detail["status"], "approved")
        self.assertEqual(detail["approvals"][0]["state"], "effective")

    # --- 回传失败按离线编号重试：已有结果绝不重复写入 ---
    def test_offline_id_retry_is_idempotent_for_each_state(self):
        plan = self.submit()
        first = self.svc.approve(plan["id"], "rev", "airspace_reviewer",
                                 {"expected_revision": 1, "offline_id": "off-idem", "reason": "批准"})
        retry = self.svc.approve(plan["id"], "rev", "airspace_reviewer",
                                 {"expected_revision": 1, "offline_id": "off-idem", "reason": "重试"})
        self.assertTrue(retry["idempotent"])
        self.assertEqual(retry["approval_id"], first["approval_id"])
        self.assertEqual(retry["decision_state"], "effective")

        plan2 = self.submit("D201")
        self.restriction()  # 版本升到 2
        stale1 = self.svc.approve(plan2["id"], "rev", "airspace_reviewer",
                                  {"expected_revision": 1, "expected_restrictions_version": 1, "offline_id": "off-p", "reason": "陈旧"})
        self.assertEqual(stale1["decision_state"], "pending_review")
        stale2 = self.svc.approve(plan2["id"], "rev", "airspace_reviewer",
                                  {"expected_revision": 1, "expected_restrictions_version": 1, "offline_id": "off-p", "reason": "重试"})
        self.assertTrue(stale2["idempotent"])
        self.assertEqual(stale2["approval_id"], stale1["approval_id"])
        # 数据库中每个 offline_id 恰好一行
        with self.svc.repo.conn as conn:
            total = conn.execute("SELECT COUNT(*) c FROM approvals WHERE offline_id IN ('off-idem','off-p')").fetchone()["c"]
        self.assertEqual(total, 2)

    def test_offline_id_reused_for_different_decision_rejected(self):
        plan = self.submit()
        self.svc.approve(plan["id"], "rev", "airspace_reviewer",
                         {"expected_revision": 1, "offline_id": "off-dup", "reason": "批准"})
        with self.assertRaises(ApiError) as ctx:
            self.svc.reject(plan["id"], "rev", "airspace_reviewer",
                            {"expected_revision": 1, "offline_id": "off-dup", "reason": "挪用编号"})
        self.assertEqual(ctx.exception.code, "offline_id_conflict")

    def test_operator_sees_only_own_pending_reviews(self):
        plan = self.submit("D202")
        self.restriction()
        self.svc.approve(plan["id"], "rev", "airspace_reviewer",
                         {"expected_revision": 1, "expected_restrictions_version": 1, "offline_id": "off-op", "reason": "陈旧"})
        mine = self.svc.pending_reviews("operator", "OP1")
        self.assertEqual(mine["count"], 1)
        other = self.svc.pending_reviews("operator", "OP-OTHER")
        self.assertEqual(other["count"], 0)


if __name__ == "__main__": unittest.main()
