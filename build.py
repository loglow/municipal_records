#!/usr/bin/env python3
"""Static site generator for the Westhampton records archive.

Scans Records/<Section>/<Body>/<Kind>/YYYY-MM-DD.pdf, merges the files into
dated record entries, and emits a deliberately single-page static site into
site/ (gitignored, disposable), linking directly to the PDFs in Records/ —
documents are never copied:

    site/index.html             the whole site: one searchable records table
    site/site.js                display-preference script
    site/style.css              copied from the repo-root source
    site/Records -> ../Records  symlink so document links resolve

Sources at the repo root: build.py and style.css (hand-edited), plus the
Records/ document tree (the only things that belong in git).

Serving: point any static server at site/ (it follows the symlink). The
GitHub Actions deploy copies site/ with symlinks dereferenced — the only
place document bytes are ever duplicated is inside that ephemeral artifact.

The taxonomy is data, not code (see SECTIONS and KINDS below): folder names
are display names, every record has a "before" document (agenda/warrant) and
an "after" document (minutes/results), and new sections or document kinds
are new table entries, not new logic.

Run:  python3 build.py
Pure standard library. Idempotent. Prints a build report to stdout.
"""

from __future__ import annotations

import datetime
import hashlib
import html
import json
import re
import shutil
import sys
import urllib.parse
import zlib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RECORDS_DIR = ROOT / "Records"
OUTPUT_DIR = ROOT / "site"
MANIFEST_PATH = ROOT / ".build-manifest.json"

SITE_TITLE = "Westhampton public records"

# ---------------------------------------------------------------------------
# Taxonomy — data, not code. Section folders live under Records/; each holds
# body folders (a board, an election type, ...) which hold kind folders.

SECTIONS: dict[str, dict] = {
    # Records/ folder -> display config (declaration order = display order)
    "Boards": {
        "title": "Boards",
        "pill": "Board",
    },
    "Town Meetings": {
        "title": "Town meetings",
        "pill": "Town Meeting",
        "known_bodies": [
            "Annual Town Meeting",
            "Special Town Meeting",
        ],
    },
    "Elections": {
        "title": "Elections",
        "pill": "Election",
        "known_bodies": [
            "Annual Town Caucus",
            "Annual Town Election",
            "Special Town Election",
            "State Primary",
            "State Election",
            "Presidential Primary",
            "Special State Primary",
            "Special State Election",
        ],
    },
}

KINDS: dict[str, tuple[str, str]] = {
    # kind folder name -> (role, document label)
    "Agendas": ("before", "Agenda"),
    "Warrants": ("before", "Warrant"),
    "Minutes": ("after", "Minutes"),
    "Results": ("after", "Results"),
}

DATE_NAME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
IGNORED_FILES = {".DS_Store", "Thumbs.db"}

DOC_ICON = (
    '<svg class="icon" viewBox="0 0 24 24" width="14" height="14" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round" aria-hidden="true">'
    '<path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 '
    '2-2V7.5z"/><path d="M14 2v6h6"/></svg>'
)

RESET_ICON = (
    '<svg class="icon" viewBox="0 0 24 24" width="14" height="14" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round" aria-hidden="true">'
    '<polyline points="1 4 1 10 7 10"/>'
    '<path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg>'
)

# Clerk contact details — single source of truth for the page footer and
# the print footer.
CONTACT = {
    "title": "Town Clerk",
    "addr1": "1 South Road",
    "addr2": "Westhampton, MA 01027",
    "email": "clerk@westhamptonma.gov",
    "phone": "413-203-3080",
}

_TEL = "".join(c for c in CONTACT["phone"] if c.isdigit())

FOOTER_HTML = f"""<address class="contact">
<div>
<p class="contact-title">{CONTACT["title"]}</p>
<p>{CONTACT["addr1"]}, {CONTACT["addr2"]}<br>
<a href="mailto:{CONTACT["email"]}">{CONTACT["email"]}</a> &bull; 
<a href="tel:+1{_TEL}">{CONTACT["phone"]}</a></p>
</div>
</address>"""

