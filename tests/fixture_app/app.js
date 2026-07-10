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

    // hard_dom.html: shadow roots, contenteditable, multi-select, SVG, canvas,
    // and a disclosure widget. Each block is gated on its host id, so it stays a
    // no-op on the other fixture pages (same guarded-wiring contract as above).

    // Open shadow root: a pinned-color <style>, a button that logs through the
    // page's logAction, and a named <slot> for the host's light-DOM span.
    var shadowOpenHost = document.getElementById("shadow-open-host");
    if (shadowOpenHost && shadowOpenHost.attachShadow) {
      var openRoot = shadowOpenHost.attachShadow({ mode: "open" });
      openRoot.innerHTML =
        "<style>button { color: rgb(9, 99, 199); }</style>" +
        '<button id="shadow-open-btn">Shadow Open</button>' +
        '<slot name="label"></slot>';
      var openBtn = openRoot.querySelector("#shadow-open-btn");
      if (openBtn) {
        openBtn.addEventListener("click", function () {
          window.logAction("click", "shadow-open-btn");
        });
      }
    }

    // Closed shadow root: attachShadow returns the root here, but the host's
    // .shadowRoot property stays null. The reference lives only in this closure
    // (never stashed on window) so external JS cannot reach the inner button.
    var shadowClosedHost = document.getElementById("shadow-closed-host");
    if (shadowClosedHost && shadowClosedHost.attachShadow) {
      var closedRoot = shadowClosedHost.attachShadow({ mode: "closed" });
      closedRoot.innerHTML =
        '<button id="shadow-closed-btn">Shadow Closed</button>';
      var closedBtn = closedRoot.querySelector("#shadow-closed-btn");
      if (closedBtn) {
        closedBtn.addEventListener("click", function () {
          window.logAction("click", "shadow-closed-btn");
        });
      }
    }

    // contenteditable div logs an input event on every edit.
    on("editable", "input", function () {
      window.logAction("input", "editable");
    });

    // Multi-select logs the comma-joined selected values as the detail.
    on("select-multi", "change", function (e) {
      var picked = [];
      var opts = e.target.options;
      for (var j = 0; j < opts.length; j++) {
        if (opts[j].selected) {
          picked.push(opts[j].value);
        }
      }
      window.logAction("change", "select-multi", picked.join(","));
    });

    // SVG circle click logs like any other element.
    on("svg-circle", "click", function () {
      window.logAction("click", "svg-circle");
    });

    // Canvas: one deterministic fill on load, plus a click log. Pinned color is
    // rgb(10, 100, 200) so any pixel oracle stays stable.
    var canvas = document.getElementById("canvas-box");
    if (canvas) {
      if (canvas.getContext) {
        var ctx = canvas.getContext("2d");
        if (ctx) {
          ctx.fillStyle = "rgb(10, 100, 200)";
          ctx.fillRect(0, 0, 80, 60);
        }
      }
      canvas.addEventListener("click", function () {
        window.logAction("click", "canvas-box");
      });
    }

    // Disclosure widget logs its open/closed state on every toggle.
    var details = document.getElementById("details-box");
    if (details) {
      details.addEventListener("toggle", function () {
        window.logAction(
          "toggle",
          "details-box",
          details.open ? "open" : "closed"
        );
      });
    }

    // interactions.html: fidelity + completeness probes. Gated on the page's
    // marker button so the whole block stays a no-op on the other fixture
    // pages (same guarded-wiring contract as the hard_dom block above). Each
    // listener records event.isTrusted where relevant, so a real user-like
    // interaction (trusted) is distinguishable from a synthetic shortcut.
    if (document.getElementById("fidelity-btn")) {
      // Event-fidelity probe: a real coordinate click emits the full
      // pointer/mouse chain (all trusted); a synthetic element.click() emits a
      // lone untrusted click.
      ["pointerdown", "mousedown", "focus", "pointerup", "mouseup", "click"].forEach(
        function (type) {
          on("fidelity-btn", type, function (e) {
            window.logAction(
              "ev",
              "fidelity",
              e.type + ":" + (e.isTrusted ? "trusted" : "untrusted")
            );
          });
        }
      );

      // Overlay traps: covered button vs the overlay covering it. The pen
      // overlay is pointer-events:none, so a real click passes through.
      on("covered-btn", "click", function () {
        window.logAction("click", "covered-btn");
      });
      on("overlay-trap", "click", function () {
        window.logAction("click", "overlay-trap");
      });
      on("pen-covered-btn", "click", function () {
        window.logAction("click", "pen-covered-btn");
      });
      on("overlay-pen", "click", function () {
        window.logAction("click", "overlay-pen");
      });

      // Offscreen target (below the tall spacer).
      on("offscreen-btn", "click", function () {
        window.logAction("click", "offscreen-btn");
      });

      // Disabled button must never log a click; the label toggles its checkbox.
      on("disabled-btn", "click", function () {
        window.logAction("click", "disabled-btn");
      });
      on("labeled-check", "change", function (e) {
        window.logAction("change", "labeled-check", e.target.checked ? "on" : "off");
      });

      // Validation form: submit is prevented (and only fires when valid);
      // an empty required field fires invalid; reset fires reset.
      var vform = document.getElementById("validated-form");
      if (vform) {
        vform.addEventListener("submit", function (e) {
          e.preventDefault();
          window.logAction("submit", "validated-form");
        });
        vform.addEventListener("reset", function () {
          window.logAction("reset", "validated-form");
        });
      }
      on("required-input", "invalid", function () {
        window.logAction("invalid", "required-input");
      });

      // Enter-submit form: a single field, no submit button; only a trusted
      // Enter keypress triggers native implicit submission.
      var eform = document.getElementById("enter-form");
      if (eform) {
        eform.addEventListener("submit", function (e) {
          e.preventDefault();
          window.logAction("submit", "enter-form");
        });
      }

      // Key probe: keydown/keyup/keypress carry isTrusted; input carries the
      // resulting value. Capped at 40 logged key events to stay bounded.
      var keyProbe = document.getElementById("key-probe");
      if (keyProbe) {
        var keyLogged = 0;
        var logKey = function (kind, e) {
          if (keyLogged >= 40) {
            return;
          }
          keyLogged++;
          window.logAction(
            "key",
            kind,
            e.key + ":" + (e.isTrusted ? "trusted" : "untrusted")
          );
        };
        keyProbe.addEventListener("keydown", function (e) {
          logKey("down", e);
        });
        keyProbe.addEventListener("keyup", function (e) {
          logKey("up", e);
        });
        keyProbe.addEventListener("keypress", function (e) {
          logKey("press", e);
        });
        keyProbe.addEventListener("input", function (e) {
          window.logAction("input", "key-probe", e.target.value);
        });
      }

      // Select exercising select_option's value / index / text paths.
      on("select-fidelity", "change", function (e) {
        window.logAction("change", "select-fidelity", e.target.value);
      });

      // Value-typed inputs: each logs its live value on input.
      ["range-input", "number-input", "date-input", "color-input"].forEach(
        function (id) {
          on(id, "input", function (e) {
            window.logAction("input", id, e.target.value);
          });
        }
      );

      // Top layer: modal <dialog> (showModal / close) + popover (toggle).
      on("dialog-open-btn", "click", function () {
        var dlg = document.getElementById("modal-dialog");
        if (dlg && dlg.showModal) {
          dlg.showModal();
        }
        window.logAction("click", "dialog-open-btn");
      });
      on("dialog-close-btn", "click", function () {
        var dlg = document.getElementById("modal-dialog");
        if (dlg && dlg.close) {
          dlg.close();
        }
      });
      var dlgEl = document.getElementById("modal-dialog");
      if (dlgEl) {
        dlgEl.addEventListener("close", function () {
          window.logAction("close", "modal-dialog");
        });
      }
      var popBox = document.getElementById("pop-box");
      if (popBox) {
        popBox.addEventListener("toggle", function (e) {
          window.logAction("toggle", "pop-box", e.newState);
        });
      }

      // In-page anchor click logs (nav-link / blank-link navigate away).
      on("anchor-link", "click", function () {
        window.logAction("click", "anchor-link");
      });

      // Scroll fidelity: a real scroll fires the listener exactly once.
      window.addEventListener(
        "scroll",
        function () {
          window.logAction("scroll", "window");
        },
        { once: true }
      );

      // Unreachable census: double-click, context-menu, drag-and-drop. No MCP
      // interaction tool can express any of these, so these listeners stay
      // silent under the tool surface (proven in the fidelity suite).
      on("dbl-target", "dblclick", function () {
        window.logAction("dblclick", "dbl-target");
      });
      on("ctx-target", "contextmenu", function (e) {
        e.preventDefault();
        window.logAction("contextmenu", "ctx-target");
      });
      on("drag-src", "dragstart", function () {
        window.logAction("dragstart", "drag-src");
      });
      var dropZone = document.getElementById("drop-zone");
      if (dropZone) {
        dropZone.addEventListener("dragover", function (e) {
          e.preventDefault();
        });
        dropZone.addEventListener("drop", function (e) {
          e.preventDefault();
          window.logAction("drop", "drop-zone");
        });
      }
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }
})();
