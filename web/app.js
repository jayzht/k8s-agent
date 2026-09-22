/* O&M Agent 审批台 —— 前端逻辑
 *
 * 安全要点：本文件**无法表达要执行什么动作**。
 * 它只能发送 diagnosis_id / candidate_index / proposal_id，
 * 真正的动作对象全部保存在服务端（见 omagent/web.py 的模块注释）。
 */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* 后端结论里用了 **粗体** 标记。先转义再渲染，避免 XSS，同时不让星号裸露。 */
const mdBold = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');

async function api(path, body) {
  const opt = body
    ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
    : {};
  const r = await fetch(path, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

function toast(msg, kind = '') {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast ' + kind;
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add('hidden'), 4200);
}

/* 页内确认框：返回 {ok, reason}。不依赖原生弹窗，不会被浏览器拦截。 */
function askConfirm(title, msg, needReason, okText = '确认执行') {
  return new Promise(resolve => {
    const m = $('modal');
    $('modal-title').textContent = title;
    $('modal-msg').textContent = msg;
    $('modal-reason').value = '';
    $('modal-reason').classList.toggle('hidden', !needReason);
    $('modal-ok').textContent = okText;
    m.classList.remove('hidden');
    setTimeout(() => (needReason ? $('modal-reason') : $('modal-ok')).focus(), 50);

    const done = (ok) => {
      m.classList.add('hidden');
      $('modal-ok').onclick = null; $('modal-cancel').onclick = null;
      m.onkeydown = null;
      resolve({ ok, reason: $('modal-reason').value.trim() });
    };
    $('modal-ok').onclick = () => done(true);
    $('modal-cancel').onclick = () => done(false);
    m.onkeydown = (e) => { if (e.key === 'Escape') done(false); };
  });
}

const App = {
  state: { diagnosis: null, workload: null },

  // ── 初始化 ────────────────────────────────────────────
  async init() {
    this.bindTabs();
    await Promise.all([this.loadStatus(), this.refreshAudit(), this.refreshKnowledge()]);
    await this.loadWorkloads();
  },

  bindTabs() {
    document.querySelectorAll('.tab').forEach(tab => {
      tab.onclick = () => {
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tabpane').forEach(p => p.classList.add('hidden'));
        tab.classList.add('active');
        $('tab-' + tab.dataset.tab).classList.remove('hidden');
        if (tab.dataset.tab === 'audit') this.refreshAudit();
        if (tab.dataset.tab === 'knowledge') this.refreshKnowledge();
      };
    });
  },

  async loadStatus() {
    try {
      const s = await api('/api/status');
      const c = $('chip-cluster');
      c.textContent = (s.cluster.ok ? '✅ ' : '❌ ') + s.cluster.info;
      c.className = 'chip ' + (s.cluster.ok ? 'ok' : 'bad');

      const l = $('chip-llm');
      const ok = s.llm.available === '是';
      l.textContent = 'LLM ' + (ok ? `${s.llm.model} 可用` : '未配置（将降级）');
      l.className = 'chip ' + (ok ? 'ok' : '');
      l.title = `base_url=${s.llm.base_url}  api_key=${s.llm.api_key}`;

      $('chip-operator').textContent = '操作人 ' + s.operator;
      if (s.demo) $('demo-box').classList.remove('hidden');
      this.renderPolicy(s.policy, s.tools);
    } catch (e) { toast('状态加载失败：' + e.message, 'err'); }
  },

  renderPolicy(p, tools) {
    const forbidden = (p.forbidden_actions || []).map(f => `<li>${esc(f)}</li>`).join('');
    const actions = Object.entries(p.registered_actions || {}).map(([n, a]) =>
      `<li><code>${esc(n)}</code> <span class="tier-${a.tier}">${a.tier}</span>` +
      ` ${a.mutating ? '<span class="dim">写</span>' : '<span class="dim">只读</span>'}</li>`).join('');
    $('policy-body').innerHTML = `
      <div class="card">
        <h3>作用域白名单</h3>
        <div class="kb-row"><span class="dim">命名空间</span><span class="n">${esc((p.allowed_namespaces||[]).join(', '))}</span></div>
        <div class="kb-row"><span class="dim">节点</span><span class="n">${esc((p.allowed_nodes||[]).join(', '))}</span></div>
        <div class="kb-row"><span class="dim">爆炸半径上限</span><span class="n">${p.max_impacted_objects}</span></div>
        <div class="kb-row"><span class="dim">变更冷却</span><span class="n">${p.cooldown_seconds}s</span></div>
        <div class="kb-row"><span class="dim">强制 dry-run</span><span class="n">${p.require_dry_run ? '是' : '否'}</span></div>
      </div>
      <div class="card"><h3>T3 禁止动作</h3><ul class="hint" style="padding-left:18px">${forbidden}</ul></div>
      <div class="card"><h3>动作白名单</h3><ul class="hint" style="padding-left:18px">${actions}</ul></div>`;
  },

  // ── 工作负载 ──────────────────────────────────────────
  async loadWorkloads() {
    try {
      const ns = $('ns').value.trim() || 'demo';
      const d = await api('/api/workloads', { namespace: ns });
      const ul = $('wl-list');
      ul.innerHTML = '';
      if (!d.workloads.length) {
        ul.innerHTML = '<li class="dim">没有找到工作负载</li>';
        return;
      }
      d.workloads.forEach(w => {
        const li = document.createElement('li');
        if (w.protected) li.className = 'protected';
        li.innerHTML = `<div>${esc(w.name)}</div>
          <div class="wl-meta">${esc(w.kind)} · ${w.replicas} 副本` +
          (w.protected ? ' · <span style="color:var(--yellow)">受保护</span>' : '') + '</div>';
        li.onclick = () => this.diagnose(w);
        ul.appendChild(li);
      });
    } catch (e) { toast('列表加载失败：' + e.message, 'err'); }
  },

  // ── 沙箱故障注入（演示用）─────────────────────────────
  async inject(scenario) {
    const label = { oom: '内存超限', crash: '启动失败', image: '镜像拉取失败',
                    pending: '调度失败', reset: '恢复基线' }[scenario] || scenario;
    if (scenario !== 'reset') {
      const ok = await askConfirm(`注入「${label}」故障？`,
        '只影响沙箱 demo 命名空间，用于演示 Agent 的诊断与处置。', false, '注入');
      if (!ok.ok) return;
    }
    toast(`正在注入：${label}…`);
    try {
      const r = await api('/api/sandbox/fault', { scenario });
      toast(r.ok ? `已注入：${label}（等 20-40 秒让它显现）` : `注入失败：${r.output.slice(0,80)}`,
            r.ok ? 'ok' : 'err');
      if (r.ok && scenario !== 'reset') {
        // 给故障一点时间显现，然后自动刷新列表
        setTimeout(() => this.loadWorkloads(), 12000);
      }
      this.refreshAudit();
    } catch (e) { toast('注入失败：' + e.message, 'err'); }
  },

  // ── 自然语言入口 ──────────────────────────────────────
  async ask() {
    const text = $('ask-text').value.trim();
    if (!text) { toast('请先描述一下问题', 'err'); return; }
    $('proposal-box').classList.add('hidden');
    $('idle').classList.add('hidden');
    $('diagnosis').classList.remove('hidden');
    $('diag-conclusion').textContent = '正在理解你的描述…';
    $('ev-table').querySelector('tbody').innerHTML = '';
    $('cand-list').innerHTML = '';
    $('diag-hint').classList.add('hidden');

    try {
      const d = await api('/api/ask', { text, namespace: $('ns').value.trim() });
      if (!d.resolved) {
        // 解析不出目标：明确告知，而不是猜一个工作负载
        $('diagnosis').classList.add('hidden');
        $('proposal-box').classList.remove('hidden');
        $('proposal-box').innerHTML = `<div class="confirm blocked">
            <div class="confirm-head"><span>🤔 无法确定你要诊断什么</span>
              <span class="dim">意图未解析</span></div>
            <div class="confirm-body">
              <div class="sec"><div class="sec-title">Agent 的反馈</div>
                <div>${esc(d.message)}</div></div>
              <div class="sec"><div class="sec-title">你的原话</div>
                <div class="mono dim">${esc(d.raw || text)}</div></div>
            </div></div>`;
        return;
      }
      this.state.diagnosis = d;
      // 把"我理解成了什么"显式展示出来，让用户能立刻发现理解偏差
      if (d.intent) {
        $('diag-hint').innerHTML = '🧭 <b>' + esc(d.intent.interpretation) + '</b>' +
          `<span class="dim">（来源=${esc(d.intent.source)}，置信度=${esc(d.intent.confidence)}）</span>`;
        $('diag-hint').classList.remove('hidden');
      }
      this.state.workload = { namespace: d.intent.namespace, name: d.intent.workload,
                              kind: d.intent.kind };
      this.renderDiagnosis(d, true);
    } catch (e) {
      $('diag-conclusion').textContent = '解析失败：' + e.message;
      toast('解析失败：' + e.message, 'err');
    }
  },

  // ── 诊断 ──────────────────────────────────────────────
  async diagnose(w) {
    $('proposal-box').classList.add('hidden');
    $('idle').classList.add('hidden');
    $('diagnosis').classList.remove('hidden');
    $('diag-conclusion').textContent = '诊断中…（LLM 模式下可能需要 10-60 秒）';
    $('ev-table').querySelector('tbody').innerHTML = '';
    $('cand-list').innerHTML = '';
    $('diag-hint').classList.add('hidden');

    this.state.workload = w;
    try {
      const d = await api('/api/diagnose', {
        namespace: w.namespace, workload: w.name, kind: w.kind,
        planner: $('planner').value, turns: parseInt($('turns').value, 10)
      });
      this.state.diagnosis = d;
      this.renderDiagnosis(d);
    } catch (e) {
      $('diag-conclusion').textContent = '诊断失败：' + e.message;
      toast('诊断失败：' + e.message, 'err');
    }
  },

  renderDiagnosis(d, keepIntentHint = false) {
    $('diag-sig').textContent = d.signature;
    const c = $('diag-conf');
    c.textContent = '置信度 ' + d.confidence;
    c.className = 'conf ' + d.confidence;
    $('diag-planner').textContent = '规划器：' + d.planner;
    $('diag-conclusion').innerHTML = mdBold(d.conclusion);

    // 历史提示（知识沉淀的价值体现）；自然语言入口下保留意图复述
    const h = $('diag-hint');
    if (keepIntentHint && d.intent) {
      h.innerHTML += d.history_hint ? `<div style="margin-top:6px">📚 ${esc(d.history_hint)}</div>` : '';
    } else if (d.history_hint) {
      h.textContent = '📚 ' + d.history_hint;
      h.classList.remove('hidden');
    } else h.classList.add('hidden');

    $('diag-findings').innerHTML = (d.findings || [])
      .map(f => `<div class="finding">• ${esc(f)}</div>`).join('');

    // 取证过程
    const inv = d.investigation || [];
    $('inv-count').textContent = inv.length ? `${inv.length} 次取证` : '（未启用多轮）';
    $('inv-body').innerHTML = inv.map(t => `
      <div style="margin-bottom:8px">
        <div class="dim">第 ${t.turn} 轮 · <code>${esc(t.tool)}</code></div>
        <pre>${esc(t.error ? '[失败] ' + t.error : t.result)}</pre>
      </div>`).join('') || '<p class="hint">本次为单轮诊断。</p>';

    // 证据链
    $('ev-table').querySelector('tbody').innerHTML = (d.evidence || [])
      .map((e, i) => `<tr><td>${i + 1}</td><td>${esc(e.kind)}</td>
        <td class="mono">${esc(e.ref)}</td><td>${esc(e.detail)}</td></tr>`).join('')
      || '<tr><td colspan="4" class="dim">无证据</td></tr>';

    // 候选动作
    const list = $('cand-list');
    if (!d.candidates || !d.candidates.length) {
      list.innerHTML = '<p class="hint">本场景无需变更动作（只读诊断即为正确处置）。</p>';
      return;
    }
    list.innerHTML = '';
    d.candidates.forEach(c => {
      const div = document.createElement('div');
      div.className = 'cand';
      const tag = c.mutating
        ? '<span class="cand-note">写操作 · 需人工确认</span>'
        : '<span class="cand-ro">只读 · 自动放行</span>';
      div.innerHTML = `<div class="cand-body">
          <div class="cand-tool">${esc(c.tool)}</div>
          <div class="cand-why">${esc(c.rationale)}</div>
          ${c.note ? `<div class="cand-note">⚠️ ${esc(c.note)}</div>` : ''}
          <div style="margin-top:5px">${tag}</div>
        </div>
        <button class="btn ${c.mutating ? 'btn-primary' : 'btn-ghost'}">
          ${c.mutating ? '生成确认卡片' : '执行只读'}
        </button>`;
      div.querySelector('button').onclick = () => this.propose(c.index);
      list.appendChild(div);
    });
  },

  // ── 方案（确认卡片） ──────────────────────────────────
  async propose(index) {
    try {
      const p = await api('/api/propose', {
        diagnosis_id: this.state.diagnosis.diagnosis_id, candidate_index: index
      });
      this.renderProposal(p);
    } catch (e) { toast('方案生成被拒：' + e.message, 'err'); }
  },

  renderProposal(p) {
    const box = $('proposal-box');
    box.classList.remove('hidden');
    const blocked = p.blocked;
    const tier = p.effective_tier;

    const impact = (p.impact_rows || []).map(([k, v]) =>
      `<div class="k">${esc(k)}</div><div>${esc(v)}</div>`).join('');
    const notes = (p.impact_notes || []).map(n =>
      `<div></div><div class="finding">• ${esc(n)}</div>`).join('');

    const ev = (p.evidence || []).slice(0, 6).map(e =>
      `<div class="ev-item">[${esc(e.kind)}] ${esc(e.ref)} — ${esc(e.detail)}</div>`).join('');

    const dry = tier === 'T3'
      ? '<span class="dry-bad">— 禁止动作，未进入 dry-run（直接拒绝）</span>'
      : (p.dry_run_ok
        ? `<span class="dry-ok">✅ 服务端 dry-run 通过</span> — ${esc(p.dry_run_output)}`
        : `<span class="dry-bad">❌ dry-run 未通过</span> — ${esc(p.dry_run_output)}`);

    const breaches = (p.breaches || []).length
      ? p.breaches.map(b => `<div class="breach ${b.severity}">
          ${b.severity === 'block' ? '⛔' : '⚠️'} <b>${esc(b.rule)}</b>：${esc(b.detail)}</div>`).join('')
      : '<div class="dry-ok">✅ 全部熔断检查通过</div>';

    const title = blocked ? '⛔ 方案已被熔断拦截' : '⚠️ 待人工确认';

    // 拦截原因必须在**最显眼处**——原先只在卡片最底部的「熔断检查」里，
    // 用户在底部只看到一个灰按钮，会以为"点了没反应"。
    const blocks = (p.breaches || []).filter(b => b.severity === 'block');
    const whyBlocked = blocked ? `
      <div class="block-banner">
        <div class="block-title">为什么不能执行</div>
        ${blocks.map(b => `<div>⛔ <b>${esc(b.rule)}</b>：${esc(b.detail)}</div>`).join('')}
        ${p.cooldown_left ? `<div class="dim">还需要等待约 ${p.cooldown_left} 秒（页面会自动刷新审计，稍后重新诊断即可）</div>` : ''}
      </div>` : '';

    box.innerHTML = `
      <div class="confirm ${blocked ? 'blocked' : ''}">
        <div class="confirm-head">
          <span>${title}</span>
          <span class="tier-${tier}">${tier} ${esc(p.tier_label)} · ${esc(p.confirm_strength)}</span>
        </div>
        <div class="confirm-body">
          ${whyBlocked}
          ${p.is_mitigation ? `
            <div class="block-banner" style="border-left-color:var(--amber);background:#d2992215;border-color:#d2992255">
              <div class="block-title" style="color:var(--amber)">⚠️ 这只是缓解，不根治</div>
              <div>执行后服务会恢复，但**根因还在**，过一段时间可能复发。
              如果下方还有别的候选动作，建议优先选根治性的那个。</div>
            </div>` : ''}
          <div class="sec">
            <div class="sec-title">建议动作</div>
            <div class="kv">
              <div class="k">动作</div><div class="mono">${esc(p.tool)}</div>
              <div class="k">目标</div><div class="mono">${esc(p.target.kind)}/${esc(p.target.name)} (ns=${esc(p.target.namespace)})</div>
              <div class="k">参数</div><div class="mono">${esc(JSON.stringify(p.params))}</div>
            </div>
          </div>
          <div class="sec">
            <div class="sec-title">为什么要做</div>
            <div>${mdBold(p.rationale || '（未提供理由）')}</div>
            ${ev}
          </div>
          <div class="sec">
            <div class="sec-title">影响面</div>
            <div class="kv">${impact}${notes}</div>
          </div>
          <div class="sec">
            <div class="sec-title">dry-run 校验</div>
            <div>${dry}</div>
          </div>
          <div class="sec">
            <div class="sec-title">回滚</div>
            <div>${esc(p.rollback || '无自动回滚路径')}</div>
            <div class="dim">预计恢复时间：${esc(p.rollback_eta)}</div>
          </div>
          <div class="sec">
            <div class="sec-title">熔断检查</div>
            ${breaches}
          </div>
        </div>
        <div class="confirm-actions">
          <button class="btn ${blocked ? 'btn-blocked' : 'btn-ok'}" id="btn-approve" ${blocked ? 'disabled' : ''}
                  title="${blocked ? esc((p.breaches || []).filter(b => b.severity === 'block').map(b => b.rule + '：' + b.detail).join('；')) : ''}">
            ${blocked ? '⛔ ' + esc((p.breaches || []).filter(b => b.severity === 'block').map(b => b.rule).join('、') || '已被拦截') : '确认执行'}
          </button>
          <button class="btn" id="btn-reject">取消</button>
        </div>
      </div>`;

    if (!blocked) $('btn-approve').onclick = () => this.decide(p.proposal_id, true);
    $('btn-reject').onclick = () => this.decide(p.proposal_id, false);
    box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  },

  async decide(proposalId, approved) {
    const box = $('proposal-box');

    if (!approved) {
      const r = await askConfirm('取消这次变更？', '不会对集群做任何改动。', false, '确认取消');
      if (!r.ok) return;
      return this._submit(box, proposalId, false, '用户取消');
    }

    // 写操作：必须填理由，且审批人即责任人
    const r = await askConfirm(
      '这是写操作，确认执行？',
      '审批人即本次变更的责任人。你的决定与理由会写入审计日志。',
      true, '确认执行'
    );
    if (!r.ok) return;
    return this._submit(box, proposalId, true, r.reason || 'web 界面确认');
  },

  async _submit(box, proposalId, approved, reason) {
    // 防重复点击：立刻把按钮禁掉并给出"执行中"反馈
    const approveBtn = $('btn-approve'), rejectBtn = $('btn-reject');
    if (approveBtn) approveBtn.disabled = true;
    if (rejectBtn) rejectBtn.disabled = true;
    toast(approved ? '正在执行…' : '正在取消…');

    try {
      const res = await api('/api/decide',
        { proposal_id: proposalId, approved, reason });
      // 关键：**整块替换**卡片，而不是把结果插在它上方——
      // 卡片很高，用户在底部点按钮，结果出现在视野外就会以为"没反应"。
      const cls = res.status === 'success' ? 'success'
                : (res.status === 'cancelled' ? 'cancelled' : 'failed');
      box.innerHTML = `<div class="result-big ${cls}">
          <div class="rtitle">${res.status === 'success' ? '✅ 执行成功'
            : res.status === 'cancelled' ? '🚫 已取消' : '❌ 未执行'}</div>
          <div>${esc(res.output || res.error || '')}</div>
          <div class="dim" style="margin-top:6px">耗时 ${res.duration_ms}ms</div>
        </div>
        <p class="hint">想再操作一次，请重新诊断生成新的方案。</p>`;
      box.scrollIntoView({ behavior: 'smooth', block: 'center' });
      toast(res.status === 'success' ? '已执行' : `结果：${res.status}`,
            res.status === 'success' ? 'ok' : 'err');
      this.refreshAudit();
      this.refreshKnowledge();
      setTimeout(() => this.loadWorkloads(), 2500);
    } catch (e) {
      if (approveBtn) approveBtn.disabled = false;
      if (rejectBtn) rejectBtn.disabled = false;
      toast('执行失败：' + e.message, 'err');
    }
  },

  // ── 越界请求演示 ──────────────────────────────────────
  async demoRefuse(rule) {
    try {
      const r = await api('/api/refuse', { rule, request: `web 界面请求：${rule}` });
      $('idle').classList.add('hidden');
      $('diagnosis').classList.add('hidden');
      const box = $('proposal-box');
      box.classList.remove('hidden');
      box.innerHTML = `<div class="confirm blocked">
          <div class="confirm-head"><span>⛔ 请求被拒绝</span><span class="tier-T3">T3 禁止</span></div>
          <div class="confirm-body">
            <div class="sec"><div class="sec-title">规则</div>
              <div class="mono">${esc(r.rule_id)}</div></div>
            <div class="sec"><div class="sec-title">说明</div><div>${mdBold(r.desc)}</div></div>
            <div class="sec"><div class="sec-title">替代建议</div><div>${esc(r.hint)}</div></div>
          </div></div>`;
      toast('已拒绝并留痕', 'ok');
      this.refreshAudit();
    } catch (e) { toast(e.message, 'err'); }
  },

  // ── 右栏 ──────────────────────────────────────────────
  async refreshAudit() {
    try {
      const d = await api('/api/audit');
      $('audit-list').innerHTML = (d.records || []).slice().reverse().map(r => {
        const p = r.payload || {};
        const summary = p.conclusion || p.output || p.tool || p.request || p.reason || '';
        return `<li>
          <span class="audit-seq">#${r.seq}</span>
          <span class="audit-ev">${esc(r.event)}</span>
          <div class="dim">${esc(r.ts.slice(0, 19))} ${esc(String(summary).slice(0, 80))}</div>
        </li>`;
      }).join('') || '<li class="dim">暂无记录</li>';
    } catch (e) { /* 忽略 */ }
  },

  async refreshKnowledge() {
    try {
      const d = await api('/api/knowledge');
      const st = d.stats || {};
      const sigs = Object.entries(st.by_signature || {}).map(([k, v]) =>
        `<div class="kb-row"><span class="kb-sig">${esc(k)}</span><span class="n">${v}</span></div>`).join('');
      $('kb-stats').innerHTML = `<div class="card">
          <div class="kb-row"><span class="dim">累计条目</span><span class="n">${st.total || 0}</span></div>
          <div class="kb-row"><span class="dim">涉及工作负载</span><span class="n">${st.workloads || 0}</span></div>
          ${sigs}
        </div>`;
      $('kb-list').innerHTML = (d.recent || []).map(e => `<li>
          <span class="kb-sig">${esc(e.signature)}</span>
          <div class="dim">${esc(e.workload)} · ${esc(e.treatment || '无动作')} → ${esc(e.outcome)}</div>
          <div class="dim">${mdBold(String(e.root_cause).slice(0, 90))}</div>
        </li>`).join('') || '<li class="dim">暂无沉淀</li>';
    } catch (e) { /* 忽略 */ }
  }
};

window.App = App;
App.init();
setInterval(() => App.refreshAudit(), 15000);
