/* 集群大屏前端。
 *
 * 三条设计约束，都是"挂在墙上"这个场景要求的：
 *   1. **整屏不滚动** —— 面板内部各自滚，页面本身永远不出现滚动条
 *   2. **没人操作** —— 5 秒自刷，不需要点任何东西
 *   3. **一眼分好坏** —— 颜色承担主要信息量，字要够大
 */

const $ = (id) => document.getElementById(id);
const CSRF = { 'X-Requested-With': 'omagent' };
const REFRESH_MS = 5000;
const NAMESPACES = ['demo', 'staging', 'observability'];

let timer = null;
let lastFetch = 0;
let authed = false;

class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

async function api(method, path, body, { timeout = 15000 } = {}) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeout);
  try {
    const headers = { ...CSRF };
    if (body) headers['Content-Type'] = 'application/json';
    const res = await fetch(path, {
      method, headers, body: body ? JSON.stringify(body) : undefined, signal: ctrl.signal,
    });
    const data = await res.json().catch(() => ({ error: '响应不是 JSON' }));
    if (!res.ok) throw new ApiError(data.error || `HTTP ${res.status}`, res.status);
    return data;
  } catch (e) {
    if (e.name === 'AbortError') throw new ApiError('请求超时', 0);
    if (e instanceof TypeError) throw new ApiError('连不上服务', 0);
    throw e;
  } finally { clearTimeout(t); }
}

const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const pctClass = (v) => (v == null ? '' : v >= 85 ? 'bad' : v >= 65 ? 'warn' : '');

/** 字节 → 人类可读 */
function hb(n) {
  if (n == null) return '—';
  const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let i = 0;
  while (Math.abs(n) >= 1024 && i < u.length - 1) { n /= 1024; i += 1; }
  return `${n < 10 ? n.toFixed(1) : Math.round(n)} ${u[i]}`;
}

/** CPU 核数 → "240m" / "1.2 核" */
function hcpu(c) {
  if (c == null) return '—';
  if (c < 1) return `${Math.round(c * 1000)}m`;
  if (c < 10) return `${c.toFixed(2)} 核`;
  return `${Math.round(c)} 核`;
}
const repClass = (w) => (!w.healthy ? (w.ready === 0 ? 'bad' : 'warn') : 'ok');

// ── 渲染 ─────────────────────────────────────────────────

function renderKpis(s) {
  const items = [
    { label: '工作负载', value: s.workloads, sub: `${s.namespaces} 个命名空间`,
      cls: s.unhealthy ? 'warn' : 'ok' },
    { label: '异常', value: s.alert_total, sub: s.alert_total ? '需要处理' : '一切正常',
      cls: s.alert_total ? 'bad' : 'ok' },
    { label: 'Pod', value: s.pods, sub: '运行中' },
    { label: '节点', value: `${s.nodes_ready}/${s.nodes}`,
      sub: s.nodes_unschedulable ? `${s.nodes_unschedulable} 个不可调度` : '全部就绪',
      cls: s.nodes_ready < s.nodes ? 'bad' : (s.nodes_unschedulable ? 'warn' : 'ok') },
    // 大数字给**绝对用量**，占比放副标题。只给百分比的话，
    // 在核多内存大的机器上它永远是 0%，等于没显示。
    { label: 'CPU 用量', value: hcpu(s.cpu_cores),
      sub: s.has_metrics ? `占可分配 ${s.cpu_pct == null ? '?' : s.cpu_pct}% · ${s.cpu_alloc} 核`
                         : '缺少指标',
      cls: pctClass(s.cpu_pct) },
    { label: '内存用量', value: hb(s.mem_bytes),
      sub: s.has_metrics ? `占可分配 ${s.mem_pct == null ? '?' : s.mem_pct}% · ${hb(s.mem_alloc)}`
                         : '缺少指标',
      cls: pctClass(s.mem_pct) },
  ];
  $('kpis').innerHTML = items.map((k) => `
    <div class="kpi ${k.cls || ''}">
      <div class="k-label">${esc(k.label)}</div>
      <div class="k-value">${esc(k.value)}</div>
      <div class="k-sub">${esc(k.sub || '')}</div>
    </div>`).join('');
}

