/* 简化版演示页的逻辑。
 *
 * 设计原则：**把术语全部翻译成人话**。
 * 用户看到的应该是"程序起来就崩，反复重启"，
 * 而不是 "crashloop / CrashLoopBackOff"。
 */

const $ = (id) => document.getElementById(id);

// ── 术语 → 人话 ────────────────────────────────────────

const VERDICT = {
  oom_killed:   ['bad',  '💥 内存不够用，被系统杀掉了'],
  crashloop:    ['bad',  '🔁 程序起来就崩，一直在重启'],
  image_pull:   ['bad',  '📦 要运行的程序包拉不下来'],
  pending_unschedulable: ['bad', '⏳ 没有机器满足它的运行条件'],
  not_ready:    ['warn', '🟡 程序在跑，但还没准备好接流量'],
  probe_kill:   ['warn', '🩺 健康检查太严，把正常的程序误杀了'],
  no_pods:      ['bad',  '🕳️ 一个都没跑起来'],
  healthy:      ['good', '✅ 它是健康的，不用管'],
  multi_root_cause: ['warn', '🧩 同时发现了几个问题'],
  service_no_endpoints: ['bad', '🚧 程序是好的，但流量送不到它那里'],
  service_target_port_mismatch: ['bad', '🚧 流量被转到了没人监听的端口'],
  config_misconfiguration: ['bad', '⚙️ 配置里的地址写错了'],
  dependency_unavailable: ['warn', '🔌 它依赖的服务自己挂了（不是它的问题）'],
  network_blocked: ['warn', '🚧 网络不通，但配置和依赖都正常'],
  node_failure: ['bad',  '🖥️ 它所在的机器出问题了'],
  node_pressure: ['bad', '🖥️ 机器资源紧张，程序被赶走了'],
  quota_exhausted: ['bad', '📊 这个环境的资源额度用完了'],
  limitrange_violation: ['bad', '📏 申请的资源超过了环境允许的上限'],
  ingress_backend_invalid: ['bad', '🚪 外部访问的入口指向了不存在的地方'],
  unknown:      ['warn', '❓ 现有信息不够，它不敢下结论'],
};

const ACTION = {
  rollout_restart: '重启服务（把程序逐个换成新的）',
  rollout_undo:    '回退到上一个版本',
  scale_workload:  '调整运行的数量',
  patch_resources: '调整内存上限',
  delete_pod:      '删掉一个程序实例让它重建',
  rollback_configmap: '把配置回退到上一个版本',
};

const FAULT_LABEL = {
  oom: '内存不够用', crash: '程序起来就崩',
  image: '程序包找不到', pending: '没机器能跑它',
};

// ── 小工具 ────────────────────────────────────────────

function toast(msg, err) {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast' + (err ? ' err' : '');
  t.hidden = false;
  clearTimeout(t._h);
  t._h = setTimeout(() => { t.hidden = true; }, 6000);
}

