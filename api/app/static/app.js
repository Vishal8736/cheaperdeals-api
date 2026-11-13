// Confirm dialogs for any <form data-confirm="...">
document.addEventListener("submit", function (e) {
  var msg = e.target.getAttribute && e.target.getAttribute("data-confirm");
  if (msg && !window.confirm(msg)) e.preventDefault();
});

// Auto-refresh KPI block on the dashboard every 30 s (no framework needed).
(function () {
  var box = document.getElementById("kpis");
  if (!box) return;
  var url = box.getAttribute("data-refresh");
  setInterval(function () {
    if (document.hidden) return;
    fetch(url, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (html) { if (html) box.innerHTML = html; })
      .catch(function () {});
  }, 30000);
})();
