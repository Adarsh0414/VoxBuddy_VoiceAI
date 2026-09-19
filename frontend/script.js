// VoxBuddy — CIE Live Cockpit
// Every visual here is driven directly by fields the backend actually sends
// (see backend/app.py UtteranceOut / backend/cie/engine.py CIEDecision) —
// nothing on this page is a canned illustration of what the engine "would"
// do; it's a live readout of what it just did.

// crypto.randomUUID() only exists in "secure contexts" (HTTPS or
// localhost). This page is often reached over plain HTTP (e.g. the
// EB *.elasticbeanstalk.com domain before a cert is attached), so fall
// back to a manual UUID v4 built on crypto.getRandomValues (which IS
// available in insecure contexts), or Math.random as a last resort.
function genUUID() {
  if (window.crypto && typeof window.crypto.randomUUID === "function") {
    return window.crypto.randomUUID();
  }
  const bytes = new Uint8Array(16);
  if (window.crypto && typeof window.crypto.getRandomValues === "function") {
    window.crypto.getRandomValues(bytes);
  } else {
    for (let i = 0; i < 16; i++) bytes[i] = Math.floor(Math.random() * 256);
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80; // variant 10
  const hex = [...bytes].map((b) => b.toString(16).padStart(2, "0"));
  return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10, 16).join("")}`;
}

const logEl = document.getElementById("log");
const rosterEl = document.getElementById("roster");
const rosterCountEl = document.getElementById("rosterCount");
const fusionBody = document.getElementById("fusionBody");
const fusionSpeakerEl = document.getElementById("fusionSpeaker");
const noisePillEl = document.getElementById("noisePill");

const ttSlider = document.getElementById("turnTaking");
const cohSlider = document.getElementById("coherence");
const ttVal = document.getElementById("ttVal");
const cohVal = document.getElementById("cohVal");

ttSlider.addEventListener("input", () => (ttVal.textContent = ttSlider.value));
cohSlider.addEventListener("input", () => (cohVal.textContent = cohSlider.value));

let sessionId = null;
let ws = null;

// FIFO queue correlating each outgoing utterance with its response, since
// this demo's WebSocket is simple ordered request/response.
const pendingMeta = [];

// Client-side speaker roster, built entirely from real per-turn fields
// (speaker_id, role, confidence, notes, primary_partner_id,
// active_partner_ids) — the backend doesn't need to push a full roster
// snapshot every turn since every turn already names the one speaker that
// changed, and that's all this needs to stay in sync.
const roster = new Map(); // speaker_id -> { role, confidence, note, turns }

function logEvent(text) {
  const div = document.createElement("div");
  div.className = "entry system";
  div.textContent = text;
  logEl.appendChild(div);
  logEl.scrollTop = logEl.scrollHeight;
}

function fmt(n, digits = 2) {
  return typeof n === "number" ? n.toFixed(digits) : "—";
}

function updateRoster(data) {
  const existing = roster.get(data.speaker_id) || { turns: 0 };
  roster.set(data.speaker_id, {
    role: data.role,
    confidence: data.confidence,
    note: data.notes,
    turns: existing.turns + 1,
  });
  renderRoster(data);
}

function renderRoster(data) {
  rosterCountEl.textContent = String(roster.size);
  if (roster.size === 0) {
    rosterEl.innerHTML = '<div class="roster-empty">No speakers yet — send an utterance to enroll one.</div>';
    return;
  }

  const activeIds = new Set(data.active_partner_ids || []);
  const primaryId = data.primary_partner_id;

  // Active partners first, then everyone else, most-recently-updated last
  // speaker pinned to the front within its group so the thing that just
  // happened is easy to find.
  const ids = [...roster.keys()].sort((a, b) => {
    const aActive = activeIds.has(a), bActive = activeIds.has(b);
    if (aActive !== bActive) return aActive ? -1 : 1;
    if (a === data.speaker_id) return -1;
    if (b === data.speaker_id) return 1;
    return 0;
  });

  rosterEl.innerHTML = "";
  for (const id of ids) {
    const s = roster.get(id);
    const card = document.createElement("div");
    card.className = `speaker-card role-${s.role}`;

    const ring = document.createElement("div");
    ring.className = "speaker-ring";
    ring.style.border = `2px solid var(--role-${s.role})`;
    ring.style.color = `var(--role-${s.role})`;
    ring.textContent = Math.round((s.confidence ?? 0) * 100);

    const meta = document.createElement("div");
    const idLine = document.createElement("div");
    idLine.className = "speaker-id";
    if (id === primaryId) {
      const crown = document.createElement("span");
      crown.className = "speaker-crown";
      crown.textContent = "★";
      crown.title = "primary partner";
      idLine.appendChild(crown);
    }
    idLine.appendChild(document.createTextNode(id));
    const metaLine = document.createElement("div");
    metaLine.className = "speaker-meta";
    metaLine.textContent = `${s.turns} turn${s.turns === 1 ? "" : "s"} · ${s.note}`;
    meta.appendChild(idLine);
    meta.appendChild(metaLine);

    const badge = document.createElement("div");
    badge.className = `speaker-role-badge role-${s.role}`;
    badge.textContent = s.role;

    card.appendChild(ring);
    card.appendChild(meta);
    card.appendChild(badge);
    rosterEl.appendChild(card);
  }
}

function meterRow(name, cssClass, raw, weight) {
  const contribution = raw != null && weight != null ? raw * weight : null;
  const pct = raw != null ? Math.round(raw * 100) : 0;
  return `
    <div class="meter-row">
      <div class="meter-name">${name}</div>
      <div class="meter-track"><div class="meter-fill ${cssClass}" style="width:${pct}%"></div></div>
      <div class="meter-readout">
        <span class="val">${fmt(raw)}</span> × ${fmt(weight)} =
        <span class="val">${fmt(contribution)}</span>
      </div>
    </div>`;
}

function renderFusion(data) {
  fusionSpeakerEl.textContent = `${data.speaker_id} — ${data.role}`;

  if (data.role === "self") {
    fusionBody.innerHTML = `<div class="fusion-empty">
      ${data.speaker_id} matched the enrolled self-voice — excluded from partner
      fusion entirely, so there's nothing to plot here by design.
    </div>`;
    return;
  }

  const bystanderPct = Math.round((data.bystander_threshold ?? 0.35) * 100);
  const lockPct = Math.round((data.lock_threshold ?? 0.62) * 100);
  const confPct = Math.min(100, Math.round((data.confidence ?? 0) * 100));

  fusionBody.innerHTML = `
    ${meterRow("VOICE SIM", "voice", data.voice_similarity, data.weight_voice)}
    ${meterRow("TURN-TAKING", "turn", data.turn_taking_score, data.weight_turn)}
    ${meterRow("SEMANTIC COH", "semantic", data.semantic_coherence_score, data.weight_semantic)}
    <div class="fusion-divider"></div>
    <div class="gauge-row">
      <div class="meter-name">FUSED CONF.</div>
      <div class="gauge-track" style="--bystander-pct:${bystanderPct}%; --lock-pct:${lockPct}%;">
        <div class="gauge-tick bystander" style="left:${bystanderPct}%">
          <span class="gauge-tick-label">bystander ${fmt(data.bystander_threshold)}</span>
        </div>
        <div class="gauge-tick lock" style="left:${lockPct}%">
          <span class="gauge-tick-label">lock ${fmt(data.lock_threshold)}</span>
        </div>
        <div class="gauge-needle role-${data.role}" style="left:${confPct}%"></div>
      </div>
      <div class="gauge-readout" style="color:var(--role-${data.role})">${fmt(data.confidence)}</div>
    </div>
    <div class="fusion-footer">
      <div class="decision-note">
        ${data.partner_switched ? '<span class="switch-flag">SWITCH</span>' : ""}
        ${data.partner_joined ? '<span class="switch-flag" style="color:var(--role-partner)">JOINED</span>' : ""}
        ${data.notes}
      </div>
    </div>`;
}

function renderTurn(data, meta) {
  const div = document.createElement("div");
  div.className = `entry ${data.role}`;

  const row1 = document.createElement("div");
  row1.className = "row1";
  row1.innerHTML =
    `<span>${meta.speakerLabel ?? ""}</span>` +
    `<span class="role-badge">${data.role}</span>` +
    `<span>conf ${fmt(data.confidence)}</span>` +
    `<span>${fmt(data.latency_ms, 1)} ms</span>` +
    (data.partner_switched ? `<span class="switch-badge">PARTNER SWITCH</span>` : "") +
    (data.partner_joined ? `<span class="switch-badge" style="color:var(--role-partner)">JOINED GROUP</span>` : "");

  const text = document.createElement("div");
  text.className = "text";
  text.textContent = meta.utteranceText ? `"${meta.utteranceText}"` : "";

  div.appendChild(row1);
  div.appendChild(text);

  const translated = document.createElement("div");
  translated.className = "translated";
  if (data.translated_text) {
    translated.textContent = `→ ${data.translated_text}`;
  } else {
    translated.style.color = "var(--dim)";
    translated.textContent = `(filtered by CIE — ${data.notes})`;
  }
  div.appendChild(translated);

  logEl.appendChild(div);
  logEl.scrollTop = logEl.scrollHeight;
}

function renderNoisePill(data) {
  if (!data.noise_profile) return;
  noisePillEl.className = `noise-pill ${data.noise_profile}`;
  noisePillEl.textContent = `env: ${data.noise_profile}`;
}

function connect() {
  sessionId = genUUID();
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${protocol}://${location.host}/ws/${sessionId}`);

  ws.onopen = () => logEvent(`Session started (${sessionId.slice(0, 8)}...)`);
  ws.onclose = () => logEvent("Session closed.");

  ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    const meta = pendingMeta.shift() || {};
    renderTurn(data, meta);
    updateRoster(data);
    renderFusion(data);
    renderNoisePill(data);
  };
}

