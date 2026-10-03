import json
import os
import sys
import logging
import base64
import uuid
import boto3

# ---------------------------------------------------------------------------
# DynamoDB persistence layer
# Appended to sys.path so Lambda can resolve the app package.
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from app.storage.dynamodb import update_extraction_fields, update_followup_state  # noqa: E402
from app.followup import (  # noqa: E402
    derive_followup,
    confirm_followup,
    ignore_followup,
    STATUS_CONFIRMED,
    STATUS_NEEDS_CLAR,
    STATUS_AWAITING,
    STATUS_IGNORED,
)

# ---------------------------------------------------------------------------
# CORS headers
# ---------------------------------------------------------------------------
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}

# ---------------------------------------------------------------------------
# Bedrock config — mirrors test_extraction.py
# ---------------------------------------------------------------------------
BEDROCK_MODEL_ID = "amazon.nova-pro-v1:0"
BEDROCK_REGION = "us-east-1"

# System prompt reused verbatim from test_extraction.py
EXTRACTION_SYSTEM_PROMPT = """You are a family care document extraction agent.

Extract ONLY information explicitly present in the source document.
Never infer, guess, add medical knowledge, or invent missing information.

Return a JSON object with EXACTLY these keys:

{
  "patient": "<name or null>",
  "document_date": "<date string or null>",
  "doctor": "<name or null>",
  "specialty": "<specialty or null>",
  "medicines": [{"name": "<string>", "dosage": "<string or null>", "frequency": "<string or null>"}],
  "requested_tests": ["<test name>"],
  "follow_up": "<instruction string or null>"
}

Rules:
- If a value is not present in the document, use null or an empty list.
- Preserve medicine dosage and frequency exactly as written.
- Do not diagnose, interpret tests, or recommend treatment.
- Do not add information from medical knowledge not in the document.
- Return only the JSON object, no explanation or markdown fencing."""

# ---------------------------------------------------------------------------
# Synthetic prescription pre-populated in the UI textarea
# ---------------------------------------------------------------------------
SAMPLE_PRESCRIPTION = """Patient: Raj Sharma
Date: 26 August 2026
Doctor: Dr. Meera Kapoor
Specialty: Cardiology

Medicines:
Amlodipine 5 mg once daily
Atorvastatin 20 mg once daily at night

Investigations:
CBC
Lipid Profile

Follow-up:
Review after 4 weeks."""

