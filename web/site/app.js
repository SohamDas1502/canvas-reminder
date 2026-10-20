// Canvas Reminder web app: sign in with an emailed code, a four-step setup, then a dashboard.
// No framework and no build step: each view is a function that renders into #app.

const app = document.getElementById("app");
const who = document.getElementById("who");
const SESSION_KEY = "cr.session";

const LEAD_OPTIONS = [
  ["7d", "1 week"], ["3d", "3 days"], ["2d", "2 days"], ["1d", "1 day"],
  ["12h", "12 hours"], ["6h", "6 hours"], ["2h", "2 hours"], ["1h", "1 hour"],
];
const MAX_LEADS = 5;
const leadLabel = (value) => (LEAD_OPTIONS.find(([v]) => v === value) || [value, value])[1];

const state = { config: null, me: null, pending: null, appleToken: null, preview: null };

// ---------- small helpers ----------------------------------------------------------------

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function render(html) {
  app.innerHTML = html;
  const focus = app.querySelector("[autofocus]");
  if (focus) focus.focus();
}

function busy(button, isBusy, label) {
  if (!button) return;
  if (isBusy) { button.dataset.label = button.textContent; button.textContent = label || "Working…"; }
  else if (button.dataset.label) button.textContent = button.dataset.label;
  button.disabled = isBusy;
}

function notice(el, kind, text) {
  el.className = `notice ${kind}`;
  el.textContent = text;
  el.classList.toggle("hidden", !text);
}

function store(key, value) {
  try { value === null ? localStorage.removeItem(key) : localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode */ }
}
function load(key) {
  try { return JSON.parse(localStorage.getItem(key)); } catch { return null; }
}

async function copy(text, button) {
  try { await navigator.clipboard.writeText(text); button.textContent = "Copied"; }
  catch { button.textContent = "Select and copy"; }
  setTimeout(() => (button.textContent = "Copy"), 1600);
}

function copyBox(value) {
  return `<div class="copy"><code>${esc(value)}</code><button type="button" class="secondary" data-copy="${esc(value)}">Copy</button></div>`;
}

app.addEventListener("click", (event) => {
  const button = event.target.closest("[data-copy]");
  if (button) copy(button.dataset.copy, button);
});

function ago(iso) {
  if (!iso) return "not yet";
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  return hours < 48 ? `${hours} h ago` : `${Math.round(hours / 24)} days ago`;
}

// ---------- sign-in (Cognito, email one-time code) ----------------------------------------

async function cognito(action, body) {
  const endpoint = state.config.cognitoEndpoint || `https://cognito-idp.${state.config.region}.amazonaws.com/`;
  const response = await fetch(endpoint, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-amz-json-1.1",
      "X-Amz-Target": `AWSCognitoIdentityProviderService.${action}`,
    },
    body: JSON.stringify({ ClientId: state.config.userPoolClientId, ...body }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(friendlyAuthError(data));
    error.code = String(data.__type || "").split("#").pop();
    throw error;
  }
  return data;
}

function friendlyAuthError(data) {
  const code = String(data.__type || "").split("#").pop();
  return {
    CodeMismatchException: "That code isn't right. Check the latest email and try again.",
    ExpiredCodeException: "That code has expired. Go back and send a new one.",
    NotAuthorizedException: "That code didn't work. Go back and send a new one.",
    LimitExceededException: "Too many attempts. Wait a few minutes and try again.",
    TooManyRequestsException: "Too many attempts. Wait a few minutes and try again.",
    InvalidParameterException: "Check the email address and try again.",
  }[code] || "Sign-in didn't work. Please try again.";
}

function randomPassword() {
  // Never shown or used: sign-in is by emailed code. Some user pools still require one at sign-up.
  const bytes = crypto.getRandomValues(new Uint8Array(24));
  return "Aa1!" + Array.from(bytes, (b) => b.toString(36).padStart(2, "0")).join("");
}

