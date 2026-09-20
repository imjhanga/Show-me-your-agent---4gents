"use strict";

let state = null;
let selected = null;
let activeTab = "worklist";

const $ = (id) => document.getElementById(id);
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );

function toast(message, isError) {
  const el = $("toast");
  el.textContent = message;
  el.className = "toast show" + (isError ? " error" : "");
  setTimeout(() => (el.className = "toast"), 3200);
}

async function api(path, body) {
  const response = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const payload = await response.json();
  if (!response.ok) {
    toast(payload.error || "Something went wrong", true);
    return null;
  }
  return payload;
}

let lastPayload = "";

async function refresh(payload) {
  const next = payload || (await api("/api/state"));
  if (!next) return;
  // Re-render only when something actually changed. Redrawing the list on every
  // poll would move the ground under the operator mid-click.
  const serialised = JSON.stringify(next);
  if (serialised === lastPayload) return;
  lastPayload = serialised;
  state = next;
  render();
}

/* ---------- rendering ---------- */

function renderStats() {
  const t = state.totals;
  $("clinic-now").textContent = "Clinic time " + state.clinic_now.slice(0, 16).replace("T", " ");
  $("mode-pill").textContent = !state.use_llm
    ? "Templates and rules only"
    : state.offline
    ? "Offline — cached model replies"
    : "Live model";
  $("kill-switch").className = "ghost" + (state.kill_switch ? " engaged" : "");
  $("kill-switch").textContent = state.kill_switch ? "Kill switch ON" : "Kill switch";

  $("stats").innerHTML = [
    ["", t.patients_scanned.toLocaleString(), "patients scanned"],
    ["", t.requirements_scanned.toLocaleString(), "follow-ups"],
    ["", t.overdue.toLocaleString(), "overdue"],
    ["good", t.auto_send.toLocaleString(), "agent sends"],
    ["warn", t.staff_approval.toLocaleString(), "need a human"],
    ["stop", t.excluded.toLocaleString(), "will not contact"],
  ]
    .map(([cls, value, label]) => `<div class="stat ${cls}"><b>${value}</b><span>${label}</span></div>`)
    .join("");

  const approvals = state.approvals.length;
  const escalations = state.escalations.length;
  $("badge-approvals").textContent = approvals;
  $("badge-approvals").className = "badge" + (approvals ? " hot" : "");
  $("badge-escalations").textContent = escalations;
  $("badge-escalations").className = "badge" + (escalations ? " hot" : "");
}

function outcomeTag(outcome) {
  if (outcome === "AUTO_SEND") return '<span class="tag auto">agent sends</span>';
  if (outcome === "STAFF_APPROVAL") return '<span class="tag approval">needs a human</span>';
  return '<span class="tag exclude">will not contact</span>';
}

function conversationActions(row) {
  const c = row.conversation;
  const id = esc(row.case_id);
  if (!c || !c.draft) {
    return `<div class="actions"><button class="primary" data-act="prepare" data-id="${id}">Draft message</button></div>`;
  }
  if (c.state === "PENDING_APPROVAL") {
    return `<div class="draft-preview">${esc(c.draft.text)}</div>
      <div class="actions">
        <button class="primary" data-act="approve" data-id="${id}">Approve &amp; send</button>
        <button class="danger" data-act="reject" data-id="${id}">Decline</button>
      </div>`;
  }
  return `<div class="draft-preview">${esc(c.draft.text)}</div>`;
}

