/* Dashboard behaviour: start a run and follow it, and record what the operator
 * did about each match.
 *
 * Deliberately dependency-free — this ships inside the same container as the
 * resume endpoint and a build step would earn nothing at this size.
 *
 * The did-you-apply flow is the interesting part. The system never submits an
 * application, so the only way it learns one happened is to ask. Clicking
 * Apply opens the posting in another tab and arms a confirmation; when the
 * operator comes back here, the page asks. It waits for a real departure
 * first — arming alone must not raise a dialog over a tab that never lost
 * focus.
 */
(function () {
  "use strict";

  var POLL_MS = 2000;
  var TOAST_MS = 2600;
  var ARMED_KEY = "jobbot.armed";

  var $ = function (id) { return document.getElementById(id); };

  var runBtn = $("run-btn");
  var runText = $("run-btn-text");
  var dryRun = $("dry-run");
  var panel = $("run-panel");
  var logEl = $("run-log");
  var label = $("run-label");
  var dot = $("run-dot");
  var closeBtn = $("run-close");
  var toastEl = $("toast");
  var modal = $("confirm");

  var timer = null;
  var toastTimer = null;

  function target() {
    var picked = document.querySelector('input[name="target"]:checked');
    return picked ? picked.value : "local";
  }

  // ------------------------------------------------------------------ toast

  function toast(text, kind) {
    if (!toastEl) return;
    toastEl.textContent = text;
    toastEl.className = "toast" + (kind === "bad" ? " toast--bad" : "");
    toastEl.hidden = false;
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.hidden = true; }, TOAST_MS);
  }

  // ------------------------------------------------------------- run panel

  function setRunLabel(text, busy) {
    if (runText) runText.textContent = text;
    runBtn.classList.toggle("is-busy", !!busy);
    runBtn.disabled = !!busy;
  }

  function say(text, kind) {
    label.textContent = text;
    dot.className = "dot" + (kind ? " dot--" + kind : "");
  }

  function renderLog(lines) {
    if (!lines || !lines.length) return;
    // Only touch the DOM when the tail actually changed — this polls every
    // couple of seconds for as long as a run lasts.
    var text = lines.join("\n");
    if (logEl.textContent === text) return;
    var pinned = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 24;
    logEl.textContent = text;
    if (pinned) logEl.scrollTop = logEl.scrollHeight;
  }

  function stopPolling() {
    if (timer) { clearInterval(timer); timer = null; }
  }

  function finish(state) {
    stopPolling();
    setRunLabel("Run", false);
    if (state.returncode === 0) {
      say("Run finished", "ok");
      // Full reload rather than a client-side re-render: the page is
      // server-rendered, and a run also changes the counts in the top bar.
      setTimeout(function () { window.location.reload(); }, 700);
    } else {
      say("Run failed (exit " + state.returncode + ")", "bad");
      if (state.error) toast(state.error, "bad");
    }
  }

  function poll() {
    fetch("/api/run/status")
      .then(function (r) { return r.json(); })
      .then(function (state) {
        renderLog(state.log_lines);
        if (state.running) {
          say((state.dry_run ? "Dry run" : "Run") + " in progress", "live");
        } else {
          finish(state);
        }
      })
      .catch(function () { /* transient; the next tick retries */ });
  }

  function startRun() {
    var where = target();
    setRunLabel("Starting", true);
    panel.hidden = false;
    logEl.textContent = "";
    say("Starting…", "live");

    fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dry_run: dryRun.checked, target: where })
    })
      .then(function (r) {
        return r.json().then(function (b) { return { ok: r.ok, body: b }; });
      })
      .then(function (res) {
        if (!res.ok) {
          var why = res.body.message || res.body.detail || "Could not start the run";
          say(why, "bad");
          toast(why, "bad");
          setRunLabel("Run", false);
          return;
        }

        if (where === "github") {
          // GitHub owns the logs for a dispatched run; there is nothing to
          // stream back here.
          say(res.body.message, "ok");
          logEl.textContent =
            "Dispatched to GitHub Actions.\n" +
            "Follow it in the Actions tab, or on the GitHub mobile app.\n" +
            "Results land in Postgres — reload this page when it finishes.";
          setRunLabel("Run", false);
          return;
        }

        setRunLabel("Running", true);
        timer = setInterval(poll, POLL_MS);
        poll();
      })
      .catch(function (err) {
        say("Could not reach the server: " + err.message, "bad");
        setRunLabel("Run", false);
      });
  }

  if (runBtn) runBtn.addEventListener("click", startRun);
  if (closeBtn) {
    closeBtn.addEventListener("click", function () { panel.hidden = true; });
  }

  // ------------------------------------------------------- status recording

  var COUNT_KEY_BY_HREF = {
    "/dashboard": "pending",
    "/dashboard/applied": "applied",
    "/dashboard/skipped": "skipped"
  };

  function updateCounts(counts) {
    if (!counts) return;
    var tabs = document.querySelectorAll(".tabs .tab");
    Array.prototype.forEach.call(tabs, function (tab) {
      var key = COUNT_KEY_BY_HREF[tab.getAttribute("href")];
      var slot = tab.querySelector(".tab__n");
      if (key && slot && typeof counts[key] === "number") {
        slot.textContent = counts[key];
      }
    });
  }

  function dropRow(jobId) {
    var row = document.querySelector('.job[data-job-id="' + jobId + '"]');
    if (!row) return;
    var list = row.parentNode;
    row.classList.add("is-going");
    setTimeout(function () {
      row.remove();
      // The empty state is server-rendered, so hand the page back to the
      // server once the last row goes.
      if (list && !list.querySelector(".job")) window.location.reload();
    }, 200);
  }

  var DONE_SAYS = {
    applied: "Applied",
    dismissed: "Dismissed",
    pending: "Moved back to matches"
  };

  function setStatus(jobId, status) {
    return fetch("/api/jobs/" + encodeURIComponent(jobId) + "/status", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: status })
    })
      .then(function (r) {
        return r.json().then(function (b) { return { ok: r.ok, body: b }; });
      })
      .then(function (res) {
        if (!res.ok) {
          toast(res.body.detail || "Could not save that", "bad");
          return;
        }
        updateCounts(res.body.counts);
        dropRow(jobId);
        toast(DONE_SAYS[status] || "Saved");
      })
      .catch(function (err) {
        toast("Could not reach the server: " + err.message, "bad");
      });
  }

  document.addEventListener("click", function (event) {
    var btn = event.target.closest ? event.target.closest(".js-status") : null;
    if (!btn) return;
    event.preventDefault();
    setStatus(btn.getAttribute("data-job-id"), btn.getAttribute("data-status"));
  });

  // --------------------------------------------------------- did you apply?

  var armed = null;
  var returnFocusTo = null;

  function saveArmed() {
    try {
      if (armed) sessionStorage.setItem(ARMED_KEY, JSON.stringify(armed));
      else sessionStorage.removeItem(ARMED_KEY);
    } catch (e) { /* private mode — the in-memory copy still works */ }
  }

  function loadArmed() {
    try {
      var raw = sessionStorage.getItem(ARMED_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (e) { return null; }
  }

  function disarm() {
    armed = null;
    saveArmed();
  }

  function openConfirm(job) {
    if (!modal || !modal.hidden) return;
    $("confirm-role").textContent = job.role || "This role";
    $("confirm-company").textContent = job.company || "";
    returnFocusTo = document.activeElement;
    modal.hidden = false;
    $("confirm-yes").focus();
  }

  function closeConfirm() {
    if (!modal || modal.hidden) return;
    modal.hidden = true;
    if (returnFocusTo && returnFocusTo.focus) returnFocusTo.focus();
    returnFocusTo = null;
  }

  if (modal) {
    $("confirm-yes").addEventListener("click", function () {
      var job = armed;
      disarm();
      closeConfirm();
      if (job) setStatus(job.jobId, "applied");
    });

    $("confirm-no").addEventListener("click", function () {
      disarm();
      closeConfirm();
    });

    // Clicking the backdrop leaves the job pending — the same as "Not yet".
    modal.addEventListener("click", function (event) {
      if (event.target === modal) { disarm(); closeConfirm(); }
    });

    document.addEventListener("keydown", function (event) {
      if (modal.hidden) return;
      if (event.key === "Escape") {
        event.preventDefault();
        disarm();
        closeConfirm();
      } else if (event.key === "Enter") {
        event.preventDefault();
        $("confirm-yes").click();
      }
    });
  }

  document.addEventListener("click", function (event) {
    var link = event.target.closest ? event.target.closest(".js-apply") : null;
    if (!link) return;
    // The link opens the posting in a new tab on its own; all we do is
    // remember what to ask about on the way back.
    armed = {
      jobId: link.getAttribute("data-job-id"),
      role: link.getAttribute("data-role"),
      company: link.getAttribute("data-company"),
      left: false
    };
    saveArmed();
  });

  function noteDeparture() {
    if (armed && !armed.left) { armed.left = true; saveArmed(); }
  }

  function askIfDue() {
    // Only ask once the operator has actually been away — otherwise the
    // dialog would appear the instant Apply is clicked.
    if (armed && armed.left) openConfirm(armed);
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) noteDeparture();
    else askIfDue();
  });
  window.addEventListener("blur", noteDeparture);
  window.addEventListener("focus", askIfDue);

  // ------------------------------------------------------------ page set-up

  // Action dates render as text on the server and are localised here, so the
  // page shows the operator's own timezone without the server guessing it.
  Array.prototype.forEach.call(
    document.querySelectorAll("[data-date]"),
    function (el) {
      var when = new Date(el.getAttribute("data-date"));
      if (!isNaN(when.getTime())) {
        el.textContent = when.toLocaleDateString(undefined, {
          day: "numeric", month: "short"
        });
      }
    }
  );

  // When a posting was found, to the hour: a job ad goes stale quickly, so
  // "2 Aug" and "today 09:14" are different decisions.
  Array.prototype.forEach.call(
    document.querySelectorAll("[data-datetime]"),
    function (el) {
      var when = new Date(el.getAttribute("data-datetime"));
      if (isNaN(when.getTime())) return;
      var days = Math.floor((Date.now() - when.getTime()) / 86400000);
      var time = when.toLocaleTimeString(undefined, {
        hour: "2-digit", minute: "2-digit"
      });
      el.textContent = days < 1
        ? "today " + time
        : when.toLocaleDateString(undefined, { day: "numeric", month: "short" })
          + " " + time;
    }
  );

  // A run started before this page loaded (a reload mid-run, or another tab)
  // should still be followed rather than looking idle.
  fetch("/api/run/status")
    .then(function (r) { return r.json(); })
    .then(function (state) {
      if (!state.running) return;
      panel.hidden = false;
      renderLog(state.log_lines);
      setRunLabel("Running", true);
      say((state.dry_run ? "Dry run" : "Run") + " in progress", "live");
      timer = setInterval(poll, POLL_MS);
    })
    .catch(function () { /* endpoint not ready; nothing to resume */ });

  // An Apply armed before a reload survives it.
  armed = loadArmed();
  if (armed) askIfDue();
})();
