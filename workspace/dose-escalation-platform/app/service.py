"""业务核心:队列冻结、安全事件、规则引擎门控、委员会投票、紧急暂停。

一致性模型(核心不变量):
  1. 每个队列有单调递增的 state_version;任何影响决策输入的变更
     (给药、迟到 AE、事件更新、替补、增补、缺失数据处理)都使其 +1。
  2. 决策会话在开启时钉住 (state_version, rule_version, snapshot_hash),
     此后所有投票必须针对同一快照;版本不符即拒绝(STALE_SNAPSHOT),
     迟到事件 / 规则修订 / 并发投票都无法把不同快照拼成结论。
  3. 法定人数在同一事务内判定,会话最多被 finalize 一次。
  4. 紧急暂停立即阻断给药写入;已给药事实永不修改、保留在快照中。
"""
from __future__ import annotations

import json
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from . import rules as rule_engine
from .domain import (
    CohortPaused, CohortStatus, Conflict, DataStatus, DuplicateVote, Evaluability,
    Frozen, Intent, InvalidInput, NotFound, PlatformError, PreconditionFailed,
    SessionStatus, StaleSnapshot, SubjectStatus, Vote, hash_obj,
)
from .ledger import Ledger
from .store import connect


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_date(s: str, field: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise InvalidInput(f"{field} 必须是 YYYY-MM-DD 格式: {s!r}")


class SafetyPlatform:
    def __init__(self, db_path: str = ":memory:", clock=None):
        self._conn = connect(db_path)
        self._lock = threading.RLock()
        self._clock = clock or date.today   # 可注入时钟,便于测试与演练
        self.ledger = Ledger(self._conn)

    # ------------------------------------------------------------ 基础设施

    @contextmanager
    def _tx(self):
        """串行化写事务:RLock + BEGIN IMMEDIATE,并发投票在此排队。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _today(self) -> date:
        return self._clock()

    def _row(self, sql: str, args=(), what: str = "记录"):
        r = self._conn.execute(sql, args).fetchone()
        if r is None:
            raise NotFound(f"{what}不存在")
        return r

    def _get_cohort(self, cohort_id: str):
        return self._row("SELECT * FROM cohorts WHERE id=?", (cohort_id,), "队列")

    def _bump(self, cohort_id: str):
        self._conn.execute(
            "UPDATE cohorts SET state_version=state_version+1 WHERE id=?",
            (cohort_id,),
        )

    def _current_ruleset(self, trial_id: str):
        return self._row(
            "SELECT * FROM rulesets WHERE trial_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
            (trial_id,), "规则集",
        )

    def _active_pause(self, cohort_id: str, trial_id: str):
        return self._conn.execute(
            "SELECT * FROM pauses WHERE active=1 AND ("
            "  (scope='COHORT' AND scope_id=?) OR (scope='TRIAL' AND scope_id=?))",
            (cohort_id, trial_id),
        ).fetchone()

    # ------------------------------------------------------------ 试验 / 委员会 / 规则

    def create_trial(self, name: str, phase: str, committee: list[dict],
                     quorum: int | None = None, approve_threshold: int | None = None,
                     ruleset: dict | None = None) -> dict:
        if not committee:
            raise InvalidInput("独立安全委员会至少需 1 名成员")
        quorum = quorum if quorum is not None else len(committee)
        approve_threshold = approve_threshold if approve_threshold is not None else quorum // 2 + 1
        if not (1 <= approve_threshold <= quorum <= len(committee)):
            raise InvalidInput("门槛需满足 1 <= 赞成门槛 <= 法定人数 <= 委员数")
        with self._tx():
            tid = _uid("trl")
            self._conn.execute(
                "INSERT INTO trials(id,name,phase,status,quorum,approve_threshold,created_at)"
                " VALUES(?,?,?,'ACTIVE',?,?,?)",
                (tid, name, phase, quorum, approve_threshold, _now()),
            )
            for m in committee:
                self._conn.execute(
                    "INSERT INTO committee_members(trial_id,member_id,name,active)"
                    " VALUES(?,?,?,1)",
                    (tid, m["id"], m.get("name", m["id"])),
                )
            body = ruleset or rule_engine.DEFAULT_RULESET_V1
            self._conn.execute(
                "INSERT INTO rulesets(id,trial_id,version,body,created_at)"
                " VALUES(?,?,?,?,?)",
                (_uid("rul"), tid, "v1", json.dumps(body), _now()),
            )
            self.ledger.append("TRIAL_CREATED", tid, {
                "name": name, "phase": phase, "committee": committee,
                "quorum": quorum, "approve_threshold": approve_threshold,
                "ruleset": body, "rule_version": "v1",
            })
            return {"id": tid, "quorum": quorum, "approve_threshold": approve_threshold,
                    "rule_version": "v1"}

    def amend_rules(self, trial_id: str, version: str, body: dict) -> dict:
        """规则修订:产生新版本。已开启的会话钉住旧版本,随即过期。"""
        self._row("SELECT * FROM trials WHERE id=?", (trial_id,), "试验")
        with self._tx():
            if self._conn.execute(
                "SELECT 1 FROM rulesets WHERE trial_id=? AND version=?",
                (trial_id, version),
            ).fetchone():
                raise Conflict(f"规则版本 {version} 已存在")
            self._conn.execute(
                "INSERT INTO rulesets(id,trial_id,version,body,created_at)"
                " VALUES(?,?,?,?,?)",
                (_uid("rul"), trial_id, version, json.dumps(body), _now()),
            )
            entry = self.ledger.append("RULESET_AMENDED", trial_id,
                                       {"version": version, "body": body})
            return {"trial_id": trial_id, "version": version, "ledger_hash": entry["hash"]}

    # ------------------------------------------------------------ 队列(创建即冻结)

    def create_cohort(self, trial_id: str, label: str, dose_mg: float,
                      schedule: str, window_days: int, subjects: list[str]) -> dict:
        """创建并冻结队列:给药方案、观察窗、受试者集合自此不可变
        (唯一例外:替补与扩展决策后的增补,均使 state_version 前进)。"""
        self._row("SELECT * FROM trials WHERE id=?", (trial_id,), "试验")
        if not subjects:
            raise InvalidInput("队列至少包含 1 名受试者")
        if len(set(subjects)) != len(subjects):
            raise InvalidInput("受试者编号重复")
        if window_days < 1:
            raise InvalidInput("观察窗至少 1 天")
        with self._tx():
            cid = _uid("coh")
            self._conn.execute(
                "INSERT INTO cohorts(id,trial_id,label,dose_mg,schedule,window_days,"
                " expanded,status,state_version,frozen_at)"
                " VALUES(?,?,?,?,?,?,0,?,1,?)",
                (cid, trial_id, label, dose_mg, schedule, window_days,
                 CohortStatus.ACTIVE.value, _now()),
            )
            for code in subjects:
                self._conn.execute(
                    "INSERT INTO subjects(id,cohort_id,code,status,data_status,enrolled_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (_uid("sub"), cid, code, SubjectStatus.ACTIVE.value,
                     DataStatus.PENDING.value, _now()),
                )
            snap = self._snapshot(cid)
            entry = self.ledger.append("COHORT_FROZEN", cid, {
                "trial_id": trial_id, "label": label, "dose_mg": dose_mg,
                "schedule": schedule, "window_days": window_days,
                "subjects": sorted(subjects), "snapshot_hash": hash_obj(snap),
            })
            return {"id": cid, "state_version": 1, "snapshot_hash": hash_obj(snap),
                    "ledger_hash": entry["hash"]}

    # ------------------------------------------------------------ 给药(事实)

    def record_dose(self, cohort_id: str, subject_code: str,
                    given_at: str | None = None) -> dict:
        """记录给药事实。紧急暂停时立即拒绝;已记录的给药永不修改。"""
        c = self._get_cohort(cohort_id)
        given = _parse_date(given_at, "given_at") if given_at else self._today()
        with self._tx():
            pause = self._active_pause(cohort_id, c["trial_id"])
            if pause:
                raise CohortPaused(
                    f"紧急暂停生效中({pause['reason']}),后续给药已阻断;"
                    f"已给药事实保留", details={"pause_id": pause["id"]})
            if c["status"] != CohortStatus.ACTIVE.value:
                raise Conflict(f"队列状态 {c['status']} 不允许给药")
            sub = self._row(
                "SELECT * FROM subjects WHERE cohort_id=? AND code=?",
                (cohort_id, subject_code), "受试者")
            if sub["status"] != SubjectStatus.ACTIVE.value:
                raise Conflict(f"受试者 {subject_code} 状态 {sub['status']},不可给药")
            did = _uid("dos")
            self._conn.execute(
                "INSERT INTO doses(id,cohort_id,subject_id,dose_mg,given_at,recorded_at)"
                " VALUES(?,?,?,?,?,?)",
                (did, cohort_id, sub["id"], c["dose_mg"], given.isoformat(), _now()),
            )
            self._bump(cohort_id)
            entry = self.ledger.append("DOSE_RECORDED", did, {
                "cohort_id": cohort_id, "subject_code": subject_code,
                "dose_mg": c["dose_mg"], "given_at": given.isoformat(),
            })
            return {"id": did, "dose_mg": c["dose_mg"], "given_at": given.isoformat(),
                    "ledger_hash": entry["hash"]}

    # ------------------------------------------------------------ 安全事件

    def report_event(self, cohort_id: str, subject_code: str, description: str,
                     severity: int, evaluability: str = "PENDING",
                     related: bool = True, occurred_at: str | None = None) -> dict:
        """报告不良事件。观察窗结束后报告的标记为迟到事件;
        无论是否迟到都使 state_version 前进,从而让进行中的会话过期。"""
        c = self._get_cohort(cohort_id)
        if not (1 <= int(severity) <= 5):
            raise InvalidInput("严重度须为 CTCAE 1-5 级")
        try:
            Evaluability(evaluability)
        except ValueError:
            raise InvalidInput(f"非法可评估状态: {evaluability}")
        occurred = _parse_date(occurred_at, "occurred_at") if occurred_at else self._today()
        with self._tx():
            sub = self._row(
                "SELECT * FROM subjects WHERE cohort_id=? AND code=?",
                (cohort_id, subject_code), "受试者")
            view = self._subject_view(c, sub)
            late = bool(view["dosed"] and view["window_complete"])
            eid = _uid("aev")
            self._conn.execute(
                "INSERT INTO events(id,cohort_id,subject_id,description,severity,"
                " evaluability,related,late,occurred_at,reported_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (eid, cohort_id, sub["id"], description, int(severity), evaluability,
                 1 if related else 0, 1 if late else 0,
                 occurred.isoformat(), _now(), _now()),
            )
            self._bump(cohort_id)
            entry = self.ledger.append("AE_REPORTED", eid, {
                "cohort_id": cohort_id, "subject_code": subject_code,
                "severity": int(severity), "evaluability": evaluability,
                "related": bool(related), "late": late,
                "occurred_at": occurred.isoformat(),
            })
            return {"id": eid, "late": late, "ledger_hash": entry["hash"]}

    def update_event(self, event_id: str, severity: int | None = None,
                     evaluability: str | None = None,
                     related: bool | None = None) -> dict:
        """更新事件(如可评估性判定完成)。同样使 state_version 前进。"""
        e = self._row("SELECT * FROM events WHERE id=?", (event_id,), "不良事件")
        if evaluability is not None:
            try:
                Evaluability(evaluability)
            except ValueError:
                raise InvalidInput(f"非法可评估状态: {evaluability}")
        if severity is not None and not (1 <= int(severity) <= 5):
            raise InvalidInput("严重度须为 CTCAE 1-5 级")
        with self._tx():
            self._conn.execute(
                "UPDATE events SET severity=?, evaluability=?, related=?, updated_at=?"
                " WHERE id=?",
                (int(severity) if severity is not None else e["severity"],
                 evaluability or e["evaluability"],
                 (1 if related else 0) if related is not None else e["related"],
                 _now(), event_id),
            )
            self._bump(e["cohort_id"])
            entry = self.ledger.append("AE_UPDATED", event_id, {
                "cohort_id": e["cohort_id"], "severity": severity,
                "evaluability": evaluability, "related": related,
            })
            return {"id": event_id, "ledger_hash": entry["hash"]}

    # ------------------------------------------------------------ 受试者:替补 / 增补 / 缺失数据

    def replace_subject(self, cohort_id: str, subject_code: str,
                        new_code: str, reason: str = "") -> dict:
        """受试者替补:原受试者标记 REPLACED(其给药/事件事实保留),
        新受试者入组。state_version 前进。"""
        c = self._get_cohort(cohort_id)
        with self._tx():
            sub = self._row(
                "SELECT * FROM subjects WHERE cohort_id=? AND code=?",
                (cohort_id, subject_code), "受试者")
            if sub["status"] != SubjectStatus.ACTIVE.value:
                raise Conflict(f"受试者 {subject_code} 状态 {sub['status']},不可替补")
            if self._conn.execute(
                "SELECT 1 FROM subjects WHERE cohort_id=? AND code=?",
                (cohort_id, new_code),
            ).fetchone():
                raise Conflict(f"受试者编号 {new_code} 已存在")
            new_id = _uid("sub")
            self._conn.execute(
                "INSERT INTO subjects(id,cohort_id,code,status,data_status,"
                " replacement_of,enrolled_at) VALUES(?,?,?,?,?,?,?)",
                (new_id, cohort_id, new_code, SubjectStatus.ACTIVE.value,
                 DataStatus.PENDING.value, sub["id"], _now()),
            )
            self._conn.execute(
                "UPDATE subjects SET status=?, replaced_by=? WHERE id=?",
                (SubjectStatus.REPLACED.value, new_id, sub["id"]),
            )
            self._bump(cohort_id)
            entry = self.ledger.append("SUBJECT_REPLACED", sub["id"], {
                "cohort_id": cohort_id, "replaced": subject_code,
                "replacement": new_code, "reason": reason,
            })
            return {"replaced": subject_code, "replacement": new_code,
                    "ledger_hash": entry["hash"]}

    def add_subjects(self, cohort_id: str, codes: list[str]) -> dict:
        """扩展决策后增补受试者。队列冻结期间禁止增补。"""
        c = self._get_cohort(cohort_id)
        if not c["expanded"]:
            raise Frozen("队列受试者集合已冻结;仅扩展(EXPAND)决策后可增补")
        with self._tx():
            for code in codes:
                if self._conn.execute(
                    "SELECT 1 FROM subjects WHERE cohort_id=? AND code=?",
                    (cohort_id, code),
                ).fetchone():
                    raise Conflict(f"受试者编号 {code} 已存在")
                self._conn.execute(
                    "INSERT INTO subjects(id,cohort_id,code,status,data_status,enrolled_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (_uid("sub"), cohort_id, code, SubjectStatus.ACTIVE.value,
                     DataStatus.PENDING.value, _now()),
                )
            self._bump(cohort_id)
            entry = self.ledger.append("SUBJECTS_ADDED", cohort_id, {"codes": codes})
            return {"added": codes, "ledger_hash": entry["hash"]}

    def set_data_status(self, cohort_id: str, subject_code: str, status: str) -> dict:
        """缺失数据处理:标记数据完整或重新挂起。"""
        self._get_cohort(cohort_id)
        try:
            DataStatus(status)
        except ValueError:
            raise InvalidInput(f"非法数据状态: {status}")
        with self._tx():
            sub = self._row(
                "SELECT * FROM subjects WHERE cohort_id=? AND code=?",
                (cohort_id, subject_code), "受试者")
            self._conn.execute(
                "UPDATE subjects SET data_status=? WHERE id=?", (status, sub["id"]))
            self._bump(cohort_id)
            entry = self.ledger.append("DATA_STATUS_SET", sub["id"], {
                "cohort_id": cohort_id, "subject_code": subject_code, "status": status,
            })
            return {"subject_code": subject_code, "status": status,
                    "ledger_hash": entry["hash"]}

    # ------------------------------------------------------------ 紧急暂停

    def pause(self, scope: str, scope_id: str, reason: str) -> dict:
        """紧急暂停:立即阻断后续给药(本方法提交后即生效);
        已给药事实全部保留。进行中的 ESCALATE/EXPAND 会话立即作废。"""
        if scope not in ("COHORT", "TRIAL"):
            raise InvalidInput("scope 须为 COHORT 或 TRIAL")
        with self._tx():
            if scope == "COHORT":
                self._get_cohort(scope_id)
            else:
                self._row("SELECT * FROM trials WHERE id=?", (scope_id,), "试验")
            if self._conn.execute(
                "SELECT 1 FROM pauses WHERE active=1 AND scope=? AND scope_id=?",
                (scope, scope_id),
            ).fetchone():
                raise Conflict("暂停已生效")
            pid = _uid("pau")
            self._conn.execute(
                "INSERT INTO pauses(id,scope,scope_id,reason,active,paused_at)"
                " VALUES(?,?,?,?,1,?)",
                (pid, scope, scope_id, reason, _now()),
            )
            # 暂停生效即作废同向的递增/扩展会话;STOP/HOLD 会话保留
            if scope == "COHORT":
                rows = self._conn.execute(
                    "SELECT id,intent FROM sessions WHERE cohort_id=? AND status='OPEN'",
                    (scope_id,)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT s.id,s.intent FROM sessions s JOIN cohorts c ON c.id=s.cohort_id"
                    " WHERE c.trial_id=? AND s.status='OPEN'", (scope_id,)).fetchall()
            superseded = []
            for r in rows:
                if r["intent"] in (Intent.ESCALATE.value, Intent.EXPAND.value):
                    self._conn.execute(
                        "UPDATE sessions SET status=?, closed_at=? WHERE id=?",
                        (SessionStatus.SUPERSEDED.value, _now(), r["id"]))
                    superseded.append(r["id"])
            entry = self.ledger.append("EMERGENCY_PAUSE", pid, {
                "scope": scope, "scope_id": scope_id, "reason": reason,
                "superseded_sessions": superseded,
            })
            return {"id": pid, "superseded_sessions": superseded,
                    "ledger_hash": entry["hash"]}

    def resume(self, scope: str, scope_id: str) -> dict:
        with self._tx():
            p = self._conn.execute(
                "SELECT * FROM pauses WHERE active=1 AND scope=? AND scope_id=?",
                (scope, scope_id)).fetchone()
            if not p:
                raise NotFound("无生效中的暂停")
            self._conn.execute(
                "UPDATE pauses SET active=0, resumed_at=? WHERE id=?",
                (_now(), p["id"]))
            entry = self.ledger.append("RESUMED", p["id"],
                                       {"scope": scope, "scope_id": scope_id})
            return {"resumed": True, "ledger_hash": entry["hash"]}

    # ------------------------------------------------------------ 快照与视图

    def _snapshot(self, cohort_id: str) -> dict:
        """决策快照:冻结配置 + 受试者(含给药事实)+ 事件 + 规则版本。
        同一逻辑状态在任何时刻重建都得到同一哈希。"""
        c = self._get_cohort(cohort_id)
        rs = self._current_ruleset(c["trial_id"])
        subjects = []
        for s in self._conn.execute(
            "SELECT * FROM subjects WHERE cohort_id=? ORDER BY code", (cohort_id,)
        ).fetchall():
            doses = [
                {"given_at": d["given_at"], "dose_mg": d["dose_mg"]}
                for d in self._conn.execute(
                    "SELECT * FROM doses WHERE subject_id=? ORDER BY given_at", (s["id"],)
                ).fetchall()
            ]
            subjects.append({
                "code": s["code"], "status": s["status"],
                "data_status": s["data_status"],
                "replacement_of": s["replacement_of"],
                "replaced_by": s["replaced_by"], "doses": doses,
            })
        events = [{
            "id": e["id"], "subject_code": self._subject_code(e["subject_id"]),
            "severity": e["severity"], "evaluability": e["evaluability"],
            "related": bool(e["related"]), "late": bool(e["late"]),
            "occurred_at": e["occurred_at"], "reported_at": e["reported_at"],
        } for e in self._conn.execute(
            "SELECT * FROM events WHERE cohort_id=? ORDER BY id", (cohort_id,)
        ).fetchall()]
        return {
            "cohort": {
                "id": c["id"], "trial_id": c["trial_id"], "label": c["label"],
                "dose_mg": c["dose_mg"], "schedule": c["schedule"],
                "window_days": c["window_days"], "expanded": bool(c["expanded"]),
                "status": c["status"],
            },
            "state_version": c["state_version"],
            "rule_version": rs["version"],
            "subjects": subjects,
            "events": events,
        }

    def _subject_code(self, subject_id: str) -> str:
        return self._conn.execute(
            "SELECT code FROM subjects WHERE id=?", (subject_id,)).fetchone()["code"]

    def _subject_view(self, cohort_row, subject_row) -> dict:
        """单个受试者的规则引擎视图。"""
        doses = self._conn.execute(
            "SELECT * FROM doses WHERE subject_id=? ORDER BY given_at",
            (subject_row["id"],)).fetchall()
        dosed = len(doses) > 0
        window_end = None
        if dosed:
            last = max(date.fromisoformat(d["given_at"]) for d in doses)
            window_end = last + timedelta(days=cohort_row["window_days"])
        window_complete = bool(window_end and self._today() >= window_end)
        rs = json.loads(
            self._current_ruleset(cohort_row["trial_id"])["body"])
        evts = [{
            "severity": e["severity"], "evaluability": e["evaluability"],
            "related": bool(e["related"]),
        } for e in self._conn.execute(
            "SELECT * FROM events WHERE subject_id=?", (subject_row["id"],)).fetchall()]
        dlt = any(rule_engine.is_dlt(rs, e) for e in evts)
        replaced = subject_row["status"] == SubjectStatus.REPLACED.value
        # 被替补且无 DLT、未完成观察窗者不计入可评估集合(其名额由替补者承担);
        # 发生过 DLT 或已完成窗的,数据仍是有效事实,继续计入。
        counts = (not replaced) or dlt or window_complete
        return {
            "code": subject_row["code"], "status": subject_row["status"],
            "data_status": subject_row["data_status"],
            "dosed": dosed, "dose_count": len(doses),
            "window_end": window_end.isoformat() if window_end else None,
            "window_complete": window_complete, "dlt": dlt, "counts": counts,
            "events": evts,
        }

    def _subject_views(self, cohort_row) -> list[dict]:
        return [
            self._subject_view(cohort_row, s)
            for s in self._conn.execute(
                "SELECT * FROM subjects WHERE cohort_id=? ORDER BY code",
                (cohort_row["id"],)).fetchall()
        ]

    def _blockers(self, cohort_row, views: list[dict]) -> list[str]:
        """决策前置条件:观察窗完整 + 缺失数据处理完毕 + 无待定事件。"""
        out = []
        for v in views:
            if not v["counts"]:
                continue
            if not v["dosed"]:
                out.append(f"受试者 {v['code']} 尚无给药记录")
            elif not v["window_complete"]:
                out.append(
                    f"受试者 {v['code']} 观察窗未结束(至 {v['window_end']})")
            if v["data_status"] != DataStatus.COMPLETE.value:
                out.append(f"受试者 {v['code']} 存在未处理的缺失数据")
        pending = self._conn.execute(
            "SELECT e.id, s.code FROM events e JOIN subjects s ON s.id=e.subject_id"
            " WHERE e.cohort_id=? AND e.evaluability='PENDING'",
            (cohort_row["id"],)).fetchall()
        for p in pending:
            out.append(f"不良事件 {p['id']}(受试者 {p['code']})可评估性待定")
        return out

    def snapshot_view(self, cohort_id: str) -> dict:
        """安全工作台视图:队列状态 + 受试者 + 事件 + 阻断项 + 规则评估。"""
        with self._lock:
            c = self._get_cohort(cohort_id)
            rs = self._current_ruleset(c["trial_id"])
            views = self._subject_views(c)
            blockers = self._blockers(c, views)
            evaluation = rule_engine.evaluate(
                json.loads(rs["body"]), views, bool(c["expanded"]))
            snap = self._snapshot(cohort_id)
            pause = self._active_pause(cohort_id, c["trial_id"])
            open_session = self._conn.execute(
                "SELECT * FROM sessions WHERE cohort_id=? AND status='OPEN'",
                (cohort_id,)).fetchone()
            trial = self._row("SELECT * FROM trials WHERE id=?", (c["trial_id"],), "试验")
            committee = [dict(m) for m in self._conn.execute(
                "SELECT * FROM committee_members WHERE trial_id=? AND active=1",
                (c["trial_id"],)).fetchall()]
            return {
                "cohort": snap["cohort"],
                "state_version": c["state_version"],
                "rule_version": rs["version"],
                "snapshot_hash": hash_obj(snap),
                "paused": dict(pause) if pause else None,
                "subjects": views,
                "events": snap["events"],
                "blockers": blockers,
                "evaluation": evaluation,
                "open_session": dict(open_session) if open_session else None,
                "committee": committee,
                "quorum": trial["quorum"],
                "approve_threshold": trial["approve_threshold"],
            }

    # ------------------------------------------------------------ 决策会话与投票

    def open_session(self, cohort_id: str, intent: str) -> dict:
        """开启决策会话:检查前置条件,钉住当前快照。
        只有观察窗完整、缺失数据处理完毕、规则允许,会话才能开启。"""
        try:
            Intent(intent)
        except ValueError:
            raise InvalidInput(f"非法决策意图: {intent}")
        with self._tx():
            c = self._get_cohort(cohort_id)
            if c["status"] not in (CohortStatus.ACTIVE.value,):
                raise Conflict(f"队列状态 {c['status']} 不允许开启决策会话")
            if self._conn.execute(
                "SELECT 1 FROM sessions WHERE cohort_id=? AND status='OPEN'",
                (cohort_id,)).fetchone():
                raise Conflict("已存在进行中的决策会话")
            pause = self._active_pause(cohort_id, c["trial_id"])
            if pause and intent not in (Intent.STOP.value, Intent.HOLD.value):
                raise PreconditionFailed(
                    "紧急暂停期间仅允许 STOP / HOLD 决策",
                    details={"pause": dict(pause)})
            views = self._subject_views(c)
            blockers = self._blockers(c, views)
            if blockers:
                raise PreconditionFailed(
                    "决策前置条件未满足", details={"blockers": blockers})
            rs = self._current_ruleset(c["trial_id"])
            evaluation = rule_engine.evaluate(
                json.loads(rs["body"]), views, bool(c["expanded"]))
            if intent not in evaluation["permitted"]:
                raise PreconditionFailed(
                    f"规则不允许 {intent}:{'; '.join(evaluation['rationale'])}",
                    details={"evaluation": evaluation})
            trial = self._row(
                "SELECT * FROM trials WHERE id=?", (c["trial_id"],), "试验")
            snap = self._snapshot(cohort_id)
            sid = _uid("ses")
            self._conn.execute(
                "INSERT INTO sessions(id,cohort_id,intent,status,state_version,"
                " rule_version,snapshot_hash,quorum,approve_threshold,"
                " snapshot_json,evaluation_json,opened_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, cohort_id, intent, SessionStatus.OPEN.value,
                 c["state_version"], rs["version"], hash_obj(snap),
                 trial["quorum"], trial["approve_threshold"],
                 json.dumps(snap, ensure_ascii=False),
                 json.dumps(evaluation, ensure_ascii=False), _now()),
            )
            entry = self.ledger.append("SESSION_OPENED", sid, {
                "cohort_id": cohort_id, "intent": intent,
                "state_version": c["state_version"], "rule_version": rs["version"],
                "snapshot_hash": hash_obj(snap), "evaluation": evaluation,
            })
            return {"id": sid, "intent": intent,
                    "state_version": c["state_version"],
                    "rule_version": rs["version"],
                    "snapshot_hash": hash_obj(snap),
                    "quorum": trial["quorum"],
                    "approve_threshold": trial["approve_threshold"],
                    "evaluation": evaluation, "ledger_hash": entry["hash"]}

    def vote(self, session_id: str, member_id: str, vote: str,
             seen_state_version: int | None = None) -> dict:
        """委员投票。整个方法在单一事务中:
        校验快照未漂移 -> 记录投票 -> 达到法定人数即定稿,
        并发投票在此串行,结论只可能来自同一快照。"""
        try:
            Vote(vote)
        except ValueError:
            raise InvalidInput(f"非法投票: {vote}")
        with self._tx():
            s = self._row("SELECT * FROM sessions WHERE id=?", (session_id,), "决策会话")
            if s["status"] != SessionStatus.OPEN.value:
                raise Conflict(f"会话状态 {s['status']},不可投票")
            c = self._get_cohort(s["cohort_id"])
            if c["state_version"] != s["state_version"]:
                raise StaleSnapshot(
                    f"队列状态已变更(v{s['state_version']} -> v{c['state_version']}),"
                    f"会话快照过期;请关闭会话并基于新快照重新开启",
                    details={"session_state_version": s["state_version"],
                             "current_state_version": c["state_version"]})
            rs = self._current_ruleset(c["trial_id"])
            if rs["version"] != s["rule_version"]:
                raise StaleSnapshot(
                    f"规则已修订({s['rule_version']} -> {rs['version']}),"
                    f"会话快照过期;请关闭会话并基于新规则重新开启",
                    details={"session_rule_version": s["rule_version"],
                             "current_rule_version": rs["version"]})
            if seen_state_version is not None and seen_state_version != s["state_version"]:
                raise StaleSnapshot(
                    f"投票基于过期快照(v{seen_state_version}),会话钉住的是"
                    f" v{s['state_version']};请刷新快照后重新投票",
                    details={"session_state_version": s["state_version"]})
            member = self._conn.execute(
                "SELECT * FROM committee_members WHERE trial_id=? AND member_id=?"
                " AND active=1", (c["trial_id"], member_id)).fetchone()
            if not member:
                raise NotFound(f"委员 {member_id} 不存在或已停用")
            try:
                self._conn.execute(
                    "INSERT INTO votes(id,session_id,member_id,vote,"
                    " state_version_seen,cast_at) VALUES(?,?,?,?,?,?)",
                    (_uid("vot"), session_id, member_id, vote,
                     s["state_version"], _now()),
                )
            except Exception as exc:
                if "UNIQUE" in str(exc):
                    raise DuplicateVote(f"委员 {member_id} 在本会话已投票")
                raise
            self.ledger.append("VOTE_CAST", session_id, {
                "member_id": member_id, "vote": vote,
                "state_version": s["state_version"],
                "snapshot_hash": s["snapshot_hash"],
            })
            votes = self._conn.execute(
                "SELECT * FROM votes WHERE session_id=?", (session_id,)).fetchall()
            total = len(votes)
            approvals = sum(1 for v in votes if v["vote"] == Vote.APPROVE.value)
            decided = None
            if total >= s["quorum"]:
                # 法定人数达成:在同一事务内定稿,会话最多被 finalize 一次
                outcome = s["intent"] if approvals >= s["approve_threshold"] else "REJECTED"
                self._finalize(s, c, votes, outcome)
                decided = outcome
            return {"session_id": session_id, "member_id": member_id,
                    "votes_cast": total, "approvals": approvals,
                    "quorum": s["quorum"], "decided": decided}

    def _finalize(self, s, c, votes, outcome: str):
        """定稿:应用队列状态变迁,写入含完整快照的决策证明。"""
        now = _now()
        self._conn.execute(
            "UPDATE sessions SET status=?, outcome=?, closed_at=? WHERE id=?",
            (SessionStatus.DECIDED.value, outcome, now, s["id"]))
        new_status = None
        if outcome == Intent.ESCALATE.value:
            new_status = CohortStatus.ESCALATED.value
        elif outcome == Intent.STOP.value:
            new_status = CohortStatus.STOPPED.value
        elif outcome == Intent.EXPAND.value:
            self._conn.execute(
                "UPDATE cohorts SET expanded=1 WHERE id=?", (c["id"],))
            self._bump(c["id"])
        if new_status:
            self._conn.execute(
                "UPDATE cohorts SET status=? WHERE id=?", (new_status, c["id"]))
            self._bump(c["id"])
        # 证明必须内嵌开会时钉住的快照与规则评估,而非定稿时的现场状态:
        # 决策结论只对那一个快照负责。
        snap = json.loads(s["snapshot_json"])
        evaluation = json.loads(s["evaluation_json"])
        proof = {
            "session_id": s["id"], "cohort_id": c["id"], "trial_id": c["trial_id"],
            "intent": s["intent"], "outcome": outcome,
            "state_version": s["state_version"],
            "rule_version": s["rule_version"],
            "snapshot_hash": s["snapshot_hash"],
            "snapshot": snap,
            "rule_evaluation": evaluation,
            "votes": [
                {"member_id": v["member_id"], "vote": v["vote"],
                 "state_version_seen": v["state_version_seen"],
                 "cast_at": v["cast_at"]}
                for v in votes
            ],
            "quorum": s["quorum"], "approve_threshold": s["approve_threshold"],
            "decided_at": now,
        }
        self.ledger.append("DECISION_FINALIZED", s["id"], proof)

    def close_session(self, session_id: str) -> dict:
        """关闭(作废)会话:快照过期后显式关闭,再基于新快照重开。"""
        with self._tx():
            s = self._row("SELECT * FROM sessions WHERE id=?", (session_id,), "决策会话")
            if s["status"] != SessionStatus.OPEN.value:
                raise Conflict(f"会话状态 {s['status']},不可关闭")
            self._conn.execute(
                "UPDATE sessions SET status=?, closed_at=? WHERE id=?",
                (SessionStatus.SUPERSEDED.value, _now(), session_id))
            entry = self.ledger.append("SESSION_CLOSED", session_id,
                                       {"cohort_id": s["cohort_id"]})
            return {"id": session_id, "status": SessionStatus.SUPERSEDED.value,
                    "ledger_hash": entry["hash"]}

    def get_session(self, session_id: str) -> dict:
        with self._lock:
            s = self._row("SELECT * FROM sessions WHERE id=?", (session_id,), "决策会话")
            c = self._get_cohort(s["cohort_id"])
            rs = self._current_ruleset(c["trial_id"])
            votes = [dict(v) for v in self._conn.execute(
                "SELECT * FROM votes WHERE session_id=? ORDER BY cast_at",
                (session_id,)).fetchall()]
            stale_reasons = []
            if s["status"] == SessionStatus.OPEN.value:
                if c["state_version"] != s["state_version"]:
                    stale_reasons.append(
                        f"队列状态 v{s['state_version']} -> v{c['state_version']}")
                if rs["version"] != s["rule_version"]:
                    stale_reasons.append(
                        f"规则 {s['rule_version']} -> {rs['version']}")
            return {**dict(s), "votes": votes, "stale_reasons": stale_reasons}

    # ------------------------------------------------------------ 决策证明

    def list_proofs(self, cohort_id: str) -> list[dict]:
        with self._lock:
            return [
                {"seq": e["seq"], "hash": e["hash"], "ts": e["ts"],
                 "session_id": e["payload"]["session_id"],
                 "intent": e["payload"]["intent"],
                 "outcome": e["payload"]["outcome"],
                 "snapshot_hash": e["payload"]["snapshot_hash"],
                 "rule_version": e["payload"]["rule_version"]}
                for e in self.ledger.entries("DECISION_FINALIZED")
                if e["payload"].get("cohort_id") == cohort_id
            ]

    def get_proof(self, session_id: str) -> dict:
        with self._lock:
            for e in self.ledger.entries("DECISION_FINALIZED"):
                if e["payload"].get("session_id") == session_id:
                    return e
            raise NotFound(f"会话 {session_id} 尚无决策证明")

    def verify_proof(self, session_id: str) -> dict:
        """独立验证决策证明:账本条目哈希自洽 + 内嵌快照哈希自洽。"""
        with self._lock:
            e = self.get_proof(session_id)
            body = {k: e[k] for k in ("seq", "ts", "kind", "ref_id", "payload", "prev_hash")}
            from .domain import canonical, sha256_hex
            entry_ok = sha256_hex(canonical(body)) == e["hash"]
            snap_ok = hash_obj(e["payload"]["snapshot"]) == e["payload"]["snapshot_hash"]
            chain = self.ledger.verify()
            return {
                "session_id": session_id,
                "entry_hash_ok": entry_ok,
                "snapshot_hash_ok": snap_ok,
                "ledger_chain_ok": chain["ok"],
                "ok": entry_ok and snap_ok and chain["ok"],
                "proof_hash": e["hash"],
            }
