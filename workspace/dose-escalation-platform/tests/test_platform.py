"""平台一致性测试:覆盖需求中的每一个"不得"与"必须"。

  - 队列冻结:方案/观察窗/受试者集合不可变
  - 决策门控:观察窗完整 + 缺失数据处理完毕 + 委员会达门槛
  - 迟到 AE / 替补 / 规则修订 / 并发投票:不得把不同快照拼成结论
  - 紧急暂停:立即阻断后续给药,保留已给药事实
  - 决策证明:哈希链可独立验证,篡改可检测
"""
from __future__ import annotations

import threading
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.domain import (
    CohortPaused, Conflict, DuplicateVote, Frozen, PreconditionFailed, StaleSnapshot,
)
from app.service import SafetyPlatform


class Clock:
    def __init__(self, d: date):
        self.d = d

    def __call__(self):
        return self.d

    def advance(self, days: int):
        self.d += timedelta(days=days)


DAY0 = date(2026, 1, 5)
WINDOW = 28


@pytest.fixture
def clock():
    return Clock(DAY0)


@pytest.fixture
def pf(clock):
    return SafetyPlatform(clock=clock)


@pytest.fixture
def trial(pf):
    return pf.create_trial(
        "ABC-101 FIH", "I",
        [{"id": "dsmb1"}, {"id": "dsmb2"}, {"id": "dsmb3"}],
        quorum=3, approve_threshold=2)


@pytest.fixture
def cohort(pf, trial):
    return pf.create_cohort(trial["id"], "C1", 10.0, "QD x 7d", WINDOW,
                            ["S01", "S02", "S03"])


def dose_all_and_wait(pf, clock, cohort, codes=("S01", "S02", "S03")):
    """全部给药、数据补齐、时钟推进到观察窗结束。"""
    for c in codes:
        pf.record_dose(cohort["id"], c, DAY0.isoformat())
        pf.set_data_status(cohort["id"], c, "COMPLETE")
    clock.advance(WINDOW)


# ---------------------------------------------------------------- 队列冻结

class TestFrozenCohort:
    def test_snapshot_hash_stable_for_same_state(self, pf, cohort):
        h1 = pf.snapshot_view(cohort["id"])["snapshot_hash"]
        h2 = pf.snapshot_view(cohort["id"])["snapshot_hash"]
        assert h1 == h2  # 同一逻辑状态,任意时刻重建,哈希相同

    def test_frozen_config_in_snapshot(self, pf, cohort):
        snap = pf.snapshot_view(cohort["id"])["cohort"]
        assert snap["dose_mg"] == 10.0 and snap["window_days"] == WINDOW
        assert snap["schedule"] == "QD x 7d"

    def test_add_subjects_blocked_before_expand(self, pf, cohort):
        with pytest.raises(Frozen):
            pf.add_subjects(cohort["id"], ["S04"])

    def test_dose_uses_frozen_regimen(self, pf, cohort):
        r = pf.record_dose(cohort["id"], "S01", DAY0.isoformat())
        assert r["dose_mg"] == 10.0  # 给药量来自冻结方案,不可在记录时篡改


# ---------------------------------------------------------------- 决策门控

class TestDecisionGating:
    def test_window_incomplete_blocks_session(self, pf, clock, cohort):
        for c in ("S01", "S02", "S03"):
            pf.record_dose(cohort["id"], c, DAY0.isoformat())
            pf.set_data_status(cohort["id"], c, "COMPLETE")
        clock.advance(WINDOW - 1)  # 观察窗还差 1 天
        with pytest.raises(PreconditionFailed) as ei:
            pf.open_session(cohort["id"], "ESCALATE")
        assert any("观察窗未结束" in b for b in ei.value.details["blockers"])

    def test_missing_data_blocks_then_unblocks(self, pf, clock, cohort):
        for c in ("S01", "S02", "S03"):
            pf.record_dose(cohort["id"], c, DAY0.isoformat())
        clock.advance(WINDOW)
        with pytest.raises(PreconditionFailed) as ei:
            pf.open_session(cohort["id"], "ESCALATE")
        assert any("缺失数据" in b for b in ei.value.details["blockers"])
        for c in ("S01", "S02", "S03"):
            pf.set_data_status(cohort["id"], c, "COMPLETE")
        s = pf.open_session(cohort["id"], "ESCALATE")
        assert s["snapshot_hash"]

    def test_pending_event_blocks(self, pf, clock, cohort):
        dose_all_and_wait(pf, clock, cohort)
        pf.report_event(cohort["id"], "S01", "头痛", 1, evaluability="PENDING")
        with pytest.raises(PreconditionFailed) as ei:
            pf.open_session(cohort["id"], "ESCALATE")
        assert any("可评估性待定" in b for b in ei.value.details["blockers"])

    def test_rules_forbid_escalate_when_dlt(self, pf, clock, cohort):
        dose_all_and_wait(pf, clock, cohort)
        pf.report_event(cohort["id"], "S01", "3级中性粒细胞减少", 3,
                        evaluability="EVALUABLE", related=True)
        pf.report_event(cohort["id"], "S02", "3级肝酶升高", 3,
                        evaluability="EVALUABLE", related=True)
        with pytest.raises(PreconditionFailed):
            pf.open_session(cohort["id"], "ESCALATE")  # 2 例 DLT:禁止递增
        s = pf.open_session(cohort["id"], "STOP")      # STOP 始终允许
        assert s["intent"] == "STOP"


