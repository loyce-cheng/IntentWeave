import "./style.css";
import { ApiError, request, ResponseGuard } from "./api";
import { readArchives, writeArchives, type ArchiveReceipt } from "./archives";

type Action = { kind: string; target: string };
type State = {
  location: string;
  herb: number;
  medicine: number;
  stock: (number | null)[];
  roads: (boolean | null)[];
};
type Evidence = {
  value: boolean | number;
  source: string;
  observed_tick: number;
  valid_until: number;
};
type View = {
  tick: number;
  revision: number;
  status: string;
  running: boolean;
  mode: string;
  state_hash: string;
  goal: string;
  world: State;
  config: { policy: string };
  actor: {
    context: { state: State; knowledge_version: number };
    evidence: Record<string, Evidence>;
    plan: Action[];
    active: { action: Action; due_tick: number } | null;
    plan_version: number;
  };
  metrics: Record<string, number>;
  last_repair: {
    outcome?: string;
    reason?: string;
    retained?: number;
    removed?: number;
    budget_used?: number;
  };
  events: { seq: number; tick: number; type: string; [key: string]: unknown }[];
  event_count: number;
  model: {
    status: string;
    source?: string;
    usage: {
      known: boolean;
      input_tokens: number | null;
      output_tokens: number | null;
    } | null;
  };
  replay: { verified: number; total: number; complete: boolean } | null;
};
type Catalog = {
  app_version: string;
  places: string[];
  labels: Record<string, string>;
  roads: string[];
  goals: Record<string, string>;
  providers: { id: string; protocol: string }[];
  models_enabled: boolean;
};
type Pair = Record<
  string,
  {
    status: string;
    tick: number;
    metrics: Record<string, number>;
    cpu_ms: number;
    recording: unknown;
  }
>;
let catalog: Catalog;
let view: View;
let session = { id: "", token: "" };
let busy = false;
const responses = new ResponseGuard();
let pollTimer: ReturnType<typeof setTimeout> | undefined;
let polling = false;
let pollFailures = 0;
const MAX_POLL_FAILURES = 3;
let archives: ArchiveReceipt[] = [];
let archiveStorageReady = true;
let perspective = "world";
let pair: Pair | null = null;
const $ = <T extends HTMLElement = HTMLElement>(id: string) =>
  document.getElementById(id) as T;
const esc = (s: unknown) =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ]!,
  );
const statusLabel: Record<string, string> = {
  ready: "准备就绪",
  running: "执行中",
  success: "目标已完成",
  unknown: "当前认知下未找到计划",
  budget_exhausted: "规划预算耗尽",
  tick_limit: "达到步数上限",
  cancelled: "已取消",
  full: "完整规划",
  patched: "局部修复",
  fallback_full: "回退完整规划",
  observe: "先获取观察",
  pending: "等待模型",
  idle: "未调用",
  accepted: "提议已接纳",
  discarded_stale: "旧回复已丢弃",
};
const actionLabel: Record<string, string> = {
  move: "移动",
  take: "取药材",
  craft: "制作药物",
  deliver: "交付",
  inspect: "检查",
  wait: "等待",
};
const label = (p: string) => catalog.labels[p] ?? p;

