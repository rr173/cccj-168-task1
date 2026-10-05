# 剂量递增安全决策平台

早期临床试验(Phase I)剂量递增的安全决策系统。每个队列的给药方案、观察窗、
受试者集合在创建时**冻结**;安全事件按严重度与可评估状态进入**规则引擎**;
只有**观察窗完整、缺失数据处理完毕、独立安全委员会达到门槛**,才能递增、
扩展或停止。所有决策产出可独立验证的**决策证明**(哈希链账本)。

## 快速开始

```bash
pip install -r requirements.txt
python3 -m pytest tests/ -q     # 29 个一致性测试
python3 -m app.demo             # 端到端故事线演示
python3 run.py                  # http://127.0.0.1:8000 安全工作台
```

## 一致性模型(核心设计)

平台要防止的根本风险:**把不同快照拼成结论**。

1. **单调状态版本**:每个队列有 `state_version`,任何影响决策输入的变更
   (给药、迟到不良事件、事件更新、受试者替补/增补、缺失数据处理)都使其 +1。
2. **决策会话钉住快照**:开启会话时固化
   `(state_version, rule_version, snapshot_hash, 完整快照JSON)`。
   此后每一票都在单事务内校验这三个锚点——
   - 迟到 AE 到达 → 版本前进 → 旧会话上的后续投票返回 `409 STALE_SNAPSHOT`;
   - 规则修订 → `rule_version` 不符 → 同样拒绝;
   - 两名委员并发投票 → `RLock + BEGIN IMMEDIATE` 串行化,各记一票,
     法定人数在同一事务内判定,会话**最多定稿一次**;
   - 投票还可携带 `seen_state_version`,委员基于过期页面的投票同样被拒。
3. **证明只对钉住的快照负责**:定稿时证明内嵌的是开会时固化的快照,
   而非定稿时的现场状态;`snapshot_hash` 可独立重算验证。
4. **紧急暂停**:提交即生效,后续给药写入立即被拒绝(`409 COHORT_PAUSED`);
   已给药事实永不修改、保留在快照中;进行中的 ESCALATE/EXPAND 会话立即作废,
   但允许委员会在暂停期间正式作出 STOP/HOLD 决定。

## 决策门控

开启决策会话(递增/扩展/停止/暂缓)的前置条件,缺一不可:

| 条件 | 实现 |
|---|---|
| 观察窗完整 | 每名计入评估的受试者 `末次给药 + window_days <= 今日` |
| 缺失数据处理完毕 | 受试者 `data_status=COMPLETE` 且无 `PENDING` 事件 |
| 规则允许 | 规则引擎(默认 3+3)输出的允许动作包含该意图 |
| 委员会达门槛 | 投票数 ≥ 法定人数且赞成票 ≥ 赞成门槛,单事务内定稿 |

默认 3+3 规则(`rulesets` 表,版本化,可修订):

```
0/3 DLT          -> 允许 ESCALATE
1/3 DLT          -> 允许 EXPAND(扩至 6 例)
>=2 DLT          -> 仅允许 STOP/HOLD
扩展后 <=1/6 DLT -> 允许 ESCALATE
DLT = CTCAE >= 3 级 且 可评估(EVALUABLE) 且 与研究药物相关
```

## 决策证明

每次定稿向账本追加一条 `DECISION_FINALIZED`,内容为自包含证明:
钉住的完整快照、快照哈希、规则版本与评估结果、全部投票(含各票钉住的
快照版本)、法定人数与门槛、结果。账本为哈希链
(`hash = sha256(canonical({seq, ts, kind, ref_id, payload, prev_hash}))`),
任何篡改都会在 `GET /api/ledger/verify` 处暴露。

## API 概览

```
POST /api/trials                              创建试验(委员会、法定人数、门槛、初始规则)
POST /api/trials/{id}/rulesets                规则修订(产生新版本,旧会话即过期)
POST /api/trials/{id}/pause|resume            试验级紧急暂停/恢复
POST /api/cohorts                             创建并冻结队列(方案/观察窗/受试者)
GET  /api/cohorts/{id}/snapshot               安全工作台视图(含阻断项与规则评估)
POST /api/cohorts/{id}/doses                  记录给药(暂停时 409)
POST /api/cohorts/{id}/events                 报告不良事件(迟到自动标记)
PATCH /api/events/{id}                        更新事件(可评估性判定等)
POST /api/cohorts/{id}/subjects/{code}/replace  受试者替补
POST /api/cohorts/{id}/subjects               扩展决策后增补受试者
POST /api/cohorts/{id}/subjects/{code}/data-status  缺失数据处理
POST /api/cohorts/{id}/pause|resume           队列级紧急暂停/恢复
POST /api/cohorts/{id}/sessions               开启决策会话(钉住快照)
POST /api/sessions/{id}/votes                 委员投票(并发安全)
POST /api/sessions/{id}/close                 关闭过期会话
GET  /api/sessions/{id}                       会话状态(含过期原因)
GET  /api/cohorts/{id}/proofs                 队列全部决策证明
GET  /api/proofs/{session_id}[/verify]        获取/独立验证决策证明
GET  /api/ledger[/verify]                     账本导出/链条校验
GET  /                                        安全工作台(单页应用)
```

## 错误语义

| HTTP | code | 含义 |
|---|---|---|
| 409 | `STALE_SNAPSHOT` | 快照已过期(迟到事件/替补/数据处理/规则修订),禁止拼接结论 |
| 409 | `COHORT_PAUSED` | 紧急暂停生效,给药被阻断 |
| 409 | `DUPLICATE_VOTE` | 委员在本会话已投票 |
| 409 | `COHORT_FROZEN` | 违反冻结约束(如未扩展先增补) |
| 422 | `PRECONDITION_FAILED` | 决策前置条件未满足(附阻断项清单) |

## 目录结构

```
app/
  domain.py     枚举、错误、规范化哈希
  store.py      SQLite 模式(单连接 + 显式事务)
  rules.py      版本化规则引擎(默认 3+3)
  ledger.py     哈希链账本
  service.py    业务核心:快照一致性、门控、投票、暂停
  api.py        队列 API(FastAPI)
  workbench.py  安全工作台(单页 HTML)
  demo.py       端到端演示
tests/test_platform.py  29 个一致性测试
```
