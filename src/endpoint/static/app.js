/* Dashboard behaviour: start a run, follow it, refresh the table when it ends.
 *
 * Deliberately dependency-free — this ships inside the same container as the
 * resume endpoint and a build step would earn nothing at this size.
 */
(function () {
  "use strict";

  var POLL_MS = 2000;

  var runBtn = document.getElementById("run-btn");
  var dryRun = document.getElementById("dry-run");
  var target = document.getElementById("run-target");
  var panel = document.getElementById("run-panel");
  var logEl = document.getElementById("run-log");
  var label = document.getElementById("run-label");
  var dot = document.getElementById("run-dot");
  var closeBtn = document.getElementById("run-close");

  var timer = null;

  function showPanel() { panel.hidden = false; }

  function setDot(kind) {
    dot.className = "dot" + (kind ? " dot--" + kind : "");
  }

  function say(text, kind) {
    label.textContent = text;
    setDot(kind);
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
    runBtn.disabled = false;
    runBtn.textContent = "Run";
    if (state.returncode === 0) {
      say("Run finished", "ok");
      refreshMatches();
    } else {
      say("Run failed (exit " + state.returncode + ")", "bad");
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

  function refreshMatches() {
    // Full reload rather than client-side re-render: the page is server-
    // rendered, and a run also refreshes the counts in the top bar.
    window.location.reload();
  }

  function startRun() {
    var where = target.value;
    runBtn.disabled = true;
    runBtn.textContent = "Starting";
    showPanel();
    logEl.textContent = "";
    say("Starting…", "live");

    fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dry_run: dryRun.checked, target: where })
    })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        if (!res.ok) {
          say(res.body.message || "Could not start the run", "bad");
          runBtn.disabled = false;
          runBtn.textContent = "Run";
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
          runBtn.disabled = false;
          runBtn.textContent = "Run";
          return;
        }

        runBtn.textContent = "Running";
        timer = setInterval(poll, POLL_MS);
        poll();
      })
      .catch(function (err) {
        say("Could not reach the server: " + err.message, "bad");
        runBtn.disabled = false;
        runBtn.textContent = "Run";
      });
  }

  runBtn.addEventListener("click", startRun);
  closeBtn.addEventListener("click", function () { panel.hidden = true; });

  // A run started before this page loaded (a reload mid-run, or another tab)
  // should still be followed rather than looking idle.
  fetch("/api/run/status")
    .then(function (r) { return r.json(); })
    .then(function (state) {
      if (!state.running) return;
      showPanel();
      renderLog(state.log_lines);
      runBtn.disabled = true;
      runBtn.textContent = "Running";
      say((state.dry_run ? "Dry run" : "Run") + " in progress", "live");
      timer = setInterval(poll, POLL_MS);
    })
    .catch(function () { /* endpoint not ready; nothing to resume */ });
})();
