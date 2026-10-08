// Admin portal. Every request carries the access key as a Bearer token; the key
// lives in sessionStorage, so closing the tab forgets it.
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };
  const inr = (n) => (n == null ? "-" : "₹" + Number(n).toLocaleString("en-IN"));
  const lotNo = (id) => "LOT " + String(id).padStart(3, "0");
  const time = (iso) => new Date(iso).toLocaleTimeString("en-GB", { hour12: false });
  const shortTime = (iso) => new Date(iso).toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
  const session = {
    get() { try { return sessionStorage.getItem("adminKey"); } catch { return null; } },
    set(v) { try { sessionStorage.setItem("adminKey", v); } catch { /* private mode */ } },
    clear() { try { sessionStorage.removeItem("adminKey"); } catch { /* ignore */ } },
  };

  let key = session.get();
  let lots = [];
  let filter = "all";
  let refreshTimer = null;
  let feed = null;
  let feedRetry = null;
  const armed = new Map(); // lot id -> timer, for the two-step Remove

  // ------------------------------------------------------------- API
  class Locked extends Error {}

  async function api(path, options = {}) {
    const res = await fetch(path, {
      ...options,
      headers: { ...(options.headers || {}), Authorization: `Bearer ${key}`,
                 ...(options.body ? { "Content-Type": "application/json" } : {}) },
    });
    if (res.status === 401 || res.status === 404) throw new Locked(res.status === 404 ? "The admin portal is switched off on this server." : "That key isn't accepted. It may have been rotated; the landing page always shows the current judge key.");
    const body = await res.json().catch(() => ({}));
    if (res.status === 429 && /wrong keys/.test(body.detail || "")) throw new Locked("Too many wrong keys from your address. Wait a minute and try again.");
    if (!res.ok) throw new Error(body.detail || `request failed (${res.status})`);
    return body;
  }

  // ------------------------------------------------------------- lock / unlock
  function lock(message) {
    key = null;
    session.clear();
    clearInterval(refreshTimer);
    clearTimeout(feedRetry);
    if (feed) { const f = feed; feed = null; f.close(); }
    $("console").hidden = true;
    $("sessionBar").hidden = true;
    $("roleTag").hidden = true;
    $("gate").hidden = false;
    $("gateError").textContent = message || "";
    $("keyInput").value = "";
    $("keyInput").focus();
  }

  async function unlock() {
    try {
      await refresh();
    } catch (err) {
      lock(err instanceof Locked ? err.message : "Couldn't reach the server.");
      return;
    }
    session.set(key);
    $("gate").hidden = true;
    $("console").hidden = false;
    $("sessionBar").hidden = false;
    clearInterval(refreshTimer);
    refreshTimer = setInterval(() => refresh().catch(onError), 4000);
    connectFeed();
  }

  function onError(err) {
    if (err instanceof Locked) lock(err.message);
    else toast(err.message, "danger");
  }

  $("gateForm").addEventListener("submit", (e) => {
    e.preventDefault();
    key = $("keyInput").value.trim();
    $("gateError").textContent = "";
    unlock();
  });
  $("lockBtn").addEventListener("click", () => lock("Locked."));

  // ------------------------------------------------------------- data
  async function refresh() {
    const [overview, lotList, bids, activity] = await Promise.all([
      api("/admin/api/overview"),
      api("/admin/api/lots"),
      api(`/admin/api/bids?limit=150&rejected_only=${$("rejectedOnly").checked}`),
      api("/admin/api/activity?limit=50"),
    ]);
    lots = lotList;
    renderOverview(overview);
    renderLots();
    renderBids(bids);
    renderActivity(activity, overview.you === "owner");
  }

  function renderOverview(o) {
    $("sOpen").textContent = o.lots.open;
    $("sClosed").textContent = o.lots.closed;
    $("sRemoved").textContent = o.lots.removed;
    $("sAccepted").textContent = o.bids.accepted.toLocaleString("en-IN");
    $("sRejected").textContent = o.bids.rejected.toLocaleString("en-IN");
    $("sReasons").textContent = Object.entries(o.rejected_by_reason).map(([r, n]) => `${n} ${r.replace(/_/g, " ")}`).join(" · ");
    $("sHour").textContent = o.bids.last_hour.toLocaleString("en-IN");
    $("servedBy").textContent = `you're on ${o.served_by}`;
    $("roleTag").hidden = false;
    $("roleTag").textContent = o.you === "owner" ? "Owner" : "Judge";
    $("roleTag").classList.toggle("is-owner", o.you === "owner");
    renderJudgePanel(o);

    $("instances").replaceChildren(...(o.instances.length ? o.instances.map((i) => {
      const li = el("li", i.alive ? "alive" : "");
      const who = el("span");
      who.append(el("span", "name", i.name), el("br"), el("small", null, i.alive ? `up since ${shortTime(i.started_at)}` : `last seen ${shortTime(i.last_seen)}`));
      li.append(el("span", "dot"), who, el("span", "count", i.alive ? `${i.websocket_clients} socket${i.websocket_clients === 1 ? "" : "s"}` : "down"));
      return li;
    }) : [el("li", "muted", "No instance has reported in yet.")]));

    const labels = {
      public_lot_creation: "Visitors can open lots",
      demo_restock: "Demo restock",
      rate_limits: "Rate limits",
      unsafe_demo_endpoint: "Unsafe race-demo endpoint",
    };
    $("settings").replaceChildren(...Object.entries(labels).flatMap(([k, label]) => {
      const on = o.settings[k];
      return [el("dt", null, label), el("dd", on ? "on" : "off", on ? "on" : "off")];
    }));
  }

  function renderLots() {
    const visible = lots.filter((l) => filter === "all"
      || (filter === "removed" ? l.removed_at : !l.removed_at && l.status === filter));
    if (!visible.length) {
      const td = el("td", "empty", "Nothing here.");
      td.colSpan = 7;
      const tr = el("tr");
      tr.append(td);
      $("lotRows").replaceChildren(tr);
      return;
    }
    $("lotRows").replaceChildren(...visible.map((l) => {
      const tr = el("tr", l.removed_at ? "is-removed" : "");
      const name = el("td");
      name.append(el("span", "lot-no", lotNo(l.id)), el("span", "lot-title", l.title));
      const status = el("td");
      const state = l.removed_at ? "removed" : l.status;
      status.append(el("span", `chip chip-${state}`, state));
      const actions = el("td");
      const box = el("div", "row-actions");
      if (!l.removed_at && l.status === "open") box.append(actionButton("Close now", "btn", () => closeLot(l)));
      if (!l.removed_at) box.append(removeButton(l));
      actions.append(box);
      tr.append(
        name,
        status,
        el("td", "num", inr(l.current_price ?? l.starting_price)),
        el("td", null, l.leader || "-"),
        el("td", "num", l.bid_count),
        el("td", "mono", shortTime(l.closed_at || l.ends_at)),
        actions,
      );
      return tr;
    }));
  }

  let judgeEnabled = false;
  function renderJudgePanel(o) {
    const panel = $("judgePanel");
    panel.hidden = !(o.you === "owner" && o.judge_access);
    if (panel.hidden) return;
    judgeEnabled = o.judge_access.enabled;
    $("judgeState").textContent = judgeEnabled ? "on · shown on the landing page" : "off";
    $("judgeKeyText").textContent = o.judge_access.key || "-";
    $("judgeKeyText").classList.toggle("is-off", !judgeEnabled);
    $("judgeToggle").textContent = judgeEnabled ? "Switch off" : "Switch on";
  }

  $("judgeToggle").addEventListener("click", async () => {
    try {
      await api(`/admin/api/judge-key/${judgeEnabled ? "disable" : "enable"}`, { method: "POST" });
      toast(judgeEnabled ? "Judge key switched off. The landing page stops showing it." : "Judge key switched back on.");
      await refresh();
    } catch (err) { onError(err); }
  });

  // Rotating cuts off everyone holding the old key: two clicks.
  let rotateArmed = null;
  $("judgeRotate").addEventListener("click", async () => {
    const b = $("judgeRotate");
    if (!rotateArmed) {
      b.textContent = "Confirm rotate";
      b.classList.add("is-armed");
      rotateArmed = setTimeout(() => { rotateArmed = null; b.textContent = "Rotate key"; b.classList.remove("is-armed"); }, 4000);
      return;
    }
    clearTimeout(rotateArmed);
    rotateArmed = null;
    b.textContent = "Rotate key";
    b.classList.remove("is-armed");
    try {
      await api("/admin/api/judge-key/rotate", { method: "POST" });
      toast("New judge key issued. The old one has stopped working.");
      await refresh();
    } catch (err) { onError(err); }
  });

  function renderActivity(rows, isOwner) {
    $("ipHead").hidden = !isOwner;
    if (!rows.length) {
      const td = el("td", "empty", "No changes yet.");
      td.colSpan = isOwner ? 5 : 4;
      const tr = el("tr");
      tr.append(td);
      $("actRows").replaceChildren(tr);
      return;
    }
    $("actRows").replaceChildren(...rows.map((r) => {
      const tr = el("tr");
      tr.append(
        el("td", "mono", time(r.at)),
        el("td", `role-${r.role}`, r.role),
        el("td", null, r.action),
        el("td", null, (r.auction_id ? lotNo(r.auction_id) + " · " : "") + (r.detail || "")),
      );
      if (isOwner) tr.append(el("td", "mono", r.ip || "-"));
      return tr;
    }));
  }

  function actionButton(label, cls, onClick) {
    const b = el("button", cls, label);
    b.type = "button";
    b.addEventListener("click", onClick);
    return b;
  }

  // Two clicks to remove: the first arms the button for a few seconds.
  function removeButton(lot) {
    const isArmed = armed.has(lot.id);
    const b = actionButton(isArmed ? "Confirm remove" : "Remove", "btn btn-danger" + (isArmed ? " is-armed" : ""), () => {
      if (!armed.has(lot.id)) {
        armed.set(lot.id, setTimeout(() => { armed.delete(lot.id); renderLots(); }, 4000));
        renderLots();
        return;
      }
      clearTimeout(armed.get(lot.id));
      armed.delete(lot.id);
      removeLot(lot);
    });
    return b;
  }

  async function closeLot(lot) {
    try {
      await api(`/admin/api/lots/${lot.id}/close`, { method: "POST" });
      toast(`${lotNo(lot.id)} closed. ${lot.leader ? lot.leader + " wins." : "No bids, so it passes."}`);
      await refresh();
    } catch (err) { onError(err); }
  }

  async function removeLot(lot) {
    try {
      await api(`/admin/api/lots/${lot.id}/remove`, { method: "POST" });
      toast(`${lotNo(lot.id)} removed from the floor.`);
      await refresh();
    } catch (err) { onError(err); }
  }

  function renderBids(bids) {
    if (!bids.length) {
      const td = el("td", "empty", "No bids yet.");
      td.colSpan = 6;
      const tr = el("tr");
      tr.append(td);
      $("bidRows").replaceChildren(tr);
      return;
    }
    $("bidRows").replaceChildren(...bids.map((b) => {
      const tr = el("tr");
      const lot = el("td");
      lot.append(el("span", "lot-no", lotNo(b.auction_id)), el("span", null, b.title));
      const outcome = b.status === "accepted" ? "accepted" : `rejected: ${(b.reason || "").replace(/_/g, " ")}`;
      tr.append(
        el("td", "mono", time(b.created_at)),
        lot,
        el("td", null, b.bidder),
        el("td", "num", inr(b.amount)),
        el("td", `outcome-${b.status}`, outcome),
        el("td", "mono", b.request_id.length > 14 ? b.request_id.slice(0, 14) + "…" : b.request_id),
      );
      return tr;
    }));
  }

  document.querySelectorAll("[data-filter]").forEach((b) => b.addEventListener("click", () => {
    filter = b.dataset.filter;
    document.querySelectorAll("[data-filter]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    renderLots();
  }));
  $("rejectedOnly").addEventListener("change", () => refresh().catch(onError));

  $("newLot").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      const lot = await api("/admin/api/lots", {
        method: "POST",
        body: JSON.stringify({
          title: f.get("title").trim(),
          description: f.get("description").trim(),
          starting_price: Number(f.get("starting_price")),
          min_increment: Number(f.get("min_increment")),
          duration_seconds: Number(f.get("duration_seconds")),
        }),
      });
      e.target.reset();
      toast(`${lotNo(lot.id)} is open for bidding.`);
      await refresh();
    } catch (err) { onError(err); }
  });

  // ------------------------------------------------------------- live feed
  function setFeed(state, label) {
    $("feedConn").dataset.state = state;
    $("feedLabel").textContent = label;
  }

  function connectFeed() {
    clearTimeout(feedRetry);
    if (!key) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const sock = new WebSocket(`${proto}://${location.host}/ws/admin`);
    feed = sock;
    setFeed("connecting", "Feed connecting");
    sock.onopen = () => sock.send(JSON.stringify({ type: "auth", key }));
    sock.onmessage = (e) => {
      if (feed !== sock) return;
      const msg = JSON.parse(e.data);
      if (msg.type === "hello") { setFeed("live", `Feed live · ${msg.instance}`); return; }
      if (msg.type === "pong") return;
      addFeedRow(msg);
    };
    sock.onclose = (e) => {
      if (feed !== sock) return;
      feed = null;
      if (e.code === 4401) { lock("That key wasn't accepted."); return; }
      setFeed("reconnecting", "Feed reconnecting");
      feedRetry = setTimeout(connectFeed, 2000);
    };
  }
  setInterval(() => { if (feed && feed.readyState === WebSocket.OPEN) feed.send('{"type":"ping"}'); }, 20000);

  function addFeedRow(msg) {
    const list = $("feed");
    const placeholder = list.querySelector(".feed-empty");
    if (placeholder) placeholder.remove();
    const a = msg.auction;
    let text;
    if (msg.type === "bid_accepted") text = `${msg.bid.bidder} bid ${inr(msg.bid.amount)} on ${a.title}`;
    else if (msg.type === "auction_closed") text = `${a.title} closed, ${a.leader ? `won by ${a.leader} at ${inr(a.current_price)}` : "no bids"}`;
    else if (msg.type === "auction_removed") text = `${a.title} removed`;
    else if (msg.type === "feed_gap") text = `${msg.instance}'s event feed reconnected; anything in the gap is in the bid log`;
    else text = JSON.stringify(msg).slice(0, 120);
    const li = el("li", msg.type === "feed_gap" ? "w-sys" : "w-in");
    li.append(
      el("time", null, new Date().toLocaleTimeString("en-GB", { hour12: false })),
      el("span", "dir", msg.type === "feed_gap" ? "·" : "←"),
      el("span", "kind", msg.type),
      el("span", "text", text),
      el("span", "v", a ? `${lotNo(a.id).toLowerCase()} v${a.version}` : ""),
    );
    list.prepend(li);
    while (list.childElementCount > 200) list.lastElementChild.remove();
    // A change worth seeing in the tables too: refresh soon, not on every bid.
    clearTimeout(addFeedRow.soon);
    addFeedRow.soon = setTimeout(() => refresh().catch(onError), 600);
  }

  // ------------------------------------------------------------- toast
  let toastTimer;
  function toast(text, kind) {
    const t = $("toast");
    t.textContent = text;
    t.dataset.kind = kind || "";
    t.classList.add("is-on");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("is-on"), 3200);
  }

  // ------------------------------------------------------------- boot
  // The landing page's "Open the admin portal" passes the judge key in the URL
  // fragment (never sent to the server). Take it, then wipe it from the address bar.
  const fromLink = new URLSearchParams(location.hash.slice(1)).get("key");
  if (fromLink) {
    key = fromLink;
    history.replaceState(null, "", location.pathname);
  }
  if (key) unlock();
  else lock();
})();
