(() => {
  "use strict";
  const API = (window.SHOP_API || "/api").replace(/\/$/, "");
  const BIZ = encodeURIComponent(window.SHOP_BIZ || "");   // a dedicated website shows one business only (affiliate | pod | dropship); empty = every business that is ON
  const Q = BIZ ? "?biz=" + BIZ : "";
  const $ = (s, r = document) => r.querySelector(s);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  let cfg = { rates: { USD: { rate: "1", symbol: "$" } }, currency: "USD", ship_flat_usd: "0", free_over_usd: "0", countries: ["US"], methods: [{ id: "manual", label: "Pay later" }], open: true, name: "Cheaperdeal" };
  let cur = "USD";
  let products = [], cart = load(), cat = "All", query = "", warmed = false;

  function load() { try { const c = JSON.parse(localStorage.getItem("cart") || "[]"); return Array.isArray(c) ? c.filter((x) => x && x.id && x.qty > 0) : []; } catch { return []; } }
  function save() { try { localStorage.setItem("cart", JSON.stringify(cart)); } catch {} }
  const rate = () => Number((cfg.rates[cur] || { rate: 1 }).rate);
  const sym = (c = cur) => (cfg.rates[c] || { symbol: c + " " }).symbol;
  const r2 = (n) => Math.round((n + Number.EPSILON) * 100) / 100;
  const conv = (usd) => r2(Number(usd) * rate());            // same rule as the server: convert each unit, then multiply
  const money = (n, c = cur) => sym(c) + Number(n).toFixed(2);
  const price = (p) => conv(p.price_usd);
  const byId = (id) => products.find((p) => p.id === id);
  // Partner products (affiliate): no cart, a link to the merchant through our tracked /shop/go address. The link is marked sponsored for search engines.
  const buy = (p, cls) => `<a href="${esc(API + p.go)}" target="_blank" rel="sponsored nofollow noopener" class="${cls}">Buy at ${esc(p.merchant)} <span class="material-symbols-outlined text-base align-middle">open_in_new</span></a>`;
  const addBtn = (p, aria) => p.affiliate
    ? buy(p, "h-10 px-3 rounded-lg bg-secondary text-white font-label-md text-label-md inline-flex items-center")
    : `<button data-add="${p.id}" class="w-10 h-10 rounded-lg bg-surface-container-low hover:bg-secondary hover:text-white flex items-center justify-center transition-colors" aria-label="${aria}"><span class="material-symbols-outlined text-lg">add_shopping_cart</span></button>`;
  const stars = (n) => "★★★★★☆☆☆☆☆".slice(5 - Math.round(n), 10 - Math.round(n));
  function toast(t) { const el = $("#toast"); el.textContent = t; el.hidden = false; clearTimeout(toast.t); toast.t = setTimeout(() => (el.hidden = true), 2600); }

  // The first call after the server slept can take ~40 s on a free plan: wait for it instead of failing.
  async function api(path, opts = {}, ms = 70000) {
    const ctl = new AbortController(); const t = setTimeout(() => ctl.abort(), ms);
    try {
      const r = await fetch(API + path, { ...opts, signal: ctl.signal, headers: { "Content-Type": "application/json", ...(opts.headers || {}) } });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || j.detail || "Something went wrong. Please try again.");
      return j;
    } catch (e) { throw new Error(e.name === "AbortError" ? "The server is taking too long. Please try again in a minute." : e.message); }
    finally { clearTimeout(t); }
  }
  function warm() { if (!warmed) { warmed = true; fetch(API + "/ping", { cache: "no-store" }).catch(() => {}); } }

  function totals() {
    let usd = 0, sub = 0;
    for (const l of cart) { const p = byId(l.id); if (p) { usd += Number(p.price_usd) * l.qty; sub += price(p) * l.qty; } }
    const free = Number(cfg.free_over_usd);
    const ship = !cart.length ? 0 : (free > 0 && usd >= free ? 0 : conv(cfg.ship_flat_usd));
    return { sub: r2(sub), ship, total: r2(sub + ship) };
  }
  function img(p, cls) {
    return p.image ? `<img src="${esc(p.image)}" alt="${esc(p.title)}" loading="lazy" class="${cls}" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'${cls} flex items-center justify-center bg-surface-container-low text-outline',innerHTML:'<span class=\\'material-symbols-outlined text-4xl\\'>image</span>'}))">`
      : `<div class="${cls} flex items-center justify-center bg-surface-container-low text-outline"><span class="material-symbols-outlined text-4xl">image</span></div>`;
  }

  function renderHead() {
    document.title = cfg.name + (cfg.tagline ? " - " + cfg.tagline : "");
    const n = cfg.name || "Cheaperdeal", cut = n.length > 5 ? Math.ceil(n.length / 2) : n.length;
    $("#n1").textContent = n.slice(0, cut); $("#n2").textContent = n.slice(cut);
    $("#tag").textContent = cfg.tagline || ""; if (cfg.tagline) $("#h1").textContent = cfg.tagline;
    const a = $("#announce"); a.textContent = cfg.announce || ""; a.hidden = !cfg.announce;
    const free = Number(cfg.free_over_usd);
    $("#shipinfo").textContent = free > 0 ? `Free shipping over ${money(conv(free))}` : "Fair shipping";
    $("#delivery").textContent = cfg.delivery_days ? `Delivery about ${cfg.delivery_days} days` : "";
    $("#supinfo").textContent = cfg.support_email || "Email us any time";
    $("#cur").innerHTML = Object.keys(cfg.rates).map((c) => `<option ${c === cur ? "selected" : ""}>${esc(c)}</option>`).join("");
    $("#cur").hidden = Object.keys(cfg.rates).length < 2;
    $("#copy").textContent = `© ${new Date().getFullYear()} ${n}`;
    const aff = cfg.mode === "affiliate";   // affiliate business: no cart, no checkout, no order tracking - only "Buy at <merchant>" links
    $("#cartb").style.display = aff ? "none" : ""; $("#trackb").style.display = aff ? "none" : "";
    if (aff) { $("#shipinfo").textContent = "Best deals from trusted stores"; $("#delivery").textContent = "You buy on the merchant's own site"; }
    else if (!cfg.open) { const a2 = $("#announce"); a2.hidden = false; a2.textContent = "The store is closed for now. You can browse, but ordering is paused."; }
  }
  function card(p) {
    return `<article class="bg-white rounded-xl shadow-sm hover:shadow-md transition-shadow overflow-hidden flex flex-col">
      <button data-open="${p.id}" class="block text-left">${img(p, "w-full aspect-square object-cover")}</button>
      <div class="p-space-md flex flex-col gap-1 flex-1"><span class="font-badge-micro text-badge-micro text-secondary uppercase tracking-wider truncate">${esc(p.category || "Deal")}</span>
        <h3 class="font-label-md text-label-md font-semibold line-clamp-2 min-h-[36px]">${esc(p.title)}</h3>${p.reviews ? `<div class="font-body-sm text-body-sm text-on-surface-variant"><span class="text-amber-500">${stars(p.rating)}</span> ${p.rating} (${p.reviews})</div>` : ""}
        <div class="mt-auto flex items-center justify-between pt-1"><span class="font-price-md text-price-md">${money(price(p))}</span>
        ${addBtn(p, "Add " + esc(p.title) + " to cart")}</div></div></article>`;
  }
  function renderShop() {
    const cats = ["All", ...new Set(products.map((p) => p.category).filter(Boolean))];
    $("#cats").innerHTML = cats.length > 1 ? cats.map((c) => `<button data-cat="${esc(c)}" class="px-space-md py-1 rounded-full font-label-md text-label-md ${c === cat ? "bg-primary-container text-white" : "bg-surface-container-low hover:bg-surface-container"}">${esc(c)}</button>`).join("") : "";
    const q = query.trim().toLowerCase();
    const list = products.filter((p) => (cat === "All" || p.category === cat) && (!q || (p.title + " " + p.category).toLowerCase().includes(q)));
    $("#grid").innerHTML = list.map(card).join(""); $("#empty").hidden = list.length > 0;
    $("#spot").innerHTML = products.slice(0, 3).map((p) => `<div class="bg-white rounded-xl p-space-md shadow-sm hover:shadow-md flex items-center gap-space-md">
      <button data-open="${p.id}" class="shrink-0">${img(p, "w-24 h-24 rounded-lg object-cover")}</button>
      <div class="flex-1 min-w-0"><span class="font-badge-micro text-badge-micro text-secondary uppercase">${esc(p.category || "New")}</span><h3 class="font-headline-sm text-headline-sm truncate">${esc(p.title)}</h3><span class="font-price-lg text-price-lg">${money(price(p))}</span></div>
      ${addBtn(p, "Add to cart")}</div>`).join("");
  }
  function renderCart() {
    cart = cart.filter((l) => byId(l.id)); save();
    const t = totals(), n = cart.reduce((s, l) => s + l.qty, 0);
    $("#cnt").textContent = n; $("#sub").textContent = money(t.sub); $("#csub").textContent = money(t.sub);
    $("#cship").textContent = t.ship ? money(t.ship) : cart.length ? "Free" : "-";
    $("#gocheck").disabled = !cart.length; $("#gocheck").classList.toggle("opacity-50", !cart.length);
    $("#lines").innerHTML = cart.length ? cart.map((l) => { const p = byId(l.id); return `<div class="flex gap-space-md">${img(p, "w-16 h-16 rounded-lg object-cover shrink-0")}
      <div class="flex-1 min-w-0"><div class="font-label-md text-label-md font-semibold truncate">${esc(p.title)}</div><div class="font-body-sm text-body-sm text-on-surface-variant">${money(price(p))}</div>
      <div class="flex items-center gap-2 mt-1"><button data-dec="${p.id}" class="w-7 h-7 rounded bg-surface-container-low" aria-label="Less">-</button><span>${l.qty}</span><button data-inc="${p.id}" class="w-7 h-7 rounded bg-surface-container-low" aria-label="More">+</button><button data-rm="${p.id}" class="ml-auto text-error font-body-sm text-body-sm">Remove</button></div></div></div>`; }).join("")
      : `<p class="text-on-surface-variant text-center py-10">Your cart is empty.</p>`;
  }
  function add(id, d = 1) {
    const p = byId(id); if (!p || p.affiliate) return; warm();
    const l = cart.find((x) => x.id === id); const q = (l ? l.qty : 0) + d;
    if (q > p.stock) return toast(`Only ${p.stock} in stock`);
    if (q <= 0) cart = cart.filter((x) => x.id !== id); else if (l) l.qty = q; else cart.push({ id, qty: q });
    renderCart(); if (d > 0) toast("Added to cart");
  }

  const ov = $("#ov"), cartEl = $("#cart"), modal = $("#modal");
  function closeAll() { ov.hidden = cartEl.hidden = modal.hidden = true; document.body.style.overflow = ""; }
  function openCart() { if (cfg.mode === "affiliate") return; warm(); renderCart(); ov.hidden = cartEl.hidden = false; document.body.style.overflow = "hidden"; }
  function openModal(html) { cartEl.hidden = true; $("#mbody").innerHTML = html; ov.hidden = modal.hidden = false; document.body.style.overflow = "hidden"; modal.scrollTop = 0; }
  const closeBtn = '<button data-close class="material-symbols-outlined float-right" aria-label="Close">close</button>';
  const field = (n, l, extra = "") => `<label class="block"><span class="font-label-md text-label-md">${l}</span><input name="${n}" required ${extra} class="mt-1 w-full h-10 px-3 rounded-lg bg-surface-container-low focus:outline-none focus:ring-2 focus:ring-secondary"></label>`;

  function product(id) {
    const p = byId(id); if (!p) return;
    openModal(`${closeBtn}<div class="grid gap-space-md">${img(p, "w-full max-h-80 object-contain rounded-lg")}<span class="font-badge-micro text-badge-micro text-secondary uppercase">${esc(p.category)}</span>
      <h3 class="font-headline-md text-headline-md">${esc(p.title)}</h3><p class="text-on-surface-variant whitespace-pre-line">${esc(p.description)}</p>
      ${p.affiliate ? `<div class="flex items-center justify-between"><span class="font-price-xl text-price-xl">${money(price(p))}</span><span class="font-body-sm text-body-sm text-on-surface-variant">sold by ${esc(p.merchant)}</span></div>
      ${buy(p, "h-12 bg-secondary hover:bg-on-secondary-container text-white rounded-lg font-label-md text-label-md inline-flex items-center justify-center")}
      <p class="font-body-sm text-body-sm text-on-surface-variant">Partner link: we may earn a commission if you buy, at no extra cost to you. Price and availability are set by ${esc(p.merchant)} and can change.</p>`
      : `<div class="flex items-center justify-between"><span class="font-price-xl text-price-xl">${money(price(p))}</span><span class="font-body-sm text-body-sm text-on-surface-variant">${p.stock} in stock</span></div>
      <button data-add="${p.id}" data-then="cart" class="h-12 bg-secondary hover:bg-on-secondary-container text-white rounded-lg font-label-md text-label-md">Add to cart</button>`}
      <div id="revs" class="border-t border-outline-variant pt-space-md"><div class="skeleton h-10 rounded-lg"></div></div></div>`);
    api(`/shop/reviews/${p.id}`, {}, 20000).then((r) => {
      const el = $("#revs"); if (!el) return;
      el.innerHTML = `<h4 class="font-headline-sm text-headline-sm mb-1">Customer reviews</h4>${r.reviews.length ? r.reviews.map((v) => `<div class="mb-2"><span class="text-amber-500">${stars(v.rating)}</span> <b>${esc(v.title)}</b><div class="font-body-sm text-body-sm text-on-surface-variant">${esc(v.name)} · ${esc(v.when)}</div><p class="whitespace-pre-line">${esc(v.body)}</p></div>`).join("") : '<p class="text-on-surface-variant">No reviews yet.</p>'}<button data-review="${p.id}" class="underline text-secondary">Bought this? Write a review</button>`;
    }).catch(() => { const el = $("#revs"); if (el) el.remove(); });
  }
  function reviewForm(id) {
    const p = byId(id); let last = {}; try { last = JSON.parse(localStorage.getItem("last") || "{}"); } catch {}
    openModal(`${closeBtn}<h3 class="font-headline-md text-headline-md mb-1">Review: ${esc(p ? p.title : "")}</h3><p class="font-body-sm text-body-sm text-on-surface-variant mb-space-md">Only customers with a paid order can review. We check your order number and email.</p>
      <form id="rvf" class="grid gap-space-sm">${field("order", "Order number", `value="${esc(last.n || "")}" placeholder="W1001"`)}${field("email", "Email on the order", 'type="email"')}
      <label class="block"><span class="font-label-md text-label-md">Rating</span><select name="rating" class="mt-1 w-full h-10 px-3 rounded-lg bg-surface-container-low"><option value="5">5 - Excellent</option><option value="4">4 - Good</option><option value="3">3 - OK</option><option value="2">2 - Poor</option><option value="1">1 - Bad</option></select></label>
      <label class="block"><span class="font-label-md text-label-md">Title</span><input name="title" maxlength="120" class="mt-1 w-full h-10 px-3 rounded-lg bg-surface-container-low"></label>
      <label class="block"><span class="font-label-md text-label-md">Your review</span><textarea name="body" required rows="4" maxlength="1500" class="mt-1 w-full px-3 py-2 rounded-lg bg-surface-container-low"></textarea></label>
      <p id="err" class="text-error font-body-sm text-body-sm" role="alert"></p><button class="h-11 bg-secondary text-white rounded-lg font-label-md text-label-md">Send review</button></form>`);
    $("#rvf").addEventListener("submit", async (e) => { e.preventDefault(); const f = Object.fromEntries(new FormData(e.target)); try { const r = await api("/shop/reviews", { method: "POST", body: JSON.stringify({ ...f, product_id: id }) }); $("#mbody").innerHTML = `${closeBtn}<p class="py-6 text-center">${esc(r.note)}</p>`; } catch (x) { $("#err").textContent = x.message; } });
  }
  function help(pre = {}) {
    let last = {}; try { last = JSON.parse(localStorage.getItem("last") || "{}"); } catch {}
    const opt = (v, l) => `<option value="${v}" ${pre.kind === v ? "selected" : ""}>${l}</option>`;
    openModal(`${closeBtn}<h3 class="font-headline-md text-headline-md mb-1">Help and feedback</h3><p class="font-body-sm text-body-sm text-on-surface-variant mb-space-md">Tell us what happened. We reply by email${cfg.support_email ? " (or write to " + esc(cfg.support_email) + ")" : ""}.</p>
      <form id="hf" class="grid gap-space-sm"><input name="website" tabindex="-1" autocomplete="off" class="hidden" aria-hidden="true">
      <label class="block"><span class="font-label-md text-label-md">What is this about?</span><select name="kind" class="mt-1 w-full h-10 px-3 rounded-lg bg-surface-container-low">${opt("issue", "Problem with my order")}${opt("return", "Return or refund")}${opt("question", "A question")}${opt("feedback", "Feedback")}</select></label>
      ${field("name", "Your name", 'autocomplete="name"')}${field("email", "Email", 'type="email" autocomplete="email"')}
      <label class="block"><span class="font-label-md text-label-md">Order number (if it is about an order)</span><input name="order" value="${esc(pre.order || last.n || "")}" placeholder="W1001" class="mt-1 w-full h-10 px-3 rounded-lg bg-surface-container-low"></label>
      <label class="block"><span class="font-label-md text-label-md">Rating of your experience (optional)</span><select name="rating" class="mt-1 w-full h-10 px-3 rounded-lg bg-surface-container-low"><option value="0">-</option><option value="5">5 - Excellent</option><option value="4">4</option><option value="3">3</option><option value="2">2</option><option value="1">1 - Bad</option></select></label>
      <label class="block"><span class="font-label-md text-label-md">Message</span><textarea name="message" required rows="4" maxlength="3000" class="mt-1 w-full px-3 py-2 rounded-lg bg-surface-container-low"></textarea></label>
      <p id="err" class="text-error font-body-sm text-body-sm" role="alert"></p><button id="hs" class="h-11 bg-secondary text-white rounded-lg font-label-md text-label-md">Send</button></form>`);
    $("#hf").addEventListener("submit", async (e) => { e.preventDefault(); const b = $("#hs"); b.disabled = true; try { const r = await api("/shop/tickets", { method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(e.target))) });
      try { localStorage.setItem("ticket", r.token); } catch {} ticket(r.token, true); } catch (x) { $("#err").textContent = x.message; b.disabled = false; } });
  }
  async function ticket(token, fresh = false) {
    openModal(`${closeBtn}<div class="skeleton h-24 rounded-lg"></div>`);
    try {
      const t = await api(`/shop/tickets/${encodeURIComponent(token)}`, {}, 20000);
      $("#mbody").innerHTML = `${closeBtn}<h3 class="font-headline-md text-headline-md">${fresh ? "We got your message" : "Your message"} (#${t.id})</h3><p class="font-body-sm text-body-sm text-on-surface-variant mb-space-sm">Status: <b>${esc(t.status)}</b>${t.order ? " · order " + esc(t.order) : ""}. Save this link to read our reply: ${link2(t.token)}</p>
        <div class="grid gap-2 mb-space-md">${t.messages.map((m) => `<div class="rounded-lg p-space-sm ${m.who === "admin" ? "bg-secondary-fixed/30" : "bg-surface-container-low"}"><div class="font-badge-micro text-badge-micro uppercase text-on-surface-variant">${m.who === "admin" ? esc(cfg.name) : "You"}</div><div class="whitespace-pre-line">${esc(m.body)}</div></div>`).join("")}</div>
        <form id="trp" class="grid gap-2"><textarea name="message" required rows="3" placeholder="Add a reply" class="px-3 py-2 rounded-lg bg-surface-container-low"></textarea><p id="err" class="text-error font-body-sm text-body-sm"></p><button class="h-10 bg-primary-container text-white rounded-lg">Send reply</button></form>`;
      $("#trp").addEventListener("submit", async (e) => { e.preventDefault(); try { await api(`/shop/tickets/${encodeURIComponent(token)}/reply`, { method: "POST", body: JSON.stringify({ message: new FormData(e.target).get("message") }) }); ticket(token); } catch (x) { $("#err").textContent = x.message; } });
    } catch (x) { $("#mbody").innerHTML = `${closeBtn}<p class="text-error">${esc(x.message)}</p>`; }
  }
  const link2 = (t) => `<a class="text-secondary underline break-all" href="/#ticket=${esc(t)}">${esc(location.origin)}/#ticket=${esc(t)}</a>`;
  function checkout() {
    if (!cfg.open) return toast("The store is closed for now");
    const t = totals(); const co = cfg.countries.map((c) => `<option>${esc(c)}</option>`).join("");
    openModal(`${closeBtn}<h3 class="font-headline-md text-headline-md mb-space-md">Checkout</h3>
      <form id="cof" class="grid gap-space-sm">${field("name", "Full name", 'autocomplete="name"')}${field("email", "Email", 'type="email" autocomplete="email"')}${field("phone", "Phone (for the courier)", 'type="tel" autocomplete="tel"')}
        ${field("address1", "Address", 'autocomplete="address-line1"')}<div class="grid grid-cols-2 gap-space-sm">${field("city", "City")}${field("state", "State")}</div>
        <div class="grid grid-cols-2 gap-space-sm">${field("zip", "PIN / ZIP", 'autocomplete="postal-code"')}<label class="block"><span class="font-label-md text-label-md">Country</span><select name="country" class="mt-1 w-full h-10 px-3 rounded-lg bg-surface-container-low">${co}</select></label></div>
        <div class="bg-surface-container-low rounded-lg p-space-md font-body-sm text-body-sm"><div class="flex justify-between"><span>Subtotal</span><span id="qs">${money(t.sub)}</span></div><div class="flex justify-between"><span>Shipping</span><span id="qh">${t.ship ? money(t.ship) : "Free"}</span></div><div class="flex justify-between font-label-md text-label-md font-bold mt-1"><span>Total (${esc(cur)})</span><span id="qt">${money(t.total)}</span></div></div>
        <fieldset class="grid gap-1"><legend class="font-label-md text-label-md mb-1">How do you want to pay?</legend>${cfg.methods.map((m, i) => `<label class="flex items-center gap-2 bg-surface-container-low rounded-lg px-3 py-2"><input type="radio" name="method" value="${esc(m.id)}" ${i === 0 ? "checked" : ""}> ${esc(m.label)}</label>`).join("")}</fieldset>
        <p id="err" class="text-error font-body-sm text-body-sm" role="alert"></p>
        <button id="pay" class="h-12 bg-secondary hover:bg-on-secondary-container text-white rounded-lg font-label-md text-label-md">Place order</button></form>`);
    api("/shop/quote", { method: "POST", body: JSON.stringify({ items: cart.map((l) => ({ id: l.id, qty: l.qty })), currency: cur }) }, 20000).then((q) => { $("#qs").textContent = money(q.subtotal, q.currency); $("#qh").textContent = Number(q.shipping) ? money(q.shipping, q.currency) : "Free"; $("#qt").textContent = money(q.total, q.currency); }).catch(() => {});
    $("#cof").addEventListener("submit", async (e) => {
      e.preventDefault(); const f = Object.fromEntries(new FormData(e.target)); const b = $("#pay"), er = $("#err"); er.textContent = "";
      b.disabled = true; b.textContent = "Please wait... (the server may be waking up, up to a minute)";
      try {
        const r = await api("/shop/orders", { method: "POST", body: JSON.stringify({ ...f, currency: cur, items: cart.map((l) => ({ id: l.id, qty: l.qty })) }) });
        cart = []; save(); renderCart();
        try { localStorage.setItem("last", JSON.stringify({ n: r.number, t: r.token })); } catch {}
        if (r.method === "razorpay") return razor(r, f);
        payScreen(r);
      } catch (x) { er.textContent = x.message; b.disabled = false; b.textContent = "Try again"; }
    });
  }
  const link = (t) => `<a class="text-secondary underline break-all" href="/#track=${esc(t)}">${esc(location.origin)}/#track=${esc(t)}</a>`;
  function qr(el, text) {
    try { const q = window.qrcode(0, "M"); q.addData(text); q.make(); el.innerHTML = q.createSvgTag({ cellSize: 4, margin: 2, scalable: true }); el.firstChild.setAttribute("class", "w-44 h-44"); }
    catch { el.textContent = ""; }
  }
  function copyBtn(v) { return `<button data-copy="${esc(v)}" class="underline text-secondary font-body-sm text-body-sm">Copy</button>`; }
  function payBody(r) {   // what to show while the order waits for payment
    const p = r.pay || {};
    if (r.method === "upi") return `<p>Pay <b>${esc(r.symbol)}${esc(r.total)}</b> to <b>${esc(p.vpa)}</b> ${copyBtn(p.vpa)}</p><div id="qr" class="my-2 flex justify-center"></div>
      <a href="${esc(p.link)}" class="inline-block h-10 leading-10 px-4 bg-secondary text-white rounded-lg">Open UPI app</a><p class="text-on-surface-variant mt-2">${esc(p.note || "")}</p>
      <form id="utrf" class="flex gap-2 mt-2"><input name="utr" placeholder="UTR / transaction id" class="flex-1 h-10 px-3 rounded-lg bg-surface-container-low"><button class="h-10 px-3 bg-primary-container text-white rounded-lg">Send</button></form><p id="utrm" class="text-secondary"></p>`;
    return `<p class="whitespace-pre-line text-on-surface-variant">${esc(p.note || "")}</p>`;
  }
  function payScreen(r) {
    openModal(`${closeBtn}<div class="py-space-md"><h3 class="font-headline-md text-headline-md">Order ${esc(r.number)} placed</h3><p class="text-on-surface-variant mb-2">Waiting for your payment of ${esc(r.symbol)}${esc(r.total)}.</p>${payBody(r)}
      <p class="mt-3 font-body-sm text-body-sm">Keep this link to see your order any time:</p>${link(r.token)}<p id="paid" class="mt-3 font-label-md text-label-md text-secondary"></p></div>`);
    if (r.method === "upi") qr($("#qr"), r.pay.link);
    const u = $("#utrf"); if (u) u.addEventListener("submit", async (e) => { e.preventDefault(); try { await api(`/shop/orders/${encodeURIComponent(r.token)}/utr`, { method: "POST", body: JSON.stringify({ utr: new FormData(u).get("utr") }) }); $("#utrm").textContent = "Thanks! We will confirm once the money shows in our account."; } catch (x) { $("#utrm").textContent = x.message; } });
    watch(r.token);
  }
  function watch(token) {   // checks every 10 s (cheap) and shows a thank-you as soon as the payment lands
    clearInterval(watch.t); let n = 0;
    watch.t = setInterval(async () => {
      if (modal.hidden || ++n > 90) return clearInterval(watch.t);
      try { const r = await api(`/shop/track?ref=${encodeURIComponent(token)}`, {}, 15000); if (r.status === "paid") { clearInterval(watch.t); const el = $("#paid"); if (el) el.textContent = "Payment received. Thank you! We are preparing your order."; } } catch {}
    }, 10000);
  }
  function done(r) {
    cart = []; save(); renderCart();
    openModal(`${closeBtn}<div class="text-center py-space-md"><span class="material-symbols-outlined text-5xl text-secondary">check_circle</span><h3 class="font-headline-md text-headline-md mt-2">Order ${esc(r.number)} confirmed</h3>
      <p class="mt-2 text-on-surface-variant">Payment received. We are preparing your order.</p><p class="mt-3 font-body-sm text-body-sm">Save this link to see your order status any time:</p>${link(r.token)}</div>`);
  }
  function loadRzp() { return new Promise((ok, no) => { if (window.Razorpay) return ok(); const s = document.createElement("script"); s.src = "https://checkout.razorpay.com/v1/checkout.js"; s.onload = ok; s.onerror = () => no(new Error("Could not load the payment window.")); document.head.appendChild(s); }); }
  async function razor(r, f) {
    try {
      await loadRzp();
      new window.Razorpay({ key: r.razorpay.key, amount: r.razorpay.amount, currency: r.razorpay.currency, order_id: r.razorpay.order_id, name: cfg.name, prefill: { name: f.name, email: f.email, contact: f.phone },
        handler: async (resp) => { try { await api(`/shop/orders/${encodeURIComponent(r.token)}/verify`, { method: "POST", body: JSON.stringify(resp) }); done(r); } catch (x) { openModal(`${closeBtn}<p class="text-error">${esc(x.message)}</p><p class="mt-2">Order number: <b>${esc(r.number)}</b></p>`); } },
        modal: { ondismiss: () => { const b = $("#pay"); if (b) { b.disabled = false; b.textContent = "Pay again"; } } } }).open();
    } catch (x) { $("#err").textContent = x.message; const b = $("#pay"); b.disabled = false; b.textContent = "Try again"; }
  }
  function track(pref = "", pemail = "") {
    let last = {}; try { last = JSON.parse(localStorage.getItem("last") || "{}"); } catch {}
    openModal(`${closeBtn}<h3 class="font-headline-md text-headline-md mb-space-md">Track your order</h3><form id="trf" class="grid gap-space-sm">
      ${field("ref", "Order number or tracking link code", `value="${esc(pref || last.n || "")}" placeholder="W1001"`)}<label class="block"><span class="font-label-md text-label-md">Email on the order</span><input name="email" type="email" value="${esc(pemail)}" class="mt-1 w-full h-10 px-3 rounded-lg bg-surface-container-low"></label>
      <button class="h-11 bg-primary-container text-white rounded-lg font-label-md text-label-md">Check status</button></form><div id="tr" class="mt-space-md"></div>`);
    const run = async (ref, email) => {
      const el = $("#tr"); el.innerHTML = '<div class="skeleton h-16 rounded-lg"></div>';
      try { const r = await api(`/shop/track?ref=${encodeURIComponent(ref)}&email=${encodeURIComponent(email)}`);
        el.innerHTML = `<div class="bg-surface-container-low rounded-lg p-space-md"><div class="font-label-md text-label-md">Order ${esc(r.number)}: <b>${esc(r.stage)}</b></div><button data-help="${esc(r.number)}" class="underline text-secondary font-body-sm text-body-sm">Problem with this order?</button>${r.status === "pending" && r.pay ? `<div class="mt-2">${payBody({ ...r, symbol: r.symbol })}</div>` : ""}${(r.tracking || []).map((t) => `<div class="font-body-sm text-body-sm mt-1">${esc(t.status)} ${t.number ? "· " + esc(t.carrier) + " " + esc(t.number) : ""}</div>`).join("")}</div>`;
      } catch (x) { el.innerHTML = `<p class="text-error font-body-sm text-body-sm">${esc(x.message)}</p>`; }
    };
    $("#trf").addEventListener("submit", (e) => { e.preventDefault(); const f = Object.fromEntries(new FormData(e.target)); run(f.ref, f.email); });
    if (pref && pref.length > 12) run(pref, "");
  }

  document.addEventListener("click", (e) => {
    const cp = e.target.closest("[data-copy]"); if (cp) { navigator.clipboard && navigator.clipboard.writeText(cp.dataset.copy).then(() => toast("Copied")); return; }
    const t = e.target.closest("[data-review],[data-help],[data-add],[data-open],[data-cat],[data-inc],[data-dec],[data-rm],[data-close]"); if (!t) return;
    const d = t.dataset;
    if (d.review) reviewForm(+d.review);
    else if ("help" in d) help({ order: d.help });
    else if (d.add) { add(+d.add); if (d.then === "cart") openCart(); }
    else if (d.open) product(+d.open);
    else if (d.cat) { cat = d.cat; renderShop(); }
    else if (d.inc) add(+d.inc, 1); else if (d.dec) add(+d.dec, -1);
    else if (d.rm) { cart = cart.filter((l) => l.id !== +d.rm); renderCart(); }
    else if ("close" in d) closeAll();
  });
  ov.addEventListener("click", closeAll); document.addEventListener("keydown", (e) => e.key === "Escape" && closeAll());
  $("#cur").addEventListener("change", (e) => { cur = e.target.value; try { localStorage.setItem("cur", cur); } catch {} renderHead(); renderShop(); renderCart(); });
  $("#cartb").onclick = openCart; $("#gocheck").onclick = checkout; $("#trackb").onclick = () => track();
  for (const [f, i] of [["#searchf", "#q"], ["#searchm", "#qm"]]) { $(f).addEventListener("submit", (e) => { e.preventDefault(); query = $(i).value; renderShop(); $("#shop").scrollIntoView(); }); $(i).addEventListener("input", (e) => { query = e.target.value; renderShop(); }); }
  $("#subf").addEventListener("submit", async (e) => { e.preventDefault(); try { await api("/shop/subscribe", { method: "POST", body: JSON.stringify({ email: new FormData(e.target).get("email") }) }); e.target.reset(); toast("Subscribed. Thank you!"); } catch (x) { toast(x.message); } });

  async function start() {
    $("#grid").innerHTML = Array.from({ length: 5 }, () => '<div class="skeleton rounded-xl aspect-[3/4]"></div>').join("");
    try { cfg = { ...cfg, ...(await api("/shop/config" + Q, {}, 20000)) }; } catch {}
    let saved = ""; try { saved = localStorage.getItem("cur") || ""; } catch {}
    cur = cfg.rates[saved] ? saved : cfg.currency;
    renderHead();
    try { products = (await api("/shop/products" + Q, {}, 20000)).products; } catch { $("#grid").innerHTML = ""; $("#empty").hidden = false; $("#empty").textContent = "Could not load products. Please refresh."; }
    renderShop(); renderCart();
    const m = location.hash.match(/^#track=(.+)$/); if (m) track(decodeURIComponent(m[1]));
    const tk = location.hash.match(/^#ticket=(.+)$/); if (tk) ticket(decodeURIComponent(tk[1]));
  }
  start();
})();
