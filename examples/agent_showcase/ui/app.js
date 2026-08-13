// ByoAI Demo Agent Showcase — vanilla JS UI. No build step, no framework.
// Talks to the FastAPI backend in app.py: GET /api/agents, POST /api/agents/{id}/run,
// GET /api/runs/{id}/events (SSE), GET /api/runs/{id}, GET /api/runs/{id}/verify.

const state = {
  agents: [],
  runId: null,
  agentId: null,
  eventSource: null,
  wfNodesByTool: {}, // tool_name -> FIFO queue of pending <li> nodes, resolved in call order
  runGeneration: 0, // bumped on every startRun so a stale run's async completion is ignored
  runRendered: false, // guards against finishRun (SSE) and pollUntilDone (fallback) both rendering the same run
  domainFilter: "all", // "all" or a specific agent.domain value, set via the domain tabs
  runInFlight: false, // true from startRun until the run's outcome is rendered
  swarmOn: false, // toggled by clicking the swarm-status pill; drives the auto-run loop
};

const SWARM_STEP_INTERVAL_MS = 4000;
let swarmIntervalId = null;

async function loadAgents() {
  const res = await fetch("/api/agents");
  state.agents = await res.json();
  renderGallery();
  // Auto-start the ambient swarm loop so cards show live heartbeats right
  // away — no click needed. Runs stay unfocused (see startRun's focus
  // param), so this never pulls the user into a run-detail view on its own.
  toggleSwarm();
}

function toggleSwarm() {
  state.swarmOn = !state.swarmOn;
  const el = document.getElementById("swarm-status");
  el.setAttribute("aria-pressed", String(state.swarmOn));
  el.title = state.swarmOn ? "Click to stop the auto-run swarm" : "Click to start the auto-run swarm";

  if (state.swarmOn) {
    runSwarmStep();
    if (!swarmIntervalId) swarmIntervalId = setInterval(runSwarmStep, SWARM_STEP_INTERVAL_MS);
  } else {
    if (swarmIntervalId) {
      clearInterval(swarmIntervalId);
      swarmIntervalId = null;
    }
    if (!state.runInFlight) setSwarmStatus("idle");
  }
}

function runSwarmStep() {
  if (!state.swarmOn || state.runInFlight || state.agents.length === 0) return;
  const agent = state.agents[Math.floor(Math.random() * state.agents.length)];
  startRun(agent, { focus: false });
}

function setSwarmStatus(kind) {
  const el = document.getElementById("swarm-status");
  el.className = "swarm-status " + kind + (state.swarmOn ? " swarm-on" : "");
  const labels = {
    idle: state.swarmOn ? "swarm on · idle" : "swarm idle — click to start",
    running: "swarm running",
    error: "swarm misfire",
  };
  document.getElementById("swarm-label").textContent = labels[kind] ?? kind;
}

function updateModelBox(agent, kind, detail) {
  const box = document.getElementById("model-box");
  box.hidden = false;
  box.className = "model-box" + (kind === "done" ? " done" : kind === "error" ? " error" : "");
  document.getElementById("model-box-name").textContent = agent.name;
  document.getElementById("model-box-detail").textContent =
    detail ?? `${agent.provider} / ${agent.model}`;
}

function setActiveCard(agentId) {
  document.querySelectorAll(".agent-card").forEach((el) => {
    el.classList.toggle("active", el.dataset.agentId === agentId);
  });
}

function renderGallery() {
  renderDomainTabs();

  const container = document.getElementById("agent-groups");
  container.innerHTML = "";
  const byDomain = {};
  for (const agent of state.agents) {
    (byDomain[agent.domain] ??= []).push(agent);
  }
  for (const [domain, agents] of Object.entries(byDomain)) {
    if (state.domainFilter !== "all" && domain !== state.domainFilter) continue;

    const group = document.createElement("div");
    group.className = "domain-group";

    const label = document.createElement("div");
    label.className = "domain-label";
    label.textContent = domain;
    group.appendChild(label);

    for (const agent of agents) {
      group.appendChild(renderAgentCard(agent));
    }
    container.appendChild(group);
  }
}