# ---------------------------------------------------------------------------
# HTML dashboard
# ---------------------------------------------------------------------------
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>CareCircle</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  html { font-size: 16px; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, sans-serif;
    background: #f7f8fc;
    color: #1a1d23;
    min-height: 100vh;
  }

  /* Header */
  .site-header {
    background: #ffffff;
    border-bottom: 1px solid #e8eaf0;
    padding: 0 24px;
    display: flex;
    align-items: center;
    height: 64px;
    position: sticky;
    top: 0;
    z-index: 100;
  }
  .logo { display: flex; align-items: center; gap: 10px; }
  .logo-icon {
    width: 36px; height: 36px;
    background: linear-gradient(135deg, #3b82f6 0%, #1d4ed8 100%);
    border-radius: 10px;
    display: flex; align-items: center; justify-content: center;
    color: #fff; font-size: 18px; flex-shrink: 0;
  }
  .logo-text { display: flex; flex-direction: column; }
  .logo-name { font-size: 1.1rem; font-weight: 700; color: #1a1d23; letter-spacing: -0.02em; }
  .logo-tagline { font-size: 0.72rem; color: #6b7280; letter-spacing: 0.01em; }

  /* Layout */
  .page { max-width: 960px; margin: 0 auto; padding: 32px 24px 64px; }
  .section-label {
    font-size: 0.7rem; font-weight: 700; letter-spacing: 0.08em;
    text-transform: uppercase; color: #9ca3af; margin-bottom: 12px;
  }

  /* Cards */
  .card { background: #ffffff; border: 1px solid #e8eaf0; border-radius: 14px; padding: 20px 24px; }
  .card + .card { margin-top: 12px; }

  /* Family grid */
  .family-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 32px; }
  @media (max-width: 600px) { .family-grid { grid-template-columns: 1fr; } }

  .member-card {
    background: #ffffff; border: 1px solid #e8eaf0; border-radius: 14px;
    padding: 18px 20px; display: flex; align-items: flex-start; gap: 14px;
  }
  .member-card.has-action { border-left: 4px solid #f59e0b; }
  .avatar {
    width: 44px; height: 44px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-weight: 700; font-size: 0.95rem; flex-shrink: 0; color: #fff;
  }
  .avatar-blue { background: linear-gradient(135deg, #3b82f6, #1d4ed8); }
  .avatar-teal { background: linear-gradient(135deg, #14b8a6, #0f766e); }
  .member-info { flex: 1; min-width: 0; }
  .member-name { font-weight: 700; font-size: 0.95rem; color: #111827; }
  .member-rel { font-size: 0.78rem; color: #6b7280; margin-bottom: 6px; }
  .member-spec { font-size: 0.82rem; color: #374151; margin-bottom: 4px; }
  .member-date { font-size: 0.78rem; color: #6b7280; }

  /* Badges */
  .badge {
    display: inline-flex; align-items: center; gap: 4px;
    font-size: 0.72rem; font-weight: 600; padding: 3px 9px;
    border-radius: 999px; margin-top: 8px;
  }
  .badge-action  { background: #fef3c7; color: #92400e; }
  .badge-ok      { background: #d1fae5; color: #065f46; }
  .badge-missing { background: #fee2e2; color: #991b1b; }
  .badge-ready   { background: #dbeafe; color: #1e40af; }
  .badge-ai      { background: #ede9fe; color: #5b21b6; }
  .badge-dot::before {
    content: ""; display: inline-block; width: 6px; height: 6px;
    border-radius: 50%; background: currentColor;
  }

  /* Alert */
  .alert {
    background: #fffbeb; border: 1px solid #fde68a; border-left: 4px solid #f59e0b;
    border-radius: 10px; padding: 14px 18px;
    display: flex; align-items: flex-start; gap: 12px; margin-bottom: 28px;
  }
  .alert-icon { font-size: 1.2rem; flex-shrink: 0; line-height: 1.3; }
  .alert-body { flex: 1; }
  .alert-title { font-weight: 700; font-size: 0.88rem; color: #92400e; margin-bottom: 2px; }
  .alert-msg { font-size: 0.83rem; color: #78350f; }

  /* Section */
  .section { margin-bottom: 32px; }
  .section-title {
    font-size: 1.05rem; font-weight: 700; color: #111827; margin-bottom: 14px;
    display: flex; align-items: center; gap: 8px;
  }
  .section-title-icon {
    width: 28px; height: 28px; background: #eff6ff; border-radius: 8px;
    display: flex; align-items: center; justify-content: center;
    font-size: 14px; flex-shrink: 0;
  }

  /* Doc source */
  .doc-source {
    display: flex; align-items: center; gap: 10px; padding: 12px 16px;
    background: #f8faff; border: 1px solid #dbeafe; border-radius: 10px; margin-bottom: 20px;
  }
  .doc-icon { font-size: 1.3rem; flex-shrink: 0; }
  .doc-title { font-weight: 600; font-size: 0.88rem; color: #1e40af; }
  .doc-meta  { font-size: 0.78rem; color: #6b7280; margin-top: 1px; }

  /* Medicine list */
  .med-list { list-style: none; }
  .med-list li {
    display: flex; align-items: center; gap: 10px; padding: 10px 0;
    border-bottom: 1px solid #f3f4f6; font-size: 0.88rem; color: #111827;
  }
  .med-list li:last-child { border-bottom: none; }
  .med-dot { width: 8px; height: 8px; border-radius: 50%; background: #3b82f6; flex-shrink: 0; }
  .med-name { font-weight: 600; }
  .med-freq { color: #6b7280; margin-left: auto; font-size: 0.8rem; }

  /* Test list */
  .test-list { list-style: none; }
  .test-list li {
    display: flex; align-items: center; justify-content: space-between;
    padding: 10px 0; border-bottom: 1px solid #f3f4f6; font-size: 0.88rem;
  }
  .test-list li:last-child { border-bottom: none; }
  .test-name { font-weight: 600; color: #111827; }
  .provenance-note { font-size: 0.73rem; color: #9ca3af; margin-top: 12px; display: flex; align-items: center; gap: 5px; }

  /* Appointment prep */
  .appt-card { background: #f8faff; border: 1px solid #dbeafe; border-radius: 12px; padding: 18px 20px; }
  .appt-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 16px; }
  .appt-title { font-weight: 700; font-size: 0.95rem; color: #1e40af; }
  .appt-detail { font-size: 0.82rem; color: #374151; margin-top: 2px; }
  .appt-checklist { list-style: none; }
  .appt-checklist li { display: flex; align-items: center; gap: 10px; font-size: 0.85rem; padding: 6px 0; color: #111827; }
  .check-icon { font-size: 1rem; flex-shrink: 0; }

  /* ── AI Analysis section ──────────────────────────────────────────────── */
  .ai-section {
    background: #faf5ff;
    border: 1px solid #ddd6fe;
    border-radius: 14px;
    padding: 22px 24px;
  }
  .ai-section-header {
    display: flex; align-items: center; gap: 10px; margin-bottom: 16px;
  }
  .ai-icon {
    width: 32px; height: 32px;
    background: linear-gradient(135deg, #7c3aed, #4f46e5);
    border-radius: 9px;
    display: flex; align-items: center; justify-content: center;
    color: #fff; font-size: 15px; flex-shrink: 0;
  }
  .ai-title { font-weight: 700; font-size: 1rem; color: #3b0764; }
  .ai-subtitle { font-size: 0.78rem; color: #7c3aed; margin-top: 1px; }

  textarea#rx-input {
    width: 100%; min-height: 180px;
    border: 1px solid #ddd6fe; border-radius: 10px;
    padding: 12px 14px; font-size: 0.84rem;
    font-family: "SF Mono", "Fira Code", "Consolas", monospace;
    background: #ffffff; color: #1a1d23; resize: vertical;
    outline: none; line-height: 1.6;
  }
  textarea#rx-input:focus { border-color: #7c3aed; box-shadow: 0 0 0 3px rgba(124,58,237,0.12); }

  button#analyze-btn {
    margin-top: 12px;
    background: linear-gradient(135deg, #7c3aed, #4f46e5);
    color: #fff; border: none; border-radius: 10px;
    padding: 10px 22px; font-size: 0.88rem; font-weight: 600;
    cursor: pointer; display: inline-flex; align-items: center; gap: 8px;
    transition: opacity 0.15s;
  }
  button#analyze-btn:hover { opacity: 0.88; }
  button#analyze-btn:disabled { opacity: 0.55; cursor: not-allowed; }

  .prescription-upload {
    margin-top: 16px; padding: 16px;
    background: #fff; border: 1px dashed #c4b5fd; border-radius: 12px;
  }
  .prescription-upload label {
    display: block; color: #3b0764; font-size: 0.88rem;
    font-weight: 600; margin-bottom: 6px;
  }
  .upload-hint { font-size: 0.78rem; line-height: 1.5; color: #6b7280; margin: 0 0 12px; }
  #rx-file { display: block; width: 100%; min-width: 0; font-family: inherit;
    font-size: 0.82rem; color: #6b7280; border-radius: 8px; }
  #rx-file::file-selector-button {
    background: #ede9fe; color: #6d28d9; border: 1px solid #ddd6fe;
    border-radius: 8px; padding: 10px 16px; margin-right: 12px;
    font-family: inherit; font-weight: 600; cursor: pointer;
  }
  #rx-file::file-selector-button:hover { background: #ddd6fe; }
  #rx-file:focus-visible { outline: 2px solid #7c3aed; outline-offset: 4px; }

  /* Spinner */
  .spinner {
    display: none; width: 16px; height: 16px;
    border: 2px solid rgba(255,255,255,0.4);
    border-top-color: #fff; border-radius: 50%;
    animation: spin 0.7s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* Result area */
  #analyze-result { margin-top: 20px; display: none; }
  .result-header {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 12px; gap: 8px; flex-wrap: wrap;
  }
  .result-label { font-weight: 700; font-size: 0.88rem; color: #3b0764; }
  .result-provenance { font-size: 0.75rem; color: #7c3aed; }

  .result-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  @media (max-width: 600px) { .result-grid { grid-template-columns: 1fr; } }

  .result-field {
    background: #ffffff; border: 1px solid #ede9fe;
    border-radius: 10px; padding: 12px 14px;
  }
  .result-field-label { font-size: 0.7rem; font-weight: 700; letter-spacing: 0.07em; text-transform: uppercase; color: #7c3aed; margin-bottom: 5px; }
  .result-field-value { font-size: 0.86rem; color: #1a1d23; line-height: 1.5; }
  .result-field-value.null-val { color: #9ca3af; font-style: italic; }

  .result-list-item { display: flex; align-items: flex-start; gap: 6px; font-size: 0.84rem; color: #1a1d23; margin-bottom: 4px; }
  .result-list-item:last-child { margin-bottom: 0; }
  .result-list-dot { width: 6px; height: 6px; border-radius: 50%; background: #7c3aed; flex-shrink: 0; margin-top: 6px; }

  .error-msg {
    background: #fee2e2; border: 1px solid #fca5a5; border-radius: 10px;
    padding: 12px 16px; font-size: 0.84rem; color: #991b1b;
  }

  /* ── Follow-up confirmation card ─────────────────────────────────────── */
  .followup-card {
    margin-top: 20px;
    background: #f0fdf4;
    border: 1px solid #bbf7d0;
    border-left: 4px solid #22c55e;
    border-radius: 12px;
    padding: 18px 20px;
    display: none;
  }
  .followup-card.ambiguous {
    background: #fffbeb;
    border-color: #fde68a;
    border-left-color: #f59e0b;
  }
  .followup-card-title {
    font-weight: 700; font-size: 0.9rem; color: #14532d; margin-bottom: 4px;
  }
  .followup-card.ambiguous .followup-card-title { color: #78350f; }
  .followup-instruction {
    font-style: italic; font-size: 0.86rem; color: #374151;
    background: #f9fafb; border: 1px solid #e5e7eb;
    border-radius: 8px; padding: 8px 12px; margin: 10px 0;
  }
  .followup-suggested {
    font-size: 0.9rem; color: #111827; margin-bottom: 4px;
  }
  .followup-suggested strong { color: #15803d; }
  .followup-hint {
    font-size: 0.75rem; color: #6b7280; margin-bottom: 14px;
  }
  .followup-actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .btn-confirm {
    background: #16a34a; color: #fff; border: none; border-radius: 8px;
    padding: 8px 18px; font-size: 0.84rem; font-weight: 600; cursor: pointer;
  }
  .btn-confirm:hover { background: #15803d; }
  .btn-edit {
    background: #fff; color: #374151; border: 1px solid #d1d5db;
    border-radius: 8px; padding: 8px 18px; font-size: 0.84rem;
    font-weight: 600; cursor: pointer;
  }
  .btn-edit:hover { background: #f9fafb; }
  .btn-ignore {
    background: none; color: #9ca3af; border: none;
    font-size: 0.82rem; cursor: pointer; padding: 8px 10px;
    text-decoration: underline;
  }
  .btn-ignore:hover { color: #6b7280; }
  .followup-date-input {
    border: 1px solid #d1d5db; border-radius: 8px;
    padding: 7px 12px; font-size: 0.84rem; color: #111827;
    display: none;
  }
  .followup-date-input:focus { outline: none; border-color: #22c55e; }
  .followup-confirmed-msg {
    font-size: 0.84rem; font-weight: 600; color: #15803d;
  }
  .followup-ignored-msg {
    font-size: 0.82rem; color: #9ca3af; font-style: italic;
  }

  /* ── Demo safety banner ───────────────────────────────────────────────── */
  .demo-banner {
    background: #fefce8; border: 1px solid #fef08a;
    border-radius: 8px; padding: 10px 16px;
    font-size: 0.76rem; color: #713f12;
    text-align: center; margin-bottom: 24px;
  }

  /* Footer */
  .footer-notice {
    margin-top: 40px; padding: 14px 18px; background: #f3f4f6;
    border-radius: 10px; font-size: 0.78rem; color: #6b7280;
    text-align: center; line-height: 1.5;
  }

  .family-heading { display:flex; justify-content:space-between; align-items:center; gap:16px; margin:24px 0 16px; flex-wrap:wrap; }
  .family-heading .section-label { margin:0; }
  .family-subtitle { color:#6b7280; font-size:.88rem; margin-top:8px; }
  .family-button { background:#6d28d9; color:white; border:0; padding:10px 16px; border-radius:9px; font:inherit; font-size:.85rem; font-weight:600; cursor:pointer; }
  .family-button.secondary { background:#ede9fe; color:#6d28d9; }
  .family-button:disabled { opacity:.5; cursor:wait; }
  button.member-card { text-align:left; font:inherit; width:100%; cursor:pointer; background:white; }
  button.member-card[aria-pressed="true"] { border:2px solid #7c3aed; background:#faf5ff; }
  #member-form label { display:block; margin:12px 0; color:#374151; }
  #member-form input { display:block; padding:10px; border:1px solid #ddd6fe; border-radius:8px; width:100%; margin-top:5px; font:inherit; }
  #family-error, #member-error { color:#b91c1c; }
  .workspace-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:16px; margin-bottom:24px; }
  .workspace-grid .card { padding:20px; }
  .workspace-grid h3 { color:#4c1d95; margin-bottom:12px; font-size:1rem; }
  .workspace-grid ul { padding-left:18px; line-height:1.8; }
  .workspace-grid p { line-height:1.6; overflow-wrap:anywhere; }
  #care-summary { margin-bottom:24px; }
</style>
</head>
<body>

<header class="site-header">
  <div class="logo">
    <div class="logo-icon">&#10084;</div>
    <div class="logo-text">
      <span class="logo-name">CareCircle</span>
      <span class="logo-tagline">Your family&#8217;s care, organized.</span>
    </div>
  </div>
</header>

<main class="page">

  <!-- Demo safety banner -->
  <div class="demo-banner">
    &#9888; Demo environment &mdash; use synthetic information only. Do not enter real medical or personally identifiable information.
  </div>

  <div class="family-heading"><div><div class="section-label">Your care circle</div>
    <p class="family-subtitle">One place for everyone’s documents, medicines and next steps.</p></div>
    <button class="family-button" onclick="openMemberForm(false)">+ Add family member</button></div>
  <p id="family-error" role="alert"></p>
  <div id="member-form" class="card" hidden>
    <h3 id="member-form-title">Add family member</h3>
    <form onsubmit="event.preventDefault(); saveMember()">
      <label>Name <input id="member-name" maxlength="80" required /></label>
      <label>Relationship <input id="member-relationship" maxlength="40" required placeholder="e.g. Mom" /></label>
      <button class="family-button" id="save-member-btn" type="submit">Save member</button>
      <button class="family-button secondary" type="button" onclick="document.getElementById('member-form').hidden=true">Cancel</button>
      <p id="member-error" role="alert"></p>
    </form>
  </div>
  <div class="family-grid" id="family-grid">Loading your family…</div>
  <section id="workspace" hidden>
    <div class="family-heading"><div><div class="section-label" id="workspace-title"></div>
      <p class="family-subtitle" id="workspace-subtitle"></p></div>
      <button class="family-button secondary" onclick="openMemberForm(true)">Edit member</button></div>
    <div id="care-summary"></div>
  <!-- AI Analysis -->
  <div class="section">
    <div class="section-title">
      <div class="section-title-icon">&#10024;</div>
      Analyze Prescription
    </div>
    <div class="ai-section">
      <div class="ai-section-header">
        <div class="ai-icon">&#129302;</div>
        <div>
          <div class="ai-title">CareCircle AI Extraction</div>
          <div class="ai-subtitle">Powered by Amazon Nova Pro &nbsp;&#183;&nbsp; us-east-1</div>
        </div>
      </div>

      <textarea id="rx-input" spellcheck="false" placeholder="Paste this family member’s synthetic prescription, or upload a .txt file below."></textarea>

      <div class="prescription-upload">
        <label for="rx-file">Or upload a prescription</label>
        <p class="upload-hint" id="upload-hint">Synthetic demo files only · .txt · up to 100 KB. The selected file replaces pasted text for analysis; its original is saved privately.</p>
        <input id="rx-file" type="file" accept=".txt,text/plain" aria-describedby="upload-hint" />
      </div>
      <button id="analyze-btn" onclick="analyzeRx()">
        <span>Analyze with CareCircle AI</span>
        <div class="spinner" id="spinner"></div>
      </button>

      <div id="analyze-result"></div>
      <div id="followup-card" class="followup-card"></div>
    </div>
  </div>

  </section>
  <div class="footer-notice">CareCircle organizes the care your doctors have documented. It does not diagnose or recommend treatment.<br>Shared synthetic demo · uploaded prescriptions are stored privately · report availability is demo data, not automatically verified.</div>
</main>

<script>
let familyMembers = [];
let selectedId = null;
let editingId = null;
let familyBusy = false;
function setFamilyBusy(busy) {
  familyBusy = busy;
  document.querySelectorAll('.family-button, button.member-card').forEach(b => b.disabled = busy);
}
async function familyApi(path, body) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch(path, {method: body ? 'POST' : 'GET',
      headers: {'Content-Type':'application/json'}, signal: controller.signal,
      ...(body ? {body:JSON.stringify(body)} : {})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not save or load your family.');
    return data;
  } finally { clearTimeout(timer); }
}
function renderFamilyGrid() {
  const grid = document.getElementById('family-grid');
  grid.replaceChildren();
  for (const member of familyMembers) {
    const state = member.care_state || {};
    const fu = state.follow_up && typeof state.follow_up === 'object' ? state.follow_up : {};
    const missing = (state.requested_tests || []).filter(t => !(state.document_availability || {})[t]).length;
    const next = fu.status === 'CONFIRMED' ? 'Confirmed review: ' + fmtDate(fu.confirmed_date)
      : fu.status === 'IGNORED' ? 'Follow-up ignored'
      : fu.status ? 'Follow-up needs confirmation' : 'No follow-up recorded';
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'member-card';
    button.setAttribute('aria-pressed', String(member.patient_id === selectedId));
    button.disabled = familyBusy;
    const initials = member.name.split(' ').filter(Boolean).map(p => p[0]).slice(0,2).join('');
    button.innerHTML = '<div class="avatar avatar-blue">' + val(initials) + '</div><div class="member-info">' +
      '<div class="member-name">' + val(member.name) + '</div><div class="member-rel">' + val(member.relationship) + '</div>' +
      '<div class="member-spec">' + val(state.specialty || 'Care Space') + '</div><div class="member-date">' + val(next) + '</div>' +
      '<span class="badge ' + (missing ? 'badge-action' : 'badge-ok') + '">' +
      (missing ? missing + ' requested item(s) not marked ready' : state.source_document ? 'Records saved' : 'Add a prescription') + '</span></div>';
    button.onclick = () => selectMember(member.patient_id);
    grid.appendChild(button);
  }
}
function renderCareSummary(member) {
  const s = member.care_state || {};
  const fu = typeof s.follow_up === 'object' && s.follow_up ? s.follow_up : {};
  document.getElementById('workspace-title').textContent = member.name + '’s Care Space';
  document.getElementById('workspace-subtitle').textContent = member.relationship + ' · Documents, medicines and follow-ups saved separately';
  const meds = (s.medicines || []).map(m => '<li>' + val([m.name,m.dosage,m.frequency].filter(Boolean).join(' · ')) + '</li>').join('');
  const tests = (s.requested_tests || []).map(t => '<li>' + val(t) + ' — ' + ((s.document_availability || {})[t] ? 'Marked ready (demo)' : 'Not marked ready') + '</li>').join('');
  const next = fu.status === 'CONFIRMED' ? fmtDate(fu.confirmed_date) + ' · Confirmed'
    : fu.status === 'IGNORED' ? 'Ignored by caregiver'
    : fu.suggested_date ? fmtDate(fu.suggested_date) + ' · Awaiting confirmation' : 'No confirmed follow-up';
  document.getElementById('care-summary').innerHTML = '<div class="workspace-grid">' +
    '<div class="card"><h3>Latest prescription</h3><p>' + val(s.source_document || 'No prescription uploaded yet') + '</p><p>' + val(s.document_date || '') + '</p><p>' + val(s.doctor || '') + '</p>' + (s.source_storage ? '<p>Original stored privately in S3</p>' : '') + '</div>' +
    '<div class="card"><h3>Prescribed medicines</h3>' + (meds ? '<ul>' + meds + '</ul>' : '<p>No medicines recorded.</p>') + '</div>' +
    '<div class="card"><h3>Preparation checklist</h3>' + (tests ? '<ul>' + tests + '</ul>' : '<p>No requested tests recorded.</p>') + '</div>' +
    '<div class="card"><h3>Next follow-up</h3><p>' + val(next) + '</p><p>' + val(fu.source_instruction || '') + '</p></div></div>';
}
function selectMember(id) {
  if (familyBusy) return;
  const member = familyMembers.find(m => m.patient_id === id);
  if (!member) return;
  selectedId = id;
  document.getElementById('workspace').hidden = false;
  document.getElementById('rx-input').value = '';
  document.getElementById('rx-file').value = '';
  document.querySelector('#analyze-btn span').textContent = 'Analyze prescription';
  document.getElementById('analyze-result').innerHTML = '';
  document.getElementById('analyze-result').style.display = 'none';
  const card = document.getElementById('followup-card');
  card.innerHTML = ''; card.style.display = 'none'; card._fuState = null;
  renderFamilyGrid(); renderCareSummary(member);
  const fu = member.care_state.follow_up;
  if (fu && typeof fu === 'object') renderFollowupCard(fu, id);
}
async function loadFamily(preferredId, select = true) {
  try {
    const data = await familyApi('/family');
    familyMembers = data.members;
    selectedId = preferredId || selectedId || (familyMembers[0] || {}).patient_id;
    document.getElementById('family-error').textContent = '';
    if (select) selectMember(selectedId);
    else {
      renderFamilyGrid();
      const member = familyMembers.find(m => m.patient_id === selectedId);
      if (member) renderCareSummary(member);
    }
  } catch (e) {
    document.getElementById('family-error').textContent = 'Family refresh failed. ' + e.message;
  }
}
function openMemberForm(edit) {
  if (familyBusy) return;
  const member = edit ? familyMembers.find(m => m.patient_id === selectedId) : null;
  editingId = member ? member.patient_id : null;
  document.getElementById('member-form-title').textContent = edit ? 'Edit family member' : 'Add family member';
  document.getElementById('member-name').value = member ? member.name : '';
  document.getElementById('member-relationship').value = member ? member.relationship : '';
  document.getElementById('member-error').textContent = '';
  document.getElementById('member-form').hidden = false;
  document.getElementById('member-name').focus();
}
async function saveMember() {
  if (familyBusy) return;
  setFamilyBusy(true);
  try {
    const data = await familyApi('/members', {patient_id: editingId,
      name: document.getElementById('member-name').value.trim(),
      relationship: document.getElementById('member-relationship').value.trim()});
    await loadFamily(data.member.patient_id, false);
    document.getElementById('member-form').hidden = true;
    setFamilyBusy(false);
    selectMember(data.member.patient_id);
  } catch (e) { document.getElementById('member-error').textContent = e.message; }
  finally { setFamilyBusy(false); }
}

async function analyzeRx() {
  if (!selectedId || familyBusy) return;
  const requestMember = selectedId;
  const btn     = document.getElementById('analyze-btn');
  const spinner = document.getElementById('spinner');
  const result  = document.getElementById('analyze-result');
  const fuCard  = document.getElementById('followup-card');
  const text    = document.getElementById('rx-input').value.trim();

  const file = document.getElementById('rx-file').files[0];
  if (!text && !file) return;

  setFamilyBusy(true);
  btn.disabled = true;
  spinner.style.display = 'block';
  result.style.display  = 'none';
  result.innerHTML      = '';
  fuCard.style.display  = 'none';
  fuCard.innerHTML      = '';

  try {
    let payload = {document_text: text};
    if (file) {
      if (!file.name.toLowerCase().endsWith('.txt') || file.size > 102400 || !file.size)
        throw new Error('Choose a non-empty UTF-8 .txt file under 100 KB.');
      const bytes = new Uint8Array(await file.arrayBuffer());
      let binary = '';
      for (const byte of bytes) binary += String.fromCharCode(byte);
      payload = {file_name: file.name, file_base64: btoa(binary)};
    }
    payload.patient_id = requestMember;
    const resp = await fetch('/analyze', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(payload)
    });
    const data = await resp.json();

    if (!resp.ok || data.error) {
      result.innerHTML = '<div class="error-msg">&#10060; ' +
        val(data.error || 'Analysis failed. Please try again.') + '</div>';
    } else {
      result.innerHTML = renderExtraction(data);
      if (data.source_document) {
        const source = document.createElement('p');
        source.textContent = 'Source: ' + data.source_document +
          (data.source_storage ? ' — original saved in S3' : ' — pasted text');
        result.prepend(source);
      }
      if (data.follow_up_state) renderFollowupCard(data.follow_up_state, data.patient_id);
      await loadFamily(requestMember, false);
    }
  } catch (e) {
    result.innerHTML = '<div class="error-msg">&#10060; Network error: ' + e.message + '</div>';
  } finally {
    setFamilyBusy(false);
    btn.disabled = false;
    spinner.style.display = 'none';
    result.style.display  = 'block';
  }
}

function val(v) {
  if (v === null || v === undefined || v === '') return '<span class="null-val">not stated</span>';
  return String(v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function renderExtraction(d) {
  const meds = Array.isArray(d.medicines) && d.medicines.length > 0
    ? d.medicines.map(m => {
        const parts = [m.name, m.dosage, m.frequency].filter(Boolean).join(' \u00b7 ');
        return '<div class="result-list-item"><div class="result-list-dot"></div><div>' + val(parts) + '</div></div>';
      }).join('')
    : '<span class="null-val">none stated</span>';

  const tests = Array.isArray(d.requested_tests) && d.requested_tests.length > 0
    ? d.requested_tests.map(t =>
        '<div class="result-list-item"><div class="result-list-dot"></div><div>' + val(t) + '</div></div>'
      ).join('')
    : '<span class="null-val">none stated</span>';

  return `
    <div class="result-header">
      <div class="result-label">&#10024; Extracted with Amazon Nova Pro</div>
      <div class="result-provenance">Grounded only in the uploaded document.</div>
    </div>
    <div class="result-grid">
      <div class="result-field">
        <div class="result-field-label">Patient</div>
        <div class="result-field-value">${val(d.patient)}</div>
      </div>
      <div class="result-field">
        <div class="result-field-label">Document Date</div>
        <div class="result-field-value">${val(d.document_date)}</div>
      </div>
      <div class="result-field">
        <div class="result-field-label">Doctor</div>
        <div class="result-field-value">${val(d.doctor)}</div>
      </div>
      <div class="result-field">
        <div class="result-field-label">Specialty</div>
        <div class="result-field-value">${val(d.specialty)}</div>
      </div>
      <div class="result-field">
        <div class="result-field-label">Prescribed Medicines</div>
        <div class="result-field-value">${meds}</div>
      </div>
      <div class="result-field">
        <div class="result-field-label">Requested Tests</div>
        <div class="result-field-value">${tests}</div>
      </div>
      <div class="result-field" style="grid-column: 1/-1;">
        <div class="result-field-label">Follow-up (documented instruction)</div>
        <div class="result-field-value">${val(d.follow_up)}</div>
      </div>
    </div>`;
}

// ── Follow-up card ──────────────────────────────────────────────────────────

function fmtDate(iso) {
  if (!iso) return null;
  const [y, m, d] = iso.split('-').map(Number);
  return new Date(y, m - 1, d).toLocaleDateString('en-GB', {day: 'numeric', month: 'long', year: 'numeric'});
}

function renderFollowupCard(fu, patientId) {
  const card = document.getElementById('followup-card');
  // Store the current follow_up_state on the card element so action handlers
  // can pass it back to /followup (backend uses it when DynamoDB still holds
  // the old raw string follow_up).
  card._fuState    = fu;
  card._patientId  = patientId;
  const status = fu.status;

  if (status === 'IGNORED') {
    card.className = 'followup-card';
    card.style.display = 'block';
    card.innerHTML = '<span class="followup-ignored-msg">Follow-up marked as ignored.</span>';
    return;
  }

  if (status === 'CONFIRMED') {
    card.className = 'followup-card';
    card.style.display = 'block';
    card.innerHTML = '<span class="followup-confirmed-msg">&#9989; Follow-up confirmed for ' + fmtDate(fu.confirmed_date) + '.</span>';
    return;
  }

  const isAmbiguous = (status === 'NEEDS_CLARIFICATION') || !fu.suggested_date;
  card.className = 'followup-card' + (isAmbiguous ? ' ambiguous' : '');
  card.style.display = 'block';

  const instr = fu.source_instruction
    ? '<div class="followup-instruction">&ldquo;' + val(fu.source_instruction) + '&rdquo;</div>'
    : '';

  if (isAmbiguous) {
    card.innerHTML = `
      <div class="followup-card-title">&#128197; Follow-up</div>
      <div style="font-size:0.84rem;color:#374151;margin-bottom:8px;">CareCircle found:</div>
      ${instr}
      <div style="font-size:0.84rem;color:#78350f;margin-bottom:12px;">We couldn&#8217;t determine an exact follow-up date from this instruction. Please enter a date or ignore.</div>
      <div class="followup-actions">
        <input type="date" id="fu-date-input" class="followup-date-input" style="display:block;" />
        <button class="btn-confirm" onclick="saveFollowupDate('${patientId}')">Save date</button>
        <button class="btn-ignore" onclick="ignoreFollowup('${patientId}')">Ignore</button>
      </div>`;
  } else {
    const suggested = fmtDate(fu.suggested_date);
    card.innerHTML = `
      <div class="followup-card-title">&#128197; Follow-up</div>
      <div style="font-size:0.84rem;color:#374151;margin-bottom:8px;">CareCircle found:</div>
      ${instr}
      <div class="followup-suggested">Suggested follow-up: <strong>${suggested}</strong></div>
      <div class="followup-hint">Derived from the documented instruction &amp; prescription date. Please confirm or adjust.</div>
      <div class="followup-actions">
        <button class="btn-confirm" onclick="confirmFollowup('${patientId}', '${fu.suggested_date}')">Confirm follow-up</button>
        <button class="btn-edit" onclick="showDateEdit('${patientId}')">Edit date</button>
        <button class="btn-ignore" onclick="ignoreFollowup('${patientId}')">Ignore</button>
      </div>
      <div id="fu-edit-row" style="display:none;margin-top:10px;display:none;">
        <input type="date" id="fu-date-input" class="followup-date-input" style="display:inline-block;" value="${fu.suggested_date}" />
        <button class="btn-confirm" style="margin-left:8px;" onclick="saveFollowupDate('${patientId}')">Save</button>
      </div>`;
  }
}

function showDateEdit(patientId) {
  const row = document.getElementById('fu-edit-row');
  if (row) row.style.display = 'flex';
}

async function confirmFollowup(patientId, confirmedDate) {
  await _followupAction(patientId, {action: 'confirm', confirmed_date: confirmedDate});
}

async function saveFollowupDate(patientId) {
  const input = document.getElementById('fu-date-input');
  if (!input || !input.value) { alert('Please select a date.'); return; }
  await _followupAction(patientId, {action: 'confirm', confirmed_date: input.value});
}

async function ignoreFollowup(patientId) {
  await _followupAction(patientId, {action: 'ignore'});
}

async function _followupAction(patientId, payload) {
  const card = document.getElementById('followup-card');
  if (card._saving) return;
  const fuState = card._fuState || null;
  const previousHTML = card.innerHTML;
  const dateInput = document.getElementById('fu-date-input');
  const previousDate = dateInput ? dateInput.value : null;
  const controller = new AbortController();
  let timer;
  card._saving = true;
  setFamilyBusy(true);
  card.innerHTML = '<span>Saving&hellip;</span>';
  try {
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => {
        reject(new Error('SAVE_TIMEOUT'));
        controller.abort();
      }, 15000);
    });
    const request = async () => {
      const resp = await fetch('/followup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: controller.signal,
        body: JSON.stringify({patient_id: patientId, follow_up_state: fuState, ...payload})
      });
      const data = await resp.json();
      if (!resp.ok || data.error || !data.follow_up) throw new Error('SAVE_FAILED');
      return data;
    };
    const data = await Promise.race([request(), timeout]);
    renderFollowupCard(data.follow_up, patientId);
    await loadFamily(patientId, false);
  } catch (e) {
    card.innerHTML = previousHTML;
    const restoredInput = document.getElementById('fu-date-input');
    if (restoredInput && previousDate !== null) restoredInput.value = previousDate;
    const error = document.createElement('div');
    error.className = 'error-msg';
    error.setAttribute('role', 'alert');
    error.textContent = e.message === 'SAVE_TIMEOUT'
      ? 'Saving timed out. The change may still have saved. Check the saved state before retrying.'
      : 'Could not verify the save. Check the saved state before retrying.';
    card.appendChild(error);
  } finally {
    clearTimeout(timer);
    card._saving = false;
    setFamilyBusy(false);
  }
}

document.getElementById('rx-file').addEventListener('change', () => {
  document.querySelector('#analyze-btn span').textContent = document.getElementById('rx-file').files.length ? 'Upload & Analyze' : 'Analyze prescription';
});
loadFamily();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Bedrock extraction — direct boto3, no Strands dependency
# ---------------------------------------------------------------------------
def _call_bedrock(document_text: str) -> dict:
    """
    Call Amazon Bedrock Converse API directly via boto3.
    Mirrors the system prompt and extraction goal from test_extraction.py
    but uses boto3 bedrock-runtime instead of the Strands Agent wrapper.
    This keeps the Lambda dependency footprint to stdlib + boto3 only.
    """
    client = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)

    response = client.converse(
        modelId=BEDROCK_MODEL_ID,
        system=[{"text": EXTRACTION_SYSTEM_PROMPT}],
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "text": (
                            "Extract the structured information from this document. "
                            "Return only the JSON object.\n\n"
                            "DOCUMENT:\n"
                            "---\n"
                            + document_text
                            + "\n---"
                        )
                    }
                ],
            }
        ],
        inferenceConfig={"maxTokens": 1024, "temperature": 0},
    )

    raw_text = response["output"]["message"]["content"][0]["text"].strip()

    # Strip any accidental markdown fences
    if raw_text.startswith("```"):
        raw_text = raw_text.split("```")[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]
        raw_text = raw_text.strip()

    return json.loads(raw_text)


# ---------------------------------------------------------------------------
# DynamoDB persistence helper
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _persist_extracted(extracted: dict, source_document="prescription", source_storage=None, selected_member=None) -> tuple:
    """
    Derive follow-up state and (if CARE_STATE_TABLE is configured) persist
    the extraction fields to DynamoDB via UpdateItem.

    Always returns (follow_up_state, patient_id).

    Persistence failures are logged to CloudWatch and re-raised so the
    /analyze caller can surface them — the endpoint returns 500 on write
    failure rather than silently reporting success.
    """
    patient_name = (extracted.get("patient") or "").strip()
    patient_id   = "_".join(patient_name.lower().split()) if patient_name else None

    if selected_member:
        patient_id = selected_member["patient_id"]
        patient_name = selected_member["name"]

    follow_up_state = derive_followup(
        extracted.get("follow_up"),
        extracted.get("document_date"),
    )

    table = os.environ.get("CARE_STATE_TABLE", "").strip()
    if not table:
        print("[CareCircle] CARE_STATE_TABLE not set — skipping persistence")
        return follow_up_state, patient_id

    if not patient_id:
        print("[CareCircle] No patient name in extraction — skipping persistence")
        return follow_up_state, patient_id

    logger.info("Persistence starting table_configured=true")

    extraction_fields = {
        "patient_id":      patient_id,
        "patient_name":    patient_name,
        "source_document": source_document,
        "source_storage": source_storage,
        "doctor":          extracted.get("doctor"),
        "specialty":       extracted.get("specialty"),
        "document_date":   extracted.get("document_date"),
        "medicines":       extracted.get("medicines") or [],
        "requested_tests": extracted.get("requested_tests") or [],
        "follow_up":       follow_up_state,
    }

    # Do NOT catch exceptions here — let them propagate to the /analyze handler
    # so a DynamoDB failure surfaces as a 500 instead of silent success.
    # CloudWatch will show the exception in the Lambda log.
    try:
        update_extraction_fields(patient_id, extraction_fields)
    except Exception as exc:
        # Exception messages/tracebacks may contain request or medical data.
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "unknown")
        logger.error("Persistence failed exception_type=%s aws_error_code=%s",
                     type(exc).__name__, code)
        raise
    logger.info("Persistence succeeded")

    return follow_up_state, patient_id


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------
def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath", "/")

    # CORS preflight
    if method == "OPTIONS":
        return {"statusCode": 204, "headers": CORS_HEADERS, "body": ""}

    # GET /
    if method == "GET" and path == "/":
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "text/html; charset=utf-8", **CORS_HEADERS},
            "body": HTML,
        }

    # GET /health
    if method == "GET" and path == "/health":
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json", **CORS_HEADERS},
            "body": json.dumps({
                "app": "CareCircle",
                "status": "running",
                "message": "Family care coordination workspace",
            }),
        }

    # Shared synthetic-demo household. No real patient data or authentication.
    if (method == "GET" and path == "/family") or (method == "POST" and path == "/members"):
        from app.storage.dynamodb import list_family_members, save_family_member, get_care_state
        def reply(status, payload):
            return {"statusCode": status, "headers": {"Content-Type": "application/json", **CORS_HEADERS},
                    "body": json.dumps(payload)}
        try:
            members = list_family_members()
            if method == "GET":
                return reply(200, {"members": [dict(m, care_state=get_care_state(m["patient_id"]) or {}) for m in members]})
            raw = event.get("body") or "{}"
            if event.get("isBase64Encoded"):
                raw = base64.b64decode(raw).decode("utf-8")
            body = json.loads(raw)
            name = body.get("name", "")
            relationship = body.get("relationship", "")
            if (not isinstance(name, str) or not isinstance(relationship, str)
                    or not 1 <= len(name.strip()) <= 80 or not 1 <= len(relationship.strip()) <= 40
                    or any(ord(c) < 32 for c in name + relationship)):
                return reply(400, {"error": "Enter a name (up to 80 characters) and relationship (up to 40)."})
            patient_id = body.get("patient_id")
            if patient_id and not any(m["patient_id"] == patient_id for m in members):
                return reply(404, {"error": "Family member not found."})
            if any(" ".join(m["name"].casefold().split()) == " ".join(name.casefold().split())
                   and m["patient_id"] != patient_id for m in members):
                return reply(409, {"error": "A member with this name already exists."})
            if not patient_id and len(members) >= 12:
                return reply(400, {"error": "This demo supports up to 12 family members."})
            member = {"patient_id": patient_id or uuid.uuid4().hex,
                      "name": name.strip(), "relationship": relationship.strip()}
            save_family_member(member)
            return reply(200, {"member": member})
        except (ValueError, TypeError):
            return reply(400, {"error": "Invalid member details."})
        except Exception as exc:
            logger.error("Family request failed exception_type=%s", type(exc).__name__)
            return reply(500, {"error": "Could not load or save the family. Please retry."})

    # POST /analyze
    if method == "POST" and path == "/analyze":
        try:
            body_raw = event.get("body") or "{}"
            if event.get("isBase64Encoded"):
                body_raw = base64.b64decode(body_raw).decode("utf-8")
            body = json.loads(body_raw)
            source_name = "Pasted prescription"
            upload = None
            if "file_base64" in body:
                try:
                    source_name = body.get("file_name", "")
                    if (not isinstance(source_name, str) or not source_name.lower().endswith(".txt")
                            or len(source_name) > 160 or any(c in source_name for c in "\\/\r\n")):
                        raise ValueError("Invalid filename")
                    encoded = body["file_base64"]
                    if not isinstance(encoded, str) or len(encoded) > 136536:
                        raise ValueError("File too large")
                    upload = base64.b64decode(encoded, validate=True)
                    if not upload or len(upload) > 102400:
                        raise ValueError("Invalid file size")
                    document_text = upload.decode("utf-8-sig").strip()
                    if "\x00" in document_text:
                        raise ValueError("Binary file")
                except (ValueError, UnicodeError, TypeError):
                    return {"statusCode": 400, "headers": {"Content-Type": "application/json", **CORS_HEADERS},
                            "body": json.dumps({"error": "Choose a non-empty UTF-8 .txt file under 100 KB."})}
                if not os.environ.get("CARE_DOCUMENT_BUCKET", "").strip():
                    return {"statusCode": 503, "headers": {"Content-Type": "application/json", **CORS_HEADERS},
                            "body": json.dumps({"error": "Document storage is not configured."})}
            else:
                document_text = (body.get("document_text") or "").strip()

            if not document_text:
                return {
                    "statusCode": 400,
                    "headers": {"Content-Type": "application/json", **CORS_HEADERS},
                    "body": json.dumps({"error": "document_text is required"}),
                }

            if not os.environ.get("CARE_STATE_TABLE", "").strip():
                logger.error("Persistence unavailable table_configured=false")
                return {
                    "statusCode": 503,
                    "headers": {"Content-Type": "application/json", **CORS_HEADERS},
                    "body": json.dumps({"error": "Care-state storage is not configured", "persisted": False}),
                }

            selected_member = None
            if body.get("patient_id"):
                from app.storage.dynamodb import list_family_members
                selected_member = next((m for m in list_family_members() if m["patient_id"] == body["patient_id"]), None)
                if not selected_member:
                    return {"statusCode": 404, "headers": {"Content-Type": "application/json", **CORS_HEADERS},
                            "body": json.dumps({"error": "Selected family member not found."})}
            extracted = _call_bedrock(document_text)
            if selected_member and " ".join((extracted.get("patient") or "").casefold().split()) != " ".join(selected_member["name"].casefold().split()):
                return {"statusCode": 409, "headers": {"Content-Type": "application/json", **CORS_HEADERS},
                        "body": json.dumps({"error": "The prescription patient does not match the selected member. Switch to the correct Care Space or correct the member name."})}
            if not (extracted.get("patient") or "").strip():
                logger.warning("Persistence unavailable patient_identity_missing=true")
                return {
                    "statusCode": 422,
                    "headers": {"Content-Type": "application/json", **CORS_HEADERS},
                    "body": json.dumps({"error": "Patient name is required to save care state", "persisted": False}),
                }

            source_storage = None
            if upload is not None:
                bucket = os.environ["CARE_DOCUMENT_BUCKET"].strip()
                key = "prescriptions/" + uuid.uuid4().hex + ".txt"
                logger.info("Source upload starting")
                try:
                    boto3.client("s3", region_name=BEDROCK_REGION).put_object(
                        Bucket=bucket, Key=key, Body=upload,
                        ContentType="text/plain; charset=utf-8", ServerSideEncryption="AES256")
                except Exception as exc:
                    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "unknown")
                    logger.error("Source upload failed exception_type=%s aws_error_code=%s", type(exc).__name__, code)
                    raise
                source_storage = {"bucket": bucket, "key": key, "filename": source_name}
                logger.info("Source upload succeeded")
            follow_up_state, patient_id = _persist_extracted(extracted, source_name, source_storage, selected_member)

            response_body = dict(extracted)
            response_body["persisted"] = True
            response_body["source_document"] = source_name
            response_body["source_storage"] = source_storage
            response_body["follow_up_state"] = follow_up_state
            response_body["patient_id"]      = patient_id

            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json", **CORS_HEADERS},
                "body": json.dumps(response_body),
            }

        except json.JSONDecodeError as e:
            return {
                "statusCode": 502,
                "headers": {"Content-Type": "application/json", **CORS_HEADERS},
                "body": json.dumps({"error": "Model returned non-JSON response", "detail": str(e)}),
            }
        except Exception as e:
            logger.error("Analyze failed exception_type=%s", type(e).__name__)
            return {
                "statusCode": 500,
                "headers": {"Content-Type": "application/json", **CORS_HEADERS},
                "body": json.dumps({"error": "Analysis could not be saved. Please retry.", "persisted": False}),
            }

    # POST /followup — confirm, edit date, or ignore a follow-up
    if method == "POST" and path == "/followup":
        logger.info("Followup request started")
        stage = "validation"
        try:
            body_raw = event.get("body") or "{}"
            if event.get("isBase64Encoded"):
                body_raw = base64.b64decode(body_raw).decode("utf-8")
            body = json.loads(body_raw)

            patient_id = (body.get("patient_id") or "").strip()
            action     = (body.get("action") or "").strip().lower()

            if not patient_id or action not in ("confirm", "ignore"):
                return {
                    "statusCode": 400,
                    "headers": {"Content-Type": "application/json", **CORS_HEADERS},
                    "body": json.dumps({"error": "patient_id and action (confirm|ignore) required"}),
                }

            if not os.environ.get("CARE_STATE_TABLE", "").strip():
                return {
                    "statusCode": 503,
                    "headers": {"Content-Type": "application/json", **CORS_HEADERS},
                    "body": json.dumps({"error": "Persistence not available: CARE_STATE_TABLE not configured"}),
                }

            # Load current follow-up state from DynamoDB.
            # Prefer the in-memory follow_up_state passed from the UI
            # (via follow_up_state in the request body) so we don't lose
            # the derived structured state when DynamoDB still holds the
            # old raw string (e.g. first analyze before mapping migration).
            from app.storage.dynamodb import get_care_state
            stage = "read"
            care_state = get_care_state(patient_id) or {}
            stored_fu  = care_state.get("follow_up")

            # If the stored value is a plain string (old format) or missing,
            # use the follow_up_state the browser passed back from /analyze.
            ui_fu = body.get("follow_up_state") or {}
            if isinstance(stored_fu, dict) and stored_fu.get("status"):
                current_fu = stored_fu
            elif isinstance(ui_fu, dict) and ui_fu.get("status"):
                current_fu = ui_fu
            elif isinstance(stored_fu, str):
                current_fu = {"source_instruction": stored_fu}
            else:
                current_fu = {}

            if action == "confirm":
                confirmed_date = (body.get("confirmed_date") or "").strip()
                if not confirmed_date:
                    return {
                        "statusCode": 400,
                        "headers": {"Content-Type": "application/json", **CORS_HEADERS},
                        "body": json.dumps({"error": "confirmed_date required for confirm action"}),
                    }
                updated_fu = confirm_followup(current_fu, confirmed_date)
            else:  # ignore
                updated_fu = ignore_followup(current_fu)

            # Persist — do NOT silence this error; a failure must return 500
            # so the frontend never shows false success.
            stage = "write"
            update_followup_state(patient_id, updated_fu)
            logger.info("Followup persistence succeeded")

            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json", **CORS_HEADERS},
                "body": json.dumps({"follow_up": updated_fu, "patient_id": patient_id}),
            }

        except Exception as e:
            code = getattr(e, "response", {}).get("Error", {}).get("Code", "unknown")
            logger.error("Followup failed stage=%s exception_type=%s aws_error_code=%s",
                         stage, type(e).__name__, code)
            return {
                "statusCode": 500,
                "headers": {"Content-Type": "application/json", **CORS_HEADERS},
                "body": json.dumps({"error": "Failed to save follow-up. Please retry."}),
            }

    # 404
    return {
        "statusCode": 404,
        "headers": {"Content-Type": "application/json", **CORS_HEADERS},
        "body": json.dumps({"error": "Not found", "path": path}),
    }
