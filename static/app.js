const bootstrap = {
  appId: document.documentElement.dataset.appId || "root",
  appLabel: document.documentElement.dataset.appLabel || "",
  basePath: document.documentElement.dataset.basePath || "/",
  healthzPath: document.documentElement.dataset.healthzPath || "/healthz",
  statePath: document.documentElement.dataset.statePath || "/api/state",
  storageKeyPrefix: document.documentElement.dataset.storageKeyPrefix || "smallos-scrum-poker-root",
  wsPath: document.documentElement.dataset.wsPath || "/ws",
};

const defaultVoteOptions = ["0", "0.5", "1", "2", "3", "5", "8", "13", "21", "40", "60", "100", "?", "coffee"];

// Keep the Render free-tier instance awake: one client per 3-minute window pings /healthz.
// The slot rotates through joined participants so the load is shared naturally.
const KEEP_ALIVE_INTERVAL_MS = 3 * 60 * 1000;
const KEEP_ALIVE_CHECK_MS = 30 * 1000; // how often each client evaluates whether it's their turn
let keepAliveTimer = null;

function checkKeepAlive() {
  const me = currentViewer();
  const isAdmin = viewerIsAdmin();
  const joined = viewerHasJoined();

  if (!appState.connected) return;

  // Admins that haven't joined a named slot keep the server alive on their own.
  if (isAdmin && !joined) {
    fetch(bootstrap.healthzPath).catch(() => { });
    return;
  }

  // Among joined participants, rotate who pings based on a shared time slot.
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
  // Run once immediately so the very first open counts, then poll every 30s.
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

const appState = {
  adminFormVisible: false,
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
  joinButton: document.getElementById("joinButton"),
  joinForm: document.getElementById("joinForm"),
  joinHelp: document.getElementById("joinHelp"),
  nameInput: document.getElementById("nameInput"),
  noticeArea: document.getElementById("noticeArea"),
  openSessionButton: document.getElementById("openSessionButton"),
  participantCount: document.getElementById("participantCount"),
  participantGrid: document.getElementById("participantGrid"),
  serverTime: document.getElementById("serverTime"),
  sessionStatus: document.getElementById("sessionStatus"),
  socketChip: document.getElementById("socketChip"),
  statusLine: document.getElementById("statusLine"),
  toggleVotesButton: document.getElementById("toggleVotesButton"),
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
  if (!params.toString()) {
    return baseUrl;
  }
  return baseUrl + "?" + params.toString();
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

function currentVoteOptions() {
  const session = activeSession();
  return Array.isArray(session.vote_options) && session.vote_options.length
    ? session.vote_options
    : defaultVoteOptions;
}

function renderVotes() {
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

function getInitials(name) {
  const parts = name.trim().split(/\s+/);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return name.slice(0, 2).toUpperCase();
}

function renderParticipants() {
  const session = activeSession();
  const participants = session.participants || [];
  const votesVisible = Boolean(session.votes_visible);
  const canKick = viewerIsAdmin();

  if (!participants.length) {
    els.participantGrid.innerHTML = '<div class="empty-board">No one has joined the table yet.</div>';
    return;
  }

  els.participantGrid.innerHTML = participants.map((participant) => {
    let voteChipClass = "vote-chip waiting";
    let voteChipContent = "\u2014";

    if (participant.vote !== null && participant.vote !== undefined) {
      voteChipClass = "vote-chip revealed";
      voteChipContent = escapeHtml(participant.vote === "coffee" ? "\u2615" : participant.vote);
    } else if (participant.has_voted) {
      voteChipClass = "vote-chip voted";
      voteChipContent = "\u2713";
    }

    const meta = participant.is_self
      ? "You"
      : !participant.is_connected
        ? "Reconnecting\u2026"
        : participant.has_voted
          ? (votesVisible ? "Revealed" : "Voted")
          : "Deciding\u2026";

    const adminBadge = participant.is_admin ? '<span class="badge">Admin</span>' : "";
    const kickButton = canKick && !participant.is_self
      ? '<button class="button-ghost kick-button" type="button" data-kick="' + participant.client_id + '" data-name="' + escapeHtml(participant.name) + '">Kick</button>'
      : "";

    return [
      '<div class="participant-row' + (participant.is_self ? " self" : "") + '">',
      '<div class="participant-avatar">' + escapeHtml(getInitials(participant.name)) + '</div>',
      '<div class="participant-info">',
      '<div class="participant-name-row">',
      '<span class="participant-name">' + escapeHtml(participant.name) + '</span>',
      adminBadge,
      '</div>',
      '<span class="participant-meta">' + escapeHtml(meta) + '</span>',
      '</div>',
      kickButton,
      '<div class="' + voteChipClass + '">' + voteChipContent + '</div>',
      '</div>',
    ].join("");
  }).join("");
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
    els.noticeArea.innerHTML = buildNotice("Live round ready. You can vote, reveal the board, or discard the current round.", "info");
    return;
  }

  els.noticeArea.innerHTML = buildNotice("Pick a display name to join the table and start voting.", "info");
}

function renderSession() {
  const session = activeSession();
  const sessionOpen = session.session_open !== false;
  const participants = session.participants || [];
  const me = currentViewer();
  const votesCast = participants.filter((participant) => participant.has_voted).length;
  const joined = viewerHasJoined();
  const isAdmin = viewerIsAdmin();
  const adminAvailable = Boolean(session.admin_auth_enabled);

  els.sessionStatus.textContent = sessionOpen ? "Joining is open" : "Joining is paused";
  els.sessionStatus.className = "session-status " + (sessionOpen ? "open" : "closed");
  els.statusLine.textContent = appState.statusLine;
  els.participantCount.textContent = String(session.participant_count || 0);
  els.votesCast.textContent = String(votesCast);
  els.connectedCount.textContent = String(session.connected_count || 0);
  els.serverTime.textContent = session.server_time || "-";
  els.joinButton.disabled = !appState.connected || (!sessionOpen && !joined && !isAdmin);
  els.joinButton.textContent = joined ? "Update Name" : "Join Session";
  els.toggleVotesButton.disabled = !appState.connected;
  els.toggleVotesButton.textContent = session.votes_visible ? "Hide Votes" : "Show Votes";
  els.clearVotesButton.disabled = !appState.connected;
  els.socketChip.textContent = appState.connected ? "Socket connected" : "Socket reconnecting";
  els.socketChip.className = "status-chip" + (appState.connected ? "" : " offline");

  if (joined) {
    els.joinHelp.textContent = "You are joined as " + me.name + ". Votes stay hidden until someone reveals them.";
  } else if (sessionOpen) {
    els.joinHelp.textContent = "Joining is open. Pick a name and hop into the session.";
  } else {
    els.joinHelp.textContent = "Joining is currently disabled for new non-admin participants.";
  }

  if (!participants.length) {
    els.boardSummary.textContent = "Waiting for the first participant.";
  } else if (session.votes_visible) {
    els.boardSummary.textContent = "Votes are revealed for " + participants.length + " participant(s).";
  } else {
    els.boardSummary.textContent = votesCast + " of " + participants.length + " participant(s) have voted.";
  }

  els.adminUnlockButton.hidden = !adminAvailable || isAdmin;
  els.adminAuthPanel.hidden = !adminAvailable || isAdmin || !appState.adminFormVisible;
  els.adminChip.hidden = !isAdmin;
  els.adminControlsPanel.hidden = !isAdmin;
  els.openSessionButton.disabled = !appState.connected || !isAdmin || sessionOpen;
  els.closeSessionButton.disabled = !appState.connected || !isAdmin || !sessionOpen;

  if (!adminAvailable) {
    els.adminStatus.innerHTML = 'Admin passphrase is not configured on this server.';
  } else if (isAdmin) {
    els.adminStatus.innerHTML = 'Admin mode is enabled for this browser session.';
  } else {
    els.adminStatus.innerHTML = 'Click <strong>Become Admin</strong> to unlock session controls with <code>ADMIN_PASSPHRASE</code>.';
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
    appState.statusLine = "Connected.";; scheduleKeepAlive(); render();
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
      appState.session = message.state;
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
      render();
      return;
    }

    if (message.type === "notice") {
      setFlash(message.message || "Success.", message.kind || "success");
      render();
      return;
    }

    if (message.type === "error") {
      setFlash(message.message || "Unknown server error.", "error");
      render();
    }
  });

  socket.addEventListener("close", () => {
    appState.connected = false;
    appState.statusLine = "Disconnected. Attempting to reconnect...";; cancelKeepAlive(); render();
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
});

els.adminUnlockButton.addEventListener("click", () => {
  appState.adminFormVisible = !appState.adminFormVisible;
  render();
  if (appState.adminFormVisible) {
    els.adminPassphraseInput.focus();
  }
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
  const target = event.target.closest("[data-vote]");
  if (!target) {
    return;
  }
  send({ type: "vote", value: target.getAttribute("data-vote") });
});

els.toggleVotesButton.addEventListener("click", () => {
  send({ type: "toggle_votes" });
});

els.clearVotesButton.addEventListener("click", () => {
  send({ type: "clear_votes" });
});

els.openSessionButton.addEventListener("click", () => {
  send({ type: "set_session_open", open: true });
});

els.closeSessionButton.addEventListener("click", () => {
  send({ type: "set_session_open", open: false });
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