function renderDomainTabs() {
  const tabs = document.getElementById("domain-tabs");
  tabs.innerHTML = "";
  const domains = [...new Set(state.agents.map((a) => a.domain))];

  const makeTab = (value, label) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "domain-tab" + (state.domainFilter === value ? " active" : "");
    btn.textContent = label;
    btn.setAttribute("role", "tab");
    btn.setAttribute("aria-selected", String(state.domainFilter === value));
    btn.addEventListener("click", () => {
      state.domainFilter = value;
      renderGallery();
    });
    tabs.appendChild(btn);
  };

  makeTab("all", "All");
  for (const domain of domains) makeTab(domain, domain);
}

function renderAgentCard(agent) {
  const card = document.createElement("div");
  card.className = "agent-card";
  card.dataset.agentId = agent.id;

  const name = document.createElement("div");
  name.className = "name";
  name.textContent = agent.name;
  card.appendChild(name);

  const desc = document.createElement("div");
  desc.className = "desc";
  desc.textContent = agent.description;
  card.appendChild(desc);

  const meta = document.createElement("div");
  meta.className = "meta";
  meta.appendChild(makeTag(agent.provider + " / " + agent.model));
  const modeTag = makeTag(agent.live ? "live" : "replay");
  modeTag.classList.add(agent.live ? "live" : "replay");
  meta.appendChild(modeTag);
  if (agent.sub_agents.length > 0) {
    const suffix = agent.sub_agents.length === 1 ? "sub-agent" : "sub-agents";
    meta.appendChild(makeTag(`${agent.sub_agents.length} ${suffix}`));
  }
  if (agent.id.includes("misfire")) {
    meta.appendChild(makeTag("misfire demo", true));
  }
  card.appendChild(meta);

  const runStatus = document.createElement("div");
  runStatus.className = "card-run-status";
  runStatus.hidden = true;
  const runDot = document.createElement("span");
  runDot.className = "card-run-dot";
  runStatus.appendChild(runDot);
  const runText = document.createElement("span");
  runText.className = "card-run-text";
  runStatus.appendChild(runText);
  card.appendChild(runStatus);

  card.addEventListener("click", () => startRun(agent));
  return card;
}

// Shows this specific card's last/current run outcome (phase + mode + time)
// right under its name/description/provider-model tags, so the heartbeat is
// unmistakably tied to which agent it belongs to. Other cards keep whatever
// status they last showed — it's a small run history, not just a spinner.
function setCardRunStatus(agentId, phase, label) {
  const card = document.querySelector(`.agent-card[data-agent-id="${agentId}"]`);
  if (!card) return;
  const status = card.querySelector(".card-run-status");
  if (!status) return;
  status.hidden = false;
  status.className = "card-run-status " + phase;
  status.querySelector(".card-run-text").textContent = label;
  // Tint the card's own border to match the phase the dot lands on.
  card.classList.toggle("ran-done", phase === "done");
  card.classList.toggle("ran-error", phase === "error");
}

function makeTag(text, isMisfire) {
  const tag = document.createElement("span");
  tag.className = "tag" + (isMisfire ? " misfire" : "");
  tag.textContent = text;
  return tag;
}