$("app").innerHTML = `
<aside class="sidebar">
  <a class="brand" href="/"><span class="brand-mark">W</span><span>Worldmind<small>界行 · 行动实验室</small></span></a>
  <div class="side-label">WORKSPACE</div>
  <div class="side-active"><span>◉</span> 河谷救援 <span class="version" id="side-version"></span></div>
  <div class="side-note">观察 → 规划 → 行动 → 修复</div>
  <div class="side-bottom"><span class="dot"></span> 本地符号规划<br><small>模型为可选提议者<br>每一次行动都留下证据</small></div>
</aside>
<main>
  <header><div class="breadcrumb">Worldmind <span>/</span> 交互演示 <span>/</span> 河谷救援</div><span class="badge" id="app-version"></span></header>
  <section class="intro"><div><div class="eyebrow">OBSERVABLE NPC PLANNING</div><h1>世界改变，计划继续。</h1><p>改变道路与资源，观察角色如何发现问题、修复计划，并验证每一步。</p></div><div class="session-indicator"><span class="dot"></span><span id="mode">离线实时运行</span></div></section>
  <div id="error" role="alert" hidden></div>
  <section class="toolbar" aria-label="模拟控制">
    <label>规划策略<select id="policy"><option>Local</option><option>Full</option></select></label>
    <label class="goal-select">当前目标<select id="goal"></select></label>
    <button id="changeGoal">改变目标</button><span class="divider"></span>
    <button id="step">单步 →</button><button id="run" class="primary">▶ 运行</button><button id="pause">暂停</button><button id="reset">↺ 重置</button><button id="cancel" class="quiet">取消任务</button>
  </section>
  <section class="metrics" id="metrics" aria-label="资源指标"></section>
  <div class="workspace-grid">
    <section class="panel map-panel"><div class="panel-head"><div><span class="index">01</span><h2>河谷地图</h2></div><select id="perspective" aria-label="地图视角"><option value="world">世界真相 · 特权调试</option><option value="actor">角色已知信息</option></select></div>
      <div id="map" role="img" aria-label="河谷区域、道路与角色位置"></div>
      <div class="background-roles">医生：诊所接收交付 · 商人：仓库固定规则（默认等待）</div><div class="map-legend"><span><i class="line"></i>通行</span><span><i class="line blocked"></i>阻断</span><span><i class="line unknown"></i>未知</span><span><i class="actor-dot"></i>焦点快递员</span></div>
      <div class="interventions"><div><strong>改变世界</strong><small>远处的变化不会立即进入角色认知</small></div><div class="intervention-row"><label class="sr-only" for="road">选择道路</label><select id="road"></select><button id="block">阻断道路</button></div><div class="intervention-row"><label class="sr-only" for="material">选择材料位置</label><select id="material"><option value="garden">药圃药材</option><option value="warehouse">仓库药材</option></select><button id="remove">移除材料</button></div></div>
    </section>
    <section class="panel plan-panel"><div class="panel-head"><div><span class="index">02</span><h2>计划与恢复</h2></div><span id="plan-version" class="badge"></span></div><div id="plan"></div></section>
    <section class="panel"><div class="panel-head"><div><span class="index">03</span><h2>角色认知</h2></div><span class="badge">受限观察</span></div><div id="belief"></div></section>
    <section class="panel"><div class="panel-head"><div><span class="index">04</span><h2>执行轨迹</h2></div><span id="event-count" class="badge"></span></div><div id="timeline" class="timeline"></div></section>
  </div>
  <section class="panel lower"><div class="panel-head"><div><span class="index">05</span><h2>对照与验证</h2></div><button id="compare">运行 Full / Local 对照</button></div><p class="muted">从相同初态开始，在 tick 1 阻断东桥。相同动作、搜索器和总预算；局部修复可能花费更多。</p><div id="comparison" class="comparison"><div class="empty">运行一次对照，让结果回答问题。</div></div>
    <div class="replay-controls"><button id="export">↓ 导出当前记录</button><label class="file-button">↑ 导入并验证<input id="import" type="file" accept=".json,application/json" /></label><span id="replay-status">同版本输入重执行 · 每步状态及事件哈希检查</span></div>
    <div id="replay-controls" hidden><button id="replay-start">回放起点</button><button id="replay-next">校验下一步 →</button><label>记录位置 <input id="cursor" type="range" min="0" value="0" /></label><button id="replay-resume" class="primary">从此处继续</button><span class="muted">创建可执行分支，保留原存档与记录版本。</span></div>
  </section>
  <section class="panel lower" aria-label="持久回放存档"><div class="panel-head"><div><span class="index">06</span><h2>回放存档</h2></div><span class="badge">服务重启后可恢复</span></div>
    <div class="archive-controls"><label>存档名称<input id="archive-name" maxlength="80" placeholder="例如：断桥后的送药记录" /></label><button id="archive-save">保存当前回放</button></div>
    <p class="muted">存档保留 7 天。恢复并验证后，可选择任意记录位置，点击“从此处继续”续跑，再单步或运行。此浏览器保存访问凭据；清除浏览器数据会丢失入口。请导出 JSON 作为长期或跨设备备份。</p>
    <div id="archive-status" class="muted" role="status"></div><div id="archives" class="archive-list"></div>
  </section>
  <section class="panel lower"><div class="panel-head"><div><span class="index">07</span><h2>可选模型提议</h2></div><span id="model-enabled" class="badge"></span></div><p class="muted">模型只接收角色认知。提议需通过语法、语义和当前版本检查。密钥仅由服务端配置。</p><div class="model-controls"><label>服务配置<select id="provider"></select></label><label>演示访问口令<input id="model-access" type="password" autocomplete="off" placeholder="不是供应商 API Key" /></label><button id="model-propose">请求一次提议</button><button id="model-cancel">取消请求</button></div><div id="model-status" class="muted"></div></section>
  <footer><span>WORLD MIND / 有限世界，可解释行动。</span><span>运行会话在内存中；需要保留的轨迹请存档或导出。</span></footer>
</main>`;

