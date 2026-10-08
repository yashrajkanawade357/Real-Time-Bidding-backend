// Bidding Floor - browser client.
//
// The server is the only source of truth. This client:
//   * renders whatever the latest snapshot / event says, and ignores anything
//     with a version at or below the one it already holds;
//   * keeps unanswered bids in an outbox and resends them, with the same
//     request_id, after every reconnect - the server de-duplicates them;
//   * reconnects with exponential backoff and jitter.
"use strict";

(() => {
  // ---------------------------------------------------------------- helpers
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };
  const inr = (n) => (n == null ? "-" : "₹" + Number(n).toLocaleString("en-IN"));
  const pad = (n) => String(n).padStart(2, "0");
  const lotNo = (id) => "LOT " + String(id).padStart(3, "0");
  const clockTime = (d) => d.toLocaleTimeString("en-GB", { hour12: false });
  const shortTime = (d) => d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  const newId = () => (window.crypto && crypto.randomUUID ? crypto.randomUUID()
    : Date.now().toString(36) + Math.random().toString(36).slice(2));
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };

  function hueOf(name) {
    let h = 0;
    for (const ch of name) h = (h * 31 + ch.codePointAt(0)) >>> 0;
    return h % 6;
  }
  function initials(name) {
    const parts = name.split(/[^\p{L}]+/u).filter(Boolean); // "arjun.m" -> AM, "bilal.45" -> B
    return (parts.slice(0, 2).map((p) => p[0]).join("") || "?").toUpperCase();
  }
  function avatar(name, cls = "avatar") {
    const a = el("span", cls, initials(name));
    a.dataset.hue = hueOf(name);
    a.setAttribute("aria-hidden", "true");
    return a;
  }
  // "7m 05s", never "07:05" - a countdown must not look like a time of day.
  function countdown(ms) {
    if (ms <= 0) return "0s";
    const s = Math.ceil(ms / 1000), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    if (h) return `${h}h ${pad(m)}m`;
    if (m) return `${m}m ${pad(s % 60)}s`;
    return `${s}s`;
  }
  function ago(ms) {
    const s = Math.max(0, Math.round(ms / 1000));
    if (s < 5) return "just now";
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    return shortTime(new Date(Date.now() - ms));
  }

  const REASONS = {
    bid_too_low: "Someone got there first",
    auction_closed: "Bidding has closed",
    watch_only: "Add your name to bid",
    invalid_amount: "That isn't a valid amount",
    invalid_request_id: "The bid couldn't be sent",
  };

  // ------------------------------------------------------------------ state
  const S = {
    me: store.get("bidder") || randomName(),
    lots: [],
    lotId: null,
    auction: null,     // latest state we trust, always from the server
    bids: [],          // accepted bids, newest first
    offset: 0,         // server clock minus local clock
    ws: null,
    conn: "connecting",
    attempt: 0,
    retryIn: 0,
    retryTimer: null,
    offline: false,
    reconnects: 0,
    pingAt: 0,
    latency: null,
    outbox: new Map(), // request_id -> amount, until the server answers
    outbidBy: null,
  };

  function randomName() {
    const names = ["asha", "bilal", "chen", "dara", "esha", "farid", "gita", "hiro", "ira", "jonah", "kavya", "leo"];
    const name = names[Math.floor(Math.random() * names.length)] + "." + Math.floor(Math.random() * 90 + 10);
    store.set("bidder", name);
    return name;
  }
  const now = () => Date.now() + S.offset;
  const msLeft = (a) => Date.parse(a.ends_at) - now();

  // ------------------------------------------------------------- the socket
  function connect() {
    clearTimeout(S.retryTimer);
    if (S.lotId == null || S.offline) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const query = S.me ? `?bidder=${encodeURIComponent(S.me)}` : "";
    const sock = new WebSocket(`${proto}://${location.host}/ws/auctions/${S.lotId}${query}`);
    S.ws = sock;
    setConn("connecting");

    sock.onopen = () => {
      if (S.ws !== sock) return;
      S.attempt = 0;
      setConn("live");
      wire("sys", "open", `socket to ${lotNo(S.lotId).toLowerCase()} as ${S.me || "watcher"}`);
      ping();
    };
    sock.onmessage = (e) => { if (S.ws === sock) onMessage(JSON.parse(e.data)); };
    sock.onclose = (e) => {
      if (S.ws !== sock) return; // replaced on purpose
      S.ws = null;
      S.latency = null;
      if (e.code === 4404) { setConn("missing"); wire("sys", "closed", "lot not found (4404)"); return; }
      if (S.offline) { setConn("offline"); return; }
      // Exponential backoff with jitter: a restarting server isn't hit by every tab at once.
      const delay = Math.min(8000, 400 * 2 ** S.attempt) * (0.5 + Math.random() / 2);
      S.attempt += 1;
      S.reconnects += 1;
      S.retryIn = delay;
      setConn("reconnecting");
      wire("sys", "closed", `code ${e.code}${e.reason ? ` (${e.reason})` : ""}, retrying in ${(delay / 1000).toFixed(1)}s`);
      S.retryTimer = setTimeout(connect, delay);
    };
  }

  function disconnect() {
    clearTimeout(S.retryTimer);
    const old = S.ws;
    S.ws = null;
    if (old) old.close(1000);
  }
  function reconnectNow() { disconnect(); S.attempt = 0; connect(); }

  function send(msg) {
    if (S.ws && S.ws.readyState === WebSocket.OPEN) {
      S.ws.send(JSON.stringify(msg));
      return true;
    }
    return false;
  }
  function ping() {
    S.pingAt = performance.now();
    send({ type: "ping" });
  }
  setInterval(ping, 5000);

  // ------------------------------------------------------- server messages
  function onMessage(msg) {
    switch (msg.type) {
      case "pong":
        S.latency = Math.max(1, Math.round(performance.now() - S.pingAt));
        renderConn();
        return;

      case "snapshot": {
        const a = msg.auction;
        const held = S.auction && S.auction.id === a.id ? S.auction.version : null;
        wire("in", "snapshot", held == null ? `${msg.bids.length} bids loaded` : `resync, v${held} → v${a.version}`, a.version);
        noteLeader(a);
        S.auction = a;
        S.bids = msg.bids;
        S.offset = Date.parse(msg.server_time) - Date.now();
        render();
        flushOutbox();
        return;
      }

      case "bid_accepted":
      case "auction_closed": {
        const a = msg.auction;
        if (!S.auction || a.version <= S.auction.version) {
          wire("in", msg.type, `ignored, already at v${S.auction ? S.auction.version : "?"}`, a.version);
          return;
        }
        wire("in", msg.type, msg.bid ? `${msg.bid.bidder} ${inr(msg.bid.amount)}` : (a.leader ? `sold to ${a.leader}` : "passed"), a.version);
        noteLeader(a);
        S.auction = a;
        if (msg.bid) S.bids = [msg.bid, ...S.bids].slice(0, 50);
        render(msg.bid && msg.bid.id);
        return;
      }

      case "bid_result":
        onBidResult(msg);
        return;

      case "error":
        wire("in", "error", msg.message);
    }
  }

  function onBidResult(msg) {
    const amount = msg.bid ? msg.bid.amount : S.outbox.get(msg.request_id);
    if (msg.reason === "busy_retry") {
      wire("in", "bid_result", "auction busy, retrying");
      setTimeout(flushOutbox, 300);
      return;
    }
    S.outbox.delete(msg.request_id);

    if (msg.accepted) {
      wire("in", "bid_result", `accepted ${inr(amount)}${msg.duplicate ? ", duplicate (not placed twice)" : ""}`, msg.auction && msg.auction.version);
      toast(msg.duplicate ? "Already placed. The retry didn't bid twice." : `Bid placed: ${inr(amount)}`);
    } else {
      wire("in", "bid_result", `rejected ${inr(amount)}: ${msg.reason}`, msg.auction && msg.auction.version);
      const floor = msg.reason === "bid_too_low" && msg.auction ? ` The next bid is ${inr(msg.auction.min_next_bid)}.` : "";
      toast(`${inr(amount)} wasn't accepted. ${REASONS[msg.reason] || msg.reason}.${floor}`, "danger");
    }

    // The result can arrive before the broadcast of the same change.
    const a = msg.auction;
    if (a && S.auction && a.id === S.auction.id && a.version > S.auction.version) {
      noteLeader(a);
      if (msg.accepted && msg.bid && !msg.duplicate) S.bids = [msg.bid, ...S.bids].slice(0, 50);
      S.auction = a;
      render(msg.bid && msg.bid.id);
    } else {
      renderPending();
      renderDev();
    }
  }

  function noteLeader(next) {
    const before = S.auction && S.auction.id === next.id ? S.auction.leader : null;
    if (next.leader && next.leader === S.me) S.outbidBy = null;
    else if (next.leader && (before === S.me || S.outbidBy)) {
      if (before === S.me) hideToast();
      S.outbidBy = next.leader;
    }
  }

  // ---------------------------------------------------------------- bidding
  function placeBid(amount) {
    if (!S.me) {
      toast("Add your name at the top right to bid.", "danger");
      $("bidder").focus();
      return;
    }
    const id = newId();
    S.outbox.set(id, amount);
    if (!sendBid(id, amount)) wire("sys", "queued", `${inr(amount)}, offline, sends on reconnect`);
    renderPending();
    renderDev();
  }
  function sendBid(id, amount) {
    const ok = send({ type: "bid", amount, request_id: id });
    if (ok) wire("out", "bid", `${inr(amount)} · request ${id.slice(0, 8)}`);
    return ok;
  }
  function flushOutbox() {
    for (const [id, amount] of S.outbox) sendBid(id, amount);
    renderPending();
  }

  $("bidBtn").addEventListener("click", () => S.auction && placeBid(S.auction.min_next_bid));
  $("customForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const amount = parseInt($("customAmount").value.replace(/[^\d]/g, ""), 10);
    if (Number.isFinite(amount) && amount > 0) {
      placeBid(amount);
      $("customAmount").value = "";
    }
  });

  // --------------------------------------------------------------- render
  function render(newBidId) {
    const a = S.auction;
    $("empty").hidden = S.lotId != null || S.lots.length > 0;
    $("lot").hidden = !a;
    if (!a) return;

    const closed = a.status === "closed";
    const leading = S.me && a.leader === S.me;

    $("lotNo").textContent = lotNo(a.id);
    $("lotOpened").textContent = `opened ${shortTime(new Date(a.created_at))}`;
    $("lotTitle").textContent = a.title;
    $("lotDesc").textContent = a.description || "No description from the seller.";
    $("lotDesc").classList.toggle("is-empty", !a.description);
    document.title = `${a.current_price != null ? inr(a.current_price) + " · " : ""}${a.title} · Bidding Floor`;

    $("fOpening").textContent = inr(a.starting_price);
    $("fIncrement").textContent = inr(a.min_increment);
    $("fBids").textContent = a.bid_count;
    $("fCloses").textContent = shortTime(new Date(closed ? a.closed_at || a.ends_at : a.ends_at));

    // price block
    $("priceLabel").textContent = closed ? (a.leader ? "Hammer price" : "Result") : (a.leader ? "Current bid" : "Opening bid");
    const price = $("price");
    price.textContent = closed && !a.leader ? "Passed" : inr(a.current_price ?? a.starting_price);
    if (newBidId) { price.classList.remove("is-bumped"); void price.offsetWidth; price.classList.add("is-bumped"); }

    const sub = $("priceSub");
    sub.replaceChildren();
    if (a.leader) {
      const who = el("span");
      who.append(closed ? "Sold to " : "", el("b", null, leading ? "you" : a.leader));
      sub.append(avatar(a.leader), who, el("span", null, `· ${a.bid_count} bid${a.bid_count === 1 ? "" : "s"}`));
    } else {
      sub.append(el("span", null, closed ? "No bids were placed" : "No bids yet"));
    }

    // notice strip
    const notice = $("notice");
    let kind = null, text = "", cta = false;
    if (closed && leading) { kind = "lead"; text = "You won this lot."; }
    else if (closed) { kind = "muted"; text = "Bidding has closed."; }
    else if (leading) { kind = "lead"; text = "You're the highest bidder."; }
    else if (S.outbidBy) { kind = "warn"; text = `You've been outbid by ${S.outbidBy}.`; cta = true; }
    notice.hidden = !kind;
    notice.className = "notice" + (kind ? " notice-" + kind : "");
    notice.replaceChildren(el("span", null, text));
    if (cta) {
      const b = el("button", "notice-action", `Bid ${inr(a.min_next_bid)}`);
      b.type = "button";
      b.addEventListener("click", () => placeBid(S.auction.min_next_bid));
      notice.append(b);
    }

    // controls
    const open = !closed && msLeft(a) > 0;
    $("bidBtn").disabled = !open;
    $("bidBtn").textContent = open ? `Bid ${inr(a.min_next_bid)}` : "Bidding closed";
    $("jumps").replaceChildren(...[1, 4, 10].map((steps) => {
      const amount = a.min_next_bid + steps * a.min_increment;
      const b = el("button", "btn", inr(amount));
      b.type = "button";
      b.disabled = !open;
      b.title = `Jump ${steps} increment${steps > 1 ? "s" : ""} past the minimum`;
      b.addEventListener("click", () => placeBid(amount));
      return b;
    }));
    $("customAmount").placeholder = a.min_next_bid.toLocaleString("en-IN");
    $("jumps").hidden = closed;
    $("customForm").hidden = closed;

    // history
    const rows = S.bids.map((bid, i) => {
      const li = el("li", "bid-row" + (bid.id === newBidId ? " is-new" : ""));
      const who = el("span", "bid-who");
      who.append(el("b", null, bid.bidder));
      if (bid.bidder === S.me) who.append(el("span", "tag", "You"));
      if (i === 0) who.append(el("span", "tag tag-lead", closed ? "Winning bid" : "Leading"));
      const when = el("span", "bid-when", ago(now() - Date.parse(bid.created_at)));
      when.dataset.at = Date.parse(bid.created_at);
      when.title = new Date(bid.created_at).toLocaleString("en-GB");
      li.append(avatar(bid.bidder, "avatar avatar-lg"), who, when, el("span", "bid-amt", inr(bid.amount)));
      return li;
    });
    if (!rows.length) rows.push(el("li", "history-empty", `No bids yet. Bidding opens at ${inr(a.starting_price)}.`));
    $("history").replaceChildren(...rows);

    // keep the sidebar in step with what the socket says
    const i = S.lots.findIndex((l) => l.id === a.id);
    if (i >= 0) S.lots[i] = a;

    renderLots();
    renderPending();
    renderDev();
    tick();
  }

  function renderPending() {
    const amounts = [...S.outbox.values()].map(inr).join(", ");
    $("pending").textContent = !S.outbox.size ? ""
      : S.ws ? `Placing ${amounts}…` : `Waiting to send ${amounts} when the connection is back`;
  }

  function setConn(state) {
    S.conn = state;
    renderConn();
    renderPending();
    renderDev();
  }
  function connLabel() {
    switch (S.conn) {
      case "live": return "Live";
      case "connecting": return "Connecting";
      case "reconnecting": return `Reconnecting in ${(S.retryIn / 1000).toFixed(1)}s`;
      case "offline": return "Offline";
      case "missing": return "Lot not found";
      default: return S.conn;
    }
  }
  function renderConn() {
    $("conn").dataset.state = S.conn;
    $("connLabel").textContent = connLabel();
    $("connMs").textContent = S.conn === "live" && S.latency != null ? `${S.latency} ms` : "";
    $("dState").textContent = connLabel();
    $("dLatency").textContent = S.latency == null ? "-" : `${S.latency} ms`;
  }
  function renderDev() {
    const v = S.auction ? `v${S.auction.version}` : "-";
    $("dVersion").textContent = v;
    $("dReconnects").textContent = S.reconnects;
    $("dQueued").textContent = S.outbox.size;
    const ms = S.conn === "live" && S.latency != null ? ` · ${S.latency} ms` : "";
    $("devSummary").textContent = `${connLabel().toLowerCase()}${ms} · ${v}`;
    renderConn();
  }

  function renderLots() {
    const open = S.lots.filter((l) => l.status === "open").length;
    $("liveCount").textContent = S.lots.length ? `${open} open · ${S.lots.length - open} closed` : "";
    $("lotCount").textContent = S.lots.length || "";
    if (!S.lots.length) {
      $("lotList").replaceChildren(el("li", "lots-empty", "No lots yet."));
      return;
    }
    $("lotList").replaceChildren(...S.lots.map((l) => {
      const closed = l.status === "closed";
      const b = el("button", "lot-card");
      b.type = "button";
      if (l.id === S.lotId) b.setAttribute("aria-current", "true");
      if (closed) b.dataset.closed = "";

      const top = el("span", "lot-card-top");
      const chip = el("span", closed ? "chip" : "chip chip-live");
      if (closed) chip.textContent = l.leader ? "Sold" : "Passed";
      else {
        const t = el("span", null, countdown(Date.parse(l.ends_at) - now()));
        t.dataset.ends = Date.parse(l.ends_at);
        chip.append(t);
      }
      top.append(el("span", "lot-card-no", lotNo(l.id)), chip);

      const bottom = el("span", "lot-card-bottom");
      bottom.append(
        el("span", "lot-card-price", closed && !l.leader ? "Unsold" : inr(l.current_price ?? l.starting_price)),
        el("span", "lot-card-meta", l.bid_count ? `${l.bid_count} bid${l.bid_count === 1 ? "" : "s"}` : closed ? "No bids" : "Opening"),
      );
      b.append(top, el("span", "lot-card-title", l.title), bottom);
      b.addEventListener("click", () => selectLot(l.id));
      const li = el("li");
      li.append(b);
      return li;
    }));
  }

  // Runs twice a second: countdowns, progress bar, relative times.
  function tick() {
    const a = S.auction;
    if (a) {
      const ms = msLeft(a);
      const closed = a.status === "closed";
      $("clockLabel").textContent = closed ? "Closed at" : ms <= 0 ? "Closing" : ms < 60000 ? "Closing soon" : "Closes in";
      $("clock").textContent = closed ? clockTime(new Date(a.closed_at || a.ends_at)) : countdown(ms);
      $("bidbox").dataset.urgency = !closed && ms < 60000 ? "high" : "";
      const span = Date.parse(a.ends_at) - Date.parse(a.created_at);
      const done = closed ? 1 : Math.min(1, Math.max(0, 1 - ms / span));
      $("clockFill").style.transform = `scaleX(${done})`;
      if (!closed && ms <= 0) {
        $("bidBtn").disabled = true;
        $("bidBtn").textContent = "Closing…";
      }
    }
    for (const node of document.querySelectorAll("[data-at]")) node.textContent = ago(now() - Number(node.dataset.at));
    for (const node of document.querySelectorAll("[data-ends]")) {
      const left = Number(node.dataset.ends) - now();
      node.textContent = left > 0 ? countdown(left) : "closing";
      node.parentElement.classList.toggle("urgent", left < 60000);
    }
  }
  setInterval(tick, 500);

  // ------------------------------------------------------------------ lots
  async function loadLots() {
    let lots;
    try {
      const res = await fetch("/auctions");
      if (!res.ok) return;
      lots = await res.json();
    } catch { return; }
    // The open socket is fresher than this poll for the lot we're watching.
    if (S.auction) {
      const i = lots.findIndex((l) => l.id === S.auction.id);
      if (i >= 0 && lots[i].version < S.auction.version) lots[i] = S.auction;
    }
    S.lots = lots;
    if (S.lotId == null) {
      const wanted = Number(new URLSearchParams(location.hash.slice(1)).get("lot"));
      const pick = lots.find((l) => l.id === wanted) || lots.find((l) => l.status === "open") || lots[0];
      if (pick) { selectLot(pick.id); return; }
    }
    renderLots();
    $("empty").hidden = lots.length > 0;
  }

  function selectLot(id) {
    if (id === S.lotId) return;
    S.lotId = id;
    S.auction = null;
    S.bids = [];
    S.outbox.clear();
    S.outbidBy = null;
    history.replaceState(null, "", `#lot=${id}`);
    $("wire").replaceChildren();
    render();
    renderLots();
    reconnectNow();
  }

  // ---------------------------------------------------------------- identity
  function renderMe() {
    $("bidder").value = S.me;
    const a = $("meAvatar");
    a.textContent = initials(S.me || "?");
    a.dataset.hue = hueOf(S.me || "?");
  }
  $("bidder").addEventListener("change", () => {
    S.me = $("bidder").value.trim().slice(0, 40);
    store.set("bidder", S.me);
    S.outbidBy = null;
    renderMe();
    wire("sys", "identity", S.me ? `bidding as ${S.me}` : "watching only");
    reconnectNow();
  });
  $("bidder").addEventListener("keydown", (e) => { if (e.key === "Enter") e.target.blur(); });

  // ---------------------------------------------------------------- devtools
  function wire(dir, kind, text, version) {
    const li = el("li", "w-" + dir);
    li.append(
      el("time", null, clockTime(new Date())),
      el("span", "dir", dir === "in" ? "←" : dir === "out" ? "→" : "·"),
      el("span", "kind", kind),
      el("span", "text", text),
      el("span", "v", version != null ? `v${version}` : ""),
    );
    const list = $("wire");
    list.prepend(li);
    while (list.childElementCount > 120) list.lastElementChild.remove();
  }

  function setDevtools(open) {
    $("devtools").dataset.open = String(open);
    $("devToggle").setAttribute("aria-expanded", String(open));
    document.body.classList.toggle("devtools-open", open);
    store.set("devtools", open ? "1" : "0");
  }
  $("devToggle").addEventListener("click", () => setDevtools($("devtools").dataset.open !== "true"));
  $("conn").addEventListener("click", () => setDevtools(true));

  $("dropBtn").addEventListener("click", () => {
    if (!S.ws) return;
    wire("sys", "drill", "dropping the connection");
    S.ws.close(4000, "drill"); // not a clean exit, so the client reconnects
  });
  $("offlineBtn").addEventListener("click", () => {
    S.offline = !S.offline;
    $("offlineBtn").setAttribute("aria-pressed", String(S.offline));
    $("offlineBtn").textContent = S.offline ? "Go back online" : "Go offline";
    if (S.offline) {
      wire("sys", "drill", "offline, new bids will wait in the outbox");
      disconnect();
      setConn("offline");
    } else {
      wire("sys", "drill", "back online");
      S.attempt = 0;
      connect();
    }
  });
  $("resyncBtn").addEventListener("click", () => {
    if (send({ type: "resync" })) wire("out", "resync", "fresh snapshot please");
  });

  // ------------------------------------------------------------------ toast
  let toastTimer;
  function toast(text, kind) {
    const t = $("toast");
    t.textContent = text;
    t.dataset.kind = kind || "";
    t.classList.add("is-on");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(hideToast, kind === "danger" ? 5000 : 2600);
  }
  function hideToast() {
    clearTimeout(toastTimer);
    $("toast").classList.remove("is-on");
  }

  // ------------------------------------------------------------- new lot
  const dialog = $("lotDialog");
  const openDialog = () => { dialog.showModal(); dialog.querySelector("input").focus(); };
  $("newLotBtn").addEventListener("click", openDialog);
  $("emptyNewLot").addEventListener("click", openDialog);
  $("lotCancel").addEventListener("click", () => dialog.close());
  $("lotForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const body = {
      title: f.get("title").trim(),
      description: f.get("description").trim(),
      starting_price: Number(f.get("starting_price")),
      min_increment: Number(f.get("min_increment")),
      duration_seconds: Number(f.get("duration_seconds")),
    };
    const res = await fetch("/auctions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) { toast("Couldn't open that lot. Check the amounts.", "danger"); return; }
    const lot = await res.json();
    dialog.close();
    e.target.reset();
    await loadLots();
    selectLot(lot.id);
  });

  // ------------------------------------------------------------------ boot
  renderMe();
  setDevtools(store.get("devtools") === "1");
  renderConn();
  loadLots();
  setInterval(loadLots, 5000);
})();
