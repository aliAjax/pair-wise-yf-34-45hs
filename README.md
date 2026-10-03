# 无人机飞行计划审批与空域协调系统

标准库独立项目。系统记录运营方计划、航线、载荷、高度、人口风险和应急方案，检查临时禁飞区、高度范围、人口风险以及相邻有效计划冲突。离线审核回传按**飞行计划版本 + 空域限制集合版本 + 离线决定**三维合并校验：版本一致且无在先决定才生效；版本陈旧或同版本并发的后到决定转入待复核并列出冲突。审核结果支持离线编号幂等回传；计划变更或新增冲突限制都会使原批准失效并生成通知。

## 运行

```bash
python3 app.py --db drone_airspace.db
```

默认监听 `127.0.0.1:8205`，首页 `/`，健康检查 `/health`。

身份头为 `X-User-Id`、`X-Role`；运营方还需 `X-Operator`。角色：`viewer`、`operator`、`airspace_reviewer`、`commander`、`auditor`。

## 离线决定合并规则

- 空域限制集合维护全局递增版本号（`meta.restrictions_version`），`GET /api/plans/{id}/check` 与 `/api/state` 均返回当前版本；离线审核时通过 `expected_restrictions_version` 回传决定所依据的版本（缺省按当前版本处理）。
- `approve`/`reject` 统一三维校验：
  1. `expected_revision` 必须等于计划当前版本，否则返回 `revision_conflict`，旧决定不允许套用；
  2. `expected_restrictions_version` 必须等于当前限制集合版本，否则决定记为 `pending_review`，冲突码 `restrictions_version_stale`，并附上当前实时重算的硬冲突/空域冲突；
  3. 同一计划版本已有有效决定时，先入库者生效（写事务由全局锁串行化），后到决定记为 `pending_review`，冲突码 `concurrent_decision`，并列出在先决定的审核人、决定与版本。
- 决定记录三态：`effective`（有效）、`superseded`（计划或限制变化后失效）、`pending_review`（待人工复核）。
- 计划 `change` 会提升 revision 并把旧版本有效决定置为 `superseded`（冲突码 `plan_revision_changed`），计划回到 draft 需重新提交审批。
- 新增限制在同一事务内提升限制版本；与已批准计划在空间/时间/高度上重叠的，原批准自动置为 `superseded`（冲突码 `restriction_changed`），计划回到 draft 并通知运营方；不重叠的计划不受影响。
- `offline_id` 全局唯一：回传失败按原编号重试时直接返回首次入库结果（`idempotent: true`，含其当时状态），不会重复写入；编号被用于其他计划或相反决定时返回 `offline_id_conflict`。

## 主要接口

- `POST /api/restrictions`：新增临时限制或禁飞区（同时提升限制集合版本、失效受影响的已批准计划）。
- `POST /api/plans`：创建飞行计划。
- `GET /api/plans/{id}/check`：检查硬约束和相邻交通冲突，返回 `restrictions_version`。
- `POST /api/plans/{id}/submit`、`approve`、`reject`：提交和审核；审核使用 `offline_id` 保证断网重连幂等，可带 `expected_restrictions_version`。
- `POST /api/plans/{id}/change`、`cancel`：版本化变更与取消，并生成通知。
- `GET /api/pending-reviews`：列出待复核离线决定及冲突明细（审核角色看全部，运营方只看本机构）。
- `GET /api/notifications`、`POST /api/expire`：通知与到期处理。
- `GET /api/state`：按角色返回计划、限制、当前 `restrictions_version` 与待复核数量。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

空域几何使用经纬度矩形和航线包围盒近似，不包含多边形、椭球距离、地形、实时遥测和完整间隔标准。紧急授权只能覆盖空域及交通冲突，不能绕过载荷与高度硬限制。身份头、无签名离线审核以及单机 SQLite 适合原型，生产环境需要 PKI、真实 GIS 引擎和跨机构事件总线。
