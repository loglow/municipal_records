#!/usr/bin/env python3
"""Deep consistency audit for the records archive — run after adding scans.

Cross-checks every PDF's FILENAME against the document's own text:

    date   — filename date vs dates written in the document, including
             warrant styles ("NINTH day of MAY, TWO THOUSAND TWENTY SIX",
             "13th day of April 2026"). When the years disagree, a printed
             weekday name arbitrates which year the writer actually meant.
    time   — filename HHMM vs times in the text ("7:00 PM", "Time: 11:00",
             "seven o'clock in the evening").
    zoom   — filename meeting code vs codes in the text: join URLs, or
             Zoom's spaced display form ("Meeting ID: 823 8344 7080").
    place  — filename location vs the text, matched through the name, its
             LOCATIONS address lines, and the alias spellings documents
             actually use ("the Westhampton Annex", "Westhampton Woods").
             Absence from the TEXT is reported but never fails the audit —
             documents often leave the venue implicit. An agenda or
             warrant FILENAME with neither a location nor a remote part
             does fail: every meeting notice says where the meeting is.
    body   — the folder the file is filed under vs the text, through the
             folder name and BODY_ALIASES (shorthand folder names spell
             out differently in print: "Town Admin. Search Comm." is
             "Town Administrator Search Committee" on the page). A body
             the text never names FAILS the audit — that is what a
             misfiled document looks like — and the report hints at
             which other body the text does name.

Findings are bucketed by severity and listed least-severe first, so the
action items — CHECK (needs a human) and MISMATCH (a filename is
probably wrong), both in red — sit last, next to the verdict. On a
normal run the informational buckets collapse to one-line counts;
--verbose (-v) lists every file in them. Documents the clerk has
already manually verified are listed in VERIFIED below and reported
separately instead of re-flagged forever.

Unlike build.py this needs one third-party library (pypdf) for real text
extraction. It manages that itself: on first run it creates a private
venv at .venv-audit/ next to this script (gitignored), installs pypdf
into it, and re-executes inside it — so plain `python3 audit.py` always
works.

Exits 1 if anything needs attention, 0 when clean.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import subprocess
import sys
import zlib
from pathlib import Path


def _bootstrap_pypdf() -> None:
    """Create a private venv with pypdf and re-exec this script in it."""
    venv_dir = Path(__file__).resolve().parent / ".venv-audit"
    if os.environ.get("AUDIT_BOOTSTRAPPED"):
        sys.exit("could not set up pypdf automatically; install by hand:\n"
                 f"  python3 -m venv {venv_dir}\n"
                 f"  {venv_dir}/bin/pip install pypdf")
    py = venv_dir / ("Scripts" if os.name == "nt" else "bin") / "python"
    try:
        if not py.exists():
            print(f"first run: creating a private venv for pypdf "
                  f"({venv_dir}) …", flush=True)
            import venv
            venv.create(venv_dir, with_pip=True)
        if subprocess.run([str(py), "-c", "import pypdf"],
                          capture_output=True).returncode != 0:
            print("installing pypdf …", flush=True)
            subprocess.run(
                [str(py), "-m", "pip", "install", "--quiet", "pypdf"],
                check=True)
    except (OSError, subprocess.CalledProcessError) as e:
        sys.exit(f"could not set up pypdf automatically ({e}); "
                 "install by hand:\n"
                 f"  python3 -m venv {venv_dir}\n"
                 f"  {venv_dir}/bin/pip install pypdf")
    os.environ["AUDIT_BOOTSTRAPPED"] = "1"
    os.execv(str(py), [str(py), str(Path(sys.argv[0]).resolve()),
                       *sys.argv[1:]])


try:
    from pypdf import PdfReader
except ImportError:
    _bootstrap_pypdf()
    raise SystemExit  # unreachable — execv does not return

# pypdf grumbles about repairable defects in scanner-produced files
# ("Ignoring wrong pointing object ..."). It recovers by itself, but the
# grumbles are worth attributing: capture them per file and report which
# document has the defect, instead of letting them dribble to stderr
# anonymously.
class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


_pdf_log = _ListHandler()
logging.getLogger("pypdf").addHandler(_pdf_log)
logging.getLogger("pypdf").propagate = False

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build import (  # noqa: E402
    LOCATIONS, RECORDS_DIR, REMOTE_PROVIDERS, STEM_RE)

# How documents actually write each filename location — beyond the name
# itself and its LOCATIONS address lines, which are probed automatically.
LOCATION_ALIASES: dict[str, tuple[str, ...]] = {
    "Town Hall": ("1 south rd",),
    "Town Hall Annex": ("westhampton annex", "the annex", "3 south rd"),
    "Public Library": ("1 north rd",),
    "Galica Residence": ("galica", "260 north rd"),
    "HRHS Library": ("hampshire regional", "school library"),
    "HRHS Room 133": ("hampshire regional", "room 133", "rm 133"),
    "HRHS Room 148": ("hampshire regional", "room 148", "rm 148"),
    "FHD Office": ("foothills",),
    "WH Woods Unit F": ("westhampton woods", "13 main rd"),
    "WES Library": ("westhampton elementary", "37 kings hwy"),
}


def location_probes(name: str) -> set[str]:
    probes = {name.lower()}
    addr = LOCATIONS.get(name)
    if isinstance(addr, str):
        addr = (addr,)
    for line in addr or ():
        # the local town/state line is on every letterhead — probing it
        # would confirm any location against any document
        if not line.lower().startswith("westhampton, ma"):
            probes.add(line.lower())
    probes.update(LOCATION_ALIASES.get(name, ()))
    return probes


# How documents actually write each body folder's name — beyond the
# folder name itself, which is probed automatically.
BODY_ALIASES: dict[str, tuple[str, ...]] = {
    "Board of Health": (
        "boh",
    ),
    "Council on Aging Advisory Board": (
        "council on aging",
        "coa advisory board",
        "coa meeting",
    ),
    "Foothills Health District Board": (
        "foothills health district",
    ),
    "Foothills Health District Executive Committee": (
        "executive committee",
        "exec. cttee",
        "exec cttee",
    ),
    "Foothills Health District Personnel Committee": (
        "personnel committee",
    ),
    "Finance Committee": (
        "fincom",
    ),
    "Hampshire Public Health Preparedness Coalition": (
        "public health preparedness coalition",
    ),
    "Hampshire Regional School Committee": (
        "hampshire regional",
    ),
    "Hampshire Regional School Finance Subcommittee": (
        "finance sub-committee",
        "finance subcommittee",
    ),
    "Hampshire Regional School Policy Subcommittee": (
        "policy subcommittee",
    ),
    "Library Board of Trustees": (
        "board of trustees",
    ),
    "Public Safety Complex Committee": (
        "westhampton safety complex",
        "public safety",
        "building committee meeting",
        "building committee",
        "public safety complex review committee",
    ),
    "Property and Energy Committee": (
        "property and energy",
        "energy assessment"
    ),
    "Town Administrator Search Committee": (
        "town administrator search committee",
        "search committee",
        "town administrator",
    ),
    "Westhampton Elementary School Committee": (
        "elementary school committee",
        "westhampton school committee",
    ),
    "Annual Town Caucus": (
        "caucus",
    ),
    "Annual Town Election": (
        "election",
    ),
    "Special Town Election": (
        "election",
    ),
    "State Election": (
        "state election",
        "election",
        "statewide",
    ),
    "State Primary": (
        "primary",
    ),
    "Presidential Primary": (
        "presidential",
        "primary",
    ),
}


def body_probes(name: str) -> set[str]:
    return {name.lower(), *BODY_ALIASES.get(name, ())}


# Findings the clerk has already checked against the paper record — shown
# as a reminder, not re-flagged. path (relative to Records/) -> note.
VERIFIED: dict[str, str] = {
    "Boards/Board of Health/Minutes/2026-03-09.pdf":
        "meeting was on Monday 2026-03-09; the 3/10/26 heading is incorrect",
    "Town Meetings/Annual Town Meeting/Warrants/2026-06-22 0900 Town Hall.pdf":
        "continuation of the 2026-05-09 meeting; same warrant for both",
    "Town Meetings/Special Town Meeting/Minutes/2025-05-10.pdf":
        "header says Annual by mistake; has 4 articles versus 39",
    "Boards/Planning Board/Minutes/2026-01-13.pdf":
        "Selectboard letterhead printed by mistake; Planning Board minutes",
    "Boards/Selectboard/Agendas/2026-06-29 1900 Town Hall, Zoom 82598611151.pdf":
        "Zoom code incorrect in OCR; the filename code is correct",
    "Boards/Selectboard/Agendas/2026-06-15 1900 Town Hall, Zoom 82598611151.pdf":
        "Zoom code unreadable in OCR; the filename code is correct",
    "Boards/Hampshire Public Health Preparedness Coalition/Agendas/2026-06-16 1130 Zoom.pdf":
        "no Zoom code is deliberate; the agenda states no code or link",
    "Boards/Selectboard/Agendas/2026-01-28 1800 Town Hall, Zoom 82598611151.pdf":
        "date changed due to winter storm; backup date does not state year",
    "Boards/Finance Committee/Minutes/2026-04-15.pdf":
        "agenda for two different boards; filed in both places",
    "Boards/Selectboard/Minutes/2026-04-15.pdf":
        "agenda for two different boards; filed in both places",
    "Boards/Town Administrator Search Committee/Minutes/2026-07-08.pdf":
        "minutes do not contain any date; date verified by clerk",
    "Boards/Hampshire Regional School Committee/Agendas/2026-01-05 1800 HRHS Library.pdf":
        "agenda for two different boards; filed in both places",
    "Boards/Westhampton Elementary School Committee/Agendas/2026-01-05 1800 HRHS Library.pdf":
        "agenda for two different boards; filed in both places",
    "Boards/Council on Aging Advisory Board/Minutes/2022-12-15.pdf":
        "verified COA minutes",
    "Boards/Zoning Board of Appeals/Minutes/2017-08-08.pdf":
        "verified ZBA minutes",
    "Boards/Cultural Council/Minutes/2013-12-03.pdf":
        "verified Cultural Council",
    "Boards/Board of Health/Minutes/2017-02-02.pdf":
        "verified date",
    "Boards/Board of Health/Minutes/2017-11-02.pdf":
        "verified date",
    "Boards/Board of Health/Minutes/2020-04-21.pdf":
        "verified date",
    "Boards/Board of Health/Minutes/2020-06-25.pdf":
        "verified date",
    "Boards/Public Safety Complex Committee/Minutes/2016-07-20.pdf":
        "verified board",
    "Boards/Public Safety Complex Committee/Minutes/2016-08-24.pdf":
        "verified board",
    "Boards/Public Safety Complex Committee/Minutes/2017-05-22.pdf":
        "verified board",
    "Boards/Public Safety Complex Committee/Minutes/2017-07-17.pdf":
        "verified board",
    "Boards/Public Safety Complex Committee/Minutes/2017-07-31.pdf":
        "verified board",
    "Boards/Public Safety Complex Committee/Minutes/2018-03-13.pdf":
        "verified date from other records",
    "Boards/Public Safety Complex Committee/Minutes/2018-03-26.pdf":
        "verified year",
    "Boards/Zoning Bylaw Review Committee/Minutes/2018-02-12.pdf":
        "suspected board",
    "Boards/Public Safety Complex Committee/Agendas/2017-05-08 1800 Town Hall.pdf":
        "verified handwritten time",
}

# ---------------------------------------------------------------------------
# Date / time vocabulary

MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday"]

_UNITS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
          "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
          "eleventh": 11, "twelfth": 12, "thirteenth": 13,
          "fourteenth": 14, "fifteenth": 15, "sixteenth": 16,
          "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
          "twentieth": 20, "thirtieth": 30, "thirty first": 31}
ORDINAL_WORDS = dict(_UNITS)
for _tens, _base in (("twenty", 20), ("thirty", 30)):
    for _u, _v in list(_UNITS.items())[:9]:
        for _sep in (" ", "-", ""):
            ORDINAL_WORDS[f"{_tens}{_sep}{_u}"] = _base + _v
_YEAR_WORDS = {"twenty": 20, "twenty one": 21, "twenty two": 22,
               "twenty three": 23, "twenty four": 24, "twenty five": 25,
               "twenty six": 26, "twenty seven": 27, "twenty eight": 28,
               "twenty nine": 29, "thirty": 30}
_HOUR_WORDS = {w: i for i, w in enumerate(
    ["one", "two", "three", "four", "five", "six", "seven", "eight",
     "nine", "ten", "eleven", "twelve"], 1)}

_MONTH_PAT = "|".join(sorted(MONTHS, key=len, reverse=True))
_ORD_PAT = "|".join(sorted(ORDINAL_WORDS, key=len, reverse=True))

# "Thursday, January 15, 2026" (weekday and ordinal suffix optional,
# comma spacing sloppy, one OCR junk character tolerated after the day —
# scans render "16th" as "16%" and the like)
RE_LONG = re.compile(
    r"(?:(" + "|".join(WEEKDAYS) + r")\s*,?\s+)?"
    r"(" + _MONTH_PAT + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?[%°'\"”*]?"
    r"\s*,?\s*(20\d\d)",
    re.I)
# "15 January 2026"
RE_LONG2 = re.compile(
    r"(\d{1,2})(?:st|nd|rd|th)?\s+(" + _MONTH_PAT + r")\.?\s*,?\s*(20\d\d)",
    re.I)
# "1/15/26", "01-15-2026"
RE_NUM = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](20\d\d|\d\d)\b")
RE_ISO = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")
# "13th day of April 2026" / "NINTH day of MAY, TWO THOUSAND TWENTY SIX"
RE_DAYOF = re.compile(
    r"(?:(\d{1,2})(?:st|nd|rd|th)?|(" + _ORD_PAT + r"))\s+day of\s+"
    r"(" + _MONTH_PAT + r")\s*,?\s*"
    r"(?:(20\d\d)|two thousand\s+((?:twenty|thirty)(?:[ -]\w+)?))?",
    re.I)
# A labeled date box ("Date: Tue Jan … 13th 2026"). Anchoring on the
# label lets this stay lenient about what OCR scatters between the month
# and the day — scans of two-column notice boxes interleave the next
# column's text there, and superscript ordinals arrive as junk.
RE_DATEBOX = re.compile(
    r"date:\s*(?:(mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?)?\s*"
    r"(" + _MONTH_PAT + r")\.?[^0-9]{0,40}?(\d{1,2})\D{0,6}?(20\d\d)",
    re.I)
# "7:00 PM" / "7 P.M."
RE_TIME = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s?m\b", re.I)
# "9:00 o'clock in the morning" / "seven o'clock in the evening"
RE_OCLOCK = re.compile(
    r"(?:(\d{1,2})(?::(\d{2}))?|(" + "|".join(_HOUR_WORDS) + r"))\s*"
    r"o.clock\s+in the\s+(morning|forenoon|afternoon|evening)", re.I)
# "Time: 11:00" (no AM/PM — trust the label, read the hour as written)
RE_TIMELABEL = re.compile(r"\btime:?\s*(\d{1,2}):(\d{2})\b", re.I)
# join URLs and Zoom's spaced display form
RE_ZOOM_URL = re.compile(r"zoom\.us/[js]/(\d{9,11})")
RE_ZOOM_ID = re.compile(r"\b(\d{3})\s?(\d{3,4})\s?(\d{4})\b")


def dates_in(text: str) -> set[tuple[datetime.date, str | None]]:
    """All plausible dates, each with the weekday word printed beside it
    (when there is one) for year arbitration."""
    found = set()

    def add(y, m, d, wd=None):
        try:
            found.add((datetime.date(y, m, d), wd.lower() if wd else None))
        except ValueError:
            pass

    for m in RE_LONG.finditer(text):
        add(int(m[4]), MONTHS[m[2].lower().rstrip(".")[:3] if len(m[2]) == 3
                              else m[2].lower()], int(m[3]), m[1])
    for m in RE_LONG2.finditer(text):
        add(int(m[3]), MONTHS[m[2].lower()], int(m[1]))
    for m in RE_NUM.finditer(text):
        y = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
        add(y, int(m[1]), int(m[2]))
    for m in RE_ISO.finditer(text):
        add(int(m[1]), int(m[2]), int(m[3]))
    for m in RE_DATEBOX.finditer(text):
        wd = None
        if m[1]:
            wd = next((w for w in WEEKDAYS
                       if w.startswith(m[1].lower())), None)
        add(int(m[4]), MONTHS[m[2].lower()], int(m[3]), wd)
    for m in RE_DAYOF.finditer(text):
        day = int(m[1]) if m[1] else ORDINAL_WORDS[
            re.sub(r"\s+", " ", m[2].lower())]
        year = None
        if m[4]:
            year = int(m[4])
        elif m[5]:
            yw = re.sub(r"[ -]+", " ", m[5].lower())
            if yw in _YEAR_WORDS:
                year = 2000 + _YEAR_WORDS[yw]
        if year:
            add(year, MONTHS[m[3].lower()], day)
    return found


def times_in(text: str) -> set[int]:
    """All plausible meeting times as HHMM (24h)."""
    found = set()

    def add(h12, minute, pm):
        if 1 <= h12 <= 12 and minute < 60:
            found.add(((h12 % 12) + (12 if pm else 0)) * 100 + minute)

    for m in RE_TIME.finditer(text):
        add(int(m[1]), int(m[2] or 0), m[3].lower() == "p")
    for m in RE_OCLOCK.finditer(text):
        h = int(m[1]) if m[1] else _HOUR_WORDS[m[3].lower()]
        add(h, int(m[2] or 0), m[4].lower() in ("afternoon", "evening"))
    for m in RE_TIMELABEL.finditer(text):
        h, minute = int(m[1]), int(m[2])
        if h <= 23 and minute < 60:      # as written, plus the PM reading
            found.add(h * 100 + minute)
            add(h, minute, True)
    return found


def zoom_codes_in(text: str, raw: str) -> set[str]:
    codes = set(RE_ZOOM_URL.findall(text)) | set(RE_ZOOM_URL.findall(raw))
    for m in RE_ZOOM_ID.finditer(text):
        codes.add("".join(m.groups()))
    return codes


# GoTo join URLs, and the hyphenated access-code form GoTo prints
# ("701-086-549"). Phone numbers don't match: US numbers group 3-3-4.
RE_GOTO_URL = re.compile(r"gotomeeting\.com/join/(\d{9,11})")
RE_GOTO_ID = re.compile(r"\b(\d{3})-(\d{3})-(\d{3})\b")


def remote_codes_in(text: str, raw: str) -> set[str]:
    """Meeting codes for every known provider — the audit checks filename
    codes against this. (intake's classifier stays Zoom-only and keeps
    using zoom_codes_in directly.)"""
    codes = zoom_codes_in(text, raw)
    codes |= set(RE_GOTO_URL.findall(text)) | set(RE_GOTO_URL.findall(raw))
    for m in RE_GOTO_ID.finditer(text):
        codes.add("".join(m.groups()))
    return codes


def raw_streams_text(path: Path) -> str:
    """Raw bytes + inflated streams as text — catches URLs that live in
    link annotations rather than page text."""
    data = path.read_bytes()
    chunks = [data]
    for m in re.finditer(rb"stream\r?\n", data):
        start = m.end()
        end = data.find(b"endstream", start)
        if end == -1:
            continue
        raw = data[start:end].rstrip(b"\r\n")
        for wbits in (47, -15):
            try:
                chunks.append(zlib.decompressobj(wbits).decompress(raw))
                break
            except zlib.error:
                continue
    return "\n".join(c.decode("latin-1", "ignore") for c in chunks)


# ---------------------------------------------------------------------------
# The audit

def parse_stem(stem: str):
    """filename -> (date, time|None, location|None, provider|None,
    code|None); mirrors build.py's grammar."""
    m = STEM_RE.match(stem)
    if not m:
        return None
    try:
        date = datetime.date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None
    hhmm = int(m[4]) if m[4] else None
    loc = prov = code = None
    for part in (m[5] or "").split(","):
        part = part.strip()
        if not part:
            continue
        matched = False
        for p in REMOTE_PROVIDERS:
            if part == p:
                prov, matched = p, True
            elif part.startswith(p + " "):
                c = part[len(p):].strip()
                if re.fullmatch(r"\d{9,11}", c):
                    prov, code, matched = p, c, True
        if not matched and loc is None:
            loc = part
    return date, hhmm, loc, prov, code


