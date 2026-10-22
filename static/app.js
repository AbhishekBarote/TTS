/**
 * Audio Research Hub - Indian English TTS Dataset
 * Frontend Application Client Logic
 * 
 * Features:
 * - Visual layout matching Audio Research Hub reference design
 * - Approved dates formatted strictly as DDMMYY (e.g. 151026)
 * - Records sorted with current / latest date at the top
 * - Debounced & manual "Check" button for URL duplicate detection
 * - Automatic Title & Channel extraction into Description
 * - Automatic Duration recording (HH:MM:SS or MM:SS)
 * - Right-column duplicate inspection preview matching UI mockup
 * - Recent submissions table widget
 * - Stepper indicator (Check Link -> Enter Details -> Review & Submit)
 * - 4-stage duplicate prevention & historical CSV de-duplication
 * - RFC 4180 CSV export adhering strictly to Section 3 schema
 */

// Application State
const state = {
  activeTab: "dashboard-view",
  currentInspectRecord: null,
  pendingRejectionContext: null, // { recordId, isReviewerAction }
  selectedCsvFile: null,
  selectedCsvContent: "",
  recordsCache: [],
  queueRecords: [],
  approvedRecords: [],
  speakerLedgers: [],
  lastDuplicateRecord: null,
  lastDuplicateCheck: null,
  speakerDurationCheck: null
};

// DOM Content Loaded Initialization
document.addEventListener("DOMContentLoaded", () => {
  initIdentity();
  initNavigation();
  initDashboardForm();
  initRevalidationModal();
  initRejectionModal();
  initCsvPreviewModal();
  initCsvUploadModal();
  initAuditModal();
  initLlmConfigModal();
  initWebSearchDeduplication();

  // Set default research date to today in input
  const today = new Date().toISOString().split("T")[0];
  const dateInput = document.getElementById("input-research-date");
  if (dateInput && !dateInput.value) {
    dateInput.value = today;
  }

  // Initial data load
  refreshAllData();
});

/* ==========================================================================
   1. RESEARCHER IDENTITY & PROFILE BADGE
   ========================================================================== */
function initIdentity() {
  const profileContainer = document.getElementById("profile-container");
  const avatarElem = document.getElementById("profile-avatar");
  const nameDisplay = document.getElementById("profile-name-display");
  const hiddenInput = document.getElementById("input-researched-by");

  // Load saved researcher name or default to "Researcher A"
  const savedName = localStorage.getItem("researched_by") || "Researcher A";
  applyResearcherName(savedName);

  // Allow clicking profile in header to quickly change identity
  if (profileContainer) {
    profileContainer.addEventListener("click", () => {
      const current = localStorage.getItem("researched_by") || "Researcher A";
      const newName = prompt("Enter your Researcher Name or ID:", current);
      if (newName && newName.trim()) {
        applyResearcherName(newName.trim());
        showToast(`Researcher switched to "${newName.trim()}"`, "info");
      }
    });
  }

  function applyResearcherName(name) {
    localStorage.setItem("researched_by", name);
    if (hiddenInput) hiddenInput.value = name;
    if (nameDisplay) nameDisplay.textContent = name;
    
    // Generate 2-letter initials
    if (avatarElem) {
      const parts = name.trim().split(/\s+/);
      let initials = "RA";
      if (parts.length >= 2) {
        initials = (parts[0][0] + parts[1][0]).toUpperCase();
      } else if (parts.length === 1 && parts[0].length >= 2) {
        initials = parts[0].substring(0, 2).toUpperCase();
      }
      avatarElem.textContent = initials;
    }
  }

  // Pre-fetch next speaker ID
  fetchNextSpeakerId();
}

async function fetchNextSpeakerId() {
  try {
    const res = await fetch("/api/next-speaker-id");
    const data = await res.json();
    const spkInput = document.getElementById("input-speaker-id");
    if (spkInput && !spkInput.value) {
      spkInput.value = data.next_speaker_id;
    }
  } catch (err) {
    console.error("Could not fetch next speaker id", err);
  }
}

/* ==========================================================================
   2. SIDEBAR NAVIGATION & VIEWS
   ========================================================================== */
function initNavigation() {
  const navButtons = document.querySelectorAll(".sidebar-nav .nav-item[data-tab]");
  navButtons.forEach(btn => {
    btn.addEventListener("click", () => {
      const targetTab = btn.getAttribute("data-tab");
      if (targetTab) {
        switchTab(targetTab);
      }
    });
  });
}

window.switchTab = function(targetId) {
  state.activeTab = targetId;

  // Update navigation button active state
  document.querySelectorAll(".sidebar-nav .nav-item").forEach(btn => {
    const tab = btn.getAttribute("data-tab");
    btn.classList.toggle("active", tab === targetId);
  });

  // Update view panel active state
  document.querySelectorAll(".view-panel").forEach(panel => {
    panel.classList.toggle("active", panel.id === targetId);
  });

  // Update header title
  const titleElem = document.getElementById("current-view-title");
  if (titleElem) {
    if (targetId === "dashboard-view") titleElem.textContent = "Dashboard Overview";
    else if (targetId === "review-view") titleElem.textContent = "Revalidation Review Queue";
    else if (targetId === "approved-view") titleElem.textContent = "Approved Dataset (DDMMYY)";
    else if (targetId === "history-view") titleElem.textContent = "All Database Records & Audit History";
  }

  // View-specific data loads
  if (targetId === "review-view") loadReviewQueue();
  if (targetId === "approved-view") loadApprovedDataset();
  if (targetId === "history-view") loadAllRecords();
};

window.focusUrlInput = function() {
  switchTab("dashboard-view");
  const input = document.getElementById("input-audio-link");
  if (input) {
    input.focus();
    input.scrollIntoView({ behavior: "smooth", block: "center" });
  }
};

window.scrollToRecent = function() {
  switchTab("dashboard-view");
  const card = document.getElementById("recent-submissions-card");
  if (card) {
    card.scrollIntoView({ behavior: "smooth", block: "start" });
  }
};

window.triggerCsvExport = function() {
  window.location.href = "/api/export/csv";
};

/* ==========================================================================
   3. DATE FORMATTING UTILITIES (DDMMYY)
   ========================================================================== */
/**
 * Formats any date string into strict DDMMYY (e.g. 15-10-2026 -> 151026).
 */
function formatDDMMYY(dateStr) {
  if (!dateStr) return "";
  const s = String(dateStr).trim();
  if (/^\d{6}$/.test(s)) return s;

  // Match YYYY-MM-DD
  const ymd = s.match(/^(\d{4})[-/](\d{1,2})[-/](\d{1,2})/);
  if (ymd) {
    const y = ymd[1].slice(-2);
    const m = ymd[2].padStart(2, "0");
    const d = ymd[3].padStart(2, "0");
    return `${d}${m}${y}`;
  }

  // Match DD-MM-YYYY
  const dmy = s.match(/^(\d{1,2})[-/](\d{1,2})[-/](\d{4})/);
  if (dmy) {
    const d = dmy[1].padStart(2, "0");
    const m = dmy[2].padStart(2, "0");
    const y = dmy[3].slice(-2);
    return `${d}${m}${y}`;
  }

  return s;
}

/**
 * Formats date into readable DD-MM-YYYY for badge or table display.
 */
function formatReadableDate(dateStr) {
  if (!dateStr) return "";
  const s = String(dateStr).trim();
  
  // If DDMMYY (6 digits)
  if (/^\d{6}$/.test(s)) {
    return `${s.slice(0, 2)}-${s.slice(2, 4)}-20${s.slice(4, 6)}`;
  }

  // Match YYYY-MM-DD
  const ymd = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (ymd) {
    return `${ymd[3]}-${ymd[2]}-${ymd[1]}`;
  }

  return s;
}

/* ==========================================================================
   4. DASHBOARD FORM, AUTO-METADATA & UNIFIED SEARCHBAR
   ========================================================================== */
let duplicateCheckTimer = null;

function detectUrlType(url) {
  if (!url) return null;
  const s = url.trim().toLowerCase();
  if (s.includes("youtube.com") || s.includes("youtu.be")) {
    return {
      type: "youtube",
      label: "YouTube Video",
      icon: "fa-brands fa-youtube",
      chipClass: "chip-yt",
      badgeClass: "badge-yt",
      heading: "2. Enter Video & Audio Details"
    };
  }
  if (s.includes("archive.org")) {
    return {
      type: "archive",
      label: "Archive.org",
      icon: "fa-solid fa-building-columns",
      chipClass: "chip-archive",
      badgeClass: "badge-archive",
      heading: "2. Enter Media & Archive Details"
    };
  }
  if (s.includes("vimeo.com")) {
    return {
      type: "vimeo",
      label: "Vimeo Video",
      icon: "fa-brands fa-vimeo-v",
      chipClass: "chip-vimeo",
      badgeClass: "badge-vimeo",
      heading: "2. Enter Video & Audio Details"
    };
  }
  if (s.includes("soundcloud.com")) {
    return {
      type: "soundcloud",
      label: "SoundCloud Audio",
      icon: "fa-brands fa-soundcloud",
      chipClass: "chip-sound",
      badgeClass: "badge-sound",
      heading: "2. Enter Audio Details"
    };
  }
  if (/^https?:\/\//i.test(s) || s.includes(".")) {
    let domain = "Website";
    try {
      const u = new URL(s.startsWith("http") ? s : "https://" + s);
      domain = u.hostname.replace(/^www\./, "");
      if (domain.length > 22) domain = domain.slice(0, 20) + "...";
    } catch (_) {}
    return {
      type: "website",
      label: domain,
      icon: "fa-solid fa-globe",
      chipClass: "chip-web",
      badgeClass: "badge-web",
      heading: "2. Enter Web Content Details"
    };
  }
  return null;
}

function updateUrlDetectionUI(url) {
  const info = detectUrlType(url);
  const chip = document.getElementById("unified-link-detector");
  const chipIcon = document.getElementById("unified-chip-icon");
  const chipText = document.getElementById("unified-link-type-text");
  const inputIcon = document.getElementById("unified-input-icon");
  const clearBtn = document.getElementById("btn-clear-audio-link");
  const heading = document.getElementById("intake-details-heading");
  const globalBadge = document.getElementById("global-link-type-badge");

  if (clearBtn) {
    if (url && url.trim()) {
      clearBtn.classList.remove("hidden");
    } else {
      clearBtn.classList.add("hidden");
    }
  }

  if (info) {
    if (chip) {
      chip.className = `link-type-chip ${info.chipClass}`;
      chip.classList.remove("hidden");
    }
    if (chipIcon) chipIcon.className = info.icon;
    if (chipText) chipText.textContent = info.label;
    if (inputIcon) {
      inputIcon.className = `${info.icon} input-prefix-icon`;
      inputIcon.style.color = (info.type === 'youtube') ? '#ef4444' : '#2563eb';
    }
    if (heading) heading.textContent = info.heading;

    if (globalBadge) {
      globalBadge.className = `header-link-badge ${info.badgeClass}`;
      globalBadge.textContent = info.label;
      globalBadge.classList.remove("hidden");
    }
  } else {
    if (chip) chip.classList.add("hidden");
    if (inputIcon) {
      inputIcon.className = "fa-solid fa-link input-prefix-icon";
      inputIcon.style.color = "#94a3b8";
    }
    if (heading) heading.textContent = "2. Enter Media & Content Details";
    if (globalBadge) globalBadge.classList.add("hidden");
  }
}

function syncSearchInputs(value, sourceId) {
  const ids = ["input-audio-link", "global-search-input", "web-input-url"];
  for (const id of ids) {
    if (id !== sourceId) {
      const el = document.getElementById(id);
      if (el && el.value !== value) el.value = value;
    }
  }
  updateUrlDetectionUI(value);
}

