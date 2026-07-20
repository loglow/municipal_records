#!/usr/bin/env python3
"""Static site generator for the Westhampton records archive.

Scans Records/Boards/<board>/Agendas|Minutes/YYYY-MM-DD.pdf, merges the files
into meeting entries, and emits a complete static site into site/ (gitignored,
disposable), linking directly to the PDFs in Records/ — documents are never
copied:

    site/index.html                     homepage: the filterable records search
    site/boards/<board-slug>/index.html per-board pages
    site/site.js                        display-preference script
    site/style.css                      copied from the repo-root source
    site/Records -> ../Records          symlink so document links resolve

Sources at the repo root: build.py and style.css (hand-edited), plus the
Records/ document tree (the only things that belong in git).

Serving: point any static server at site/ (it follows the symlink). The
GitHub Actions deploy copies site/ with symlinks dereferenced — the only
place document bytes are ever duplicated is inside that ephemeral artifact.

Run:  python3 build.py
Pure standard library. Idempotent. Prints a build report to stdout.
"""

from __future__ import annotations

import datetime
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
BOARDS_DIR = ROOT / "Records" / "Boards"
OUTPUT_DIR = ROOT / "site"
MANIFEST_PATH = ROOT / ".build-manifest.json"

# Source subfolder name -> document kind
KIND_DIRS = {"Agendas": "agenda", "Minutes": "minutes"}

SITE_TITLE = "Westhampton public records"

DATE_NAME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
IGNORED_FILES = {".DS_Store", "Thumbs.db"}

DOC_ICON = (
    '<svg class="icon" viewBox="0 0 24 24" width="14" height="14" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round" aria-hidden="true">'
    '<path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 '
    '2-2V7.5z"/><path d="M14 2v6h6"/></svg>'
)

ARROW_ICON = (
    '<svg class="icon" viewBox="0 0 24 24" width="15" height="15" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round" aria-hidden="true">'
    '<path d="M19 12H5"/><path d="m12 19-7-7 7-7"/></svg>'
)

RESET_ICON = (
    '<svg class="icon" viewBox="0 0 24 24" width="14" height="14" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round" aria-hidden="true">'
    '<polyline points="1 4 1 10 7 10"/>'
    '<path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg>'
)

FOOTER_HTML = """<address class="contact">
<div>
<p class="contact-title">Clerk&rsquo;s office</p>
<p>1 South Road<br>
Westhampton, MA 01027<br>
<a href="mailto:clerk@westhamptonma.gov">clerk@westhamptonma.gov</a><br>
<a href="tel:+14132033080">413-203-3080</a></p>
</div>
<div>
<p class="contact-title">Office hours</p>
<p>Tuesday, 12:00&nbsp;PM to 6:00&nbsp;PM<br>
Wednesday, 12:00&nbsp;PM to 6:00&nbsp;PM<br>
<em>or by appointment</em></p>
</div>
</address>"""


# ---------------------------------------------------------------------------
# Data model


@dataclass
class Document:
    """One PDF: an agenda or minutes for one meeting of one board."""

    board: str
    date: datetime.date
    kind: str  # "agenda" or "minutes"
    source: Path
    size: int

    def url(self, root: str) -> str:
        rel = self.source.relative_to(ROOT).as_posix()
        return root + urllib.parse.quote(rel)


@dataclass
class Meeting:
    """A meeting entry: (board, date) with optional agenda and minutes."""

    board: str
    date: datetime.date
    agenda: Document | None = None
    minutes: Document | None = None


# ---------------------------------------------------------------------------
# Helpers


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "board"


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


def pdf_has_text_layer(path: Path) -> bool:
    """Heuristic: does this PDF embed any fonts / text-drawing operators?

    Image-only scans have neither. Checks raw bytes first, then peeks inside
    Flate-compressed streams where object streams may hide font dictionaries.
    """
    data = path.read_bytes()
    if b"/Font" in data:
        return True
    for m in re.finditer(rb"stream\r?\n", data):
        start = m.end()
        end = data.find(b"endstream", start)
        if end == -1:
            continue
        try:
            chunk = zlib.decompress(data[start:end].rstrip(b"\r\n"))
        except zlib.error:
            continue
        if b"/Font" in chunk or b"Tj" in chunk or b"TJ" in chunk:
            return True
    return False


# ---------------------------------------------------------------------------
# Scanning


