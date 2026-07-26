#!/usr/bin/env python3
"""Sort unsorted scans into the archive — the audit run in reverse.

Drop PDFs into Inbox/Unprocessed/ (gitignored; the Inbox itself holds
only subfolders — Unprocessed, Staged, Duplicates), then:

    python3 intake.py            STAGE: move each confidently-classified
                                 file into Inbox/Staged/ under its derived
                                 archive name plus a destination tag —
                                 "2026-01-14 1900 Town Hall [Finance
                                 Committee, Agendas].pdf" — an easy list
                                 to eyeball and correct by renaming
    python3 intake.py --apply    file everything in Staged/ where its
                                 [Board, Kind] tag says (hand-corrected
                                 names included), then run the audit
    python3 intake.py --selftest grade the classifier against every file
                                 already in Records/ (tuning aid)

For each PDF the classifier reads the text and infers what the filename
grammar needs: which body (via the audit's BODY_ALIASES vocabulary, with
extra weight near the top of page one where letterheads live), whether it
is a before-doc (agenda/warrant) or after-doc (minutes/results), the
meeting date, and — for before-docs — the time, location, and Zoom code.

For now the classifier files BOARD records only — town-meeting and
election documents are rarer, stranger (combined warrants, handwritten
tally sheets), and are simply held for manual filing.

Anything ambiguous in a destination-critical field (body, kind, date) is
HELD in Unprocessed/ with its analysis printed; nothing is ever guessed
silently. A missing time is omitted with a note — the clerk can add it.
An agenda with neither a location nor any Zoom presence is HELD: every
meeting notice must say where the meeting is. An agenda that mentions
Zoom without one readable meeting code is HELD too — a bare ', Zoom'
filename is almost never right, so the clerk files those by hand. So are
joint-meeting minutes (two CALL TO ORDERs): by convention the clerk
files a copy under each board.
A file that classifies cleanly but whose destination already exists (in
the archive, in Staged/, or earlier in the same batch) is a duplicate of
something already filed; it is set aside in Inbox/Duplicates/.

Shares audit.py's parsers and pypdf bootstrap; plain `python3 intake.py`
always works.
"""

from __future__ import annotations

import datetime
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit import (  # noqa: E402  (bootstraps pypdf on first import)
    MONTHS, ORDINAL_WORDS, RE_DATEBOX, RE_DAYOF, RE_ISO, RE_LONG, RE_LONG2,
    RE_NUM, RE_OCLOCK, RE_TIME, RE_TIMELABEL, WEEKDAYS, _HOUR_WORDS,
    _YEAR_WORDS, PdfReader, body_probes, location_probes,
    VERIFIED, parse_stem, raw_streams_text, zoom_codes_in)
from build import LOCATIONS, RECORDS_DIR, ROOT, SECTIONS  # noqa: E402

INBOX = ROOT / "Inbox"
UNPROCESSED = INBOX / "Unprocessed"
DUPLICATES = INBOX / "Duplicates"
STAGED = INBOX / "Staged"

# A staged filename: the derived archive name plus its destination tag,
# e.g. "2026-01-14 1900 Town Hall [Finance Committee, Agendas].pdf"
TAG_RE = re.compile(r"^(?P<stem>.+) \[(?P<body>.+), "
                    r"(?P<kind>Agendas|Minutes|Warrants|Results)\]$")

# Which kind folder a before/after document belongs to, per section.
KIND_FOR = {
    "Boards": {"before": "Agendas", "after": "Minutes"},
    "Town Meetings": {"before": "Warrants", "after": "Minutes"},
    "Elections": {"before": "Warrants", "after": "Results"},
}

# Role signals, scored over the whole text; the first-400-char title zone
# counts triple. Weights reflect how unambiguous each phrase is — a bare
# "minutes" also appears in agendas ("Accept Minutes").
BEFORE_SIGNALS = {"agenda": 3, "notice of meeting": 3, "meeting notice": 3,
                  "notice of public meeting": 3, "public meeting notice": 3,
                  "public meeting": 3, "posted": 2,
                  "warrant": 3, "notify and warn": 4,
                  "notice of public hearing": 3}
