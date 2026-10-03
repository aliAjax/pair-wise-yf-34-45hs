#!/usr/bin/env python3
"""Drone flight-plan approval and airspace coordination service (standard library only)."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PORT = 8205
ROLES = {"viewer", "operator", "airspace_reviewer", "commander", "auditor"}
ACTIVE_STATUSES = {"submitted", "approved"}
DECISIONS = {"approved", "rejected"}
# 决定状态：effective=当前有效决定；superseded=计划或限制变化后失效；
# pending_review=版本对不上或同版本冲突，转人工复核
REVIEW_STATES = {"effective", "superseded", "pending_review"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message); self.status, self.code, self.message, self.details = status, code, message, details


def utcnow() -> datetime: return datetime.now(timezone.utc)
def iso(value: datetime | None = None) -> str: return (value or utcnow()).replace(microsecond=0).isoformat().replace("+00:00", "Z")
def parse_time(value: str | None) -> datetime:
    if not value: raise ApiError(400, "time_required", "必须提供 ISO 8601 时间")
    try: parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc: raise ApiError(400, "invalid_time", f"时间格式错误: {value}") from exc
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def route_bbox(route: list[list[float]]) -> tuple[float, float, float, float]:
    xs = [float(point[0]) for point in route]; ys = [float(point[1]) for point in route]
    return min(xs), min(ys), max(xs), max(ys)


def boxes_overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float], buffer: float = 0.0) -> bool:
    return a[0] <= b[2] + buffer and a[2] + buffer >= b[0] and a[1] <= b[3] + buffer and a[3] + buffer >= b[1]


def times_overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool: return a_start < b_end and b_start < a_end


def validate_route(route: Any) -> list[list[float]]:
    if not isinstance(route, list) or len(route) < 2: raise ApiError(400, "invalid_route", "航线至少需要两个经纬度点")
    normalized: list[list[float]] = []
    for point in route:
        if not isinstance(point, list) or len(point) != 2 or not all(isinstance(v, (int, float)) for v in point): raise ApiError(400, "invalid_route_point", "每个航线点必须是 [经度,纬度]")
        lon, lat = float(point[0]), float(point[1])
        if not -180 <= lon <= 180 or not -90 <= lat <= 90: raise ApiError(400, "invalid_coordinates", "经纬度超出范围")
        normalized.append([lon, lat])
    return normalized


class Repository:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        # 所有写事务串行化：两个审核员同时回传时，先拿到锁先入库的一方生效
        self.write_lock = threading.RLock()
        self.conn.row_factory = sqlite3.Row; self.conn.execute("PRAGMA foreign_keys=ON"); self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS meta(
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS restrictions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL, min_lon REAL NOT NULL, min_lat REAL NOT NULL,
            max_lon REAL NOT NULL, max_lat REAL NOT NULL, min_altitude REAL NOT NULL DEFAULT 0, max_altitude REAL NOT NULL,
            starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS flight_plans(
            id INTEGER PRIMARY KEY AUTOINCREMENT, operator_id TEXT NOT NULL, callsign TEXT NOT NULL, drone_model TEXT NOT NULL,
            payload_kg REAL NOT NULL, route_json TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, max_altitude REAL NOT NULL,
            population_risk INTEGER NOT NULL, emergency_plan TEXT NOT NULL, region TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
            revision INTEGER NOT NULL DEFAULT 1, created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(operator_id,callsign,starts_at)
        );
        CREATE TABLE IF NOT EXISTS approvals(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES flight_plans(id), plan_revision INTEGER NOT NULL,
            restrictions_version INTEGER NOT NULL, reviewer TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL,
            offline_id TEXT UNIQUE, override_kind TEXT, state TEXT NOT NULL DEFAULT 'effective', conflicts_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notifications(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES flight_plans(id), kind TEXT NOT NULL,
            message TEXT NOT NULL, created_at TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS audit_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, actor TEXT NOT NULL, role TEXT NOT NULL, action TEXT NOT NULL,
            detail_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """)
        # 轻量迁移：为既有数据库补齐新列，空域限制集合版本从 1 开始
        existing = {row["name"] for row in self.conn.execute("PRAGMA table_info(approvals)")}
        if "restrictions_version" not in existing: self.conn.execute("ALTER TABLE approvals ADD COLUMN restrictions_version INTEGER NOT NULL DEFAULT 1")
        if "state" not in existing: self.conn.execute("ALTER TABLE approvals ADD COLUMN state TEXT NOT NULL DEFAULT 'effective'")
        if "conflicts_json" not in existing: self.conn.execute("ALTER TABLE approvals ADD COLUMN conflicts_json TEXT NOT NULL DEFAULT '[]'")
        self.conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('restrictions_version','1')")

    def restrictions_version(self) -> int:
        return int(self.conn.execute("SELECT value FROM meta WHERE key='restrictions_version'").fetchone()["value"])

    @contextmanager
    def tx(self):
        with self.write_lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try: yield self.conn; self.conn.execute("COMMIT")
            except Exception: self.conn.execute("ROLLBACK"); raise

    @staticmethod
    def audit(conn: sqlite3.Connection, plan_id: int | None, actor: str, role: str, action: str, detail: dict[str, Any]) -> None:
        conn.execute("INSERT INTO audit_log(plan_id,actor,role,action,detail_json,created_at) VALUES(?,?,?,?,?,?)",
                     (plan_id, actor, role, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), iso()))

    @staticmethod
    def notify(conn: sqlite3.Connection, plan_id: int, kind: str, message: str) -> None:
        conn.execute("INSERT INTO notifications(plan_id,kind,message,created_at) VALUES(?,?,?,?)", (plan_id, kind, message, iso()))


