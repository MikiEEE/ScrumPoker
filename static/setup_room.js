const form = document.getElementById("setupRoomForm");
const passwordInput = document.getElementById("roomAdminPassphrase");
const confirmInput = document.getElementById("roomAdminPassphraseConfirm");
const statusArea = document.getElementById("setupRoomStatus");
const createButton = document.getElementById("createRoomButton");

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function setStatus(message, kind = "info") {
  statusArea.innerHTML = message
    ? '<div class="notice ' + kind + '">' + escapeHtml(message) + "</div>"
    : "";
}

async function copyRoomUrl(url) {
  if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") {
    return false;
  }
  try {
    await navigator.clipboard.writeText(window.location.origin + url);
    return true;
  } catch (error) {
    return false;
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const password = passwordInput.value.trim();
  const confirmation = confirmInput.value.trim();

  if (!password) {
    setStatus("Choose a room admin password before creating the room.", "error");
    return;
  }

  if (password !== confirmation) {
    setStatus("The room password and confirmation must match.", "error");
    return;
  }

  createButton.disabled = true;
  setStatus("Creating room...", "info");

  try {
    const response = await fetch("/api/rooms", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ admin_passphrase: password }),
    });

    const isJson = (response.headers.get("content-type") || "").includes("application/json");
    const payload = isJson ? await response.json() : null;

    if (!response.ok || !payload) {
      const text = payload && payload.message ? payload.message : await response.text();
      throw new Error(text || "Unable to create room right now.");
    }

    const creatorClaimKey = "smallos-scrum-poker-creator-claim-" + payload.room_id;
    window.sessionStorage.setItem(creatorClaimKey, payload.creator_claim_token);

    const copied = await copyRoomUrl(payload.room_url);
    setStatus(copied ? "Room created. Link copied. Redirecting..." : "Room created. Redirecting...", "success");
    window.setTimeout(() => {
      window.location.assign(payload.room_url);
    }, 300);
  } catch (error) {
    setStatus(error.message || "Unable to create room right now.", "error");
    createButton.disabled = false;
  }
});
