"""安全工作台:单页 HTML,直接操作队列 API。

面板:队列总览 / 给药与事件录入 / 决策与投票 / 决策证明与账本验证。
所有结论都标注其快照版本与哈希,过期会话会被醒目标记。
"""

WORKBENCH_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>剂量递增安全决策平台 · 安全工作台</title>
<style>
  :root { --bd:#d7dde5; --ok:#0a7d3b; --bad:#b3261e; --warn:#9a6a00; --bg:#f6f8fa; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
         margin: 0; background: var(--bg); color: #1c2430; font-size: 14px; }
  header { background: #10233f; color: #fff; padding: 10px 20px; display: flex;
           align-items: center; gap: 16px; }
  header h1 { font-size: 16px; margin: 0; }
  header .sp { flex: 1; }
  main { padding: 16px 20px; display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  .card { background: #fff; border: 1px solid var(--bd); border-radius: 8px; padding: 14px; }
  .card h2 { font-size: 14px; margin: 0 0 10px; color: #10233f; }
  .full { grid-column: 1 / -1; }
  table { border-collapse: collapse; width: 100%; }
  th, td { border: 1px solid var(--bd); padding: 4px 8px; text-align: left; font-size: 13px; }
  th { background: #eef2f7; }
  input, select, button { font: inherit; padding: 4px 8px; margin: 2px 2px; }
  button { border: 1px solid var(--bd); background: #fff; border-radius: 5px; cursor: pointer; }
  button.primary { background: #10233f; color: #fff; border-color: #10233f; }
  button.danger { background: var(--bad); color: #fff; border-color: var(--bad); }
  .badge { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px;
           background: #e6ebf2; margin: 1px; }
  .b-ok { background: #d9f2e3; color: var(--ok); }
  .b-bad { background: #fadcd9; color: var(--bad); }
  .b-warn { background: #f7ead0; color: var(--warn); }
  .mono { font-family: ui-monospace, Consolas, monospace; font-size: 12px; }
  .row { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: 6px 0; }
  .banner { padding: 8px 12px; border-radius: 6px; margin: 6px 0; }
  .banner.pause { background: #fadcd9; color: var(--bad); font-weight: 600; }
  .banner.stale { background: #f7ead0; color: var(--warn); }
  ul.blockers { margin: 4px 0; padding-left: 20px; color: var(--bad); }
  #toast { position: fixed; right: 16px; bottom: 16px; max-width: 460px; z-index: 9; }
  .toast { background: #10233f; color: #fff; padding: 10px 14px; border-radius: 6px;
           margin-top: 8px; white-space: pre-wrap; font-size: 13px; }
  .toast.err { background: var(--bad); }
  fieldset { border: 1px solid var(--bd); border-radius: 6px; margin: 8px 0; }
  legend { font-size: 12px; color: #5a6b80; }
  .hash { word-break: break-all; }
</style>
</head>
<body>
<header>
  <h1>剂量递增安全决策平台 · 安全工作台</h1>
  <span class="sp"></span>
  <span id="ledgerBadge" class="badge">账本未校验</span>
  <button onclick="verifyLedger()">验证账本</button>
</header>
<main>
  <div class="card">
    <h2>① 试验与队列</h2>
    <fieldset><legend>新建试验(含独立安全委员会)</legend>
      <div class="row">
        <input id="tName" placeholder="试验名称" value="ABC-101 首次人体试验">
        <input id="tCommittee" placeholder="委员ID,逗号分隔" value="dsmb1,dsmb2,dsmb3" size="24">
        <input id="tQuorum" placeholder="法定人数" value="3" size="6">
        <input id="tThreshold" placeholder="赞成门槛" value="2" size="6">
        <button class="primary" onclick="createTrial()">创建试验</button>
      </div>
    </fieldset>
    <fieldset><legend>新建队列(创建即冻结:方案 / 观察窗 / 受试者集合)</legend>
      <div class="row">
        <input id="cLabel" placeholder="队列" value="C1">
        <input id="cDose" placeholder="剂量 mg" value="10" size="6">
        <input id="cSchedule" placeholder="给药计划" value="QD x 7d" size="10">
        <input id="cWindow" placeholder="观察窗(天)" value="28" size="8">
        <input id="cSubjects" placeholder="受试者编号,逗号分隔" value="S01,S02,S03" size="18">
        <button class="primary" onclick="createCohort()">创建并冻结</button>
      </div>
    </fieldset>
    <div class="row">
      <label>当前队列:</label>
      <select id="cohortSel" onchange="pickCohort()"></select>
      <button onclick="refresh()">刷新</button>
      <span id="cohortMeta"></span>
    </div>
    <div id="pauseBanner"></div>
  </div>

  <div class="card">
    <h2>② 规则评估与决策前置条件</h2>
    <div id="evalBox"></div>
    <div id="blockerBox"></div>
  </div>

  <div class="card full">
    <h2>③ 受试者</h2>
    <table id="subjTable">
      <thead><tr><th>编号</th><th>状态</th><th>数据</th><th>给药次数</th>
        <th>观察窗截止</th><th>窗完整</th><th>DLT</th><th>计入评估</th><th>操作</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>

  <div class="card">
    <h2>④ 给药与安全事件</h2>
    <fieldset><legend>记录给药(暂停时立即阻断;已给药事实保留)</legend>
      <div class="row">
        <input id="dSub" placeholder="受试者" size="6">
        <input id="dDate" type="date">
        <button class="primary" onclick="recordDose()">记录给药</button>
      </div>
    </fieldset>
    <fieldset><legend>报告不良事件(迟到事件自动标记并使快照前进)</legend>
      <div class="row">
        <input id="eSub" placeholder="受试者" size="6">
        <input id="eDesc" placeholder="描述" size="14">
        <select id="eSev"><option>1</option><option>2</option><option selected>3</option>
          <option>4</option><option>5</option></select>
        <select id="eEval"><option>PENDING</option><option>EVALUABLE</option>
          <option>NON_EVALUABLE</option></select>
        <label><input id="eRel" type="checkbox" checked>相关</label>
        <button class="primary" onclick="reportEvent()">报告</button>
      </div>
    </fieldset>
    <table id="evtTable">
      <thead><tr><th>ID</th><th>受试者</th><th>级别</th><th>可评估</th>
        <th>相关</th><th>迟到</th><th>操作</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>

  <div class="card">
    <h2>⑤ 队列管控</h2>
    <fieldset><legend>受试者</legend>
      <div class="row">
        <input id="rOld" placeholder="被替补" size="6">
        <input id="rNew" placeholder="替补编号" size="6">
        <button onclick="replaceSubject()">替补</button>
        <input id="aCodes" placeholder="增补编号,逗号分隔" size="14">
        <button onclick="addSubjects()">增补(需扩展决策)</button>
      </div>
    </fieldset>
    <fieldset><legend>紧急暂停:立即阻断后续给药,保留已给药事实</legend>
      <div class="row">
        <input id="pReason" placeholder="暂停原因" size="24">
        <button class="danger" onclick="pauseCohort()">紧急暂停本队列</button>
        <button onclick="resumeCohort()">恢复</button>
      </div>
    </fieldset>
    <fieldset><legend>规则修订(使进行中会话立即过期)</legend>
      <div class="row">
        <input id="rsVersion" placeholder="新版本" value="v2" size="4">
        <input id="rsBody" placeholder='规则JSON' size="34"
          value='{"design":"3+3","dlt_min_severity":3,"require_related":true,"initial_size":3,"expanded_size":6}'>
        <button onclick="amendRules()">修订规则</button>
      </div>
    </fieldset>
  </div>

  <div class="card full">
    <h2>⑥ 决策会话与委员会投票(结论只可能来自同一快照)</h2>
    <div class="row">
      <select id="intentSel">
        <option>ESCALATE</option><option>EXPAND</option>
        <option>STOP</option><option>HOLD</option>
      </select>
      <button class="primary" onclick="openSession()">开启决策会话</button>
      <span class="badge">只有观察窗完整、缺失数据处理完毕且规则允许,会话才能开启</span>
    </div>
    <table id="sesTable">
      <thead><tr><th>会话</th><th>意图</th><th>状态</th><th>钉住版本</th>
        <th>规则</th><th>投票</th><th>结果</th><th>操作</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>

  <div class="card full">
    <h2>⑦ 决策证明(哈希链账本,可独立验证)</h2>
    <table id="proofTable">
      <thead><tr><th>账本序号</th><th>会话</th><th>意图</th><th>结果</th>
        <th>快照哈希</th><th>规则版本</th><th>证明哈希</th><th>操作</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>
</main>
<div id="toast"></div>

<script>
let trialId = localStorage.getItem('trialId') || '';
let cohortId = localStorage.getItem('cohortId') || '';
let cohorts = JSON.parse(localStorage.getItem('cohorts') || '[]');

function toast(msg, err) {
  const d = document.createElement('div');
  d.className = 'toast' + (err ? ' err' : '');
  d.textContent = msg;
  document.getElementById('toast').appendChild(d);
  setTimeout(() => d.remove(), 7000);
}
async function api(method, path, body) {
  const r = await fetch(path, {method, headers: {'Content-Type': 'application/json'},
    body: body ? JSON.stringify(body) : undefined});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    const e = j.error || {};
    let m = `[${e.code || r.status}] ${e.message || '请求失败'}`;
    if (e.details && e.details.blockers) m += '\n' + e.details.blockers.join('\n');
    throw new Error(m);
  }
  return j;
}
const run = (p) => p.then(r => { refresh(); return r; })
  .catch(e => toast(e.message, true));

async function createTrial() {
  const committee = document.getElementById('tCommittee').value.split(',')
    .map(s => s.trim()).filter(Boolean).map(id => ({id, name: id}));
  const r = await run(api('POST', '/api/trials', {
    name: tName.value, phase: 'I', committee,
    quorum: +tQuorum.value, approve_threshold: +tThreshold.value}));
  if (r) { trialId = r.id; localStorage.setItem('trialId', trialId);
    toast('试验已创建: ' + trialId); }
}
async function createCohort() {
  const r = await run(api('POST', '/api/cohorts', {
    trial_id: trialId, label: cLabel.value, dose_mg: +cDose.value,
    schedule: cSchedule.value, window_days: +cWindow.value,
    subjects: cSubjects.value.split(',').map(s => s.trim()).filter(Boolean)}));
  if (r) { cohorts.push({id: r.id, label: cLabel.value});
    localStorage.setItem('cohorts', JSON.stringify(cohorts));
    cohortId = r.id; localStorage.setItem('cohortId', cohortId);
    toast('队列已冻结,快照哈希: ' + r.snapshot_hash.slice(0, 16) + '…'); }
}
function pickCohort() {
  cohortId = document.getElementById('cohortSel').value;
  localStorage.setItem('cohortId', cohortId); refresh();
}
async function refresh() {
  const sel = document.getElementById('cohortSel');
  sel.innerHTML = cohorts.map(c =>
    `<option value="${c.id}" ${c.id===cohortId?'selected':''}>${c.label}</option>`).join('');
  if (!cohortId) return;
  let v;
  try { v = await api('GET', `/api/cohorts/${cohortId}/snapshot`); }
  catch (e) { toast(e.message, true); return; }
  const c = v.cohort;
  document.getElementById('cohortMeta').innerHTML =
    `<span class="badge">${c.label} · ${c.dose_mg}mg · ${c.schedule}</span>` +
    `<span class="badge ${c.status==='ACTIVE'?'b-ok':'b-bad'}">${c.status}</span>` +
    `<span class="badge">状态 v${v.state_version}</span>` +
    `<span class="badge">规则 ${v.rule_version}</span>` +
    `<span class="badge mono hash" title="${v.snapshot_hash}">快照 ${v.snapshot_hash.slice(0,12)}…</span>`;
  document.getElementById('pauseBanner').innerHTML = v.paused
    ? `<div class="banner pause">⛔ 紧急暂停生效:${v.paused.reason} —— 后续给药已阻断,已给药事实保留</div>` : '';
  document.getElementById('evalBox').innerHTML =
    `<div>DLT <b>${v.evaluation.dlt_count}</b> 例 · 可评估 <b>${v.evaluation.evaluable_count}</b> 例</div>` +
    `<div>允许动作:` + v.evaluation.permitted.map(p =>
      `<span class="badge b-ok">${p}</span>`).join('') + `</div>` +
    v.evaluation.rationale.map(r => `<div class="mono">${r}</div>`).join('');
  document.getElementById('blockerBox').innerHTML = v.blockers.length
    ? `<b>阻断项(须全部解除方可决策):</b><ul class="blockers">` +
      v.blockers.map(b => `<li>${b}</li>`).join('') + '</ul>'
    : '<span class="badge b-ok">前置条件全部满足</span>';
  document.querySelector('#subjTable tbody').innerHTML = v.subjects.map(s => `
    <tr><td>${s.code}</td><td>${s.status}</td>
      <td><span class="badge ${s.data_status==='COMPLETE'?'b-ok':'b-warn'}">${s.data_status}</span></td>
      <td>${s.dose_count}</td><td>${s.window_end || '—'}</td>
      <td>${s.window_complete ? '✔' : '✘'}</td>
      <td>${s.dlt ? '<span class="badge b-bad">DLT</span>' : ''}</td>
      <td>${s.counts ? '✔' : '✘'}</td>
      <td>${s.data_status !== 'COMPLETE'
        ? `<button onclick="setData('${s.code}','COMPLETE')">数据补齐</button>` : ''}
        ${s.status === 'ACTIVE'
        ? `<button onclick="document.getElementById('rOld').value='${s.code}'">替补…</button>` : ''}
      </td></tr>`).join('');
  document.querySelector('#evtTable tbody').innerHTML = v.events.map(e => `
    <tr><td class="mono">${e.id.slice(0,10)}…</td><td>${e.subject_code}</td>
      <td>${e.severity} 级</td>
      <td><span class="badge ${e.evaluability==='PENDING'?'b-warn':''}">${e.evaluability}</span></td>
      <td>${e.related ? '✔' : '✘'}</td>
      <td>${e.late ? '<span class="badge b-warn">迟到</span>' : ''}</td>
      <td>${e.evaluability === 'PENDING'
        ? `<button onclick="updEvent('${e.id}','EVALUABLE')">判可评估</button>
           <button onclick="updEvent('${e.id}','NON_EVALUABLE')">判不可评估</button>` : ''}
      </td></tr>`).join('');
  const ses = v.open_session;
  let sesRows = '';
  if (ses) {
    const sv = await api('GET', `/api/sessions/${ses.id}`);
    const stale = sv.stale_reasons.length
      ? `<div class="banner stale">⚠ 快照已过期:${sv.stale_reasons.join(';')} —— 请关闭后重开</div>` : '';
    sesRows = `<tr><td class="mono">${ses.id.slice(0,10)}…</td><td>${ses.intent}</td>
      <td>${ses.status}${stale}</td><td>v${ses.state_version}</td><td>${ses.rule_version}</td>
      <td>${sv.votes.map(x => `${x.member_id}:${x.vote}`).join(', ') || '—'}</td>
      <td>${ses.outcome || '—'}</td>
      <td>${v.committee.map(m =>
        `<button onclick="castVote('${ses.id}','${m.member_id}','APPROVE',${ses.state_version})">${m.member_id} 赞成</button>`).join('')}
        ${v.committee.map(m =>
        `<button onclick="castVote('${ses.id}','${m.member_id}','REJECT',${ses.state_version})">${m.member_id} 反对</button>`).join('')}
        <button onclick="closeSession('${ses.id}')">关闭</button></td></tr>`;
  }
  document.querySelector('#sesTable tbody').innerHTML = sesRows || '<tr><td colspan="8">无进行中会话</td></tr>';
  const proofs = await api('GET', `/api/cohorts/${cohortId}/proofs`);
  document.querySelector('#proofTable tbody').innerHTML = proofs.map(p => `
    <tr><td>${p.seq}</td><td class="mono">${p.session_id.slice(0,10)}…</td>
      <td>${p.intent}</td><td><b>${p.outcome}</b></td>
      <td class="mono hash">${p.snapshot_hash.slice(0,16)}…</td><td>${p.rule_version}</td>
      <td class="mono hash">${p.hash.slice(0,16)}…</td>
      <td><button onclick="verifyProof('${p.session_id}')">验证</button>
          <button onclick="showProof('${p.session_id}')">查看</button></td></tr>`).join('')
    || '<tr><td colspan="8">暂无决策证明</td></tr>';
}
function recordDose() { run(api('POST', `/api/cohorts/${cohortId}/doses`,
  {subject_code: dSub.value, given_at: dDate.value || null})).then(r =>
  r && toast(`已记录给药 ${r.dose_mg}mg @ ${r.given_at}`)); }
function reportEvent() { run(api('POST', `/api/cohorts/${cohortId}/events`, {
  subject_code: eSub.value, description: eDesc.value, severity: +eSev.value,
  evaluability: eEval.value, related: eRel.checked})).then(r =>
  r && toast(r.late ? '事件已记录(迟到):快照版本已前进,进行中会话将过期'
                    : '事件已记录')); }
function updEvent(id, ev) { run(api('PATCH', `/api/events/${id}`, {evaluability: ev})); }
function setData(code, st) { run(api('POST',
  `/api/cohorts/${cohortId}/subjects/${code}/data-status`, {status: st})); }
function replaceSubject() { run(api('POST',
  `/api/cohorts/${cohortId}/subjects/${rOld.value}/replace`,
  {new_code: rNew.value, reason: '工作台操作'})); }
function addSubjects() { run(api('POST', `/api/cohorts/${cohortId}/subjects`,
  {codes: aCodes.value.split(',').map(s => s.trim()).filter(Boolean)})); }
function pauseCohort() { run(api('POST', `/api/cohorts/${cohortId}/pause`,
  {reason: pReason.value || '紧急安全暂停'})); }
function resumeCohort() { run(api('POST', `/api/cohorts/${cohortId}/resume`)); }
function amendRules() { run(api('POST', `/api/trials/${trialId}/rulesets`,
  {version: rsVersion.value, body: JSON.parse(rsBody.value)})); }
function openSession() { run(api('POST', `/api/cohorts/${cohortId}/sessions`,
  {intent: intentSel.value})).then(r =>
  r && toast(`会话已开启,钉住快照 v${r.state_version} / ${r.rule_version}\n` +
             `哈希 ${r.snapshot_hash.slice(0,16)}…`)); }
function castVote(sid, member, vote, ver) { run(api('POST',
  `/api/sessions/${sid}/votes`,
  {member_id: member, vote, seen_state_version: ver})).then(r =>
  r && toast(r.decided ? `法定人数达成,决策定稿:${r.decided}`
                       : `投票已记录(${r.votes_cast}/${r.quorum})`)); }
function closeSession(sid) { run(api('POST', `/api/sessions/${sid}/close`)); }
async function verifyProof(sid) {
  const r = await run(api('GET', `/api/proofs/${sid}/verify`));
  if (r) toast(r.ok ? `✔ 证明有效\n条目哈希✔ 快照哈希✔ 账本链✔`
                    : '✘ 证明无效: ' + JSON.stringify(r), !r.ok);
}
async function showProof(sid) {
  const r = await run(api('GET', `/api/proofs/${sid}`));
  if (r) alert(JSON.stringify(r.payload, null, 2));
}
async function verifyLedger() {
  const r = await run(api('GET', '/api/ledger/verify'));
  const b = document.getElementById('ledgerBadge');
  if (r) { b.textContent = r.ok ? `账本链完整(${r.entries} 条)` : '账本链断裂!';
           b.className = 'badge ' + (r.ok ? 'b-ok' : 'b-bad'); }
}
dDate.valueAsDate = new Date();
refresh();
</script>
</body>
</html>
"""