function renderWorklist() {
  $("worklist").innerHTML = state.worklist
    .map((row) => {
      const c = row.conversation;
      const stateTag = c && c.state !== "MONITORING" ? `<span class="tag state">${esc(c.state)}</span>` : "";
      return `<div class="row ${row.risk_outcome === "AUTO_SEND" ? "auto" : "approval"} ${
        selected === row.case_id ? "selected" : ""
      }" data-select="${esc(row.case_id)}">
        <div class="row-head">
          <span class="rank">#${row.rank}</span>
          <span class="who">${esc(row.patient_name)}</span>
          <span class="what">${esc(row.requirement_label)} · ${row.days_overdue}d overdue</span>
          <span class="spacer"></span>${stateTag}${outcomeTag(row.risk_outcome)}
        </div>
        <div class="why">${esc(row.why)}</div>
        <div class="reason">${esc(row.risk_reason)}</div>
        ${selected === row.case_id ? conversationActions(row) : ""}
      </div>`;
    })
    .join("");
}

function renderConversationList(target, items, emptyText) {
  if (!items.length) {
    $(target).innerHTML = `<p class="hint">${emptyText}</p>`;
    return;
  }
  $(target).innerHTML = items
    .map((c) => {
      const isApproval = c.state === "PENDING_APPROVAL";
      return `<div class="row ${isApproval ? "approval" : ""} ${
        selected === c.case_id ? "selected" : ""
      }" data-select="${esc(c.case_id)}">
        <div class="row-head">
          <span class="who">${esc(c.patient_name)}</span>
          <span class="what">${esc(c.requirement_label)}</span>
          <span class="spacer"></span><span class="tag state">${esc(c.state)}</span>
        </div>
        <div class="reason">${esc(c.escalation_reason || c.risk_reason)}</div>
        ${c.draft ? `<div class="draft-preview">${esc(c.draft.text)}</div>` : ""}
        ${
          isApproval
            ? `<div class="actions">
                 <button class="primary" data-act="approve" data-id="${esc(c.case_id)}">Approve &amp; send</button>
                 <button class="danger" data-act="reject" data-id="${esc(c.case_id)}">Decline</button>
               </div>`
            : ""
        }
      </div>`;
    })
    .join("");
}

function renderRefusals() {
  $("refusal-summary").innerHTML =
    '<div class="summary-grid">' +
    state.refusal_summary
      .map(
        (r) =>
          `<div class="summary-row"><b>${r.count}</b><span>${esc(r.reason)}</span></div>`
      )
      .join("") +
    "</div>";

  $("refusals").innerHTML = state.refusals
    .map(
      (r) => `<div class="row refused">
        <div class="row-head">
          <span class="who">${esc(r.patient_name)}</span>
          <span class="what">${esc(r.requirement_kind.toLowerCase())}</span>
          <span class="spacer"></span><span class="tag exclude">${esc(r.rule_id)}</span>
        </div>
        <div class="reason">${esc(r.reason)}</div>
        <div class="codes">${r.eligibility_reason_codes
          .map((code) => `<span class="code">${esc(code)}</span>`)
          .join("")}</div>
      </div>`
    )
    .join("");
}