function sendUtterance(speakerLabel, text, targetLang, turnTaking, coherence, noiseClass) {
  pendingMeta.push({ speakerLabel, utteranceText: text });
  ws.send(
    JSON.stringify({
      speaker_label: speakerLabel,
      text,
      target_lang: targetLang,
      turn_taking_score: turnTaking,
      semantic_coherence_score: coherence,
      noise_class: noiseClass,
    })
  );
}

document.getElementById("sendBtn").addEventListener("click", () => {
  const speakerLabel = document.getElementById("speakerLabel").value.trim() || "speaker";
  const text = document.getElementById("utteranceText").value.trim();
  if (!text) return;
  const targetLang = document.getElementById("targetLang").value;
  const noiseClass = document.getElementById("noiseClass").value;
  sendUtterance(speakerLabel, text, targetLang, parseFloat(ttSlider.value), parseFloat(cohSlider.value), noiseClass);
});

document.getElementById("resetBtn").addEventListener("click", () => {
  logEl.innerHTML = "";
  roster.clear();
  rosterCountEl.textContent = "0";
  rosterEl.innerHTML = '<div class="roster-empty">No speakers yet — send an utterance to enroll one.</div>';
  fusionBody.innerHTML = '<div class="fusion-empty">Waiting for the first utterance...</div>';
  fusionSpeakerEl.textContent = "";
  noisePillEl.className = "noise-pill clean";
  noisePillEl.textContent = "env: clean";
  if (ws) ws.close();
  connect();
});