const api = <T>(path: string, options: RequestInit = {}) =>
  request<T>(path, options, { ...session });
const spath = (id = session.id) => `/sessions/${id}`;

/** Only a live runner or model request can change without another user action. */
function schedulePoll() {
  clearTimeout(pollTimer);
  pollTimer = undefined;
  if (
    busy ||
    polling ||
    !session.id ||
    !view ||
    document.hidden ||
    pollFailures >= MAX_POLL_FAILURES ||
    (!view.running && view.model.status !== "pending")
  )
    return;
  pollTimer = setTimeout(refresh, 500 * 2 ** pollFailures);
}

async function refresh() {
  if (busy || polling || !session.id || document.hidden) return;
  clearTimeout(pollTimer);
  pollTimer = undefined;
  polling = true;
  const stamp = responses.capture(session.id);
  try {
    await updateView(spath(), { signal: AbortSignal.timeout(10_000) });
    if (responses.current(stamp, session.id)) pollFailures = 0;
  } catch (error) {
    if (responses.current(stamp, session.id)) {
      pollFailures =
        error instanceof ApiError && [404, 410].includes(error.status)
          ? MAX_POLL_FAILURES
          : pollFailures + 1;
      notify(error);
      if (pollFailures >= MAX_POLL_FAILURES)
        $("error").textContent += " · 自动刷新已暂停，请重新操作或刷新页面。";
    }
  } finally {
    polling = false;
    schedulePoll();
  }
}