function initDashboardForm() {
  const urlInput = document.getElementById("input-audio-link");
  const checkBtn = document.getElementById("btn-manual-check");
  const form = document.getElementById("submission-form");
  const cancelBtn = document.getElementById("btn-cancel-form");
  const autoSpkBtn = document.getElementById("btn-auto-spk");
  const durationInput = document.getElementById("input-duration");
  const spkInput = document.getElementById("input-speaker-id");
  const viewDupFullBtn = document.getElementById("btn-view-duplicate-full");
  const refreshLedgersBtn = document.getElementById("btn-refresh-speaker-ledgers");

  const clearBtn = document.getElementById("btn-clear-audio-link");
  const pasteBtn = document.getElementById("btn-paste-audio-link");

  const globalInput = document.getElementById("global-search-input");
  const globalCheckBtn = document.getElementById("btn-global-check");

  // Debounced URL check on intake input
  urlInput?.addEventListener("input", () => {
    clearTimeout(duplicateCheckTimer);
    const url = urlInput.value.trim();
    syncSearchInputs(url, "input-audio-link");
    if (!url) {
      resetDuplicateUI();
      return;
    }
    duplicateCheckTimer = setTimeout(() => {
      performDuplicateCheck(url);
    }, 500);
  });

  // Enter key on intake input triggers check without premature form submit
  urlInput?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      const url = urlInput.value.trim();
      if (url) performDuplicateCheck(url);
    }
  });

  // Clear button
  clearBtn?.addEventListener("click", () => {
    if (urlInput) urlInput.value = "";
    syncSearchInputs("", "input-audio-link");
    resetDuplicateUI();
    urlInput?.focus();
  });

  // Paste button
  pasteBtn?.addEventListener("click", async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (text && urlInput) {
        urlInput.value = text.trim();
        syncSearchInputs(text.trim(), "input-audio-link");
        showToast("Pasted URL from clipboard", "info");
        performDuplicateCheck(text.trim());
      }
    } catch (err) {
      showToast("Unable to access clipboard. Please paste manually.", "warning");
      urlInput?.focus();
    }
  });

  // Header Global Searchbar listeners
  globalInput?.addEventListener("input", () => {
    clearTimeout(duplicateCheckTimer);
    const url = globalInput.value.trim();
    syncSearchInputs(url, "global-search-input");
    if (!url) {
      resetDuplicateUI();
      return;
    }
    duplicateCheckTimer = setTimeout(() => {
      performDuplicateCheck(url);
    }, 500);
  });

  globalInput?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      const url = globalInput.value.trim();
      if (url) {
        switchTab("dashboard-view");
        performDuplicateCheck(url);
      }
    }
  });

  globalCheckBtn?.addEventListener("click", () => {
    const url = globalInput?.value.trim() || urlInput?.value.trim();
    if (!url) {
      showToast("Please paste any YouTube link or Website URL to check.", "warning");
      globalInput?.focus();
      return;
    }
    switchTab("dashboard-view");
    performDuplicateCheck(url);
  });

  // Manual Check button click
  checkBtn?.addEventListener("click", () => {
    const url = urlInput?.value.trim();
    if (!url) {
      showToast("Please enter any YouTube link or Website URL to check.", "warning");
      urlInput?.focus();
      return;
    }
    performDuplicateCheck(url);
  });

  // Live speaker duration cap check when speaker ID or duration changes
  const triggerLiveCapCheck = debounce(async () => {
    const spk = spkInput?.value.trim();
    const dur = durationInput?.value.trim();
    if (spk) {
      await checkSpeakerCapLive(spk, dur);
    }
  }, 400);

  spkInput?.addEventListener("input", triggerLiveCapCheck);
  durationInput?.addEventListener("input", triggerLiveCapCheck);

  // Suggest next speaker button
  autoSpkBtn?.addEventListener("click", async () => {
    try {
      const res = await fetch("/api/next-speaker-id");
      const data = await res.json();
      document.getElementById("input-speaker-id").value = data.next_speaker_id;
      showToast(`Assigned Speaker ID: ${data.next_speaker_id}`, "info");
      checkSpeakerCapLive(data.next_speaker_id, durationInput?.value.trim());
    } catch (err) {
      showToast("Could not assign speaker ID", "error");
    }
  });

  // Refresh speaker ledgers
  refreshLedgersBtn?.addEventListener("click", async () => {
    const icon = refreshLedgersBtn.querySelector("i");
    if (icon) icon.classList.add("fa-spin");
    await loadSpeakerLedgers();
    if (icon) icon.classList.remove("fa-spin");
    showToast("Speaker duration ledgers refreshed", "info");
  });

  // Cancel button resets form
  cancelBtn?.addEventListener("click", () => {
    form.reset();
    document.getElementById("input-research-date").value = new Date().toISOString().split("T")[0];
    document.getElementById("input-accent").value = "Indian English";
    syncSearchInputs("", null);
    resetDuplicateUI();
    updateStepper(1);
    fetchNextSpeakerId();
  });

  // Form Submission
  form?.addEventListener("submit", async (e) => {
    e.preventDefault();
    await submitNewRecord();
  });

  // View full duplicate record modal button
  viewDupFullBtn?.addEventListener("click", () => {
    if (state.lastDuplicateRecord && state.lastDuplicateRecord.id) {
      viewAuditTrail(state.lastDuplicateRecord.id);
    }
  });
}

function updateStepper(stepNumber) {
  const s1 = document.getElementById("step-node-1");
  const s2 = document.getElementById("step-node-2");
  const s3 = document.getElementById("step-node-3");

  if (!s1 || !s2 || !s3) return;

  s1.classList.remove("active", "completed");
  s2.classList.remove("active", "completed");
  s3.classList.remove("active", "completed");

  if (stepNumber === 1) {
    s1.classList.add("active");
  } else if (stepNumber === 2) {
    s1.classList.add("completed");
    s2.classList.add("active");
  } else if (stepNumber >= 3) {
    s1.classList.add("completed");
    s2.classList.add("completed");
    s3.classList.add("active");
  }
}

function resetDuplicateUI() {
  const banner = document.getElementById("duplicate-status-box");
  const sideDupCard = document.getElementById("side-duplicate-card");
  const submitBtn = document.getElementById("btn-submit-review");
  const meter = document.getElementById("speaker-duration-meter");

  if (banner) banner.classList.add("hidden");
  if (sideDupCard) sideDupCard.classList.add("hidden");
  if (meter) meter.classList.add("hidden");
  if (submitBtn) submitBtn.disabled = false;
  state.lastDuplicateRecord = null;
  state.lastDuplicateCheck = null;
  state.speakerDurationCheck = null;
  updateUrlDetectionUI(document.getElementById("input-audio-link")?.value || "");
  updateStepper(1);
}

async function checkSpeakerCapLive(speakerId, durationStr) {
  if (!speakerId) return;
  try {
    const res = await fetch(`/api/speakers/${encodeURIComponent(speakerId)}/duration`);
    if (!res.ok) return;
    const data = await res.json();
    renderSpeakerDurationMeter(data, durationStr);
  } catch (err) {
    console.error("Error checking speaker cap", err);
  }
}

function renderSpeakerDurationMeter(capData, candidateDurationStr) {
  const meter = document.getElementById("speaker-duration-meter");
  const textElem = document.getElementById("spk-meter-text");
  const fillElem = document.getElementById("spk-progress-fill");
  const remainingElem = document.getElementById("spk-remaining-text");
  const deltaElem = document.getElementById("spk-delta-text");
  const submitBtn = document.getElementById("btn-submit-review");

  if (!meter || !capData) return;
  meter.classList.remove("hidden");

  state.speakerDurationCheck = capData;
  const currentSec = capData.current_accepted_seconds || 0;
  const maxSec = capData.max_seconds || 12600;
  const pct = Math.min(100, Math.round((currentSec / maxSec) * 100));

  textElem.textContent = `${capData.formatted_current || '0s'} / 3h 30m (${pct}%)`;
  fillElem.style.width = `${pct}%`;

  meter.classList.remove("meter-warning", "meter-danger");
  fillElem.classList.remove("fill-warning", "fill-danger");

  if (pct >= 90) {
    meter.classList.add("meter-danger");
    fillElem.classList.add("fill-danger");
  } else if (pct >= 65) {
    meter.classList.add("meter-warning");
    fillElem.classList.add("fill-warning");
  }

  remainingElem.textContent = `${capData.formatted_remaining || '3h 30m'} remaining`;

  // Check if candidate fits or exceeds
  if (capData.is_valid === false) {
    deltaElem.className = "text-danger font-bold";
    deltaElem.textContent = "⚠️ Exceeds 3.5h limit! Cannot accept video.";
    if (submitBtn) submitBtn.disabled = true;
  } else {
    deltaElem.className = "text-emerald";
    deltaElem.textContent = "✓ Fits within 3.5h limit";
    if (submitBtn && (!state.lastDuplicateCheck || state.lastDuplicateCheck.status !== "DUPLICATE")) {
      submitBtn.disabled = false;
    }
  }
}

async function performDuplicateCheck(url) {
  if (!url || !url.trim()) return;
  url = url.trim();

  const descInput = document.getElementById("input-description");
  const durInput = document.getElementById("input-duration");
  const sourceInput = document.getElementById("input-source");
  const spkInput = document.getElementById("input-speaker-id");
  const banner = document.getElementById("duplicate-status-box");
  const sideDupCard = document.getElementById("side-duplicate-card");
  const submitBtn = document.getElementById("btn-submit-review");

  // Visual cues on checking
  const statusIcon = document.getElementById("banner-status-icon");
  const statusPill = document.getElementById("banner-status-pill");
  const latencyChip = document.getElementById("banner-latency-chip");
  const platformTag = document.getElementById("banner-platform-tag");
  const bannerTitle = document.getElementById("banner-title");
  const bannerDesc = document.getElementById("banner-desc");
  const evidenceContainer = document.getElementById("banner-evidence-chips");

  // Metadata drawer fields
  const metaTitle = document.getElementById("fast-meta-title");
  const metaAuthor = document.getElementById("fast-meta-author");
  const metaDuration = document.getElementById("fast-meta-duration");
  const metaCanonical = document.getElementById("fast-meta-canonical");
  const metaHash = document.getElementById("fast-meta-hash");

  if (banner) {
    banner.classList.remove("hidden");
    banner.className = "status-banner banner-warning";
    if (statusIcon) statusIcon.innerHTML = `<i class="fa-solid fa-circle-notch fa-spin text-primary"></i>`;
    if (statusPill) {
      statusPill.className = "llm-badge-pill badge-potential";
      statusPill.textContent = "CHECKING URL...";
    }
    if (latencyChip) latencyChip.innerHTML = `<i class="fa-solid fa-bolt"></i> Fetching...`;
    if (bannerTitle) bannerTitle.textContent = "Validating URL & Fetching Content...";
    if (bannerDesc) bannerDesc.textContent = "Safe HTTP/HTTPS validation, lightweight stream fetching, and exact hash matching in progress.";
    if (evidenceContainer) evidenceContainer.innerHTML = "";
  }

  try {
    const res = await fetch("/api/check-duplicate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: url,
        description: descInput?.value || "",
        speakers_id: spkInput?.value || "",
        source: sourceInput?.value || "",
        before_processing_duration: durInput?.value || ""
      })
    });

    const data = await res.json();
    state.lastDuplicateCheck = data;

    const meta = data.metadata || {};
    const platform = data.platform || "Web";
    const latency = data.request_latency_ms || 0;
    const version = data.endpoint_version || "fast-pipeline";
    const canonical = data.canonical_url || meta.canonical_url || url;
    const hash = meta.content_hash || "";

    // 1. Auto-fill metadata into form inputs if appropriate
    if (meta.formatted_description && (!descInput.value.trim() || descInput.value.trim().length < 10)) {
      descInput.value = meta.formatted_description;
    }
    if (meta.duration && (!durInput.value.trim() || durInput.value === "18:35")) {
      durInput.value = meta.duration;
    }
    if (platform && sourceInput && (!sourceInput.value.trim() || sourceInput.value === "YouTube" || sourceInput.value === "Web")) {
      sourceInput.value = platform;
    }

    // 2. Render Speaker Duration Meter
    if (data.speaker_duration_check) {
      renderSpeakerDurationMeter(data.speaker_duration_check, durInput.value);
    }

    // 3. Populate Extracted Essential Content Preview Drawer
    if (metaTitle) metaTitle.textContent = meta.title || (data.existing_record ? data.existing_record.title : "—");
    if (metaAuthor) metaAuthor.textContent = meta.author || meta.channel || (data.existing_record ? (data.existing_record.channel_name || data.existing_record.author) : "—");
    if (metaDuration) metaDuration.textContent = meta.duration || (data.existing_record ? data.existing_record.before_processing_duration : "—");
    if (metaCanonical) metaCanonical.textContent = canonical || "—";
    if (metaHash) metaHash.textContent = hash || (data.status === "FETCH_FAILED" ? "Unavailable (Fetch Failed)" : "—");

    // 4. Update Latency Chip & Platform Tag
    if (latencyChip) latencyChip.innerHTML = `<i class="fa-solid fa-bolt"></i> ${latency}ms (${escapeHtml(version)})`;
    if (platformTag) platformTag.textContent = `Platform: ${escapeHtml(platform)}`;

    // 5. Update Evidence Chips
    if (evidenceContainer) {
      if (data.evidence_fields && data.evidence_fields.length > 0) {
        evidenceContainer.innerHTML = data.evidence_fields.map(f => `<span class="evidence-chip">${escapeHtml(f)}</span>`).join("");
      } else {
        evidenceContainer.innerHTML = "";
      }
    }

    // 6. Handle 4 Distinct Pipeline Classification States:
    if (data.status === "FETCH_FAILED") {
      // Inaccessible page or validation failure. NEVER mark as unique! Block submission.
      if (sideDupCard) sideDupCard.classList.add("hidden");
      state.lastDuplicateRecord = null;

      if (banner) {
        banner.className = "status-banner banner-failed";
        if (statusIcon) statusIcon.innerHTML = `<i class="fa-solid fa-triangle-exclamation text-danger"></i>`;
        if (statusPill) {
          statusPill.className = "llm-badge-pill badge-fetch-failed";
          statusPill.textContent = "FETCH FAILED";
        }
        if (bannerTitle) {
          bannerTitle.className = "banner-headline text-danger";
          bannerTitle.textContent = "Page Inaccessible / Verification Failed";
        }
        if (bannerDesc) {
          bannerDesc.textContent = data.reason || "The URL could not be safely reached or fetched. Inaccessible pages cannot be verified or submitted into the dataset.";
        }
      }

      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.title = "Submission blocked: URL content could not be verified or is inaccessible.";
      }
      updateStepper(1);
      showToast(`Fetch Failed: Inaccessible URL blocked from submission.`, "error");

    } else if (data.status === "DUPLICATE") {
      const ex = data.existing_record || {};
      state.lastDuplicateRecord = ex;

      if (sideDupCard) {
        sideDupCard.classList.remove("hidden");
        const descText = (ex.description || "").trim();
        const firstLine = descText.split("\n")[0] || "Recorded Audio";

        const elemTitle = document.getElementById("side-dup-title");
        const elemDur = document.getElementById("side-dup-duration");
        const elemSrc = document.getElementById("side-dup-source");
        const elemSub = document.getElementById("side-dup-submitter");
        const elemSt = document.getElementById("side-dup-status");
        const elemSpk = document.getElementById("side-dup-spk");
        const elemAcc = document.getElementById("side-dup-accent");
        const elemDate = document.getElementById("side-dup-date");
        const elemStTxt = document.getElementById("side-dup-status-text");
        const elemRes = document.getElementById("side-dup-researcher");
        const elemDesc = document.getElementById("side-dup-desc");

        if (elemTitle) elemTitle.textContent = firstLine;
        if (elemDur) elemDur.textContent = ex.before_processing_duration || "—";
        if (elemSrc) elemSrc.textContent = ex.source || platform || "Web";
        if (elemSub) elemSub.textContent = ex.researched_by || "Researcher";
        if (elemSt) elemSt.textContent = ex.status || "DUPLICATE";
        if (elemSpk) elemSpk.textContent = ex.speakers_id || "—";
        if (elemAcc) elemAcc.textContent = ex.accent || "Indian English";
        if (elemDate) elemDate.textContent = formatReadableDate(ex.research_date) || "Today";
        if (elemStTxt) elemStTxt.textContent = ex.status || "DUPLICATE";
        if (elemRes) elemRes.textContent = ex.researched_by || "—";
        if (elemDesc) elemDesc.textContent = descText;
      }

      if (banner) {
        banner.className = "status-banner banner-warning";
        if (statusIcon) statusIcon.innerHTML = `<i class="fa-solid fa-circle-xmark text-danger"></i>`;
        if (statusPill) {
          statusPill.className = "llm-badge-pill badge-duplicate";
          statusPill.textContent = "DUPLICATE DETECTED";
        }
        if (bannerTitle) {
          bannerTitle.className = "banner-headline text-danger";
          bannerTitle.textContent = "Duplicate Content Detected";
        }
        if (bannerDesc) bannerDesc.textContent = data.reason || "This content is already represented in the dataset and cannot be accepted.";
      }

      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.title = "Submission blocked: Duplicate content detected.";
      }
      updateStepper(1);
      showToast(`Duplicate blocked: ${data.reason}`, "error");

    } else if (data.status === "POTENTIAL_DUPLICATE") {
      if (sideDupCard && data.existing_record) {
        sideDupCard.classList.remove("hidden");
        const ex = data.existing_record;
        const elemTitle = document.getElementById("side-dup-title");
        const elemSt = document.getElementById("side-dup-status");
        if (elemTitle) elemTitle.textContent = (ex.title || ex.description || "").split("\n")[0];
        if (elemSt) elemSt.textContent = "Potential Match";
      }

      if (banner) {
        banner.className = "status-banner banner-warning";
        if (statusIcon) statusIcon.innerHTML = `<i class="fa-solid fa-triangle-exclamation text-amber"></i>`;
        if (statusPill) {
          statusPill.className = "llm-badge-pill badge-potential";
          statusPill.textContent = "POTENTIAL DUPLICATE";
        }
        if (bannerTitle) {
          bannerTitle.className = "banner-headline text-amber";
          bannerTitle.textContent = "Potential Duplicate Candidate";
        }
        if (bannerDesc) bannerDesc.textContent = `${data.reason} (Flagged for revalidation review queue upon submission).`;
      }

      const capOk = !data.speaker_duration_check || data.speaker_duration_check.is_valid !== false;
      if (submitBtn) {
        submitBtn.disabled = !capOk;
        submitBtn.title = capOk ? "Submit candidate for Revalidation Review" : "Speaker duration limit exceeded";
      }
      updateStepper(2);
      showToast("Flagged as potential duplicate. Routed for Revalidation Review.", "warning");

    } else {
      // UNIQUE
      if (sideDupCard) sideDupCard.classList.add("hidden");
      state.lastDuplicateRecord = null;

      if (banner) {
        banner.className = "status-banner banner-success";
        if (statusIcon) statusIcon.innerHTML = `<i class="fa-solid fa-circle-check text-emerald"></i>`;
        if (statusPill) {
          statusPill.className = "llm-badge-pill badge-unique";
          statusPill.textContent = "VERIFIED UNIQUE";
        }
        if (bannerTitle) {
          bannerTitle.className = "banner-headline text-emerald";
          bannerTitle.textContent = "Verified Unique Content";
        }
        if (bannerDesc) bannerDesc.textContent = data.reason || "Fast pipeline confirmed this URL and essential content are unique across the indexed database.";
      }

      const capOk = !data.speaker_duration_check || data.speaker_duration_check.is_valid !== false;
      if (submitBtn) {
        submitBtn.disabled = !capOk;
        submitBtn.title = capOk ? "Ready to submit" : "Speaker duration limit exceeded";
      }
      updateStepper(2);
      showToast("Verified Unique! Metadata and essential content extracted.", "success");
    }

  } catch (err) {
    console.error("Duplicate check error:", err);
    if (banner) {
      banner.className = "status-banner banner-failed";
      if (statusIcon) statusIcon.innerHTML = `<i class="fa-solid fa-triangle-exclamation text-danger"></i>`;
      if (statusPill) {
        statusPill.className = "llm-badge-pill badge-fetch-failed";
        statusPill.textContent = "CHECK ERROR";
      }
      if (bannerTitle) bannerTitle.textContent = "Verification Pipeline Error";
      if (bannerDesc) bannerDesc.textContent = "Failed to communicate with verification backend. Please verify your connection.";
    }
    if (submitBtn) submitBtn.disabled = true;
    showToast("Error executing duplicate check: " + err.message, "error");
  }
}