# ---------------------------------------------------------------- 快照一致性:迟到事件 / 替补 / 规则修订 / 并发投票

class TestSnapshotConsistency:
    def test_late_ae_invalidates_open_session(self, pf, clock, cohort):
        dose_all_and_wait(pf, clock, cohort)
        s = pf.open_session(cohort["id"], "ESCALATE")
        pf.vote(s["id"], "dsmb1", "APPROVE")
        # 观察窗结束后才报到的迟到不良事件
        r = pf.report_event(cohort["id"], "S02", "迟发皮疹", 2,
                            evaluability="EVALUABLE", related=True)
        assert r["late"] is True
        # 后续投票被拒绝:不得把"开会时的快照"和"含迟到事件的快照"拼成结论
        with pytest.raises(StaleSnapshot) as ei:
            pf.vote(s["id"], "dsmb2", "APPROVE")
        assert ei.value.code == "STALE_SNAPSHOT"
        # 已投出的票作为事实保留,但会话无法定稿
        ses = pf.get_session(s["id"])
        assert len(ses["votes"]) == 1 and ses["stale_reasons"]
        pf.close_session(s["id"])
        # 基于包含迟到事件的新快照重开,正常定稿
        s2 = pf.open_session(cohort["id"], "ESCALATE")
        assert s2["state_version"] > s["state_version"]
        for m in ("dsmb1", "dsmb2", "dsmb3"):
            pf.vote(s2["id"], m, "APPROVE")
        proof = pf.get_proof(s2["id"])
        assert any(e["subject_code"] == "S02" and e["late"]
                   for e in proof["payload"]["snapshot"]["events"])

    def test_replacement_invalidates_open_session(self, pf, clock, cohort):
        dose_all_and_wait(pf, clock, cohort)
        s = pf.open_session(cohort["id"], "ESCALATE")
        pf.replace_subject(cohort["id"], "S03", "S03B", "失访")
        with pytest.raises(StaleSnapshot):
            pf.vote(s["id"], "dsmb1", "APPROVE")

    def test_replaced_subject_excluded_replacement_counted(self, pf, clock, cohort):
        pf.record_dose(cohort["id"], "S01", DAY0.isoformat())
        pf.record_dose(cohort["id"], "S02", DAY0.isoformat())
        # S03 未给药即被替补 -> 不计入评估集合;替补者 S03B 计入
        pf.replace_subject(cohort["id"], "S03", "S03B", "入组前退出")
        pf.record_dose(cohort["id"], "S03B", DAY0.isoformat())
        for c in ("S01", "S02", "S03B"):
            pf.set_data_status(cohort["id"], c, "COMPLETE")
        clock.advance(WINDOW)
        view = pf.snapshot_view(cohort["id"])
        by_code = {s["code"]: s for s in view["subjects"]}
        assert by_code["S03"]["counts"] is False     # 被替补且无 DLT、未完成窗
        assert by_code["S03B"]["counts"] is True
        assert view["evaluation"]["evaluable_count"] == 3
        s = pf.open_session(cohort["id"], "ESCALATE")  # 3 例可评估 -> 允许
        assert s["intent"] == "ESCALATE"

    def test_rule_amendment_invalidates_open_session(self, pf, clock, cohort, trial):
        dose_all_and_wait(pf, clock, cohort)
        s = pf.open_session(cohort["id"], "ESCALATE")
        pf.vote(s["id"], "dsmb1", "APPROVE")
        pf.amend_rules(trial["id"], "v2", {
            "design": "3+3", "dlt_min_severity": 4,   # DLT 定义收紧
            "require_related": True, "initial_size": 3, "expanded_size": 6})
        with pytest.raises(StaleSnapshot) as ei:
            pf.vote(s["id"], "dsmb2", "APPROVE")
        assert "规则已修订" in str(ei.value)

    def test_client_side_stale_view_rejected(self, pf, clock, cohort):
        dose_all_and_wait(pf, clock, cohort)
        s = pf.open_session(cohort["id"], "ESCALATE")
        pf.report_event(cohort["id"], "S01", "恶心", 1, evaluability="EVALUABLE")
        # 委员基于旧版本快照投票 -> 拒绝
        with pytest.raises(StaleSnapshot):
            pf.vote(s["id"], "dsmb1", "APPROVE",
                    seen_state_version=s["state_version"])

    def test_concurrent_votes_serialize_single_decision(self, pf, clock, trial):
        """两名委员并发投票:串行化、各记一票、法定人数在同一事务内判定,
        决策恰好定稿一次,账本中恰好一条 DECISION_FINALIZED。"""
        t2 = pf.create_trial("并发试验", "I", [{"id": "m1"}, {"id": "m2"}],
                             quorum=2, approve_threshold=2)
        c2 = pf.create_cohort(t2["id"], "C1", 5.0, "QD", WINDOW, ["A01", "A02", "A03"])
        dose_all_and_wait(pf, clock, c2, ("A01", "A02", "A03"))
        s = pf.open_session(c2["id"], "ESCALATE")
        barrier = threading.Barrier(2)
        results, errors = [], []

        def cast(member):
            try:
                barrier.wait(timeout=5)  # 对齐起跑线,最大化竞争
                results.append(pf.vote(s["id"], member, "APPROVE"))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=cast, args=(m,)) for m in ("m1", "m2")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert not errors, f"并发投票出现异常: {errors}"
        assert len(results) == 2                       # 两票都记录
        assert sum(1 for r in results if r["decided"]) == 1   # 恰好定稿一次
        ses = pf.get_session(s["id"])
        assert ses["status"] == "DECIDED" and ses["outcome"] == "ESCALATE"
        assert len(ses["votes"]) == 2                  # 无重复、无丢失
        finals = [e for e in pf.ledger.entries("DECISION_FINALIZED")
                  if e["payload"]["session_id"] == s["id"]]
        assert len(finals) == 1                        # 账本中唯一决策证明
        # 证明中的每一票都钉在同一快照版本上
        assert {v["state_version_seen"] for v in finals[0]["payload"]["votes"]} \
            == {s["state_version"]}

    def test_concurrent_vote_and_late_ae_never_mix(self, pf, clock, cohort):
        """投票与迟到事件并发:无论时序如何,结论要么不含事件、要么整体重开,
        绝不允许一半票数在旧快照、一半在新快照上定稿。"""
        dose_all_and_wait(pf, clock, cohort)
        s = pf.open_session(cohort["id"], "ESCALATE")
        pf.vote(s["id"], "dsmb1", "APPROVE")
        pf.report_event(cohort["id"], "S01", "迟发头晕", 1, evaluability="EVALUABLE")
        # 事件落地后,旧会话上的任何后续投票都必须失败
        for m in ("dsmb2", "dsmb3"):
            with pytest.raises(StaleSnapshot):
                pf.vote(s["id"], m, "APPROVE")
        assert pf.get_session(s["id"])["status"] == "OPEN"  # 未定稿,未撕裂