async function updateView(path: string, options: RequestInit = {}) {
  const capability = { ...session };
  const stamp = responses.capture(capability.id);
  const latest = await request<View>(path, options, capability);
  if (responses.accept(stamp, session.id, latest.revision, view.revision)) {
    const changed =
      latest.revision !== view.revision ||
      latest.state_hash !== view.state_hash ||
      latest.running !== view.running ||
      JSON.stringify(latest.model) !== JSON.stringify(view.model);
    view = latest;
    if (changed) render();
    schedulePoll();
  }
}
function notify(error: unknown) {
  $("error").textContent =
    error instanceof Error ? error.message : String(error);
  $("error").hidden = false;
}
async function perform(fn: () => Promise<void>) {
  if (busy) return;
  busy = true;
  responses.invalidate();
  pollFailures = 0;
  schedulePoll();
  $("error").hidden = true;
  renderControls();
  try {
    await fn();
  } catch (error) {
    notify(error);
    if (error instanceof ApiError && [404, 410].includes(error.status))
      pollFailures = MAX_POLL_FAILURES;
    // Synchronize after a conflict, but never re-submit the user's mutation.
    if (error instanceof ApiError && error.status === 409) {
      try {
        await updateView(spath());
      } catch (refreshError) {
        notify(refreshError);
      }
    }
  } finally {
    busy = false;
    renderControls();
    schedulePoll();
  }
}
async function createSession() {
  if (session.id) {
    try {
      await api(spath(), { method: "DELETE" });
    } catch (error) {
      if (!(error instanceof ApiError) || ![404, 410].includes(error.status))
        throw error;
    }
  }
  const data = await api<{ id: string; token: string; view: View }>(
    "/sessions",
    {
      method: "POST",
      body: JSON.stringify({
        policy: $<HTMLSelectElement>("policy").value,
        goal: $<HTMLSelectElement>("goal").value,
      }),
    },
  );
  responses.invalidate();
  session = { id: data.id, token: data.token };
  view = data.view;
  pair = null;
  $("comparison").innerHTML =
    '<div class="empty">运行一次对照，让结果回答问题。</div>';
  render();
}
async function command(kind: string, target?: string) {
  await updateView(spath() + "/commands", {
    method: "POST",
    body: JSON.stringify({
      command_id: crypto.randomUUID(),
      expected_revision: view.revision,
      command: { kind, ...(target ? { target } : {}) },
    }),
  });
}
async function control(operation: string) {
  await updateView(spath() + "/control", {
    method: "POST",
    body: JSON.stringify({ operation }),
  });
}
function download(data: unknown, name: string) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }),
  );
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}
const positions: Record<string, [number, number]> = {
  clinic: [90, 90],
  warehouse: [90, 255],
  garden: [255, 295],
  workshop: [275, 125],
  bridge: [460, 90],
  ferry: [465, 275],
  home: [635, 175],
};
function map() {
  // The response already contains the authoritative actor-only projection.
  // Reading it avoids a second request and a map from a different revision.
  const s = perspective === "world" ? view.world : view.actor.context.state;
  const roads = catalog.roads
    .map((r, i) => {
      const [a, b] = r.split("|");
      const [x1, y1] = positions[a],
        [x2, y2] = positions[b];
      const state = s.roads[i];
      return `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" class="road ${state === false ? "closed" : state === null ? "unknown" : ""}"/>${state === false ? `<text x="${(x1 + x2) / 2}" y="${(y1 + y2) / 2 - 6}" class="blocked-label">阻断</text>` : ""}`;
    })
    .join("");
  const nodes = catalog.places
    .map((p, i) => {
      const [x, y] = positions[p];
      const active = p === s.location;
      return `<g class="node ${active ? "current" : ""}"><circle cx="${x}" cy="${y}" r="28"/><text x="${x}" y="${y + 5}" class="node-symbol">${p === "clinic" ? "✚" : p === "workshop" ? "⚒" : p === "home" ? "⌂" : p === "bridge" ? "⌁" : p === "ferry" ? "≈" : "◇"}</text><text x="${x}" y="${y + 48}" class="node-name">${esc(label(p))}</text><text x="${x}" y="${y + 65}" class="node-stock">药材 ${s.stock[i] ?? "未知"}</text>${active ? `<rect x="${x - 28}" y="${y - 53}" width="56" height="20" rx="10" class="courier-tag"/><text x="${x}" y="${y - 39}" class="courier-text">快递员</text>` : ""}</g>`;
    })
    .join("");
  $("map").innerHTML =
    `<svg viewBox="0 0 730 390" aria-hidden="true"><defs><pattern id="dots" width="22" height="22" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r="1" fill="#dfe4da"/></pattern></defs><rect width="730" height="390" fill="url(#dots)"/><path d="M365 -20 C420 95 345 170 410 260 S430 350 420 410" class="river"/>${roads}${nodes}</svg>`;
}
function render() {
  $("mode").textContent =
    view.mode === "verified_replay"
      ? "录制回放 · 逐步校验"
      : "离线模拟 · 实际执行";
  const m = view.metrics;
  $("metrics").innerHTML = [
    [
      "运行状态",
      view.status === "running" && !view.running
        ? "已暂停"
        : (statusLabel[view.status] ?? view.status),
      `TICK ${view.tick} · ${view.config.policy}`,
    ],
    [
      "决策预算",
      `${m.budget_used.toLocaleString()}`,
      `${m.expansions} 展开 + ${m.validations} 校验`,
    ],
    [
      "计划恢复",
      String(m.repairs),
      `${m.fallbacks} 次完整回退 · ${m.decisions} 次规划`,
    ],
    [
      "角色资源",
      `${view.world.herb} / ${view.world.medicine}`,
      "药材 / 药物 · 制作消耗 " + m.consumed,
    ],
  ]
    .map(
      ([title, value, detail]) =>
        `<article class="metric"><span>${esc(title)}</span><strong>${esc(value)}</strong><small>${esc(detail)}</small></article>`,
    )
    .join("");
  map();
  $("plan-version").textContent = `PLAN v${view.actor.plan_version}`;
  const repair = view.last_repair;
  $("plan").innerHTML =
    `<div class="goal-card"><small>当前承诺</small><strong>${esc(catalog.goals[view.goal])}</strong><span>当前位置：${esc(label(view.world.location))}</span></div>
  ${repair.outcome ? `<div class="repair"><strong>${esc(statusLabel[repair.outcome] ?? repair.outcome)}</strong><p>${esc(repair.reason)}</p><small>保留 ${repair.retained ?? 0} 步 · 移除 ${repair.removed ?? 0} 步 · 本次预算 ${repair.budget_used ?? 0}</small></div>` : ""}
  ${view.actor.active ? `<div class="active-action">制作进行中 · tick ${view.actor.active.due_tick} 完成<br><small>材料已消耗，取消不退款</small></div>` : ""}
  <ol class="plan-list">${view.actor.plan.map((a, i) => `<li><span class="step-number">${String(i + 1).padStart(2, "0")}</span><div><strong>${esc(actionLabel[a.kind] ?? a.kind)}</strong><small>${esc(label(a.target))}</small></div><span class="step-pending">待执行</span></li>`).join("")}</ol>
  ${!view.actor.plan.length ? `<div class="empty">${view.status === "success" ? "✓ 交付已完成，轨迹可导出验证。" : "暂无待执行计划，单步或运行开始搜索。"}</div>` : ""}`;
  const entries = Object.entries(view.actor.evidence);
  $("belief").innerHTML =
    `<div class="belief-top">认知版本 ${view.actor.context.knowledge_version} <span>亲眼观察与简报均有时效</span></div><div class="belief-table"><table><thead><tr><th>事实</th><th>已知值</th><th>来源 / 时效</th></tr></thead><tbody>${entries
      .map(([key, e]) => {
        const expired = e.valid_until <= view.tick;
        const title = key.startsWith("road:")
          ? key.slice(5).split("|").map(label).join(" ↔ ")
          : label(key.slice(6)) + "药材";
        return `<tr><td>${esc(title)}</td><td class="${expired ? "unknown-text" : ""}">${expired ? "未知（过期）" : typeof e.value === "boolean" ? (e.value ? "通行" : "阻断") : e.value}</td><td>${e.source === "direct" ? "直接观察" : "初始简报"} · t${e.observed_tick} → ${e.valid_until}</td></tr>`;
      })
      .join("")}</tbody></table></div>`;
  $("event-count").textContent = `${view.event_count} 事件`;
  const eventNames: Record<string, string> = {
    planning: "规划决策",
    action_started: "动作开始",
    action_completed: "动作完成",
    intervention: "玩家干预",
    goal_changed: "目标改变",
    interrupted: "动作中断",
    cancelled: "任务取消",
    merchant_rule: "商人规则",
    proposal_accepted: "模型提议接纳",
    proposal_rejected: "模型提议拒绝",
  };
  $("timeline").innerHTML =
    view.events
      .slice()
      .reverse()
      .map((e) => {
        const action = e.action as Action | undefined;
        const detail = action
          ? `${actionLabel[action.kind]} → ${label(action.target)}`
          : e.type === "planning"
            ? `${statusLabel[String(e.outcome)] ?? e.outcome} · ${e.reason}`
            : e.target
              ? String(e.target)
              : e.goal
                ? catalog.goals[String(e.goal)]
                : String(e.outcome ?? "");
        return `<div class="event"><span class="event-time">t${String(e.tick).padStart(2, "0")}</span><div><strong>${esc(eventNames[e.type] ?? e.type)}</strong><p>${esc(detail)}</p></div><span class="event-seq">#${e.seq}</span></div>`;
      })
      .join("") || '<div class="empty">行动发生后，证据会出现在这里。</div>';
  $("replay-controls").hidden = !view.replay;
  if (view.replay) {
    $("replay-status").textContent =
      `已验证 ${view.replay.verified} / ${view.replay.total} 步 · ${view.replay.complete ? "全部一致" : "逐步回放"}`;
    $<HTMLInputElement>("cursor").max = String(view.replay.total);
    $<HTMLInputElement>("cursor").value = String(view.replay.verified);
  } else
    $("replay-status").textContent =
      "同版本输入重执行 · 每步状态及事件哈希检查";
  const u = view.model.usage;
  $("model-status").textContent =
    `状态：${statusLabel[view.model.status] ?? view.model.status} · 用量：${u?.known ? `${u.input_tokens} 输入 / ${u.output_tokens} 输出 token` : "未知或未调用"} · 费用未知`;
  renderControls();
}

