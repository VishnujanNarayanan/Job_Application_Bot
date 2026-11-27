// ==UserScript==
// @name         Job Bot autofill
// @namespace    job-application-bot
// @version      1.0.0
// @description  Fills ATS application forms from the job bot's answers (#154). Never submits.
// @match        https://*.greenhouse.io/*
// @match        https://jobs.lever.co/*
// @match        https://jobs.ashbyhq.com/*
// @match        https://*.myworkdayjobs.com/*
// @match        https://*.myworkdaysite.com/*
// @match        https://jobs.smartrecruiters.com/*
// @match        https://apply.workable.com/*
// @match        https://*.recruitee.com/*
// @match        https://ats.rippling.com/*
// @match        https://*.bamboohr.com/*
// @match        https://*.eightfold.ai/*
// @match        https://*.keka.com/*
// @match        https://*.oraclecloud.com/*
// @match        https://www.amazon.jobs/*
// @match        https://amazon.jobs/*
// @grant        GM_xmlhttpRequest
// @grant        GM_registerMenuCommand
// @connect      __JAB_HOST__
// @run-at       document-idle
// @downloadURL  __JAB_ENDPOINT__/autofill/jab.user.js
// @updateURL    __JAB_ENDPOINT__/autofill/jab.user.js
// ==/UserScript==

/*
 * How it works (docs/autofill.md):
 *  - The Telegram Apply button opens the posting with #jab=<job_id>. That tag
 *    starts autofill for this tab; without it, an "Autofill" button appears
 *    on pages with a form, and nothing happens until it is pressed.
 *  - Every visible, still-empty field is described (label, surrounding text,
 *    type, options) and sent to the endpoint, which answers each one.
 *  - Known answers are typed in, the tailored resume PDF is attached, and
 *    anything unknown or worth checking is outlined. Pages that appear later
 *    (Workday's next step) are filled as they appear.
 *  - It never clicks a submit button, never presses Enter, never touches a
 *    field that already has a value.
 */