async function sendCode(email) {
  try {
    const created = await cognito("SignUp", {
      Username: email,
      Password: randomPassword(),
      UserAttributes: [{ Name: "email", Value: email }],
    });
    return { mode: "confirm", email, session: created.Session };
  } catch (error) {
    if (error.code !== "UsernameExistsException") throw error;
  }
  try {
    const started = await cognito("InitiateAuth", {
      AuthFlow: "USER_AUTH",
      AuthParameters: { USERNAME: email, PREFERRED_CHALLENGE: "EMAIL_OTP" },
    });
    return { mode: "otp", email, session: started.Session };
  } catch (error) {
    if (error.code !== "UserNotConfirmedException") throw error;
    await cognito("ResendConfirmationCode", { Username: email });
    return { mode: "confirm", email, session: null };
  }
}

async function checkCode(pending, code) {
  if (pending.mode === "confirm") {
    const confirmed = await cognito("ConfirmSignUp", {
      Username: pending.email,
      ConfirmationCode: code,
      ...(pending.session ? { Session: pending.session } : {}),
    });
    if (!confirmed.Session) return null; // confirmed, but a sign-in code is still needed
    const signedIn = await cognito("InitiateAuth", {
      AuthFlow: "USER_AUTH",
      AuthParameters: { USERNAME: pending.email },
      Session: confirmed.Session,
    });
    return signedIn.AuthenticationResult;
  }
  const answered = await cognito("RespondToAuthChallenge", {
    ChallengeName: "EMAIL_OTP",
    Session: pending.session,
    ChallengeResponses: { USERNAME: pending.email, EMAIL_OTP_CODE: code },
  });
  return answered.AuthenticationResult;
}

function saveTokens(result, refreshToken) {
  const session = {
    idToken: result.IdToken,
    refreshToken: result.RefreshToken || refreshToken,
    expiresAt: Date.now() + (result.ExpiresIn || 3600) * 1000,
  };
  store(SESSION_KEY, session);
  return session;
}

async function idToken() {
  const session = load(SESSION_KEY);
  if (!session) return null;
  if (Date.now() < session.expiresAt - 60000) return session.idToken;
  try {
    const refreshed = await cognito("InitiateAuth", {
      AuthFlow: "REFRESH_TOKEN_AUTH",
      AuthParameters: { REFRESH_TOKEN: session.refreshToken },
    });
    return saveTokens(refreshed.AuthenticationResult, session.refreshToken).idToken;
  } catch {
    store(SESSION_KEY, null);
    return null;
  }
}

function signOut() {
  store(SESSION_KEY, null);
  state.me = null;
  who.innerHTML = "";
  viewSignIn();
}

// ---------- API -----------------------------------------------------------------------------

async function api(method, path, body) {
  const token = await idToken();
  if (!token) { signOut(); throw new Error("Please sign in again."); }
  const response = await fetch(`/api${path}`, {
    method,
    headers: { Authorization: `Bearer ${token}`, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401) { signOut(); throw new Error("Please sign in again."); }
  if (!response.ok) throw new Error(data.error || "Something went wrong. Please try again.");
  return data;
}

// ---------- views: sign in ------------------------------------------------------------------

function viewSignIn() {
  render(`
    <section class="card">
      <h1>Never miss a Canvas due date.</h1>
      <p class="lede">Paste your Canvas calendar link once. We check it every 30 minutes and remind you before each assignment is due, by email, phone notification or Apple Reminders.</p>
      <form id="email-form" class="row" novalidate>
        <label class="field" style="flex:1 1 260px">Your email
          <input id="email" type="email" autocomplete="email" required placeholder="you@student.unimelb.edu.au" autofocus>
        </label>
        <button class="primary" type="submit" style="align-self:flex-end">Email me a code</button>
      </form>
      <p class="small muted">No password. We email you a 6-digit code each time you sign in.</p>
      <div id="msg" class="hidden"></div>
    </section>`);
  const form = document.getElementById("email-form");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const email = document.getElementById("email").value.trim().toLowerCase();
    const msg = document.getElementById("msg");
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) return notice(msg, "error", "Enter a valid email address.");
    const button = form.querySelector("button");
    busy(button, true, "Sending…");
    try {
      state.pending = await sendCode(email);
      viewCode();
    } catch (error) {
      notice(msg, "error", error.message);
      busy(button, false);
    }
  });
}