function renderControls() {
  if (!view) return;
  const terminal = !["ready", "running"].includes(view.status);
  for (const id of ["step", "run", "changeGoal", "block", "remove", "cancel"])
    $<HTMLButtonElement>(id).disabled =
      busy ||
      !!view.replay ||
      (terminal && ["step", "run", "cancel"].includes(id)) ||
      (view.running && ["step", "run"].includes(id));
  $<HTMLButtonElement>("step").disabled ||= view.model.status === "pending";
  for (const id of [
    "pause",
    "reset",
    "compare",
    "export",
    "replay-start",
    "replay-next",
    "replay-resume",
    "model-cancel",
  ])
    $<HTMLButtonElement>(id).disabled = busy;
  $<HTMLButtonElement>("pause").disabled ||= !view.running;
  $<HTMLButtonElement>("model-cancel").disabled ||=
    view.model.status !== "pending";
  $<HTMLButtonElement>("replay-start").disabled ||= view.replay?.verified === 0;
  $<HTMLButtonElement>("replay-next").disabled ||= !!view.replay?.complete;
  $<HTMLSelectElement>("policy").disabled = busy;
  $<HTMLInputElement>("import").disabled = busy;
  $<HTMLInputElement>("cursor").disabled = busy;
  $<HTMLButtonElement>("archive-save").disabled = busy || !archiveStorageReady;
  for (const button of Array.from(
    $("archives").querySelectorAll<HTMLButtonElement>("button"),
  )) {
    const receipt = archives[Number(button.dataset.restore)];
    button.disabled =
      busy ||
      (button.dataset.restore !== undefined &&
        receipt.expires_at * 1000 <= Date.now());
  }
  $<HTMLButtonElement>("model-propose").disabled =
    busy ||
    !catalog.models_enabled ||
    !!view.replay ||
    !!view.actor.active ||
    terminal ||
    view.model.status === "pending";
}

