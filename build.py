#!/usr/bin/env python3
"""Static site generator for the Westhampton records archive.

Scans Records/<Section>/<Body>/<Kind>/YYYY-MM-DD.pdf — agendas and
warrants may carry the meeting time, place, and remote-meeting code as
"YYYY-MM-DD HHMM Location, Provider code.pdf" (24-hour time; each part
optional) — merges the files into
dated record entries, and emits a deliberately single-page static site into
site/ (gitignored, disposable), linking directly to the PDFs in Records/ —
documents are never copied:

    site/index.html             the whole site: a searchable records
                                table and a month-grid calendar view of
                                the same records (View toggle in the
                                header; the filters govern both)
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

import calendar
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

# Remote-meeting providers: a location part reading "<Provider> <code>"
# becomes a join link in the Location column. A new provider is a new entry.
REMOTE_PROVIDERS: dict[str, str] = {
    "Zoom": "https://zoom.us/j/{code}",
}

REMOTE_CODE_RE = re.compile(r"^\d{9,11}$")

STEM_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?: (\d{4}))?(?: (.+))?$")
IGNORED_FILES = {".DS_Store", "Thumbs.db"}

DOC_ICON = (
    '<svg class="icon" viewBox="0 0 24 24" width="14" height="14" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round" aria-hidden="true">'
    '<path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 '
    '2-2V7.5z"/><path d="M14 2v6h6"/></svg>'
)

EXT_ICON = (
    '<svg class="icon" viewBox="0 0 24 24" width="10" height="10" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round" aria-hidden="true">'
    '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 '
    '2-2h6"/><path d="M15 3h6v6"/><path d="M10 14 21 3"/></svg>'
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

# Known meeting places: location name -> address lines revealed when a
# reader expands the location name in the table. A new place is a new
# entry; a single line may be a plain string.
LOCATIONS: dict[str, tuple[str, ...] | str] = {
    "Town Hall": ("Westhampton Town Hall", "1 South Road", "Westhampton, MA 01027"),
    "Town Hall Annex": ("Westhampton Town Hall Annex", "3 South Road", "Westhampton, MA 01027"),
    "Public Library": ("Westhampton Public Library", "1 North Road", "Westhampton, MA 01027"),
    "Galica Residence": ("Galica Residence", "260 North Road", "Westhampton, MA 01027"),
    "HRHS Library": ("Hampshire Regional High School", "School Library", "19 Stage Road", "Westhampton, MA 01027"),
    "FHD Office": ("Foothills Health District Office", "45 Main Street", "Williamsburg, MA 01096"),
    "WH Woods Unit F": ("Westhampton Woods Senior Housing", "13 Main Road Unit F", "Westhampton, MA 01027"),
    "WES Library": ("Westhampton Elementary School", "School Library", "37 Kings Highway", "Westhampton, MA 01027"),
    "HRHS Room 133": ("Hampshire Regional High School", "Career Center Guidance Room 133", "19 Stage Road", "Westhampton, MA 01027"),
    "HRHS Room 148": ("Hampshire Regional High School", "Conference Room 148", "19 Stage Road", "Westhampton, MA 01027"),
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

PRINT_CONTACT_LINE = (f"{CONTACT['title']} &bull; "
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
    time: datetime.time | None
    location: str | None  # physical place
    remote: tuple[str, str | None] | None  # (provider, join URL or None)

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


def fmt_time(t: datetime.time) -> str:
    """12-hour display form; the ISO form is t.strftime('%H:%M')."""
    return f"{(t.hour % 12) or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


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

    Text layer: look for actual text-showing operators (Tj/TJ preceded by
    a string close, in raw or Flate-compressed content streams) — merely
    embedding a font is not enough, since image-only scans sometimes
    carry a stray font object. Pages: count page objects (raw plus inside
    compressed object streams, where modern PDFs often keep them),
    falling back to the page tree's /Count. Both are heuristics; page
    count returns None when unsure.
    """
    data = path.read_bytes()
    chunks = [data]
    for m in re.finditer(rb"stream\r?\n", data):
        start = m.end()
        end = data.find(b"endstream", start)
        if end == -1:
            continue
        raw = data[start:end].rstrip(b"\r\n")
        # wbits 47 auto-detects zlib/gzip framing; -15 handles the raw
        # deflate some producers emit
        for wbits in (47, -15):
            try:
                chunks.append(zlib.decompressobj(wbits).decompress(raw))
                break
            except zlib.error:
                continue
    text_op = re.compile(rb"[)>\]]\s*T[jJ]")
    has_text = any(text_op.search(c) for c in chunks)
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
                    m = STEM_RE.match(f.stem)
                    date = None
                    if m:
                        try:
                            date = datetime.date(int(m[1]), int(m[2]), int(m[3]))
                        except ValueError:
                            pass
                    if date is None:
                        warnings.append(
                            f"Malformed date in filename (expected "
                            f"YYYY-MM-DD[ HHMM Location].pdf), skipped: "
                            f"{f.relative_to(ROOT)}")
                        continue
                    time = location = remote = None
                    if m[4]:
                        try:
                            time = datetime.time(int(m[4][:2]), int(m[4][2:]))
                        except ValueError:
                            warnings.append(
                                f"Malformed time '{m[4]}' in filename, "
                                f"ignored: {f.relative_to(ROOT)}")
                    # The location, with an optional comma-separated
                    # "<Provider> <code>" remote part for hybrid meetings
                    # (or as the whole location when remote-only). A bare
                    # provider name with no meeting code still shows as a
                    # remote pill, just gray and unlinked.
                    for part in (m[5] or "").split(","):
                        part = part.strip()
                        if not part:
                            continue
                        for prov, url in REMOTE_PROVIDERS.items():
                            if part == prov:
                                if remote is None:
                                    remote = (prov, None)
                                break
                            code = (part[len(prov):].strip()
                                    if part.startswith(prov + " ") else "")
                            if code and REMOTE_CODE_RE.match(code):
                                if remote is None:
                                    remote = (prov, url.format(code=code))
                                break
                            if code:
                                warnings.append(
                                    f"Unrecognized {prov} meeting code "
                                    f"'{code}' (left as location text): "
                                    f"{f.relative_to(ROOT)}")
                        else:
                            if location is None:
                                location = part
                            else:
                                warnings.append(
                                    f"Multiple locations in filename (one "
                                    f"supported), ignored '{part}': "
                                    f"{f.relative_to(ROOT)}")
                    has_text, pages = pdf_info(f)
                    docs.append(Document(
                        section=section, body=body, date=date, role=role,
                        label=label, source=f, size=f.stat().st_size,
                        pages=pages, time=time, location=location,
                        remote=remote))
                    if not has_text:
                        warnings.append(
                            f"Possible image-only scan (no text layer "
                            f"found): {f.relative_to(ROOT)} — consider OCR")
    return docs