(function () {
  'use strict';

  const ENDPOINT = '__JAB_ENDPOINT__';
  const SESSION_KEY = 'jab-session';
  const SESSION_MS = 3 * 3600 * 1000;
  const TAG = /([#&])jab=([^&]*)/;

  // ------------------------------------------------------------------------
  // Session: which match this tab is applying to
  // ------------------------------------------------------------------------

  function takeTag() {
    const m = location.hash.match(TAG);
    if (!m) return null;
    const id = decodeURIComponent(m[2]);
    // Drop the tag so the page's own router and analytics never see it.
    let hash = location.hash.replace(TAG, (all, sep) => (sep === '#' ? '#' : '')).replace(/^#&/, '#');
    if (hash === '#') hash = '';
    history.replaceState(history.state, '', location.pathname + location.search + hash);
    return id;
  }

  function session() {
    const tagged = takeTag();
    if (tagged) {
      const s = { jobId: tagged, at: Date.now(), opened: false };
      sessionStorage.setItem(SESSION_KEY, JSON.stringify(s));
      return s;
    }
    try {
      const s = JSON.parse(sessionStorage.getItem(SESSION_KEY) || 'null');
      if (s && Date.now() - s.at < SESSION_MS) return s;
    } catch (e) { /* a bad value is no session */ }
    return null;
  }

  function saveSession(s) {
    if (s) sessionStorage.setItem(SESSION_KEY, JSON.stringify(s));
  }

  // ------------------------------------------------------------------------
  // Endpoint
  // ------------------------------------------------------------------------

  function request(method, url, opts) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest(Object.assign({
        method, url, timeout: 20000,
        onload: (r) => (r.status >= 200 && r.status < 300 ? resolve(r) : reject(new Error('HTTP ' + r.status))),
        onerror: () => reject(new Error('endpoint unreachable')),
        ontimeout: () => reject(new Error('endpoint timed out')),
      }, opts || {}));
    });
  }

  async function matchFields(body) {
    const r = await request('POST', ENDPOINT + '/autofill/match', {
      headers: { 'Content-Type': 'application/json', 'X-JAB': '1' },
      data: JSON.stringify(body),
    });
    return JSON.parse(r.responseText);
  }

  async function fetchResume(url) {
    const r = await request('GET', url, { responseType: 'blob' });
    return r.response;
  }

  // ------------------------------------------------------------------------
  // Reading the form
  // ------------------------------------------------------------------------

  // Whitespace only: a trailing * is how most forms mark a required field.
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const PANEL_ID = 'jab-panel';

  function visible(el) {
    if (!el || !el.isConnected) return false;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  }

  // Text of a node without the controls inside it (a <label> wrapping a <select>
  // would otherwise read as the select's two hundred country names).
  function textOf(node) {
    if (!node) return '';
    const copy = node.cloneNode(true);
    copy.querySelectorAll('input, select, textarea, option, script, style, svg, [role=listbox], [role=option]')
      .forEach((n) => n.remove());
    // Join text nodes with spaces: textContent runs "Resume" and "Upload" together.
    const parts = [];
    const walk = document.createTreeWalker(copy, NodeFilter.SHOW_TEXT);
    while (walk.nextNode()) parts.push(walk.currentNode.nodeValue);
    return clean(parts.join(' '));
  }

  function byIds(ids) {
    return clean((ids || '').split(/\s+/).map((i) => textOf(document.getElementById(i))).join(' '));
  }

  function labelOf(el) {
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = byIds(lb); if (t) return t; }
    if (el.labels && el.labels.length) {
      const t = textOf(el.labels[0]);
      if (t) return t;
    }
    for (const ref of [el.id, el.name]) {
      if (!ref) continue;
      const l = document.querySelector(`label[for="${CSS.escape(ref)}"]`);
      if (l && textOf(l)) return textOf(l);
    }
    const aria = el.getAttribute('aria-label');
    if (aria) return clean(aria);
    return clean(el.getAttribute('placeholder') || el.getAttribute('title') || '');
  }

  // The nearest enclosing text that is not the field's own: a question heading
  // beside "Attach", or above a group of radio buttons.
  function contextOf(el, exclude) {
    let node = el.parentElement;
    for (let depth = 0; node && depth < 7; depth++, node = node.parentElement) {
      if (node.tagName === 'FORM' || node === document.body) break;
      const legend = node.tagName === 'FIELDSET' && node.querySelector('legend');
      if (legend && textOf(legend)) return textOf(legend).slice(0, 300);
      const lb = node.getAttribute('aria-labelledby');
      if (lb && byIds(lb)) return byIds(lb).slice(0, 300);
      let t = textOf(node);
      for (const x of exclude || []) t = t.replace(x, ' ');
      t = clean(t);
      if (t.length >= 2) return t.slice(0, 300);
    }
    return '';
  }

  function requiredOf(el, label) {
    return !!(el.required || el.getAttribute('aria-required') === 'true' || /[*✱]/.test(label || ''));
  }

  // Some forms mark "required" only with a class on the question's title.
  function isRequiredBox(el) {
    const entry = el.closest('fieldset, [class*="field"], [class*="question"]');
    return !!(entry && entry.querySelector('[class*="required" i]'));
  }

  function hasValue(el) {
    if (el.type === 'file') return el.files && el.files.length > 0;
    if (el.tagName === 'SELECT') {
      const o = el.options[el.selectedIndex];
      return !!(o && o.value && !/^(select|choose|please)/i.test(o.text.trim()));
    }
    return !!(el.value && el.value.trim());
  }

  const handled = new WeakSet();
  let refSeq = 0;
  const refs = new Map();

  function remember(kind, els, extra) {
    const ref = 'f' + (++refSeq);
    els.forEach((e) => handled.add(e));
    refs.set(ref, Object.assign({ kind, els }, extra || {}));
    return ref;
  }

  function optionLabel(el) {
    return clean(labelOf(el) || (el.parentElement && textOf(el.parentElement)) || el.value);
  }

  function scan() {
    const fields = [];
    const groups = new Map();
    const nodes = document.querySelectorAll(
      'input, select, textarea, [role="combobox"], button[aria-haspopup="listbox"]');
    for (const el of nodes) {
      if (handled.has(el) || el.closest('#' + PANEL_ID)) continue;
      if (el.disabled || el.readOnly) continue;
      const type = (el.getAttribute('type') || el.type || '').toLowerCase();
      if (['hidden', 'submit', 'button', 'image', 'reset', 'password'].includes(type)) continue;
      if (/captcha/i.test((el.name || '') + (el.id || ''))) continue;
      const isFile = type === 'file';
      const isTick = type === 'radio' || type === 'checkbox';
      // Styled file inputs and ticks hide the real element: judge by its label.
      const shown = visible(el) || ((isFile || isTick) && (
        (el.labels && [...el.labels].some(visible)) || visible(el.parentElement)));
      if (!shown) continue;

      const toggles = isTick && el.parentElement
        ? [...el.parentElement.querySelectorAll(':scope > button[aria-pressed], :scope > button[data-option]')] : [];
      if (toggles.length >= 2) {
        if (toggles.some((b) => b.getAttribute('aria-pressed') === 'true')) { handled.add(el); continue; }
        const label = labelOf(el) || contextOf(el, toggles.map((b) => clean(b.textContent)));
        fields.push({
          ref: remember('buttons', [el], { buttons: toggles }), label, context: label, name: el.name || '',
          id: el.id || '', automation_id: '', autocomplete: '', type: 'radio',
          options: toggles.map((b) => clean(b.textContent)), required: requiredOf(el, label) || isRequiredBox(el),
        });
        continue;
      }
      if (isTick) {
        const box = el.closest('[role="radiogroup"], [role="group"], fieldset');
        const key = type + ':' + (el.name || (box && (box.id || box.getAttribute('aria-labelledby'))) || Math.random());
        if (!groups.has(key)) groups.set(key, { type, els: [] });
        groups.get(key).els.push(el);
        continue;
      }
      if (hasValue(el) && !isFile) { handled.add(el); continue; }
      if (isFile && hasValue(el)) { handled.add(el); continue; }

      const label = labelOf(el);
      const combo = el.getAttribute('role') === 'combobox' || el.getAttribute('aria-haspopup') === 'listbox'
        || el.getAttribute('aria-autocomplete') === 'list';
      let ftype = el.tagName === 'SELECT' ? (el.multiple ? 'multiselect' : 'select')
        : el.tagName === 'TEXTAREA' ? 'textarea' : combo ? 'combobox' : (type || 'text');
      if (el.tagName === 'BUTTON') ftype = 'combobox';
      const options = el.tagName === 'SELECT' ? [...el.options].map((o) => clean(o.text)) : [];
      const f = {
        ref: remember(ftype, [el]), label, context: contextOf(el, [label]),
        name: el.name || '', id: el.id || '', automation_id: el.getAttribute('data-automation-id') || '',
        autocomplete: el.getAttribute('autocomplete') || '', type: ftype, options,
        required: requiredOf(el, label),
      };
      fields.push(f);
    }

    for (const g of groups.values()) {
      if (g.els.some((e) => e.checked)) { g.els.forEach((e) => handled.add(e)); continue; }
      const opts = g.els.map(optionLabel);
      if (g.type === 'checkbox' && g.els.length === 1) {
        const el = g.els[0];
        const own = optionLabel(el);
        fields.push({
          ref: remember('checkbox', g.els), label: own, context: contextOf(el, [own]), name: el.name || '',
          id: el.id || '', automation_id: '', autocomplete: '', type: 'checkbox', options: [],
          required: requiredOf(el, own),
        });
        continue;
      }
      // The question is the text around the whole group, minus the options.
      let box = g.els[0].parentElement;
      while (box && !g.els.every((e) => box.contains(e))) box = box.parentElement;
      const legend = box && box.tagName === 'FIELDSET' && box.querySelector('legend');
      const lb = box && box.getAttribute('aria-labelledby');
      let question = (legend && textOf(legend)) || (lb && byIds(lb)) || '';
      if (!question && box) question = contextOf(g.els[0], opts);
      const ftype = g.type === 'radio' ? 'radio' : 'checkbox_group';
      fields.push({
        ref: remember(ftype, g.els), label: question, context: question, name: g.els[0].name || '',
        id: '', automation_id: '', autocomplete: '', type: ftype, options: opts,
        required: g.els.some((e) => e.required) || /[*✱]/.test(question),
      });
    }
    return fields;
  }

  // ------------------------------------------------------------------------
  // Filling
  // ------------------------------------------------------------------------

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const fire = (el, type) => el.dispatchEvent(new Event(type, { bubbles: true }));

  // React and friends track the value on the prototype setter; assigning
  // el.value directly is overwritten on the next render.
  function setNative(el, value) {
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype
      : el.tagName === 'SELECT' ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, value);
  }

  function typeInto(el, value) {
    el.focus();
    setNative(el, value);
    fire(el, 'input');
    fire(el, 'change');
    el.blur();
    fire(el, 'blur');
  }

  const n = (t) => clean(t).toLowerCase().replace(/[^a-z0-9+]+/g, ' ').trim();

  function chooseSelect(el, text) {
    const want = n(text);
    const opt = [...el.options].find((o) => n(o.text) === want);
    if (!opt) return false;
    setNative(el, opt.value);
    fire(el, 'input');
    fire(el, 'change');
    return true;
  }

  function pressOn(el) {
    for (const t of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
      el.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window }));
    }
  }

  function tick(els, texts) {
    const want = new Set(texts.map(n));
    let done = 0;
    for (const el of els) {
      if (want.has(n(optionLabel(el))) && !el.checked) { el.click(); done++; }
    }
    return done > 0;
  }

  async function chooseCombo(el, text) {
    const want = n(text);
    const input = el.tagName === 'INPUT' ? el : el.querySelector('input');
    if (input) {
      input.focus();
      setNative(input, text);
      fire(input, 'input');
    } else {
      pressOn(el);
    }
    for (let i = 0; i < 12; i++) {
      await sleep(250);
      const opts = [...document.querySelectorAll('[role="option"]')].filter(visible);
      const hit = opts.find((o) => n(o.textContent) === want)
        || opts.find((o) => n(o.textContent).startsWith(want + ' '))
        || (opts.length === 1 && n(opts[0].textContent).includes(want) ? opts[0] : null);
      if (hit) { pressOn(hit); await sleep(150); return true; }
    }
    // Close the list without choosing. Escape never submits.
    (input || el).dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    if (input) { setNative(input, ''); fire(input, 'input'); }
    return false;
  }

  // A text box with a suggestion list beside it (Lever's location): typing alone
  // leaves the choice unmade. Search, then click the suggestion that matches.
  function looksTypeahead(el) {
    if (el.getAttribute('aria-autocomplete') || el.getAttribute('aria-controls') || el.getAttribute('role') === 'combobox') return true;
    const box = el.parentElement;
    return !!(box && box.querySelector('[class*="dropdown" i], [class*="autocomplete" i], [class*="suggest" i], [class*="typeahead" i]'));
  }

  function suggestionsNear(el) {
    const scope = el.closest('li, .application-field, [class*="field" i]') || el.parentElement;
    const local = [...scope.querySelectorAll('[role="option"], [class*="result" i] > *, [class*="suggest" i] > *, li')]
      .filter((o) => visible(o) && o !== el && !o.contains(el) && clean(o.textContent));
    return local.length ? local : [...document.querySelectorAll('[role="option"]')].filter(visible);
  }

  async function typeahead(el, search, full) {
    el.focus();
    setNative(el, search);
    fire(el, 'input');
    el.dispatchEvent(new KeyboardEvent('keyup', { key: search.slice(-1), bubbles: true }));
    const want = n(search);
    for (let i = 0; i < 16; i++) {
      await sleep(250);
      const opts = suggestionsNear(el);
      const hit = opts.find((o) => n(o.textContent) === n(full)) || opts.find((o) => n(o.textContent).startsWith(want));
      if (hit) { pressOn(hit); await sleep(200); return true; }
    }
    // No list came: keep the full answer typed, for a person to confirm.
    setNative(el, full);
    fire(el, 'input');
    fire(el, 'change');
    return false;
  }

  function attach(el, blob, filename) {
    const dt = new DataTransfer();
    dt.items.add(new File([blob], filename, { type: 'application/pdf' }));
    el.files = dt.files;
    fire(el, 'input');
    fire(el, 'change');
    return el.files.length === 1;
  }

  // ------------------------------------------------------------------------
  // Showing what happened
  // ------------------------------------------------------------------------

  const STYLE = `
    .jab-review  { outline: 2px solid #e0a000 !important; outline-offset: 2px; }
    .jab-missing { outline: 2px solid #d93025 !important; outline-offset: 2px; }
    .jab-optional{ outline: 2px dashed #5b8def !important; outline-offset: 2px; }
    #${PANEL_ID} { position: fixed; right: 16px; bottom: 16px; z-index: 2147483647; max-width: 340px;
      font: 13px/1.4 system-ui, sans-serif; background: #1f2328; color: #f0f0f0; border-radius: 8px;
      padding: 10px 12px; box-shadow: 0 4px 16px rgba(0,0,0,.35); }
    #${PANEL_ID} button { font: inherit; margin: 6px 6px 0 0; padding: 4px 10px; border-radius: 6px;
      border: 1px solid #555; background: #2d333b; color: #f0f0f0; cursor: pointer; }
    #${PANEL_ID} .k { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; }
  `;

  function panel() {
    let p = document.getElementById(PANEL_ID);
    if (!p) {
      const style = document.createElement('style');
      style.textContent = STYLE;
      document.head.appendChild(style);
      p = document.createElement('div');
      p.id = PANEL_ID;
      document.body.appendChild(p);
    }
    return p;
  }

  function say(html, buttons) {
    const p = panel();
    p.innerHTML = '<b>Job Bot</b> · ' + html;
    for (const [label, fn] of buttons || []) {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = label;
      b.addEventListener('click', fn);
      p.appendChild(b);
    }
    const close = document.createElement('button');
    close.type = 'button';
    close.textContent = 'Hide';
    close.addEventListener('click', () => p.remove());
    p.appendChild(close);
  }

  function mark(ref, cls, why) {
    const r = refs.get(ref);
    if (!r) return;
    let target = r.kind === 'buttons' ? r.buttons[0].parentElement : r.els[0];
    if (r.kind !== 'buttons' && (r.els.length > 1 || r.kind === 'file' || target.type === 'checkbox' || target.type === 'radio')) {
      target = r.els[0].closest('fieldset, [role="radiogroup"], [role="group"]') || r.els[0].parentElement || target;
      if (r.els.length > 1) {
        while (target && !r.els.every((e) => target.contains(e))) target = target.parentElement;
      }
    }
    if (!target) return;
    target.classList.add(cls);
    if (why) target.title = 'Job Bot: ' + why;
  }

  // ------------------------------------------------------------------------
  // One pass over the page
  // ------------------------------------------------------------------------

  const totals = { filled: 0, review: 0, missing: 0, optional: 0, resume: '' };
  let busy = false;
  let active = false;
  let current = null;

  function summary() {
    const bits = [];
    if (totals.filled) bits.push(`${totals.filled} filled`);
    if (totals.review) bits.push(`<span class="k" style="background:#e0a000"></span>${totals.review} to check`);
    if (totals.missing) bits.push(`<span class="k" style="background:#d93025"></span>${totals.missing} required, no answer`);
    if (totals.optional) bits.push(`<span class="k" style="border:1px dashed #5b8def"></span>${totals.optional} optional, no answer`);
    if (totals.resume) bits.push(totals.resume);
    return (bits.join(' · ') || 'nothing to fill') + '<br><small>Review the form and submit it yourself.</small>';
  }

  async function apply(decision, resume) {
    const r = refs.get(decision.ref);
    if (!r) return;
    const el = r.els[0];
    const a = decision.action;
    if (a === 'skip') return;
    if (a === 'resume') {
      if (!resume) {
        totals.resume = 'resume: attach it yourself (no match tag)';
        return mark(decision.ref, 'jab-missing', 'attach your resume');
      }
      try {
        const blob = await fetchResume(resume.url);
        if (attach(el, blob, resume.filename)) { totals.resume = 'resume attached'; return; }
      } catch (e) {
        totals.resume = 'resume: not available (' + e.message + ')';
      }
      return mark(decision.ref, 'jab-missing', 'attach your resume');
    }
    if (a === 'unknown') {
      if (decision.required || r.required) { totals.missing++; return mark(decision.ref, 'jab-missing', decision.reason || 'no answer yet'); }
      totals.optional++;
      return mark(decision.ref, 'jab-optional', decision.reason || 'no answer yet');
    }
    const v = decision.value;
    let ok = false;
    if (r.kind === 'select' || r.kind === 'multiselect') ok = chooseSelect(el, Array.isArray(v) ? v[0] : v);
    else if (r.kind === 'radio' || r.kind === 'checkbox_group') ok = tick(r.els, Array.isArray(v) ? v : [v]);
    else if (r.kind === 'checkbox') { if (v === true && !el.checked) el.click(); ok = true; if (v !== true) return; }
    else if (r.kind === 'buttons') {
      const b = r.buttons.find((x) => n(x.textContent) === n(String(v)));
      if (b) { b.click(); ok = true; }
    } else if (r.kind === 'combobox') {
      ok = await chooseCombo(el, String(v));
      if (!ok) { totals.review++; return mark(decision.ref, 'jab-review', 'choose: ' + v); }
    } else if (decision.search && looksTypeahead(el)) {
      if (!(await typeahead(el, decision.search, String(v)))) {
        totals.review++;
        return mark(decision.ref, 'jab-review', 'typed ' + v + '; pick it from the list if one appears');
      }
      ok = true;
    } else { typeInto(el, String(v)); ok = true; }
    if (!ok) {
      if (r.required) { totals.missing++; return mark(decision.ref, 'jab-missing', 'could not choose: ' + v); }
      totals.optional++;
      return mark(decision.ref, 'jab-optional', 'could not choose: ' + v);
    }
    if (a === 'review') { totals.review++; mark(decision.ref, 'jab-review', 'filled; please check'); }
    else totals.filled++;
  }

  async function pass() {
    if (busy) return;
    busy = true;
    try {
      const fields = scan();
      if (!fields.length) return;
      fields.forEach((f) => { refs.get(f.ref).required = f.required; });
      let res;
      try {
        res = await matchFields({
          url: location.href, job_id: current && current.jobId,
          first: !!(current && !current.opened), fields,
        });
      } catch (e) {
        fields.forEach((f) => { const r = refs.get(f.ref); r && r.els.forEach((x) => handled.delete(x)); });
        say('endpoint not reachable (' + e.message + '). Nothing filled.', [['Retry', run]]);
        return;
      }
      if (current && !current.opened) { current.opened = true; saveSession(current); }
      for (const d of res.decisions) {
        const f = fields.find((x) => x.ref === d.ref);
        d.required = f && f.required;
        try { await apply(d, res.resume); } catch (e) { mark(d.ref, 'jab-missing', 'fill failed: ' + e.message); }
      }
      say(summary());
    } finally {
      busy = false;
    }
  }

  // Workday and other single-page forms swap in each step without a reload.
  let timer = null;
  const observer = new MutationObserver(() => {
    if (!active) return;
    clearTimeout(timer);
    timer = setTimeout(pass, 900);
  });

  function run() {
    active = true;
    observer.observe(document.body, { childList: true, subtree: true });
    pass();
  }

  function offer() {
    // Untagged page: never fill unasked. Offer once a real form is on screen.
    const check = () => {
      if (active || document.getElementById(PANEL_ID)) return true;
      const inputs = [...document.querySelectorAll('input[type=text], input[type=email], input:not([type]), textarea')]
        .filter(visible);
      if (inputs.length >= 3) { say('a form is open.', [['Autofill', run]]); return true; }
      return false;
    };
    if (check()) return;
    const watch = new MutationObserver(() => { if (check()) watch.disconnect(); });
    watch.observe(document.body, { childList: true, subtree: true });
    setTimeout(() => watch.disconnect(), 120000);
  }

  current = session();
  GM_registerMenuCommand('Autofill this form', run);
  if (current) run(); else offer();
})();