function renderPair() {
  if (!pair) return;
  $("comparison").innerHTML =
    `<table><thead><tr><th>策略</th><th>结果</th><th>步数</th><th>展开 / 校验</th><th>总预算</th><th>修复 / 回退</th><th>规划 CPU</th></tr></thead><tbody>${Object.entries(
      pair,
    )
      .map(
        ([k, r]) =>
          `<tr><td><strong>${k}</strong></td><td>${esc(statusLabel[r.status] ?? r.status)}</td><td>${r.tick}</td><td>${r.metrics.expansions} / ${r.metrics.validations}</td><td>${r.metrics.budget_used}</td><td>${r.metrics.repairs} / ${r.metrics.fallbacks}</td><td>${r.cpu_ms.toFixed(2)} ms</td></tr>`,
      )
      .join(
        "",
      )}</tbody></table><p class="muted">这是单个固定场景的实际运行，包含失败。CPU 计时受机器负载影响，不构成算法优越性结论。</p><button id="export-pair">导出两条对照轨迹</button>`;
  $("export-pair").onclick = () => download(pair, "worldmind-comparison.json");
}
async function seek(until: number) {
  if (until === view.replay?.verified) return;
  await updateView(spath() + "/replay", {
    method: "POST",
    body: JSON.stringify({ until }),
  });
}