async function submitNewRecord() {
  const url = document.getElementById("input-audio-link").value.trim();
  const researchDate = document.getElementById("input-research-date").value;
  const gender = document.getElementById("select-gender").value;
  const source = document.getElementById("input-source").value.trim();
  const duration = document.getElementById("input-duration").value.trim();
  const description = document.getElementById("input-description").value.trim();
  const accent = document.getElementById("input-accent").value.trim();
  const licensing = document.getElementById("select-licensing").value;
  const speakerId = document.getElementById("input-speaker-id").value.trim();
  const researchedBy = localStorage.getItem("researched_by") || "Researcher A";

  // Form Validation
  if (!url) {
    showToast("Please provide an Audio/Video URL.", "error");
    document.getElementById("input-audio-link").focus();
    return;
  }
  if (!source) {
    showToast("Source is required.", "error");
    document.getElementById("input-source").focus();
    return;
  }
  if (!description) {
    showToast("Description is required.", "error");
    document.getElementById("input-description").focus();
    return;
  }
  if (!duration) {
    showToast("Duration is required.", "error");
    document.getElementById("input-duration").focus();
    return;
  }
  if (!speakerId) {
    showToast("Speakers_ID is required.", "error");
    document.getElementById("input-speaker-id").focus();
    return;
  }

  // Pre-submission Speaker Duration Cap Verification (Section 9)
  if (state.speakerDurationCheck && state.speakerDurationCheck.is_valid === false) {
    showToast(`Cannot submit: ${state.speakerDurationCheck.message}`, "error");
    return;
  }

  // Block confirmed DUPLICATE submission
  if (state.lastDuplicateCheck && state.lastDuplicateCheck.status === "DUPLICATE") {
    showToast("Cannot submit duplicate recording. Duplicate recordings must be excluded.", "error");
    return;
  }

  // Block FETCH_FAILED submission - never allow dead/inaccessible pages into dataset
  if (state.lastDuplicateCheck && state.lastDuplicateCheck.status === "FETCH_FAILED") {
    showToast("Cannot submit URL: Page content could not be verified or is inaccessible.", "error");
    return;
  }

  updateStepper(3);

  const dupCheck = state.lastDuplicateCheck || {};
  const payload = {
    research_date: researchDate,
    source: source,
    audio_link: url,
    description: description,
    gender: gender,
    before_processing_duration: duration,
    accent: accent,
    licensing: licensing,
    licensing_evidence: "",
    sanskrit_evidence: "",
    speakers_id: speakerId,
    researched_by: researchedBy,
    action: "SEND_FOR_REVIEW",
    duplicate_status: dupCheck.status || "UNIQUE",
    matched_video_id: dupCheck.matched_video_id || null,
    decision_reason: dupCheck.reason || null,
    evidence_fields: dupCheck.evidence_fields || []
  };

  try {
    const res = await fetch("/api/records", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });

    if (res.status === 409) {
      const err = await res.json();
      showToast(err.detail?.message || "Duplicate recording detected!", "error");
      performDuplicateCheck(url);
      return;
    }

    if (!res.ok) {
      const err = await res.json();
      showToast(err.detail || "Submission failed", "error");
      return;
    }

    const created = await res.json();
    showToast(`Record #${created.id} submitted for Revalidation!`, "success");

    // Reset Form
    document.getElementById("submission-form").reset();
    document.getElementById("input-research-date").value = new Date().toISOString().split("T")[0];
    document.getElementById("input-accent").value = "Indian English";
    resetDuplicateUI();
    fetchNextSpeakerId();

    // Refresh Dashboard metrics, speaker ledgers & tables
    refreshAllData();

  } catch (err) {
    console.error("Submission failed", err);
    showToast("Network error submitting record", "error");
  }
}

/* ==========================================================================
   5. REVALIDATION QUEUE VIEW
   ========================================================================== */
async function loadReviewQueue() {
  const container = document.getElementById("queue-container");
  const emptyState = document.getElementById("queue-empty-state");
  const countBadge = document.getElementById("queue-badge-count");

  try {
    const res = await fetch("/api/records?status=PENDING_REVALIDATION");
    const records = await res.json();
    state.queueRecords = records;

    if (countBadge) countBadge.textContent = `${records.length} Waiting`;

    if (records.length === 0) {
      container.innerHTML = "";
      emptyState.classList.remove("hidden");
      return;
    }

    emptyState.classList.add("hidden");
    container.innerHTML = records.map(rec => createQueueCardHtml(rec)).join("");

    container.querySelectorAll(".btn-inspect-rec").forEach(btn => {
      btn.addEventListener("click", () => {
        const id = parseInt(btn.dataset.id);
        openRevalidateModal(id);
      });
    });

  } catch (err) {
    console.error("Queue load error", err);
    showToast("Error loading review queue", "error");
  }
}

function createQueueCardHtml(rec) {
  let statusBadge = "";
  if (rec.duplicate_status === "POTENTIAL_DUPLICATE") {
    statusBadge = `<span class="llm-badge-pill badge-potential mb-1"><i class="fa-solid fa-triangle-exclamation"></i> Potential Duplicate</span>`;
  } else if (rec.duplicate_status === "DUPLICATE") {
    statusBadge = `<span class="llm-badge-pill badge-duplicate mb-1"><i class="fa-solid fa-circle-xmark"></i> Duplicate Match</span>`;
  } else {
    statusBadge = `<span class="llm-badge-pill badge-unique mb-1">Pre-filter Unique</span>`;
  }

  const reasonHtml = rec.decision_reason
    ? `<div class="text-xs text-muted mb-2 font-mono" style="background: rgba(0,0,0,0.03); padding: 0.3rem 0.5rem; border-radius: 4px;"><i class="fa-solid fa-brain text-primary"></i> ${escapeHtml(rec.decision_reason)}</div>`
    : "";

  return `
    <div class="card p-3" style="display: flex; flex-direction: column; justify-content: space-between; border-left: 4px solid var(--amber);">
      <div>
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.35rem;">
          <span class="badge badge-amber">${escapeHtml(rec.source)}</span>
          <span class="font-mono font-bold">${escapeHtml(rec.speakers_id)}</span>
        </div>
        <div class="mb-2">${statusBadge}</div>
        <div style="font-family: var(--font-mono); font-size: 0.85rem; white-space: pre-wrap; margin-bottom: 0.5rem;">${escapeHtml(rec.description)}</div>
        ${reasonHtml}
      </div>
      <div>
        <div class="text-xs text-muted" style="display: flex; justify-content: space-between; margin-bottom: 0.75rem;">
          <span><i class="fa-regular fa-clock"></i> ${escapeHtml(rec.before_processing_duration)} &bull; ${escapeHtml(rec.gender)}</span>
          <span>By: <strong>${escapeHtml(rec.researched_by)}</strong></span>
        </div>
        <button class="btn btn-primary btn-sm w-100 btn-inspect-rec" data-id="${rec.id}">
          <i class="fa-solid fa-clipboard-check"></i> Inspect & Revalidate
        </button>
      </div>
    </div>
  `;
}

document.getElementById("queue-search")?.addEventListener("input", (e) => {
  const query = e.target.value.toLowerCase().trim();
  document.querySelectorAll("#queue-container > div").forEach(card => {
    const text = card.textContent.toLowerCase();
    card.style.display = text.includes(query) ? "flex" : "none";
  });
});

document.getElementById("btn-refresh-queue")?.addEventListener("click", loadReviewQueue);

/* ==========================================================================
   6. REVALIDATION INSPECTION & APPROVAL MODAL (WITH CONTENT EDITING)
   ========================================================================== */