function renderAudit() {
  $("audit").innerHTML = state.audit
    .map((event) => {
      const detail = Object.entries(event)
        .filter(([k]) => !["recorded_at", "event_type"].includes(k))
        .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`)
        .join("  ");
      return `<div class="audit-row">
        <time>${esc(event.recorded_at.slice(11, 19))}</time>
        <code>${esc(event.event_type)}</code>
        <span class="detail">${esc(detail)}</span>
      </div>`;
    })
    .join("");
}

function findConversation(caseId) {
  if (!caseId) return null;
  const row = state.worklist.find((r) => r.case_id === caseId);
  if (row && row.conversation) return row.conversation;
  return (
    state.approvals.concat(state.escalations, state.active).find((c) => c.case_id === caseId) || null
  );
}

function renderChat() {
  const c = findConversation(selected);
  const input = $("reply-input");
  const send = $("reply-send");

  if (!c) {
    $("chat-name").textContent = "No conversation selected";
    $("chat-sub").textContent = "Pick a patient from the worklist";
    $("chat-avatar").textContent = "–";
    $("chat-body").innerHTML = '<p class="chat-empty">Select a patient on the left to see their conversation.</p>';
    $("agent-readout").innerHTML = "";
    input.disabled = send.disabled = true;
    return;
  }

  $("chat-avatar").textContent = (c.patient_name || "?").trim()[0] || "?";
  $("chat-name").textContent = c.patient_name;
  $("chat-sub").textContent = (c.phone || "no number on file") + " · " + c.requirement_label;

  $("chat-body").innerHTML = c.messages.length
    ? c.messages
        .map(
          (m) => `<div class="bubble ${m.direction === "outbound" ? "out" : "in"}">${esc(m.text)}
            <time>${esc(m.at.slice(11, 16))}</time></div>`
        )
        .join("")
    : '<p class="chat-empty">Nothing sent yet.</p>';
  $("chat-body").scrollTop = $("chat-body").scrollHeight;

  const parts = [];
  if (c.understanding) {
    const u = c.understanding;
    parts.push(`<div class="readout-item ${u.escalate ? "escalated" : ""}">
      <div class="readout-label">Agent read the reply as</div>
      <b>${esc(u.intent)}</b> · ${Math.round(u.confidence * 100)}% confident · via ${esc(u.source)}
      ${u.defer_until ? `<br>Retry scheduled for <b>${esc(u.defer_until)}</b>` : ""}
      ${u.escalation_reason ? `<br>${esc(u.escalation_reason)}` : ""}
    </div>`);
  }
  parts.push(`<div class="readout-item">
    <div class="readout-label">Workflow state</div>
    <b>${esc(c.state)}</b>${c.defer_until ? ` until ${esc(c.defer_until)}` : ""}
  </div>`);
  $("agent-readout").innerHTML = parts.join("");

  const canReply = c.messages.some((m) => m.direction === "outbound");
  input.disabled = send.disabled = !canReply;
  input.placeholder = canReply ? "Type as the patient…" : "Send the reminder first";
}

function render() {
  renderStats();
  renderWorklist();
  renderConversationList("approvals", state.approvals, "Nothing is waiting for approval.");
  renderConversationList(
    "escalations",
    state.escalations,
    "Nothing has been escalated."
  );
  renderRefusals();
  renderAudit();
  renderChat();
}

/* ---------- events ---------- */

document.addEventListener("click", async (event) => {
  const tab = event.target.closest(".tab");
  if (tab) {
    activeTab = tab.dataset.tab;
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    document
      .querySelectorAll(".panel")
      .forEach((p) => p.classList.toggle("active", p.id === "panel-" + activeTab));
    return;
  }

  const action = event.target.closest("[data-act]");
  if (action) {
    event.stopPropagation();
    const { act, id } = action.dataset;
    selected = id;
    const endpoint = { prepare: "/api/prepare", approve: "/api/approve", reject: "/api/reject" }[act];
    const payload = await api(endpoint, { case_id: id });
    if (payload) {
      await refresh(payload);
      if (act === "approve") toast("Sent. Now reply as the patient on the right.");
      if (act === "prepare") toast("Drafted and checked before sending.");
    }
    return;
  }

  const row = event.target.closest("[data-select]");
  if (row) {
    selected = row.dataset.select;
    render();
  }
});

$("composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("reply-input");
  const text = input.value.trim();
  if (!text || !selected) return;
  input.value = "";
  const payload = await api("/api/reply", { case_id: selected, text });
  if (payload) await refresh(payload);
});

$("kill-switch").addEventListener("click", async () => {
  const payload = await api("/api/kill-switch", { engaged: !state.kill_switch });
  if (payload) {
    await refresh(payload);
    toast(state.kill_switch ? "Patient-facing actions disabled." : "Kill switch released.");
  }
});

$("reset").addEventListener("click", async () => {
  const payload = await api("/api/reset", {});
  if (payload) {
    selected = null;
    await refresh(payload);
    toast("Demo reset.");
  }
});

refresh();
setInterval(() => refresh(), 4000);