// focus=true (the default, used for manual card clicks) opens the run-detail
// panel for this run. focus=false (used by the background swarm loop) only
// updates the gallery-level indicators — card heartbeat, active-card border,
// swarm pill — and leaves whatever's currently on screen alone, so ambient
// auto-run traffic never yanks the user into a detail view they didn't ask
// to see.
async function startRun(agent, { focus = true } = {}) {
  // Only a focused (user-initiated) run moves the selection marker. An ambient
  // swarm run must leave it alone — it's the "you are here" tie between the
  // run-detail panel and the card it's describing, and clearing it on every
  // background run stranded the panel with no card pointing at it.
  if (focus) {
    document.querySelectorAll(".agent-card").forEach((el) => {
      el.classList.toggle("selected", el.dataset.agentId === agent.id);
    });
  }

  if (focus && state.eventSource) {
    state.eventSource.close();
    state.eventSource = null;
  }
  state.agentId = agent.id;
  if (focus) state.wfNodesByTool = {};
  state.runRendered = false;
  state.runInFlight = true;
  const generation = ++state.runGeneration;

  if (!focus) setSwarmStatus("running");
  setActiveCard(agent.id);
  setCardRunStatus(agent.id, "running", "running…");

  if (focus) {
    document.getElementById("run-empty").hidden = true;
    document.getElementById("run-active").hidden = false;
    document.getElementById("run-agent-name").textContent = agent.name;
    setStatus("running");
    updateModelBox(agent, "running");
    document.getElementById("timeline").innerHTML = "";
    document.getElementById("workflow-graph").innerHTML = "";
    document.getElementById("span-tree").innerHTML = "";
    document.getElementById("outcome-text").textContent = "—";
    document.getElementById("violations-panel").hidden = true;
    document.getElementById("violations-list").innerHTML = "";
    document.getElementById("verify-result").innerHTML = "";
    document.getElementById("verify-btn").disabled = true;
    document.getElementById("verify-btn").onclick = null;
    document.getElementById("tamper-btn").disabled = true;
    document.getElementById("tamper-btn").onclick = null;
  }

  let run_id;
  try {
    const res = await fetch(`/api/agents/${agent.id}/run`, { method: "POST" });
    if (!res.ok) throw new Error(`run start failed: HTTP ${res.status}`);
    ({ run_id } = await res.json());
  } catch (err) {
    if (generation !== state.runGeneration) return;
    if (focus) {
      const badge = document.getElementById("run-status");
      badge.className = "badge";
      badge.textContent = "failed to start";
      document.getElementById("outcome-text").textContent = `Could not start this run: ${err.message}`;
      updateModelBox(agent, "error", "failed to start");
    }
    if (!focus) setSwarmStatus("error");
    setActiveCard(null);
    setCardRunStatus(agent.id, "error", "failed to start");
    state.runInFlight = false;
    return;
  }
  if (generation !== state.runGeneration) return;
  state.runId = run_id;

  streamEvents(run_id, generation, focus);
  pollUntilDone(run_id, generation, focus);
}

function setStatus(kind) {
  const badge = document.getElementById("run-status");
  badge.className = "badge " + kind;
  badge.textContent = kind === "running" ? "running…" : "done";
}

function streamEvents(runId, generation, focus) {
  const es = new EventSource(`/api/runs/${runId}/events`);
  if (focus) state.eventSource = es;
  es.onmessage = (msg) => {
    if (generation !== state.runGeneration) {
      es.close();
      return;
    }
    const event = JSON.parse(msg.data);
    if (focus) {
      appendTimelineEvent(event);
      appendWorkflowNode(event);
    }
    if (event.kind === "run_complete") {
      finishRun(runId, generation, focus);
    }
  };
  es.onerror = () => {
    es.close();
    if (state.eventSource === es) {
      state.eventSource = null;
    }
  };
}

async function finishRun(runId, generation, focus) {
  const res = await fetch(`/api/runs/${runId}`);
  const summary = await res.json();
  if (generation !== state.runGeneration || state.runRendered) return;
  if (summary.done) {
    state.runRendered = true;
    renderRunSummary(summary, focus);
  }
}

function appendTimelineEvent(event) {
  const list = document.getElementById("timeline");
  const li = document.createElement("li");
  li.className = event.kind;
  if (event.data && event.data.policy_violation) {
    li.classList.add("flagged");
  }
  if (event.parent_span_id) {
    li.classList.add("depth-1");
  }

  const kindEl = document.createElement("div");
  kindEl.className = "kind";
  kindEl.textContent = event.kind.replace("_", " ");
  li.appendChild(kindEl);

  const bodyEl = document.createElement("div");
  bodyEl.className = "body";
  bodyEl.textContent = describeEvent(event);
  li.appendChild(bodyEl);

  list.appendChild(li);
  list.scrollTop = list.scrollHeight;
}