function initRevalidationModal() {
  const modal = document.getElementById("modal-revalidate");
  const closeBtn = document.getElementById("modal-rev-close");
  const approveBtn = document.getElementById("btn-modal-approve");
  const saveDraftBtn = document.getElementById("btn-modal-save-draft");
  const rejectBtn = document.getElementById("btn-modal-reject");
  const editUrlInput = document.getElementById("modal-edit-url");
  const urlOpenBtn = document.getElementById("modal-rev-url-open");

  closeBtn?.addEventListener("click", () => modal.classList.add("hidden"));
  modal?.addEventListener("click", (e) => {
    if (e.target === modal) modal.classList.add("hidden");
  });

  // Sync open link href when URL input changes
  editUrlInput?.addEventListener("input", () => {
    const u = editUrlInput.value.trim();
    if (urlOpenBtn) urlOpenBtn.href = u || "#";
  });

  // Save changes without changing status
  saveDraftBtn?.addEventListener("click", async () => {
    await performRevalidationAction("SAVE_ONLY");
  });

  // Save changes & approve
  approveBtn?.addEventListener("click", async () => {
    await performRevalidationAction("APPROVE");
  });

  // Reject
  rejectBtn?.addEventListener("click", () => {
    if (!state.currentInspectRecord) return;
    state.pendingRejectionContext = {
      recordId: state.currentInspectRecord.id,
      isReviewerAction: true
    };
    modal.classList.add("hidden");
    openRejectionModal();
  });
}

function safeSetField(id, value) {
  const el = document.getElementById(id);
  if (!el) return;
  const val = value !== undefined && value !== null ? value : "";
  if (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT") {
    el.value = val;
  } else {
    el.textContent = val;
  }
}

async function openRevalidateModal(recordId) {
  try {
    const res = await fetch(`/api/records/${recordId}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}: Record not found`);
    const rec = await res.json();
    state.currentInspectRecord = rec;

    const modal = document.getElementById("modal-revalidate");
    if (!modal) {
      console.error("modal-revalidate element not found in DOM");
      return;
    }

    const titleText = `Record #${rec.id}: ${rec.speakers_id || rec.speaker_channel_name || 'Candidate'} (${rec.source || 'Audio'})`;
    safeSetField("modal-rev-title", titleText);
    safeSetField("modal-rev-status-badge", rec.status || "PENDING_REVALIDATION");

    // Populate editable candidate fields (and fallback elements)
    safeSetField("modal-edit-url", rec.audio_link);
    safeSetField("modal-rev-url-text", rec.audio_link);
    const urlOpenBtn = document.getElementById("modal-rev-url-open") || document.getElementById("modal-rev-url");
    if (urlOpenBtn) urlOpenBtn.href = rec.audio_link || "#";

    safeSetField("modal-edit-channel", rec.speaker_channel_name);
    safeSetField("modal-edit-spk", rec.speakers_id);
    safeSetField("modal-rev-spk", rec.speakers_id);

    safeSetField("modal-edit-duration", rec.before_processing_duration);
    safeSetField("modal-rev-duration", rec.before_processing_duration);

    safeSetField("modal-edit-source", rec.source || "YouTube");
    safeSetField("modal-rev-source", rec.source || "YouTube");

    safeSetField("modal-edit-gender", rec.gender || "Unspecified");
    safeSetField("modal-rev-gender", rec.gender || "Unspecified");

    safeSetField("modal-edit-accent", rec.accent || "Indian English");
    safeSetField("modal-rev-accent", rec.accent || "Indian English");

    safeSetField("modal-edit-licensing", rec.licensing || "Standard YouTube");
    safeSetField("modal-edit-sanskrit", rec.sanskrit_evidence);

    safeSetField("modal-edit-description", rec.description);
    safeSetField("modal-rev-description", rec.description);

    safeSetField("modal-rev-researcher", rec.researched_by || "Researcher");
    safeSetField("modal-rev-date", rec.research_date);

    // Reviewer inputs
    const reviewerInput = document.getElementById("modal-input-reviewer");
    if (reviewerInput) {
      reviewerInput.value = localStorage.getItem("revalidated_by") || localStorage.getItem("researched_by") || "Reviewer";
    }
    const notesInput = document.getElementById("modal-input-notes");
    if (notesInput) notesInput.value = "";

    // Reset checklist items
    for (let i = 1; i <= 8; i++) {
      const chk = document.getElementById(`chk-${i}`);
      if (chk) chk.checked = false;
    }

    modal.classList.remove("hidden");
  } catch (err) {
    console.error("Failed to load candidate record details:", err);
    showToast(`Failed to load candidate record details: ${err.message}`, "error");
  }
}

async function performRevalidationAction(action, rejectionReason = null) {
  if (!state.currentInspectRecord) return;
  const recordId = state.currentInspectRecord.id;
  const reviewer = document.getElementById("modal-input-reviewer")?.value.trim() || "";
  const notes = document.getElementById("modal-input-notes")?.value.trim() || "";

  if (!reviewer) {
    showToast("Please enter Reviewer Name (Revalidated_By).", "error");
    document.getElementById("modal-input-reviewer")?.focus();
    return;
  }

  localStorage.setItem("revalidated_by", reviewer);

  // Collect candidate content modifications made by the revalidator
  const payload = {
    action: action,
    reviewer: reviewer,
    rejection_reason: rejectionReason,
    notes: notes,
    audio_link: document.getElementById("modal-edit-url")?.value.trim(),
    speaker_channel_name: document.getElementById("modal-edit-channel")?.value.trim(),
    speakers_id: document.getElementById("modal-edit-spk")?.value.trim(),
    before_processing_duration: document.getElementById("modal-edit-duration")?.value.trim(),
    source: document.getElementById("modal-edit-source")?.value,
    gender: document.getElementById("modal-edit-gender")?.value,
    accent: document.getElementById("modal-edit-accent")?.value.trim(),
    licensing: document.getElementById("modal-edit-licensing")?.value.trim(),
    sanskrit_evidence: document.getElementById("modal-edit-sanskrit")?.value.trim(),
    description: document.getElementById("modal-edit-description")?.value.trim()
  };

  try {
    const res = await fetch(`/api/records/${recordId}/revalidate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });

    if (!res.ok) {
      const err = await res.json();
      showToast(err.detail || "Action failed", "error");
      return;
    }

    const updated = await res.json();
    state.currentInspectRecord = updated;

    if (action === "SAVE_ONLY") {
      showToast(`Record #${updated.id} content changes saved!`, "success");
    } else {
      document.getElementById("modal-revalidate")?.classList.add("hidden");
      if (action === "APPROVE") {
        showToast(`Record #${updated.id} content updated & APPROVED for final dataset!`, "success");
      } else {
        showToast(`Record #${updated.id} REJECTED at revalidation.`, "warning");
      }
    }

    refreshAllData();
    loadReviewQueue();
  } catch (err) {
    console.error("Revalidation error", err);
    showToast("Error processing revalidation action", "error");
  }
}

/* ==========================================================================
   7. REJECTION REASON PROMPT MODAL
   ========================================================================== */
function initRejectionModal() {
  const modal = document.getElementById("modal-rejection");
  const closeBtn = document.getElementById("modal-rej-close");
  const cancelBtn = document.getElementById("btn-cancel-rejection");
  const confirmBtn = document.getElementById("btn-confirm-rejection");
  const quickSelect = document.getElementById("rej-quick-reason");
  const reasonText = document.getElementById("rej-reason-text");

  closeBtn?.addEventListener("click", () => modal.classList.add("hidden"));
  cancelBtn?.addEventListener("click", () => modal.classList.add("hidden"));

  quickSelect?.addEventListener("change", (e) => {
    if (e.target.value) {
      reasonText.value = e.target.value;
    }
  });

  confirmBtn?.addEventListener("click", async () => {
    const reason = reasonText.value.trim();
    if (!reason) {
      showToast("Rejection reason is mandatory.", "error");
      reasonText.focus();
      return;
    }

    modal.classList.add("hidden");

    if (state.pendingRejectionContext?.isReviewerAction) {
      await performRevalidationAction("REJECT", reason);
    }
  });
}

function openRejectionModal() {
  const modal = document.getElementById("modal-rejection");
  document.getElementById("rej-reason-text").value = "";
  document.getElementById("rej-quick-reason").value = "";
  modal.classList.remove("hidden");
}

/* ==========================================================================
   8. APPROVED DATASET VIEW (STRICT DDMMYY & CURRENT DATE AT TOP)
   ========================================================================== */
async function loadApprovedDataset() {
  const tbody = document.getElementById("approved-tbody");
  const search = document.getElementById("approved-search")?.value || "";
  const sourceFilter = document.getElementById("filter-source")?.value || "";

  try {
    let url = `/api/records?status=APPROVED`;
    if (sourceFilter.trim()) url += `&source=${encodeURIComponent(sourceFilter.trim())}`;
    if (search.trim()) url += `&search=${encodeURIComponent(search.trim())}`;

    const res = await fetch(url);
    const records = await res.json();

    // Sort by research_date DESC (current date at top), then id DESC
    records.sort((a, b) => {
      const dateA = a.research_date || "";
      const dateB = b.research_date || "";
      if (dateA !== dateB) {
        return dateB.localeCompare(dateA);
      }
      return (b.id || 0) - (a.id || 0);
    });

    state.approvedRecords = records;

function formatDateSlash(dateStr) {
  if (!dateStr) return "";
  const s = String(dateStr).trim();
  if (s.includes("/")) {
    const parts = s.split("/");
    if (parts.length === 3) {
      if (parts[0].length === 4) return `${parts[2].padStart(2, '0')}/${parts[1].padStart(2, '0')}/${parts[0]}`;
      if (parts[2].length === 4) return `${parts[0].padStart(2, '0')}/${parts[1].padStart(2, '0')}/${parts[2]}`;
      return `${parts[0].padStart(2, '0')}/${parts[1].padStart(2, '0')}/20${parts[2]}`;
    }
  }
  if (s.includes("-")) {
    const parts = s.split("-");
    if (parts.length === 3) {
      if (parts[0].length === 4) return `${parts[2].padStart(2, '0')}/${parts[1].padStart(2, '0')}/${parts[0]}`;
      if (parts[2].length === 4) return `${parts[0].padStart(2, '0')}/${parts[1].padStart(2, '0')}/${parts[2]}`;
      return `${parts[0].padStart(2, '0')}/${parts[1].padStart(2, '0')}/20${parts[2]}`;
    }
  }
  if (s.length === 6 && /^\d+$/.test(s)) {
    return `${s.substring(0, 2)}/${s.substring(2, 4)}/20${s.substring(4)}`;
  }
  return s;
}

    if (records.length === 0) {
      tbody.innerHTML = `<tr><td colspan="16" class="text-center text-muted" style="padding: 2.5rem;">No approved records found matching your filters.</td></tr>`;
      return;
    }

    tbody.innerHTML = records.map(r => {
      const dateSlash = formatDateSlash(r.research_date);
      const spkChannel = r.speaker_channel_name || r.channel_name || r.speakers_id;
      const assignedTo = r.assigned_to || r.revalidated_by || r.researched_by || '—';

      return `
        <tr>
          <td class="font-mono text-sm font-bold text-emerald" title="Original: ${escapeHtml(r.research_date)}">${escapeHtml(dateSlash)}</td>
          <td><span class="tag-pill">${escapeHtml(r.source)}</span></td>
          <td>
            <a href="${escapeHtml(r.audio_link)}" target="_blank" rel="noopener noreferrer" class="table-link" title="${escapeHtml(r.audio_link)}">
              Link <i class="fa-solid fa-arrow-up-right-from-square"></i>
            </a>
          </td>
          <td><span class="badge badge-indigo">${escapeHtml(r.text_link || 'ASR')}</span></td>
          <td style="max-width: 240px;">
            <div style="font-family: var(--font-mono); font-size: 0.825rem; white-space: pre-wrap;">${escapeHtml(r.description)}</div>
          </td>
          <td><span class="font-mono text-xs">${escapeHtml(r.dataset_name || '—')}</span></td>
          <td>${escapeHtml(r.gender)}</td>
          <td class="font-mono text-sm">${escapeHtml(r.before_processing_duration)}</td>
          <td class="font-mono font-bold">${escapeHtml(r.speakers_id)}</td>
          <td>${escapeHtml(spkChannel)}</td>
          <td>${escapeHtml(r.accent || 'Normal Accent')}</td>
          <td><span class="badge badge-indigo">${escapeHtml(r.licensing || 'CC')}</span></td>
          <td>${escapeHtml(r.researched_by)}</td>
          <td class="text-emerald font-bold">${escapeHtml(assignedTo)}</td>
          <td><span class="badge ${r.handover === 'Yes' ? 'badge-emerald' : 'badge-slate'}">${escapeHtml(r.handover || 'No')}</span></td>
          <td>
            <button class="btn btn-outline btn-sm" onclick="viewAuditTrail(${r.id})" title="View Lifecycle History">
              <i class="fa-solid fa-clock-rotate-left"></i>
            </button>
          </td>
        </tr>
      `;
    }).join("");

  } catch (err) {
    console.error("Approved dataset load error", err);
    showToast("Error loading approved dataset", "error");
  }
}

document.getElementById("approved-search")?.addEventListener("input", debounce(loadApprovedDataset, 300));
document.getElementById("filter-source")?.addEventListener("input", debounce(loadApprovedDataset, 300));