def merge(docs: list[Document], warnings: list[str]) -> list[Record]:
    """Fold documents into (section, body, date) records — one before-doc
    and one after-doc per record; duplicates warn and are ignored.

    A body can meet twice in one day: two before-docs with DISTINCT
    filename times are two meetings, each its own record. The after-docs
    then carry the meeting time too ("2026-01-28 1800.pdf") to say which
    meeting they belong to — an after-doc that cannot be paired gets its
    own record and a warning, so it stays visible either way."""
    buckets: dict[tuple[str, str, datetime.date], list[Document]] = {}
    for doc in docs:
        buckets.setdefault((doc.section, doc.body, doc.date),
                           []).append(doc)
    records = []
    for (section, body, date), group in buckets.items():
        befores = [d for d in group if d.role == "before"]
        slots = {d.time for d in befores}
        if len(befores) > 1 and len(slots) == len(befores) \
                and None not in slots:
            by_time = {b.time: Record(section=section, body=body,
                                      date=date, before=b)
                       for b in befores}
            for a in (d for d in group if d.role == "after"):
                rec = by_time.get(a.time)
                if rec is None:
                    warnings.append(
                        f"{len(befores)} meetings for {body} "
                        f"{date.isoformat()} — this after-doc needs a "
                        f"time matching one of them: "
                        f"{a.source.relative_to(ROOT)}")
                    records.append(Record(section=section, body=body,
                                          date=date, after=a))
                elif rec.after is not None:
                    warnings.append(
                        f"Two 'after' documents for {body} "
                        f"{date.isoformat()} — keeping "
                        f"{rec.after.source.relative_to(ROOT)}, ignoring "
                        f"{a.source.relative_to(ROOT)}")
                else:
                    rec.after = a
            records.extend(by_time.values())
            continue
        rec = Record(section=section, body=body, date=date)
        for doc in group:
            existing = getattr(rec, doc.role)
            if existing is not None:
                warnings.append(
                    f"Two '{doc.role}' documents for {doc.body} "
                    f"{doc.date.isoformat()} — keeping "
                    f"{existing.source.relative_to(ROOT)}, ignoring "
                    f"{doc.source.relative_to(ROOT)}")
                continue
            setattr(rec, doc.role, doc)
        records.append(rec)
    return records


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
<script>try{{if(localStorage.getItem("theme")==="light")document.documentElement.setAttribute("data-theme","light")}}catch(e){{}}</script>
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
      <span class="control-label" id="time-label">Time</span>
      <div class="toggle" role="group" aria-labelledby="time-label">
        <button type="button" id="time-12" aria-pressed="true">12h</button>
        <button type="button" id="time-24" aria-pressed="false">24h</button>
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
        <button type="button" id="theme-dark" aria-pressed="true">Dark</button>
        <button type="button" id="theme-light" aria-pressed="false">Light</button>
      </div>
    </div>
    <div class="control" id="view-control">
      <span class="control-label" id="view-label">View</span>
      <div class="toggle" role="group" aria-labelledby="view-label">
        <button type="button" id="view-table-btn" aria-pressed="true">Table</button>
        <button type="button" id="view-cal-btn" aria-pressed="false">Calendar</button>
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