// Renders the run as a LangGraph-style node chain: one node per tool call,
// wired left-to-right with animated edges, so the audience sees a workflow
// pipeline rather than a flat log.
function appendWorkflowNode(event) {
  const graph = document.getElementById("workflow-graph");

  if (event.kind === "tool_use") {
    if (graph.children.length > 0) {
      const edge = document.createElement("li");
      edge.className = "wf-edge";
      graph.appendChild(edge);
    }
    const node = document.createElement("li");
    node.className = "wf-node active";
    const dot = document.createElement("span");
    dot.className = "dot";
    node.appendChild(dot);
    const label = document.createElement("span");
    label.className = "label";
    label.textContent = event.tool_name;
    node.appendChild(label);
    if (event.data && event.data.policy_violation) {
      node.dataset.policyViolation = "1";
    }
    graph.appendChild(node);
    const key = event.span_id + "|" + event.tool_name;
    (state.wfNodesByTool[key] ??= []).push(node);
    return;
  }

  if (event.kind === "tool_result") {
    const key = event.span_id + "|" + event.tool_name;
    const queue = state.wfNodesByTool[key];
    const node = queue && queue.shift();
    if (!node) return;
    const errored = event.data && event.data.result && event.data.result.error;
    const flagged = node.dataset.policyViolation === "1" || errored;
    node.classList.remove("active");
    node.classList.add(flagged ? "flagged" : "validated");
    // Don't let the node's outcome be carried by colour alone — add a glyph and
    // an accessible name so a flagged step is still identifiable without it.
    const mark = document.createElement("span");
    mark.className = "wf-mark";
    mark.setAttribute("aria-hidden", "true");
    mark.textContent = flagged ? "⚠" : "✓";
    node.appendChild(mark);
    node.setAttribute(
      "aria-label",
      `${event.tool_name} — ${flagged ? "flagged, off-scope or errored" : "validated"}`,
    );
  }
}

function describeEvent(event) {
  if (event.kind === "message" && event.text) return event.text;
  if (event.kind === "tool_use") {
    return `${event.tool_name}(${JSON.stringify(event.data?.input ?? {})})`;
  }
  if (event.kind === "tool_result") {
    return `${event.tool_name} → ${JSON.stringify(event.data?.result ?? {})}`;
  }
  if (event.kind === "session_start") return "session started";
  if (event.kind === "api_error") return event.data?.reason || "model API error — using fallback transcript";
  if (event.kind === "run_complete") return event.text ?? "";
  return JSON.stringify(event.data ?? {});
}