/* ==========================================================================
   9. CSV PREVIEW MODAL
   ========================================================================== */
function initCsvPreviewModal() {
  const previewBtn = document.getElementById("btn-preview-csv");
  const modal = document.getElementById("modal-csv-preview");
  const closeBtn = document.getElementById("modal-csv-close");
  const closeBtn2 = document.getElementById("btn-close-csv-preview");

  previewBtn?.addEventListener("click", async () => {
    try {
      const res = await fetch("/api/export/preview");
      const data = await res.json();

      document.getElementById("preview-record-count").textContent = `${data.count} Approved Records`;
      document.getElementById("csv-preview-content").textContent = data.csv_text;
      modal.classList.remove("hidden");
    } catch (err) {
      showToast("Failed to generate CSV preview", "error");
    }
  });

  closeBtn?.addEventListener("click", () => modal.classList.add("hidden"));
  closeBtn2?.addEventListener("click", () => modal.classList.add("hidden"));
}

/* ==========================================================================
   10. CSV IMPORT & HISTORICAL DE-DUPLICATION MODAL
   ========================================================================== */
function initCsvUploadModal() {
  const modal = document.getElementById("modal-upload-csv");
  const openBtnTab = document.getElementById("btn-tab-open-import");
  const closeBtn = document.getElementById("modal-upload-close");
  const cancelBtn = document.getElementById("btn-cancel-import");
  const browseBtn = document.getElementById("btn-browse-file");
  const fileInput = document.getElementById("csv-file-input");
  const dropZone = document.getElementById("csv-drop-zone");
  const startImportBtn = document.getElementById("btn-start-import");
  const resultsCard = document.getElementById("csv-import-results");

  window.openImportModal = function() {
    resultsCard.classList.add("hidden");
    document.getElementById("csv-selected-info").classList.add("hidden");
    startImportBtn.disabled = true;
    state.selectedCsvContent = "";
    state.selectedCsvFile = null;
    modal.classList.remove("hidden");
  };

  openBtnTab?.addEventListener("click", window.openImportModal);
  closeBtn?.addEventListener("click", () => modal.classList.add("hidden"));
  cancelBtn?.addEventListener("click", () => modal.classList.add("hidden"));

  browseBtn?.addEventListener("click", () => fileInput.click());
  dropZone?.addEventListener("click", (e) => {
    if (e.target !== browseBtn) fileInput.click();
  });

  // Drag and drop handlers
  ["dragenter", "dragover"].forEach(name => {
    dropZone?.addEventListener(name, (e) => {
      e.preventDefault();
      dropZone.classList.add("drag-active");
    });
  });

  ["dragleave", "drop"].forEach(name => {
    dropZone?.addEventListener(name, (e) => {
      e.preventDefault();
      dropZone.classList.remove("drag-active");
    });
  });

  dropZone?.addEventListener("drop", (e) => {
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      handleCsvFile(e.dataTransfer.files[0]);
    }
  });

  fileInput?.addEventListener("change", (e) => {
    if (e.target.files && e.target.files.length > 0) {
      handleCsvFile(e.target.files[0]);
    }
  });

  function handleCsvFile(file) {
    if (!file.name.toLowerCase().endsWith(".csv")) {
      showToast("Please choose a valid .csv file.", "error");
      return;
    }

    state.selectedCsvFile = file;
    const reader = new FileReader();

    reader.onload = (event) => {
      state.selectedCsvContent = event.target.result;
      const lines = state.selectedCsvContent.trim().split("\n");
      const rowCount = Math.max(0, lines.length - 1);

      document.getElementById("csv-file-name").textContent = file.name;
      document.getElementById("csv-file-size").textContent = `${(file.size / 1024).toFixed(1)} KB • ${rowCount} data rows`;
      document.getElementById("csv-selected-info").classList.remove("hidden");
      startImportBtn.disabled = false;
      showToast(`Ready to import "${file.name}" (${rowCount} rows)`, "info");
    };

    reader.readAsText(file, "UTF-8");
  }

  startImportBtn?.addEventListener("click", async () => {
    if (!state.selectedCsvContent) {
      showToast("No CSV file selected.", "error");
      return;
    }

    startImportBtn.disabled = true;
    startImportBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Processing & Matching Duplicates...';

    const targetStatus = document.getElementById("import-target-status").value;

    try {
      const res = await fetch("/api/upload-csv", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          csv_text: state.selectedCsvContent,
          target_status: targetStatus
        })
      });

      if (!res.ok) {
        const err = await res.json();
        showToast(err.detail || "Import failed", "error");
        startImportBtn.disabled = false;
        startImportBtn.innerHTML = '<i class="fa-solid fa-play"></i> Start Import & Match Duplicates';
        return;
      }

      const report = await res.json();

      document.getElementById("res-total-rows").textContent = report.total_rows;
      document.getElementById("res-imported-count").textContent = report.imported;
      document.getElementById("res-skipped-count").textContent = report.skipped_duplicates;
      resultsCard.classList.remove("hidden");

      startImportBtn.innerHTML = '<i class="fa-solid fa-check"></i> Import Complete';

      showToast(`Import finished! ${report.imported} new records ingested, ${report.skipped_duplicates} duplicates skipped.`, "success");

      refreshAllData();
    } catch (err) {
      console.error("CSV import error", err);
      showToast("Network error uploading CSV.", "error");
      startImportBtn.disabled = false;
      startImportBtn.innerHTML = '<i class="fa-solid fa-play"></i> Start Import & Match Duplicates';
    }
  });
}

/* ==========================================================================
   11. ALL RECORDS & AUDIT HISTORY
   ========================================================================== */
async function loadAllRecords(statusFilter = "ALL") {
  const tbody = document.getElementById("all-records-tbody");

  try {
    const res = await fetch(`/api/records?status=${encodeURIComponent(statusFilter)}`);
    const records = await res.json();

    // Sort by research_date DESC, then id DESC
    records.sort((a, b) => {
      const dateA = a.research_date || "";
      const dateB = b.research_date || "";
      if (dateA !== dateB) return dateB.localeCompare(dateA);
      return (b.id || 0) - (a.id || 0);
    });

    if (records.length === 0) {
      tbody.innerHTML = `<tr><td colspan="10" class="text-center text-muted" style="padding: 2.5rem;">No records found.</td></tr>`;
      return;
    }

    tbody.innerHTML = records.map(r => {
      const dateDdmmyy = r.research_date_ddmmyy || formatDDMMYY(r.research_date);

      return `
        <tr>
          <td class="font-mono text-muted">#${r.id}</td>
          <td><span class="badge ${getStatusBadgeClass(r.status)}">${r.status}</span></td>
          <td class="font-mono text-sm">${escapeHtml(dateDdmmyy)}</td>
          <td>${escapeHtml(r.source)}</td>
          <td class="font-mono font-bold">${escapeHtml(r.speakers_id)}</td>
          <td class="font-mono">${escapeHtml(r.before_processing_duration)}</td>
          <td style="max-width: 280px;">
            <div style="font-family: var(--font-mono); font-size: 0.85rem; white-space: pre-wrap;">${escapeHtml(r.description)}</div>
            ${r.rejection_reason ? `<div class="text-xs text-danger mt-1"><strong>Reason:</strong> ${escapeHtml(r.rejection_reason)}</div>` : ''}
          </td>
          <td>${escapeHtml(r.researched_by)}</td>
          <td>${escapeHtml(r.revalidated_by || '—')}</td>
          <td>
            <button class="btn btn-outline btn-sm" onclick="viewAuditTrail(${r.id})">
              <i class="fa-solid fa-list-timeline"></i> Audit
            </button>
          </td>
        </tr>
      `;
    }).join("");

  } catch (err) {
    console.error("All records load error", err);
  }
}

function getStatusBadgeClass(status) {
  if (status === "APPROVED") return "badge-emerald";
  if (status === "PENDING_REVALIDATION") return "badge-amber";
  if (status.includes("REJECTED")) return "badge-rose";
  return "badge-blue";
}

document.querySelectorAll(".status-tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".status-tab-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    loadAllRecords(btn.dataset.status);
  });
});

function initAuditModal() {
  const modal = document.getElementById("modal-audit-trail");
  const closeBtn = document.getElementById("modal-audit-close");
  const closeBtn2 = document.getElementById("btn-close-audit");

  closeBtn?.addEventListener("click", () => modal.classList.add("hidden"));
  closeBtn2?.addEventListener("click", () => modal.classList.add("hidden"));
  modal?.addEventListener("click", (e) => {
    if (e.target === modal) modal.classList.add("hidden");
  });
}

window.viewAuditTrail = async function(recordId) {
  try {
    const res = await fetch(`/api/records/${recordId}`);
    if (!res.ok) throw new Error("Record not found");
    const rec = await res.json();

    const modal = document.getElementById("modal-audit-trail");
    const header = document.getElementById("audit-record-header");
    const timeline = document.getElementById("audit-timeline-container");

    header.innerHTML = `
      <div class="card p-3" style="background: #f8fafc; border: 1px solid var(--border-color);">
        <div style="display: flex; justify-content: space-between; align-items: center;">
          <h4 style="font-size: 0.95rem; margin: 0;">Record #${rec.id} &bull; ${escapeHtml(rec.speakers_id)}</h4>
          <span class="badge ${getStatusBadgeClass(rec.status)}">${rec.status}</span>
        </div>
        <div class="text-xs text-muted mt-1" style="word-break: break-all;">${escapeHtml(rec.audio_link)}</div>
        <div class="text-xs text-primary mt-1 font-mono" style="white-space: pre-wrap;">${escapeHtml(rec.description)}</div>
      </div>
    `;

    const logs = rec.audit_history || [];
    if (logs.length === 0) {
      timeline.innerHTML = `<p class="text-muted text-sm">No recorded status changes.</p>`;
    } else {
      timeline.innerHTML = logs.map(l => `
        <div class="timeline-item">
          <div class="timeline-header">
            <span class="timeline-actor"><i class="fa-solid fa-user"></i> ${escapeHtml(l.actor)}</span>
            <span class="font-mono text-xs text-muted">${formatDate(l.timestamp)}</span>
          </div>
          <div class="timeline-action">
            Action: <strong>${escapeHtml(l.action)}</strong>
            ${l.to_status ? ` &rarr; <span class="badge ${getStatusBadgeClass(l.to_status)}">${l.to_status}</span>` : ''}
          </div>
          ${l.reason ? `<div class="timeline-comment text-danger"><strong>Reason:</strong> ${escapeHtml(l.reason)}</div>` : ''}
          ${l.comments ? `<div class="timeline-comment">${escapeHtml(l.comments)}</div>` : ''}
        </div>
      `).join("");
    }

    modal.classList.remove("hidden");
  } catch (err) {
    showToast("Error fetching audit trail", "error");
  }
};

/* ==========================================================================
   12. METRICS & RECENT SUBMISSIONS SYNC
   ========================================================================== */
async function refreshAllData() {
  try {
    const res = await fetch("/api/stats");
    const stats = await res.json();

    // 1. Four Top Metric Cards
    const mySubmissionsElem = document.getElementById("dash-my-submissions-count");
    const mySubtextElem = document.getElementById("dash-my-subtext");
    const pendingElem = document.getElementById("dash-pending-count");
    const approvedElem = document.getElementById("dash-approved-count");
    const totalHoursElem = document.getElementById("dash-total-hours");
    const speakersSubtextElem = document.getElementById("dash-speakers-subtext");

    const researcherName = localStorage.getItem("researched_by") || "Researcher A";

    // Set metric numbers
    if (mySubmissionsElem) {
      mySubmissionsElem.textContent = stats.total_records || 0;
    }
    if (mySubtextElem) {
      mySubtextElem.textContent = `${stats.pending_revalidation || 0} pending • ${stats.approved || 0} approved • ${stats.rejected_by_researcher + stats.rejected_at_revalidation} rejected`;
    }
    if (pendingElem) {
      pendingElem.textContent = stats.pending_revalidation || 0;
    }
    if (approvedElem) {
      approvedElem.textContent = stats.approved || 0;
    }
    if (totalHoursElem) {
      totalHoursElem.textContent = stats.approved_hours_decimal !== undefined ? stats.approved_hours_decimal : "0.0";
    }
    if (speakersSubtextElem) {
      speakersSubtextElem.textContent = `Across ${stats.approved_speakers || 0} speakers`;
    }

    // 2. Sidebar Badges
    const sidePending = document.getElementById("side-pending-badge");
    const sideApproved = document.getElementById("side-approved-badge");
    if (sidePending) sidePending.textContent = stats.pending_revalidation || 0;
    if (sideApproved) sideApproved.textContent = stats.approved || 0;

    // 3. Status Tabs Counts
    const cAll = document.getElementById("count-all");
    const cApp = document.getElementById("count-approved");
    const cPen = document.getElementById("count-pending");
    const cRejRes = document.getElementById("count-rej-res");
    const cRejRev = document.getElementById("count-rej-rev");
    const cDraft = document.getElementById("count-draft");

    if (cAll) cAll.textContent = stats.total_records || 0;
    if (cApp) cApp.textContent = stats.approved || 0;
    if (cPen) cPen.textContent = stats.pending_revalidation || 0;
    if (cRejRes) cRejRes.textContent = stats.rejected_by_researcher || 0;
    if (cRejRev) cRejRev.textContent = stats.rejected_at_revalidation || 0;
    if (cDraft) cDraft.textContent = stats.pending_research || 0;

    // 4. Populate Right-Column "Recent Submissions (My Entries)" Widget
    const recentTbody = document.getElementById("recent-submissions-tbody");
    if (recentTbody && stats.recent_submissions) {
      if (stats.recent_submissions.length === 0) {
        recentTbody.innerHTML = `<tr><td colspan="3" class="text-center text-muted" style="padding: 1.5rem;">No submissions yet.</td></tr>`;
      } else {
        recentTbody.innerHTML = stats.recent_submissions.map(row => {
          let badgeClass = "badge-amber";
          let badgeLabel = "Pending";

          if (row.status === "APPROVED") {
            badgeClass = "badge-emerald";
            badgeLabel = "Approved";
          } else if (row.status.includes("REJECTED")) {
            badgeClass = "badge-rose";
            badgeLabel = "Rejected";
          } else if (row.status === "PENDING_RESEARCH") {
            badgeClass = "badge-blue";
            badgeLabel = "Draft";
          }

          const dateDisplay = formatReadableDate(row.research_date) || "Today";

          return `
            <tr style="cursor: pointer;" onclick="viewAuditTrail(${row.id})" title="Click to inspect Record #${row.id}">
              <td class="font-mono text-xs">${escapeHtml(dateDisplay)}</td>
              <td style="max-width: 180px;">
                <div class="text-truncate font-bold text-xs">${escapeHtml(row.title || 'Audio Entry')}</div>
              </td>
              <td><span class="badge ${badgeClass}">${badgeLabel}</span></td>
            </tr>
          `;
        }).join("");
      }
    }

    // Refresh active view tables and speaker ledgers
    loadSpeakerLedgers();
    if (state.activeTab === "review-view") loadReviewQueue();
    if (state.activeTab === "approved-view") loadApprovedDataset();
    if (state.activeTab === "history-view") loadAllRecords();

  } catch (err) {
    console.error("Stats refresh error", err);
  }
}