function renderWorkloads(groups) {
  let total = 0, bad = 0;
  const html = groups.map((g) => {
    if (g.error) {
      return `<div class="ns-group"><div class="ns-head"><b>${esc(g.namespace)}</b>
        <span style="color:var(--bad)">读取失败：${esc(g.error)}</span></div></div>`;
    }
    total += g.workloads.length;
    const unhealthy = g.workloads.filter((w) => !w.healthy).length;
    bad += unhealthy;
    const cards = g.workloads.map((w) => `
      <div class="wl ${repClass(w)}">
        <div class="wl-top">
          <span class="wl-name">${esc(w.name)}</span>
          <span class="wl-rep">${w.ready}/${w.desired}</span>
        </div>
        <div class="wl-sub">${esc(w.kind)}${w.restarts ? ` · 重启 ${w.restarts}` : ''}${
          w.healthy ? '' : ` · ${esc((w.problems || [])[0] || '异常')}`}</div>
        ${w.known_cases ? `<div class="wl-case">📚 ${w.known_cases} 条历史案例</div>` : ''}
      </div>`).join('');
    return `<div class="ns-group">
      <div class="ns-head"><b>${esc(g.namespace)}</b>
        <span>${g.workloads.length} 个负载</span>
        ${unhealthy ? `<span style="color:var(--bad)">· ${unhealthy} 个异常</span>` : '<span>· 全部正常</span>'}
        ${g.standalone && g.standalone.length ? `<span style="color:var(--warn)">· ${g.standalone.length} 个 Service/HPA 异常</span>` : ''}
      </div>
      <div class="wl-grid">${cards}</div>
    </div>`;
  }).join('');
  $('workloads').innerHTML = html || '<div class="empty">没有工作负载</div>';
  $('wl-sub').textContent = `${total} 个负载${bad ? ` · ${bad} 个异常` : ''}`;
}

function renderNodes(nodes) {
  $('nodes').innerHTML = nodes.map((n) => {
    const cpu = n.cpu_pct, mem = n.mem_pct;
    const bar = (label, v, abs) => `
      <div class="node-bar">
        <span class="lbl">${label}</span>
        <span class="track"><span class="fill ${pctClass(v)}" style="width:${v == null ? 0 : Math.max(1.5, v)}%"></span></span>
        <span class="abs">${abs}</span>
        <span class="pct">${v == null ? '—' : v + '%'}</span>
      </div>`;
    return `<div class="node ${n.ready ? '' : 'off'}">
      <div class="node-top">
        <span class="node-name">${esc(n.name.replace('om-sandbox-', ''))}</span>
        <span class="node-tags">${esc(n.zone || '')} ${esc(n.pool || '')} · ${n.pods} Pod</span>
      </div>
      ${bar('CPU', cpu, hcpu(n.cpu_cores))}${bar('内存', mem, hb(n.mem_bytes))}
      ${n.unschedulable ? '<div class="node-sched">⛔ 已封锁，不接受新 Pod</div>' : ''}
      ${n.pressured && n.pressured.length ? `<div class="node-sched">⚠️ ${esc(n.pressured.join('、'))}</div>` : ''}
    </div>`;
  }).join('') || '<div class="empty">没有节点</div>';
}

function renderAlerts(alerts) {
  $('alert-count').textContent = alerts.length;
  $('alert-count').classList.toggle('bad', alerts.length > 0);
  if (!alerts.length) {
    $('alerts').innerHTML = '<div class="empty good">✓ 没有异常</div>';
    return;
  }
  $('alerts').innerHTML = alerts.map((a) => `
    <div class="alert">
      <div class="a-top">
        <span class="a-ns">${esc(a.namespace)}</span>
        <span class="a-name">${esc(a.name)}</span>
        ${a.desired ? `<span class="a-rep">${a.ready}/${a.desired}</span>` : ''}
      </div>
      ${(a.problems || []).map((p) => `<div class="a-prob">${esc(p)}</div>`).join('')}
      ${a.case_hint ? `<div class="a-case">📚 ${esc(a.case_hint)}</div>` : ''}
    </div>`).join('');
}

function renderActivity(acts) {
  const label = { exec: '执行', decision: '裁决', violation: '门禁', case: '沉淀', fault: '注入' };
  $('activity').innerHTML = acts.map((a) => `
    <div class="act ${esc(a.kind)}">
      <span class="t">${esc((a.ts || '').slice(11, 19))}</span>
      <span class="k">${esc(label[a.kind] || a.kind)}</span>
      <span class="txt">${esc(a.text)}</span>
    </div>`).join('') || '<div class="empty">暂无记录</div>';
}