// Fallback safety net only: SSE's run_complete event (see finishRun) is the
// primary completion signal. This polls far more slowly and exists purely to
// catch the case where the SSE stream drops the message or errors out.
async function pollUntilDone(runId, generation, focus) {
  for (let i = 0; i < 60; i++) {
    if (generation !== state.runGeneration) return;
    const res = await fetch(`/api/runs/${runId}`);
    const summary = await res.json();
    if (generation !== state.runGeneration || state.runRendered) return;
    if (summary.done) {
      state.runRendered = true;
      renderRunSummary(summary, focus);
      return;
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  if (generation !== state.runGeneration) return;
  if (state.eventSource) {
    state.eventSource.close();
    state.eventSource = null;
  }
  if (focus) {
    const badge = document.getElementById("run-status");
    badge.className = "badge";
    badge.textContent = "stalled";
    document.getElementById("outcome-text").textContent =
      "Timed out waiting for the run to finish — the agent may have stalled.";
  }
  if (!focus) setSwarmStatus("error");
  setActiveCard(null);
  if (state.agentId) setCardRunStatus(state.agentId, "error", "stalled");
  state.runInFlight = false;
  const agent = state.agents.find((a) => a.id === state.agentId);
  if (focus && agent) updateModelBox(agent, "error", "stalled");
}

function renderRunSummary(summary, focus) {
  if (state.eventSource) {
    state.eventSource.close();
    state.eventSource = null;
  }

  const runComplete = summary.events.find((e) => e.kind === "run_complete");
  const mode = runComplete?.data?.mode;

  if (focus) {
    setStatus("done");
    renderSpanTree(summary.spans);
    document.getElementById("outcome-text").textContent = runComplete?.text ?? "(no final text)";
    const modeEl = document.getElementById("outcome-mode");
    modeEl.className = "tag" + (mode ? " " + mode : "");
    modeEl.textContent = mode ?? "";
  }

  const errored = mode === "misfire" || Boolean(summary.flagged);
  if (!focus) setSwarmStatus(errored ? "error" : "idle");
  setActiveCard(null);
  if (state.agentId) {
    const stamp = new Date().toLocaleTimeString();
    setCardRunStatus(state.agentId, errored ? "error" : "done", `${mode ?? "done"} · ${stamp}`);
  }
  state.runInFlight = false;
  const agent = state.agents.find((a) => a.id === state.agentId);
  if (focus && agent) updateModelBox(agent, errored ? "error" : "done", mode);
  if (!focus) return;

  if (summary.flagged) {
    const panel = document.getElementById("violations-panel");
    panel.hidden = false;
    const list = document.getElementById("violations-list");
    list.innerHTML = "";
    for (const v of summary.policy_violations) {
      const li = document.createElement("li");
      const tool = document.createElement("span");
      tool.className = "tool";
      tool.textContent = v.tool_name;
      li.appendChild(tool);
      const reason = document.createElement("div");
      reason.textContent = v.reason;
      li.appendChild(reason);
      list.appendChild(li);
    }
  }

  const verifyBtn = document.getElementById("verify-btn");
  verifyBtn.disabled = false;
  verifyBtn.onclick = () => runVerify(summary.run_id);

  const tamperBtn = document.getElementById("tamper-btn");
  tamperBtn.disabled = false;
  tamperBtn.onclick = () => runTamperDemo(summary.run_id);
}

async function runTamperDemo(runId) {
  const resultEl = document.getElementById("verify-result");
  const res = await fetch(`/api/demo/tamper/${runId}`, { method: "POST" });
  if (res.status === 403) {
    resultEl.textContent = "Tamper demo disabled — restart the server with DEMO_TAMPER=1 to enable.";
    return;
  }
  const body = await res.json();
  resultEl.textContent = `Tampered seq ${body.tampered_seq} in place. Re-verifying…`;
  await runVerify(runId);
}

function renderSpanTree(spans) {
  const tree = document.getElementById("span-tree");
  tree.innerHTML = "";
  const byParent = {};
  for (const s of spans) {
    (byParent[s.parent_span_id ?? ""] ??= []).push(s);
  }

  function walk(parentKey, depth) {
    for (const span of byParent[parentKey] ?? []) {
      const label = depth === 0 ? "root span " : "↳ ".repeat(depth) + "sub-agent span ";
      const li = spanTreeItem(label, span.span_id, depth > 0 ? "child" : "");
      if (depth > 0) li.style.marginLeft = `${depth}rem`;
      tree.appendChild(li);
      walk(span.span_id, depth + 1);
    }
  }
  walk("", 0);
}

function spanTreeItem(label, spanId, className) {
  const li = document.createElement("li");
  if (className) li.className = className;
  li.appendChild(document.createTextNode(label));
  const idEl = document.createElement("span");
  idEl.className = "span-id";
  idEl.textContent = spanId;
  li.appendChild(idEl);
  return li;
}

async function runVerify(runId) {
  const resultEl = document.getElementById("verify-result");
  resultEl.textContent = "verifying…";
  const res = await fetch(`/api/runs/${runId}/verify`);
  const verify = await res.json();
  resultEl.innerHTML = "";

  const rows = [
    ["Chain intact", verify.chain_ok],
    ["Digests ok", verify.digests_ok],
  ];
  for (const [label, ok] of rows) {
    const row = document.createElement("div");
    row.className = "verify-line";
    const labelEl = document.createElement("span");
    labelEl.textContent = label;
    const valueEl = document.createElement("span");
    valueEl.className = ok === null ? "verify-na" : ok ? "verify-ok" : "verify-bad";
    valueEl.textContent = ok === null ? "n/a" : ok ? "✓ verified" : "✗ FAILED";
    row.append(labelEl, valueEl);
    resultEl.appendChild(row);
  }

  if (verify.tampered_events && verify.tampered_events.length > 0) {
    const warn = document.createElement("div");
    warn.className = "verify-bad";
    warn.textContent = `${verify.tampered_events.length} broken link(s) detected`;
    resultEl.appendChild(warn);
  }
}

document.getElementById("swarm-status").addEventListener("click", toggleSwarm);
document.getElementById("swarm-status").addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    toggleSwarm();
  }
});

loadAgents();