function viewCode() {
  render(`
    <section class="card">
      <h2>Check your email</h2>
      <p>We sent a 6-digit code to <strong>${esc(state.pending.email)}</strong>. It can take a minute; check spam if it doesn't arrive.</p>
      <form id="code-form" class="row" novalidate>
        <label class="field" style="flex:1 1 200px">Code
          <input id="code" class="code" type="text" inputmode="numeric" autocomplete="one-time-code" maxlength="8" required autofocus>
        </label>
        <button class="primary" type="submit" style="align-self:flex-end">Sign in</button>
      </form>
      <div id="msg" class="hidden"></div>
      <button type="button" class="link" id="back">Use a different email</button>
    </section>`);
  document.getElementById("back").addEventListener("click", viewSignIn);
  const form = document.getElementById("code-form");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const code = document.getElementById("code").value.replace(/\s/g, "");
    const button = form.querySelector("button");
    const msg = document.getElementById("msg");
    busy(button, true, "Checking…");
    try {
      const result = await checkCode(state.pending, code);
      if (!result) {
        state.pending = await sendCode(state.pending.email); // email confirmed; now a sign-in code
        notice(msg, "ok", "Email confirmed. We've sent one more code to finish signing in.");
        busy(button, false);
        document.getElementById("code").value = "";
        return;
      }
      saveTokens(result);
      state.pending = null;
      await start();
    } catch (error) {
      notice(msg, "error", error.message);
      busy(button, false);
    }
  });
}

// ---------- views: setup -----------------------------------------------------------------

function stepsBar(step) {
  return `<div class="steps-bar" aria-hidden="true">${[1, 2, 3, 4].map((n) => `<span class="${n <= step ? "on" : ""}"></span>`).join("")}</div>
          <div class="step-label">Step ${step} of 4</div>`;
}

function feedCard({ step, done }) {
  return `
    <section class="card">
      ${step ? stepsBar(1) : ""}
      <h2>${done ? "Your Canvas calendar" : "Paste your Canvas calendar link"}</h2>
      <ol class="howto">
        <li>In Canvas, open <strong>Calendar</strong> (left menu).</li>
        <li>At the bottom right, click <strong>Calendar Feed</strong>.</li>
        <li>Copy the whole link and paste it here.</li>
      </ol>
      <p class="small muted">The link lets anyone read your Canvas calendar, so treat it like a password. We store it encrypted and never show it again.</p>
      <form id="feed-form" class="row" novalidate>
        <label class="field" style="flex:1 1 300px">Calendar feed link
          <input id="feed" type="url" inputmode="url" autocomplete="off" spellcheck="false" placeholder="https://canvas…/feeds/calendars/user_….ics" ${step ? "autofocus" : ""}>
        </label>
        <button class="primary" type="submit" style="align-self:flex-end">${done ? "Replace link" : "Check link"}</button>
      </form>
      <div id="feed-msg" class="hidden"></div>
      <div id="preview"></div>
    </section>`;
}

function bindFeedForm(onSaved) {
  const form = document.getElementById("feed-form");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const msg = document.getElementById("feed-msg");
    const url = document.getElementById("feed").value.trim();
    if (!url) return notice(msg, "error", "Paste your Canvas calendar feed link first.");
    const button = form.querySelector("button");
    busy(button, true, "Checking…");
    try {
      const result = await api("PUT", "/me/feed", { url });
      document.getElementById("feed").value = "";
      state.preview = result;
      state.me.feed_connected = true;
      notice(msg, "ok", result.upcoming_count
        ? `Connected. We found ${result.upcoming_count} upcoming assignment${result.upcoming_count === 1 ? "" : "s"}.`
        : "Connected. There are no upcoming assignments right now; we'll keep checking.");
      document.getElementById("preview").innerHTML = previewList(result.preview);
      onSaved(result);
    } catch (error) {
      notice(msg, "error", error.message);
    } finally {
      busy(button, false);
    }
  });
}

function previewList(items) {
  if (!items || !items.length) return "";
  return `<ul class="list">${items.map((a) => `
    <li><div class="what">${esc(a.title)}<div class="course">${esc(a.course)}</div></div><div class="when">${esc(a.due)}</div></li>`).join("")}</ul>`;
}