def scan(warnings: list[str]) -> list[Document]:
    docs: list[Document] = []
    if not BOARDS_DIR.is_dir():
        warnings.append(f"Source tree missing: {BOARDS_DIR.relative_to(ROOT)}")
        return docs
    for board_dir in sorted(BOARDS_DIR.iterdir()):
        if board_dir.name in IGNORED_FILES:
            continue
        if not board_dir.is_dir():
            warnings.append(
                f"Unexpected file (not a board folder): "
                f"{board_dir.relative_to(ROOT)}"
            )
            continue
        board = board_dir.name
        for kind_dir in sorted(board_dir.iterdir()):
            if kind_dir.name in IGNORED_FILES:
                continue
            if not kind_dir.is_dir() or kind_dir.name not in KIND_DIRS:
                warnings.append(
                    f"Unexpected entry (expected Agendas/ or Minutes/): "
                    f"{kind_dir.relative_to(ROOT)}"
                )
                continue
            kind = KIND_DIRS[kind_dir.name]
            for f in sorted(kind_dir.iterdir()):
                if f.name in IGNORED_FILES:
                    continue
                if f.is_dir():
                    warnings.append(f"Unexpected subfolder: {f.relative_to(ROOT)}")
                    continue
                if f.suffix.lower() != ".pdf":
                    warnings.append(f"Not a PDF, skipped: {f.relative_to(ROOT)}")
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
                        f"Malformed date in filename (expected YYYY-MM-DD.pdf), "
                        f"skipped: {f.relative_to(ROOT)}"
                    )
                    continue
                docs.append(Document(board=board, date=date, kind=kind,
                                     source=f, size=f.stat().st_size))
                if not pdf_has_text_layer(f):
                    warnings.append(
                        f"Possible image-only scan (no text layer found): "
                        f"{f.relative_to(ROOT)} — consider OCR"
                    )
    return docs


def merge(docs: list[Document]) -> dict[str, list[Meeting]]:
    """Group documents into meeting entries, keyed by board name."""
    meetings: dict[tuple[str, datetime.date], Meeting] = {}
    for doc in docs:
        key = (doc.board, doc.date)
        m = meetings.setdefault(key, Meeting(board=doc.board, date=doc.date))
        setattr(m, doc.kind, doc)
    boards: dict[str, list[Meeting]] = {}
    for m in meetings.values():
        boards.setdefault(m.board, []).append(m)
    for entries in boards.values():
        entries.sort(key=lambda m: m.date, reverse=True)
    return dict(sorted(boards.items(), key=lambda kv: kv[0].lower()))


# ---------------------------------------------------------------------------
# HTML


