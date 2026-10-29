/* Shared non-financial appearance preference. Runs before styles or page paint. */
(function () {
  "use strict";

  var root = document.documentElement;
  var system = window.matchMedia("(prefers-color-scheme: dark)");
  var preference = "system";

  // Only exact allowlisted values are accepted; duplicate/path-conflicting
  // preferences and malformed values fall back to the operating system.
  try {
    var saved = document.cookie.split(";").map(function (part) { return part.trim(); })
      .filter(function (part) { return part.indexOf("finn_nancy_theme=") === 0; });
    if (saved.length === 1) {
      var value = saved[0].slice("finn_nancy_theme=".length);
      if (value === "light" || value === "dark") preference = value;
    }
  } catch (_) {
    // Browsers may deny cookies. Appearance still works for this page.
  }

  function apply() {
    var theme = preference === "system" ? (system.matches ? "dark" : "light") : preference;
    root.dataset.theme = theme;
    // Bootstrap uses the same resolved preference; no second theme store.
    root.dataset.bsTheme = theme;
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", theme === "dark" ? "#000000" : "#ffffff");
  }

  apply();
  if (system.addEventListener) system.addEventListener("change", apply);
  else system.addListener(apply);

  function choose(event) {
    var value = event.target.value;
    preference = value === "light" || value === "dark" ? value : "system";
    try {
      var cookie = "finn_nancy_theme=" + (preference === "system" ? "" : preference);
      cookie += "; Path=/; Max-Age=" + (preference === "system" ? "0" : "31536000");
      cookie += "; SameSite=Lax";
      if (window.location.protocol === "https:") cookie += "; Secure";
      document.cookie = cookie;
    } catch (_) {
      // A denied preference write must not break the control or other scripts.
    }
    event.target.value = preference;
    apply();
  }

  function connect() {
    var select = document.querySelector("[data-theme-select]");
    if (select) {
      select.value = preference;
      select.addEventListener("change", choose);
    }
    var control = document.querySelector(".theme-control");
    var host = document.querySelector("[data-theme-host]") || document.querySelector(".hero, .chat-head")
      || document.querySelector(".app-main");
    if (control && host) {
      host.appendChild(control);
      control.hidden = false;
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", connect, { once: true });
  } else {
    connect();
  }
}());
