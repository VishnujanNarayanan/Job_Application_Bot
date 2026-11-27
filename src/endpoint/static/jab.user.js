// ==UserScript==
// @name         Job Bot autofill
// @namespace    job-application-bot
// @version      1.2.0
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
// @match        https://www.linkedin.com/jobs/*
// @match        https://www.naukri.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM_registerMenuCommand
// @grant        GM_getValue
// @grant        GM_setValue
// @connect      __JAB_HOST__
// @run-at       document-start
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
 *  - Opened from Telegram on LinkedIn or Naukri, it only remembers the job
 *    (30 minutes) and has the endpoint save that job's resume to Downloads;
 *    the company site you reach from there is then filled for that job.
 *  - When you press Submit (or Next on a multi-page form), it reads what each
 *    field now holds and sends it to /autofill/learn, so an answer you typed
 *    or corrected is used next time. It doesn't delay or block the submit.
 */
(function () {
  'use strict';

  const ENDPOINT = '__JAB_ENDPOINT__';
  const SESSION_KEY = 'jab-session';
  const SESSION_MS = 3 * 3600 * 1000;
  const TAG = /([#&])jab=([^&]*)/;
  const PENDING = 'jab-pending';   // the job last opened from Telegram, for the site LinkedIn sends you to
  const PENDING_MS = 30 * 60 * 1000;
  const BOARD_HOSTS = /(^|\.)(linkedin\.com|naukri\.com)$/;

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

  function session(tagged) {
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

  async function opened(jobId) {
    const r = await request('POST', ENDPOINT + '/autofill/opened', {
      headers: { 'Content-Type': 'application/json', 'X-JAB': '1' }, data: JSON.stringify({ job_id: jobId }),
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

  // The heading a field sits under, when it is a sibling above ("Resume/CV" over a drop zone).
  function headingBefore(el) {
    let node = el;
    for (let depth = 0; node && depth < 6; depth++, node = node.parentElement) {
      let sib = node.previousElementSibling;
      for (let i = 0; sib && i < 4; i++, sib = sib.previousElementSibling) {
        if (/^H[1-6]$/.test(sib.tagName) || sib.getAttribute('role') === 'heading') return textOf(sib);
        const h = sib.querySelector && sib.querySelector('h1, h2, h3, h4, h5, h6, [role="heading"]');
        if (h && !sib.querySelector('input, select, textarea, button')) return textOf(h);
      }
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

  // Workday's search-and-pick boxes ("How did you hear", School, Skills).
  const promptBox = (el) => el.closest('[data-automation-id="multiselectInputContainer"]');
  const PLACEHOLDER = /^(select( one)?|choose( one)?|please select|--|none selected|search)\b/i;

  function hasValue(el) {
    if (el.tagName === 'BUTTON') { const t = clean(el.textContent); return !!t && !PLACEHOLDER.test(t); }
    const box = promptBox(el);
    if (box) return !!box.querySelector('[data-automation-id="selectedItem"], [data-automation-id="selectedItemList"] li');
    if (el.type === 'file') return el.files && el.files.length > 0;
    if (el.tagName === 'SELECT') {
      const o = el.options[el.selectedIndex];
      return !!(o && o.value && !/^(select|choose|please)/i.test(o.text.trim()));
    }
    return !!(el.value && el.value.trim());
  }

  const handled = new WeakSet();
  const prefilled = new Set();  // labels of fields already answered (the phone code Workday pre-selects)
  let refSeq = 0;
  const refs = new Map();

  function remember(kind, els, extra) {
    const ref = 'f' + (++refSeq);
    els.forEach((e) => handled.add(e));
    refs.set(ref, Object.assign({ kind, els }, extra || {}));
    return ref;
  }

  // "Work Experience 1", "Education 2": the repeated section a field sits in.
  const SECTION = /^(work experience|experience|employment|education)\s*\d+/i;
  function groupOf(el) {
    let node = el.parentElement;
    for (let depth = 0; node && depth < 14; depth++, node = node.parentElement) {
      const lb = node.getAttribute && node.getAttribute('aria-labelledby');
      const t = (lb && byIds(lb)) || '';
      if (SECTION.test(t)) return t.match(SECTION)[0];
      const h = node.querySelector && node.querySelector(':scope > h2, :scope > h3, :scope > h4, :scope > h5, :scope > div > h3, :scope > div > h4, :scope > [role="heading"]');
      if (h && SECTION.test(clean(h.textContent))) return clean(h.textContent).match(SECTION)[0];
    }
    return '';
  }

  // Workday splits a date into Month / Day / Year boxes under one label ("From").
  function datePart(el) {
    const m = (el.getAttribute('data-automation-id') || '').match(/dateSection(Month|Day|Year)/);
    if (!m) return null;
    const field = el.closest('[data-automation-id^="formField-"]');
    const lab = field && field.querySelector('label, legend');
    return { part: m[1].toLowerCase(), of: lab ? textOf(lab) : '' };
  }

  const inPopup = (el) => !!el.closest('[role="listbox"], [data-automation-id="activeListContainer"], '
    + '[data-automation-widget="wd-popup"], [data-automation-id="promptOption"]');

  function optionLabel(el) {
    return clean(labelOf(el) || (el.parentElement && textOf(el.parentElement)) || el.value);
  }

  function scan() {
    const fields = [];
    const groups = new Map();
    const nodes = document.querySelectorAll(
      'input, select, textarea, [role="combobox"], button[aria-haspopup="listbox"]');
    for (const el of nodes) {
      if (handled.has(el) || el.closest('#' + PANEL_ID) || inPopup(el)) continue;
      if (el.disabled || el.readOnly) continue;
      const type = (el.getAttribute('type') || el.type || '').toLowerCase();
      // A listbox button (Workday's dropdowns) is a question, though its type is "button".
      if (el.tagName !== 'BUTTON' && ['hidden', 'submit', 'button', 'image', 'reset', 'password'].includes(type)) continue;
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
      if (hasValue(el) && !isFile) { handled.add(el); prefilled.add(labelOf(el)); continue; }
      if (isFile && hasValue(el)) { handled.add(el); continue; }

      let label = labelOf(el);
      const dp = datePart(el);
      if (dp) label = clean(dp.of + ' ' + dp.part);
      const combo = el.getAttribute('role') === 'combobox' || el.getAttribute('aria-haspopup') === 'listbox'
        || el.getAttribute('aria-autocomplete') === 'list';
      let ftype = el.tagName === 'SELECT' ? (el.multiple ? 'multiselect' : 'select')
        : el.tagName === 'TEXTAREA' ? 'textarea' : combo ? 'combobox' : (type || 'text');
      if (el.tagName === 'BUTTON') ftype = 'combobox';
      if (promptBox(el)) ftype = 'prompt';
      const options = el.tagName === 'SELECT' ? [...el.options].map((o) => clean(o.text)) : [];
      let context = contextOf(el, [label]);
      if (isFile) context = clean(headingBefore(el) + ' ' + context);
      const fieldBox = el.closest('[data-automation-id^="formField-"]');
      const f = {
        ref: remember(ftype, [el]), label, context,
        name: el.name || '', id: el.id || '', automation_id: el.getAttribute('data-automation-id') || '',
        autocomplete: el.getAttribute('autocomplete') || '', type: ftype, options,
        placeholder: el.getAttribute('placeholder') || '', group: groupOf(el),
        field_id: (fieldBox && fieldBox.getAttribute('data-automation-id')) || '',
        // A drop zone's own label says nothing; the "*" sits on the heading above it.
        required: requiredOf(el, label) || (isFile && /[*✱]/.test(context)) || !!(fieldBox && isRequiredBox(el)),
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
          id: el.id || '', automation_id: el.getAttribute('data-automation-id') || '', autocomplete: '',
          type: 'checkbox', options: [], group: groupOf(el), required: requiredOf(el, own),
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
        id: '', automation_id: '', autocomplete: '', type: ftype, options: opts, group: groupOf(g.els[0]),
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

  // The option among ``opts`` for the first answer in ``wants`` that one matches.
  function bestOption(opts, wants) {
    for (const w of wants) {
      const want = n(w);
      if (!want) continue;
      const hit = opts.find((o) => n(o.textContent) === want)
        || opts.find((o) => n(o.textContent).startsWith(want + ' '))
        || (want.length >= 4 ? opts.find((o) => (' ' + n(o.textContent) + ' ').includes(' ' + want + ' ')) : null);
      if (hit) return hit;
    }
    return null;
  }

  async function chooseCombo(el, text, alts) {
    const wants = [text].concat(alts || []);
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
      const hit = bestOption(opts, wants)
        || (input && opts.length === 1 && n(opts[0].textContent).includes(n(text)) ? opts[0] : null);
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

  // Workday's search box: type, Enter to search (a synthetic key never submits a
  // form), then click the result. A category ("Job Posting Sites >") opens its
  // list, which is searched again.
  const PROMPT_OPTS = '[data-automation-id="promptOption"], [data-automation-id="promptLeafNode"], [role="option"]';

  async function choosePrompt(el, wants) {
    const box = promptBox(el);
    const chosen = () => !!(box && box.querySelector('[data-automation-id="selectedItem"], [data-automation-id="selectedItemList"] li'));
    for (const want of wants) {
      el.focus();
      setNative(el, String(want));
      fire(el, 'input');
      for (const t of ['keydown', 'keypress', 'keyup']) {
        el.dispatchEvent(new KeyboardEvent(t, { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
      }
      for (let level = 0; level < 3 && !chosen(); level++) {
        let hit = null;
        for (let i = 0; i < 12 && !hit; i++) {
          await sleep(300);
          const opts = [...document.querySelectorAll(PROMPT_OPTS)].filter((o) => visible(o) && !o.closest('#' + PANEL_ID));
          hit = bestOption(opts, [want]);
        }
        if (!hit) break;
        const tickBox = hit.querySelector('input[type="checkbox"], input[type="radio"]');
        pressOn(tickBox || hit);
        await sleep(500);
      }
      if (chosen()) {
        el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
        return true;
      }
      setNative(el, '');
      fire(el, 'input');
    }
    el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
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
  // Reading what a field holds now (at submit)
  // ------------------------------------------------------------------------

  function selectedText(sel) {
    const o = sel.options[sel.selectedIndex];
    return o && o.value && !/^(select|choose|please)/i.test(o.text.trim()) ? clean(o.text) : '';
  }

  function readValue(r) {
    const el = r.els[0];
    switch (r.kind) {
      case 'select': return selectedText(el);
      case 'multiselect': return [...el.selectedOptions].map((o) => clean(o.text));
      case 'radio': { const c = r.els.find((e) => e.checked); return c ? optionLabel(c) : ''; }
      case 'checkbox_group': return r.els.filter((e) => e.checked).map(optionLabel);
      case 'checkbox': return el.checked ? 'Yes' : '';
      case 'file': return '';
      case 'buttons': {
        const b = r.buttons.find((x) => x.getAttribute('aria-pressed') === 'true'
          || /(^|[\s_-])(active|selected|checked)\b/i.test(x.className));
        return b ? clean(b.textContent) : '';
      }
      case 'prompt': {
        const box = promptBox(el);
        const items = box ? [...box.querySelectorAll('[data-automation-id="selectedItem"], [data-automation-id="selectedItemList"] li')]
          .map((x) => clean(x.textContent)).filter(Boolean) : [];
        return items.length > 1 ? items : (items[0] || '');
      }
      case 'combobox': {
        if (el.tagName === 'BUTTON') { const t = clean(el.textContent); return PLACEHOLDER.test(t) ? '' : t; }
        const box = el.closest('[class*="control" i], [class*="container" i]');
        const one = box && box.querySelector('[class*="single-value" i], [class*="singleValue" i]');
        if (one) return clean(one.textContent);
        const many = box ? [...box.querySelectorAll('[class*="multi-value__label" i], [class*="multiValueLabel" i]')]
          .map((x) => clean(x.textContent)) : [];
        return many.length ? many : clean(el.value);
      }
      default: return clean(el.value);
    }
  }

  const isEmpty = (v) => (Array.isArray(v) ? !v.length : !v);

  // ------------------------------------------------------------------------
  // Learning from what you submit
  // ------------------------------------------------------------------------

  const QUEUE = 'jab-learn-queue';
  const sent = new Map();  // ref -> the value last reported, so each is sent once
  const SUBMITISH = /^(submit|submit application|submit your application|apply|apply now|send|send application|next|continue|save and continue|save & continue|review|review and submit|finish)\b/i;

  async function flush() {
    const q = GM_getValue(QUEUE, []);
    if (!q.length) return;
    GM_setValue(QUEUE, []);
    const failed = [];
    for (const body of q) {
      try {
        await request('POST', ENDPOINT + '/autofill/learn', {
          headers: { 'Content-Type': 'application/json', 'X-JAB': '1' }, data: JSON.stringify(body),
        });
      } catch (e) { failed.push(body); }
    }
    if (failed.length) GM_setValue(QUEUE, failed.concat(GM_getValue(QUEUE, [])).slice(-20));
  }

  function report() {
    const fields = [];
    for (const [ref, r] of refs) {
      if (!r.decision || !r.decision.question || !r.els[0] || !r.els[0].isConnected) continue;
      const value = readValue(r);
      if (isEmpty(value)) continue;
      const k = JSON.stringify(value);
      if (sent.get(ref) === k) continue;
      sent.set(ref, k);
      fields.push({
        question: r.decision.question, type: r.ftype || r.kind, options: r.options || [], value,
        filled: r.filled === undefined ? null : r.filled, key: r.decision.key || null,
        source: r.decision.source || null, required: !!r.required,
      });
    }
    if (!fields.length) return;
    // Queued first: if the page unloads before the request goes out, the next
    // ATS page sends it.
    const q = GM_getValue(QUEUE, []);
    q.push({ url: location.href, job_id: current && current.jobId, fields });
    GM_setValue(QUEUE, q.slice(-20));
    flush();
  }

  function onSubmitish(e) {
    if (!active) return;
    if (e.type === 'click') {
      const t = e.target && e.target.closest && e.target.closest('button, input[type=submit], [role=button], a');
      if (!t || t.closest('#' + PANEL_ID)) return;
      const text = clean(t.textContent || t.value || t.getAttribute('aria-label') || '');
      if (!(t.type === 'submit' || SUBMITISH.test(text))) return;
    }
    try { report(); } catch (err) { /* never get in the way of a submit */ }
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

  const esc = (t) => String(t == null ? '' : t).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

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
      ok = await chooseCombo(el, String(v), decision.alts);
      if (!ok) { totals.review++; return mark(decision.ref, 'jab-review', 'choose: ' + v); }
    } else if (r.kind === 'prompt') {
      ok = await choosePrompt(el, [String(v)].concat(decision.alts || []));
      if (!ok) { totals.review++; return mark(decision.ref, 'jab-review', 'search and pick: ' + v); }
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
      fields.forEach((f) => {
        const r = refs.get(f.ref);
        Object.assign(r, { required: f.required, ftype: f.type, options: f.options });
      });
      let res;
      try {
        res = await matchFields({
          url: location.href, job_id: current && current.jobId,
          first: !!(current && !current.opened), fields, prefilled: [...prefilled].filter(Boolean).slice(0, 100),
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
        const r = refs.get(d.ref);
        if (r) {
          r.decision = d;
          // What the autofill left in the field: compared at submit to spot a correction.
          const now = (d.action === 'fill' || d.action === 'review') ? readValue(r) : '';
          r.filled = isEmpty(now) ? null : now;
        }
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

  function offer(text, buttons) {
    // Never fill unasked: offer once a real form is on screen.
    const check = () => {
      if (active || document.getElementById(PANEL_ID)) return true;
      const inputs = [...document.querySelectorAll('input[type=text], input[type=email], input:not([type]), textarea')]
        .filter(visible);
      if (inputs.length >= 3) { say(text, buttons); return true; }
      return false;
    };
    if (check()) return;
    const watch = new MutationObserver(() => { if (check()) watch.disconnect(); });
    watch.observe(document.body, { childList: true, subtree: true });
    setTimeout(() => watch.disconnect(), 120000);
  }

  // ------------------------------------------------------------------------
  // Job boards (LinkedIn, Naukri): never filled. The job opened from Telegram
  // is remembered for the company site their Apply button leads to.
  // ------------------------------------------------------------------------

  const jobName = (j) => [j && j.company, j && j.role].filter(Boolean).join(' — ');

  async function onBoard(tagged) {
    if (!tagged) return;
    GM_setValue(PENDING, { jobId: tagged, at: Date.now() });
    try {
      const j = await opened(tagged);
      const name = jobName(j);
      say('remembered this job' + (name ? ': ' + esc(name) : '') + '.'
        + (j.saved_to ? ' Resume saved to Downloads.' : '')
        + '<br><small>Apply on the company site within 30 minutes and that form is filled for this job.</small>');
    } catch (e) {
      say('remembered this job (endpoint not reachable: ' + esc(e.message) + ').');
    }
  }

  function pending() {
    const p = GM_getValue(PENDING, null);
    return p && Date.now() - p.at < PENDING_MS ? p : null;
  }

  async function fromPending(p) {
    let j = {};
    try { j = await opened(p.jobId); } catch (e) { /* still offered, unnamed */ }
    const start = () => { current = { jobId: p.jobId, at: Date.now(), opened: false }; saveSession(current); run(); };
    // The company's name in this site's address or title: surely the same job.
    const here = ' ' + n(location.hostname.replace(/\./g, ' ') + ' ' + document.title) + ' ';
    const words = n(j.company || '').split(' ').filter((w) => w.length >= 3);
    if (words.length && words.some((w) => here.includes(' ' + w + ' '))) return start();
    const name = jobName(j) || 'the job opened from Telegram';
    offer('a form is open.', [['Autofill for ' + name, start], ['Autofill, no resume', run]]);
  }

  // ------------------------------------------------------------------------
  // Debugging: what the page looks like to the script, saved by the endpoint
  // under data/logs/autofill_debug/ (local, gitignored).
  // ------------------------------------------------------------------------

  async function debugSnapshot() {
    const els = [...document.querySelectorAll('input, select, textarea, button[aria-haspopup], [role="combobox"]')]
      .filter((e) => !e.closest('#' + PANEL_ID) && e.type !== 'hidden').slice(0, 300);
    const fields = els.map((e) => {
      const box = e.closest('[data-automation-id^="formField-"]') || e.parentElement || e;
      return {
        tag: e.tagName, type: e.type || '', label: labelOf(e), group: groupOf(e), visible: visible(e),
        automation_id: e.getAttribute('data-automation-id') || '', handled: handled.has(e),
        html: box.outerHTML.slice(0, 2500),
      };
    });
    const seen = [...refs.entries()].map(([ref, r]) => ({ ref, kind: r.kind, decision: r.decision || null }));
    try {
      await request('POST', ENDPOINT + '/autofill/debug', {
        headers: { 'Content-Type': 'application/json', 'X-JAB': '1' },
        data: JSON.stringify({ url: location.href, title: document.title, fields, refs: seen }),
      });
      say('form sent to Job Bot for debugging.');
    } catch (e) {
      say('could not send the form: ' + esc(e.message));
    }
  }

  // ------------------------------------------------------------------------
  // Start
  // ------------------------------------------------------------------------

  // At document-start, before the page's own router can drop the tag.
  const tagged = takeTag();

  function start() {
    if (BOARD_HOSTS.test(location.hostname)) { onBoard(tagged); return; }
    current = session(tagged);
    document.addEventListener('submit', onSubmitish, true);
    document.addEventListener('click', onSubmitish, true);
    flush();
    GM_registerMenuCommand('Autofill this form', run);
    GM_registerMenuCommand('Send this form to Job Bot (debugging)', debugSnapshot);
    if (current) { run(); return; }
    const p = pending();
    if (p) { fromPending(p); return; }
    offer('a form is open.', [['Autofill', run]]);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => setTimeout(start, 300));
  else start();
})();
