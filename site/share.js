(function () {
  try {
    var u = encodeURIComponent(location.href);
    var t = encodeURIComponent((document.title || "PayCensus — the machine-payment census").replace(/\s*[|—-]\s*PayCensus.*$/i, ""));
    var items = [
      ["X", "https://twitter.com/intent/tweet?text=" + t + "&url=" + u, "#000000"],
      ["in", "https://www.linkedin.com/sharing/share-offsite/?url=" + u, "#0A66C2"],
      ["TG", "https://t.me/share/url?url=" + u + "&text=" + t, "#229ED9"],
      ["f", "https://www.facebook.com/sharer/sharer.php?u=" + u, "#1877F2"]
    ];
    var css = document.createElement("style");
    css.textContent =
      ".pcshare{display:flex;gap:8px;flex-wrap:wrap;align-items:center;font:12px/1.4 system-ui,-apple-system,sans-serif}" +
      ".pcshare a{display:inline-block;padding:3px 12px;border-radius:999px;color:#fff;text-decoration:none;font-weight:600;opacity:.9}" +
      ".pcshare a:hover{opacity:1;filter:brightness(1.15)}" +
      ".pcshare .lbl{color:#9aa0aa;font-size:11px;letter-spacing:2px;font-weight:600}" +
      ".pcshare-hd{justify-content:flex-end;padding:7px 18px;background:rgba(10,12,16,.72);backdrop-filter:blur(4px)}" +
      ".pcshare-ft{justify-content:center;padding:18px 0 28px}";
    document.head.appendChild(css);
    function bar(cls) {
      var d = document.createElement("div");
      d.className = "pcshare " + cls;
      d.innerHTML = '<span class="lbl">SHARE</span>' + items.map(function (it) {
        return '<a href="' + it[1] + '" target="_blank" rel="noopener" style="background:' + it[2] + '">' + it[0] + '</a>';
      }).join("");
      return d;
    }
    if (!document.querySelector(".pcshare-hd")) {
      document.body.insertBefore(bar("pcshare-hd"), document.body.firstChild);
    }
    if (!document.querySelector(".pcshare-ft")) {
      document.body.appendChild(bar("pcshare-ft"));
    }
  } catch (e) { /* never break the host page */ }
})();