function renderCases(cases) {
  $('cases').innerHTML = cases.map((c) => `
    <div class="case">
      <div class="c-top">
        <span class="c-wl">${esc(c.workload)}</span>
        <span class="c-tool">${esc(c.tool)}</span>
        <span class="c-by">${c.verified ? '✓ 已证实' : ''} ${esc(c.operator)} · ${esc((c.ts || '').slice(5, 16).replace('T', ' '))}</span>
      </div>
      <div class="c-sym">${esc(c.symptoms)}</div>
    </div>`).join('') || '<div class="empty">还没有沉淀任何处置</div>';
}

// ── 主循环 ───────────────────────────────────────────────

function tickClock() {
  const d = new Date();
  $('clock').textContent = d.toTimeString().slice(0, 8);
}

function renderUpdated() {
  if (!lastFetch) return;
  const ago = Math.round((Date.now() - lastFetch) / 1000);
  $('updated').textContent = ago <= 1 ? '刚刚更新' : `${ago} 秒前更新`;
}

async function refresh() {
  try {
    const w = await api('GET', `/api/wall?namespaces=${NAMESPACES.join(',')}`);
    renderKpis(w.summary);
    renderWorkloads(w.groups);
    renderNodes(w.nodes);
    renderAlerts(w.alerts);
    renderActivity(w.activity);
    renderCases(w.cases);

    const s = w.summary;
    const pill = $('health-pill');
    if (s.alert_total === 0) {
      pill.className = 'pill ok'; pill.textContent = '✓ 集群正常';
    } else if (s.alert_total <= 2) {
      pill.className = 'pill warn'; pill.textContent = `${s.alert_total} 处异常`;
    } else {
      pill.className = 'pill bad'; pill.textContent = `${s.alert_total} 处异常`;
    }
    lastFetch = Date.now();
    renderUpdated();
  } catch (e) {
    if (e.status === 401 || e.status === 403) {
      authed = false;
      if (timer) { clearInterval(timer); timer = null; }
      $('login-mask').hidden = false;
      return;
    }
    $('health-pill').className = 'pill bad';
    $('health-pill').textContent = `数据读取失败：${e.message}`;
  }
}

async function start() {
  authed = true;
  $('login-mask').hidden = true;
  try {
    const st = await api('GET', '/api/status');
    $('cluster-info').textContent = st.cluster.ok
      ? `${st.cluster.info} · ${st.operator}` : `集群不可达：${st.cluster.info}`;
  } catch { /* 状态拉不到不影响大屏主体 */ }
  await refresh();
  if (timer) clearInterval(timer);
  timer = setInterval(refresh, REFRESH_MS);
}

/** 按 1920×1080 的设计尺寸等比缩放到实际分辨率，并居中。
 *  这样"在设计尺寸下验证过的布局"在任何屏幕上原样成立——
 *  不需要为每个分辨率调字号，也不会出现某块面板被挤出去。
 *  16:9 时正好铺满；其它比例会有黑边，这对大屏是可接受的。 */
const DESIGN_W = 1920, DESIGN_H = 1080;
function fitStage() {
  const canvas = $('canvas');
  if (!canvas) return;
  const vw = window.innerWidth, vh = window.innerHeight;
  const scale = Math.min(vw / DESIGN_W, vh / DESIGN_H);
  canvas.style.transform = `scale(${scale})`;
  canvas.style.left = `${Math.round((vw - DESIGN_W * scale) / 2)}px`;
  canvas.style.top = `${Math.round((vh - DESIGN_H * scale) / 2)}px`;
  document.body.dataset.scale = scale.toFixed(3);
}

async function boot() {
  fitStage();
  window.addEventListener('resize', fitStage);
  tickClock();
  setInterval(tickClock, 1000);
  setInterval(renderUpdated, 1000);

  $('btn-full').addEventListener('click', () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else document.documentElement.requestFullscreen();
  });
  $('login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    $('login-btn').disabled = true;
    $('login-err').textContent = '';
    try {
      await api('POST', '/api/login', {
        username: $('login-user').value.trim(), password: $('login-pass').value,
      });
      $('login-pass').value = '';
      await start();
    } catch (err) {
      $('login-err').textContent = err.message || '登录失败';
    } finally {
      $('login-btn').disabled = false;
    }
  });

  try {
    const me = await api('GET', '/api/me');
    if (me.authenticated) await start();
    else $('login-mask').hidden = false;
  } catch (e) {
    $('login-mask').hidden = false;
    $('login-err').textContent = `连不上服务：${e.message}`;
  }
}

boot();
