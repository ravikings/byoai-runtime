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
};

async function loadAgents() {
  const res = await fetch("/api/agents");
  state.agents = await res.json();
  renderGallery();
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

  card.addEventListener("click", () => startRun(agent));
  return card;
}

function makeTag(text, isMisfire) {
  const tag = document.createElement("span");
  tag.className = "tag" + (isMisfire ? " misfire" : "");
  tag.textContent = text;
  return tag;
}

async function startRun(agent) {
  document.querySelectorAll(".agent-card").forEach((el) => {
    el.classList.toggle("selected", el.dataset.agentId === agent.id);
  });

  if (state.eventSource) {
    state.eventSource.close();
    state.eventSource = null;
  }
  state.agentId = agent.id;
  state.wfNodesByTool = {};
  state.runRendered = false;
  const generation = ++state.runGeneration;

  document.getElementById("run-empty").hidden = true;
  document.getElementById("run-active").hidden = false;
  document.getElementById("run-agent-name").textContent = agent.name;
  setStatus("running");
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

  let run_id;
  try {
    const res = await fetch(`/api/agents/${agent.id}/run`, { method: "POST" });
    if (!res.ok) throw new Error(`run start failed: HTTP ${res.status}`);
    ({ run_id } = await res.json());
  } catch (err) {
    if (generation !== state.runGeneration) return;
    const badge = document.getElementById("run-status");
    badge.className = "badge";
    badge.textContent = "failed to start";
    document.getElementById("outcome-text").textContent = `Could not start this run: ${err.message}`;
    return;
  }
  if (generation !== state.runGeneration) return;
  state.runId = run_id;

  streamEvents(run_id, generation);
  pollUntilDone(run_id, generation);
}

function setStatus(kind) {
  const badge = document.getElementById("run-status");
  badge.className = "badge " + kind;
  badge.textContent = kind === "running" ? "running…" : "done";
}

function streamEvents(runId, generation) {
  const es = new EventSource(`/api/runs/${runId}/events`);
  state.eventSource = es;
  es.onmessage = (msg) => {
    if (generation !== state.runGeneration) {
      es.close();
      return;
    }
    const event = JSON.parse(msg.data);
    appendTimelineEvent(event);
    appendWorkflowNode(event);
    if (event.kind === "run_complete") {
      finishRun(runId, generation);
    }
  };
  es.onerror = () => {
    es.close();
    if (state.eventSource === es) {
      state.eventSource = null;
    }
  };
}

async function finishRun(runId, generation) {
  const res = await fetch(`/api/runs/${runId}`);
  const summary = await res.json();
  if (generation !== state.runGeneration || state.runRendered) return;
  if (summary.done) {
    state.runRendered = true;
    renderRunSummary(summary);
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
    node.className = "wf-node";
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
    node.classList.add(flagged ? "flagged" : "validated");
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
async function pollUntilDone(runId, generation) {
  for (let i = 0; i < 60; i++) {
    if (generation !== state.runGeneration) return;
    const res = await fetch(`/api/runs/${runId}`);
    const summary = await res.json();
    if (generation !== state.runGeneration || state.runRendered) return;
    if (summary.done) {
      state.runRendered = true;
      renderRunSummary(summary);
      return;
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  if (generation !== state.runGeneration) return;
  if (state.eventSource) {
    state.eventSource.close();
    state.eventSource = null;
  }
  const badge = document.getElementById("run-status");
  badge.className = "badge";
  badge.textContent = "stalled";
  document.getElementById("outcome-text").textContent =
    "Timed out waiting for the run to finish — the agent may have stalled.";
}

function renderRunSummary(summary) {
  setStatus("done");
  if (state.eventSource) {
    state.eventSource.close();
    state.eventSource = null;
  }

  renderSpanTree(summary.spans);

  const runComplete = summary.events.find((e) => e.kind === "run_complete");
  document.getElementById("outcome-text").textContent = runComplete?.text ?? "(no final text)";

  const mode = runComplete?.data?.mode;
  const modeEl = document.getElementById("outcome-mode");
  modeEl.className = "tag" + (mode ? " " + mode : "");
  modeEl.textContent = mode ?? "";

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

loadAgents();