# Site-wide display preferences: theme, sort order, and date and time
# format toggles in the header. Sort reorders any `table.records` tbody;
# time format swaps text on [data-24] cells; date format recomposes
# [data-iso] cells around their persistent year element (which the index
# script owns as a filter toggle). The pressed choice in each toggle is
# disabled.
SITE_JS = """\
(function () {
  var controls = document.getElementById('display-controls');
  if (!controls) return;
  var themeBtns = {
    dark: document.getElementById('theme-dark'),
    light: document.getElementById('theme-light')
  };
  var sortBtns = {
    newest: document.getElementById('sort-newest'),
    oldest: document.getElementById('sort-oldest')
  };
  var dateBtns = {
    short: document.getElementById('date-short'),
    iso: document.getElementById('date-iso')
  };
  var timeBtns = {
    h12: document.getElementById('time-12'),
    h24: document.getElementById('time-24')
  };
  var viewBtns = {
    table: document.getElementById('view-table-btn'),
    cal: document.getElementById('view-cal-btn')
  };
  var viewTable = document.getElementById('view-table');
  var viewCal = document.getElementById('view-cal');

  var theme = 'dark', sort = 'newest', datefmt = 'short', timefmt = 'h12';
  var view = 'table';
  try {
    if (localStorage.getItem('theme') === 'light') theme = 'light';
    if (localStorage.getItem('sort') === 'oldest') sort = 'oldest';
    if (localStorage.getItem('datefmt') === 'iso') datefmt = 'iso';
    if (localStorage.getItem('timefmt') === 'h24') timefmt = 'h24';
    if (localStorage.getItem('view') === 'cal') view = 'cal';
  } catch (e) {}

  var sortables = [];
  document.querySelectorAll('table.records tbody').forEach(function (el) {
    sortables.push({ el: el, items: Array.prototype.slice.call(el.children) });
  });
  // Date cells are two parts — the year element persists across format
  // swaps (the index script upgrades it into a filter toggle), so the
  // swap recomposes around it instead of rewriting the cell's text
  var dateCells = Array.prototype.slice.call(
    document.querySelectorAll('[data-iso]'));
  dateCells.forEach(function (cell) {
    var rest = cell.querySelector('.date-rest');
    if (rest) cell.setAttribute('data-rest', rest.textContent);
  });
  var timeCells = Array.prototype.slice.call(
    document.querySelectorAll('[data-24]'));
  timeCells.forEach(function (cell) {
    cell.setAttribute('data-12', cell.textContent);
  });

  function setPressed(group, val) {
    Object.keys(group).forEach(function (k) {
      group[k].setAttribute('aria-pressed', String(k === val));
      group[k].disabled = k === val;  // the active choice is inert
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
    dateCells.forEach(function (cell) {
      var rest = cell.querySelector('.date-rest');
      var y = cell.querySelector('.date-y');
      if (!rest || !y) return;
      if (datefmt === 'iso') {
        rest.textContent = cell.getAttribute('data-iso').slice(4);
        cell.insertBefore(y, rest);  // "2026" + "-07-15"
      } else {
        rest.textContent = cell.getAttribute('data-rest');
        cell.appendChild(y);         // "Jul 15, " + "2026"
      }
    });
    setPressed(dateBtns, datefmt);
    store('datefmt', datefmt, 'short');
  }

  function applyTimes() {
    var attr = timefmt === 'h24' ? 'data-24' : 'data-12';
    timeCells.forEach(function (cell) {
      cell.textContent = cell.getAttribute(attr);
    });
    setPressed(timeBtns, timefmt);
    store('timefmt', timefmt, 'h12');
  }

  function applyTheme() {
    setPressed(themeBtns, theme);
    store('theme', theme, 'dark');
    if (theme === 'light') {
      document.documentElement.setAttribute('data-theme', 'light');
    } else {
      document.documentElement.removeAttribute('data-theme');
    }
  }

  function applyView() {
    viewTable.hidden = view === 'cal';
    viewCal.hidden = view !== 'cal';
    setPressed(viewBtns, view);
    store('view', view, 'table');
  }

  Object.keys(themeBtns).forEach(function (k) {
    themeBtns[k].addEventListener('click', function () { theme = k; applyTheme(); });
  });
  if (viewTable && viewCal) {
    Object.keys(viewBtns).forEach(function (k) {
      viewBtns[k].addEventListener('click', function () { view = k; applyView(); });
    });
    applyView();
  } else {
    document.getElementById('view-control').remove();
  }
  sortBtns.newest.addEventListener('click', function () { sort = 'newest'; applySort(); });
  sortBtns.oldest.addEventListener('click', function () { sort = 'oldest'; applySort(); });
  dateBtns.short.addEventListener('click', function () { datefmt = 'short'; applyDates(); });
  dateBtns.iso.addEventListener('click', function () { datefmt = 'iso'; applyDates(); });
  timeBtns.h12.addEventListener('click', function () { timefmt = 'h12'; applyTimes(); });
  timeBtns.h24.addEventListener('click', function () { timefmt = 'h24'; applyTimes(); });

  applySort();
  applyDates();
  applyTimes();
  applyTheme();
  controls.hidden = false;

  // The controls wrap under the wordmark (with a divider) the moment
  // they would no longer fit beside it — measured, not a breakpoint,
  // so adding a control can never desynchronize the divider
  var headerEl = document.querySelector('header');
  var wrapEl = controls.parentNode;
  var nameEl = wrapEl.querySelector('.site-name');
  function fitControls() {
    var kids = Array.prototype.slice.call(controls.children);
    var cGap = parseFloat(getComputedStyle(controls).columnGap) || 0;
    var w = kids.reduce(function (sum, k) { return sum + k.offsetWidth; },
                        cGap * (kids.length - 1));
    var st = getComputedStyle(wrapEl);
    var avail = wrapEl.clientWidth - parseFloat(st.paddingLeft)
      - parseFloat(st.paddingRight);
    var gap = parseFloat(st.columnGap) || 0;
    headerEl.classList.toggle('wrapped',
      avail < nameEl.offsetWidth + gap + w);
  }
  window.addEventListener('resize', fitControls);
  if (window.ResizeObserver) new ResizeObserver(fitControls).observe(wrapEl);
  if (document.fonts && document.fonts.ready)
    document.fonts.ready.then(fitControls);
  fitControls();
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
  // Calendar chips mirror their table rows (shared data-rec) — the
  // filters compute row visibility once and the chips follow it
  var rowByRec = {};
  rows.forEach(function (r) { rowByRec[r.getAttribute('data-rec')] = r; });
  var calEvents = Array.prototype.slice.call(
    document.querySelectorAll('#view-cal .cal-ev'));
  var c = {
    reset: document.getElementById('f-reset'),
    body: document.getElementById('f-body'),
    year: document.getElementById('f-year'),
    loc: document.getElementById('f-loc'),
    status: document.getElementById('f-status'),
    q: document.getElementById('f-search')
  };
  form.hidden = false;
  form.addEventListener('submit', function (e) { e.preventDefault(); });

  // Every column offers in-table filter TOGGLES: clicking a value sets
  // it as that column's filter, clicking the same value again clears it.
  // All are upgraded from plain text/spans so the no-JS page keeps
  // honest static content.
  function toggleFilter(ctrl, val) {
    ctrl.value = ctrl.value === val ? '' : val;
    apply();
  }
  function shortcutBtn(className, text, onClick) {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = className;
    btn.textContent = text;
    btn.setAttribute('aria-label', 'Toggle filter by ' + text);
    btn.addEventListener('click', onClick);
    return btn;
  }

  rows.forEach(function (r) {
    // body name
    var bodyCell = r.cells[3];
    var bodyBtn = shortcutBtn('body-btn', bodyCell.textContent, function () {
      toggleFilter(c.body, 'b|' + r.getAttribute('data-section') + '|' +
        r.getAttribute('data-body'));
    });
    bodyCell.textContent = '';
    bodyCell.appendChild(bodyBtn);
    // the year within the date
    var year = r.cells[0].querySelector('.date-y');
    year.parentNode.replaceChild(
      shortcutBtn('date-y', year.textContent, function () {
        toggleFilter(c.year, r.getAttribute('data-year'));
      }), year);
    // type and status pills (buttons keep the span's pill classes)
    var type = r.cells[4].querySelector('.tag');
    type.parentNode.replaceChild(
      shortcutBtn(type.className, type.textContent, function () {
        toggleFilter(c.body, 's|' + r.getAttribute('data-section'));
      }), type);
    var status = r.cells[5].querySelector('.tag');
    status.parentNode.replaceChild(
      shortcutBtn(status.className, status.textContent, function () {
        toggleFilter(c.status, r.getAttribute('data-status'));
      }), status);
  });

  // Remote pills: the provider-name segment toggles the location filter
  // (providers count as locations); the link segment stays a plain link
  document.querySelectorAll('#records-table .remote-name').forEach(function (span) {
    var prov = span.textContent;
    span.parentNode.replaceChild(
      shortcutBtn('remote-name', prov, function () {
        toggleFilter(c.loc, prov);
      }), span);
  });

  // Location cells: the name filters like every other column; a separate
  // triangle button before names with a known address expands it in place
  var TRI = '<svg class="tri" viewBox="0 0 24 24" width="18" height="18" ' +
    'fill="currentColor" aria-hidden="true"><path d="M8 5l8 7-8 7z"/></svg>';
  var locToggles = [];
  document.querySelectorAll('#records-table .loc-place').forEach(function (span) {
    var cell = span.parentNode;
    var name = span.textContent;
    var nameBtn = shortcutBtn('loc-name', name, function () {
      toggleFilter(c.loc, name);
    });
    var addrData = span.getAttribute('data-addr');
    if (addrData) {
      var tri = document.createElement('button');
      tri.type = 'button';
      tri.className = 'loc-tri';
      tri.innerHTML = TRI;
      tri.setAttribute('aria-expanded', 'false');
      tri.setAttribute('aria-label', name + ' address');
      // grid-rows 0fr -> 1fr animates the reveal; the overflow-hidden
      // middle layer is what actually clips during the slide
      var addr = document.createElement('div');
      addr.className = 'loc-addr';
      addr.setAttribute('aria-hidden', 'true');
      var inner = document.createElement('div');
      inner.className = 'loc-addr-in';
      var text = document.createElement('div');
      text.className = 'loc-addr-text';
      addrData.split('|').forEach(function (line, i) {
        if (i) text.appendChild(document.createElement('br'));
        text.appendChild(document.createTextNode(line));
      });
      inner.appendChild(text);
      addr.appendChild(inner);
      cell.insertBefore(tri, span);
      cell.appendChild(addr);
      locToggles.push({ btn: tri, addr: addr });
      tri.addEventListener('click', function () {
        var open = tri.getAttribute('aria-expanded') === 'true';
        // Accordion: opening one collapses whichever other one is open
        if (!open) {
          locToggles.forEach(function (t) {
            if (t.btn !== tri &&
                t.btn.getAttribute('aria-expanded') === 'true') {
              t.btn.setAttribute('aria-expanded', 'false');
              t.addr.classList.remove('open');
              t.addr.setAttribute('aria-hidden', 'true');
            }
          });
        }
        tri.setAttribute('aria-expanded', String(!open));
        addr.classList.toggle('open', !open);
        addr.setAttribute('aria-hidden', String(open));
      });
    }
    cell.replaceChild(nameBtn, span);
  });

  // Calendar chips: body, location, and provider names are the same
  // filter toggles the table's cells offer
  calEvents.forEach(function (ev) {
    var r = rowByRec[ev.getAttribute('data-rec')];
    var name = ev.querySelector('.cal-ev-name');
    if (name) {
      var nb = shortcutBtn('chip-btn', name.textContent, function () {
        toggleFilter(c.body, 'b|' + r.getAttribute('data-section') + '|' +
          r.getAttribute('data-body'));
      });
      name.textContent = '';
      name.appendChild(nb);
    }
    var loc = ev.querySelector('.cal-ev-loc');
    if (loc) {
      var lb = shortcutBtn('chip-btn', loc.textContent, function () {
        toggleFilter(c.loc, r.getAttribute('data-loc'));
      });
      loc.textContent = '';
      loc.appendChild(lb);
    }
    var prov = ev.querySelector('.cal-ev-prov');
    if (prov) {
      var pb = shortcutBtn('chip-btn', prov.textContent, function () {
        toggleFilter(c.loc, r.getAttribute('data-remote'));
      });
      prov.textContent = '';
      prov.appendChild(pb);
    }
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
    var bodyVal = c.body.value, year = c.year.value, loc = c.loc.value;
    var status = c.status.value;
    var terms = c.q.value.trim().toLowerCase().split(/\\s+/).filter(Boolean);
    var shownDocs = 0;
    rows.forEach(function (r) {
      var ok = rowMatchesBody(r, bodyVal) &&
               (!year || r.getAttribute('data-year') === year) &&
               (!loc || r.getAttribute('data-loc') === loc ||
                r.getAttribute('data-remote') === loc) &&
               (!status || r.getAttribute('data-status') === status) &&
               terms.every(function (t) {
                 return r.getAttribute('data-search').indexOf(t) !== -1; });
      r.hidden = !ok;
      if (ok) shownDocs += Number(r.getAttribute('data-ndocs'));
    });
    calEvents.forEach(function (ev) {
      ev.classList.toggle('cal-ev-dim',
        rowByRec[ev.getAttribute('data-rec')].hidden);
    });
    var filtered = Boolean(bodyVal || year || loc || status || terms.length);
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
    setAll('js-print-filters', 'Filters: Year = ' + (c.year.value || 'All') +
      ', Location = ' + (c.loc.value || 'All') +
      ', Body = ' + bodyLabel +
      ', Status = ' + (c.status.value
        ? c.status.options[c.status.selectedIndex].text : 'All') +
      (q ? ', Search = \\u201c' + q + '\\u201d' : ''));
  }

  window.addEventListener('beforeprint', updatePrintFilters);

  ['body', 'year', 'loc', 'status'].forEach(function (k) {
    c[k].addEventListener('change', apply);
  });
  c.q.addEventListener('input', apply);
  c.reset.addEventListener('click', function () {
    c.body.value = c.year.value = c.loc.value = c.status.value =
      c.q.value = '';
    apply();
  });
  apply();

  // Calendar month navigation. Only months containing records exist,
  // so prev/next also step across gaps in the archive; the dropdown
  // doubles as the month title.
  var months = Array.prototype.slice.call(
    document.querySelectorAll('.cal-month'));
  if (months.length) {
    var now = new Date();
    var cur = now.getFullYear() + '-' +
      String(now.getMonth() + 1).padStart(2, '0');
    var startIdx = months.length - 1;
    months.forEach(function (m, i) {
      if (m.getAttribute('data-month') <= cur) startIdx = i;
    });
    var idx = startIdx;
    var first = document.getElementById('cal-first');
    var prev = document.getElementById('cal-prev');
    var next = document.getElementById('cal-next');
    var last = document.getElementById('cal-last');
    var todayBtn = document.getElementById('cal-today');
    var monthSel = document.getElementById('cal-month-sel');
    var yearSel = document.getElementById('cal-year-sel');
    var viewCal = document.getElementById('view-cal');
    var navEl = document.querySelector('.cal-nav');
    var MONTH_NAMES = ['January', 'February', 'March', 'April', 'May',
      'June', 'July', 'August', 'September', 'October', 'November',
      'December'];
    function showMonth() {
      months.forEach(function (m, i) { m.hidden = i !== idx; });
      var dm = months[idx].getAttribute('data-month');
      var year = dm.slice(0, 4);
      yearSel.value = year;
      // the month list holds only this year's months with records
      monthSel.textContent = '';
      months.forEach(function (m) {
        var v = m.getAttribute('data-month');
        if (v.slice(0, 4) !== year) return;
        var o = document.createElement('option');
        o.value = v;
        o.textContent = MONTH_NAMES[parseInt(v.slice(5), 10) - 1];
        monthSel.appendChild(o);
      });
      monthSel.value = dm;
      first.disabled = prev.disabled = idx === 0;
      last.disabled = next.disabled = idx === months.length - 1;
      todayBtn.disabled = idx === startIdx;
      // Today lights up when away; the month picker for any month that
      // is not the current one (same name in another year is still a
      // different month); the year picker only when the year differs
      navEl.classList.toggle('cal-away', idx !== startIdx);
      var cur = months[startIdx].getAttribute('data-month');
      monthSel.classList.toggle('away', dm !== cur);
      yearSel.classList.toggle('away', dm.slice(0, 4) !== cur.slice(0, 4));
    }
    prev.addEventListener('click', function () {
      if (idx > 0) { idx--; showMonth(); } });
    next.addEventListener('click', function () {
      if (idx < months.length - 1) { idx++; showMonth(); } });
    first.addEventListener('click', function () {
      idx = 0; showMonth(); });
    last.addEventListener('click', function () {
      idx = months.length - 1; showMonth(); });
    todayBtn.addEventListener('click', function () {
      idx = startIdx; showMonth(); });
    monthSel.addEventListener('change', function () {
      months.forEach(function (m, i) {
        if (m.getAttribute('data-month') === monthSel.value) idx = i;
      });
      showMonth();
    });
    yearSel.addEventListener('change', function () {
      // jump to the chosen year's month nearest the current one
      var curM = parseInt(
        months[idx].getAttribute('data-month').slice(5), 10);
      var best = idx, bestD = 99;
      months.forEach(function (m, i) {
        var v = m.getAttribute('data-month');
        if (v.slice(0, 4) !== yearSel.value) return;
        var d = Math.abs(parseInt(v.slice(5), 10) - curM);
        if (d < bestD) { bestD = d; best = i; }
      });
      idx = best;
      showMonth();
    });
    document.addEventListener('keydown', function (e) {
      if (viewCal.hidden ||
          /^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName)) return;
      if (e.key === 'ArrowLeft') prev.click();
      if (e.key === 'ArrowRight') next.click();
    });
    showMonth();
  }
})();
"""