function channelsForm(me) {
  const on = (c) => me.channels.includes(c);
  const choice = (id, title, summary, guide) => `
    <div class="choice ${on(id) ? "on" : ""}" data-choice="${id}">
      <label class="choice-head">
        <input type="checkbox" name="channel" value="${id}" ${on(id) ? "checked" : ""}>
        <span><span class="title">${title}</span><span class="small muted">${summary}</span></span>
      </label>
      <div class="guide ${on(id) ? "" : "hidden"}">${guide}</div>
    </div>`;
  const ntfyLink = `${me.ntfy_server}/${me.ntfy_topic}`;
  return `
    <div class="choices" style="display:flex;flex-direction:column;gap:10px">
      ${choice("email", "Email", `Reminders go to ${esc(me.email)}.`,
        `<p>Nothing to set up. The first one might land in spam; mark it "not spam" so the rest arrive.</p>`)}
      ${choice("ntfy", "Phone notifications", "Instant push notifications with the free ntfy app.", `
        <ol class="howto">
          <li>Install <strong>ntfy</strong>: <a href="https://apps.apple.com/app/ntfy/id1625396347" target="_blank" rel="noopener">iPhone</a> · <a href="https://play.google.com/store/apps/details?id=io.heckel.ntfy" target="_blank" rel="noopener">Android</a></li>
          <li>Tap <strong>+</strong> and subscribe to this topic (it's private to you):</li>
        </ol>
        ${copyBox(me.ntfy_topic)}
        <p class="small muted">On your phone you can also open <a href="${esc(ntfyLink)}" target="_blank" rel="noopener">this link</a> and subscribe from there.</p>`)}
      ${choice("apple", "Apple Reminders", "A reminder in the Reminders app on your iPhone, via a Shortcut.", `
        <p>A Shortcut on your iPhone checks for new assignments a few times a day and adds them to Reminders.</p>
        <div id="apple-setup">${appleSetup(me)}</div>`)}
    </div>`;
}

function appleSetup(me) {
  if (state.appleToken) {
    return `
      <ol class="howto">
        <li>In <strong>Reminders</strong>, create a list called <strong>Canvas</strong>.</li>
        <li>In <strong>Shortcuts</strong>, add <strong>Get Contents of URL</strong> with this address:</li>
      </ol>
      ${copyBox(me.apple_feed_url)}
      <ol class="howto" start="3">
        <li>Under <em>Headers</em>, add <strong>Authorization</strong> with this value (shown once, so copy it now):</li>
      </ol>
      ${copyBox(`Bearer ${state.appleToken}`)}
      <ol class="howto" start="4">
        <li>For each item in <code>items</code>: find a reminder whose notes contain its <code>tag</code>; update it, or add one with its <code>title</code>, <code>due</code>, <code>alert</code> and <code>notes</code>.</li>
        <li>In <strong>Automation</strong>, run the shortcut daily at 8 am, 1 pm and 6 pm.</li>
      </ol>
      <p class="small"><a href="/help.html#apple" target="_blank" rel="noopener">Full step-by-step Shortcut guide</a></p>`;
  }
  return `
    <p class="small muted">${me.apple_token_set ? "You already have a Shortcut token. Making a new one stops the old one working." : "You'll need a personal token for the Shortcut."}</p>
    <button type="button" class="secondary" id="apple-token">${me.apple_token_set ? "Make a new token" : "Create my Shortcut token"}</button>`;
}

function bindChannels() {
  app.querySelectorAll("[data-choice] input").forEach((box) => {
    box.addEventListener("change", () => {
      const card = box.closest("[data-choice]");
      card.classList.toggle("on", box.checked);
      card.querySelector(".guide").classList.toggle("hidden", !box.checked);
    });
  });
}

// Bound once: the button is re-created on every render.
app.addEventListener("click", async (event) => {
  if (event.target.id !== "apple-token") return;
  event.preventDefault();
  busy(event.target, true, "Creating…");
  try {
    state.appleToken = (await api("POST", "/me/apple-token")).token;
    state.me.apple_token_set = true;
    document.getElementById("apple-setup").innerHTML = appleSetup(state.me);
  } catch (error) {
    busy(event.target, false);
    alertBox(error.message);
  }
});

function timingForm(me) {
  const chosen = new Set(me.lead_times);
  const custom = me.lead_times.filter((t) => !LEAD_OPTIONS.some(([v]) => v === t));
  return `
    <div style="display:flex;flex-direction:column;gap:10px">
      <h3>How long before each due date?</h3>
      <p class="small muted">Pick up to ${MAX_LEADS}. Every app you chose reminds you at each time.</p>
      <div class="chips">${LEAD_OPTIONS.map(([value, label]) => `
        <label class="chip"><input type="checkbox" name="lead" value="${value}" ${chosen.has(value) ? "checked" : ""}><span>${label}</span></label>`).join("")}
      </div>
      <label class="field" style="max-width:280px">Other times (optional)
        <input id="custom-leads" type="text" placeholder="e.g. 4h, 5d" value="${esc(custom.join(", "))}">
      </label>
    </div>
    <div style="display:flex;flex-direction:column;gap:10px">
      <h3>A summary as well?</h3>
      <div class="chips">
        ${[["off", "No summary"], ["daily", "Every morning"], ["weekly", "Monday mornings"]].map(([value, label]) => `
          <label class="chip"><input type="radio" name="digest" value="${value}" ${me.digest === value ? "checked" : ""}><span>${label}</span></label>`).join("")}
      </div>
      <p class="small muted">Sent at 8 am Melbourne time: the next 2 days (every morning) or the week ahead (Mondays).</p>
    </div>`;
}

function readPreferences() {
  const leadInputs = app.querySelectorAll("input[name=lead]");
  const leads = [...app.querySelectorAll("input[name=lead]:checked")].map((x) => x.value);
  const custom = (document.getElementById("custom-leads")?.value || "")
    .split(/[,\s]+/).map((x) => x.trim().toLowerCase()).filter(Boolean);
  const channels = [...app.querySelectorAll("input[name=channel]:checked")].map((x) => x.value);
  return {
    channels: channels.length ? channels : state.me.channels,
    // Pages without the timing form keep the saved times.
    lead_times: leadInputs.length ? [...new Set([...leads, ...custom])] : state.me.lead_times,
    digest: app.querySelector("input[name=digest]:checked")?.value || state.me.digest,
    paused: state.me.status === "paused",
  };
}

async function savePreferences(patch, msg) {
  const prefs = { ...readPreferences(), ...patch };
  if (prefs.lead_times.length > MAX_LEADS) {
    notice(msg, "error", `Pick at most ${MAX_LEADS} reminder times.`);
    return false;
  }
  try {
    state.me = { ...state.me, ...(await api("PUT", "/me/preferences", prefs)) };
    return true;
  } catch (error) {
    notice(msg, "error", error.message);
    return false;
  }
}

function alertBox(text) {
  const msg = document.getElementById("msg") || document.getElementById("feed-msg");
  if (msg) notice(msg, "error", text);
}

function viewStep1() {
  render(feedCard({ step: true, done: false }) + `<div class="row"><button class="primary hidden" id="next">Next: where to remind you</button></div>`);
  bindFeedForm(() => document.getElementById("next").classList.remove("hidden"));
  document.getElementById("next").addEventListener("click", viewStep2);
}

function viewStep2() {
  render(`
    <section class="card">
      ${stepsBar(2)}
      <h2>Where should we remind you?</h2>
      <p class="muted">Pick as many as you like. Google Calendar and Microsoft To Do are coming soon.</p>
      ${channelsForm(state.me)}
      <div id="msg" class="hidden"></div>
      <div class="row"><button class="primary" id="next">Next: when</button><button class="secondary" id="back">Back</button></div>
    </section>`);
  bindChannels();
  document.getElementById("back").addEventListener("click", viewStep1);
  document.getElementById("next").addEventListener("click", async (event) => {
    const msg = document.getElementById("msg");
    if (!app.querySelector("input[name=channel]:checked")) return notice(msg, "error", "Pick at least one.");
    busy(event.target, true, "Saving…");
    const ok = await savePreferences({ channels: [...app.querySelectorAll("input[name=channel]:checked")].map((x) => x.value) }, msg);
    busy(event.target, false);
    if (ok) viewStep3();
  });
}

function viewStep3() {
  render(`
    <section class="card">
      ${stepsBar(3)}
      <h2>When should we remind you?</h2>
      ${timingForm(state.me)}
      <div id="msg" class="hidden"></div>
      <div class="row"><button class="primary" id="next">Next: test it</button><button class="secondary" id="back">Back</button></div>
    </section>`);
  document.getElementById("back").addEventListener("click", viewStep2);
  document.getElementById("next").addEventListener("click", async (event) => {
    const msg = document.getElementById("msg");
    busy(event.target, true, "Saving…");
    const ok = await savePreferences({}, msg);
    busy(event.target, false);
    if (ok) viewStep4();
  });
}

function viewStep4() {
  render(`
    <section class="card">
      ${stepsBar(4)}
      <h2>Send yourself a test</h2>
      <p>We'll send one test reminder to each place you chose, so you know it works.</p>
      <div class="row"><button class="primary" id="test">Send a test</button></div>
      <div id="msg" class="hidden"></div>
      <div class="row"><button class="primary hidden" id="done">Go to my dashboard</button><button class="link" id="skip">Skip</button></div>
    </section>`);
  const goDashboard = () => viewDashboard();
  document.getElementById("skip").addEventListener("click", goDashboard);
  document.getElementById("done").addEventListener("click", goDashboard);
  document.getElementById("test").addEventListener("click", (event) => sendTest(event.target, () => {
    document.getElementById("done").classList.remove("hidden");
    document.getElementById("skip").classList.add("hidden");
  }));
}

async function sendTest(button, after) {
  const msg = document.getElementById("msg");
  busy(button, true, "Sending…");
  try {
    const { results } = await api("POST", "/me/test");
    const names = { email: "Email", ntfy: "Phone notification" };
    const lines = Object.entries(results).map(([k, v]) => `${names[k] || k}: ${v === "sent" ? "sent" : "failed"}`);
    const failed = Object.values(results).includes("failed");
    const note = state.me.channels.includes("apple") ? " Apple Reminders updates the next time your Shortcut runs." : "";
    notice(msg, failed ? "warn" : "ok", (lines.join(" · ") || "Nothing to send to.") + note);
    if (after) after();
  } catch (error) {
    notice(msg, "error", error.message);
  } finally {
    busy(button, false);
  }
}

// ---------- views: dashboard -------------------------------------------------------------

async function viewDashboard() {
  const me = state.me;
  const paused = me.status === "paused";
  const dotClass = me.last_error ? "error" : paused ? "paused" : "";
  const statusText = paused
    ? "Paused. You won't get reminders until you resume."
    : `On. We check your Canvas calendar every 30 minutes (last check: ${ago(me.last_run_at)}).`;
  render(`
    <section class="card">
      <div class="status"><span class="dot ${dotClass}"></span><strong>${esc(statusText)}</strong></div>
      ${me.last_error ? `<div class="notice error">The last check had a problem: ${esc(me.last_error)} If your Canvas link changed, replace it below.</div>` : ""}
      <div class="row">
        <button class="secondary" id="test">Send a test</button>
        <button class="secondary" id="pause">${paused ? "Resume reminders" : "Pause reminders"}</button>
      </div>
      <div id="msg" class="hidden"></div>
    </section>

    <section class="card">
      <h2>Coming up</h2>
      <div id="assignments"><p class="muted">Loading…</p></div>
    </section>

    <section class="card">
      <details class="section"><summary>Where you're reminded: ${esc(me.channels.map((c) => ({ email: "Email", ntfy: "Phone", apple: "Apple Reminders" })[c]).join(", "))}</summary>
        <div>${channelsForm(me)}<div class="row"><button class="primary" id="save-channels">Save</button></div><div id="channels-msg" class="hidden"></div></div>
      </details>
    </section>

    <section class="card">
      <details class="section"><summary>When: ${esc(me.lead_times.map(leadLabel).join(", "))} before${me.digest !== "off" ? ` · ${me.digest} summary` : ""}</summary>
        <div>${timingForm(me)}<div class="row"><button class="primary" id="save-timing">Save</button></div><div id="timing-msg" class="hidden"></div></div>
      </details>
    </section>

    <section class="card">
      <details class="section"><summary>Your Canvas link</summary><div id="feed-slot"></div></details>
    </section>

    <section class="card">
      <details class="section"><summary>Delete my account</summary>
        <div>
          <p>This deletes your Canvas link, settings, assignment history and sign-in. Reminders stop straight away. It can't be undone.</p>
          <label class="field" style="max-width:260px">Type DELETE to confirm<input id="confirm-delete" type="text" autocomplete="off"></label>
          <div class="row"><button class="danger" id="delete">Delete everything</button></div>
          <div id="delete-msg" class="hidden"></div>
        </div>
      </details>
    </section>`);

  document.getElementById("feed-slot").innerHTML = feedCard({ step: false, done: true }).replace(/^\s*<section class="card">|<\/section>\s*$/g, "");
  bindFeedForm(() => {});
  bindChannels();
  document.getElementById("test").addEventListener("click", (event) => sendTest(event.target));
  document.getElementById("pause").addEventListener("click", async (event) => {
    busy(event.target, true, "Saving…");
    if (await savePreferences({ paused: !paused, channels: me.channels, lead_times: me.lead_times, digest: me.digest }, document.getElementById("msg"))) viewDashboard();
    else busy(event.target, false);
  });
  document.getElementById("save-channels").addEventListener("click", async (event) => {
    const msg = document.getElementById("channels-msg");
    const channels = [...app.querySelectorAll("input[name=channel]:checked")].map((x) => x.value);
    if (!channels.length) return notice(msg, "error", "Pick at least one.");
    busy(event.target, true, "Saving…");
    if (await savePreferences({ channels, lead_times: me.lead_times, digest: me.digest }, msg)) viewDashboard();
    else busy(event.target, false);
  });
  document.getElementById("save-timing").addEventListener("click", async (event) => {
    busy(event.target, true, "Saving…");
    if (await savePreferences({ channels: me.channels }, document.getElementById("timing-msg"))) viewDashboard();
    else busy(event.target, false);
  });
  document.getElementById("delete").addEventListener("click", async (event) => {
    const msg = document.getElementById("delete-msg");
    if (document.getElementById("confirm-delete").value.trim() !== "DELETE") return notice(msg, "error", "Type DELETE to confirm.");
    busy(event.target, true, "Deleting…");
    try {
      await api("DELETE", "/me");
      store(SESSION_KEY, null);
      state.me = null;
      who.innerHTML = "";
      render(`<section class="card"><h2>Your account is deleted</h2><p>Everything we stored about you is gone. Thanks for trying Canvas Reminder.</p></section>`);
    } catch (error) {
      notice(msg, "error", error.message);
      busy(event.target, false);
    }
  });

  try {
    const { assignments } = await api("GET", "/me/assignments");
    document.getElementById("assignments").innerHTML = assignments.length
      ? `<ul class="list">${assignments.map((a) => `
          <li><div class="what">${a.url ? `<a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.title)}</a>` : esc(a.title)}<div class="course">${esc(a.course)}${a.status === "removed" ? " · removed from Canvas" : ""}</div></div><div class="when">${esc(a.due)}</div></li>`).join("")}</ul>`
      : `<p class="muted">${me.last_run_at ? "Nothing due. Enjoy it." : "Your first check runs within 30 minutes; your assignments will appear here."}</p>`;
  } catch (error) {
    document.getElementById("assignments").innerHTML = `<p class="notice error">${esc(error.message)}</p>`;
  }
}

// ---------- start --------------------------------------------------------------------------

async function start() {
  state.me = await api("GET", "/me");
  who.innerHTML = `<span>${esc(state.me.email)}</span><button class="link" id="sign-out">Sign out</button>`;
  document.getElementById("sign-out").addEventListener("click", signOut);
  if (!state.me.feed_connected) viewStep1();
  else viewDashboard();
}

(async function boot() {
  try {
    state.config = await (await fetch("/config.json", { cache: "no-store" })).json();
  } catch {
    render(`<section class="card"><p class="notice error">The site isn't configured yet (missing config.json).</p></section>`);
    return;
  }
  if (await idToken()) {
    try { await start(); return; } catch { /* fall through to sign-in */ }
  }
  viewSignIn();
})();
