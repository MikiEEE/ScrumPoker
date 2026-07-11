const bootstrap = {
  appId: document.documentElement.dataset.appId || "room",
  appLabel: document.documentElement.dataset.appLabel || "",
  basePath: document.documentElement.dataset.basePath || "/",
  creatorClaimStorageKey: document.documentElement.dataset.creatorClaimStorageKey || "",
  healthzPath: document.documentElement.dataset.healthzPath || "/healthz",
  roomId: document.documentElement.dataset.roomId || "room",
  roomKind: document.documentElement.dataset.roomKind || "premium",
  statePath: document.documentElement.dataset.statePath || "/api/state",
  storageKeyPrefix: document.documentElement.dataset.storageKeyPrefix || "smallos-scrum-poker-room",
  wsPath: document.documentElement.dataset.wsPath || "/ws",
};

const defaultVoteOptions = ["0", "0.5", "1", "2", "3", "5", "8", "13", "21", "40", "60", "100", "?", "coffee"];

const KEEP_ALIVE_INTERVAL_MS = 3 * 60 * 1000;
const KEEP_ALIVE_CHECK_MS = 30 * 1000;
let keepAliveTimer = null;

function checkKeepAlive() {
  const me = currentViewer();
  const isAdmin = viewerIsAdmin();
  const joined = viewerHasJoined();

  if (!appState.connected) return;

  if (isAdmin && !joined) {
    fetch(bootstrap.healthzPath).catch(() => { });
    return;
  }

  if (joined) {
    const session = activeSession();
    const participants = session.participants || [];
    if (!participants.length) return;
    const sortedIds = participants.map((p) => p.client_id).sort((a, b) => a - b);
    const slot = Math.floor(Date.now() / KEEP_ALIVE_INTERVAL_MS);
    const designeeId = sortedIds[slot % sortedIds.length];
    if (me.client_id === designeeId) {
      fetch(bootstrap.healthzPath).catch(() => { });
    }
  }
}

function scheduleKeepAlive() {
  if (keepAliveTimer !== null) return;
  checkKeepAlive();
  keepAliveTimer = setInterval(checkKeepAlive, KEEP_ALIVE_CHECK_MS);
}

function cancelKeepAlive() {
  if (keepAliveTimer !== null) {
    clearInterval(keepAliveTimer);
    keepAliveTimer = null;
  }
}

const nameStorageKey = bootstrap.storageKeyPrefix + "-name";
const sessionTokenStorageKey = bootstrap.storageKeyPrefix + "-session-token";
const tabIdStorageKey = bootstrap.storageKeyPrefix + "-tab-id";
const joinPanelExpandedStorageKey = bootstrap.storageKeyPrefix + "-join-panel-expanded";
const creatorClaimStorageKey = bootstrap.creatorClaimStorageKey || "";

const appState = {
  adminFormVisible: false,
  answerDraft: "",
  answerDraftDirty: false,
  connected: false,
  flash: null,
  session: null,
  socket: null,
  statusLine: "Connecting...",
};

const els = {
  adminAuthCancelButton: document.getElementById("adminAuthCancelButton"),
  adminAuthForm: document.getElementById("adminAuthForm"),
  adminAuthPanel: document.getElementById("adminAuthPanel"),
  adminChip: document.getElementById("adminChip"),
  adminPassphraseInput: document.getElementById("adminPassphraseInput"),
  adminStatus: document.getElementById("adminStatus"),
  adminUnlockButton: document.getElementById("adminUnlockButton"),
  boardSummary: document.getElementById("boardSummary"),
  clearVotesButton: document.getElementById("clearVotesButton"),
  closeSessionButton: document.getElementById("closeSessionButton"),
  connectedCount: document.getElementById("connectedCount"),
  joinPanelBody: document.getElementById("joinPanelBody"),
  joinPanelCollapsed: document.getElementById("joinPanelCollapsed"),
  joinPanelCollapsedText: document.getElementById("joinPanelCollapsedText"),
  joinPanelCollapsedTitle: document.getElementById("joinPanelCollapsedTitle"),
  joinPanelExpandButton: document.getElementById("joinPanelExpandButton"),
  joinPanelToggleButton: document.getElementById("joinPanelToggleButton"),
  joinButton: document.getElementById("joinButton"),
  joinForm: document.getElementById("joinForm"),
  joinHelp: document.getElementById("joinHelp"),
  nameInput: document.getElementById("nameInput"),
  noticeArea: document.getElementById("noticeArea"),
  openSessionButton: document.getElementById("openSessionButton"),
  pointingModeButton: document.getElementById("pointingModeButton"),
  participantCount: document.getElementById("participantCount"),
  participantGrid: document.getElementById("participantGrid"),
  serverTime: document.getElementById("serverTime"),
  shortAnswerModeButton: document.getElementById("shortAnswerModeButton"),
  sessionStatus: document.getElementById("sessionStatus"),
  socketChip: document.getElementById("socketChip"),
  statusLine: document.getElementById("statusLine"),
  toggleVotesButton: document.getElementById("toggleVotesButton"),
  responsePanelTitle: document.getElementById("responsePanelTitle"),
  responsesCastLabel: document.getElementById("responsesCastLabel"),
  roundFooter: document.getElementById("roundFooter"),
  voteGrid: document.getElementById("voteGrid"),
  voteSummary: document.getElementById("voteSummary"),
  votesCast: document.getElementById("votesCast"),
  adminControlsPanel: document.getElementById("adminControlsPanel"),
};