def cal_event(rec: Record, idx: int) -> str:
    """One calendar entry, everything always visible: time on top, the
    body name, a letter-link per existing document, then the location
    (italic) and the remote link on their own lines."""
    slug = rec.section.lower().replace(" ", "-")
    links = []
    for doc in (rec.before, rec.after):
        if doc:
            links.append(
                f'<a class="cal-ev-doc" href="{doc.url()}" '
                f'title="{esc(doc.label)}" aria-label="{esc(rec.body)} '
                f'{esc(doc.label.lower())}, {esc(long_date(rec.date))}">'
                f'{doc.label[0]}</a>')
    time = rec.before.time if rec.before else None
    time_html = (f'<div class="cal-ev-time" data-24="{time:%H:%M}">'
                 f'{fmt_time(time)}</div>' if time else "")
    place = rec.before.location if rec.before else None
    remote = rec.before.remote if rec.before else None
    place_html = (f'<div class="cal-ev-loc">{esc(place)}</div>'
                  if place else "")
    remote_html = ""
    if remote:
        prov, join_url = remote
        remote_html = (f'<div class="cal-ev-remote">'
                       f'<span class="cal-ev-prov">{esc(prov)}</span>'
                       + (f' <a class="cal-ev-join" href="{esc(join_url)}" '
                          f'aria-label="Join {esc(prov)} meeting">'
                          f'{EXT_ICON}</a>' if join_url else "")
                       + '</div>')
    aria = f"{rec.body}, {long_date(rec.date)}"
    return (f'<div class="cal-ev cal-ev-{slug}" data-rec="{idx}">'
            f'{time_html}<div class="cal-ev-name" title="{esc(aria)}">'
            f'{esc(rec.body)}</div>'
            f'<span class="cal-ev-links">{"".join(links)}</span>'
            f'{place_html}{remote_html}</div>')


