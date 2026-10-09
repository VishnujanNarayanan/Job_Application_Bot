/* Tailor page (#20): post pasted advert text to /api/tailor and draw the
 * breakdown it returns. Same breakdown as `python -m src.cli.tailor`, built by
 * src.tailor.breakdown, so the two surfaces cannot disagree.
 *
 * Dependency-free like app.js. Text is always set with textContent, never as
 * HTML: every string here came from a pasted advert or the LLM's reading of it.
 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var form = $("tailor-form");
  if (!form) return;

  var goBtn = $("tf-go");
  var goText = $("tf-go-text");
  var note = $("tf-note");
  var out = $("tailor-out");

  function fmt(n) { return (n === null || n === undefined) ? "—" : Number(n).toFixed(3); }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function chips(host, items, empty) {
    host.textContent = "";
    if (!items.length) { host.appendChild(el("span", "muted", empty)); return; }
    items.forEach(function (t) { host.appendChild(el("span", "gap", t)); });
  }

  function busy(on) {
    goBtn.disabled = on;
    goBtn.classList.toggle("is-busy", on);
    goText.textContent = on ? "Working…" : "Generate";
    note.textContent = "";
  }

  function draw(b) {
    $("to-title").textContent = b.role + " at " + b.company;
    $("to-sub").textContent = b.job_id + " · " +
      (b.reused_parse ? "reused the stored parse" : "new parse") + " · " +
      b.role_level + ", " + b.years_required + " years asked";
    $("to-pdf").href = b.pdf_url;
    $("to-docx").href = b.docx_url;

    $("to-score").textContent = Number(b.final_score).toFixed(2);
    $("to-fill").style.width = (100 * Math.max(0, Math.min(1, b.final_score))).toFixed(1) + "%";
    $("to-track").setAttribute("aria-label",
      "Score " + Number(b.final_score).toFixed(2) + " against a threshold of " + b.threshold);
    $("to-below").hidden = !b.below_threshold;

    var kv = $("to-score-kv");
    kv.textContent = "";
    [
      ["Final", fmt(b.final_score)],
      ["Fit", fmt(b.fit)],
      ["Lead entry", fmt(b.lead_entry) + " (similarity " + fmt(b.similarity) +
        ", lead coverage " + fmt(b.lead_coverage) + ")"],
      ["Keyword coverage", fmt(b.keyword_coverage)],
      ["Repetition", fmt(b.repetition)],
      ["Applicants", fmt(b.applicant_multiplier) + " (" +
        (b.applicants === null ? "unknown count" : b.applicants + " applicants") + ")"]
    ].forEach(function (row) {
      var div = el("div", "tout__kvrow");
      div.appendChild(el("dt", "", row[0]));
      div.appendChild(el("dd", "", row[1]));
      kv.appendChild(div);
    });

    $("to-kw-h").textContent = "Required keywords · " + b.required_shown.length +
      "/" + b.required_total + " shown";
    chips($("to-shown"), b.required_shown, "none");
    chips($("to-missing"), b.required_missing, "none");
    $("to-nip-row").hidden = !b.not_in_profile.length;
    chips($("to-nip"), b.not_in_profile, "none");

    var list = $("to-entries");
    list.textContent = "";
    b.entries.forEach(function (e) {
      var li = el("li", "tout__entry");
      var head = el("div", "tout__entryhead");
      head.appendChild(el("span", "strong", e.header));
      head.appendChild(el("span", "muted",
        e.kind + " · score " + fmt(e.score) + " · " + e.bullets + " bullets"));
      li.appendChild(head);
      var adds = el("p", "gaps");
      adds.appendChild(el("span", "gaps__label", "Adds"));
      var host = el("span");
      chips(host, e.adds, "nothing new");
      adds.appendChild(host);
      li.appendChild(adds);
      list.appendChild(li);
    });

    var rows = $("to-filters");
    rows.textContent = "";
    b.filters.forEach(function (f) {
      var tr = el("tr");
      var mark = el("td", f.would_reject ? "tout__reject" : "muted",
        f.would_reject ? "would reject" : "pass");
      tr.appendChild(mark);
      tr.appendChild(el("td", "strong", f.name));
      tr.appendChild(el("td", "muted", f.detail));
      rows.appendChild(tr);
    });

    out.hidden = false;
    out.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  var stepsBox = $("tailor-steps");
  var stepsList = $("to-steps");

  function addStep(ev) {
    var li = el("li", "tsteps__item" + (/^\s/.test(ev.message) ? " tsteps__item--sub" : ""));
    li.appendChild(el("span", "tsteps__t", ev.t.toFixed(1) + "s"));
    li.appendChild(el("span", "tsteps__msg", ev.message.trim()));
    stepsList.appendChild(li);
    li.scrollIntoView({ block: "nearest" });
  }

  function fail(message) {
    busy(false);
    note.textContent = message;
    note.className = "tout__err";
  }

  // The endpoint streams newline-delimited JSON: many "step" events while it
  // works, then one "done" (the breakdown) or "error".
  function readStream(res) {
    var reader = res.body.getReader();
    var decoder = new TextDecoder();
    var buffer = "";
    var finished = false;

    function handle(line) {
      if (!line.trim()) return;
      var ev = JSON.parse(line);
      if (ev.type === "step") { addStep(ev); return; }
      finished = true;
      if (ev.type === "done") { busy(false); note.className = "muted"; draw(ev); }
      else { fail(ev.message || "Something went wrong."); }
    }

    function pump() {
      return reader.read().then(function (chunk) {
        if (chunk.done) {
          handle(buffer);
          if (!finished) fail("The server stopped before finishing.");
          return;
        }
        buffer += decoder.decode(chunk.value, { stream: true });
        var lines = buffer.split("\n");
        buffer = lines.pop();
        lines.forEach(handle);
        return pump();
      });
    }
    return pump();
  }

  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var payload = {};
    ["text", "company", "role", "location", "applicants", "url"].forEach(function (k) {
      payload[k] = $("tf-" + k).value;
    });
    busy(true);
    out.hidden = true;
    stepsList.textContent = "";
    stepsBox.hidden = false;
    fetch("/api/tailor", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })
      .then(function (res) {
        if (!res.ok) {
          return res.json().then(function (body) {
            stepsBox.hidden = true;
            fail(body.detail || body.message || "Something went wrong.");
          });
        }
        return readStream(res);
      })
      .catch(function () { fail("Could not reach the server."); });
  });
})();