function renderArchives() {
  $("archives").innerHTML = archives.length
    ? archives
        .map((archive, index) => {
          const expires = new Date(archive.expires_at * 1000).toLocaleString();
          const expired = archive.expires_at * 1000 <= Date.now();
          return `<article class="archive-row"><div><strong>${esc(archive.name)}</strong><small>${archive.step_count} 步 · ${esc(archive.schema_version)}<br>${expired ? "已到期" : "到期：" + esc(expires)}</small></div><div class="archive-actions"><button data-restore="${index}" ${expired ? "disabled" : ""}>恢复回放</button><button data-delete="${index}" class="quiet">删除存档</button></div></article>`;
        })
        .join("")
    : '<div class="empty">此浏览器尚无存档入口。保存后可在刷新页面或服务重启后恢复。</div>';
  for (const button of Array.from(
    $("archives").querySelectorAll<HTMLButtonElement>("[data-restore]"),
  )) {
    button.onclick = () =>
      perform(async () => {
        const archive = archives[Number(button.dataset.restore)];
        await updateView(spath() + `/archives/${archive.id}/restore`, {
          method: "POST",
          headers: { "X-Archive-Token": archive.token },
        });
        $("archive-status").textContent =
          `已验证并恢复“${archive.name}”。可回放查看，或点击“从此处继续”续跑。`;
      });
  }
  for (const button of Array.from(
    $("archives").querySelectorAll<HTMLButtonElement>("[data-delete]"),
  )) {
    button.onclick = () =>
      perform(async () => {
        const archive = archives[Number(button.dataset.delete)];
        let missing = false;
        try {
          await request(`/archives/${archive.id}`, {
            method: "DELETE",
            headers: { "X-Archive-Token": archive.token },
          });
        } catch (error) {
          if (!(error instanceof ApiError) || error.status !== 404) throw error;
          missing = true;
        }
        archives = readArchives().filter((item) => item.id !== archive.id);
        writeArchives(archives);
        renderArchives();
        $("archive-status").textContent = missing
          ? "已移除本地入口；服务端存档不存在、已到期或凭据无效。"
          : `已删除“${archive.name}”。`;
      });
  }
}