def build_calendar_html(ordered: list[Record],
                        today: datetime.date) -> str:
    """The same records as a month grid — only months that have records
    exist; the nav skips the gaps. Events mirror the table rows (data-rec
    pairs them) so the filters govern both views."""
    by_date: dict[datetime.date, list] = {}
    months: dict[tuple[int, int], int] = {}
    for i, rec in enumerate(ordered):
        slot = (rec.before.time if rec.before and rec.before.time
                else datetime.time.max)  # timeless records sink last
        by_date.setdefault(rec.date, []).append(
            (slot, rec.body.lower(), cal_event(rec, i)))
        months[(rec.date.year, rec.date.month)] = 0
    grid = calendar.Calendar(firstweekday=6)  # Sunday first
    dows = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    out = []
    for y, m in sorted(months):
        cells = [f'<div class="cal-dow">{d}</div>' for d in dows]
        for day in grid.itermonthdates(y, m):
            classes = ["cal-day"]
            if day.month != m:
                classes.append("is-out")
            if day == today and day.month == m:
                classes.append("is-today")
            evs = by_date.get(day, []) if day.month == m else []
            cells.append(
                f'<div class="{" ".join(classes)}">'
                f'<p class="cal-daynum">{day.day}</p>'
                + "".join(h for *_, h in sorted(evs)) + "</div>")
        out.append(
            f'<div class="cal-month" data-month="{y}-{m:02d}" hidden>'
            f'<div class="cal">{"".join(cells)}</div></div>')
    # Separate month and year pickers: the month list never exceeds
    # twelve however many years the archive grows to. The month options
    # are per-year, so the script fills them in.
    year_opts = "\n".join(
        f'    <option>{y}</option>'
        for y in sorted({y for y, _ in months}, reverse=True))
    prev_icon = ('<svg viewBox="0 0 24 24" width="16" height="16" '
                 'fill="none" stroke="currentColor" stroke-width="2.5" '
                 'stroke-linecap="round" stroke-linejoin="round" '
                 'aria-hidden="true"><path d="M14 6l-6 6 6 6"/></svg>')
    next_icon = prev_icon.replace('M14 6l-6 6 6 6', 'M10 6l6 6-6 6')
    first_icon = prev_icon.replace('M14 6l-6 6 6 6',
                                   'M12 6l-6 6 6 6M19 6l-6 6 6 6')
    last_icon = prev_icon.replace('M14 6l-6 6 6 6',
                                  'M5 6l6 6-6 6M12 6l6 6-6 6')
    return f"""<div class="cal-nav">
  <button type="button" class="cal-arrow" id="cal-first" aria-label="Earliest month">{first_icon}</button>
  <button type="button" class="cal-arrow" id="cal-prev" aria-label="Previous month">{prev_icon}</button>
  <button type="button" id="cal-today">{RESET_ICON}Today</button>
  <select id="cal-month-sel" aria-label="Go to month"></select>
  <select id="cal-year-sel" aria-label="Go to year">
{year_opts}
  </select>
  <button type="button" class="cal-arrow" id="cal-next" aria-label="Next month">{next_icon}</button>
  <button type="button" class="cal-arrow" id="cal-last" aria-label="Latest month">{last_icon}</button>
</div>
{"".join(out)}"""


