// Landing page: the lots currently open, straight from the API.
"use strict";

(() => {
  const list = document.getElementById("liveLots");
  const status = document.getElementById("liveStatus");
  const inr = (n) => "₹" + Number(n).toLocaleString("en-IN");
  const pad = (n) => String(n).padStart(2, "0");
  let offset = 0; // server clock minus ours, taken from the Date header
  let lots = [];

  function left(ms) {
    if (ms <= 0) return "closing";
    const s = Math.ceil(ms / 1000), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    if (h) return `${h}h ${pad(m)}m`;
    if (m) return `${m}m ${pad(s % 60)}s`;
    return `${s}s`;
  }

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function render() {
    const open = lots.filter((l) => l.status === "open").slice(0, 4);
    if (!open.length) {
      list.replaceChildren(el("li", "live-empty", "Nothing is open right now. New lots open regularly; check the floor."));
      return;
    }
    list.replaceChildren(...open.map((l) => {
      const a = el("a");
      a.href = `/floor#lot=${l.id}`;
      const top = el("span", "row");
      const chip = el("span", "chip chip-live");
      const time = el("span", null, left(Date.parse(l.ends_at) - (Date.now() + offset)));
      time.dataset.ends = Date.parse(l.ends_at);
      chip.append(time);
      top.append(el("span", "no", "LOT " + String(l.id).padStart(3, "0")), chip);
      const bids = l.bid_count ? `${l.bid_count} bid${l.bid_count === 1 ? "" : "s"} · next ${inr(l.min_next_bid)}` : "No bids yet · opening bid";
      a.append(top, el("span", "title", l.title), el("span", "price", inr(l.current_price ?? l.starting_price)), el("span", "meta", bids));
      const li = el("li", "live-lot");
      li.append(a);
      return li;
    }));
  }

  async function load() {
    try {
      const res = await fetch("/auctions");
      const date = res.headers.get("date");
      if (date) offset = Date.parse(date) - Date.now();
      lots = await res.json();
      const open = lots.filter((l) => l.status === "open").length;
      status.textContent = `${open} open now · live from the database`;
      render();
    } catch {
      status.textContent = "couldn't reach the server";
    }
  }

  setInterval(() => {
    for (const node of list.querySelectorAll("[data-ends]")) {
      const ms = Number(node.dataset.ends) - (Date.now() + offset);
      node.textContent = left(ms);
      node.parentElement.classList.toggle("urgent", ms < 60000);
    }
  }, 500);

  load();
  setInterval(load, 5000);
})();