const MARKET_SCENARIO = [
  { speaker: "shopkeeper", text: "namaste, kitne ka hai yeh?", tt: 0.9, coh: 0.9, noise: "quiet", delay: 400 },
  { speaker: "shopkeeper", text: "aap kahan se ho?", tt: 0.85, coh: 0.85, noise: "quiet", delay: 1200 },
  { speaker: "random_vendor_nearby", text: "aloo le lo, sasta aloo", tt: 0.1, coh: 0.05, noise: "loud_crowd", delay: 1200 },
  { speaker: "shopkeeper", text: "accha, France se!", tt: 0.85, coh: 0.8, noise: "quiet", delay: 1200 },
  // Second legitimate voice — room available in the 2-slot group, joins directly.
  { speaker: "shopkeeper_spouse", text: "aur yeh dekhiye, bahut accha hai", tt: 0.9, coh: 0.9, noise: "quiet", delay: 1400 },
  // Group is now full (2/2) — a same-confidence voice is correctly rejected,
  // since nobody's absent and there's no free slot.
  { speaker: "another_random_vendor", text: "sasta sasta, dekh lo", tt: 0.9, coh: 0.9, noise: "quiet", delay: 1000 },
  // Wait past the 8s absence timeout so the spouse's slot becomes
  // replaceable, then a new customer takes it — moderate confidence (0.9/0.9)
  // requires two confirming turns from the same voice (see cie/engine.py).
  { speaker: "new_customer", text: "excusez-moi, avez-vous ceci en bleu?", tt: 0.9, coh: 0.9, noise: "quiet", delay: 9000 },
  { speaker: "new_customer", text: "avez-vous ceci en bleu?", tt: 0.9, coh: 0.9, noise: "quiet", delay: 1400 },
];

document.getElementById("scenarioBtn").addEventListener("click", async () => {
  logEvent("Running market-stall scenario...");
  for (const [i, step] of MARKET_SCENARIO.entries()) {
    if (step.delay >= 5000) {
      logEvent(`(waiting ${(step.delay / 1000).toFixed(1)}s — clearing the partner-absence timeout before the next speaker)`);
    }
    await new Promise((r) => setTimeout(r, step.delay));
    sendUtterance("scenario:" + step.speaker, step.text, "en", step.tt, step.coh, step.noise);
  }
});

connect();