async function saveArchive() {
  archives = readArchives();
  if (archives.length >= 64)
    throw new Error("本地存档索引已满，请先删除不需要的存档。");
  writeArchives(archives);
  const name =
    $<HTMLInputElement>("archive-name").value.trim() ||
    `${catalog.goals[view.goal]} · tick ${view.tick}`;
  const receipt = await api<ArchiveReceipt>(spath() + "/archives", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
  archives = [...readArchives(), receipt];
  try {
    writeArchives(archives);
  } catch (error) {
    download(receipt, "worldmind-archive-access.json");
    renderArchives();
    throw new Error("浏览器无法保存存档入口，已下载访问凭据备份；请妥善保管。");
  }
  renderArchives();
  $("archive-status").textContent =
    `已保存“${receipt.name}”；服务重启后仍可恢复。`;
}

async function init() {
  catalog = await api<Catalog>("/catalog");
  $("side-version").textContent = catalog.app_version;
  $("app-version").textContent = `v${catalog.app_version}`;
  try {
    archives = readArchives();
    renderArchives();
  } catch (error) {
    archiveStorageReady = false;
    $("archive-status").textContent = String(error);
    $<HTMLButtonElement>("archive-save").disabled = true;
  }
  $("goal").innerHTML = Object.entries(catalog.goals)
    .map(([id, name]) => `<option value="${esc(id)}">${esc(name)}</option>`)
    .join("");
  $("road").innerHTML = catalog.roads
    .map(
      (r) =>
        `<option value="${esc(r)}">${r.split("|").map(label).join(" ↔ ")}</option>`,
    )
    .join("");
  $<HTMLSelectElement>("road").value = "bridge|home";
  $("provider").innerHTML = catalog.providers
    .map(
      (p) =>
        `<option value="${esc(p.id)}">${esc(p.id)} · ${esc(p.protocol)}</option>`,
    )
    .join("");
  $("model-enabled").textContent = catalog.models_enabled
    ? "需服务端访问口令"
    : "默认关闭 · 无 API 费用";
  $<HTMLButtonElement>("model-propose").disabled = !catalog.models_enabled;
  $<HTMLInputElement>("model-access").disabled = !catalog.models_enabled;
  await createSession();
  $("policy").onchange = () => perform(createSession);
  $("step").onclick = () => perform(() => command("step"));
  $("run").onclick = () => perform(() => control("run"));
  $("pause").onclick = () => perform(() => control("pause"));
  $("reset").onclick = () => perform(() => control("reset"));
  $("cancel").onclick = () => perform(() => command("cancel"));
  $("changeGoal").onclick = () =>
    perform(() => command("goal", $<HTMLSelectElement>("goal").value));
  $("block").onclick = () =>
    perform(() => command("block", $<HTMLSelectElement>("road").value));
  $("remove").onclick = () =>
    perform(() => command("remove", $<HTMLSelectElement>("material").value));
  $("perspective").onchange = () => {
    perspective = $<HTMLSelectElement>("perspective").value;
    map();
  };
  $("compare").onclick = () =>
    perform(async () => {
      pair = await api<Pair>(spath() + "/compare", { method: "POST" });
      renderPair();
    });
  $("export").onclick = () =>
    perform(async () =>
      download(await api(spath() + "/export"), "worldmind-recording.json"),
    );
  $("import").onchange = () =>
    perform(async () => {
      const file = $<HTMLInputElement>("import").files?.[0];
      if (!file) return;
      if (file.size > 2_000_000) throw new Error("记录文件不能超过 2 MB");
      await updateView(spath() + "/import", {
        method: "POST",
        body: JSON.stringify({ recording: JSON.parse(await file.text()) }),
      });
      $<HTMLInputElement>("import").value = "";
    });
  $("replay-start").onclick = () => perform(() => seek(0));
  $("replay-resume").onclick = () =>
    perform(async () => {
      await updateView(spath() + "/resume", {
        method: "POST",
        body: JSON.stringify({
          command_id: crypto.randomUUID(),
          expected_revision: view.revision,
        }),
      });
      $("archive-status").textContent =
        "已从当前记录位置创建可执行分支，现为暂停状态。可单步、运行或改变目标；原存档保持不变。";
    });
  $("replay-next").onclick = () =>
    perform(() =>
      seek(Math.min(view.replay!.total, view.replay!.verified + 1)),
    );
  $("cursor").onchange = () =>
    perform(() => seek(Number($<HTMLInputElement>("cursor").value)));
  $("model-propose").onclick = async () => {
    if (busy || view.model.status === "pending") return;
    responses.invalidate();
    const stamp = responses.capture(session.id);
    $("error").hidden = true;
    try {
      view.model.status = "pending";
      pollFailures = 0;
      render();
      schedulePoll();
      await updateView(spath() + "/model", {
        method: "POST",
        headers: {
          "X-Model-Access": $<HTMLInputElement>("model-access").value,
        },
        body: JSON.stringify({
          provider: $<HTMLSelectElement>("provider").value,
        }),
      });
    } catch (error) {
      if (responses.current(stamp, session.id)) notify(error);
    }
  };
  $("model-cancel").onclick = () =>
    perform(async () => {
      await api(spath() + "/model/cancel", { method: "POST" });
      await updateView(spath());
    });
  $("archive-save").onclick = () => perform(saveArchive);
  document.addEventListener("visibilitychange", () => {
    schedulePoll();
    if (!document.hidden) {
      // The runner may have finished while this page was in the background.
      pollFailures = 0;
      void refresh();
    }
  });
}
init().catch(notify);
