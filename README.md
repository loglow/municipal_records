# Westhampton public records

The public records archive of the Town Clerk of Westhampton, Massachusetts.
The repository itself is the archive: the documents live in `Records/`, and a
small generator builds a single-page website for searching and reading them.

## Publishing a document

1. Drop the PDF into the right folder (see filing rules below).
2. Commit and push.

That's it — the site rebuilds and deploys automatically via GitHub Actions.

## Filing rules

One universal pattern:

```
Records/<Section>/<Body>/<Kind>/YYYY-MM-DD.pdf

Records/Boards/Selectboard/Agendas/2026-07-13.pdf
Records/Boards/Selectboard/Agendas/2026-07-13 1900 Town Hall.pdf
Records/Town Meetings/Annual Town Meeting/Warrants/2026-05-09.pdf
Records/Elections/State Election/Results/2026-11-03.pdf
```

- **Folder names are display names.** A new board or election type is just a
  new folder — no code changes.
- **Filenames start with the date** of the meeting or event, `YYYY-MM-DD.pdf`.
  Agendas and warrants may also carry the meeting time and place, either or
  both: `YYYY-MM-DD HHMM Location.pdf` (24-hour time; the location may
  contain spaces). A comma separates the location from a remote-meeting
  part written as a provider name followed by a meeting code, which
  appears as a pill linking to the meeting: hybrid meetings look like
  `2026-01-12 1800 Town Hall, Zoom 82383447080.pdf`, remote-only meetings
  like `2026-01-12 1800 Zoom 82383447080.pdf`. A provider name alone (no
  meeting code) shows as the same pill, gray and unlinked.
  Known providers (and their link formats) are the `REMOTE_PROVIDERS`
  table in `build.py`. Locations listed in the `LOCATIONS` table in
  `build.py` are expandable on the site to show their address. Anything
  the filename doesn't carry shows a dash.
- Kinds are `Agendas`/`Warrants` (posted beforehand) and `Minutes`/`Results`
  (the record afterward). Minutes and results are normally date-only —
  each row's time and place come from its agenda. But when one body meets
  twice on the same day, the agendas' distinct times make two records,
  and each minutes file must then carry its meeting's time too
  (`2026-01-28 1800.pdf`) so it pairs with the right one.
- **Never rename a published folder.** Document URLs are the folder paths,
  and published links should keep working indefinitely. Decide names before
  pushing.

## Building and previewing locally

```
python3 build.py
```

Pure Python standard library, no dependencies. It scans `Records/`, writes
the site into `site/` (gitignored, disposable), and prints a report —
including warnings for misfiled documents, malformed dates, unrecognized
folder names, and PDFs that appear to have no text layer (candidates for
OCR). Documents are never copied; `site/` links to them in place through a
symlink.

To preview, point any static file server at `site/`, e.g.:

```
python3 -m http.server 8000 -d site
```

## Auditing new scans

`audit.py` cross-checks every filename against the document's own text —
the date (printed weekday names arbitrate year typos), the meeting time,
the location (via its aliases and address), the body it is filed under,
and the Zoom meeting code. It also flags two files filed for the same
meeting (same folder, same date) — build.py would keep one and ignore
the rest.
Run it after adding a batch of scans:

```
python3 audit.py
```

Its one dependency (pypdf) manages itself: on first run the script
creates a private venv at `.venv-audit/` beside it (gitignored), installs
pypdf into it, and re-runs inside it — nothing touches system Python.
It exits nonzero when something needs attention. Findings that have been
hand-checked against the paper record are listed in `VERIFIED` at the top
of the script so they are reported as reminders instead of re-flagged.

## Filing new scans

Drop unsorted PDFs into `Inbox/Unprocessed/` (gitignored) and run:

```
python3 intake.py
```

It reads each document, derives its archive name — body, agenda/minutes,
date, time, location, Zoom code — and *stages* it: the file moves into
`Inbox/Staged/` renamed to its derived name plus a destination tag,
e.g. `2026-01-14 1900 Town Hall [Finance Committee, Agendas].pdf`. Go
down that list, correct any name by editing it, then:

```
python3 intake.py --apply
```

files everything in `Staged/` where its `[Board, Kind]` tag says
(including your corrections) and runs the audit over the result.
Anything intake cannot classify with confidence stays in `Unprocessed/`
with its analysis printed; it never guesses silently. A document whose
destination is already occupied is set aside in `Inbox/Duplicates/`. (`--selftest` grades the classifier against everything
already filed; files in audit's `VERIFIED` list are graded separately —
their names encode clerk knowledge the text doesn't contain, so those
disagreements are expected.) Boards only for now; town-meeting and election documents
are held for manual filing.

## Where things live

| What | Where |
| --- | --- |
| Documents (the archive itself) | `Records/` |
| Generator, taxonomy, clerk contact info, meeting-place addresses | `build.py` (`SECTIONS`, `KINDS`, `CONTACT`, `LOCATIONS` at the top) |
| All styling, screen and print | `style.css` |
| Filename-vs-document audit | `audit.py` (`VERIFIED` list at the top) |
| Deployment | `.github/workflows/deploy.yml` |

The generated site is one page with two views of the same records — a
filterable, searchable table and a month-grid calendar (the View toggle
in the header switches; the filters govern both). The print stylesheet
always produces the table: a clean black-and-white listing stamped with
the date, active filters, and the clerk's contact information.
