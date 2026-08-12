// ByoAI Demo Agent Showcase — vanilla JS UI. No build step, no framework.
// Talks to the FastAPI backend in app.py: GET /api/agents, POST /api/agents/{id}/run,
// GET /api/runs/{id}/events (SSE), GET /api/runs/{id}, GET /api/runs/{id}/verify.

const state = {
  agents: [],
  runId: null,
  agentId: null,
  eventSource: null,
  spanTextByKey: {}, // "trace_id|span_id" -> depth (0 = root)
};

async function loadAgents() {
  const res = await fetch("/api/agents");
  state.agents = await res.json();
  renderGallery();
}

function renderGallery() {
  const container = document.getElementById("agent-groups");
  container.innerHTML = "";
  const byDomain = {};
  for (const agent of state.agents) {
    (byDomain[agent.domain] ??= []).push(agent);
  }
  for (const [domain, agents] of Object.entries(byDomain)) {
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
  if (agent.sub_agents.length > 0) {
    meta.appendChild(makeTag(`${agent.sub_agents.length} sub-agent`));
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
  state.spanTextByKey = {};

  document.getElementById("run-empty").hidden = true;
  document.getElementById("run-active").hidden = false;
  document.getElementById("run-agent-name").textContent = agent.name;
  setStatus("running");
  document.getElementById("timeline").innerHTML = "";
  document.getElementById("span-tree").innerHTML = "";
  document.getElementById("outcome-text").textContent = "—";
  document.getElementById("violations-panel").hidden = true;
  document.getElementById("violations-list").innerHTML = "";
  document.getElementById("verify-result").innerHTML = "";
  document.getElementById("verify-btn").disabled = true;

  const res = await fetch(`/api/agents/${agent.id}/run`, { method: "POST" });
  const { run_id } = await res.json();
  state.runId = run_id;

  streamEvents(run_id);
  pollUntilDone(run_id);
}

function setStatus(kind) {
  const badge = document.getElementById("run-status");
  badge.className = "badge " + kind;
  badge.textContent = kind === "running" ? "running…" : "done";
}

function streamEvents(runId) {
  const es = new EventSource(`/api/runs/${runId}/events`);
  state.eventSource = es;
  es.onmessage = (msg) => {
    const event = JSON.parse(msg.data);
    appendTimelineEvent(event);
  };
  es.onerror = () => {
    es.close();
  };
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

function describeEvent(event) {
  if (event.kind === "message" && event.text) return event.text;
  if (event.kind === "tool_use") {
    return `${event.tool_name}(${JSON.stringify(event.data?.input ?? {})})`;
  }
  if (event.kind === "tool_result") {
    return `${event.tool_name} → ${JSON.stringify(event.data?.result ?? {})}`;
  }
  if (event.kind === "session_start") return "session started";
  if (event.kind === "api_error") return event.data?.reason ?? "model API error — using fallback transcript";
  if (event.kind === "run_complete") return event.text ?? "";
  return JSON.stringify(event.data ?? {});
}

async function pollUntilDone(runId) {
  for (let i = 0; i < 300; i++) {
    const res = await fetch(`/api/runs/${runId}`);
    const summary = await res.json();
    if (summary.done) {
      renderRunSummary(summary);
      return;
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  setStatus("running");
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
  const roots = spans.filter((s) => !s.parent_span_id);
  const children = spans.filter((s) => s.parent_span_id);

  for (const root of roots) {
    tree.appendChild(spanTreeItem("root span ", root.span_id, ""));
    for (const child of children.filter((c) => c.parent_span_id === root.span_id)) {
      tree.appendChild(spanTreeItem("↳ sub-agent span ", child.span_id, "child"));
    }
  }
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