# ---------------------------------------------------------------- 委员会门槛

class TestCommitteeThreshold:
    def test_quorum_required(self, pf, clock, cohort):
        dose_all_and_wait(pf, clock, cohort)
        s = pf.open_session(cohort["id"], "ESCALATE")
        r = pf.vote(s["id"], "dsmb1", "APPROVE")
        assert r["decided"] is None  # 未达法定人数,不得出结论
        assert pf.get_session(s["id"])["status"] == "OPEN"

    def test_threshold_not_met_rejected(self, pf, clock, cohort):
        dose_all_and_wait(pf, clock, cohort)
        s = pf.open_session(cohort["id"], "ESCALATE")
        pf.vote(s["id"], "dsmb1", "APPROVE")
        pf.vote(s["id"], "dsmb2", "REJECT")
        r = pf.vote(s["id"], "dsmb3", "REJECT")
        assert r["decided"] == "REJECTED"  # 1/3 赞成 < 门槛 2
        assert pf.snapshot_view(cohort["id"])["cohort"]["status"] == "ACTIVE"

    def test_duplicate_vote_rejected(self, pf, clock, cohort):
        dose_all_and_wait(pf, clock, cohort)
        s = pf.open_session(cohort["id"], "ESCALATE")
        pf.vote(s["id"], "dsmb1", "APPROVE")
        with pytest.raises(DuplicateVote):
            pf.vote(s["id"], "dsmb1", "APPROVE")

    def test_vote_after_decision_rejected(self, pf, clock, cohort):
        dose_all_and_wait(pf, clock, cohort)
        s = pf.open_session(cohort["id"], "ESCALATE")
        for m in ("dsmb1", "dsmb2", "dsmb3"):
            pf.vote(s["id"], m, "APPROVE")
        with pytest.raises(Conflict):
            pf.vote(s["id"], "dsmb1", "APPROVE")

    def test_non_member_rejected(self, pf, clock, cohort):
        dose_all_and_wait(pf, clock, cohort)
        s = pf.open_session(cohort["id"], "ESCALATE")
        with pytest.raises(Exception) as ei:
            pf.vote(s["id"], "outsider", "APPROVE")
        assert getattr(ei.value, "status", None) == 404