function websocketUrl() {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const baseUrl = protocol + "://" + window.location.host + bootstrap.wsPath;
  const params = new URLSearchParams();
  const sessionToken = window.sessionStorage.getItem(sessionTokenStorageKey);
  const tabId = getTabId();

  if (sessionToken) {
    params.set("session_token", sessionToken);
  }
  if (tabId) {
    params.set("tab_id", tabId);
  }
  return params.toString() ? baseUrl + "?" + params.toString() : baseUrl;
}

function createBrowserTabId() {
  if (window.crypto && typeof window.crypto.randomUUID === "function") {
    return window.crypto.randomUUID();
  }
  return "tab-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
}

function getTabId() {
  let tabId = window.sessionStorage.getItem(tabIdStorageKey);
  if (!tabId) {
    tabId = createBrowserTabId();
    window.sessionStorage.setItem(tabIdStorageKey, tabId);
  }
  return tabId;
}

function setFlash(message, kind = "info") {
  appState.flash = message ? { kind, message } : null;
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function clearFlash() {
  appState.flash = null;
}

function send(message) {
  if (!appState.socket || appState.socket.readyState !== WebSocket.OPEN) {
    return;
  }
  clearFlash();
  appState.socket.send(JSON.stringify(message));
  render();
}

function buildNotice(message, kind) {
  const className = kind ? "notice " + kind : "notice";
  return '<div class="' + className + '">' + escapeHtml(message) + "</div>";
}

function activeSession() {
  return appState.session || {};
}

function currentViewer() {
  return activeSession().me || {};
}

function viewerIsAdmin() {
  return Boolean(currentViewer().is_admin);
}

function viewerHasJoined() {
  return Boolean(currentViewer().name);
}

function joinPanelShouldBeExpanded(joined, isAdmin) {
  if (!joined && !isAdmin) {
    return true;
  }

  const storedValue = window.localStorage.getItem(joinPanelExpandedStorageKey);
  if (storedValue === null) {
    return false;
  }

  return storedValue === "true";
}

function setJoinPanelExpanded(expanded) {
  window.localStorage.setItem(joinPanelExpandedStorageKey, expanded ? "true" : "false");
}

function currentVoteOptions() {
  const session = activeSession();
  return Array.isArray(session.vote_options) && session.vote_options.length
    ? session.vote_options
    : defaultVoteOptions;
}

function maybeClaimCreatorAdmin() {
  if (bootstrap.roomKind !== "ephemeral" || !creatorClaimStorageKey) {
    return;
  }
  const token = window.sessionStorage.getItem(creatorClaimStorageKey);
  if (token) {
    send({ type: "claim_creator_admin", token });
  }
}

function maybeClearCreatorClaimToken(me) {
  if (!creatorClaimStorageKey) {
    return;
  }
  if (me && me.is_admin) {
    window.sessionStorage.removeItem(creatorClaimStorageKey);
  }
}

function renderVotes() {
  const session = activeSession();
  if (session.response_mode === "short_answer") {
    renderShortAnswer();
    return;
  }
  const selectedVote = currentViewer().vote || null;
  const joined = viewerHasJoined();

  els.voteGrid.innerHTML = currentVoteOptions().map((option) => {
    const active = selectedVote === option ? " active" : "";
    const label = option === "coffee" ? "break" : option;
    return '<button class="vote-card' + active + '" type="button" data-vote="' + option + '"' + (joined ? "" : " disabled") + ">" + escapeHtml(label) + "</button>";
  }).join("");

  if (!joined) {
    els.voteSummary.textContent = "Join the session to cast a vote.";
    return;
  }

  if (selectedVote) {
    els.voteSummary.textContent = "Your current vote: " + selectedVote + ".";
  } else {
    els.voteSummary.textContent = "You are in the room. Pick a card when you are ready.";
  }
}

function renderShortAnswer() {
  const me = currentViewer();
  const joined = viewerHasJoined();
  const isReady = Boolean(me.is_ready);
  els.responsePanelTitle.textContent = "Your Answer";
  els.voteGrid.innerHTML = [
    '<div class="answer-ready-actions">',
    '<button type="button" data-answer-ready="true"' + (joined ? "" : " disabled") + '>Ready</button>',
    '<button type="button" class="button-secondary" data-answer-ready="false"' + (joined && isReady ? "" : " disabled") + '>Not Ready</button>',
    '</div>',
  ].join("");

  if (!joined) {
    els.voteSummary.textContent = "Join the session to answer.";
  } else if (isReady) {
    els.voteSummary.textContent = "Your answer is ready. Select Not Ready to edit it.";
  } else {
    els.voteSummary.textContent = "Type your answer on the board, then select Ready.";
  }
}

function getInitials(name) {
  const parts = name.trim().split(/\s+/);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return name.slice(0, 2).toUpperCase();
}

function renderParticipants() {
  const session = activeSession();
  const participants = session.participants || [];
  const canKick = viewerIsAdmin();
  const activeAnswerInput = document.activeElement && document.activeElement.id === "shortAnswerInput";
  const answerSelectionStart = activeAnswerInput ? document.activeElement.selectionStart : null;
  const answerSelectionEnd = activeAnswerInput ? document.activeElement.selectionEnd : null;

  if (!participants.length) {
    els.participantGrid.innerHTML = '<div class="empty-board">No one has joined the table yet.</div>';
    return;
  }

  els.participantGrid.innerHTML = participants.map((participant) => {
    let voteChipClass = "vote-chip waiting";
    let voteChipContent = "\u2014";

    const shortAnswerMode = session.response_mode === "short_answer";
    const hasResponse = shortAnswerMode ? participant.is_ready : participant.has_voted;

    if (shortAnswerMode && participant.short_answer !== null && participant.short_answer !== undefined) {
      voteChipClass = "vote-chip revealed answer-revealed";
      voteChipContent = escapeHtml(participant.short_answer);
    } else if (!shortAnswerMode && participant.vote !== null && participant.vote !== undefined) {
      voteChipClass = "vote-chip revealed";
      voteChipContent = escapeHtml(participant.vote === "coffee" ? "\u2615" : participant.vote);
    } else if (hasResponse) {
      voteChipClass = "vote-chip voted";
      voteChipContent = "\u2713";
    }

    const meta = participant.is_self
      ? "You"
      : !participant.is_connected
        ? "Reconnecting\u2026"
        : hasResponse
          ? (session.votes_visible ? "Revealed" : "Voted")
          : (shortAnswerMode ? "Not ready" : "Deciding\u2026");

    const responseMeta = shortAnswerMode && hasResponse && !session.votes_visible ? "Ready" : meta;

    const adminBadge = participant.is_admin ? '<span class="badge">Admin</span>' : "";
    const kickButton = canKick && !participant.is_self
      ? '<button class="button-ghost kick-button" type="button" data-kick="' + participant.client_id + '" data-name="' + escapeHtml(participant.name) + '">Kick</button>'
      : "";
    const responseControl = shortAnswerMode && participant.is_self
      ? [
        '<div class="participant-answer-editor">',
        '<textarea id="shortAnswerInput" maxlength="128" rows="2" placeholder="Type a short answer…" aria-label="Your short answer"' + (participant.is_ready ? " disabled" : "") + '>' + escapeHtml(appState.answerDraft) + '</textarea>',
        '<div class="answer-counter"><span id="answerCharacterCount">' + appState.answerDraft.length + '</span>/128</div>',
        '</div>',
      ].join("")
      : '<div class="' + voteChipClass + '">' + voteChipContent + '</div>';

    return [
      '<div class="participant-row' + (participant.is_self ? " self" : "") + '">',
      '<div class="participant-avatar">' + escapeHtml(getInitials(participant.name)) + '</div>',
      '<div class="participant-info">',
      '<div class="participant-name-row">',
      '<span class="participant-name">' + escapeHtml(participant.name) + '</span>',
      adminBadge,
      '</div>',
      '<span class="participant-meta">' + escapeHtml(responseMeta) + '</span>',
      '</div>',
      kickButton,
      responseControl,
      '</div>',
    ].join("");
  }).join("");

  if (activeAnswerInput) {
    const restoredInput = document.getElementById("shortAnswerInput");
    if (restoredInput && !restoredInput.disabled) {
      restoredInput.focus();
      restoredInput.setSelectionRange(answerSelectionStart, answerSelectionEnd);
    }
  }
}

function renderNotice() {
  const session = activeSession();

  if (appState.flash) {
    els.noticeArea.innerHTML = buildNotice(appState.flash.message, appState.flash.kind);
    return;
  }

  if (!session.session_open && !viewerHasJoined()) {
    els.noticeArea.innerHTML = buildNotice("Joining is paused by an administrator. You can still watch the board update live.", "closed");
    return;
  }

  if (viewerHasJoined()) {
    els.noticeArea.innerHTML = buildNotice(
      session.response_mode === "short_answer"
        ? "Live round ready. Write an answer, mark it ready, or reveal the board."
        : "Live round ready. You can vote, reveal the board, or discard the current round.",
      "info"
    );
    return;
  }

  els.noticeArea.innerHTML = buildNotice(
    session.response_mode === "short_answer"
      ? "Pick a display name to join the table and answer."
      : "Pick a display name to join the table and start voting.",
    "info"
  );
}

function renderSession() {
  const session = activeSession();
  const sessionOpen = session.session_open !== false;
  const participants = session.participants || [];
  const me = currentViewer();
  const shortAnswerMode = session.response_mode === "short_answer";
  const votesCast = participants.filter((participant) => shortAnswerMode ? participant.is_ready : participant.has_voted).length;
  const joined = viewerHasJoined();
  const isAdmin = viewerIsAdmin();
  const adminAvailable = Boolean(session.admin_auth_enabled);
  const joinLimit = session.join_limit || participants.length || 0;
  const joinPanelExpanded = joinPanelShouldBeExpanded(joined, isAdmin);
  const canToggleJoinPanel = joined || isAdmin;
  const showCollapsedJoinPanel = canToggleJoinPanel && !joinPanelExpanded;

  els.sessionStatus.textContent = sessionOpen ? "Joining is open" : "Joining is paused";
  els.sessionStatus.className = "session-status " + (sessionOpen ? "open" : "closed");
  els.statusLine.textContent = appState.statusLine;
  els.participantCount.textContent = String(session.participant_count || 0);
  els.votesCast.textContent = String(votesCast);
  els.responsesCastLabel.textContent = shortAnswerMode ? "ready" : "voted";
  els.roundFooter.textContent = shortAnswerMode
    ? "Hidden rounds show who is ready without exposing their answer."
    : "Hidden rounds show who voted without exposing the value.";
  els.responsePanelTitle.textContent = shortAnswerMode ? "Your Answer" : "Your Vote";
  els.connectedCount.textContent = String(session.connected_count || 0);
  els.serverTime.textContent = session.server_time || "-";
  els.joinButton.disabled = !appState.connected || (!sessionOpen && !joined && !isAdmin);
  els.joinButton.textContent = joined ? "Update Name" : "Join Session";
  els.toggleVotesButton.disabled = !appState.connected;
  els.toggleVotesButton.textContent = session.votes_visible
    ? (shortAnswerMode ? "Hide Answers" : "Hide Votes")
    : (shortAnswerMode ? "Reveal Answers" : "Show Votes");
  els.clearVotesButton.textContent = shortAnswerMode ? "Clear Answers" : "Discard Votes";
  els.clearVotesButton.disabled = !appState.connected;
  els.socketChip.textContent = appState.connected ? "Socket connected" : "Socket reconnecting";
  els.socketChip.className = "status-chip" + (appState.connected ? "" : " offline");
  els.joinPanelBody.hidden = showCollapsedJoinPanel;
  els.joinPanelCollapsed.hidden = !showCollapsedJoinPanel;
  els.joinPanelToggleButton.hidden = !canToggleJoinPanel || showCollapsedJoinPanel;
  els.joinPanelToggleButton.textContent = showCollapsedJoinPanel ? "Show join panel" : "Hide join panel";
  els.joinPanelCollapsedTitle.textContent = joined
    ? "Joined as " + me.name + "."
    : isAdmin
      ? "Admin mode enabled."
      : "Ready when you are.";
  els.joinPanelCollapsedText.textContent = joined
    ? "The join controls are tucked away, but still available if you want to update your name or use admin features."
    : isAdmin
      ? "The join controls are tucked away, but you can reopen them any time as an admin."
      : "The join controls are tucked away.";

  if (joined) {
    els.joinHelp.textContent = "You are joined as " + me.name + ". " + (shortAnswerMode ? "Answers" : "Votes") + " stay hidden until someone reveals them.";
  } else if (sessionOpen) {
    els.joinHelp.textContent = "Joining is open. This room supports up to " + joinLimit + " named participants.";
  } else {
    els.joinHelp.textContent = "Joining is currently disabled for new non-admin participants.";
  }

  if (!participants.length) {
    els.boardSummary.textContent = "Waiting for the first participant.";
  } else if (session.votes_visible) {
    els.boardSummary.textContent = (shortAnswerMode ? "Answers" : "Votes") + " are revealed for " + participants.length + " participant(s).";
  } else {
    els.boardSummary.textContent = votesCast + " of " + participants.length + " participant(s) " + (shortAnswerMode ? "are ready." : "have voted.");
  }

  els.adminUnlockButton.hidden = !adminAvailable || isAdmin;
  els.adminAuthPanel.hidden = !adminAvailable || isAdmin || !appState.adminFormVisible;
  els.adminChip.hidden = !isAdmin;
  els.adminControlsPanel.hidden = !isAdmin;
  els.openSessionButton.disabled = !appState.connected || !isAdmin || sessionOpen;
  els.closeSessionButton.disabled = !appState.connected || !isAdmin || !sessionOpen;
  els.pointingModeButton.disabled = !appState.connected || !isAdmin || !shortAnswerMode;
  els.shortAnswerModeButton.disabled = !appState.connected || !isAdmin || shortAnswerMode;

  if (!adminAvailable) {
    els.adminStatus.textContent = "Admin access is not configured on this server.";
  } else if (isAdmin) {
    els.adminStatus.textContent = "Admin mode is enabled for this browser session.";
  } else {
    els.adminStatus.textContent = session.admin_auth_help || "Use Become Admin to unlock session controls.";
  }
}

function render() {
  renderVotes();
  renderParticipants();
  renderSession();
  renderNotice();
}

function connect() {
  const socket = new WebSocket(websocketUrl());
  appState.socket = socket;

  socket.addEventListener("open", () => {
    appState.connected = true;
    appState.statusLine = "Connected.";
    scheduleKeepAlive();
    render();
    maybeClaimCreatorAdmin();
  });

  socket.addEventListener("message", (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch (error) {
      setFlash("Received invalid JSON from the server.", "error");
      render();
      return;
    }

    if (message.type === "state") {
      const previousMode = appState.session && appState.session.response_mode;
      const previousRoundId = appState.session && appState.session.round_id;
      appState.session = message.state;
      const nextMode = message.state.response_mode || "pointing";
      if (previousMode !== nextMode || (previousRoundId !== undefined && previousRoundId !== message.state.round_id)) {
        appState.answerDraft = "";
        appState.answerDraftDirty = false;
      }
      if (nextMode === "short_answer" && !appState.answerDraftDirty && message.state.me && message.state.me.short_answer) {
        appState.answerDraft = message.state.me.short_answer;
      }
      appState.statusLine = message.state.session_open
        ? "Room is live and ready for the next estimate."
        : "Room is live, but joining is paused.";
      if (message.state.me && message.state.me.session_token) {
        window.sessionStorage.setItem(sessionTokenStorageKey, message.state.me.session_token);
      }
      if (message.state.me && message.state.me.name) {
        window.localStorage.setItem(nameStorageKey, message.state.me.name);
        els.nameInput.value = message.state.me.name;
      }
      if (message.state.me && message.state.me.is_admin) {
        appState.adminFormVisible = false;
        els.adminPassphraseInput.value = "";
      }
      maybeClearCreatorClaimToken(message.state.me);
      render();
      return;
    }

    if (message.type === "notice") {
      setFlash(message.message || "Success.", message.kind || "success");
      render();
      return;
    }

    if (message.type === "error") {
      if (creatorClaimStorageKey && String(message.message || "").toLowerCase().includes("creator admin claim")) {
        window.sessionStorage.removeItem(creatorClaimStorageKey);
      }
      setFlash(message.message || "Unknown server error.", "error");
      render();
    }
  });

  socket.addEventListener("close", () => {
    appState.connected = false;
    appState.statusLine = "Disconnected. Attempting to reconnect...";
    cancelKeepAlive();
    render();
    window.setTimeout(connect, 1200);
  });

  socket.addEventListener("error", () => {
    setFlash("WebSocket connection error.", "error");
    render();
  });
}

els.joinForm.addEventListener("submit", (event) => {
  event.preventDefault();
  send({ type: "join", name: els.nameInput.value.trim() });
  setJoinPanelExpanded(false);
});

els.adminUnlockButton.addEventListener("click", () => {
  appState.adminFormVisible = !appState.adminFormVisible;
  render();
  if (appState.adminFormVisible) {
    els.adminPassphraseInput.focus();
  }
});

els.joinPanelToggleButton.addEventListener("click", () => {
  setJoinPanelExpanded(false);
  render();
});

els.joinPanelExpandButton.addEventListener("click", () => {
  setJoinPanelExpanded(true);
  render();
  els.nameInput.focus();
});

els.adminAuthCancelButton.addEventListener("click", () => {
  appState.adminFormVisible = false;
  els.adminPassphraseInput.value = "";
  render();
});

els.adminAuthForm.addEventListener("submit", (event) => {
  event.preventDefault();
  send({ type: "become_admin", passphrase: els.adminPassphraseInput.value });
});

els.voteGrid.addEventListener("click", (event) => {
  const readyTarget = event.target.closest("[data-answer-ready]");
  if (readyTarget) {
    if (readyTarget.getAttribute("data-answer-ready") === "true") {
      send({ type: "submit_short_answer", answer: appState.answerDraft });
      appState.answerDraftDirty = false;
    } else {
      send({ type: "set_answer_ready", ready: false });
    }
    return;
  }
  const target = event.target.closest("[data-vote]");
  if (!target) {
    return;
  }
  send({ type: "vote", value: target.getAttribute("data-vote") });
});

els.participantGrid.addEventListener("input", (event) => {
  if (event.target.id !== "shortAnswerInput") return;
  appState.answerDraft = event.target.value.slice(0, 128);
  appState.answerDraftDirty = true;
  const counter = document.getElementById("answerCharacterCount");
  if (counter) counter.textContent = String(appState.answerDraft.length);
});

els.toggleVotesButton.addEventListener("click", () => {
  send({ type: "toggle_votes" });
});

els.clearVotesButton.addEventListener("click", () => {
  if (activeSession().response_mode === "short_answer") {
    appState.answerDraft = "";
    appState.answerDraftDirty = false;
  }
  send({ type: "clear_votes" });
});

els.openSessionButton.addEventListener("click", () => {
  send({ type: "set_session_open", open: true });
});

els.closeSessionButton.addEventListener("click", () => {
  send({ type: "set_session_open", open: false });
});

els.pointingModeButton.addEventListener("click", () => {
  send({ type: "set_response_mode", mode: "pointing" });
});

els.shortAnswerModeButton.addEventListener("click", () => {
  send({ type: "set_response_mode", mode: "short_answer" });
});

els.participantGrid.addEventListener("click", (event) => {
  const target = event.target.closest("[data-kick]");
  if (!target) {
    return;
  }
  const name = target.getAttribute("data-name") || "this user";
  if (!window.confirm("Kick " + name + " from the session?")) {
    return;
  }
  send({ type: "kick_user", client_id: Number(target.getAttribute("data-kick")) });
});

const savedName = window.localStorage.getItem(nameStorageKey);
if (savedName) {
  els.nameInput.value = savedName;
}

render();
connect();