AFTER_SIGNALS = {"minutes of the meeting": 4, "meeting minutes": 4,
                 "record of proceedings": 4, "we certify": 4,
                 "total votes cast": 4, "was called to order": 3,
                 "called the meeting to order": 3, "called to order at": 3,
                 "seconded": 3,
                 "motion passed": 3, "members present": 3, "present:": 3,
                 "minutes": 1}


def score_signals(zone: str, low: str, signals: dict[str, int]) -> int:
    return sum(w * ((3 if s in zone else 0) + (1 if s in low else 0))
               for s, w in signals.items())


def infer_role(low: str) -> str | None:
    zone = low[:400]
    before = score_signals(zone, low, BEFORE_SIGNALS)
    after = score_signals(zone, low, AFTER_SIGNALS)
    if before == after:
        return None
    return "before" if before > after else "after"


# Subcommittees print their parent organization's letterhead; when a
# child-specific alias is present, the child wins over its parent.
PARENT = {"FHD Executive Committee": "FHD Board",
          "FHD Personnel Committee": "FHD Board",
          "HRS Finance Subcommittee": "HRS Committee",
          "HRS Policy Subcommittee": "HRS Committee"}


GUEST_RE = re.compile(r"(?:\bwith|\bw/)\s+(?:the\s+)?(?:westhampton\s+)?"
                      r"(?:[a-z,& ]{0,40}?\band\s+(?:the\s+)?)?$")


def _guest(low: str, pos: int) -> bool:
    """A body named after 'with' ('joint meeting with Selectboard and
    Finance Committee', 'in collaboration with the …') is the GUEST —
    this is not its meeting, so the mention does not count."""
    return bool(GUEST_RE.search(low[max(0, pos - 60):pos]))


def _squashed_find(low: str, probe: str) -> int | None:
    """Find probe with all spacing/punctuation ignored — scanners
    shred letterheads into 'SELE CT. B OARD'. Returns the position in
    the original text (guest mentions skipped), or None."""
    sq, idx = [], []
    for i, c in enumerate(low):
        if c.isalnum():
            sq.append(c)
            idx.append(i)
    sq = "".join(sq)
    want = re.sub(r"[^a-z0-9]", "", probe)
    i = sq.find(want)
    while i != -1:
        if not _guest(low, idx[i]):
            return idx[i]
        i = sq.find(want, i + 1)
    return None


def infer_body(low: str, sections_of: dict[str, str]) -> tuple[str | None, str]:
    """-> (body or None, why). A body named in the MEETING TITLE ('BOH
    meeting minutes', 'Minutes of the Meeting (FinCom)') outranks every
    vocabulary score — letterhead templates get reused across boards, but
    the title says whose meeting it was. Guest mentions (after 'with')
    never count."""
    zone = low[:300]
    scores, zone_hit, titled = {}, {}, set()
    for body in sections_of:
        positions = []
        for p in body_probes(body):
            pos = next((m.start() for m in
                        re.finditer(rf"\b{re.escape(p)}\b", low)
                        if not _guest(low, m.start())), None)
            if (pos is None or pos >= len(zone)) and len(p) >= 8:
                sq = _squashed_find(low, p)
                if sq is not None and (pos is None or sq < pos):
                    pos = sq
            if pos is not None:
                positions.append(pos)
        if positions:
            scores[body] = sum(3 if pos < len(zone) else 1
                               for pos in positions)
            zone_hit[body] = any(pos < len(zone) for pos in positions)
        for p in body_probes(body):
            esc = re.escape(p)
            for pat in (rf"\b({esc})\)?\s*(?:board\s+|committee\s+)?"
                        rf"(?:meeting|minutes|agenda)",
                        rf"(?:meeting|minutes|agenda)\s*\(?\s*({esc})\b"):
                m = re.search(pat, zone)
                if m and not _guest(zone, m.start(1)):
                    titled.add(body)
                    break
    for child, parent in PARENT.items():
        if child in scores and parent in scores:
            del scores[parent]
            titled.discard(parent)
    if not scores:
        return None, "no body vocabulary found in the text"
    titled &= set(scores)
    if len(titled) == 1:
        (body,) = titled
        return body, "named in the meeting title"
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    if len(ranked) > 1 and ranked[0][1] - ranked[1][1] < 2 \
            and not (zone_hit[ranked[0][0]] and not zone_hit[ranked[1][0]]):
        tie = ", ".join(b for b, _ in ranked[:3])
        return None, f"ambiguous between: {tie}"
    return ranked[0][0], f"score {ranked[0][1]}"