def page(*, title: str, root: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<script>try{{var t=localStorage.getItem("theme");if(t==="light"||t==="dark")document.documentElement.setAttribute("data-theme",t)}}catch(e){{}}</script>
<link rel="stylesheet" href="{root}style.css">
<script src="{root}site.js" defer></script>
</head>
<body>
<a class="skip-link" href="#main">Skip to content</a>
<header>
<div class="wrap">
  <p class="site-name"><a href="{root}index.html">{esc(SITE_TITLE)}</a></p>
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


# Site-wide display preferences: theme select plus sort-order and date-format
# toggles in the header. Sort reorders any `table.records` tbody and any
# `.year-sections` container; date format swaps text on any [data-iso] cell.
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
  ['table.records tbody', '.year-sections'].forEach(function (selector) {
    document.querySelectorAll(selector).forEach(function (el) {
      sortables.push({ el: el, items: Array.prototype.slice.call(el.children) });
    });
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


def doc_link(doc: Document, root: str, aria: str) -> str:
    return (
        f'<a class="doc-link" href="{doc.url(root)}" aria-label="{esc(aria)}">'
        f'{DOC_ICON}<span class="size">{fmt_size(doc.size)}</span></a>'
    )


def agenda_cell(m: Meeting, root: str) -> str:
    if m.agenda:
        aria = f"{m.board} agenda, {long_date(m.date)} (PDF, {fmt_size(m.agenda.size)})"
        return doc_link(m.agenda, root, aria)
    return '<span class="muted" aria-label="No agenda">—</span>'


def minutes_cell(m: Meeting, root: str, today: datetime.date) -> str:
    if m.minutes:
        aria = f"{m.board} minutes, {long_date(m.date)} (PDF, {fmt_size(m.minutes.size)})"
        return doc_link(m.minutes, root, aria)
    if m.date < today:
        return ('<span class="muted pending" '
                'aria-label="Minutes pending approval">Pending</span>')
    return '<span class="muted" aria-label="No minutes yet">—</span>'


INDEX_SCRIPT = """
(function () {
  var form = document.getElementById('filters');
  var countLine = document.getElementById('count-line');
  var noResults = document.getElementById('no-results');
  var rows = Array.prototype.slice.call(
    document.querySelectorAll('#records-table tbody tr'));
  var total = rows.reduce(function (n, r) {
    return n + Number(r.getAttribute('data-ndocs')); }, 0);
  var baseText = countLine.textContent;
  var c = {
    reset: document.getElementById('f-reset'),
    board: document.getElementById('f-board'),
    year: document.getElementById('f-year'),
    type: document.getElementById('f-type'),
    q: document.getElementById('f-search')
  };
  form.hidden = false;
  form.addEventListener('submit', function (e) { e.preventDefault(); });

  function apply() {
    var board = c.board.value, year = c.year.value, type = c.type.value;
    var terms = c.q.value.trim().toLowerCase().split(/\\s+/).filter(Boolean);
    var shownDocs = 0, shownRows = 0;
    rows.forEach(function (r) {
      var ok = (!board || r.getAttribute('data-board') === board) &&
               (!year || r.getAttribute('data-year') === year) &&
               (!type || r.getAttribute('data-docs').indexOf(type) !== -1) &&
               terms.every(function (t) {
                 return r.getAttribute('data-search').indexOf(t) !== -1; });
      r.hidden = !ok;
      if (ok) {
        shownRows++;
        shownDocs += Number(r.getAttribute('data-ndocs'));
      }
    });
    var filtered = Boolean(board || year || type || terms.length);
    Object.keys(c).forEach(function (k) {
      if (k !== 'reset')
        c[k].classList.toggle('set', Boolean(c[k].value.trim()));
    });
    countLine.textContent = filtered
      ? shownDocs + ' of ' + total + ' records shown'
      : baseText;
    noResults.hidden = shownRows > 0;
    c.reset.disabled = !filtered;
  }

  ['board', 'year', 'type'].forEach(function (k) {
    c[k].addEventListener('change', apply);
  });
  c.q.addEventListener('input', apply);
  c.reset.addEventListener('click', function () {
    c.board.value = c.year.value = c.type.value = c.q.value = '';
    apply();
  });
  apply();
})();
"""


def build_index_page(boards: dict[str, list[Meeting]], n_docs: int,
                     today: datetime.date) -> str:
    root = ""
    all_meetings = sorted(
        (m for entries in boards.values() for m in entries),
        key=lambda m: (m.date, m.board.lower()), reverse=True,
    )
    years = sorted({m.date.year for m in all_meetings}, reverse=True)

    board_opts = "\n".join(
        f'<option value="{esc(b)}">{esc(b)}</option>' for b in boards)
    year_opts = "\n".join(f'<option value="{y}">{y}</option>' for y in years)

    rows = []
    for m in all_meetings:
        tag = timing_tag(m.date, today)
        docs_present = " ".join(
            k for k in ("agenda", "minutes") if getattr(m, k))
        ndocs = len(docs_present.split()) if docs_present else 0
        search = " ".join([
            m.board.lower(), m.date.isoformat(),
            long_date(m.date).lower(), short_date(m.date).lower(),
            docs_present, tag,
        ])
        board_url = f"{root}boards/{slugify(m.board)}/index.html"
        rows.append(
            f'<tr data-board="{esc(m.board)}" data-year="{m.date.year}" '
            f'data-docs="{docs_present}" data-ndocs="{ndocs}" '
            f'data-search="{esc(search)}">\n'
            f'<th scope="row" data-iso="{m.date.isoformat()}">{short_date(m.date)}</th>\n'
            f'<td><a href="{board_url}">{esc(m.board)}</a></td>\n'
            f'<td class="tags"><span class="tag tag-{tag}">{tag.capitalize()}</span></td>\n'
            f"<td>{agenda_cell(m, root)}</td>\n"
            f"<td>{minutes_cell(m, root, today)}</td>\n</tr>"
        )

    noun = "record" if n_docs == 1 else "records"
    body = f"""<h1>Records search</h1>

<form class="filters" id="filters" hidden>
  <button type="button" id="f-reset" disabled>{RESET_ICON}Reset</button>
  <select id="f-board" aria-label="Filter by board">
    <option value="">All boards</option>
{board_opts}
  </select>
  <select id="f-year" aria-label="Filter by year">
    <option value="">All years</option>
{year_opts}
  </select>
  <select id="f-type" aria-label="Filter by record type">
    <option value="">All types</option>
    <option value="agenda">Agendas</option>
    <option value="minutes">Minutes</option>
  </select>
  <input type="search" id="f-search" placeholder="Search" aria-label="Search records">
</form>

<table id="records-table" class="records">
<thead>
<tr><th scope="col" class="date-col">Date</th><th scope="col">Board</th><th scope="col">Tags</th><th scope="col">Agenda</th><th scope="col">Minutes</th></tr>
</thead>
<tbody>
{chr(10).join(rows)}
</tbody>
</table>
<p class="muted" id="no-results" hidden>No records match the current filters.</p>
<p class="count" id="count-line">{n_docs} {noun}</p>
<script>{INDEX_SCRIPT}</script>
"""
    return page(title=f"Records search — {SITE_TITLE}", root=root, body=body)


def archive_span(entries: list[Meeting]) -> str:
    """Human-readable date range of a board's archive, oldest to newest."""
    first = min(m.date for m in entries)
    last = max(m.date for m in entries)
    if first == last:
        return long_date(first)
    if first.year == last.year:
        return f"{first:%B} {first.day} – {long_date(last)}"
    return f"{long_date(first)} – {long_date(last)}"


def build_board_page(board: str, entries: list[Meeting],
                     today: datetime.date) -> str:
    root = "../../"
    years = sorted({m.date.year for m in entries}, reverse=True)
    year_nav = ""
    if len(years) > 1:
        links = " · ".join(f'<a href="#y{y}">{y}</a>' for y in years)
        year_nav = f'<nav class="year-nav" aria-label="Jump to year"><p>Jump to: {links}</p></nav>\n'
    sections = []
    for year in years:
        rows = []
        for m in entries:
            if m.date.year != year:
                continue
            tag = timing_tag(m.date, today)
            rows.append(
                f'<tr>\n<th scope="row" data-iso="{m.date.isoformat()}">{short_date(m.date)}</th>\n'
                f'<td class="tags"><span class="tag tag-{tag}">{tag.capitalize()}</span></td>\n'
                f"<td>{agenda_cell(m, root)}</td>\n"
                f"<td>{minutes_cell(m, root, today)}</td>\n</tr>"
            )
        sections.append(f"""<section aria-labelledby="y{year}-h">
<h2 id="y{year}"><span id="y{year}-h">{year}</span></h2>
<table class="records">
<thead>
<tr><th scope="col" class="date-col">Date</th><th scope="col">Tags</th><th scope="col">Agenda</th><th scope="col">Minutes</th></tr>
</thead>
<tbody>
{chr(10).join(rows)}
</tbody>
</table>
</section>""")
    n = len(entries)
    noun = "meeting record" if n == 1 else "meeting records"
    stats = f"{n} {noun}, {archive_span(entries)}"
    body = f"""<nav aria-label="Breadcrumb"><p class="crumb"><a href="{root}index.html">{ARROW_ICON}Home</a></p></nav>
<h1>{esc(board)}</h1>
<p class="board-stats">{stats}</p>
{year_nav}<div class="year-sections">
{chr(10).join(sections)}
</div>
"""
    return page(title=f"{board} — {SITE_TITLE}", root=root, body=body)


# ---------------------------------------------------------------------------
# Build


def build() -> int:
    today = datetime.date.today()
    warnings: list[str] = []
    docs = scan(warnings)
    boards = merge(docs)

    slugs: dict[str, str] = {}
    for board in boards:
        slug = slugify(board)
        if slug in slugs:
            warnings.append(
                f"Board slug collision: '{board}' and '{slugs[slug]}' both "
                f"map to '{slug}' — rename one folder"
            )
        slugs[slug] = board

    # Clean and regenerate site/ (never touches Records/ — the symlink
    # inside site/ is removed as a link, not followed).
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir()
    # Legacy cleanup: earlier versions generated at the repo root.
    for d in (ROOT / "boards", ROOT / "index"):
        if d.exists():
            shutil.rmtree(d)
    for f in (ROOT / "index.html", ROOT / "site.js"):
        f.unlink(missing_ok=True)

    (OUTPUT_DIR / "index.html").write_text(
        build_index_page(boards, len(docs), today), encoding="utf-8")
    for board, entries in boards.items():
        out = OUTPUT_DIR / "boards" / slugify(board) / "index.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(build_board_page(board, entries, today),
                       encoding="utf-8")
    (OUTPUT_DIR / "site.js").write_text(SITE_JS, encoding="utf-8")
    if (ROOT / "style.css").is_file():
        shutil.copy2(ROOT / "style.css", OUTPUT_DIR / "style.css")
    else:
        warnings.append("style.css is missing at the repo root — pages will "
                        "render unstyled")
    # Documents are served through this symlink locally; a deploy step
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

    n_pages = 1 + len(boards)
    print(f"{SITE_TITLE} — build report, {today.isoformat()}")
    n_agendas = sum(1 for d in docs if d.kind == "agenda")
    n_minutes = len(docs) - n_agendas
    n_meetings = sum(len(e) for e in boards.values())
    print(f"{len(docs)} PDFs ({n_agendas} agendas, {n_minutes} minutes) → "
          f"{n_meetings} meetings across {len(boards)} boards\n")
    width = max((len(b) for b in boards), default=5) + 2
    print(f"{'Board':<{width}}{'Meetings':>9}{'Agendas':>9}{'Minutes':>9}")
    for board, entries in boards.items():
        na = sum(1 for m in entries if m.agenda)
        nm = sum(1 for m in entries if m.minutes)
        print(f"{board:<{width}}{len(entries):>9}{na:>9}{nm:>9}")
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
    print(f"\nWrote {n_pages} pages to site/ (documents linked in place, "
          f"not copied)")
    return 0


if __name__ == "__main__":
    sys.exit(build())
