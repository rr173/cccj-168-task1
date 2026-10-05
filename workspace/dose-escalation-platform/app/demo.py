"""端到端演示:走一遍完整的安全决策故事线。

  python3 -m app.demo

场景:
  1. 创建试验 + 独立安全委员会(3 人,法定人数 3,赞成门槛 2)
  2. 冻结队列 C1(10mg,28 天观察窗,3 例受试者)
  3. 给药 -> 缺失数据挂起 -> 数据补齐 -> 观察窗完整
  4. 开启递增决策会话(钉住快照)
  5. 迟到不良事件到达 -> 旧会话立即过期,投票被拒
  6. 关闭旧会话,基于新快照重开 -> 委员会投票 -> 递增定稿
  7. 验证决策证明与哈希链账本
  8. 下一队列:紧急暂停阻断给药、保留已给药事实
"""
from __future__ import annotations

from datetime import date, timedelta

from .domain import CohortPaused, StaleSnapshot
from .service import SafetyPlatform


class Clock:
    def __init__(self, d):
        self.d = d

    def __call__(self):
        return self.d

    def advance(self, days):
        self.d += timedelta(days=days)


def line(title):
    print(f"\n{'=' * 64}\n{title}\n{'=' * 64}")


def main():
    clock = Clock(date(2026, 1, 5))
    pf = SafetyPlatform(clock=clock)

    line("1. 创建试验与独立安全委员会")
    t = pf.create_trial("ABC-101 首次人体剂量递增试验", "I",
                        [{"id": "dsmb1", "name": "张委员"},
                         {"id": "dsmb2", "name": "李委员"},
                         {"id": "dsmb3", "name": "王委员"}],
                        quorum=3, approve_threshold=2)
    print(f"试验 {t['id']}  法定人数 {t['quorum']}  赞成门槛 {t['approve_threshold']}")

    line("2. 冻结队列 C1(方案 / 观察窗 / 受试者集合)")
    c = pf.create_cohort(t["id"], "C1", 10.0, "QD x 7d", 28, ["S01", "S02", "S03"])
    print(f"队列 {c['id']}  快照哈希 {c['snapshot_hash'][:24]}…")

    line("3. 给药 -> 缺失数据 -> 数据补齐 -> 观察窗完整")
    for code in ("S01", "S02", "S03"):
        pf.record_dose(c["id"], code, clock().isoformat())
        print(f"  {code} 给药 10mg @ {clock()}")
    try:
        pf.open_session(c["id"], "ESCALATE")
    except Exception as e:
        print(f"  缺失数据未处理,禁止开启决策会话: {e}")
    for code in ("S01", "S02", "S03"):
        pf.set_data_status(c["id"], code, "COMPLETE")
    clock.advance(28)
    print(f"  数据已补齐,时钟推进至 {clock()}(观察窗完整)")

    line("4. 开启递增决策会话(钉住快照)")
    s = pf.open_session(c["id"], "ESCALATE")
    print(f"  会话 {s['id']}  钉住 v{s['state_version']} / 规则 {s['rule_version']}"
          f" / 快照 {s['snapshot_hash'][:24]}…")
    pf.vote(s["id"], "dsmb1", "APPROVE")
    print("  dsmb1 已投赞成票")

    line("5. 迟到不良事件到达 -> 旧快照立即失效")
    r = pf.report_event(c["id"], "S02", "迟发型皮疹(2级)", 2,
                        evaluability="EVALUABLE", related=True)
    print(f"  事件已记录(late={r['late']}),队列状态前进")
    try:
        pf.vote(s["id"], "dsmb2", "APPROVE")
    except StaleSnapshot as e:
        print(f"  dsmb2 投票被拒: {e}")
    pf.close_session(s["id"])
    print("  旧会话已关闭(作废),不得把两个快照拼成结论")

    line("6. 基于新快照重开会话 -> 委员会达门槛 -> 递增定稿")
    s2 = pf.open_session(c["id"], "ESCALATE")
    for m in ("dsmb1", "dsmb2", "dsmb3"):
        r = pf.vote(s2["id"], m, "APPROVE")
        print(f"  {m} 投票({r['votes_cast']}/{r['quorum']})"
              + (f" -> 定稿 {r['decided']}" if r["decided"] else ""))

    line("7. 验证决策证明与账本")
    v = pf.verify_proof(s2["id"])
    print(f"  证明验证: 条目哈希 {v['entry_hash_ok']}  快照哈希 {v['snapshot_hash_ok']}"
          f"  账本链 {v['ledger_chain_ok']}")
    proof = pf.get_proof(s2["id"])
    print(f"  证明哈希 {proof['hash'][:32]}…  结果 {proof['payload']['outcome']}")
    chain = pf.ledger.verify()
    print(f"  账本链: {chain['entries']} 条记录,完整={chain['ok']}")

    line("8. 下一队列:紧急暂停阻断给药,保留已给药事实")
    c2 = pf.create_cohort(t["id"], "C2", 20.0, "QD x 7d", 28, ["S11", "S12", "S13"])
    pf.record_dose(c2["id"], "S11", clock().isoformat())
    pf.pause("COHORT", c2["id"], "S11 出现 2 级过敏样反应,待 DSMB 评估")
    try:
        pf.record_dose(c2["id"], "S12", clock().isoformat())
    except CohortPaused as e:
        print(f"  S12 给药被阻断: {e}")
    view = pf.snapshot_view(c2["id"])
    s11 = next(x for x in view["subjects"] if x["code"] == "S11")
    print(f"  已给药事实保留: S11 给药 {s11['dose_count']} 次")
    pf.resume("COHORT", c2["id"])
    pf.record_dose(c2["id"], "S12", clock().isoformat())
    print("  暂停解除后 S12 给药成功")
    print(f"\n最终账本校验: {pf.ledger.verify()}")


if __name__ == "__main__":
    main()
