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
  (the record afterward).
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

## Where things live

| What | Where |
| --- | --- |
| Documents (the archive itself) | `Records/` |
| Generator, taxonomy, clerk contact info, meeting-place addresses | `build.py` (`SECTIONS`, `KINDS`, `CONTACT`, `LOCATIONS` at the top) |
| All styling, screen and print | `style.css` |
| Deployment | `.github/workflows/deploy.yml` |

The generated site is one page: a filterable, searchable table of every
record, with a print stylesheet that produces a clean black-and-white
listing stamped with the date, active filters, and the clerk's contact
information.