/* ==========================================================================
   SPEAKER DURATION LEDGERS & LLM CONFIG MODAL (SECTIONS 7 & 9)
   ========================================================================== */
async function loadSpeakerLedgers() {
  const container = document.getElementById("dashboard-speakers-grid");
  const countBadge = document.getElementById("speaker-count-badge");
  if (!container) return;

  try {
    const res = await fetch("/api/speakers/durations");
    if (!res.ok) return;
    const data = await res.json();
    const speakers = data.speakers || [];
    state.speakerLedgers = speakers;

    if (countBadge) {
      countBadge.textContent = `${speakers.length} Speakers`;
    }

    if (speakers.length === 0) {
      container.innerHTML = `<div class="p-3 text-muted text-xs">No speakers recorded yet.</div>`;
      return;
    }

    // Sort alphabetically by speaker display name
    const sortedSpeakers = [...speakers].sort((a, b) => {
      const nameA = (a.speaker_name || a.speaker_id || "").toLowerCase();
      const nameB = (b.speaker_name || b.speaker_id || "").toLowerCase();
      return nameA.localeCompare(nameB);
    });

    container.innerHTML = sortedSpeakers.map(spk => {
      const pct = spk.percentage_used || 0;
      let fillClass = "";
      if (pct >= 90) fillClass = "fill-danger";
      else if (pct >= 65) fillClass = "fill-warning";

      const cardClass = spk.is_capped ? "speaker-ledger-card is-capped" : "speaker-ledger-card";
      const badgeHtml = spk.is_capped
        ? `<span class="badge badge-rose text-xs"><i class="fa-solid fa-lock"></i> Capped (3.5h reached)</span>`
        : `<span class="badge badge-emerald text-xs">${spk.formatted_remaining} left</span>`;

      const displayName = spk.speaker_name || spk.speaker_id;
      const showSubId = spk.speaker_name && spk.speaker_name.toUpperCase() !== spk.speaker_id;

      return `
        <div class="${cardClass}">
          <div class="speaker-card-top">
            <div class="speaker-card-name-group">
              <span class="speaker-card-name" title="${escapeHtml(displayName)}">
                <i class="fa-solid fa-microphone text-primary"></i>
                ${escapeHtml(displayName)}
              </span>
              ${showSubId ? `<span class="speaker-card-id-sub">ID: ${escapeHtml(spk.speaker_id)}</span>` : ""}
            </div>
            ${badgeHtml}
          </div>
          <div>
            <div class="meter-header">
              <span class="text-xs text-muted">
                <i class="fa-solid fa-layer-group"></i> ${spk.record_count} ${spk.record_count === 1 ? "record" : "records"}
              </span>
              <span class="font-mono text-xs font-bold">${spk.formatted_accepted} / 3h 30m (${pct}%)</span>
            </div>
            <div class="progress-bar-bg">
              <div class="progress-bar-fill ${fillClass}" style="width: ${Math.min(100, pct)}%;"></div>
            </div>
          </div>
        </div>
      `;
    }).join("");
  } catch (err) {
    console.error("Error loading speaker ledgers", err);
  }
}

window.openLlmConfigModal = async function() {
  const modal = document.getElementById("modal-llm-config");
  const resultBox = document.getElementById("llm-test-result-box");
  if (!modal) return;
  modal.classList.remove("hidden");
  if (resultBox) resultBox.classList.add("hidden");

  try {
    const res = await fetch("/api/llm/config");
    if (res.ok) {
      const cfg = await res.json();
      const baseEl = document.getElementById("cfg-llm-base-url");
      const pathEl = document.getElementById("cfg-llm-endpoint-path");
      const modelEl = document.getElementById("cfg-llm-model");
      if (baseEl) baseEl.value = cfg.base_url || "";
      if (pathEl) pathEl.value = cfg.endpoint_path || "/v1/chat/completions";
      if (modelEl) modelEl.value = cfg.model || "meta-llama/Llama-3-8B-Instruct";
    }
  } catch (err) {
    console.warn("Failed to fetch LLM config", err);
  }

  // Check YouTube Data API v3 Status
  const ytSummary = document.getElementById("yt-api-summary-text");
  try {
    const ytRes = await fetch("/api/youtube/status");
    if (ytRes.ok) {
      const ytData = await ytRes.json();
      if (ytData.connected) {
        if (ytSummary) {
          ytSummary.innerHTML = `<span style="color: #16a34a; font-weight: 600;"><i class="fa-solid fa-circle-check"></i> Connected & Active</span> &bull; <em style="color: #64748b;">(Secured via .env)</em>`;
        }
      } else {
        if (ytSummary) {
          ytSummary.innerHTML = `<span style="color: #d97706;"><i class="fa-solid fa-triangle-exclamation"></i> ${escapeHtml(ytData.source || 'oEmbed Fallback Active')}</span>`;
        }
      }
    }
  } catch (e) {
    if (ytSummary) ytSummary.textContent = "Status check unavailable";
  }
};

function initLlmConfigModal() {
  const modal = document.getElementById("modal-llm-config");
  const openBtn = document.getElementById("btn-open-llm-settings");
  const sideBtn = document.getElementById("side-tab-llm");
  const closeBtn = document.getElementById("modal-llm-close");
  const footerCloseBtn = document.getElementById("btn-close-llm-config");
  const saveBtn = document.getElementById("btn-save-llm-config");
  const testBtn = document.getElementById("btn-test-llm-endpoint");
  const resultBox = document.getElementById("llm-test-result-box");
  const ytTestBtn = document.getElementById("btn-test-yt-api");
  const ytResultBox = document.getElementById("yt-test-result-box");

  const openHandler = () => window.openLlmConfigModal();
  openBtn?.addEventListener("click", openHandler);
  sideBtn?.addEventListener("click", openHandler);

  const closeHandler = () => modal?.classList.add("hidden");
  closeBtn?.addEventListener("click", closeHandler);
  footerCloseBtn?.addEventListener("click", closeHandler);
  modal?.addEventListener("click", (e) => {
    if (e.target === modal) closeHandler();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && modal && !modal.classList.contains("hidden")) {
      closeHandler();
    }
  });

  // YouTube API Test Button Handler
  ytTestBtn?.addEventListener("click", async () => {
    if (ytResultBox) {
      ytResultBox.classList.remove("hidden");
      ytResultBox.style.background = "#f1f5f9";
      ytResultBox.style.border = "1px solid #cbd5e1";
      ytResultBox.innerHTML = `<span><i class="fa-solid fa-spinner fa-spin text-primary"></i> Testing YouTube Data API v3...</span>`;
    }
    try {
      const res = await fetch("/api/youtube/test", { method: "POST" });
      const data = await res.json();
      if (ytResultBox) {
        if (data.connected) {
          ytResultBox.style.background = "#f0fdf4";
          ytResultBox.style.border = "1px solid #86efac";
          ytResultBox.innerHTML = `
            <div style="color: #166534; font-weight: 600;">
              <i class="fa-solid fa-circle-check text-success"></i> ${escapeHtml(data.message)}
            </div>
            <div class="mt-1" style="font-size: 11px; color: #15803d;">
              <strong>Status:</strong> Connected & Active &bull; <strong>Service:</strong> ${escapeHtml(data.api_service)} &bull; <strong>Key:</strong> Secured (hidden)
            </div>
            <div class="mt-1 text-muted" style="font-size: 11px;">
              Sample video: <em>"${escapeHtml(data.sample_title)}"</em> by <strong>${escapeHtml(data.sample_channel)}</strong> (${escapeHtml(data.sample_duration)})
            </div>
          `;
          const ytSummary = document.getElementById("yt-api-summary-text");
          if (ytSummary) {
            ytSummary.innerHTML = `<span style="color: #16a34a; font-weight: 600;"><i class="fa-solid fa-circle-check"></i> Connected & Active</span> &bull; <em style="color: #64748b;">(Secured via .env)</em>`;
          }
        } else {
          ytResultBox.style.background = "#fef2f2";
          ytResultBox.style.border = "1px solid #fca5a5";
          ytResultBox.innerHTML = `
            <div style="color: #991b1b; font-weight: 600;">
              <i class="fa-solid fa-circle-xmark text-danger"></i> YouTube API Verification Failed
            </div>
            <div class="mt-1 text-danger" style="font-size: 11px;">
              ${escapeHtml(data.error || data.message)}
            </div>
            <div class="mt-1 text-muted" style="font-size: 11px;">
              Make sure <code>YOUTUBE_API_KEY</code> is set in <code>.env</code> with a valid Google Cloud YouTube Data API v3 key.
            </div>
          `;
        }
      }
    } catch (err) {
      if (ytResultBox) {
        ytResultBox.style.background = "#fee2e2";
        ytResultBox.style.border = "1px solid #fca5a5";
        ytResultBox.innerHTML = `<span class="text-danger">Network error: ${escapeHtml(String(err))}</span>`;
      }
    }
  });

  saveBtn?.addEventListener("click", async () => {
    const payload = {
      base_url: document.getElementById("cfg-llm-base-url").value.trim(),
      endpoint_path: document.getElementById("cfg-llm-endpoint-path").value.trim(),
      model: document.getElementById("cfg-llm-model").value.trim(),
      timeout_seconds: 4,
      top_k_matches: 3
    };
    const key = document.getElementById("cfg-llm-api-key").value.trim();
    if (key) payload.api_key = key;

    try {
      const res = await fetch("/api/llm/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      if (res.ok) {
        showToast("LLM endpoint configuration saved successfully!", "success");
        closeHandler();
      } else {
        showToast("Failed to update LLM configuration", "error");
      }
    } catch (err) {
      showToast("Network error saving LLM config", "error");
    }
  });

  // LLM Endpoint Live Test Button Handler
  testBtn?.addEventListener("click", async () => {
    if (resultBox) {
      resultBox.classList.remove("hidden");
      resultBox.style.background = "#f1f5f9";
      resultBox.style.border = "1px solid #cbd5e1";
      resultBox.innerHTML = `<span><i class="fa-solid fa-spinner fa-spin text-primary"></i> Testing endpoint connection...</span>`;
    }

    const payload = {
      base_url: document.getElementById("cfg-llm-base-url")?.value?.trim() || "",
      endpoint_path: document.getElementById("cfg-llm-endpoint-path")?.value?.trim() || "",
      model: document.getElementById("cfg-llm-model")?.value?.trim() || "",
      timeout_seconds: 4,
      test_title: "Indian English lecture with Sanskrit mantras",
      test_channel: "swapnalok",
      test_duration: "00:18:35"
    };
    const key = document.getElementById("cfg-llm-api-key")?.value?.trim();
    if (key) payload.api_key = key;

    try {
      const res = await fetch("/api/llm/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (resultBox) {
        if (!data.is_remote) {
          // Standalone Semantic Engine
          resultBox.style.background = "#f0fdf4";
          resultBox.style.border = "1px solid #86efac";
          resultBox.innerHTML = `
            <div style="color: #166534; font-weight: 600;">
              <i class="fa-solid fa-circle-check text-success"></i> Standalone Semantic Engine Active & Ready
            </div>
            <div class="mt-1 text-xs" style="color: #15803d;">
              ${escapeHtml(data.message)}
            </div>
            <div class="mt-1 text-muted" style="font-size: 11px;">
              Engine: <strong>${escapeHtml(data.model)}</strong> &bull; Latency: ${data.decision?.latency_ms || 0}ms &bull; Zero external API charges
            </div>
          `;
        } else if (data.success) {
          // Remote LLM connection successful!
          resultBox.style.background = "#f0fdf4";
          resultBox.style.border = "1px solid #86efac";
          resultBox.innerHTML = `
            <div style="color: #166534; font-weight: 600;">
              <i class="fa-solid fa-circle-check text-success"></i> Remote Endpoint Connected Successfully!
            </div>
            <div class="mt-1 text-xs" style="color: #15803d;">
              Endpoint: <strong>${escapeHtml(data.endpoint_url)}</strong> &bull; Model: <strong>${escapeHtml(data.model)}</strong>
            </div>
            <div class="mt-1 text-muted" style="font-size: 11px;">
              Latency: ${data.decision?.latency_ms}ms &bull; Status: <span class="badge badge-emerald">${escapeHtml(data.decision?.status)}</span>
            </div>
          `;
        } else {
          // Remote LLM connection failed!
          resultBox.style.background = "#fef2f2";
          resultBox.style.border = "1px solid #fca5a5";
          resultBox.innerHTML = `
            <div style="color: #991b1b; font-weight: 600;">
              <i class="fa-solid fa-circle-xmark text-danger"></i> Endpoint Connection Failed
            </div>
            <div class="mt-1 text-xs" style="color: #b91c1c;">
              ${escapeHtml(data.error || data.message)}
            </div>
            <div class="mt-1 text-muted" style="font-size: 11px;">
              Endpoint: <code>${escapeHtml(data.endpoint_url)}</code> &bull; Model: <code>${escapeHtml(data.model)}</code>
            </div>
          `;
        }
      }
    } catch (err) {
      if (resultBox) {
        resultBox.style.background = "#fee2e2";
        resultBox.style.border = "1px solid #fca5a5";
        resultBox.innerHTML = `<span class="text-danger">Network test failed: ${escapeHtml(String(err))}</span>`;
      }
    }
  });
}