PRINT_CONTACT_LINE = (f"<strong>{CONTACT['title']}</strong> &bull; "
                      f"{CONTACT['addr1']}, "
                      f"{CONTACT['addr2']} &bull; {CONTACT['email']} &bull; "
                      f"{CONTACT['phone']}")


# ---------------------------------------------------------------------------
# Data model


@dataclass
class Document:
    """One PDF: a before-document (agenda/warrant) or after-document
    (minutes/results) for one dated record of one body."""

    section: str
    body: str
    date: datetime.date
    role: str  # "before" or "after"
    label: str  # "Agenda", "Warrant", "Minutes", "Results"
    source: Path
    size: int
    pages: int | None

    def url(self) -> str:
        return urllib.parse.quote(self.source.relative_to(ROOT).as_posix())


@dataclass
class Record:
    """A dated record: (section, body, date) with its documents."""

    section: str
    body: str
    date: datetime.date
    before: Document | None = None
    after: Document | None = None


# ---------------------------------------------------------------------------
# Helpers


def long_date(d: datetime.date) -> str:
    return f"{d:%B} {d.day}, {d.year}"


def short_date(d: datetime.date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def fmt_size(n: int) -> str:
    kb = n / 1024
    if kb < 1000:
        return f"{max(1, round(kb))} KB"
    return f"{n / 1048576:.1f} MB"


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def timing_tag(date: datetime.date, today: datetime.date) -> str:
    if date < today:
        return "past"
    if date == today:
        return "today"
    return "upcoming"


def body_sort_key(section: str, body: str):
    """Known bodies keep their declared (logical) order; others alphabetical."""
    known = SECTIONS.get(section, {}).get("known_bodies", [])
    if body in known:
        return (0, known.index(body), "")
    return (1, 0, body.lower())


def pdf_info(path: Path) -> tuple[bool, int | None]:
    """One pass over a PDF: (has_text_layer, page_count).

    Text layer: image-only scans embed no fonts / text operators. Pages:
    count page objects (raw plus inside Flate-compressed object streams,
    where modern PDFs often keep them), falling back to the page tree's
    /Count. Both are heuristics; page count returns None when unsure.
    """
    data = path.read_bytes()
    chunks = [data]
    for m in re.finditer(rb"stream\r?\n", data):
        start = m.end()
        end = data.find(b"endstream", start)
        if end == -1:
            continue
        try:
            chunks.append(zlib.decompress(data[start:end].rstrip(b"\r\n")))
        except zlib.error:
            continue
    has_text = b"/Font" in data or any(
        b"/Font" in c or b"Tj" in c or b"TJ" in c for c in chunks[1:])
    pages = sum(len(re.findall(rb"/Type\s*/Page(?!s)", c)) for c in chunks)
    if pages == 0:
        counts = [int(n) for c in chunks
                  for n in re.findall(rb"/Count\s+(\d+)", c)]
        pages = max(counts, default=0)
    return has_text, (pages or None)


# ---------------------------------------------------------------------------
# Scanning


def scan(warnings: list[str]) -> list[Document]:
    docs: list[Document] = []
    if not RECORDS_DIR.is_dir():
        warnings.append("Source tree missing: Records/")
        return docs
    for section_dir in sorted(RECORDS_DIR.iterdir()):
        if section_dir.name in IGNORED_FILES:
            continue
        if not section_dir.is_dir():
            warnings.append(
                f"Unexpected file (not a section folder): "
                f"{section_dir.relative_to(ROOT)}")
            continue
        section = section_dir.name
        if section not in SECTIONS:
            warnings.append(
                f"Unknown section folder (expected one of "
                f"{', '.join(SECTIONS)}), skipped: "
                f"{section_dir.relative_to(ROOT)}")
            continue
        known = SECTIONS[section].get("known_bodies")
        for body_dir in sorted(section_dir.iterdir()):
            if body_dir.name in IGNORED_FILES:
                continue
            if not body_dir.is_dir():
                warnings.append(
                    f"Unexpected file (not a body folder): "
                    f"{body_dir.relative_to(ROOT)}")
                continue
            body = body_dir.name
            if known and body not in known:
                warnings.append(
                    f"'{body}' is not a known {section} type — typo? "
                    f"({body_dir.relative_to(ROOT)})")
            for kind_dir in sorted(body_dir.iterdir()):
                if kind_dir.name in IGNORED_FILES:
                    continue
                if not kind_dir.is_dir() or kind_dir.name not in KINDS:
                    warnings.append(
                        f"Unexpected entry (expected one of "
                        f"{', '.join(KINDS)}): {kind_dir.relative_to(ROOT)}")
                    continue
                role, label = KINDS[kind_dir.name]
                for f in sorted(kind_dir.iterdir()):
                    if f.name in IGNORED_FILES:
                        continue
                    if f.is_dir():
                        warnings.append(
                            f"Unexpected subfolder: {f.relative_to(ROOT)}")
                        continue
                    if f.suffix.lower() != ".pdf":
                        warnings.append(
                            f"Not a PDF, skipped: {f.relative_to(ROOT)}")
                        continue
                    m = DATE_NAME_RE.match(f.stem)
                    date = None
                    if m:
                        try:
                            date = datetime.date(int(m[1]), int(m[2]), int(m[3]))
                        except ValueError:
                            pass
                    if date is None:
                        warnings.append(
                            f"Malformed date in filename (expected "
                            f"YYYY-MM-DD.pdf), skipped: {f.relative_to(ROOT)}")
                        continue
                    has_text, pages = pdf_info(f)
                    docs.append(Document(
                        section=section, body=body, date=date, role=role,
                        label=label, source=f, size=f.stat().st_size,
                        pages=pages))
                    if not has_text:
                        warnings.append(
                            f"Possible image-only scan (no text layer "
                            f"found): {f.relative_to(ROOT)} — consider OCR")
    return docs


def merge(docs: list[Document], warnings: list[str]) -> list[Record]:
    """Fold documents into (section, body, date) records — one before-doc
    and one after-doc per record; duplicates warn and are ignored."""
    records: dict[tuple[str, str, datetime.date], Record] = {}
    for doc in docs:
        key = (doc.section, doc.body, doc.date)
        rec = records.setdefault(
            key, Record(section=doc.section, body=doc.body, date=doc.date))
        existing = getattr(rec, doc.role)
        if existing is not None:
            warnings.append(
                f"Two '{doc.role}' documents for {doc.body} "
                f"{doc.date.isoformat()} — keeping "
                f"{existing.source.relative_to(ROOT)}, ignoring "
                f"{doc.source.relative_to(ROOT)}")
            continue
        setattr(rec, doc.role, doc)
    return list(records.values())


# ---------------------------------------------------------------------------
# HTML


def asset_version(data: bytes) -> str:
    """Short content hash appended to asset URLs so browsers can cache
    forever yet never serve a stale stylesheet or script."""
    return hashlib.sha1(data).hexdigest()[:8]


def page(*, title: str, body: str) -> str:
    css = ROOT / "style.css"
    css_v = asset_version(css.read_bytes()) if css.is_file() else "0"
    js_v = asset_version(SITE_JS.encode())
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<script>try{{var t=localStorage.getItem("theme");if(t==="light"||t==="dark")document.documentElement.setAttribute("data-theme",t)}}catch(e){{}}</script>
<link rel="stylesheet" href="style.css?v={css_v}">
<script src="site.js?v={js_v}" defer></script>
</head>
<body>
<a class="skip-link" href="#main">Skip to content</a>
<header>
<div class="wrap">
  <p class="site-name"><a href="index.html">{esc(SITE_TITLE)}</a></p>
  <div class="display-controls" id="display-controls" hidden>
    <div class="control">
      <span class="control-label" id="date-label">Date</span>
      <div class="toggle" role="group" aria-labelledby="date-label">
        <button type="button" id="date-short" aria-pressed="true">Text</button>
        <button type="button" id="date-iso" aria-pressed="false">ISO</button>
      </div>
    </div>
    <div class="control">
      <span class="control-label" id="sort-label">Sort</span>
      <div class="toggle" role="group" aria-labelledby="sort-label">
        <button type="button" id="sort-newest" aria-pressed="true">Desc</button>
        <button type="button" id="sort-oldest" aria-pressed="false">Asc</button>
      </div>
    </div>
    <div class="control">
      <span class="control-label" id="theme-label">Theme</span>
      <div class="toggle" role="group" aria-labelledby="theme-label">
        <button type="button" id="theme-auto" aria-pressed="true">Auto</button>
        <button type="button" id="theme-light" aria-pressed="false">Light</button>
        <button type="button" id="theme-dark" aria-pressed="false">Dark</button>
      </div>
    </div>
  </div>
</div>
</header>
<main id="main">
{body}
</main>
<footer>
<div class="wrap">
{FOOTER_HTML}
</div>
</footer>
</body>
</html>
"""


# Site-wide display preferences: theme, sort order, and date format toggles
# in the header. Sort reorders any `table.records` tbody; date format swaps
# text on any [data-iso] cell.
SITE_JS = """\
(function () {
  var controls = document.getElementById('display-controls');
  if (!controls) return;
  var themeBtns = {
    auto: document.getElementById('theme-auto'),
    light: document.getElementById('theme-light'),
    dark: document.getElementById('theme-dark')
  };
  var sortBtns = {
    newest: document.getElementById('sort-newest'),
    oldest: document.getElementById('sort-oldest')
  };
  var dateBtns = {
    short: document.getElementById('date-short'),
    iso: document.getElementById('date-iso')
  };

  var theme = 'auto', sort = 'newest', datefmt = 'short';
  try {
    var t = localStorage.getItem('theme');
    if (t === 'light' || t === 'dark') theme = t;
    if (localStorage.getItem('sort') === 'oldest') sort = 'oldest';
    if (localStorage.getItem('datefmt') === 'iso') datefmt = 'iso';
  } catch (e) {}

  var sortables = [];
  document.querySelectorAll('table.records tbody').forEach(function (el) {
    sortables.push({ el: el, items: Array.prototype.slice.call(el.children) });
  });
  var dateCells = Array.prototype.slice.call(
    document.querySelectorAll('[data-iso]'));
  dateCells.forEach(function (cell) {
    cell.setAttribute('data-short', cell.textContent);
  });

  function setPressed(group, val) {
    Object.keys(group).forEach(function (k) {
      group[k].setAttribute('aria-pressed', String(k === val));
    });
  }

  function store(key, val, defval) {
    try {
      if (val === defval) localStorage.removeItem(key);
      else localStorage.setItem(key, val);
    } catch (e) {}
  }

  function applySort() {
    sortables.forEach(function (s) {
      (sort === 'oldest' ? s.items.slice().reverse() : s.items)
        .forEach(function (item) { s.el.appendChild(item); });
    });
    setPressed(sortBtns, sort);
    store('sort', sort, 'newest');
  }

  function applyDates() {
    var attr = datefmt === 'iso' ? 'data-iso' : 'data-short';
    dateCells.forEach(function (cell) {
      cell.textContent = cell.getAttribute(attr);
    });
    setPressed(dateBtns, datefmt);
    store('datefmt', datefmt, 'short');
  }

  function applyTheme() {
    setPressed(themeBtns, theme);
    store('theme', theme, 'auto');
    if (theme === 'auto') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', theme);
  }

  Object.keys(themeBtns).forEach(function (k) {
    themeBtns[k].addEventListener('click', function () { theme = k; applyTheme(); });
  });
  sortBtns.newest.addEventListener('click', function () { sort = 'newest'; applySort(); });
  sortBtns.oldest.addEventListener('click', function () { sort = 'oldest'; applySort(); });
  dateBtns.short.addEventListener('click', function () { datefmt = 'short'; applyDates(); });
  dateBtns.iso.addEventListener('click', function () { datefmt = 'iso'; applyDates(); });

  applySort();
  applyDates();
  applyTheme();
  controls.hidden = false;
})();
"""


def doc_cell(doc: Document | None) -> str:
    if doc is None:
        return '<span class="muted">—</span>'
    spoken = f"PDF, {fmt_size(doc.size)}"
    if doc.pages:
        spoken += f", {doc.pages} page" + ("s" if doc.pages != 1 else "")
    pages_html = f'<span class="pages">{doc.pages or ""}</span>'
    aria = (f"{doc.body} {doc.label.lower()}, {long_date(doc.date)} "
            f"({spoken})")
    return (
        f'<a class="doc-link" href="{doc.url()}" aria-label="{esc(aria)}">'
        f'{DOC_ICON}<span class="size">{fmt_size(doc.size)}</span>'
        f'{pages_html}</a>'
    )


INDEX_SCRIPT = """
(function () {
  var form = document.getElementById('filters');
  var countLine = document.getElementById('count-line');

  function setAll(cls, text) {
    document.querySelectorAll('.' + cls).forEach(function (el) {
      el.textContent = text;
    });
  }
  var rows = Array.prototype.slice.call(
    document.querySelectorAll('#records-table tbody tr'));
  var total = rows.reduce(function (n, r) {
    return n + Number(r.getAttribute('data-ndocs')); }, 0);
  var c = {
    reset: document.getElementById('f-reset'),
    body: document.getElementById('f-body'),
    year: document.getElementById('f-year'),
    q: document.getElementById('f-search')
  };
  form.hidden = false;
  form.addEventListener('submit', function (e) { e.preventDefault(); });

  // Body names act as filter shortcuts: clicking one selects that body in
  // the dropdown. Upgraded from plain text here so the no-JS page keeps
  // honest static text.
  rows.forEach(function (r) {
    var cell = r.cells[1];
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'body-btn';
    btn.textContent = cell.textContent;
    btn.setAttribute('aria-label',
      'Filter by ' + cell.textContent);
    btn.addEventListener('click', function () {
      c.body.value = 'b|' + r.getAttribute('data-section') + '|' +
        r.getAttribute('data-body');
      apply();
    });
    cell.textContent = '';
    cell.appendChild(btn);
  });

  function rowMatchesBody(r, val) {
    if (!val) return true;
    var sep = val.indexOf('|');
    var mode = val.slice(0, sep), rest = val.slice(sep + 1);
    if (mode === 's') return r.getAttribute('data-section') === rest;
    var sep2 = rest.indexOf('|');
    return r.getAttribute('data-section') === rest.slice(0, sep2) &&
           r.getAttribute('data-body') === rest.slice(sep2 + 1);
  }

  function apply() {
    var bodyVal = c.body.value, year = c.year.value;
    var terms = c.q.value.trim().toLowerCase().split(/\\s+/).filter(Boolean);
    var shownDocs = 0;
    rows.forEach(function (r) {
      var ok = rowMatchesBody(r, bodyVal) &&
               (!year || r.getAttribute('data-year') === year) &&
               terms.every(function (t) {
                 return r.getAttribute('data-search').indexOf(t) !== -1; });
      r.hidden = !ok;
      if (ok) shownDocs += Number(r.getAttribute('data-ndocs'));
    });
    var filtered = Boolean(bodyVal || year || terms.length);
    Object.keys(c).forEach(function (k) {
      if (k !== 'reset')
        c[k].classList.toggle('set', Boolean(c[k].value.trim()));
    });
    var countText = shownDocs + ' of ' + total + ' records shown';
    countLine.textContent = countText;
    setAll('js-print-count', countText);
    c.reset.disabled = !filtered;
    updatePrintFilters();
  }

  function timestamp() {
    var now = new Date();
    var pad = function (n) { return String(n).padStart(2, '0'); };
    var time = now.toLocaleTimeString('en-US',
      { hour: 'numeric', minute: '2-digit', timeZoneName: 'short' });
    return now.getFullYear() + '-' + pad(now.getMonth() + 1) + '-' +
      pad(now.getDate()) + ' ' + time;
  }

  function updatePrintFilters() {
    var bodyVal = c.body.value;
    var bodyLabel = bodyVal
      ? c.body.options[c.body.selectedIndex].text : 'All';
    var q = c.q.value.trim();
    setAll('js-print-generated', 'Generated: ' + timestamp());
    setAll('js-print-filters', 'Filters: Body = ' + bodyLabel +
      ', Year = ' + (c.year.value || 'All') +
      (q ? ', Search = \\u201c' + q + '\\u201d' : ''));
  }

  window.addEventListener('beforeprint', updatePrintFilters);

  ['body', 'year'].forEach(function (k) {
    c[k].addEventListener('change', apply);
  });
  c.q.addEventListener('input', apply);
  c.reset.addEventListener('click', function () {
    c.body.value = c.year.value = c.q.value = '';
    apply();
  });
  apply();
})();
"""


def build_index_page(records: list[Record], docs: list[Document],
                     sections_present: list[str],
                     today: datetime.date) -> str:
    page_title = "Meetings & elections"
    ordered = sorted(records, key=lambda r: (r.date, r.body.lower()),
                     reverse=True)
    years = sorted({r.date.year for r in ordered}, reverse=True)

    groups = []
    for s in sections_present:
        cfg = SECTIONS[s]
        bodies = sorted({r.body for r in records if r.section == s},
                        key=lambda b: body_sort_key(s, b))
        opts = [f'    <option value="s|{esc(s)}">'
                f"All {cfg['title'].lower()}</option>"]
        opts += [f'    <option value="b|{esc(s)}|{esc(b)}">{esc(b)}</option>'
                 for b in bodies]
        groups.append(f'  <optgroup label="{esc(cfg["title"])}">\n'
                      + "\n".join(opts) + "\n  </optgroup>")
    body_opts = "\n".join(groups)
    year_opts = "\n".join(f'    <option>{y}</option>' for y in years)

    rows = []
    for rec in ordered:
        pill = SECTIONS[rec.section]["pill"]
        pill_class = "tag-" + rec.section.lower().replace(" ", "-")
        tag = timing_tag(rec.date, today)
        ndocs = (1 if rec.before else 0) + (1 if rec.after else 0)
        search = " ".join([
            rec.body.lower(), pill.lower(), rec.date.isoformat(),
            long_date(rec.date).lower(), short_date(rec.date).lower(), tag,
        ])
        rows.append(
            f'<tr data-section="{esc(rec.section)}" data-body="{esc(rec.body)}" '
            f'data-year="{rec.date.year}" data-ndocs="{ndocs}" '
            f'data-search="{esc(search)}">\n'
            f'<th scope="row" data-iso="{rec.date.isoformat()}">'
            f'{short_date(rec.date)}</th>\n'
            f'<td>{esc(rec.body)}</td>\n'
            f'<td class="tags center"><span class="tag tag-section {pill_class}">{esc(pill)}</span></td>\n'
            f'<td class="tags center"><span class="tag tag-{tag}">{tag.capitalize()}</span></td>\n'
            f'<td class="doc">{doc_cell(rec.before)}</td>\n'
            f'<td class="doc">{doc_cell(rec.after)}</td>\n</tr>'
        )

    n_docs = len(docs)
    footer_lines = f"""<p class="print-count js-print-count">{n_docs} of {n_docs} records shown</p>
<p><span class="js-print-generated">Generated just now</span> &bull; 
<span class="js-print-filters">Filters: Body = All, Year = All</span></p>
<p>{PRINT_CONTACT_LINE}</p>"""
    body = f"""<h1>{page_title}</h1>

<form class="filters" id="filters" hidden>
  <button type="button" id="f-reset" disabled>{RESET_ICON}Reset</button>
  <select id="f-body" aria-label="Filter by body">
    <option value="">All bodies</option>
{body_opts}
  </select>
  <select id="f-year" aria-label="Filter by year">
    <option value="">All years</option>
{year_opts}
  </select>
  <input type="search" id="f-search" placeholder="Search" aria-label="Search records">
</form>

<table id="records-table" class="records">
<thead>
<tr><th scope="col" class="date-col">Date</th><th scope="col">Body</th><th scope="col" class="center">Type</th><th scope="col" class="center">Status</th><th scope="col" class="center doc">Agenda/<br>Warrant</th><th scope="col" class="center doc">Minutes/<br>Results</th></tr>
</thead>
<tbody>
{chr(10).join(rows)}
</tbody>
<tfoot class="print-only">
<tr><td colspan="6"><div class="print-spacer"></div></td></tr>
</tfoot>
</table>
<p class="count" id="count-line">{n_docs} of {n_docs} records shown</p>
<div class="print-footer">
{footer_lines}
</div>
<script>{INDEX_SCRIPT}</script>
"""
    return page(title=f"{SITE_TITLE} — {page_title}", body=body)


# ---------------------------------------------------------------------------
# Build


def build() -> int:
    today = datetime.date.today()
    warnings: list[str] = []
    docs = scan(warnings)
    records = merge(docs, warnings)
    sections_present = [s for s in SECTIONS
                        if any(r.section == s for r in records)]

    # Clean and regenerate site/ (never touches Records/ — the symlink
    # inside site/ is removed as a link, not followed).
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir()

    (OUTPUT_DIR / "index.html").write_text(
        build_index_page(records, docs, sections_present, today),
        encoding="utf-8")
    (OUTPUT_DIR / "site.js").write_text(SITE_JS, encoding="utf-8")
    if (ROOT / "style.css").is_file():
        shutil.copy2(ROOT / "style.css", OUTPUT_DIR / "style.css")
    else:
        warnings.append("style.css is missing at the repo root — pages will "
                        "render unstyled")
    # Documents are served through this symlink locally; the deploy step
    # dereferences it when staging the final artifact.
    (OUTPUT_DIR / "Records").symlink_to("../Records", target_is_directory=True)

    # ---- build report -----------------------------------------------------
    current = {str(d.source.relative_to(ROOT)): d.size for d in docs}
    previous: dict[str, int] = {}
    first_build = not MANIFEST_PATH.exists()
    if not first_build:
        try:
            previous = json.loads(MANIFEST_PATH.read_text())["files"]
        except (ValueError, KeyError):
            first_build = True
    new_files = sorted(set(current) - set(previous))
    MANIFEST_PATH.write_text(json.dumps({"files": current}, indent=1))

    print(f"{SITE_TITLE} — build report, {today.isoformat()}")
    print(f"{len(docs)} PDFs → {len(records)} records across "
          f"{len(sections_present)} section(s)\n")
    for s in sections_present:
        s_records = [r for r in records if r.section == s]
        s_docs = [d for d in docs if d.section == s]
        print(f"{SECTIONS[s]['title']} — {len(s_docs)} PDFs, "
              f"{len(s_records)} records")
        bodies = sorted({r.body for r in s_records},
                        key=lambda b: body_sort_key(s, b))
        width = max(len(b) for b in bodies) + 2
        for b in bodies:
            n_rec = sum(1 for r in s_records if r.body == b)
            n_before = sum(1 for d in s_docs if d.body == b and d.role == "before")
            n_after = sum(1 for d in s_docs if d.body == b and d.role == "after")
            print(f"  {b:<{width}}{n_rec:>4} records{n_before:>4} before"
                  f"{n_after:>4} after")
        print()
    if first_build:
        print(f"New since last build: {len(new_files)} files (first build)")
    elif new_files:
        print(f"New since last build: {len(new_files)} files")
        for f in new_files:
            print(f"  + {f}")
    else:
        print("New since last build: none")
    if warnings:
        print(f"\n{len(warnings)} warning(s):")
        for w in warnings:
            print(f"  ! {w}")
    print("\nWrote site/index.html (documents linked in place, not copied)")
    return 0


if __name__ == "__main__":
    sys.exit(build())
