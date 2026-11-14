(async () => {
  // Fills {{name}} / {{email}} / {{address}} / {{days}} from the live shop settings (cached by the edge, so this costs the server nothing).
  const api = (window.SHOP_API || "/api").replace(/\/$/, "");
  let c = {};
  try { c = await (await fetch(api + "/shop/config")).json(); } catch {}
  const v = { name: c.name || "Cheaperdeal", email: c.support_email || "our support email", address: c.address || "", days: c.delivery_days || "10-25" };
  document.title = document.title + " - " + v.name; document.getElementById("brand").textContent = v.name;
  document.querySelectorAll("[data-fill]").forEach((el) => { el.textContent = v[el.dataset.fill] || el.textContent; });
  const main = document.querySelector("main"); main.innerHTML = main.innerHTML.replace(/\{\{(name|email|address|days)\}\}/g, (_m, k) => v[k].replace(/[<>&]/g, ""));
})();