async function api(path, body) {
  const r = await fetch(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const d = await r.json().catch(() => ({ error: '返回内容无法解析' }));
  if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
  return d;
}

/** 清洗日志文本。
 *
 * 后端有时返回的是 Python 的 bytes repr —— `b'[2026-...]\n...'`。
 * 对第一次接触运维的人来说这是天书，必须还原成真正的多行文本。 */
function cleanLog(s) {
  let t = String(s || '');
  t = t.replace(/^b['"]/, '').replace(/['"]$/, '');   // 去掉 b'...' 外壳
  t = t.replace(/\\r\\n|\\n/g, '\n').replace(/\\t/g, '  ');  // 转义还原成真换行
  t = t.replace(/\\"/g, '"').replace(/\\\\/g, '\\');
  const lines = t.split('\n').filter((l) => l.trim());
  // 日志的**末尾**才是关键（错误通常最后打），保留最后 8 行
  return lines.slice(-8).join('\n');
}

/** Service 保护规则翻译成人话 */
function plainPdb(v) {
  const m = /minAvailable=(\d+)/.exec(v || '');
  const n = /当前允许中断\s*(\d+)/.exec(v || '');
  if (!m) return v;
  let out = `要求至少 ${m[1]} 个实例保持在线`;
  if (n) out += n[1] === '0'
    ? '，所以维护期间一个都不能停'
    : `，最多可以停 ${n[1]} 个`;
  return out;
}

/** 把 Markdown 的粗体/行内代码转成 HTML —— 后端结论里会用到。 */
function fmt(text) {
  return (text || '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
    .replace(/`([^`]+)`/g, '<code>$1</code>');
}

// ── 状态 ──────────────────────────────────────────────

const S = { fault: null, diag: null, prop: null, busy: false };

// ── 第 1 步：制造故障 ─────────────────────────────────

document.querySelectorAll('.choice').forEach((btn) => {
  btn.onclick = async () => {
    if (S.busy) return;
    const fault = btn.dataset.fault;
    S.busy = true;
    btn.innerHTML = `<b><span class="spinner"></span>正在制造故障…</b>`;
    try {
      const r = await api('/api/sandbox/fault', { scenario: fault });
      if (!r.ok) throw new Error((r.output || '').slice(0, 120) || '注入失败');
      S.fault = fault;
      collapse('s1');
      // 让故障显现
      $('s2').hidden = false;
      $('wait-hint').hidden = false;
      $('s2-sub').textContent = `已经制造了「${FAULT_LABEL[fault]}」，现在让 Agent 去看一眼`;
      $('s2').scrollIntoView({ behavior: 'smooth', block: 'center' });
    } catch (e) {
      toast('制造失败：' + e.message, true);
    } finally {
      btn.innerHTML = btn.innerHTML.replace(/<span class="spinner"><\/span>/, '');
      S.busy = false;
      // 复原按钮文案（简单起见重新载入原始文本）
      renderFaultButtons();
    }
  };
});

const FAULT_TEXT = {
  oom:     ['💥 内存不够用', '程序吃内存超过了上限，被系统杀掉'],
  crash:   ['🔁 程序起来就崩', '启动时报错退出，然后不停重启'],
  image:   ['📦 程序包找不到', '要运行的程序包拉不下来'],
  pending: ['⏳ 没机器能跑它', '要求的机器条件没人满足'],
};

function renderFaultButtons() {
  document.querySelectorAll('.choice').forEach((b) => {
    const [t, s] = FAULT_TEXT[b.dataset.fault];
    b.innerHTML = `<b>${t}</b><span>${s}</span>`;
  });
}

// ── 第 2 步：诊断 ─────────────────────────────────────

$('wait-ok').onclick = () => { $('wait-hint').hidden = true; };

$('btn-diagnose').onclick = async () => {
  if (S.busy) return;
  S.busy = true;
  const btn = $('btn-diagnose');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>正在查看…';

  try {
    // 先看这个环境里有哪些工作负载
    const { workloads } = await api('/api/workloads', { namespace: 'demo' });
    const target =
      workloads.find((w) => w.name === 'api-gateway') || workloads[0];
    if (!target) throw new Error('演示环境里没有找到可诊断的服务');

    const d = await api('/api/diagnose', {
      namespace: target.namespace, workload: target.name,
      kind: target.kind, planner: 'rule', turns: 1,
    });
    S.diag = d;
    collapse('s2');
    renderConclusion(d);
    $('s3').hidden = false;
    $('s3').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) {
    toast('诊断失败：' + e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = '开始诊断';
    S.busy = false;
  }
};

// ── 第 3 步：结论 + 证据 ──────────────────────────────

function renderConclusion(d) {
  const [cls, label] = VERDICT[d.signature] || ['warn', '❓ ' + d.signature];
  $('verdict').className = 'verdict ' + cls;
  $('verdict').textContent = label;
  $('conclusion').innerHTML = fmt(d.conclusion);

  const ev = $('evidence');
  const items = d.evidence || [];
  ev.innerHTML = items.length
    ? items.map((e) => {
        const isLog = /log/i.test(e.kind || '') || /\n|b'/.test(e.detail || '');
        const detail = isLog
          ? `<div class="det log">${cleanLog(e.detail).replace(/</g, '&lt;')}</div>`
          : `<div>${fmt(e.detail)}</div>`;
        return `<div class="ev">
          <div class="ref">${fmt(e.ref)}</div>${detail}
          <div class="det">来源：${fmt(e.source)}</div></div>`;
      }).join('')
    : '<p class="muted small">没有额外证据。</p>';

  // 有候选动作才进第 4 步
  if ((d.candidates || []).length) {
    $('s4').hidden = false;
    loadProposal(0);
  } else {
    $('s4').hidden = true;
    showOutcome('ok', '不需要动手',
      'Agent 没有给出任何修复动作——这次要么是健康的，要么根因不在这个服务身上，' +
      '不该乱动。\n\n这本身就是一个正确结果：**不动手也是一种处置**。');
  }
}

$('toggle-evidence').onclick = () => {
  const ev = $('evidence');
  ev.hidden = !ev.hidden;
  $('toggle-evidence').textContent =
    (ev.hidden ? '▸' : '▾') + ' 它看到了哪些证据';
};

// ── 第 4 步：方案与决定 ───────────────────────────────

async function loadProposal(index) {
  const c = S.diag.candidates[index];
  try {
    const p = await api('/api/propose', {
      diagnosis_id: S.diag.diagnosis_id, candidate_index: index,
    });
    S.prop = p;
    renderPlan(p, c);
  } catch (e) {
    toast('生成方案失败：' + e.message, true);
  }
}

function renderPlan(p, c) {
  const blocked = !!p.blocked;
  const blocks = (p.breaches || []).filter((b) => b.severity === 'block');
  const imp = p.impact || {};

  const rows = [];
  rows.push(['它想做什么', ACTION[p.tool] || p.tool]);
  if (c && c.rationale) rows.push(['为什么', fmt(c.rationale)]);
  if (c && c.note) rows.push(['⚠️ 注意', fmt(c.note)]);

  // 影响面：翻译成人话
  if (imp.replicas) rows.push(['会动到几个', `${imp.replicas} 个程序实例`]);
  if (imp.pods) rows.push(['涉及的实例', `${imp.pods} 个`]);
  if (imp.stateful) rows.push(['有数据吗', '⚠️ 是有状态服务，动它要小心']);
  if (imp.pvc) rows.push(['有大盘数据吗', '⚠️ 挂着持久化存储']);
  if (imp.pdb) rows.push(['保护规则', plainPdb(imp.pdb)]);
  if (imp.services && imp.services.length)
    rows.push(['哪些入口受影响', imp.services.join('、')]);

  if (p.dry_run_ok) rows.push(['试跑结果', '✅ 已经先试跑过一遍，确认能成功']);

  const why = blocked ? `
    <div class="blocked">
      <div class="t">这项操作被安全规则拦住了</div>
      ${blocks.map((b) => `<div>⛔ ${fmt(b.rule)}：${fmt(b.detail)}</div>`).join('')}
      ${p.cooldown_left ? `<div class="muted small" style="margin-top:6px">
         大约还需要等 ${p.cooldown_left} 秒。这是防止反复折腾服务的保护机制。</div>` : ''}
    </div>` : '';

  $('plan').innerHTML = `
    ${why}
    <p class="plan-title">它建议这么修</p>
    <p class="plan-sub">这只是建议。要不要做，由你决定。</p>
    <table class="tbl">
      ${rows.map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join('')}
    </table>`;

  const yes = $('btn-yes');
  yes.disabled = blocked;
  yes.textContent = blocked ? '⛔ 已被拦住，不能执行' : '✅ 就这么修';
}

$('btn-yes').onclick = () => decide(true);

$('btn-no').onclick = async () => {
  if (!S.prop) return;
  showOutcome('ok', '你选择了不动手',
    '这是完全合理的决定。Agent 的价值是帮你把情况看清楚，' +
    '而不是替你做主。');
};

async function decide(approved) {
  if (S.busy || !S.prop) return;
  S.busy = true;
  const btn = $('btn-yes');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>正在执行…';
  try {
    const r = await api('/api/decide', {
      proposal_id: S.prop.proposal_id, approved, reason: '演示页确认',
    });
    if (r.status === 'success') {
      showOutcome('ok', '✅ 修好了', (r.output || '') +
        `\n\n耗时 ${r.duration_ms} 毫秒。整个过程已经记进审计日志。`);
    } else {
      showOutcome('fail', '⚠️ 没能执行', r.output || r.error || '未知原因');
    }
  } catch (e) {
    showOutcome('fail', '⚠️ 没能执行', e.message);
  } finally {
    S.busy = false;
  }
}

function showOutcome(kind, title, detail) {
  ['s1', 's2', 's3', 's4'].forEach(collapse);
  $('s5').hidden = false;
  $('outcome').innerHTML = `<div class="outcome ${kind === 'ok' ? 'ok' : 'fail'}">
      <div>${title}</div>
      <div class="detail">${fmt(detail)}</div></div>`;
  $('s5').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

// ── 步骤收起 ─────────────────────────────────────────
//
// 走完一步就把它折成一行。否则滚到第 4 步时，
// 第 1 步的四个按钮还占着半屏，用户会以为自己点错了。

function collapse(id) {
  const el = $(id);
  if (el) el.classList.add('done');
}

// ── 重来 ──────────────────────────────────────────────

$('btn-again').onclick = async () => {
  try { await api('/api/sandbox/fault', { scenario: 'reset' }); } catch (e) { /* 忽略 */ }
  ['s1', 's2', 's3', 's4', 's5'].forEach((id) => {
    $(id).hidden = true;
    $(id).classList.remove('done');
  });
  $('evidence').hidden = true;
  $('toggle-evidence').textContent = '▸ 它看到了哪些证据';
  S.fault = S.diag = S.prop = null;
  renderFaultButtons();
  window.scrollTo({ top: 0, behavior: 'smooth' });
  toast('已恢复原状，可以再来一次');
};