STAMP_RE = re.compile(r"post(?:ed)?|\brec\b|rec'?d|received|filed|approved",
                      re.I)


def _stamped_positions(text: str, positions: list[int],
                       reach: int = 90) -> set[int]:
    """Each stamp word ('POSTED:', 'RECEIVED', 'approved') claims the
    FIRST dated/timed thing that follows it, within reach — two-column
    headers scatter other text between the stamp and its value, but the
    meeting's own value comes later still."""
    claimed = set()
    for m in STAMP_RE.finditer(text):
        following = [pos for pos in positions
                     if 0 <= pos - m.end() <= reach]
        if following:
            claimed.add(min(following))
    return claimed


def dated_matches(text: str):
    """Every date mention with its position and any printed weekday."""
    hits: list[tuple[int, datetime.date, str | None]] = []

    def add(pos, y, m, d, wd=None):
        try:
            hits.append((pos, datetime.date(y, m, d),
                         wd.lower() if wd else None))
        except ValueError:
            pass

    for m in RE_LONG.finditer(text):
        key = m[2].lower().rstrip(".")
        add(m.start(), int(m[4]), MONTHS[key if key in MONTHS else key[:3]],
            int(m[3]), m[1])
    for m in RE_LONG2.finditer(text):
        add(m.start(), int(m[3]), MONTHS[m[2].lower()], int(m[1]))
    for m in RE_NUM.finditer(text):
        add(m.start(), int(m[3]) + (2000 if len(m[3]) == 2 else 0),
            int(m[1]), int(m[2]))
    for m in RE_ISO.finditer(text):
        add(m.start(), int(m[1]), int(m[2]), int(m[3]))
    for m in RE_DATEBOX.finditer(text):
        wd = None
        if m[1]:
            wd = next((w for w in WEEKDAYS
                       if w.startswith(m[1].lower())), None)
        add(m.start(), int(m[4]), MONTHS[m[2].lower()], int(m[3]), wd)
    for m in RE_DAYOF.finditer(text):
        day = int(m[1]) if m[1] else ORDINAL_WORDS[
            re.sub(r"\s+", " ", m[2].lower())]
        year = None
        if m[4]:
            year = int(m[4])
        elif m[5]:
            yw = re.sub(r"[ -]+", " ", m[5].lower())
            year = 2000 + _YEAR_WORDS[yw] if yw in _YEAR_WORDS else None
        if year:
            add(m.start(), year, MONTHS[m[3].lower()], day)
    return sorted(hits)


def pick_date(text: str,
              future_ok: bool = True) -> tuple[datetime.date | None, str]:
    """The meeting date = the first date mentioned that is not a
    posting/receipt stamp (headers lead; references follow). A printed
    weekday that contradicts the date usually means the author reused
    last year's header: when the weekday fits the adjacent year AND that
    moves the date toward today, correct the year (with a note);
    otherwise trust the printed date and just note the stale weekday.
    future_ok=False (after-docs) refuses to correct INTO the future —
    minutes cannot postdate today, so there the printed year outranks
    the printed weekday (stale weekdays are common; see the audit)."""
    hits = dated_matches(text)
    if not hits:
        return None, "no readable date", 0
    claimed = _stamped_positions(text, [pos for pos, _, _ in hits])
    live = [h for h in hits if h[0] not in claimed] or hits
    date_pos, date, _ = live[0]
    hits = live
    wd = next((w for _, d, w in hits if d == date and w), None)
    if wd and WEEKDAYS[date.weekday()] != wd:
        today = datetime.date.today()
        for shift in (1, -1):
            try:
                cand = date.replace(year=date.year + shift)
            except ValueError:
                continue
            if not future_ok and cand > today:
                continue
            if WEEKDAYS[cand.weekday()] == wd and \
                    abs(cand - today) < abs(date - today):
                return cand, (f"doc says {date.isoformat()} but its "
                              f"'{wd.capitalize()}' fits {cand.year} — "
                              f"year corrected"), date_pos
        return date, (f"printed weekday '{wd.capitalize()}' does "
                      f"not match {date.isoformat()}"), date_pos
    return date, "ok", date_pos


