/* Single JS home for the fixture app (plan_E2E STEP 1).
 *
 * Every page includes this via <script src="app.js">. It defines the action
 * log and the global hook surface immediately (so cdp-functions / dynamic-hook
 * tests see them right after load), then wires whichever fixture elements are
 * present once the DOM is ready. No external URLs; the only timer is the
 * bounded 200ms reveal used as a wait_for_element target.
 */

(function () {
  "use strict";

  // --- Action log: proof that "the right action was performed". Each entry is
  // "<kind>:<id>" plus ":<detail>" when a detail is supplied. ---
  window.__actions = [];
  window.logAction = function logAction(kind, id, detail) {
    var entry = kind + ":" + id;
    if (detail !== undefined && detail !== null) {
      entry += ":" + detail;
    }
    window.__actions.push(entry);
    var pre = document.getElementById("action-log");
    if (pre) {
      pre.textContent = window.__actions.join("\n");
    }
  };

  // --- Global function + hookable API surface (cdp-functions / hooks tests). ---
  window.calcTotal = function calcTotal(a, b) {
    return a + b;
  };
  window.appAPI = {
    getUser: function () {
      return { id: 7, name: "Fixture User", role: "tester" };
    },
    setFlag: function (v) {
      window.__flag = v;
      return v;
    },
    version: "1.0-fixture",
  };
  // Same-origin fetch trigger reused by network + dynamic-hook interception.
  window.triggerFetch = function triggerFetch(path) {
    return fetch(path).then(function (r) {
      return r.status;
    });
  };

  function on(id, event, handler) {
    var el = document.getElementById(id);
    if (el) {
      el.addEventListener(event, handler);
    }
    return el;
  }

  function wire() {
    // Counter button increments #counter-value.
    on("btn-counter", "click", function () {
      var v = document.getElementById("counter-value");
      if (v) {
        v.textContent = String(parseInt(v.textContent, 10) + 1);
      }
      window.logAction("click", "btn-counter");
    });

    // Select / checkbox / radios log the chosen value as detail.
    on("select-single", "change", function (e) {
      window.logAction("change", "select-single", e.target.value);
    });
    on("check-me", "change", function (e) {
      window.logAction("change", "check-me", e.target.checked ? "on" : "off");
    });
    var radios = document.querySelectorAll("input[name='flavor']");
    for (var i = 0; i < radios.length; i++) {
      radios[i].addEventListener("change", function (e) {
        window.logAction("change", "flavor", e.target.value);
      });
    }

    // Text + textarea log their natural events (value is the primary assert).
    on("text-input", "keydown", function () {
      window.logAction("keydown", "text-input");
    });
    on("textarea-input", "input", function () {
      window.logAction("input", "textarea-input");
    });

    // Form submit stays on-page.
    var form = document.getElementById("hook-form");
    if (form) {
      form.addEventListener("submit", function (e) {
        e.preventDefault();
        window.logAction("submit", "hook-form");
      });
    }

    // Bounded reveal: 200ms after the click #delayed-el becomes visible.
    on("reveal-btn", "click", function () {
      window.logAction("click", "reveal-btn");
      setTimeout(function () {
        var el = document.getElementById("delayed-el");
        if (el) {
          el.classList.remove("hidden");
        }
      }, 200);
    });

    // Network trigger buttons (same-origin, document-relative paths).
    on("fetch-json-btn", "click", function () {
      window.logAction("click", "fetch-json-btn");
      window.triggerFetch("api/json");
    });
    on("post-echo-btn", "click", function () {
      window.logAction("click", "post-echo-btn");
      fetch("api/echo", { method: "POST", body: "fixture-payload" });
    });

    // Cookie / storage setters.
    on("set-client-cookie-btn", "click", function () {
      document.cookie = "client_cookie=from-js; path=/";
      window.logAction("click", "set-client-cookie-btn");
    });
    on("set-local-storage-btn", "click", function () {
      window.localStorage.setItem("fixture_key", "fixture-value");
      window.logAction("click", "set-local-storage-btn");
    });

    // extract.html styled card: one click + one mouseover listener so
    // extract_element_events has real addEventListener listeners to find.
    var card = document.getElementById("styled-card");
    if (card) {
      card.addEventListener("click", function () {
        window.logAction("click", "styled-card");
      });
      card.addEventListener("mouseover", function () {
        window.logAction("mouseover", "styled-card");
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }
})();