# ---------------------------------------------------------------- 紧急暂停

class TestEmergencyPause:
    def test_pause_blocks_dosing_immediately_preserves_facts(self, pf, clock, cohort):
        pf.record_dose(cohort["id"], "S01", DAY0.isoformat())
        pf.pause("COHORT", cohort["id"], "2级过敏反应,待评估")
        with pytest.raises(CohortPaused) as ei:
            pf.record_dose(cohort["id"], "S02", DAY0.isoformat())
        assert ei.value.code == "COHORT_PAUSED"
        view = pf.snapshot_view(cohort["id"])
        s01 = next(s for s in view["subjects"] if s["code"] == "S01")
        assert s01["dose_count"] == 1          # 已给药事实保留
        assert view["paused"] is not None
        pf.resume("COHORT", cohort["id"])
        pf.record_dose(cohort["id"], "S02", DAY0.isoformat())  # 恢复后可用

    def test_pause_supersedes_escalate_allows_stop(self, pf, clock, cohort):
        dose_all_and_wait(pf, clock, cohort)
        s = pf.open_session(cohort["id"], "ESCALATE")
        r = pf.pause("COHORT", cohort["id"], "SAE 待调查")
        assert s["id"] in r["superseded_sessions"]   # 递增会话立即作废
        with pytest.raises(PreconditionFailed):
            pf.open_session(cohort["id"], "ESCALATE")  # 暂停期间禁止递增
        s2 = pf.open_session(cohort["id"], "STOP")     # 但允许正式停止
        for m in ("dsmb1", "dsmb2", "dsmb3"):
            pf.vote(s2["id"], m, "APPROVE")
        assert pf.snapshot_view(cohort["id"])["cohort"]["status"] == "STOPPED"

    def test_trial_level_pause_blocks_all_cohorts(self, pf, clock, trial, cohort):
        pf.pause("TRIAL", trial["id"], "全试验暂停")
        with pytest.raises(CohortPaused):
            pf.record_dose(cohort["id"], "S01", DAY0.isoformat())


# ---------------------------------------------------------------- 扩展路径(3+3)

class TestExpandPath:
    def test_one_dlt_expand_then_escalate(self, pf, clock, cohort):
        dose_all_and_wait(pf, clock, cohort)
        pf.report_event(cohort["id"], "S01", "3级腹泻", 3,
                        evaluability="EVALUABLE", related=True)
        view = pf.snapshot_view(cohort["id"])
        assert "EXPAND" in view["evaluation"]["permitted"]
        assert "ESCALATE" not in view["evaluation"]["permitted"]  # 1/3 DLT 不可直接递增
        s = pf.open_session(cohort["id"], "EXPAND")
        for m in ("dsmb1", "dsmb2", "dsmb3"):
            pf.vote(s["id"], m, "APPROVE")
        assert pf.snapshot_view(cohort["id"])["cohort"]["expanded"] is True
        # 扩展后增补 3 例并完成观察
        pf.add_subjects(cohort["id"], ["S04", "S05", "S06"])
        dose_all_and_wait(pf, clock, cohort, ("S04", "S05", "S06"))
        view = pf.snapshot_view(cohort["id"])
        assert view["evaluation"]["evaluable_count"] == 6
        assert "ESCALATE" in view["evaluation"]["permitted"]  # <=1/6 DLT