def first_time(text: str, after: int = 0) -> int | None:
    """The meeting time = the first time at or after the meeting date's
    position (headers read 'Monday, June 15, 2026, 7:00 PM' — posting
    stamps and their times come earlier)."""
    hits: list[tuple[int, int]] = []
    for m in RE_TIME.finditer(text):
        h, mnt = int(m[1]), int(m[2] or 0)
        if 1 <= h <= 12 and mnt < 60:
            pm = m[3].lower() == "p"
            hits.append((m.start(), ((h % 12) + (12 if pm else 0)) * 100 + mnt))
    for m in RE_OCLOCK.finditer(text):
        h = int(m[1]) if m[1] else _HOUR_WORDS[m[3].lower()]
        mnt = int(m[2] or 0)
        if 1 <= h <= 12 and mnt < 60:
            pm = m[4].lower() in ("afternoon", "evening")
            hits.append((m.start(), ((h % 12) + (12 if pm else 0)) * 100 + mnt))
    for m in RE_TIMELABEL.finditer(text):
        h, mnt = int(m[1]), int(m[2])
        if h <= 23 and mnt < 60:
            hits.append((m.start(), (h + 12 if 1 <= h <= 7 else h) * 100
                         + mnt))
    if not hits:
        return None
    later = [h for h in hits if h[0] >= after]
    return min(later or hits)[1]


def infer_location(low: str,
                   letterhead_ok: bool = False) -> tuple[str | None, str]:
    """Reverse alias lookup, confined to the header zone — body text
    mentions half the town's buildings. A venue phrase ('at the …',
    'in the …') outranks a bare mention, because letterheads carry
    building names too (Finance Committee's says TOWN HALL even when it
    meets elsewhere); then the earliest mention wins. At the same spot a
    longer probe beats its own substring ('the Town Hall Annex' is the
    Annex, not the Town Hall), and remaining ties (shared campus names)
    break toward the location with more of its own vocabulary elsewhere
    in the document."""
    zone = low[:700]
    letterhead = {"foothills", "hampshire regional",
                  "westhampton elementary", "westhampton elementary school",
                  "foothills health district office",
                  "45 main street", "williamsburg, ma 01096"}
    if letterhead_ok:
        letterhead = set()
    ranked = []  # (venue? 0 : 1, position, -probe-len, -own-vocab, name)
    for name in LOCATIONS:
        probes = ({"town hall"} if name == "Town Hall"
                  else location_probes(name))
        vocab = sum(1 for pr in probes if pr in low)
        for probe in probes:
            esc = re.escape(probe)
            m = re.search(r"\b(?:at|in)\s+(?:the\s+)?" + esc, zone)
            if m:
                # a venue phrase is a deliberate statement — letterheads
                # never say 'at the …', so no exclusion here
                ranked.append((0, m.start(), -len(probe), -vocab, name))
            elif probe not in letterhead:
                m = re.search(rf"\b{esc}\b", zone)
                if m:
                    ranked.append((1, m.start(), -len(probe), -vocab,
                                   name))
    if not ranked:
        # No venue named at all — but a Town Hall letterhead address means
        # the meeting is almost certainly there (clerk's rule of thumb).
        if "1 south road" in zone:
            return "Town Hall", "inferred from the 1 South Road letterhead"
        return None, "none"
    ranked.sort()
    top = ranked[0]
    rivals = {r[4] for r in ranked if r[:3] == top[:3]}
    if len(rivals) > 1 and len({r[3] for r in ranked
                                if r[:3] == top[:3]}) == 1:
        return None, "ambiguous: " + ", ".join(sorted(rivals))
    return top[4], "ok"