class DroneAirspaceService:
    def __init__(self, path: str | Path): self.repo = Repository(path)

    @staticmethod
    def identity(headers: Any) -> tuple[str, str, str]:
        actor, role, operator = headers.get("X-User-Id", "").strip(), headers.get("X-Role", "").strip(), headers.get("X-Operator", "").strip()
        if not actor or role not in ROLES: raise ApiError(401, "unauthorized", "需要 X-User-Id 和有效 X-Role")
        if role == "operator" and not operator: raise ApiError(401, "operator_required", "运营方角色必须提供 X-Operator")
        return actor, role, operator

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None: return dict(row) if row else None

    def create_restriction(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "restriction_forbidden", "只有空域审核员或指挥官可以维护限制")
        name, kind, reason = str(body.get("name", "")).strip(), str(body.get("kind", "")).strip(), str(body.get("reason", "")).strip()
        if kind not in {"no_fly", "temporary_limit"} or not name or not reason: raise ApiError(400, "invalid_restriction", "名称、类型和原因必填")
        try:
            min_lon, min_lat, max_lon, max_lat = map(float, (body.get("min_lon"), body.get("min_lat"), body.get("max_lon"), body.get("max_lat")))
            min_alt, max_alt = float(body.get("min_altitude", 0)), float(body.get("max_altitude"))
        except (TypeError, ValueError): raise ApiError(400, "invalid_restriction", "空域范围和高度必须为数字")
        start, end = parse_time(body.get("starts_at")), parse_time(body.get("ends_at"))
        if min_lon >= max_lon or min_lat >= max_lat or min_alt < 0 or max_alt <= min_alt or end <= start:
            raise ApiError(400, "invalid_restriction", "空域范围、高度或时间无效")
        with self.repo.tx() as conn:
            cur = conn.execute("""INSERT INTO restrictions(name,kind,min_lon,min_lat,max_lon,max_lat,min_altitude,max_altitude,starts_at,ends_at,reason,status,created_at)
                                  VALUES(?,?,?,?,?,?,?,?,?,?,?,'active',?)""", (name, kind, min_lon, min_lat, max_lon, max_lat, min_alt, max_alt, iso(start), iso(end), reason, iso()))
            new_row = conn.execute("SELECT * FROM restrictions WHERE id=?", (cur.lastrowid,)).fetchone()
            # 空域限制集合发生变化：版本号 +1
            conn.execute("UPDATE meta SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) WHERE key='restrictions_version'")
            new_version = self.repo.restrictions_version()
            # 已批准计划若与新限制空域/时间/高度重叠，原批准立即失效并要求重新审批
            invalidated: list[dict[str, Any]] = []
            for plan in conn.execute("SELECT * FROM flight_plans WHERE status='approved'"):
                p_start, p_end = parse_time(plan["starts_at"]), parse_time(plan["ends_at"])
                bbox = route_bbox(self._route(plan))
                if self._restriction_hits_plan(new_row, bbox, p_start, p_end, plan["max_altitude"]):
                    conflict = [{"code": "restriction_changed", "restriction_id": new_row["id"], "name": new_row["name"],
                                 "restrictions_version": new_version,
                                 "message": "空域限制集合发生变化且与本计划冲突，原批准失效，必须重新审批"}]
                    count = self._invalidate_approvals(conn, plan["id"], conflict, actor, role, "approval_invalidated_by_restriction")
                    if count:
                        conn.execute("UPDATE flight_plans SET status='draft',updated_at=? WHERE id=?", (iso(), plan["id"]))
                        Repository.notify(conn, plan["id"], "approval_invalidated",
                                          f"飞行计划 {plan['callsign']} 与新增空域限制 {name} 冲突，原批准已失效，需要重新审批")
                        invalidated.append({"plan_id": plan["id"], "callsign": plan["callsign"]})
            Repository.audit(conn, None, actor, role, "restriction_created",
                             {"restriction_id": cur.lastrowid, "restrictions_version": new_version, "invalidated_plans": invalidated})
            result = dict(new_row); result["restrictions_version"] = new_version; result["invalidated_plans"] = invalidated
            return result

    def create_plan(self, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "plan_forbidden", "只有运营方可以创建飞行计划")
        required = ("callsign", "drone_model", "starts_at", "ends_at", "emergency_plan", "region")
        if any(body.get(key) in (None, "") for key in required): raise ApiError(400, "missing_fields", "飞行计划字段不完整")
        route = validate_route(body.get("route")); start, end = parse_time(body["starts_at"]), parse_time(body["ends_at"])
        try: payload, altitude = float(body.get("payload_kg")), float(body.get("max_altitude"))
        except (TypeError, ValueError): raise ApiError(400, "invalid_numbers", "payload_kg 和 max_altitude 必须为数字")
        risk = body.get("population_risk")
        if not 0 <= payload <= 25 or altitude <= 0 or not isinstance(risk, int) or not 0 <= risk <= 5:
            raise ApiError(400, "invalid_plan", "载荷、高度或人口风险无效")
        if end <= start or start <= utcnow(): raise ApiError(400, "invalid_time", "飞行时间必须在未来且结束晚于开始")
        bbox = route_bbox(route)
        with self.repo.tx() as conn:
            try:
                cur = conn.execute("""INSERT INTO flight_plans(operator_id,callsign,drone_model,payload_kg,route_json,starts_at,ends_at,max_altitude,population_risk,emergency_plan,region,created_by,created_at,updated_at)
                                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                   (operator, str(body["callsign"]).upper(), body["drone_model"], payload, json.dumps(route), iso(start), iso(end), altitude, risk, body["emergency_plan"], body["region"], actor, iso(), iso()))
            except sqlite3.IntegrityError as exc: raise ApiError(409, "plan_duplicate", "同一运营方、呼号和起飞时间的计划已存在") from exc
            plan_id = cur.lastrowid; Repository.audit(conn, plan_id, actor, role, "plan_created", {"bbox": bbox, "revision": 1})
            return self.get_plan(plan_id, role, operator)

    def _plan_row(self, conn: sqlite3.Connection, plan_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM flight_plans WHERE id=?", (plan_id,)).fetchone()
        if not row: raise ApiError(404, "plan_not_found", "飞行计划不存在")
        return row

    @staticmethod
    def _route(row: sqlite3.Row) -> list[list[float]]: return json.loads(row["route_json"])

    def check_conflicts(self, plan_id: int, role: str, operator: str) -> dict[str, Any]:
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if role == "operator" and plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能查看其他运营方计划")
            if role not in {"operator", "airspace_reviewer", "commander", "auditor", "viewer"}: raise ApiError(403, "check_forbidden", "无权检查冲突")
            return self._conflict_report(conn, plan)

    @staticmethod
    def _approval_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["conflicts"] = json.loads(item.pop("conflicts_json", "[]") or "[]")
        return item

    def _active_approvals(self, conn: sqlite3.Connection, plan_id: int) -> list[sqlite3.Row]:
        return list(conn.execute("SELECT * FROM approvals WHERE plan_id=? AND state='effective' ORDER BY id", (plan_id,)))

    def _invalidate_approvals(self, conn: sqlite3.Connection, plan_id: int, conflicts: list[dict[str, Any]], actor: str = "system", role: str = "system", action: str = "approval_superseded") -> int:
        """计划或限制变化后，把该计划上仍有效的决定全部置为失效。"""
        rows = self._active_approvals(conn, plan_id)
        for row in rows:
            conn.execute("UPDATE approvals SET state='superseded', conflicts_json=? WHERE id=?",
                         (json.dumps(conflicts, ensure_ascii=False), row["id"]))
            Repository.audit(conn, plan_id, actor, role, action,
                             {"approval_id": row["id"], "offline_id": row["offline_id"], "conflicts": conflicts})
        return len(rows)

    @staticmethod
    def _restriction_hits_plan(restriction: sqlite3.Row, bbox: tuple[float, float, float, float], start: datetime, end: datetime, max_altitude: float) -> bool:
        rbox = (restriction["min_lon"], restriction["min_lat"], restriction["max_lon"], restriction["max_lat"])
        if not boxes_overlap(bbox, rbox): return False
        if not times_overlap(start, end, parse_time(restriction["starts_at"]), parse_time(restriction["ends_at"])): return False
        return max_altitude > restriction["min_altitude"] and restriction["max_altitude"] > 0

    def _conflict_report(self, conn: sqlite3.Connection, plan: sqlite3.Row) -> dict[str, Any]:
        route = self._route(plan); bbox = route_bbox(route); start, end = parse_time(plan["starts_at"]), parse_time(plan["ends_at"])
        hard: list[dict[str, Any]] = []; blocking: list[dict[str, Any]] = []
        if plan["payload_kg"] > 25: hard.append({"code": "payload_limit", "message": "载荷超过 25kg 硬限制"})
        if plan["max_altitude"] > 120: hard.append({"code": "altitude_limit", "message": "常规计划高度不得超过 120m"})
        if plan["population_risk"] > 3: blocking.append({"code": "population_risk", "risk": plan["population_risk"], "message": "人口风险超过常规批准阈值"})
        for restriction in conn.execute("SELECT * FROM restrictions WHERE status='active'"):
            if self._restriction_hits_plan(restriction, bbox, start, end, plan["max_altitude"]):
                item = {"code": "airspace_restriction", "restriction_id": restriction["id"], "name": restriction["name"], "kind": restriction["kind"], "reason": restriction["reason"]}
                blocking.append(item)
        adjacent: list[dict[str, Any]] = []
        for other in conn.execute("SELECT * FROM flight_plans WHERE id!=? AND status IN ('submitted','approved') AND starts_at<? AND ends_at>?", (plan["id"], iso(end), iso(start))):
            if boxes_overlap(bbox, route_bbox(self._route(other)), 0.002):
                adjacent.append({"plan_id": other["id"], "callsign": other["callsign"], "operator_id": other["operator_id"], "status": other["status"], "starts_at": other["starts_at"], "ends_at": other["ends_at"]})
        if adjacent: blocking.append({"code": "adjacent_traffic", "plans": adjacent, "message": "相邻航路与有效计划重叠"})
        return {"plan_id": plan["id"], "revision": plan["revision"], "restrictions_version": self.repo.restrictions_version(),
                "hard_violations": hard, "blocking_conflicts": blocking, "approvable": not hard and not blocking}

    def get_plan(self, plan_id: int, role: str, operator: str = "") -> dict[str, Any]:
        conn = self.repo.conn; row = self._plan_row(conn, plan_id)
        if role == "operator" and row["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能查看其他运营方计划")
        result = dict(row); result["route"] = json.loads(result.pop("route_json")); result["route_bbox"] = route_bbox(result["route"])
        if role == "viewer":
            result = {key: result[key] for key in ("id", "callsign", "starts_at", "ends_at", "max_altitude", "region", "status", "valid_until" if "valid_until" in result else "updated_at")}
        if role in {"airspace_reviewer", "commander", "auditor"}: result["approvals"] = [self._approval_dict(r) for r in conn.execute("SELECT * FROM approvals WHERE plan_id=? ORDER BY id", (plan_id,))]
        return result

    def submit(self, plan_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "submit_forbidden", "只有运营方可以提交计划")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能提交其他运营方计划")
            if plan["status"] == "submitted": return {"plan": self.get_plan(plan_id, role, operator), "idempotent": True}
            if plan["status"] not in {"draft", "rejected"}: raise ApiError(409, "invalid_transition", "当前状态不能提交")
            if parse_time(plan["starts_at"]) <= utcnow(): raise ApiError(409, "plan_expired", "计划起飞时间已过")
            conn.execute("UPDATE flight_plans SET status='submitted',updated_at=? WHERE id=?", (iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_submitted", {"revision": plan["revision"]})
            return {"plan": self.get_plan(plan_id, role, operator), "idempotent": False}

    def _store_review(self, conn: sqlite3.Connection, plan: sqlite3.Row, actor: str, role: str, decision: str,
                      reason: str, offline_id: str, expected_plan_rev: int, expected_restrictions_version: int,
                      state: str, conflicts: list[dict[str, Any]], override_kind: str | None,
                      notify_kind: str, notify_message: str, audit_action: str) -> int:
        cur = conn.execute("""INSERT INTO approvals(plan_id,plan_revision,restrictions_version,reviewer,decision,reason,offline_id,override_kind,state,conflicts_json,created_at)
                              VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                           (plan["id"], expected_plan_rev, expected_restrictions_version, actor, decision, reason,
                            offline_id, override_kind, state, json.dumps(conflicts, ensure_ascii=False), iso()))
        audit_detail: dict[str, Any] = {"plan_revision": expected_plan_rev, "restrictions_version": expected_restrictions_version,
                                        "offline_id": offline_id, "state": state}
        if conflicts: audit_detail["conflicts"] = conflicts
        if override_kind: audit_detail["override_kind"] = override_kind
        Repository.audit(conn, plan["id"], actor, role, audit_action, audit_detail)
        Repository.notify(conn, plan["id"], notify_kind, notify_message)
        return cur.lastrowid

    def _review_result(self, conn: sqlite3.Connection, plan_id: int, role: str, approval_id: int | None,
                       state: str, conflicts: list[dict[str, Any]], idempotent: bool, override_kind: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {"plan": self.get_plan(plan_id, role, ""), "idempotent": idempotent,
                                  "decision_state": state, "conflicts": conflicts}
        if approval_id is not None: result["approval_id"] = approval_id
        if override_kind is not None: result["override_kind"] = override_kind
        return result

    def _merge_review(self, plan_id: int, actor: str, role: str, body: dict[str, Any], decision: str) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}:
            raise ApiError(403, "review_forbidden", "只有空域审核员或指挥官可以审核计划")
        expected_rev, offline_id = body.get("expected_revision"), str(body.get("offline_id", "")).strip()
        reason = str(body.get("reason", "")).strip()
        override = str(body.get("override_reason", "")).strip()
        if not isinstance(expected_rev, int) or not offline_id or not reason:
            raise ApiError(400, "review_details_required", "expected_revision、offline_id 和 reason 必填")
        # 离线端可回传决定所依据的空域限制集合版本；缺省时按当前版本处理
        expected_rv = body.get("expected_restrictions_version")
        if expected_rv is None: expected_rv = self.repo.restrictions_version()
        if not isinstance(expected_rv, int) or expected_rv < 1:
            raise ApiError(400, "invalid_restrictions_version", "expected_restrictions_version 必须是正整数")
        with self.repo.tx() as conn:
            # 离线编号幂等：同一编号重试只返回已入库结果，绝不重复写入
            prior = conn.execute("SELECT * FROM approvals WHERE offline_id=?", (offline_id,)).fetchone()
            if prior:
                if prior["plan_id"] != plan_id or prior["decision"] != decision:
                    raise ApiError(409, "offline_id_conflict", "该离线审核编号已经用于其他决定")
                return self._review_result(conn, plan_id, role, prior["id"], prior["state"],
                                           self._approval_dict(prior)["conflicts"], True, prior["override_kind"])
            plan = self._plan_row(conn, plan_id)
            if plan["status"] in {"canceled", "expired"}:
                raise ApiError(409, "plan_closed", "已取消或过期的计划不能再审核")
            # 三维合并校验之一：飞行计划版本必须一致，不一致直接拒绝套用旧决定
            # （即使计划已回到 draft，旧版本决定也要明确报版本失效，而不是“未提交”）
            if plan["revision"] != expected_rev:
                raise ApiError(409, "revision_conflict", "计划版本已变化，旧审核决定不能套用，请基于新版本重新审批")
            if plan["status"] == "draft":
                raise ApiError(409, "invalid_transition", "当前版本尚未提交，不能审核")
            current_rv = self.repo.restrictions_version()
            conflicts: list[dict[str, Any]] = []
            # 三维合并校验之三：同版本是否已有先入库的决定（先查，避免被计划状态挡住）
            existing = self._active_approvals(conn, plan_id)
            if existing:
                winner = existing[-1]
                conflicts.append({"code": "concurrent_decision", "existing_approval_id": winner["id"],
                                  "existing_reviewer": winner["reviewer"], "existing_decision": winner["decision"],
                                  "existing_plan_revision": winner["plan_revision"],
                                  "existing_restrictions_version": winner["restrictions_version"],
                                  "message": "同一计划版本已有先入库的审核决定，后到决定转入待复核"})
            # 用当前约束重算，供陈旧版本决定列出此刻的真实冲突
            report = self._conflict_report(conn, plan)
            # 三维合并校验之二：空域限制集合版本一致，否则决定依据已变，连同当前冲突一起列出
            if expected_rv != current_rv:
                conflicts.append({"code": "restrictions_version_stale", "expected_restrictions_version": expected_rv,
                                  "current_restrictions_version": current_rv,
                                  "hard_violations": report["hard_violations"], "blocking_conflicts": report["blocking_conflicts"],
                                  "message": "离线决定依据的空域限制集合已变化，旧决定不能直接生效"})
            if conflicts:
                approval_id = self._store_review(
                    conn, plan, actor, role, decision, reason, offline_id, expected_rev, expected_rv,
                    "pending_review", conflicts, None, "pending_review",
                    f"飞行计划 {plan['callsign']} 的离线{('批准' if decision == 'approved' else '拒绝')}决定存在冲突，已转入待复核",
                    "decision_pending_review")
                return self._review_result(conn, plan_id, role, approval_id, "pending_review", conflicts, False)
            # 走到这里同版本没有任何有效决定；已拒绝状态说明决定已被其他路径固化
            if plan["status"] == "rejected":
                raise ApiError(409, "revision_conflict", "计划当前版本已被拒绝，需要重新提交后再审批")
            if decision == "approved":
                if report["hard_violations"]: raise ApiError(409, "hard_constraint_violation", "计划违反不可覆盖的安全约束", report)
                if report["blocking_conflicts"] and not (role == "commander" and override):
                    raise ApiError(409, "airspace_conflict", "计划存在空域或相邻交通冲突", report)
                override_kind = "emergency_authority" if report["blocking_conflicts"] else None
                approval_id = self._store_review(
                    conn, plan, actor, role, "approved", reason, offline_id, expected_rev, current_rv,
                    "effective", [], override_kind, "approved", f"飞行计划 {plan['callsign']} 已批准", "plan_approved")
                conn.execute("UPDATE flight_plans SET status='approved',updated_at=? WHERE id=?", (iso(), plan_id))
                if override_kind:
                    Repository.audit(conn, plan_id, actor, role, "emergency_override_used",
                                     {"override_reason": override, "conflicts": report["blocking_conflicts"]})
                return self._review_result(conn, plan_id, role, approval_id, "effective", [], False, override_kind)
            approval_id = self._store_review(
                conn, plan, actor, role, "rejected", reason, offline_id, expected_rev, current_rv,
                "effective", [], None, "rejected", f"飞行计划 {plan['callsign']} 被拒绝：{reason}", "plan_rejected")
            conn.execute("UPDATE flight_plans SET status='rejected',updated_at=? WHERE id=?", (iso(), plan_id))
            return self._review_result(conn, plan_id, role, approval_id, "effective", [], False)

    def approve(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._merge_review(plan_id, actor, role, body, "approved")

    def reject(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._merge_review(plan_id, actor, role, body, "rejected")

    def change(self, plan_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "change_forbidden", "只有运营方可以变更计划")
        expected = body.get("expected_revision")
        if not isinstance(expected, int): raise ApiError(400, "revision_required", "expected_revision 必填")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能修改其他运营方计划")
            if plan["status"] in {"canceled", "expired"}: raise ApiError(409, "plan_closed", "已取消或过期计划不能修改")
            if plan["revision"] != expected: raise ApiError(409, "revision_conflict", "计划版本已变化")
            route = validate_route(body.get("route", self._route(plan)))
            start = parse_time(body.get("starts_at", plan["starts_at"])); end = parse_time(body.get("ends_at", plan["ends_at"]))
            if end <= start or start <= utcnow(): raise ApiError(400, "invalid_time", "新飞行时间无效")
            payload = float(body.get("payload_kg", plan["payload_kg"])); altitude = float(body.get("max_altitude", plan["max_altitude"]))
            risk = body.get("population_risk", plan["population_risk"])
            if not 0 <= payload <= 25 or altitude <= 0 or not isinstance(risk, int) or not 0 <= risk <= 5: raise ApiError(400, "invalid_plan", "变更后的载荷、高度或风险无效")
            revision = expected + 1
            # 计划变化：原版本上仍有效的全部决定立即失效，必须重新审批
            stale_conflict = [{"code": "plan_revision_changed", "previous_revision": expected, "current_revision": revision,
                               "message": "飞行计划已修改，旧版本上的审核决定全部失效，需要重新审批"}]
            stale_count = self._invalidate_approvals(conn, plan_id, stale_conflict, actor, role, "approval_invalidated_by_plan_change")
            conn.execute("""UPDATE flight_plans SET route_json=?,starts_at=?,ends_at=?,payload_kg=?,max_altitude=?,population_risk=?,emergency_plan=?,region=?,status='draft',revision=?,updated_at=? WHERE id=?""",
                         (json.dumps(route), iso(start), iso(end), payload, altitude, risk, body.get("emergency_plan", plan["emergency_plan"]), body.get("region", plan["region"]), revision, iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_changed", {"from_revision": expected, "to_revision": revision, "previous_status": plan["status"], "invalidated_approvals": stale_count})
            if plan["status"] == "approved": Repository.notify(conn, plan_id, "approval_invalidated", f"飞行计划 {plan['callsign']} 已修改，原批准自动失效")
            else: Repository.notify(conn, plan_id, "changed", f"飞行计划 {plan['callsign']} 已更新，需重新提交审核")
            return self.get_plan(plan_id, role, operator)

    def cancel(self, plan_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        reason = str(body.get("reason", "")).strip()
        if not reason: raise ApiError(400, "reason_required", "取消原因必填")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if role == "operator" and plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能取消其他运营方计划")
            if role not in {"operator", "airspace_reviewer", "commander"}: raise ApiError(403, "cancel_forbidden", "当前角色不能取消计划")
            if plan["status"] == "canceled": return {"plan": self.get_plan(plan_id, role, operator), "idempotent": True}
            if plan["status"] == "expired": raise ApiError(409, "plan_expired", "已过期计划不能取消")
            conn.execute("UPDATE flight_plans SET status='canceled',updated_at=? WHERE id=?", (iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_canceled", {"reason": reason})
            Repository.notify(conn, plan_id, "canceled", f"飞行计划 {plan['callsign']} 已取消：{reason}")
            return {"plan": self.get_plan(plan_id, role, operator), "idempotent": False}

    def pending_reviews(self, role: str, operator: str) -> dict[str, Any]:
        """列出转入待复核的离线决定及其冲突。"""
        if role == "operator":
            rows = self.repo.conn.execute("""SELECT a.* FROM approvals a JOIN flight_plans p ON p.id=a.plan_id
                                             WHERE a.state='pending_review' AND p.operator_id=? ORDER BY a.id""", (operator,))
        elif role in {"airspace_reviewer", "commander", "auditor"}:
            rows = self.repo.conn.execute("SELECT * FROM approvals WHERE state='pending_review' ORDER BY id")
        else:
            raise ApiError(403, "pending_reviews_forbidden", "当前角色不能查看待复核决定")
        items = []
        for row in rows:
            item = self._approval_dict(row)
            plan = self.repo.conn.execute("SELECT callsign,revision,status,operator_id FROM flight_plans WHERE id=?", (row["plan_id"],)).fetchone()
            item["plan"] = dict(plan) if plan else None
            items.append(item)
        return {"restrictions_version": self.repo.restrictions_version(), "pending_reviews": items, "count": len(items)}

    def notifications(self, actor: str, role: str, operator: str) -> dict[str, Any]:
        if role == "operator":
            rows = self.repo.conn.execute("""SELECT n.* FROM notifications n JOIN flight_plans p ON p.id=n.plan_id WHERE p.operator_id=? ORDER BY n.id DESC""", (operator,))
        elif role in {"airspace_reviewer", "commander", "auditor"}: rows = self.repo.conn.execute("SELECT * FROM notifications ORDER BY id DESC")
        else: raise ApiError(403, "notifications_forbidden", "当前角色不能读取通知")
        return {"notifications": [dict(r) for r in rows]}

    def expire_plans(self, actor: str, role: str) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "expire_forbidden", "当前角色不能执行到期处理")
        now = iso()
        with self.repo.tx() as conn:
            rows = list(conn.execute("SELECT * FROM flight_plans WHERE status='approved' AND ends_at<=?", (now,)))
            for row in rows:
                conn.execute("UPDATE flight_plans SET status='expired',updated_at=? WHERE id=?", (now, row["id"]))
                Repository.audit(conn, row["id"], actor, role, "plan_expired", {})
                Repository.notify(conn, row["id"], "expired", f"飞行计划 {row['callsign']} 已过期")
        return {"expired": len(rows)}

    def state(self, role: str, operator: str) -> dict[str, Any]:
        conn = self.repo.conn
        if role == "operator": rows = conn.execute("SELECT * FROM flight_plans WHERE operator_id=? ORDER BY id DESC", (operator,))
        elif role in {"airspace_reviewer", "commander", "auditor"}: rows = conn.execute("SELECT * FROM flight_plans ORDER BY id DESC")
        else: rows = conn.execute("SELECT * FROM flight_plans WHERE status='approved' ORDER BY id DESC")
        plans = []
        for row in rows:
            item = self.get_plan(row["id"], role, operator); plans.append(item)
        restrictions = [dict(r) for r in conn.execute("SELECT * FROM restrictions WHERE status='active' ORDER BY id DESC")] if role in {"airspace_reviewer", "commander", "auditor"} else []
        payload: dict[str, Any] = {"plans": plans, "restrictions": restrictions,
                                   "restrictions_version": self.repo.restrictions_version(), "server_time": iso()}
        if role in {"airspace_reviewer", "commander", "auditor"}:
            payload["pending_review_count"] = conn.execute("SELECT COUNT(*) AS c FROM approvals WHERE state='pending_review'").fetchone()["c"]
        return payload


def send_json(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    raw = json.dumps(payload, ensure_ascii=False, default=str).encode(); handler.send_response(status); handler.send_header("Content-Type", "application/json; charset=utf-8"); handler.send_header("Content-Length", str(len(raw))); handler.end_headers(); handler.wfile.write(raw)


class Handler(BaseHTTPRequestHandler):
    service: DroneAirspaceService; web_root: Path
    def log_message(self, fmt: str, *args: Any) -> None: print(f"{self.address_string()} - {fmt % args}")
    def body(self) -> dict[str, Any]:
        size = int(self.headers.get("Content-Length", "0"))
        if not size: return {}
        try: value = json.loads(self.rfile.read(size))
        except json.JSONDecodeError as exc: raise ApiError(400, "invalid_json", "请求体不是有效 JSON") from exc
        if not isinstance(value, dict): raise ApiError(400, "invalid_json", "请求体必须是对象")
        return value
    def get_api(self, path: str) -> tuple[int, Any]:
        if path == "/health": return 200, {"status": "ok", "service": "drone-airspace"}
        actor, role, operator = self.service.identity(self.headers)
        if path == "/api/state": return 200, self.service.state(role, operator)
        if path == "/api/notifications": return 200, self.service.notifications(actor, role, operator)
        if path == "/api/pending-reviews": return 200, self.service.pending_reviews(role, operator)
        parts = [p for p in path.split("/") if p]
        if len(parts) == 3 and parts[:2] == ["api", "plans"] and parts[2].isdigit(): return 200, self.service.get_plan(int(parts[2]), role, operator)
        if len(parts) == 4 and parts[:2] == ["api", "plans"] and parts[2].isdigit() and parts[3] == "check": return 200, self.service.check_conflicts(int(parts[2]), role, operator)
        raise ApiError(404, "not_found", "接口不存在")
    def post_api(self, path: str) -> tuple[int, Any]:
        actor, role, operator = self.service.identity(self.headers); body = self.body(); parts = [p for p in path.split("/") if p]
        if path == "/api/restrictions": return 201, self.service.create_restriction(actor, role, body)
        if path == "/api/plans": return 201, self.service.create_plan(actor, role, operator, body)
        if path == "/api/expire": return 200, self.service.expire_plans(actor, role)
        if len(parts) == 4 and parts[:2] == ["api", "plans"] and parts[2].isdigit():
            pid, action = int(parts[2]), parts[3]
            routes = {
                "submit": lambda: self.service.submit(pid, actor, role, operator, body),
                "approve": lambda: self.service.approve(pid, actor, role, body),
                "reject": lambda: self.service.reject(pid, actor, role, body),
                "change": lambda: self.service.change(pid, actor, role, operator, body),
                "cancel": lambda: self.service.cancel(pid, actor, role, operator, body),
            }
            if action in routes: return 200, routes[action]()
        raise ApiError(404, "not_found", "接口不存在")
    def handle_request(self, method: str) -> None:
        parsed = urlparse(self.path)
        try:
            if method == "GET" and parsed.path == "/":
                raw = (self.web_root / "index.html").read_bytes(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
            status, payload = self.get_api(parsed.path) if method == "GET" else self.post_api(parsed.path); send_json(self, status, payload)
        except ApiError as exc:
            payload = {"error": exc.code, "message": exc.message}
            if exc.details is not None: payload["details"] = exc.details
            send_json(self, exc.status, payload)
        except Exception as exc: print(f"unhandled error: {exc!r}"); send_json(self, 500, {"error": "internal_error", "message": str(exc)})
    def do_GET(self) -> None: self.handle_request("GET")
    def do_POST(self) -> None: self.handle_request("POST")


def create_server(db_path: str | Path, host: str = "127.0.0.1", port: int = PORT) -> ThreadingHTTPServer:
    service = DroneAirspaceService(db_path); handler = type("DroneHandler", (Handler,), {"service": service, "web_root": Path(__file__).resolve().parent / "static"}); return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=PORT); parser.add_argument("--db", default=os.environ.get("DRONE_DB", "drone_airspace.db")); args = parser.parse_args()
    server = create_server(args.db, args.host, args.port); print(f"drone-airspace listening on http://{args.host}:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__ == "__main__": main()