/* ==========================================================================
   13. TOAST NOTIFICATIONS & UTILITY HELPERS
   ========================================================================== */
function showToast(message, type = "info") {
  const container = document.getElementById("toast-container");
  if (!container) return;

  const toast = document.createElement("div");
  toast.className = `toast toast-${type}`;

  const icon = type === "success" ? "circle-check" : (type === "error" ? "circle-xmark" : (type === "warning" ? "triangle-exclamation" : "circle-info"));
  toast.innerHTML = `<i class="fa-solid fa-${icon}"></i> <span>${escapeHtml(message)}</span>`;

  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateX(40px)";
    toast.style.transition = "all 0.3s ease";
    setTimeout(() => toast.remove(), 300);
  }, 4000);
}

function escapeHtml(text) {
  if (!text) return "";
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function formatDate(isoStr) {
  if (!isoStr) return "";
  try {
    const d = new Date(isoStr);
    return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  } catch (e) {
    return isoStr;
  }
}

function debounce(fn, wait) {
  let timer;
  return function(...args) {
    clearTimeout(timer);
    timer = setTimeout(() => fn.apply(this, args), wait);
  };
}

/* ==========================================================================
   8-STAGE WEB SEARCH DEDUPLICATION CLIENT CONTROLLER
   ========================================================================== */

let currentWebVerificationData = null;

function initWebSearchDeduplication() {
  // Web search deduplication tab is deprecated & removed.
  // All tasks are performed in the unified Dashboard view.
}

async function runWebSearchVerification() {
  const inputUrl = document.getElementById("web-input-url");
  const inputRawHtml = document.getElementById("web-input-raw-html");
  const checkAutoStore = document.getElementById("web-auto-store");
  const btnRun = document.getElementById("btn-run-web-verify");
  const btnIcon = document.getElementById("web-verify-btn-icon");
  const btnText = document.getElementById("web-verify-btn-text");

  const url = (inputUrl ? inputUrl.value : "").trim();
  if (!url) {
    showToast("Please enter a valid webpage URL to verify.", "warning");
    if (inputUrl) inputUrl.focus();
    return;
  }

  const rawHtml = inputRawHtml ? inputRawHtml.value.trim() : "";
  const autoStore = checkAutoStore ? checkAutoStore.checked : true;

  // Set running state
  if (btnRun) btnRun.disabled = true;
  if (btnIcon) btnIcon.className = "fa-solid fa-circle-notch fa-spin";
  if (btnText) btnText.textContent = "Verifying 8 Stages...";

  setPipelineRunningState();

  try {
    const payload = {
      url: url,
      html_content: rawHtml || null,
      researched_by: state.activeUser || "Web Researcher",
      auto_store: autoStore
    };

    const res = await fetch("/api/web-verify/check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: "Verification failed" }));
      throw new Error(err.detail || `Server returned error ${res.status}`);
    }

    const data = await res.json();
    currentWebVerificationData = data;

    // Render results
    renderPipelineCompletedState(data);
    renderWebVerdict(data);
    renderWebDetails(data);

    const resultsContainer = document.getElementById("web-results-container");
    if (resultsContainer) {
      resultsContainer.classList.remove("hidden");
      resultsContainer.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    if (autoStore) {
      showToast("Web verification finished and idempotently recorded!", "success");
      const statusMsg = document.getElementById("web-save-status-msg");
      if (statusMsg) statusMsg.innerHTML = `<span class="text-emerald font-semibold"><i class="fa-solid fa-check"></i> Automatically stored in database</span>`;
    } else {
      showToast("Web verification finished!", "info");
      const statusMsg = document.getElementById("web-save-status-msg");
      if (statusMsg) statusMsg.textContent = "Ready to store (Manual)";
    }

    loadWebVerifications();

  } catch (err) {
    console.error("Web verification error:", err);
    showToast(`Verification failed: ${err.message}`, "error");
    resetPipelineToIdle();
  } finally {
    if (btnRun) btnRun.disabled = false;
    if (btnIcon) btnIcon.className = "fa-solid fa-play";
    if (btnText) btnText.textContent = "Run 8-Stage Verification";
  }
}

function setPipelineRunningState() {
  const timeBadge = document.getElementById("pipeline-total-time");
  if (timeBadge) {
    timeBadge.className = "badge badge-blue font-mono text-xs";
    timeBadge.textContent = "Running Pipeline...";
  }

  for (let i = 1; i <= 8; i++) {
    const node = document.getElementById(`pnode-${i}`);
    const badge = document.getElementById(`pbadge-${i}`);
    if (node) {
      node.className = "pipeline-step-node node-active-running";
    }
    if (badge) {
      badge.className = "node-status-badge badge-running";
      badge.textContent = "Running";
    }
  }
}

function resetPipelineToIdle() {
  const timeBadge = document.getElementById("pipeline-total-time");
  if (timeBadge) {
    timeBadge.className = "badge badge-gray font-mono text-xs";
    timeBadge.textContent = "Idle";
  }

  for (let i = 1; i <= 8; i++) {
    const node = document.getElementById(`pnode-${i}`);
    const badge = document.getElementById(`pbadge-${i}`);
    if (node) {
      node.className = "pipeline-step-node";
    }
    if (badge) {
      badge.className = "node-status-badge badge-idle";
      badge.textContent = "Idle";
    }
  }
}

function renderPipelineCompletedState(data) {
  const timeBadge = document.getElementById("pipeline-total-time");
  const totalMs = data.latencies_ms ? data.latencies_ms.total_latency_ms : (data.latency_ms || 0);
  if (timeBadge) {
    timeBadge.className = "badge badge-emerald font-mono text-xs";
    timeBadge.textContent = `Completed in ${totalMs}ms`;
  }

  // Stage 1: URL Normalization
  const n1 = document.getElementById("pnode-1");
  const b1 = document.getElementById("pbadge-1");
  const sub1 = document.getElementById("pnode-1-sub");
  if (n1 && b1) {
    const isExactUrlDup = (data.status === "DUPLICATE" && data.evidence_fields && data.evidence_fields.includes("exact_canonical_url"));
    if (isExactUrlDup) {
      n1.className = "pipeline-step-node node-state-dup";
      b1.className = "node-status-badge badge-dup";
      b1.textContent = "Exact Match";
    } else {
      n1.className = "pipeline-step-node node-state-pass";
      b1.className = "node-status-badge badge-pass";
      b1.textContent = "Normalized";
    }
    if (sub1 && data.domain) sub1.textContent = data.domain;
  }

  // Stage 2: Content Extraction
  const n2 = document.getElementById("pnode-2");
  const b2 = document.getElementById("pbadge-2");
  const sub2 = document.getElementById("pnode-2-sub");
  if (n2 && b2) {
    n2.className = "pipeline-step-node node-state-pass";
    b2.className = "node-status-badge badge-pass";
    b2.textContent = `${data.word_count || 0} words`;
    if (sub2) sub2.textContent = data.title ? data.title.substring(0, 18) + "..." : "Extracted";
  }

  // Stage 3: Content Hash (SHA-256)
  const n3 = document.getElementById("pnode-3");
  const b3 = document.getElementById("pbadge-3");
  const sub3 = document.getElementById("pnode-3-sub");
  if (n3 && b3) {
    const isExactHashDup = (data.status === "DUPLICATE" && data.evidence_fields && data.evidence_fields.includes("exact_sha256_content_hash"));
    if (isExactHashDup) {
      n3.className = "pipeline-step-node node-state-dup";
      b3.className = "node-status-badge badge-dup";
      b3.textContent = "Hash Match";
    } else {
      n3.className = "pipeline-step-node node-state-pass";
      b3.className = "node-status-badge badge-pass";
      b3.textContent = "SHA-256 OK";
    }
    if (sub3 && data.content_hash) sub3.textContent = data.content_hash.substring(0, 10) + "...";
  }

  // Stage 4: Lexical Similarity
  const n4 = document.getElementById("pnode-4");
  const b4 = document.getElementById("pbadge-4");
  const sub4 = document.getElementById("pnode-4-sub");
  if (n4 && b4) {
    const topScore = data.top_matches && data.top_matches.length > 0 ? (data.top_matches[0].lexical_score || 0) : 0;
    n4.className = "pipeline-step-node node-state-pass";
    b4.className = topScore > 65 ? "node-status-badge badge-dup" : (topScore > 40 ? "node-status-badge badge-warn" : "node-status-badge badge-pass");
    b4.textContent = `TF-IDF ${topScore}%`;
    if (sub4) sub4.textContent = `${(data.top_matches || []).length} Candidates`;
  }

  // Stage 5: Passage Overlap
  const n5 = document.getElementById("pnode-5");
  const b5 = document.getElementById("pbadge-5");
  const sub5 = document.getElementById("pnode-5-sub");
  if (n5 && b5) {
    const maxPassage = data.top_matches && data.top_matches.length > 0 ? (data.top_matches[0].max_passage_similarity || 0) : 0;
    n5.className = "pipeline-step-node node-state-pass";
    b5.className = maxPassage > 75 ? "node-status-badge badge-dup" : (maxPassage > 45 ? "node-status-badge badge-warn" : "node-status-badge badge-pass");
    b5.textContent = `Overlap ${maxPassage}%`;
    if (sub5) sub5.textContent = `${(data.passages || []).length} Passages`;
  }

  // Stage 6: Pre-Filter Gate
  const n6 = document.getElementById("pnode-6");
  const b6 = document.getElementById("pbadge-6");
  const sub6 = document.getElementById("pnode-6-sub");
  if (n6 && b6) {
    const pfStatus = data.prefilter_status || "CLEARLY_UNIQUE";
    if (pfStatus === "CLEARLY_UNIQUE") {
      n6.className = "pipeline-step-node node-state-pass";
      b6.className = "node-status-badge badge-pass";
      b6.textContent = "Unique";
      if (sub6) sub6.textContent = "Bypassed LLM";
    } else if (pfStatus === "EXACT_DUPLICATE") {
      n6.className = "pipeline-step-node node-state-dup";
      b6.className = "node-status-badge badge-dup";
      b6.textContent = "Exact Dup";
      if (sub6) sub6.textContent = "Bypassed LLM";
    } else if (pfStatus === "PROBABLE_DUPLICATE") {
      n6.className = "pipeline-step-node node-state-warn";
      b6.className = "node-status-badge badge-warn";
      b6.textContent = "Probable";
      if (sub6) sub6.textContent = "Sent to LLM";
    } else {
      n6.className = "pipeline-step-node node-state-warn";
      b6.className = "node-status-badge badge-warn";
      b6.textContent = "Review";
      if (sub6) sub6.textContent = "Insufficient";
    }
  }

  // Stage 7: LLM Verification
  const n7 = document.getElementById("pnode-7");
  const b7 = document.getElementById("pbadge-7");
  const sub7 = document.getElementById("pnode-7-sub");
  if (n7 && b7) {
    if (data.llm_called) {
      n7.className = data.status === "DUPLICATE" ? "pipeline-step-node node-state-dup" : (data.status === "POTENTIAL_DUPLICATE" ? "pipeline-step-node node-state-warn" : "pipeline-step-node node-state-pass");
      b7.className = data.status === "DUPLICATE" ? "node-status-badge badge-dup" : (data.status === "POTENTIAL_DUPLICATE" ? "node-status-badge badge-warn" : "node-status-badge badge-pass");
      b7.textContent = data.status;
      if (sub7) sub7.textContent = data.endpoint_version || "LLM Adjudicated";
    } else {
      n7.className = "pipeline-step-node";
      b7.className = "node-status-badge badge-skip";
      b7.textContent = "Skipped";
      if (sub7) sub7.textContent = "Rule-Determined";
    }
  }

  // Stage 8: Idempotent Storage
  const n8 = document.getElementById("pnode-8");
  const b8 = document.getElementById("pbadge-8");
  const sub8 = document.getElementById("pnode-8-sub");
  if (n8 && b8) {
    n8.className = "pipeline-step-node node-state-pass";
    b8.className = "node-status-badge badge-pass";
    b8.textContent = "Saved";
    if (sub8) sub8.textContent = "Zero Duplicates";
  }
}

function renderWebVerdict(data) {
  const banner = document.getElementById("web-verdict-banner");
  const iconElem = document.getElementById("web-verdict-icon");
  const titleElem = document.getElementById("web-verdict-title");
  const confElem = document.getElementById("web-verdict-conf");
  const latElem = document.getElementById("web-verdict-latency");
  const matchElem = document.getElementById("web-verdict-matched-id");
  const reasonElem = document.getElementById("web-verdict-reason");

  const status = (data.status || "UNIQUE").toUpperCase();
  const totalMs = data.latencies_ms ? data.latencies_ms.total_latency_ms : (data.latency_ms || 0);

  if (banner) {
    banner.className = `web-verdict-card verdict-${status.toLowerCase().replace(/_/g, "-")}`;
  }

  if (iconElem) {
    if (status === "DUPLICATE") {
      iconElem.innerHTML = `<i class="fa-solid fa-circle-xmark"></i>`;
    } else if (status === "POTENTIAL_DUPLICATE") {
      iconElem.innerHTML = `<i class="fa-solid fa-triangle-exclamation"></i>`;
    } else {
      iconElem.innerHTML = `<i class="fa-solid fa-circle-check"></i>`;
    }
  }

  if (titleElem) {
    if (status === "DUPLICATE") {
      titleElem.textContent = "CONFIRMED DUPLICATE CONTENT";
    } else if (status === "POTENTIAL_DUPLICATE") {
      titleElem.textContent = "POTENTIAL DUPLICATE / REVIEW REQUIRED";
    } else {
      titleElem.textContent = "UNIQUE & DISTINCT CONTENT VERIFIED";
    }
  }

  if (confElem) {
    const pct = Math.round((data.confidence || 1.0) * 100);
    confElem.textContent = `Confidence: ${pct}%`;
  }

  if (latElem) {
    latElem.textContent = `Latency: ${totalMs}ms`;
  }

  if (matchElem) {
    matchElem.textContent = data.matched_page_id ? `Match: Record #${data.matched_page_id}` : `Match: None`;
  }

  if (reasonElem) {
    reasonElem.textContent = data.decision_reason || "Adjudicated by multi-signal verification engine.";
  }
}

function renderWebDetails(data) {
  // Left Column
  const titleElem = document.getElementById("web-res-title");
  const domainElem = document.getElementById("web-res-domain");
  const canonElem = document.getElementById("web-res-canonical");
  const authorElem = document.getElementById("web-res-author");
  const dateElem = document.getElementById("web-res-date");
  const descElem = document.getElementById("web-res-desc");
  const hashElem = document.getElementById("web-res-hash");
  const wordBadge = document.getElementById("web-word-count-badge");
  const headingsContainer = document.getElementById("web-res-headings");
  const normTextElem = document.getElementById("web-res-norm-text");

  if (titleElem) titleElem.textContent = data.title || "Untitled Webpage";
  if (domainElem) domainElem.textContent = data.domain || "-";
  if (canonElem) canonElem.textContent = data.canonical_url || "-";
  if (authorElem) authorElem.textContent = data.author || "Not Specified";
  if (dateElem) dateElem.textContent = data.publication_date || "Not Specified";
  if (descElem) descElem.textContent = data.description || "No meta description found.";
  if (hashElem) hashElem.textContent = data.content_hash || "-";
  if (wordBadge) wordBadge.textContent = `${data.word_count || 0} words`;

  if (headingsContainer) {
    const headings = data.headings || [];
    if (headings.length > 0) {
      headingsContainer.innerHTML = headings.map(h => `<span class="heading-tag">${escapeHtml(h)}</span>`).join("");
    } else {
      headingsContainer.innerHTML = `<span class="text-xs text-muted">No H1-H3 headings found</span>`;
    }
  }

  if (normTextElem) {
    normTextElem.textContent = data.normalized_text || "No text content extracted.";
  }

  // Right Column: Similarity Breakdown & Top Candidates
  const matches = data.top_matches || [];
  const topMatch = matches.length > 0 ? matches[0] : null;

  const lexicalVal = topMatch ? (topMatch.tfidf_similarity || 0) : 0;
  const passageVal = topMatch ? (topMatch.max_passage_similarity || 0) : 0;
  const compVal = topMatch ? (topMatch.composite_similarity || data.prefilter_score || 0) : 0;

  const meterLex = document.getElementById("web-meter-lexical");
  const progLex = document.getElementById("web-prog-lexical");
  const meterPass = document.getElementById("web-meter-passage");
  const progPass = document.getElementById("web-prog-passage");
  const meterComp = document.getElementById("web-meter-composite");
  const progComp = document.getElementById("web-prog-composite");
  const matchCountBadge = document.getElementById("web-match-count-badge");

  if (meterLex) meterLex.textContent = `${lexicalVal}%`;
  if (progLex) progLex.style.width = `${Math.min(100, lexicalVal)}%`;

  if (meterPass) meterPass.textContent = `${passageVal}%`;
  if (progPass) progPass.style.width = `${Math.min(100, passageVal)}%`;

  if (meterComp) meterComp.textContent = `${compVal}%`;
  if (progComp) progComp.style.width = `${Math.min(100, compVal)}%`;

  if (matchCountBadge) matchCountBadge.textContent = `${matches.length} Candidate${matches.length === 1 ? "" : "s"}`;

  const topMatchesList = document.getElementById("web-top-matches-list");
  if (topMatchesList) {
    if (matches.length === 0) {
      topMatchesList.innerHTML = `
        <div class="empty-matches-placeholder">
          <i class="fa-regular fa-circle-check text-emerald fa-2x mb-2"></i>
          <p class="text-sm font-semibold">No Overlapping Candidates Found</p>
          <p class="text-xs text-muted">This webpage content is clearly distinct from all existing database records.</p>
        </div>
      `;
    } else {
      topMatchesList.innerHTML = matches.map(m => {
        const id = m.id || m.canonical_url;
        const title = m.title || "Untitled Record";
        const url = m.canonical_url || m.audio_link || "-";
        const author = m.author ? `&bull; Author: ${escapeHtml(m.author)}` : "";
        const comp = m.composite_similarity || 0;
        const passSim = m.max_passage_similarity || 0;
        const matchingPassages = m.matching_passages || [];
        const evidence = m.supporting_evidence || [];

        let passageHtml = "";
        if (matchingPassages.length > 0) {
          passageHtml = `
            <div class="passage-snippet-box">
              <span class="font-semibold text-xs" style="color: #7c3aed;">Sample Matching Passage (${matchingPassages[0].similarity}% overlap):</span><br>
              "${escapeHtml(matchingPassages[0].matched_passage)}"
            </div>
          `;
        }

        let evidenceHtml = "";
        if (evidence.length > 0) {
          evidenceHtml = `
            <div class="evidence-chips">
              ${evidence.map(e => `<span class="evidence-chip">${escapeHtml(e)}</span>`).join("")}
            </div>
          `;
        }

        return `
          <div class="match-item-card">
            <div class="match-card-header">
              <div class="match-card-title">Record #${id}: ${escapeHtml(title)}</div>
              <span class="match-card-score-pill">${comp}% Match</span>
            </div>
            <div class="match-card-url">${escapeHtml(url)} ${author}</div>
            <div class="text-xs text-muted">Passage Overlap: <strong>${passSim}%</strong> &bull; Lexical Score: <strong>${m.tfidf_similarity || 0}%</strong></div>
            ${passageHtml}
            ${evidenceHtml}
          </div>
        `;
      }).join("");
    }
  }
}

async function saveCurrentWebVerification() {
  if (!currentWebVerificationData) {
    showToast("No active verification to save.", "warning");
    return;
  }

  const btnSave = document.getElementById("btn-web-manual-save");
  if (btnSave) btnSave.disabled = true;

  try {
    const res = await fetch("/api/web-verify/store", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(currentWebVerificationData)
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: "Failed to store" }));
      throw new Error(err.detail || `Server error ${res.status}`);
    }

    const data = await res.json();
    showToast(`Stored successfully (Action: ${data.record.action}, ID: ${data.record.id})`, "success");
    const statusMsg = document.getElementById("web-save-status-msg");
    if (statusMsg) statusMsg.innerHTML = `<span class="text-emerald font-semibold"><i class="fa-solid fa-check"></i> Idempotently recorded (ID #${data.record.id})</span>`;
    loadWebVerifications();
  } catch (err) {
    showToast(`Failed to store: ${err.message}`, "error");
  } finally {
    if (btnSave) btnSave.disabled = false;
  }
}