# ---------------------------------------------------------------- 决策证明与账本

class TestProofAndLedger:
    def test_proof_verifiable_and_self_contained(self, pf, clock, cohort):
        dose_all_and_wait(pf, clock, cohort)
        s = pf.open_session(cohort["id"], "ESCALATE")
        for m in ("dsmb1", "dsmb2", "dsmb3"):
            pf.vote(s["id"], m, "APPROVE")
        v = pf.verify_proof(s["id"])
        assert v["ok"] and v["entry_hash_ok"] and v["snapshot_hash_ok"]
        proof = pf.get_proof(s["id"])["payload"]
        assert proof["outcome"] == "ESCALATE"
        assert proof["snapshot_hash"] == s["snapshot_hash"]
        assert len(proof["votes"]) == 3
        assert proof["rule_version"] == "v1"

    def test_ledger_chain_verifies(self, pf, clock, cohort):
        dose_all_and_wait(pf, clock, cohort)
        assert pf.ledger.verify()["ok"]

    def test_tamper_detected(self, pf, clock, cohort):
        dose_all_and_wait(pf, clock, cohort)
        pf._conn.execute(
            "UPDATE ledger SET payload='{\"forged\": true}' WHERE seq=1")
        r = pf.ledger.verify()
        assert not r["ok"] and r["first_bad_seq"] == 1


# ---------------------------------------------------------------- API 端到端

class TestApi:
    def test_full_flow_over_http(self, clock):
        client = TestClient(create_app(clock=clock))
        t = client.post("/api/trials", json={
            "name": "E2E", "committee": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
            "quorum": 3, "approve_threshold": 2}).json()
        c = client.post("/api/cohorts", json={
            "trial_id": t["id"], "label": "C1", "dose_mg": 10, "schedule": "QD",
            "window_days": WINDOW, "subjects": ["S01", "S02", "S03"]}).json()
        for code in ("S01", "S02", "S03"):
            r = client.post(f"/api/cohorts/{c['id']}/doses",
                            json={"subject_code": code, "given_at": DAY0.isoformat()})
            assert r.status_code == 201
            client.post(f"/api/cohorts/{c['id']}/subjects/{code}/data-status",
                        json={"status": "COMPLETE"})
        clock.advance(WINDOW)
        s = client.post(f"/api/cohorts/{c['id']}/sessions",
                        json={"intent": "ESCALATE"}).json()
        # 迟到事件使会话过期:HTTP 层返回 409 + STALE_SNAPSHOT
        client.post(f"/api/cohorts/{c['id']}/events", json={
            "subject_code": "S01", "description": "迟发头痛", "severity": 1,
            "evaluability": "EVALUABLE"})
        r = client.post(f"/api/sessions/{s['id']}/votes",
                        json={"member_id": "a", "vote": "APPROVE"})
        assert r.status_code == 409 and r.json()["error"]["code"] == "STALE_SNAPSHOT"
        client.post(f"/api/sessions/{s['id']}/close")
        s2 = client.post(f"/api/cohorts/{c['id']}/sessions",
                         json={"intent": "ESCALATE"}).json()
        for m in ("a", "b", "c"):
            client.post(f"/api/sessions/{s2['id']}/votes",
                        json={"member_id": m, "vote": "APPROVE"})
        v = client.get(f"/api/proofs/{s2['id']}/verify").json()
        assert v["ok"]
        assert client.get("/api/ledger/verify").json()["ok"]

    def test_pause_over_http(self, clock):
        client = TestClient(create_app(clock=clock))
        t = client.post("/api/trials", json={
            "name": "P", "committee": [{"id": "a"}]}).json()
        c = client.post("/api/cohorts", json={
            "trial_id": t["id"], "label": "C1", "dose_mg": 5, "schedule": "QD",
            "window_days": 7, "subjects": ["S01"]}).json()
        client.post(f"/api/cohorts/{c['id']}/doses", json={"subject_code": "S01"})
        client.post(f"/api/cohorts/{c['id']}/pause", json={"reason": "SAE"})
        r = client.post(f"/api/cohorts/{c['id']}/doses", json={"subject_code": "S01"})
        assert r.status_code == 409 and r.json()["error"]["code"] == "COHORT_PAUSED"
        snap = client.get(f"/api/cohorts/{c['id']}/snapshot").json()
        assert snap["subjects"][0]["dose_count"] == 1  # 已给药事实保留
