import {table} from '../components/common/table.js';
import {badge} from '../components/common/badge.js';
import {esc,date} from '../utils/html.js';
import {session} from '../hooks/session.js';
import {READ_STATES} from '../constants/enums.js';

const readBadge=v=>`<span class="badge read-${esc(v)}">${esc(READ_STATES[v]||v)}</span>`;

export function windowsPage(items,sets,nodes){
  const canWrite=session.user?.role!=='viewer';
  const open=items.filter(w=>w.status==='open');
  const done=items.filter(w=>w.status!=='open');
  return `<div class="split">
    <section>
      <div class="section-top"><h2>共享演练窗口</h2><span>SHARED TIMELINE / READ CAPACITY</span></div>
      ${table(['窗口 / 创建时间','时限 / 默认占用','状态','试验数','证据'],items.map(w=>[
        `<strong>${esc(w.name)}</strong><small>#${w.id} · ${date(w.created_at)}</small>`,
        `${w.horizon_ms}ms / ${w.default_hold_ms}ms`,
        badge(w.status),
        String(w.result?.summary?.trials ?? (w.config_snapshot?.trials?.length||0)),
        `<a href="#window/${w.id}">查看 →</a>`]))}
      <div class="protocol"><h3>共享窗口调度口径</h3><ol>
        <li>窗口内多个试验共用一条时间轴，读请求按到达进入同一个 FIFO 等待队列</li>
        <li>每个节点 read_capacity 个并发读槽，读启动即占用 hold_ms，跨试验不得抢占同一份剩余容量</li>
        <li>容量不足可排队或回退主库；超过 latest_start_ms 过期拒绝，窗口关闭仍排队则拒绝</li>
        <li>等待期间的复制推进、节点故障/恢复在重新调度时即时影响选路</li>
        <li>取消试验：排队读立即撤回不占容量，已开始占用保留至结束，已完成决策不改写</li>
      </ol></div>
      ${done.length?`<div class="section-top" style="margin-top:20px"><h2>已关闭窗口</h2><span>EVIDENCE FROZEN</span></div>
      ${table(['窗口','状态','调度 / 过期 / 撤回 / 关闭','校验和'],done.map(w=>[
        `<a href="#window/${w.id}"><strong>${esc(w.name)}</strong></a>`,badge(w.status),
        `${w.result?.summary?.scheduled??0} / ${w.result?.summary?.expired??0} / ${w.result?.summary?.withdrawn??0} / ${w.result?.summary?.closed_unrouted??0}`,
        `<code class="ck">${esc((w.result?.checksum||'').slice(0,12))}…</code>`]))}`:''}
    </section>
    <section class="form-panel">
      <span class="eyebrow">SHARED DRILL WINDOW</span>
      <h2>配置共享演练窗口</h2>
      <form id="window-form">
        <label>窗口名称<input name="name" value="交接班共享窗口-${new Date().toISOString().slice(0,10)}" required></label>
        <div class="fields">
          <label>窗口时限 horizon_ms<input name="horizon_ms" type="number" min="1" value="5000" required></label>
          <label>默认占用 hold_ms<input name="default_hold_ms" type="number" min="0" value="100" required></label>
        </div>
        <p class="help">时限是共享时间轴的绝对终点；读事件未显式给 hold_ms 时取默认占用。当前节点读容量：${nodes.map(n=>`${esc(n.name)}=${n.read_capacity}`).join('，')||'暂无节点'}</p>
        <button class="primary" ${canWrite?'':'disabled'}>创建窗口 +</button>
      </form>
      <p class="help">创建后在窗口详情页发起多个试验、设置取消点并执行；执行后证据冻结，不能再追加试验。</p>
    </section>
  </div>`;
}

export function windowDetailPage(w,sets){
  if(!w)return '<div class="empty">窗口不存在或尚未创建。</div>';
  const canWrite=session.user?.role!=='viewer';
  const isOpen=w.status==='open';
  const trials=w.trials||[];
  const r=w.result;
  return `<div class="evidence-top">
    <div><span class="eyebrow">SHARED WINDOW / #${w.id}</span>
      <h2>${esc(w.name)} ${badge(w.status)}</h2>
      <p>时限 ${w.horizon_ms}ms · 默认占用 ${w.default_hold_ms}ms · ${date(w.created_at)}</p></div>
    <div class="evidence-actions">
      ${isOpen?`<button id="execute-window" class="primary" ${canWrite&&trials.length?'':'disabled'}>执行窗口 ↗</button>`:
        `<button id="window-export" data-id="${w.id}">导出 JSON ↓</button>`}
      <a href="#windows"><button>返回列表</button></a>
    </div>
  </div>
  ${w.error?`<div class="error">${esc(w.error)}</div>`:''}
  ${isOpen?`
  <div class="split" style="margin-bottom:25px">
    <section>
      <div class="section-top"><h2>窗口内试验（${trials.length}）</h2><span>QUEUED TRIALS</span></div>
      ${table(['试验 / 请求集','延迟 / 粘滞','取消点','状态','操作'],trials.map(t=>[
        `<strong>${esc(t.name)}</strong><small>${esc(sets.find(s=>s.id===t.request_set_id)?.code||'#')+t.request_set_id}</small>`,
        `${t.max_delay_ms}ms / ${t.sticky_ms}ms`,
        t.cancel_at_ms==null?'—':t.cancel_at_ms+'ms',
        badge(t.status),
        t.status==='queued'?`<button class="small cancel-trial" data-id="${t.id}" ${canWrite?'':'disabled'}>取消</button>`:'—']))}
      ${trials.length===0?'<div class="empty">还没有试验，用右侧表单发起。</div>':''}
    </section>
    <section class="form-panel">
      <span class="eyebrow">ENROLL TRIAL</span>
      <h2>向窗口发起试验</h2>
      <form id="trial-window-form">
        <label>试验名称<input name="name" value="共享窗口试验" required></label>
        <label>会话请求集<select name="request_set_id">${sets.map(s=>`<option value="${s.id}">${esc(s.code)} · ${esc(s.title)}</option>`).join('')}</select></label>
        <div class="fields">
          <label>延迟门槛ms<input name="max_delay_ms" type="number" min="0" value="100" required></label>
          <label>粘滞ms<input name="sticky_ms" type="number" min="0" value="500" required></label>
        </div>
        <label>时间轴取消点 cancel_at_ms（可选）<input name="cancel_at_ms" type="number" min="0" max="${w.horizon_ms}" placeholder="留空表示不取消"></label>
        <button class="primary" ${canWrite?'':'disabled'}>发起试验 +</button>
      </form>
      <p class="help">读事件的 hold_ms / latest_start_ms 在请求集 JSON 中配置；取消点是窗口时间轴上的绝对时刻。</p>
    </section>
  </div>`:''}
  ${r?windowEvidence(r):'<div class="notice">窗口尚未执行。发起试验后点击「执行窗口」，系统将在共享时间轴上统一重放并冻结证据。</div>'}`;
}

function windowEvidence(r){
  const sum=r.summary;
  const primary=r.final_nodes.find(n=>n.role==='primary')?.name;
  return `
  <div class="verdict">
    <div class="verdict-icon">${sum.all_scheduled_reads_safe?'✓':'!'}</div>
    <div><span class="eyebrow">SHARED CAPACITY CHECK</span>
      <h2>${sum.all_scheduled_reads_safe?'全部已调度读满足写后读与容量约束':'存在不安全或越界调度'}</h2>
      <p>试验 ${sum.trials}（取消 ${sum.cancelled_trials}）· 读 ${sum.reads}：调度 ${sum.scheduled} · 过期 ${sum.expired} · 撤回 ${sum.withdrawn} · 关闭 ${sum.closed_unrouted} · 峰值排队 ${sum.peak_queue_depth} · 峰值在途 ${sum.peak_in_flight}</p></div>
  </div>
  <div class="policy-strip">默认占用 ${r.window.default_hold_ms}ms <span>／</span> 时限 ${r.window.horizon_ms}ms <span>／</span> 谱系 ${esc(r.lineage)} <span>／</span> 副本读 ${sum.replica_reads} · 主库读 ${sum.primary_reads}</div>
  <div class="section-top"><h2>等待读取去向台账（跨试验共享队列）</h2><span>READ LEDGER / FIFO</span></div>
  ${table(['# / 试验','到达 / 期限','占用 hold','状态','去向节点','启动→结束 / 等待','判定理由'],r.read_ledger.map(e=>[
    `${e.seq}<small>#${esc(e.trial_name)}</small>`,
    `${e.at_ms}ms<small>${e.latest_start_ms==null?'无期限':e.latest_start_ms+'ms'}`,
    e.hold_ms+'ms',
    readBadge(e.status),
    e.node?esc(e.node):'<span class="muted">—</span>',
    e.start_ms==null?'—':`${e.start_ms}→${e.end_ms}ms<small>等待 ${e.wait_ms}ms</small>`,
    esc(e.reason)]))}
  <div class="section-top" style="margin-top:25px"><h2>共享时间轴事件</h2><span>UNIFIED TIMELINE</span></div>
  ${table(['时间','试验 / 会话','类型','决策 / 节点','理由'],r.trace.map(x=>[
    x.at_ms+'ms',
    `${esc(x.trial_name||'-')}<small>${esc(x.session)}</small>`,
    esc(x.kind),
    x.decision==='rejected'?'<span class="rejected">拒绝</span>':x.decision==='withdrawn'?'<span class="rejected">撤回</span>':`${esc(x.decision)} ${x.node?('· '+esc(x.node)):''}`,
    esc(x.reason)]))}
  <div class="section-top" style="margin-top:25px"><h2>分试验结果</h2><span>PER-TRIAL</span></div>
  ${table(['试验','状态','读调度/过期/撤回/关闭','副本/主库读','最大等待','全部安全'],r.trial_results.map(t=>[
    `<strong>${esc(t.name)}</strong><small>#${t.trial_id}${t.cancel_at_ms==null?'':' · 取消点'+t.cancel_at_ms+'ms'}</small>`,
    badge(t.status==='cancelled'?'cancelled':'completed'),
    `${t.summary.scheduled} / ${t.summary.expired} / ${t.summary.withdrawn} / ${t.summary.closed}`,
    `${t.summary.replica_reads} / ${t.summary.primary_reads}`,
    t.summary.max_wait_ms+'ms',
    t.summary.all_scheduled_safe?'✓':'!']))}
  <div class="evidence-notes">
    <section><h3>容量与排队口径</h3><p>${esc(r.policy.capacity_model)}</p><p>${esc(r.policy.queue_model)}</p><p>${esc(r.policy.deadline_boundary)}</p></section>
    <section><h3>取消与确定性</h3><p>${esc(r.policy.cancel_rule)}</p><p>${esc(r.mode)}</p><code>${esc(r.checksum)}</code></section>
  </div>`;
}