def joint_hosts(low: str, sections_of: dict[str, str]) -> list[str]:
    """Minutes of a joint session convene each body in turn ('CALL TO
    ORDER - Selectboard … CALL TO ORDER – Finance Committee'). Returns
    the bodies named directly after a call to order, in document order."""
    hosts = []
    for m in re.finditer(r"call(?:ed)?\s+to\s+order\s*[–—:\-]*\s*", low):
        seg = low[m.end():m.end() + 40]
        for body in sections_of:
            if any(seg.startswith(p) for p in body_probes(body)) \
                    and body not in hosts:
                hosts.append(body)
    return hosts


def classify(pdf: Path, sections_of: dict[str, str]):
    """-> (dest: Path | None, notes: list[str]); dest None means hold."""
    notes = []
    try:
        reader = PdfReader(str(pdf))
        text = re.sub(r"\s+", " ", " ".join(
            page.extract_text() or "" for page in reader.pages))
    except Exception as e:  # noqa: BLE001
        return None, [f"unreadable PDF: {e}"]
    low = text.lower()
    if len(low.strip()) < 40:
        return None, ["no text layer — needs OCR first"]

    body, why = infer_body(low, sections_of)
    if not body:
        return None, [f"body: {why}"]
    section = sections_of[body]

    role = infer_role(low)
    if not role:
        return None, [f"cannot tell agenda/warrant from minutes/results "
                      f"(body: {body})"]
    kind = KIND_FOR[section][role]

    if kind == "Minutes":
        hosts = joint_hosts(low, sections_of)
        if len(hosts) > 1:
            return None, ["joint meeting minutes — call "
                          + " and ".join(hosts) + " to order; the clerk "
                          "files a copy under each board by hand"]

    date, why, date_pos = pick_date(text, future_ok=(role == "before"))
    if not date:
        return None, [f"date: {why} (body: {body}, kind: {kind})"]
    if role == "after" and date > datetime.date.today():
        return None, [f"{kind.lower()} dated {date.isoformat()}, in the "
                      f"future — the year must be wrong"]
    if why != "ok":
        notes.append(why)

    stem = date.isoformat()
    if role == "after":
        # If this body met more than once that day (several timed
        # before-docs in the archive), the minutes must carry a time to
        # say which meeting they belong to — pair by the call-to-order
        # time, which lands within minutes of the scheduled start.
        before_dir = (RECORDS_DIR / section / body
                      / KIND_FOR[section]["before"])
        slots = sorted({p[1] for p in (parse_stem(f.stem) for f in
                                       before_dir.glob(
                                           f"{date.isoformat()} *.pdf"))
                        if p and p[1] is not None})
        if len(slots) > 1:
            started = first_time(text, after=date_pos)
            pick = next((t for t in slots if started is not None
                         and abs((t // 100) * 60 + t % 100
                                 - ((started // 100) * 60
                                    + started % 100)) <= 20), None)
            if pick is None:
                return None, [f"{body} met {len(slots)} times on "
                              f"{date.isoformat()} — cannot tell which "
                              f"meeting these minutes belong to"]
            stem += f" {pick:04d}"
            notes.append(f"{body} met {len(slots)} times that day — "
                         f"time added to pair with the {pick:04d} "
                         f"meeting")
    if role == "before":
        hhmm = first_time(text, after=date_pos)
        if hhmm is not None:
            stem += f" {hhmm:04d}"
        else:
            notes.append("no time found — omitted")
        loc, why = infer_location(low)
        if loc:
            stem += f" {loc}"
            if why != "ok":
                notes.append(f"location {loc} — {why}")
        elif why != "none":
            notes.append(f"location {why} — omitted")
        raw = raw_streams_text(pdf)
        url_codes = set(re.findall(r"zoom\.us/[js]/(\d{9,11})", low)) \
            | set(re.findall(r"zoom\.us/[js]/(\d{9,11})", raw))
        every = url_codes | zoom_codes_in(text, raw)
        # Zoom's dial-in lines list phone numbers (+13126266799) that
        # scan just like meeting codes — drop '+'-prefixed digit runs
        every = {c for c in every
                 if f"+{c}" not in low and f"+{c}" not in raw}
        # OCR sometimes truncates a URL: a code that is a prefix of a
        # longer candidate is the same code, mangled
        full = {c for c in every
                if not any(o != c and o.startswith(c) for o in every)}
        # a one-tap entry ('...,,86808148161#') states the meeting code
        # exactly — trust it over a misread spaced Meeting ID line
        onetap = {c for c in full
                  if re.search(rf"{c}\s?#", low) or f"{c}#" in raw}
        codes = (url_codes & full) or onetap or full
        remote = ""
        if len(codes) == 1:
            remote = f"Zoom {codes.pop()}"
        elif len(codes) > 1:
            return None, notes + ["several different Zoom codes — "
                                  + ", ".join(sorted(codes))]
        elif "zoom" in low:
            # a Zoom meeting whose code we cannot read is a filing the
            # clerk should make by hand — bare ', Zoom' is almost never
            # right
            return None, notes + ["Zoom meeting but no readable "
                                  "meeting code"]
        if remote:
            stem += (", " if loc else " ") + remote
        if not loc and not remote:
            # No Zoom in the doc, so a building named only in the
            # letterhead really is where they are meeting.
            loc, why = infer_location(low, letterhead_ok=True)
            if loc:
                stem += f" {loc}"
                notes.append(f"location {loc} — letterhead, but the doc "
                             f"has no Zoom, so the meeting is there")
            else:
                return None, notes + ["no location or Zoom found — an "
                                      "agenda must say where the meeting "
                                      "is" + (f" ({why})" if why != "none"
                                              else "")]
    return RECORDS_DIR / section / body / kind / f"{stem}.pdf", notes


def sections_map() -> dict[str, str]:
    # Boards only for now — see the module docstring
    return {body.name: "Boards"
            for body in (RECORDS_DIR / "Boards").iterdir()
            if body.is_dir()}


def selftest() -> int:
    """Grade the classifier against everything already filed. Files the
    clerk has hand-verified against the paper record (audit's VERIFIED
    list) are graded separately and shown dimmed — their names encode
    knowledge the text does not contain, so a disagreement there is
    expected, not a miss."""
    sections_of = sections_map()
    dim = sys.stdout.isatty()
    ok = wrong = held = expected = 0
    for pdf in sorted((RECORDS_DIR / "Boards").rglob("*.pdf")):
        dest, notes = classify(pdf, sections_of)
        rel = str(pdf.relative_to(RECORDS_DIR))
        if dest == pdf:
            ok += 1
        elif rel in VERIFIED:
            expected += 1
            tag = "hold" if dest is None else "diff"
            line = (f"  {tag}. {rel}\n"
                    f"        clerk-verified, disagreement expected: "
                    f"{VERIFIED[rel]}")
            print(f"\033[2m{line}\033[0m" if dim else line)
        elif dest is None:
            held += 1
            print(f"  HOLD  {rel}\n        {'; '.join(notes)}")
        else:
            wrong += 1
            print(f"  DIFF  {rel}\n"
                  f"     -> {dest.relative_to(RECORDS_DIR)}"
                  + (f"  ({'; '.join(notes)})" if notes else ""))
    total = ok + wrong + held + expected
    print(f"\nselftest: {ok}/{total} exact, {wrong} filed differently, "
          f"{held} would be held for a human"
          + (f", {expected} clerk-verified disagreements (expected)"
             if expected else ""))
    return 0


def set_aside(pdf: Path) -> None:
    DUPLICATES.mkdir(exist_ok=True)
    spot, n = DUPLICATES / pdf.name, 2
    while spot.exists():
        spot = DUPLICATES / f"{pdf.stem} ({n}){pdf.suffix}"
        n += 1
    shutil.move(str(pdf), str(spot))


def apply_staged() -> int:
    """File everything in Staged/ where its [Board, Kind] tag says —
    including any names the clerk hand-corrected after staging."""
    sections_of = sections_map()
    pdfs = sorted(STAGED.glob("*.pdf")) if STAGED.exists() else []
    if not pdfs:
        print(f"nothing staged ({STAGED.relative_to(ROOT)}/ is empty) — "
              f"run a plain intake pass first.")
        return 0
    filed, dupes, problems = 0, [], []
    for pdf in pdfs:
        m = TAG_RE.match(pdf.stem)
        if not m:
            problems.append((pdf, "no [Board, Kind] tag in the name"))
            continue
        if m["body"] not in sections_of:
            problems.append((pdf, f"unknown board '{m['body']}'"))
            continue
        dest = (RECORDS_DIR / sections_of[m["body"]] / m["body"]
                / m["kind"] / f"{m['stem']}.pdf")
        if dest.exists():
            dupes.append((pdf, f"already at {dest.relative_to(ROOT)}"))
            set_aside(pdf)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(pdf), str(dest))
        print(f"  {pdf.name}\n     -> {dest.relative_to(ROOT)}")
        filed += 1
    if dupes:
        print(f"\nduplicates ({len(dupes)}) — moved to "
              f"{DUPLICATES.relative_to(ROOT)}/:")
        for pdf, why in dupes:
            print(f"  {pdf.name}\n        {why}")
    if problems:
        heading = (f"left in {STAGED.relative_to(ROOT)}/ "
                   f"({len(problems)}) — needs a human:")
        if sys.stdout.isatty():
            heading = "\033[31m" + heading + "\033[0m"
        print(f"\n{heading}")
        for pdf, why in problems:
            print(f"  {pdf.name}\n        {why}")
    loose = ([p for p in UNPROCESSED.iterdir()
              if p.suffix.lower() == ".pdf"]
             if UNPROCESSED.exists() else [])
    if loose:
        print(f"\n{len(loose)} PDF(s) still in "
              f"{UNPROCESSED.relative_to(ROOT)}/ (held or new) — run a "
              f"plain intake pass to stage them.")
    if not filed:
        return 0
    print(f"\nfiled {filed} document(s); running the audit …\n",
          flush=True)
    return subprocess.run(
        [sys.executable, str(ROOT / "audit.py")]).returncode


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    if "--apply" in sys.argv:
        return apply_staged()

    UNPROCESSED.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(p for p in UNPROCESSED.iterdir()
                  if p.suffix.lower() == ".pdf")
    if not pdfs:
        print(f"Nothing to process ({UNPROCESSED.relative_to(ROOT)}/ is "
              f"empty) — drop scans there and rerun.")
        return 0

    sections_of = sections_map()
    staged, dupes, held = [], [], []
    proposed = {}
    for pdf in pdfs:
        dest, notes = classify(pdf, sections_of)
        if dest is None:
            held.append((pdf, notes))
            continue
        name = (f"{dest.stem} [{dest.parent.parent.name}, "
                f"{dest.parent.name}]{dest.suffix}")
        if dest.exists():
            dupes.append((pdf, f"already at {dest.relative_to(ROOT)}"))
            set_aside(pdf)
        elif dest in proposed:
            dupes.append((pdf, f"same destination as {proposed[dest]} "
                               f"in this batch"))
            set_aside(pdf)
        elif (STAGED / name).exists():
            dupes.append((pdf, f"already staged as {name}"))
            set_aside(pdf)
        else:
            proposed[dest] = pdf.name
            STAGED.mkdir(exist_ok=True)
            shutil.move(str(pdf), str(STAGED / name))
            staged.append((pdf, name, notes))

    for pdf, name, notes in staged:
        print(f"  {pdf.name}\n     -> {name}")
        for note in notes:
            print(f"        note: {note}")
    if dupes:
        print(f"\nduplicates ({len(dupes)}) — moved to "
              f"{DUPLICATES.relative_to(ROOT)}/:")
        for pdf, why in dupes:
            print(f"  {pdf.name}\n        {why}")
    if held:
        heading = (f"held in {UNPROCESSED.relative_to(ROOT)}/ "
                   f"({len(held)}) — needs a human:")
        if sys.stdout.isatty():
            heading = "\033[31m" + heading + "\033[0m"
        print(f"\n{heading}")
        for pdf, notes in held:
            print(f"  {pdf.name}\n        {'; '.join(notes)}")
    if staged:
        print(f"\nstaged {len(staged)} document(s) in "
              f"{STAGED.relative_to(ROOT)}/ — each name ends with its "
              f"[Board, Kind] destination; check the list, correct "
              f"anything wrong by renaming, then run --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
