// ==UserScript==
// @name         Job Bot autofill
// @namespace    job-application-bot
// @version      1.7.1
// @description  Fills ATS application forms from the job bot's answers (#154). Never submits.
// @match        *://*/*
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
 *  - It is installed for every site but acts only: on a tagged page (any
 *    site), on a known ATS, or on a page that looks like a job application
 *    (a resume upload, or "apply" and a form). Elsewhere it does nothing
 *    beyond its menu item, and sends nothing unless asked.
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
  // Set by the auto-apply bot (src/autoapply) before it injects this script into
  // LinkedIn's Easy Apply dialog: { jobId, scope }. Only the dialog is read and
  // filled; the bot itself presses Next / Review / Submit.
  const AUTO = window.__jabAutoApply || null;
  // Auto-apply: LinkedIn pre-fills answers the operator gave before. Where an answer
  // is stored, it replaces LinkedIn's; where none is, LinkedIn's stays. The resume
  // is the bot's job (it uploads the tailored PDF through the file picker).
  const OVERWRITE = !!(AUTO && AUTO.overwrite);
  const NO_FILES = !!(AUTO && AUTO.noFiles);
  const scopeRoot = () => (AUTO && AUTO.scope && document.querySelector(AUTO.scope)) || document;
  const SESSION_KEY = 'jab-session';
  const SESSION_MS = 3 * 3600 * 1000;
  const TAG = /([#&])jab=([^&]*)/;
  const PENDING = 'jab-pending';   // the job last opened from Telegram, for the site LinkedIn sends you to
  const PENDING_MS = 30 * 60 * 1000;
  const BOARD_HOSTS = /(^|\.)(linkedin\.com|naukri\.com)$/;
  const ATS_HOSTS = new RegExp('(^|\\.)(' + [
    'greenhouse\\.io', 'lever\\.co', 'ashbyhq\\.com', 'myworkdayjobs\\.com', 'myworkdaysite\\.com',
    'smartrecruiters\\.com', 'workable\\.com', 'recruitee\\.com', 'rippling\\.com', 'bamboohr\\.com',
    'eightfold\\.ai', 'keka\\.com', 'oraclecloud\\.com', 'amazon\\.jobs', 'successfactors\\.(com|eu)',
    'sapsf\\.(com|eu)', 'icims\\.com', 'taleo\\.net', 'jobvite\\.com', 'darwinbox\\.in', 'zohorecruit\\.(com|in)',
    'freshteam\\.com', 'personio\\.(de|com)', 'teamtailor\\.com', 'breezy\\.hr', 'applytojob\\.com',
    'pinpointhq\\.com', 'avature\\.net', 'phenompeople\\.com', 'ultipro\\.com', 'ukg\\.net', 'adp\\.com',
    'paylocity\\.com', 'dayforcehcm\\.com', 'turbohire\\.co', 'hire\\.trakstar\\.com', 'workforcenow\\.adp\\.com',
  ].join('|') + ')$');

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

  // The document or shadow root an element lives in: ids and labels are looked up there.
  const rootOf = (el) => (el && el.getRootNode ? el.getRootNode() : document);

  function byIds(ids, root) {
    const r = root && root.getElementById ? root : document;
    return clean((ids || '').split(/\s+/).map((i) => textOf(r.getElementById(i) || document.getElementById(i))).join(' '));
  }

  // Every element matching ``selector``, inside open web components (shadow DOM) too.
  function deepAll(selector, root) {
    const out = [];
    const visit = (r) => {
      out.push(...r.querySelectorAll(selector));
      for (const host of r.querySelectorAll('*')) if (host.shadowRoot) visit(host.shadowRoot);
    };
    visit(root || scopeRoot());
    return out;
  }

  // The text a person would read as this field's question when the page links
  // none to it: the nearest short text just above it, or just left of it.
  function labelNear(el) {
    const box = el.getBoundingClientRect();
    if (!box.width && !box.height) return '';
    // A label on the same row, to the left, beats text above: in a two-column table
    // the label column is as wide as its longest label, so "Country" can sit far
    // from its box while the row above ("Attach a Cover Letter") is close.
    let left = null;
    let above = null;
    let node = el.parentElement;
    for (let depth = 0; node && depth < 5 && !left; depth++, node = node.parentElement) {
      for (const t of node.querySelectorAll('label, legend, span, p, div, td, th, h1, h2, h3, h4, h5, h6, strong, b')) {
        if (t.contains(el) || t.closest('#' + PANEL_ID)) continue;
        const own = clean([...t.childNodes].filter((c) => c.nodeType === 3).map((c) => c.nodeValue).join(' '));
        if (!own || own.length > 160) continue;
        const r = t.getBoundingClientRect();
        const sameRow = Math.abs((r.top + r.bottom) / 2 - (box.top + box.bottom) / 2) < Math.max(box.height, 24);
        if (sameRow && r.right <= box.left + 4 && box.left - r.right < 800) {
          const d = box.left - r.right;
          if (!left || d < left.d) left = { d, text: own };
        } else if (r.bottom <= box.top + 4 && box.top - r.bottom < 70 && r.left < box.right && r.right > box.left - 20) {
          const d = box.top - r.bottom;
          if (!above || d < above.d) above = { d, text: own };
        }
      }
    }
    return (left || above || { text: '' }).text;
  }

  function labelOf(el) {
    const root = rootOf(el);
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = byIds(lb, root); if (t) return t; }
    if (el.labels && el.labels.length) {
      const t = textOf(el.labels[0]);
      if (t) return t;
    }
    for (const ref of [el.id, el.name]) {
      if (!ref || !root.querySelector) continue;
      const l = root.querySelector(`label[for="${CSS.escape(ref)}"]`);
      if (l && textOf(l)) return textOf(l);
    }
    const aria = el.getAttribute('aria-label');
    if (aria) return clean(aria);
    // Not a tooltip this script wrote itself.
    const title = /^Job Bot/.test(el.getAttribute('title') || '') ? '' : el.getAttribute('title');
    const hint = clean(el.getAttribute('placeholder') || title || '');
    // "No Selection" / "- Select -" say nothing: the label beside the box does.
    if (hint && !PLACEHOLDER.test(hint)) return hint;
    return labelNear(el) || hint;
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
      if (lb && byIds(lb, rootOf(node))) return byIds(lb, rootOf(node)).slice(0, 300);
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

  // The question written just before a field's block ("Please indicate your date of
  // birth" above boxes labelled only Month / Day / Year), skipping sibling fields.
  function textBefore(el, own) {
    let node = el;
    for (let depth = 0; node && depth < 4; depth++, node = node.parentElement) {
      let sib = node.previousElementSibling;
      for (let i = 0; sib && i < 4; i++, sib = sib.previousElementSibling) {
        if (sib.querySelector && sib.querySelector('input, select, textarea, [role="combobox"]')) continue;
        const t = textOf(sib);
        if (t === own || (sib.tagName === 'LABEL' && sib.htmlFor === el.id)) continue;  // its own label
        if (t.length >= 2 && t.length <= 300) return t;
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
  const PLACEHOLDER = /^\s*(?:(?:select( one| an option)?|choose( one| an option)?|please (select|choose)|none selected|no selection|search)\b|-+\s*(select|choose)?\s*-*\s*$|--)/i;

  // What a button-like widget shows, minus its own label ("Country Select an option" -> "Select an option").
  function shownText(el) {
    let t = clean(el.textContent);
    const lb = el.getAttribute('aria-labelledby') ? byIds(el.getAttribute('aria-labelledby'), rootOf(el)) : '';
    if (lb && t.startsWith(lb)) t = clean(t.slice(lb.length));
    return t;
  }

  function hasValue(el) {
    if (el.tagName === 'BUTTON' || !NATIVE.test(el.tagName)) { const t = shownText(el); return !!t && !PLACEHOLDER.test(t); }
    const box = promptBox(el);
    if (box) return !!box.querySelector('[data-automation-id="selectedItem"], [data-automation-id="selectedItemList"] li');
    if (el.type === 'file') return el.files && el.files.length > 0;
    if (el.tagName === 'SELECT') {
      const o = el.options[el.selectedIndex];
      return !!(o && o.value && !PLACEHOLDER.test(o.text));
    }
    return !!(el.value && el.value.trim());
  }

  // Everything that can hold an answer, whatever the design: native fields,
  // standard widget roles, rich-text editors.
  const CONTROLS = 'input, select, textarea, [role="combobox"], button[aria-haspopup="listbox"], '
    + '[contenteditable="true"], [contenteditable=""], [role="textbox"], [role="radiogroup"], '
    + '[role="checkbox"], [role="switch"], [aria-pressed]';
  const NATIVE = /^(INPUT|SELECT|TEXTAREA|BUTTON)$/;
  const SKIP_TYPES = ['hidden', 'submit', 'button', 'image', 'reset', 'password'];
  const OUTSIDE_FORM = 'header, nav, footer, [role="navigation"], [role="banner"], [role="contentinfo"]';

  // A read-only box that opens a list or calendar when clicked (Ant Design, date pickers).
  function looksPicker(el) {
    return !!(el.getAttribute('aria-haspopup') || el.getAttribute('role') === 'combobox' || el.getAttribute('aria-controls')
      || /date|picker|calendar|select|dropdown|combo/i.test((el.className || '') + ' ' + (el.id || '') + ' ' + (el.name || ''))
      || (el.parentElement && el.parentElement.querySelector('button, [role="button"]')));
  }

  // A native <select> hidden behind a styled widget (Select2, Chosen, Choices, bootstrap-select):
  // the widget follows the select's value, so the select is filled and the widget outlined.
  const WIDGET = /select2|chosen|selectize|choices|bootstrap-select|nice-select|ms-parent|custom-select|dropdown/i;
  function backingWidget(sel) {
    for (const c of [sel.nextElementSibling, sel.previousElementSibling, sel.parentElement]) {
      if (c && c !== sel && visible(c) && WIDGET.test((c.className && String(c.className)) || '')) return c;
    }
    return null;
  }

  // A hidden file input inside a visible area that talks about uploading a resume.
  function uploadArea(el) {
    let node = el.parentElement;
    for (let d = 0; node && d < 4; d++, node = node.parentElement) {
      if (visible(node) && /resume|cv|curriculum|upload|attach|drop|browse/i.test(textOf(node).slice(0, 300))) return node;
    }
    return null;
  }

  const ariaChecked = (el) => el.getAttribute('aria-checked') === 'true' || el.getAttribute('aria-pressed') === 'true';
  const richText = (el) => !NATIVE.test(el.tagName) && (el.isContentEditable || el.getAttribute('role') === 'textbox');

  let siteExtra = [];  // selectors of controls learned on this site (fields you filled that the scan missed)
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
  // A heading without a number ("Education", "Work experience"): its blocks are
  // numbered in the order they appear (an "Add another" repeats the block).
  const SECTION_BARE = /^(work experience|professional experience|experience|employment( history)?|work history|education|academic( details| qualifications?)?|qualifications?)\b/i;
  function groupOf(el) {
    let node = el.parentElement;
    for (let depth = 0; node && depth < 14; depth++, node = node.parentElement) {
      const lb = node.getAttribute && node.getAttribute('aria-labelledby');
      const t = (lb && byIds(lb)) || '';
      if (SECTION.test(t)) return t.match(SECTION)[0];
      const h = node.querySelector && node.querySelector(':scope > h2, :scope > h3, :scope > h4, :scope > h5, :scope > div > h3, :scope > div > h4, :scope > [role="heading"], :scope > legend');
      const ht = h ? clean(h.textContent) : '';
      if (SECTION.test(ht)) return ht.match(SECTION)[0];
      if (h && SECTION_BARE.test(ht) && ht.length < 60) {
        const kind = /educat|academ|qualif/i.test(ht) ? 'Education' : 'Work Experience';
        // The field's own block under the heading. Entries (blocks with several
        // fields; "Add another" repeats them) are numbered in order; a lone
        // question above them ("Do you have past experience?") belongs to entry 1.
        let block = el;
        while (block.parentElement && block.parentElement !== node) block = block.parentElement;
        const isEntry = (b) => b.querySelectorAll
          && b.querySelectorAll('input:not([type=hidden]), select, textarea, [role="combobox"]').length >= 2;
        let index = 1;
        if (isEntry(block)) for (let sib = block.previousElementSibling; sib; sib = sib.previousElementSibling) if (isEntry(sib)) index++;
        return kind + ' ' + index;
      }
    }
    return entryOf(el);
  }

  // Whatever the headings look like: a date (or title, location...) in the same
  // small block as "University" / "Degree" is an education entry; next to
  // "Company" / "Position", a work entry. Repeated blocks ("Add another") are
  // numbered by their order among blocks of the same kind.
  const EDU_FIELDS = /\b(university|school|college|institute|institution|degree|qualification|field of study|major|gpa|cgpa|grade)\b/i;
  const WORK_FIELDS = /\b(company|employer|organi[sz]ation|position|job title|designation|role title|current position)\b/i;
  const CONTROL_SEL = 'input:not([type=hidden]):not([type=button]):not([type=submit]), select, textarea, [role="combobox"]';
  function blockKind(node) {
    const controls = [...node.querySelectorAll(CONTROL_SEL)];
    if (controls.length < 2 || controls.length > 14) return '';
    const labels = controls.map((c) => labelOf(c)).join(' | ');
    // A block holding the applicant's own details is the main form, not an entry.
    if (/\b(e-?mail|phone|mobile|first name|last name|full name|surname)\b/i.test(labels)) return '';
    const edu = EDU_FIELDS.test(labels);
    const work = WORK_FIELDS.test(labels);
    return edu && !work ? 'Education' : work && !edu ? 'Work Experience' : '';
  }
  function entryOf(el) {
    // "Earliest start date", "When can you join": availability, never an entry's date.
    if (/earliest|availab|join|notice|can you start|could you start/i.test(labelOf(el))) return '';
    let node = el.parentElement;
    for (let depth = 0; node && depth < 7; depth++, node = node.parentElement) {
      const kind = blockKind(node);
      if (!kind) continue;
      // Siblings of the same kind before it: earlier entries.
      let index = 1;
      for (let sib = node.previousElementSibling; sib; sib = sib.previousElementSibling) if (blockKind(sib) === kind) index++;
      return kind + ' ' + index;
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
    + '[data-automation-widget="wd-popup"], [data-automation-id="promptOption"], .select2-dropdown, .select2-search, '
    + '.chosen-drop, .choices__list--dropdown, [class*="dropdown-menu" i], [class*="menu-list" i]');

  function optionLabel(el) {
    return clean(labelOf(el) || (el.parentElement && textOf(el.parentElement)) || el.value);
  }

  function scan() {
    const fields = [];
    const groups = new Map();
    const nodes = deepAll(CONTROLS + (siteExtra.length ? ', ' + siteExtra.join(', ') : ''));
    for (const el of nodes) {
      if (handled.has(el) || el.closest('#' + PANEL_ID) || inPopup(el)) continue;
      if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
      if (el.readOnly && !looksPicker(el)) continue;
      const type = (el.getAttribute('type') || el.type || '').toLowerCase();
      // A listbox button (Workday's dropdowns) is a question, though its type is "button".
      if (NATIVE.test(el.tagName) && el.tagName !== 'BUTTON' && SKIP_TYPES.includes(type)) continue;
      if (/captcha/i.test((el.name || '') + (el.id || '') + (el.className || ''))) continue;
      const isFile = type === 'file';
      if (isFile && NO_FILES) continue;
      const isTick = el.tagName === 'INPUT' && (type === 'radio' || type === 'checkbox');
      const widget = el.tagName === 'SELECT' && !visible(el) ? backingWidget(el) : null;
      if (widget && (widget.matches('[role="combobox"]') || widget.querySelector('[role="combobox"]'))) { handled.add(el); continue; }
      // Styled file inputs and ticks hide the real element: judge by its label.
      const shown = visible(el) || widget || ((isFile || isTick) && (
        (el.labels && [...el.labels].some(visible)) || visible(el.parentElement)))
        || (isFile && uploadArea(el));
      if (!shown) continue;

      // Custom radio group: <div role="radiogroup"> of <div role="radio">.
      if (el.getAttribute('role') === 'radiogroup') {
        const radios = [...el.querySelectorAll('[role="radio"]')].filter((x) => !NATIVE.test(x.tagName));
        if (!radios.length) continue;  // native radios inside: read as a group below
        if (radios.some(ariaChecked)) { radios.concat(el).forEach((x) => handled.add(x)); continue; }
        const question = labelOf(el) || contextOf(el, radios.map((x) => clean(x.textContent)));
        fields.push({
          ref: remember('ariaradio', [el], { radios }), label: question, context: question, name: '', id: el.id || '',
          automation_id: '', autocomplete: '', type: 'radio', options: radios.map((x) => clean(x.textContent)),
          group: groupOf(el), required: requiredOf(el, question) || isRequiredBox(el),
        });
        radios.forEach((x) => handled.add(x));
        continue;
      }
      // A row of toggle buttons (aria-pressed) not tied to a hidden input: Yes/No, or chips.
      if (el.hasAttribute('aria-pressed') && !el.getAttribute('role') && el.parentElement) {
        const row = [...el.parentElement.children].filter((c) => c.hasAttribute('aria-pressed'));
        if (row.length < 2 || row.some((c) => handled.has(c))) { handled.add(el); continue; }
        row.forEach((c) => handled.add(c));
        if (row.some(ariaChecked)) continue;
        const question = labelOf(el.parentElement) || headingBefore(el.parentElement) || labelNear(el.parentElement)
          || contextOf(el.parentElement, row.map((c) => clean(c.textContent)));
        const yesNo = row.length === 2 && row.every((c) => /^(yes|no)$/i.test(clean(c.textContent)));
        fields.push({
          ref: remember(yesNo ? 'ariaradio' : 'ariachecks', [el.parentElement], yesNo ? { radios: row } : { chips: row }),
          label: question, context: question, name: '', id: '', automation_id: '', autocomplete: '',
          type: yesNo ? 'radio' : 'checkbox_group', options: row.map((c) => clean(c.textContent)),
          group: groupOf(el), required: /[*✱]/.test(question),
        });
        continue;
      }
      // A row of chips you tick several of ("Onsite / Hybrid / Remote"): one question.
      if (!NATIVE.test(el.tagName) && el.getAttribute('role') === 'checkbox' && el.parentElement) {
        const chips = [...el.parentElement.children].filter((c) => c.getAttribute('role') === 'checkbox');
        if (chips.length >= 2) {
          chips.forEach((c) => handled.add(c));
          if (chips.some(ariaChecked)) continue;
          const question = labelOf(el.parentElement) || headingBefore(el.parentElement) || labelNear(el.parentElement)
            || contextOf(el.parentElement, chips.map((c) => clean(c.textContent)));
          fields.push({
            ref: remember('ariachecks', [el.parentElement], { chips }), label: question, context: question, name: '',
            id: '', automation_id: '', autocomplete: '', type: 'checkbox_group', options: chips.map((c) => clean(c.textContent)),
            group: groupOf(el), required: /[*✱]/.test(question),
          });
          continue;
        }
      }
      // Custom checkbox / switch: <div role="checkbox" aria-checked>.
      if (!NATIVE.test(el.tagName) && /^(checkbox|switch)$/.test(el.getAttribute('role') || '')) {
        if (ariaChecked(el)) { handled.add(el); continue; }
        const own = labelOf(el) || clean(el.textContent) || contextOf(el, []);
        fields.push({
          ref: remember('ariacheck', [el]), label: own, context: contextOf(el, [own]), name: '', id: el.id || '',
          automation_id: '', autocomplete: '', type: 'checkbox', options: [], group: groupOf(el),
          required: requiredOf(el, own),
        });
        continue;
      }
      // Rich-text editor (contenteditable): the outermost one only.
      if (richText(el)) {
        if (el.parentElement && el.parentElement.closest('[contenteditable="true"], [contenteditable=""]')) continue;
        if (clean(el.textContent)) { handled.add(el); continue; }
        const label = labelOf(el) || contextOf(el, []);
        fields.push({
          ref: remember('richtext', [el]), label, context: contextOf(el, [label]), name: '', id: el.id || '',
          automation_id: '', autocomplete: '', type: 'textarea', options: [], group: groupOf(el),
          required: requiredOf(el, label) || isRequiredBox(el),
        });
        continue;
      }
      if (!NATIVE.test(el.tagName) && el.getAttribute('role') !== 'combobox' && !siteExtra.length) continue;

      const toggles = isTick && el.parentElement
        ? [...el.parentElement.querySelectorAll(':scope > button[aria-pressed], :scope > button[data-option]')] : [];
      if (toggles.length >= 2) {
        // Read once: through this hidden input, or already as a row of toggle buttons.
        if (toggles.some((b) => handled.has(b))) { handled.add(el); continue; }
        toggles.forEach((b) => handled.add(b));
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
      const had = hasValue(el) && !isFile;
      if (had && !OVERWRITE) { handled.add(el); prefilled.add(labelOf(el)); continue; }
      if (isFile && hasValue(el)) { handled.add(el); continue; }

      let label = labelOf(el);
      const dp = datePart(el);
      if (dp) label = clean(dp.of + ' ' + dp.part);
      const combo = el.getAttribute('role') === 'combobox' || el.getAttribute('aria-haspopup') === 'listbox'
        || el.getAttribute('aria-autocomplete') === 'list';
      let ftype = el.tagName === 'SELECT' ? (el.multiple ? 'multiselect' : 'select')
        : el.tagName === 'TEXTAREA' ? 'textarea' : combo ? 'combobox' : (type || 'text');
      if (el.tagName === 'BUTTON' || (el.readOnly && el.tagName === 'INPUT')) ftype = 'combobox';
      if (!NATIVE.test(el.tagName)) ftype = 'combobox';  // a learned custom control (site recipe)
      if (promptBox(el)) ftype = 'prompt';
      const options = el.tagName === 'SELECT' ? [...el.options].map((o) => clean(o.text)) : [];
      let context = contextOf(el, [label]);
      if (isFile) context = clean(headingBefore(el) + ' ' + context);
      // A label of one or two words ("Month", "Day") leans on the question written before it.
      if (label.split(' ').length <= 2 && (!context || context === label)) context = textBefore(el, label) || context;
      const fieldBox = el.closest('[data-automation-id^="formField-"]');
      const f = {
        ref: remember(ftype, [el], Object.assign(widget ? { markEl: widget } : {}, had ? { had: true } : {})), label, context,
        name: el.name || '', id: el.id || '', automation_id: el.getAttribute('data-automation-id') || '',
        autocomplete: el.getAttribute('autocomplete') || '', type: ftype, options,
        placeholder: el.getAttribute('placeholder') || '', group: groupOf(el),
        field_id: (fieldBox && fieldBox.getAttribute('data-automation-id')) || '',
        // A drop zone's own label says nothing; the "*" sits on the heading above it.
        required: requiredOf(el, label) || (isFile && /[*✱]/.test(context)) || !!(fieldBox && isRequiredBox(el)),
      };
      fields.push(f);
    }

    for (const { el, box, about } of (NO_FILES ? [] : uploadButtons())) {
      fields.push({
        ref: remember('uploadbutton', [el], { markEl: box, box }), label: about, context: about, name: '',
        id: el.id || '', automation_id: '', autocomplete: '', type: 'file', options: [], group: '',
        required: /[*✱]|required/i.test(about),
      });
    }

    for (const zone of (NO_FILES ? [] : dropZones())) {
      const label = headingBefore(zone) || labelNear(zone) || contextOf(zone, []) || textOf(zone);
      const context = clean(headingBefore(zone) + ' ' + textOf(zone)).slice(0, 300);
      fields.push({
        ref: remember('dropzone', [zone]), label: 'Drop files here', context, name: '', id: zone.id || '',
        automation_id: '', autocomplete: '', type: 'file', options: [], group: '',
        required: /[*✱]/.test(label + ' ' + context),
      });
    }

    for (const g of groups.values()) {
      // A list of uploaded documents to pick from (LinkedIn's resume cards): no question.
      if (g.type === 'radio' && g.els.map(optionLabel).every((o) => /\.(pdf|docx?|rtf|odt|txt)\b/i.test(o))) {
        g.els.forEach((e) => handled.add(e));
        continue;
      }
      const groupHad = g.els.some((e) => e.checked);
      if (groupHad && !OVERWRITE) { g.els.forEach((e) => handled.add(e)); continue; }
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
        ref: remember(ftype, g.els, groupHad ? { had: true } : null), label: question, context: question, name: g.els[0].name || '',
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

  // The slow way, for fields that ignore a value set at once: one key at a time.
  function typeKeys(el, value) {
    el.focus();
    setNative(el, '');
    fire(el, 'input');
    for (const ch of String(value)) {
      el.dispatchEvent(new KeyboardEvent('keydown', { key: ch, bubbles: true }));
      el.dispatchEvent(new KeyboardEvent('keypress', { key: ch, bubbles: true }));
      setNative(el, el.value + ch);
      el.dispatchEvent(new InputEvent('input', { bubbles: true, data: ch, inputType: 'insertText' }));
      el.dispatchEvent(new KeyboardEvent('keyup', { key: ch, bubbles: true }));
    }
    fire(el, 'change');
    el.blur();
    fire(el, 'blur');
  }

  function setRich(el, value) {
    el.focus();
    const sel = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(el);
    sel.removeAllRanges();
    sel.addRange(range);
    // insertText goes through the editor's own input handling (Quill, ProseMirror, Draft).
    if (!document.execCommand('insertText', false, value) || !clean(el.textContent)) {
      el.textContent = value;
      el.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
    }
    el.blur();
  }

  // Did the field keep the answer? Compared as a person would read it.
  const stuck = (r, v) => {
    const now = readValue(r);
    const have = Array.isArray(now) ? now.join(' ') : now;
    if (!have) return false;
    const digits = (x) => String(x).replace(/\D/g, '');
    // A box that formats what is typed ("+91 90000 00000"): the digits are what count.
    if (digits(v).length >= 6 && digits(have).endsWith(digits(v))) return true;
    return n(have) === n(v) || n(have).includes(n(v)) || n(v).includes(n(have));
  };

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
      el.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true }));
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

  // The options of this combobox's own list when it names one, else any visible.
  function comboOptions(el) {
    const id = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
    const own = id && document.getElementById(id);
    const scope = own && visible(own) ? own : document;
    return [...scope.querySelectorAll('[role="option"], [role="treeitem"], .select2-results__option, .chosen-results li, .choices__item--choice')]
      .filter((o) => visible(o) && !/loading|searching|no results/i.test(o.textContent));
  }

  // Lists that load from the server show "Loading…" first: wait for them (up to ~8 s).
  const LOADING = /loading|searching|please wait|fetching/i;
  function listLoading(el) {
    const id = el.getAttribute && (el.getAttribute('aria-controls') || el.getAttribute('aria-owns'));
    const own = id && document.getElementById(id);
    if (own && own.getAttribute('aria-busy') === 'true') return true;
    return deepAll('[role="listbox"], [role="tree"], .select2-results, .select2-results__option, [class*="loading" i], [aria-busy="true"]')
      .some((x) => visible(x) && !x.closest('#' + PANEL_ID) && (x.getAttribute('aria-busy') === 'true'
        || (LOADING.test(x.textContent || '') && (x.textContent || '').length < 60) || /loading|spinner/i.test(String(x.className))));
  }

  // The longest any one field may take, however its list loads.
  const FIELD_BUDGET_MS = 8000;
  const POLL_MS = 100;

  // Wait for one of ``wants`` to appear: picked the moment it shows. Gives up after
  // ``ms`` unless the list says it is still loading, and never past ``deadline``.
  async function waitOption(el, wants, ms, deadline) {
    const start = Date.now();
    while (Date.now() < deadline) {
      const hit = bestOption(comboOptions(el), wants);
      if (hit) return hit;
      if (Date.now() - start >= ms && !listLoading(el)) return null;
      await sleep(POLL_MS);
    }
    return bestOption(comboOptions(el), wants);
  }

  async function chooseCombo(el, text, alts) {
    const wants = [text].concat(alts || []);
    const deadline = Date.now() + FIELD_BUDGET_MS;
    const input = el.tagName === 'INPUT' ? el : el.querySelector('input');
    const side = input && input.parentElement && input.parentElement.querySelector('button');
    // 1. Open it: a click shows the whole list on most pick-lists (SuccessFactors, Workday).
    pressOn(input || el);
    let hit = await waitOption(input || el, wants, 800, deadline);
    // 2a. A search box the widget opened (Select2, Chosen): search there.
    if (!hit) {
      const search = [document.activeElement, ...deepAll('.select2-search__field, .chosen-search input, .choices__input--cloned, input[type="search"]')]
        .find((x) => x && x !== input && x.tagName === 'INPUT' && visible(x) && !x.closest('#' + PANEL_ID));
      if (search) {
        search.focus();
        setNative(search, text);
        search.dispatchEvent(new InputEvent('input', { bubbles: true, data: text, inputType: 'insertText' }));
        search.dispatchEvent(new KeyboardEvent('keyup', { key: text.slice(-1), bubbles: true }));
        hit = await waitOption(input || el, wants, 2500, deadline);  // results may come from the server
      }
    }
    // 2. Type to filter (long lists, react-select).
    if (!hit && input) {
      input.focus();
      setNative(input, text);
      fire(input, 'input');
      input.dispatchEvent(new KeyboardEvent('keyup', { key: text.slice(-1), bubbles: true }));
      hit = await waitOption(input, wants, 1500, deadline);
    }
    // 3. The arrow button beside it.
    if (!hit && side && Date.now() < deadline) {
      if (input) { setNative(input, ''); fire(input, 'input'); }
      pressOn(side);
      hit = await waitOption(input || el, wants, 1000, deadline);
    }
    if (hit) {
      const inner = hit.querySelector('a, [role="menuitem"]');
      pressOn(inner || hit);
      await sleep(200);
      return true;
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
    const deadline = Date.now() + FIELD_BUDGET_MS;
    const box = promptBox(el);
    const chosen = () => !!(box && box.querySelector('[data-automation-id="selectedItem"], [data-automation-id="selectedItemList"] li'));
    for (const want of wants) {
      if (Date.now() >= deadline) break;
      el.focus();
      setNative(el, String(want));
      fire(el, 'input');
      for (const t of ['keydown', 'keypress', 'keyup']) {
        el.dispatchEvent(new KeyboardEvent(t, { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
      }
      for (let level = 0; level < 3 && !chosen(); level++) {
        let hit = null;
        const until = Math.min(deadline, Date.now() + 3000);
        while (!hit && Date.now() < until) {
          const opts = [...document.querySelectorAll(PROMPT_OPTS)].filter((o) => visible(o) && !o.closest('#' + PANEL_ID));
          hit = bestOption(opts, [want]);
          if (!hit) await sleep(POLL_MS);
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

  const pdfFile = (blob, filename) => new File([blob], filename, { type: 'application/pdf' });

  function attach(el, blob, filename) {
    const dt = new DataTransfer();
    dt.items.add(pdfFile(blob, filename));
    el.files = dt.files;
    fire(el, 'input');
    fire(el, 'change');
    return el.files.length === 1;
  }

  // An upload area you drag a file onto (react-dropzone, Uppy, Filestack, Dropzone.js):
  // the same events a real drag-and-drop sends, carrying the PDF.
  function dropOn(zone, blob, filename) {
    const dt = new DataTransfer();
    dt.items.add(pdfFile(blob, filename));
    for (const type of ['dragenter', 'dragover', 'drop']) {
      zone.dispatchEvent(new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: dt }));
    }
  }

  // Did the page take the file? Its name, "uploaded", or a progress bar appears near it.
  async function uploadShows(area, filename, before, pageBefore) {
    const stem = n(filename.replace(/\.pdf$/i, ''));
    const pageHad = (pageBefore || '').includes(stem);
    for (let i = 0; i < 10; i++) {
      await sleep(300);
      const now = n(textOf(area));
      if (now !== before && (now.includes(stem) || /\b(uploaded|uploading|attached|remove|replace|\d+ ?%)\b/.test(now))) return true;
      if (!pageHad && n(document.body.innerText).includes(stem)) return true;
    }
    return false;
  }

  // An "Upload a CV" / "+" button whose file input appears only in the dialog it
  // opens (SuccessFactors, iCIMS, Taleo attachment fields).
  const RESUME_WORDS = /\b(resume|résumé|cv|curriculum vitae)\b/i;
  function uploadButtons() {
    const out = [];
    const seenBoxes = new Set();
    for (const el of deepAll('button, [role="button"], a')) {
      if (!visible(el) || handled.has(el) || el.closest('#' + PANEL_ID) || el.closest(OUTSIDE_FORM)) continue;
      const own = clean(labelOf(el) + ' ' + textOf(el) + ' ' + (el.className || ''));
      if (!/upload|attach|browse|choose file|select file|from (device|computer)|my (device|computer)|plus|addattach/i.test(own)) continue;
      // Never: skip the resume, paste text, cloud pickers, or a section's own Add/Remove.
      if (/without|later|skip|paste|linkedin|dropbox|google|drive|onedrive|indeed|add another|remove|delete|cancel/i.test(own)) continue;
      // The field around it must be about the resume, not a cover letter or photo.
      let box = el.parentElement;
      for (let d = 0; box && d < 4 && !RESUME_WORDS.test(textOf(box).slice(0, 200)); d++) box = box.parentElement;
      if (!box || seenBoxes.has(box)) continue;
      const about = textOf(box).slice(0, 200);
      if (/cover letter|photo|picture|transcript|certificate/i.test(about) && !RESUME_WORDS.test(own)) continue;
      if (textOf(box).length > 600) continue;  // a whole section, not one field
      if ([...box.querySelectorAll('input[type="file"]')].some((f) => handled.has(f))) continue;
      seenBoxes.add(box);
      out.push({ el, box, about });
    }
    return out;
  }

  // A visible area that asks for a file to be dropped, with no file input of its own.
  const DROP_TEXT = /drag (and|&|n) drop|drop (your |the |a )?(file|files|resume|cv|document)s?( here)?|drop here|browse files|choose file|select file/i;
  function dropZones() {
    const zones = [];
    for (const el of deepAll('div, section, label, form, [role="button"]')) {
      if (!visible(el) || el.closest('#' + PANEL_ID) || handled.has(el)) continue;
      const own = textOf(el);
      if (own.length > 400 || !DROP_TEXT.test(own)) continue;
      // The smallest such area: skip one whose child is also a zone.
      if ([...el.children].some((c) => visible(c) && DROP_TEXT.test(textOf(c)) && textOf(c).length <= 400)) continue;
      // A caption beside a file input the scan already took ("Choose file"): not a second upload.
      const near = (el.parentElement && el.parentElement.parentElement) || el;
      if ([...near.querySelectorAll('input[type="file"]')].some((f) => handled.has(f))) continue;
      zones.push(el);
    }
    return zones;
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
      case 'ariacheck': return ariaChecked(el) ? 'Yes' : '';
      case 'ariaradio': { const c = r.radios.find(ariaChecked); return c ? clean(c.textContent) : ''; }
      case 'ariachecks': return r.chips.filter(ariaChecked).map((c) => clean(c.textContent));
      case 'richtext': return clean(el.textContent);
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
        if (el.tagName === 'BUTTON' || !NATIVE.test(el.tagName)) { const t = shownText(el); return PLACEHOLDER.test(t) ? '' : t; }
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
  // Nothing missed silently
  // ------------------------------------------------------------------------

  // How to find this element again on this site: id, name, or tag + role + stable classes.
  function fingerprint(el) {
    const tag = el.tagName.toLowerCase();
    if (el.id && !/\d{3,}|[:.]/.test(el.id)) return `${tag}#${CSS.escape(el.id)}`;
    if (el.getAttribute('name')) return `${tag}[name="${CSS.escape(el.getAttribute('name'))}"]`;
    const auto = el.getAttribute('data-automation-id') || el.getAttribute('data-testid') || el.getAttribute('data-qa');
    if (auto) return `${tag}[${el.hasAttribute('data-automation-id') ? 'data-automation-id' : el.hasAttribute('data-testid') ? 'data-testid' : 'data-qa'}="${CSS.escape(auto)}"]`;
    const role = el.getAttribute('role');
    const classes = [...el.classList].filter((c) => !/\d{2,}|^(jab-|is-|has-|active|focus|hover|open|selected)/i.test(c)).slice(0, 2);
    return tag + (role ? `[role="${role}"]` : '') + classes.map((c) => '.' + CSS.escape(c)).join('');
  }

  // Looks like a place to answer something, yet no pass took it: outline it grey.
  const FILLABLE = CONTROLS + ', [aria-haspopup="listbox"], [aria-haspopup="true"], [tabindex="0"][class*="select" i], '
    + '[class*="dropdown" i][tabindex], [class*="picker" i] input';
  const unseen = new Set();
  const sentMissed = new WeakMap();

  function coverage() {
    let added = 0;
    for (const el of deepAll(FILLABLE)) {
      if (handled.has(el) || unseen.has(el) || el.closest('#' + PANEL_ID) || inPopup(el) || el.closest(OUTSIDE_FORM)) continue;
      if (!visible(el) || el.disabled || (el.readOnly && !looksPicker(el))) continue;
      const type = (el.getAttribute('type') || '').toLowerCase();
      if (NATIVE.test(el.tagName) && SKIP_TYPES.includes(type)) continue;
      if (/captcha|search/i.test((el.name || '') + (el.id || '') + (el.className || '') + type)) continue;
      if (el.closest('[role="radiogroup"], fieldset') && handled.has(el.closest('[role="radiogroup"], fieldset'))) continue;
      if (hasValue(el)) continue;
      unseen.add(el);
      el.classList.add('jab-unseen');
      el.title = "Job Bot didn't recognise this field";
      added++;
    }
    totals.unseen = unseen.size;
    if (added) debugSnapshot(true);  // so the next fix starts from this very form
  }

  // After Next / Submit, the site's own "This field is required": whatever it
  // points at is a field this script missed or could not fill.
  const ERROR_TEXT = /required|must (have|be|contain|select)|please (enter|select|fill|provide|choose|upload|complete)|can'?t be (blank|empty)|cannot be (blank|empty)|is (invalid|missing|mandatory)|enter a valid|mandatory/i;

  function siteErrors() {
    let found = 0;
    const errs = deepAll('[role="alert"], [aria-live="assertive"], [aria-live="polite"], [class*="err" i], '
      + '[class*="invalid" i], [class*="warning" i], [id*="err" i], [class*="help" i]')
      .filter((e) => visible(e) && !e.closest('#' + PANEL_ID) && ERROR_TEXT.test(clean(e.textContent)) && clean(e.textContent).length < 220);
    const marked = new Set();
    for (const e of errs) {
      const msg = clean(e.textContent).slice(0, 120);
      let control = e.id ? deepAll(`[aria-describedby~="${CSS.escape(e.id)}"], [aria-errormessage="${CSS.escape(e.id)}"]`)[0] : null;
      for (let node = e.parentElement, d = 0; !control && node && d < 4; d++, node = node.parentElement) {
        control = [...node.querySelectorAll(FILLABLE)].find((c) => visible(c) && !c.closest('#' + PANEL_ID));
      }
      if (!control || marked.has(control)) continue;
      marked.add(control);
      const ref = [...refs.entries()].find(([, r]) => r.els.includes(control) || (r.radios && r.radios.includes(control)));
      if (ref) { mark(ref[0], 'jab-missing', 'the site says: ' + msg); found++; continue; }
      if (!unseen.has(control)) { unseen.add(control); control.classList.add('jab-unseen'); found++; }
      control.title = 'Job Bot missed this; the site says: ' + msg;
    }
    for (const c of deepAll('[aria-invalid="true"]')) {
      if (!visible(c) || marked.has(c) || handled.has(c) || unseen.has(c)) continue;
      unseen.add(c); c.classList.add('jab-unseen'); c.title = 'Job Bot missed this; the site marks it invalid'; found++;
    }
    totals.unseen = unseen.size;
    if (found) { say(summary()); debugSnapshot(true); }
  }

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
    const missed = [];
    for (const el of unseen) {
      if (!el.isConnected) continue;
      const value = NATIVE.test(el.tagName) ? clean(el.value) : clean(el.textContent);
      if (!value || PLACEHOLDER.test(value)) continue;
      if (sentMissed.get(el) === value) continue;  // one Submit fires both click and submit
      sentMissed.set(el, value);
      const question = labelOf(el) || contextOf(el, []);
      fields.push({ question, type: 'text', options: [], value, filled: null, key: null, source: null, required: false });
      missed.push({ selector: fingerprint(el), question });
    }
    if (!fields.length) return;
    // Queued first: if the page unloads before the request goes out, the next
    // ATS page sends it.
    const q = GM_getValue(QUEUE, []);
    q.push({ url: location.href, job_id: current && current.jobId, fields, missed });
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
    // Give the site a moment to show its own "required" messages.
    setTimeout(() => { try { siteErrors(); } catch (err) { /* advisory only */ } }, 1500);
  }

  // ------------------------------------------------------------------------
  // Showing what happened
  // ------------------------------------------------------------------------

  const STYLE = `
    .jab-review  { outline: 2px solid #e0a000 !important; outline-offset: 2px; }
    .jab-missing { outline: 2px solid #d93025 !important; outline-offset: 2px; }
    .jab-optional{ outline: 2px dashed #5b8def !important; outline-offset: 2px; }
    .jab-unseen  { outline: 2px dashed #9aa0a6 !important; outline-offset: 2px; }
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
    r.outcome = cls.replace('jab-', '') + (why ? ': ' + why : '');
    let target = r.markEl || (r.kind === 'buttons' ? r.buttons[0].parentElement : r.els[0]);
    if (!r.markEl && !/^(buttons|ariaradio|ariacheck|ariachecks|richtext|dropzone|uploadbutton)$/.test(r.kind) && (r.els.length > 1 || r.kind === 'file' || target.type === 'checkbox' || target.type === 'radio')) {
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

  const totals = { filled: 0, review: 0, missing: 0, optional: 0, unseen: 0, resume: '' };
  let busy = false;
  let active = false;
  let current = null;

  function summary() {
    const bits = [];
    if (totals.filled) bits.push(`${totals.filled} filled`);
    if (totals.review) bits.push(`<span class="k" style="background:#e0a000"></span>${totals.review} to check`);
    if (totals.missing) bits.push(`<span class="k" style="background:#d93025"></span>${totals.missing} required, no answer`);
    if (totals.optional) bits.push(`<span class="k" style="border:1px dashed #5b8def"></span>${totals.optional} optional, no answer`);
    if (totals.unseen) bits.push(`<span class="k" style="border:1px dashed #9aa0a6"></span>${totals.unseen} not recognised (sent to Job Bot)`);
    if (totals.resume) bits.push(totals.resume);
    return (bits.join(' · ') || 'nothing to fill') + '<br><small>Review the form and submit it yourself.</small>';
  }

  async function apply(decision, resume) {
    const r = refs.get(decision.ref);
    if (!r) return;
    const el = r.els[0];
    const a = decision.action;
    if (a === 'skip') return;
    if (a === 'resume' && NO_FILES) return;  // auto-apply: the bot uploads the tailored PDF itself
    if (a === 'resume') {
      if (!resume) {
        totals.resume = 'resume: attach it yourself (no match tag)';
        return mark(decision.ref, 'jab-missing', 'attach your resume');
      }
      try {
        const blob = await fetchResume(resume.url);
        const area = r.kind === 'dropzone' ? el : (uploadArea(el) || el.parentElement || el);
        const before = n(textOf(area));
        const pageBefore = n(document.body.innerText);
        if (r.kind === 'uploadbutton') {
          // Open its dialog, then give the file to the input that appears there.
          const had = new Set(deepAll('input[type="file"]'));
          pressOn(el);
          let input = null;
          for (let i = 0; i < 20 && !input; i++) {
            await sleep(200);
            input = deepAll('input[type="file"]').find((f) => !had.has(f) && !f.disabled);
          }
          if (input && attach(input, blob, resume.filename)
              && await uploadShows(r.box, resume.filename, n(textOf(r.box)), pageBefore)) {
            totals.resume = 'resume attached'; return;
          }
          if (input) {
            totals.resume = 'resume set in the upload dialog; please check it shows';
            totals.review++;
            return mark(decision.ref, 'jab-review', 'resume set in the upload dialog; check it shows (also in Downloads)');
          }
        } else if (r.kind === 'dropzone') {
          dropOn(el, blob, resume.filename);
          if (await uploadShows(area, resume.filename, before, pageBefore)) { totals.resume = 'resume attached (dropped)'; return; }
          // A hidden input may have appeared once the area was used.
          const input = el.querySelector('input[type="file"]');
          if (input && attach(input, blob, resume.filename) && await uploadShows(area, resume.filename, before, pageBefore)) {
            totals.resume = 'resume attached'; return;
          }
        } else if (attach(el, blob, resume.filename)) {
          // Most pages show the file at once; one that doesn't may want it dropped instead.
          if (await uploadShows(area, resume.filename, before, pageBefore)) { totals.resume = 'resume attached'; return; }
          const zone = area !== el && DROP_TEXT.test(textOf(area)) ? area : null;
          if (zone) {
            dropOn(zone, blob, resume.filename);
            if (await uploadShows(area, resume.filename, before, pageBefore)) { totals.resume = 'resume attached (dropped)'; return; }
          }
          totals.resume = 'resume set; please check it shows';
          totals.review++;
          return mark(decision.ref, 'jab-review', 'resume set; check the page shows it (it is also in Downloads)');
        }
      } catch (e) {
        totals.resume = 'resume: not available (' + e.message + ')';
      }
      return mark(decision.ref, 'jab-missing', 'attach your resume (the PDF is in Downloads)');
    }
    if (a === 'unknown') {
      if (r.had) return;  // LinkedIn's remembered answer stays
      if (decision.required || r.required) { totals.missing++; return mark(decision.ref, 'jab-missing', decision.reason || 'no answer yet'); }
      totals.optional++;
      return mark(decision.ref, 'jab-optional', decision.reason || 'no answer yet');
    }
    const v = decision.value;
    let ok = false;
    if (r.kind === 'ariachecks') {
      const want = Array.isArray(v) ? v : [v];
      for (const w of want) {
        const chip = bestOption(r.chips, [String(w)]);
        if (chip && !ariaChecked(chip)) { pressOn(chip); await sleep(120); if (!ariaChecked(chip)) chip.click(); }
      }
      ok = r.chips.some(ariaChecked);
    } else if (r.kind === 'ariaradio') {
      const hit = bestOption(r.radios, [String(v)].concat(decision.alts || []));
      if (hit) { pressOn(hit); await sleep(150); ok = ariaChecked(hit) || (hit.click(), await sleep(150), ariaChecked(hit)); }
    } else if (r.kind === 'ariacheck') {
      if (v === true && !ariaChecked(el)) { pressOn(el); await sleep(150); if (!ariaChecked(el)) el.click(); }
      ok = true;
      if (v !== true) return;
    } else if (r.kind === 'richtext') {
      setRich(el, String(v));
      ok = stuck(r, String(v));
    } else if (r.kind === 'select' || r.kind === 'multiselect') ok = chooseSelect(el, Array.isArray(v) ? v[0] : v);
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
    } else {
      typeInto(el, String(v));
      // Some fields drop a value set at once (masked phone boxes, custom inputs): type it.
      if (!stuck(r, String(v))) typeKeys(el, String(v));
      if (!stuck(r, String(v))) { totals.review++; return mark(decision.ref, 'jab-review', "typed " + v + " but the field didn't keep it"); }
      ok = true;
    }
    if (ok && r.kind === 'combobox' && !readValue(r)) {
      totals.review++;
      return mark(decision.ref, 'jab-review', 'picked ' + v + '; please check it took');
    }
    if (!ok) {
      if (r.required) { totals.missing++; return mark(decision.ref, 'jab-missing', 'could not choose: ' + v); }
      totals.optional++;
      return mark(decision.ref, 'jab-optional', 'could not choose: ' + v);
    }
    if (a === 'review') { totals.review++; mark(decision.ref, 'jab-review', 'filled; please check'); }
    else { totals.filled++; r.outcome = 'filled: ' + String(readValue(r)).slice(0, 60); }
  }

  let recipeLoaded = false;
  async function loadRecipe() {
    if (recipeLoaded) return;
    recipeLoaded = true;
    try {
      const r = await request('GET', ENDPOINT + '/autofill/site?host=' + encodeURIComponent(location.hostname),
        { headers: { 'X-JAB': '1' } });
      siteExtra = (JSON.parse(r.responseText).selectors || []).filter((sel) => {
        try { document.querySelector(sel); return true; } catch (e) { return false; }
      });
    } catch (e) { /* no recipe: the generic scan alone */ }
  }

  async function pass() {
    if (busy) return;
    busy = true;
    try {
      await loadRecipe();
      const fields = scan();
      if (!fields.length) { coverage(); if (totals.unseen) say(summary()); return; }
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
        unseen.forEach((x) => { if (handled.has(x)) { unseen.delete(x); x.classList.remove('jab-unseen'); } });
        const r = refs.get(d.ref);
        if (r) {
          r.decision = d;
          // What the autofill left in the field: compared at submit to spot a correction.
          const now = (d.action === 'fill' || d.action === 'review') ? readValue(r) : '';
          r.filled = isEmpty(now) ? null : now;
        }
      }
      coverage();
      say(summary());
    } finally {
      busy = false;
      window.__jabPasses = (window.__jabPasses || 0) + 1;
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
    // Steps that load as new pages on the same site keep going in this tab.
    try { sessionStorage.setItem('jab-active', JSON.stringify({ host: location.hostname, at: Date.now() })); } catch (e) { /* private mode */ }
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

  let snapshotSent = 0;
  async function debugSnapshot(silent) {
    if (silent && Date.now() - snapshotSent < 60000) return;  // automatic ones: at most one a minute
    snapshotSent = Date.now();
    const els = deepAll(FILLABLE)
      .filter((e) => !e.closest('#' + PANEL_ID) && e.type !== 'hidden').slice(0, 300);
    const fields = els.map((e) => {
      const box = e.closest('[data-automation-id^="formField-"]') || e.parentElement || e;
      return {
        tag: e.tagName, type: e.type || '', label: labelOf(e), group: groupOf(e), visible: visible(e),
        automation_id: e.getAttribute('data-automation-id') || '', handled: handled.has(e), unseen: unseen.has(e),
        selector: fingerprint(e),
        html: box.outerHTML.slice(0, 2500),
      };
    });
    const seen = [...refs.entries()].map(([ref, r]) => ({
      ref, kind: r.kind, decision: r.decision || null, outcome: r.outcome || null, now: readValue(r) }));
    try {
      await request('POST', ENDPOINT + '/autofill/debug', {
        headers: { 'Content-Type': 'application/json', 'X-JAB': '1' },
        data: JSON.stringify({ url: location.href, title: document.title, fields, refs: seen, auto: !!silent }),
      });
      if (!silent) say('form sent to Job Bot for debugging.');
    } catch (e) {
      if (!silent) say('could not send the form: ' + esc(e.message));
    }
  }

  // ------------------------------------------------------------------------
  // Start
  // ------------------------------------------------------------------------

  // At document-start, before the page's own router can drop the tag.
  const tagged = takeTag();

  // A page that looks like a job application, on a site not known as an ATS.
  function looksLikeApplication() {
    const words = (document.title + ' ' + (document.body.innerText || '').slice(0, 20000)).toLowerCase();
    const resume = /\b(resume|résumé|curriculum vitae|cv)\b/.test(words);
    const files = deepAll('input[type=file]');
    // "Upload your resume" pages: one drop area, no other fields (Archer, Filestack pickers).
    if (resume && (files.length || DROP_TEXT.test(words)) && /upload|attach|drop/.test(words)) return true;
    const inputs = deepAll('input:not([type=hidden]), select, textarea').filter(visible);
    if (inputs.length < 3) return false;
    return (files.length > 0 && resume) || (/\bappl(y|ication)\b/.test(words) && resume);
  }

  function start() {
    if (AUTO) {
      // Driven by the auto-apply bot: fill the dialog for this job, nothing else.
      current = { jobId: AUTO.jobId, at: Date.now(), opened: false };
      document.addEventListener('submit', onSubmitish, true);
      document.addEventListener('click', onSubmitish, true);
      run();
      return;
    }
    if (BOARD_HOSTS.test(location.hostname)) { onBoard(tagged); return; }
    // Any tagged page: the job is also remembered, for a form embedded in a frame
    // (iCIMS, an embedded Greenhouse) or the next page / tab of the application.
    if (tagged) GM_setValue(PENDING, { jobId: tagged, at: Date.now() });
    const known = ATS_HOSTS.test(location.hostname);
    // Inside another site's frame (ads, widgets): nothing, unless it is an ATS form
    // or a job opened from Telegram is pending.
    if (window.top !== window.self && !known && !tagged && !pending()) return;
    current = session(tagged);
    document.addEventListener('submit', onSubmitish, true);
    document.addEventListener('click', onSubmitish, true);
    flush();
    GM_registerMenuCommand('Autofill this form', run);
    GM_registerMenuCommand('Send this form to Job Bot (debugging)', debugSnapshot);
    if (current) { run(); return; }
    try {
      const was = JSON.parse(sessionStorage.getItem('jab-active') || 'null');
      if (was && was.host === location.hostname && Date.now() - was.at < SESSION_MS) { run(); return; }
    } catch (e) { /* none */ }
    if (known) {
      const p = pending();
      if (p) { fromPending(p); return; }
      offer('a form is open.', [['Autofill', run]]);
      return;
    }
    // Any other site: look a few times as the page settles; offer only on an application.
    let tries = 0;
    const look = () => {
      if (active || document.getElementById(PANEL_ID)) return;
      if (looksLikeApplication()) {
        const p = pending();
        if (p) fromPending(p); else say('this looks like a job application.', [['Autofill', run]]);
        return;
      }
      if (++tries < 5) setTimeout(look, [1500, 3000, 6000, 12000][tries - 1] || 12000);
    };
    setTimeout(look, 800);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => setTimeout(start, 300));
  else start();
})();