def build_index_page(records: list[Record], docs: list[Document],
                     sections_present: list[str],
                     today: datetime.date) -> str:
    page_title = "Meetings & elections"
    ordered = sorted(
        records,
        key=lambda r: (r.date, r.body.lower(),
                       (r.before.time if r.before and r.before.time
                        else datetime.time.min)),
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
    locations = sorted(
        {r.before.location for r in ordered
         if r.before and r.before.location}
        | {r.before.remote[0] for r in ordered
           if r.before and r.before.remote})
    loc_opts = "\n".join(f'    <option>{esc(l)}</option>' for l in locations)

    dash = '<span class="muted">—</span>'
    rows = []
    for i, rec in enumerate(ordered):
        pill = SECTIONS[rec.section]["pill"]
        pill_class = "tag-" + rec.section.lower().replace(" ", "-")
        tag = timing_tag(rec.date, today)
        ndocs = (1 if rec.before else 0) + (1 if rec.after else 0)
        # Meeting time, place, and remote link ride on the agenda/warrant
        # filename
        time = rec.before.time if rec.before else None
        place = rec.before.location if rec.before else None
        remote = rec.before.remote if rec.before else None
        time_td = (f'<td class="time" data-24="{time:%H:%M}">'
                   f'{fmt_time(time)}</td>' if time
                   else f'<td class="time">{dash}</td>')
        # The place (with its address for the JS expand toggle when
        # known), then the remote provider as a link pill beside it
        loc_html = ""
        if place:
            addr = LOCATIONS.get(place)
            if isinstance(addr, str):  # single-line entries need no tuple
                addr = (addr,)
            data = f' data-addr="{esc("|".join(addr))}"' if addr else ""
            loc_html = (f'<span class="loc-place"{data}>'
                        f'{esc(place)}</span>')
        if remote:
            prov, join_url = remote
            beside = " beside" if place else ""
            if join_url:
                # Two connected segments: the name (a filter toggle once
                # JS upgrades it) and the join link
                loc_html += (
                    f'<span class="tag tag-remote split{beside}">'
                    f'<span class="remote-name">{esc(prov)}</span>'
                    f'<a class="remote-link" href="{esc(join_url)}" '
                    f'aria-label="Join {esc(prov)} meeting">{EXT_ICON}</a>'
                    f'</span>')
            else:  # no meeting code in the filename: gray, unlinked
                loc_html += (f'<span class="tag tag-remote nolink{beside}">'
                             f'<span class="remote-name">{esc(prov)}</span>'
                             f'</span>')
        loc_td = (f'<td class="loc">{loc_html}</td>' if loc_html
                  else f'<td class="loc">{dash}</td>')
        search = " ".join([
            rec.body.lower(), pill.lower(), rec.date.isoformat(),
            long_date(rec.date).lower(), short_date(rec.date).lower(), tag,
        ] + ([place.lower()] if place else [])
          + ([remote[0].lower()] if remote else []))
        rows.append(
            f'<tr data-rec="{i}" '
            f'data-section="{esc(rec.section)}" data-body="{esc(rec.body)}" '
            f'data-year="{rec.date.year}" data-loc="{esc(place or "")}" '
            f'data-remote="{esc(remote[0]) if remote else ""}" '
            f'data-status="{tag}" '
            f'data-ndocs="{ndocs}" data-search="{esc(search)}">\n'
            f'<th scope="row" data-iso="{rec.date.isoformat()}">'
            f'<span class="date-rest">{rec.date:%b} {rec.date.day}, </span>'
            f'<span class="date-y">{rec.date.year}</span></th>\n'
            f'{time_td}\n'
            f'{loc_td}\n'
            f'<td>{esc(rec.body)}</td>\n'
            f'<td class="center"><span class="tag tag-section {pill_class}">{esc(pill)}</span></td>\n'
            f'<td class="center"><span class="tag tag-{tag}">{tag.capitalize()}</span></td>\n'
            f'<td class="doc">{doc_cell(rec.before)}</td>\n'
            f'<td class="doc">{doc_cell(rec.after)}</td>\n</tr>'
        )

    n_docs = len(docs)
    footer_lines = f"""<p class="print-count"><strong>{esc(page_title)}</strong> &bull; <span class="js-print-count">{n_docs} of {n_docs} records shown</span></p>
<p><span class="js-print-generated">Generated just now</span> &bull; 
<span class="js-print-filters">Filters: Year = All, Location = All, Body = All, Status = All</span></p>
<p>{PRINT_CONTACT_LINE}</p>"""
    body = f"""<h1>{page_title}</h1>

<form class="filters" id="filters" hidden>
  <button type="button" id="f-reset" disabled>{RESET_ICON}Reset</button>
  <select id="f-year" aria-label="Filter by year">
    <option value="">All years</option>
{year_opts}
  </select>
  <select id="f-loc" aria-label="Filter by location">
    <option value="">All locations</option>
{loc_opts}
  </select>
  <select id="f-body" aria-label="Filter by body">
    <option value="">All bodies</option>
{body_opts}
  </select>
  <select id="f-status" aria-label="Filter by status">
    <option value="">All statuses</option>
    <option value="upcoming">Upcoming</option>
    <option value="today">Today</option>
    <option value="past">Past</option>
  </select>
  <input type="search" id="f-search" placeholder="Search" aria-label="Search records">
</form>

<div id="view-table">
<div class="table-scroll">
<table id="records-table" class="records">
<thead>
<tr><th scope="col" class="date-col">Date</th><th scope="col" class="time">Time</th><th scope="col" class="loc">Location</th><th scope="col">Body</th><th scope="col" class="center">Type</th><th scope="col" class="center">Status</th><th scope="col" class="center doc">Agenda/<br>Warrant</th><th scope="col" class="center doc">Minutes/<br>Results</th></tr>
</thead>
<tbody>
{chr(10).join(rows)}
</tbody>
<tfoot class="print-only">
<tr><td colspan="8"><div class="print-spacer"></div></td></tr>
</tfoot>
</table>
</div>
</div>
<div id="view-cal" hidden>
{build_calendar_html(ordered, today)}
</div>
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