async function loadWebVerifications() {
  const tbody = document.getElementById("web-verifications-tbody");
  if (!tbody) return;

  const filterSearch = document.getElementById("filter-web-search");
  const filterStatus = document.getElementById("filter-web-status");

  const search = filterSearch ? filterSearch.value.trim() : "";
  const status = filterStatus ? filterStatus.value : "ALL";

  let url = `/api/web-verify/records?status=${encodeURIComponent(status)}`;
  if (search) url += `&search=${encodeURIComponent(search)}`;

  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP error ${res.status}`);
    const data = await res.json();
    const records = data.records || [];

    if (records.length === 0) {
      tbody.innerHTML = `<tr><td colspan="9" class="text-center py-4 text-muted">No stored web verification records found.</td></tr>`;
      return;
    }

    tbody.innerHTML = records.map(r => {
      const statusBadge = r.status === "DUPLICATE"
        ? `<span class="badge badge-duplicate">DUPLICATE</span>`
        : (r.status === "POTENTIAL_DUPLICATE" ? `<span class="badge badge-potential">REVIEW</span>` : `<span class="badge badge-unique">UNIQUE</span>`);

      const confPct = Math.round((r.confidence || 1.0) * 100);
      const hashShort = r.content_hash ? r.content_hash.substring(0, 12) + "..." : "-";
      const dateStr = formatDate(r.created_at);

      return `
        <tr>
          <td class="font-mono text-xs">${r.id}</td>
          <td>
            <strong class="text-sm">${escapeHtml(r.title || "Untitled")}</strong>
            <div class="text-xs text-muted font-mono">${escapeHtml(r.domain || "-")}</div>
          </td>
          <td>
            <a href="${escapeHtml(r.canonical_url)}" target="_blank" class="text-xs font-mono break-all text-primary">${escapeHtml(r.canonical_url)}</a>
          </td>
          <td class="text-xs">${escapeHtml(r.author || "Not specified")}</td>
          <td class="font-mono text-xs" title="${escapeHtml(r.content_hash || "")}">${hashShort}</td>
          <td>${statusBadge}</td>
          <td class="font-mono text-xs">${confPct}%</td>
          <td class="text-xs text-muted">${dateStr}</td>
          <td>
            <button type="button" class="btn btn-secondary btn-sm" onclick="openWebRecordDetails(${r.id})" title="View Complete Verification Details">
              <i class="fa-solid fa-eye"></i> View
            </button>
          </td>
        </tr>
      `;
    }).join("");

  } catch (err) {
    console.error("Failed to load web verifications:", err);
    tbody.innerHTML = `<tr><td colspan="9" class="text-center py-4 text-danger">Error loading records: ${escapeHtml(err.message)}</td></tr>`;
  }
}

async function openWebRecordDetails(recordId) {
  try {
    const res = await fetch(`/api/web-verify/records/${recordId}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    currentWebVerificationData = data;
    renderPipelineCompletedState(data);
    renderWebVerdict(data);
    renderWebDetails(data);

    const inputUrl = document.getElementById("web-input-url");
    if (inputUrl) inputUrl.value = data.canonical_url || data.url || "";

    const resultsContainer = document.getElementById("web-results-container");
    if (resultsContainer) {
      resultsContainer.classList.remove("hidden");
      resultsContainer.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  } catch (err) {
    showToast(`Failed to open record #${recordId}: ${err.message}`, "error");
  }
}

window.copyFastContentHash = function() {
  const hashElem = document.getElementById("fast-meta-hash");
  if (!hashElem) return;
  const hash = hashElem.textContent.trim();
  if (!hash || hash === "-" || hash.includes("Unavailable")) {
    showToast("No valid content hash available to copy", "warning");
    return;
  }
  navigator.clipboard.writeText(hash).then(() => {
    showToast("SHA-256 Content Hash copied to clipboard!", "success");
  }).catch(() => {
    showToast("Failed to copy hash", "error");
  });
};

window.copyContentHash = function() {
  window.copyFastContentHash();
};

window.inspectInWebPipeline = function(targetUrl) {
  if (!targetUrl) return;
  switchTab("dashboard-view");
  const input = document.getElementById("input-audio-link");
  if (input) {
    input.value = targetUrl;
    syncSearchInputs(targetUrl, "input-audio-link");
  }
  performDuplicateCheck(targetUrl);
};