def fmt_hhmm(t: int) -> str:
    return f"{t // 100:02d}:{t % 100:02d}"


def main() -> int:
    verbose = any(a in ("-v", "--verbose") for a in sys.argv[1:])
    mismatch, check, doc_typos, no_date, no_time, notes = [], [], [], [], [], []
    grumbles, no_loc, weekday_typos = [], [], []
    n = n_date_ok = n_time_ok = n_loc_ok = n_body_ok = 0
    n_zoom_ok = 0
    all_bodies = sorted(
        body.name for section in RECORDS_DIR.iterdir() if section.is_dir()
        for body in section.iterdir() if body.is_dir())

    pdfs = sorted(RECORDS_DIR.rglob("*.pdf"))
    live = sys.stderr.isatty()  # progress ticker on the terminal only
    by_meeting: dict[tuple[str, object], list[str]] = {}

    for i, pdf in enumerate(pdfs, 1):
        rel = str(pdf.relative_to(RECORDS_DIR))
        if live:
            sys.stderr.write(f"\r\033[K  auditing {i}/{len(pdfs)}  "
                             f"{rel[:90]}")
            sys.stderr.flush()
        parsed = parse_stem(pdf.stem)
        if not parsed:
            continue  # build.py already warns about malformed names
        fdate, ftime, floc, _prov, fcode = parsed
        n += 1
        by_meeting.setdefault(
            (str(pdf.parent.relative_to(RECORDS_DIR)), fdate, ftime),
            []).append(pdf.name)
        if rel in VERIFIED:
            notes.append((rel, VERIFIED[rel]))
            continue
        kind = pdf.relative_to(RECORDS_DIR).parts[2]
        if kind in ("Agendas", "Warrants") and not floc and not _prov:
            check.append((rel, "no location or remote part in the "
                               "filename — where is the meeting?"))
        if kind in ("Minutes", "Results") \
                and fdate > datetime.date.today():
            check.append((rel, f"dated {fdate.isoformat()}, in the "
                               f"future — {kind.lower()} record a "
                               f"meeting that already happened"))
        _pdf_log.messages.clear()
        try:
            reader = PdfReader(str(pdf))
            text = re.sub(r"\s+", " ", " ".join(
                page.extract_text() or "" for page in reader.pages))
        except Exception as e:  # noqa: BLE001 — report and move on
            check.append((rel, f"could not read PDF: {e}"))
            continue
        if _pdf_log.messages:
            uniq = sorted(set(_pdf_log.messages))
            grumbles.append(
                (rel, f"{len(_pdf_log.messages)} parser warning(s): "
                      + "; ".join(uniq[:3])
                      + (" …" if len(uniq) > 3 else "")))

        # -- date ------------------------------------------------------
        dates = dates_in(text)
        if not dates:
            no_date.append(rel)
        elif any(d == fdate for d, _ in dates):
            n_date_ok += 1
            stale = sorted({wd for d, wd in dates if d == fdate and wd
                            and WEEKDAYS[d.weekday()] != wd})
            if stale:
                weekday_typos.append(
                    (rel, f"printed '{stale[0].capitalize()}' but "
                          f"{fdate.isoformat()} is a "
                          f"{WEEKDAYS[fdate.weekday()].capitalize()}"))
        else:
            near = [(d, wd) for d, wd in dates
                    if (d.month, d.day) == (fdate.month, fdate.day)]
            if near:
                for d, wd in near:
                    if wd and WEEKDAYS[fdate.weekday()] == wd != \
                            WEEKDAYS[d.weekday()]:
                        doc_typos.append(
                            (rel, f"doc says {d.isoformat()}, but its "
                                  f"'{wd.capitalize()}' matches the "
                                  f"filename year — doc-side typo"))
                        break
                    if wd and WEEKDAYS[d.weekday()] == wd != \
                            WEEKDAYS[fdate.weekday()]:
                        mismatch.append(
                            (rel, f"doc says {d.isoformat()} and its "
                                  f"'{wd.capitalize()}' matches the DOC "
                                  f"year — filename year looks wrong"))
                        break
                else:
                    d = near[0][0]
                    check.append((rel, f"doc says {d.isoformat()} (no "
                                       f"weekday printed to arbitrate)"))
            else:
                sample = ", ".join(sorted(d.isoformat() for d, _ in dates)[:4])
                check.append((rel, f"no date in the text matches the "
                                   f"filename; text has: {sample}"))

        # -- time ------------------------------------------------------
        if ftime is not None:
            times = times_in(text)
            # after-docs carry a time only to pair with one of several
            # same-day meetings, and record when the meeting actually
            # started ("called to order at 6:05 PM") — allow slack
            slack = 20 if kind in ("Minutes", "Results") else 0

            def near(t, want=ftime, s=slack):
                return abs((t // 100 * 60 + t % 100)
                           - (want // 100 * 60 + want % 100)) <= s

            if not times:
                no_time.append(rel)
            elif ftime in times or any(near(t) for t in times):
                n_time_ok += 1
            else:
                check.append(
                    (rel, f"filename time {fmt_hhmm(ftime)}; doc has "
                          + ", ".join(fmt_hhmm(t) for t in sorted(times))))

        # -- location --------------------------------------------------
        low = text.lower()
        if floc:
            if any(probe in low for probe in location_probes(floc)):
                n_loc_ok += 1
            else:
                no_loc.append(rel)

        # -- body (the folder the file is filed under) -----------------
        body = pdf.relative_to(RECORDS_DIR).parts[1]
        if any(probe in low for probe in body_probes(body)):
            n_body_ok += 1
        else:
            others = [b for b in all_bodies if b != body
                      and any(pr in low for pr in body_probes(b))]
            hint = (" — but the text names: " + ", ".join(others[:3])
                    if others else "")
            check.append((rel, f"filed under '{body}', which the text "
                               f"never names{hint}"))

        # -- zoom code -------------------------------------------------
        if _prov and not fcode:
            check.append((rel, f"bare '{_prov}' with no meeting code — "
                               f"almost never right; verify or find the "
                               f"code"))
        if fcode:
            codes = remote_codes_in(text, raw_streams_text(pdf))
            if not codes:
                check.append((rel, f"filename {_prov} code {fcode}, but "
                                   f"the document never shows one"))
            elif fcode in codes:
                n_zoom_ok += 1
            else:
                mismatch.append(
                    (rel, f"filename {_prov} code {fcode}; document has "
                          + ", ".join(sorted(codes))))

    if live:
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()

    # Two files in the same Kind folder with the same date are two
    # documents for one meeting — build.py keeps one and ignores the rest.
    for (folder, dupdate, duptime), names in sorted(
            by_meeting.items(), key=lambda kv: str(kv[0])):
        if len(names) > 1:
            when = dupdate.isoformat() + (f" {fmt_hhmm(duptime)}"
                                          if duptime is not None else "")
            check.append((folder, f"{len(names)} files for the "
                          f"{when} meeting: " + ", ".join(names)))

    # -- report ---------------------------------------------------------
    print(f"Records audit — {datetime.date.today().isoformat()}, "
          f"{n} PDFs checked")
    print(f"verified in text: {n_date_ok} dates, {n_time_ok} times, "
          f"{n_loc_ok} locations, {n_body_ok} bodies, "
          f"{n_zoom_ok} Zoom codes")

    def section(title, rows, red=False, brief=None):
        """Full listing when verbose (or red — always shown); otherwise
        just a one-line count."""
        if not rows:
            return
        if not (verbose or red):
            print(f"{brief or title}: {len(rows)}")
            return
        heading = f"{title}:"
        if red and sys.stdout.isatty():
            heading = "\033[31m" + heading + "\033[0m"
        print(f"\n{heading}")
        for row in rows:
            if isinstance(row, tuple):
                print(f"  {row[0]}\n      {row[1]}")
            else:
                print(f"  {row}")

    section("Doc-side year typos (filenames verified by weekday)", doc_typos,
            brief="Doc-side year typos")
    section("Stale printed weekdays (dates match their filenames — the "
            "documents' weekday words are wrong)", weekday_typos,
            brief="Stale printed weekdays")
    section("Previously verified by the clerk", notes)
    section("PDF parser grumbles (defects pypdf auto-repaired — harmless "
            "unless a file also fails checks)", grumbles,
            brief="PDF parser grumbles")
    section("No readable date in text (handwritten forms, terse minutes)",
            no_date, brief="No readable date in text")
    section("No readable time in text", no_time)
    section("Location not seen in text (often just implicit; "
            "informational)", no_loc, brief="Location not seen in text")

    section("MISMATCHES — the filename is probably wrong", mismatch,
            red=True)
    section("CHECK — needs a human look", check, red=True)

    code = 1 if (mismatch or check) else 0
    verdict = f"exit {code} — " + ("clean" if code == 0 else
                                   "needs attention")
    if sys.stdout.isatty():
        verdict = ("\033[32m" if code == 0 else "\033[31m") \
            + verdict + "\033[0m"
    print(f"\n{verdict}")
    return code


if __name__ == "__main__":
    sys.exit(main())
