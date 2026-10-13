# Activities and timelines

**Built in Phase 4.** Code: [`backend/arkray/activities/`](../backend/arkray/activities/) and
[`frontend/src/features/activities/`](../frontend/src/features/activities/). Decisions:
[ADR-0009](adr/0009-unified-activity-model.md) (one table), [ADR-0020](adr/0020-activity-integrity.md)
(lead-bound, ownership, composite keys), [ADR-0021](adr/0021-materialized-timeline.md)
(the timeline) and [ADR-0027](adr/0027-leads-removed-from-the-ui.md) (Leads removed from the
UI: tasks and meetings are created from an opportunity; its lead is shown as the customer).

Phase 4 supports exactly three types: **task**, **meeting** and **note**. Call, email and
WhatsApp are prepared for (see [Adding a type](#adding-a-type)), not built.

## Model

```
Lead 1 ── * Activity * ── 0..1 Opportunity (of the same lead)
Lead 1 ── * TimelineEntry ── 0..1 Opportunity, 0..1 Activity     (append-only)
```

One table, `activities_activity`, with a `type` discriminator; which columns a type uses and
which statuses it may have are CHECK constraints (the matrix in
[database.md](database.md#activities_activity-built-phase-4)):

| Column | task | meeting | note |
|---|---|---|---|
| `title` (subject, ≤ 200) | required | required | empty |
| `description` (≤ 10,000) | optional description | optional agenda | **required**: the note |
| `status` | `open` → `completed` / `cancelled` | `scheduled` → `completed` / `cancelled` | none (NULL) |
| `priority` | `low` / `normal` (default) / `high` | — | — |
| `due_at` | optional (timestamp) | — | — |
| `starts_at`, `ends_at` | — | required; end after start; ≤ 24 h | — |
| `location`, `meeting_url` | — | optional; the link `https://` only, no credentials | — |
| `completed_at` + `completed_by` | set exactly while completed | same | never |
| `cancelled_at` + `cancelled_by` | set exactly while cancelled | same | never |

Common: `lead` (required, fixed), `opportunity` (optional, fixed, must be the lead's),
`owner` (the authorization key), `created_by` (the author; never changes), `archived_at`,
`version`, `created_at`, `updated_at`. Generated (stored): `current_owner_id` (the owner
while the activity is current work, else NULL), `schedule_sort` (a task's due time with "no
due date" as 9999-12-31, a meeting's start, a note's creation; NOT NULL, the one "when" for
sorting and date filters), `opportunity_key` (the opportunity, with "none" as a value, for
the timeline's composite key) and `created_sort` (a copy of `created_at` for the
organisation-wide sort only; see [Performance](#performance)).

Every activity is about a lead: from the lead's page, or from one of its opportunities
(the lead is then implied). Links never change; to fix a wrong link, archive and recreate.
Since ADR-0027 the UI has no lead pages: it creates activities about an opportunity only,
and shows the lead as the opportunity's customer; the API still accepts a lead alone.

Statuses are stable keys; labels are the UI's. Due dates are timestamps: the form asks for
a date and a time (pre-filled with 18:00, the end of the working day, in India time).

## Tasks

- Created **open**; priority `normal` unless chosen; due time optional.
- **Overdue** = open and `due_at` before now. Never stored: computed when read
  (`is_overdue` in every response, the `overdue=true` filter, the summary), because it
  changes by time passing alone. An undated task is never overdue.
- **Complete** (`POST …/complete {version}`): status `completed`, `completed_at` = now,
  `completed_by` = the actor (an admin completing Rahul's task is recorded as the admin; the
  task stays Rahul's). A task completion is not a customer contact.
- **Double completion** is a **no-op success** (200, unchanged, no audit or timeline entry,
  no version check): retries and double clicks are harmless, and the result is what both
  wanted. (Phase 3 precedent: moving to the current stage.)
- **Cancel** (`…/cancel`): status `cancelled` with who and when; cancelling twice is a no-op.
  Completed and cancelled exclude each other (422 "Reopen it first").
- **Reopen** (`…/reopen`): authorised users may reopen a completed or cancelled task. It
  returns to `open`, the completion or cancellation is cleared on the task, and the earlier
  completion stays in the timeline and the audit trail (history is never rewritten). Open
  work belongs to the lead's current owner (see [Ownership](#ownership)).
- **Edit** (`PATCH {version, …}`): subject, description, priority, due time; only while
  open. Completed and cancelled work is history (422 "Reopen it to make changes").

## Meetings

- Created **scheduled**, with start and end (end after start, at most 24 hours; refused:
  naive timestamps, malformed offsets, years outside 2000–2099). Stored in UTC, shown in
  India time.
- **Complete** records that the meeting took place, and is a customer interaction: the
  lead's last contact advances to the meeting's start (below). It is allowed only once the
  meeting has started (422 "This meeting hasn't started yet. Change its time…"), so a
  future meeting can never count as contact. The API says so in advance (`completable`).
- **Awaiting outcome**: a scheduled meeting whose end has passed (`is_overdue` for
  meetings): shown so it gets completed or cancelled.
- **Cancel**, **reopen** (back to scheduled; a cancelled meeting can be rescheduled by
  reopening and editing its times) and **edit** (only while scheduled) as for tasks. Changing
  a scheduled meeting's start or end is recorded on the timeline as *rescheduled*.
- No video-conferencing or calendar integration.

## Notes

- A note is its text (required) about a lead or one of its opportunities. No title, status,
  due date or times (CHECK-enforced). There is no `Lead.notes` or `Opportunity.notes`
  column: notes live in the activity and timeline system.
- Adding one is two steps on the opportunity page (its Notes tab): type, **Save note**.
- **Author vs owner**: `created_by` is the author, forever (an admin's note in Rahul's
  workspace says *written by the admin*). The note's owner is the lead's owner: a note is
  the lead's context and follows it on reassignment, so the new owner can read it, while the
  author shown never changes.
- **The author edits a note's text, and so may an administrator working in someone's
  workspace** (`crm.manage_any`; others get 403). The note then records **who edited it and
  when** (`edited_by`, `edited_at`) and shows "Edited by Anita Rao", so nobody's words are
  presented as someone else's; the audit has `note.updated` with the actor. Previous text is
  not kept (privacy-minimal, as audit never stores text). Anyone who may write in the
  workspace may **archive** a note (written by mistake, on the wrong lead).
- The deal page's **Notes** tab (`GET …/opportunities/{id}/notes`): whole text, author,
  edits, files and whether the caller may change each note; *+ Add note* with files.
- Note text is never logged, put in audit metadata, timeline data, exception messages,
  analytics or domain events. Lists and timelines carry a 240-character preview only.
  Embeddings for Ask Arkray are Phase 8 (the domain events below are the extension point).

## Attachments

Files on notes ([ADR-0026](adr/0026-user-pipelines-support-sessions-attachments.md);
`attachments.py`, `storage.py`).

- **Allowed types** (`ATTACHMENT_ALLOWED_EXTENSIONS`, from a catalog the server recognises
  by content): PDF, PNG, JPEG, WebP, DOCX, XLSX, CSV, TXT by default (GIF and PPTX can be
  enabled). A file must be what its extension says (magic bytes; OOXML packages with their
  main part and **no active content**: no macro project, OLE object (`*.bin` parts other
  than printer settings) or ActiveX control under any part name. Every other part is judged by
  what it holds, whatever it is called and wherever it sits (Office activates an embedded
  package through a `package` relationship to any part): an OLE compound file is refused; an
  OOXML package (a worksheet, document or deck embedded as an object) is opened and checked one
  level deep with these same rules and the content checks below (a workbook's formulas always; a
  document's Word fields when its `[Content_Types].xml` declares WordprocessingML parts or it
  holds `word/document.xml`; whatever its main part is called: review round 2, PARSERS2-4, they
  ran only when the main part was `xl/workbook.xml` or `word/document.xml`), refused if it
  carries active content; a package under a
  picture's name, a package inside a package, or under `/embeddings/` anything named other than
  a picture or `.docx/.xlsx/.pptx` (a macro-enabled `.docm/.xlsm/.pptm`, a renamed executable)
  is refused (adversarial review of 73ff1e3, L7, R159, and its review round 1: an embedded
  `.docx` could load a remote template or carry macros, and the embedded package itself was
  never inspected before). Embedded packages have their own byte budget: one is held in memory
  while it is checked, at most 16 MiB of it, at most 32 MiB for all of an upload's (an ordinary
  proposal with two 2.2 MB workbooks, or one of 4.5 MB, passes); past it, or past the 10,000
  entries an upload and its embedded packages may hold in all, the file is "too large to check",
  not a type mismatch. A package's end records (ZIP64 ones too, found as `zipfile` finds them)
  are read before its directory: a package, outer or embedded, that declares more entries than
  are left, or a directory larger than 128 bytes per entry left or 2 MiB, is "too large to check"
  before a record is parsed (review round 2, PARSERS2-3: `zipfile` parses every record before the
  entries can be counted, and 357,000 records in a 16 MiB embedded package cost 2.2 s and 149
  MiB from a 50 KB upload; now 0.02 s, the embedded package's own bytes the largest memory).
  Then its records are walked as `zipfile` will walk them, and a package with more records than
  entries left, or a record whose name, extra field or comment is over 1 KiB (Office writes names
  under 200 bytes and extra fields of at most 36), is "too large to check" too; the directory's
  bytes are paid for from the work pool (review round 4, PARSERS4-1: `zipfile` parses each
  record with its name and extra field, Python 3.12 decodes a 64 KiB extra field in 16 ms, and
  names of 65,000 `%` cost every pass that decodes them; a 20-55 KB upload took 3.6-4.5 s until
  the CPU deadline, now 0.00-0.02 s). No macro, Excel 4.0 macro sheet, OLE, ActiveX,
  external-link, data-connection or query-table content type declared, and no relationship that is an
  OLE object, a template, an ActiveX control, a macro project or an Excel 4.0 (XLM) macro
  sheet (international ones too; R132), a subdocument, a frame, a link to another workbook,
  a data connection or a query table, or that points outside the package (anything but a hyperlink, by its exact
  Transitional or Strict Type URI, with `TargetMode` other than `Internal`, or a URL/UNC
  target); no document Word imports (altChunk: an `aFChunk` relationship, an RTF, MHT or HTML
  content type or part name) and no Office add-in (web extensions, task panes):
  `[Content_Types].xml` and every `.rels` part are parsed as XML (defusedxml: no DTD or
  entities; UTF-8, or UTF-16 with its byte-order mark, and a declared encoding that agrees),
  never searched as bytes, and a part that doesn't parse refuses the file (deep security
  audit M-3 and its review). They are streamed, each element looked at and forgotten, at most
  20,000 elements, 1,000 attributes on one and 1,000 levels each (then "too large to check"), and
  what the content types declare is read once a package and reused by every check (review round
  3, PARSERS3-2: both were built into trees and the content types parsed again by each check, so
  a 2 KB upload of four 1 MiB element-dense copies cost 4-6 s and 75-128 MiB; now 0.03 s and 2
  MiB). A part named twice (letter case ignored, percent-decoded, as OPC compares part names)
  refuses the package, as Office does. The zip directory, those small XML parts and the first bytes of
  every other part are read, bounded (1 MiB each, 4 MiB in all, then "too large to check"); a
  member compressed with anything but deflate or stored is
  refused unread (adversarial review of 73ff1e3, found with L7, R167: `zipfile` decompresses
  such a member whole on the first read whatever length is asked, so a tiny member
  understating its size could expand to gigabytes);
  text is UTF-8 without NUL bytes). Everything else is refused:
  executables, scripts (`.exe .dll .bat .cmd .ps1 .sh .js`), HTML, SVG, archives, macro documents, double extensions (`invoice.pdf.exe`).
- **Word fields and CSV formulas** (R132): a Word document is refused when a **field** would
  start a program or fetch content from elsewhere once it updates: `DDE` and `DDEAUTO` always;
  `INCLUDETEXT`, `INCLUDE`, `INCLUDEPICTURE`, `IMPORT` or `LINK` with any argument naming
  another place (a URL of any scheme, `file:` included, or a UNC path, `\\host` or `//host`; a
  drive, root or relative path passes: it names the reader's own disk, and ordinary documents
  carry them, such as an Excel range pasted as a link); `DATABASE` always; and a field that
  can't be judged without evaluating the document: a name that isn't a literal word (a nested
  field, a quoted string, a symbol: the 2017 DDE obfuscation spelled `DDEAUTO` with nested
  `QUOTE` fields), or a fetching field whose argument isn't one (a nested `MERGEFIELD`) or
  whose code is over 64 Ki characters. Every story is read (body, headers, footers, footnotes,
  endnotes, comments, text boxes, building blocks: every `*.xml` part and every part
  `[Content_Types].xml` declares WordprocessingML, so a renamed part is read too), as Word
  assembles a field: its code split over any number of runs (run text inside a code counts; the
  blanks Word trims around a run without `xml:space="preserve"` are trimmed), `w:fldSimple`,
  fields nested in codes and results, and every reading of tracked changes (as recorded, all
  accepted, all rejected). A tab, a line break or a paragraph mark inside a code is read both
  as a blank and as nothing, and the code is also read bare (blanks, breaks and nested parts
  removed): one that starts `DDE`, `DATABASE`, `INCLUDE`, `IMPORT` or `LINK` that way is that
  field, however its letters are separated (independent review: `DDEAUTO`, a tab, then the
  program passed when breaks only joined). Content Word may or may not read must not cut across
  a field: a code that runs into an `mc:AlternateContent` branch, a field begun or ended across
  one, field content in an element of a namespace declared ignorable, or over 64 nested fields
  refuse the document. Each part is decoded as the relationship parts are (UTF-8, or UTF-16
  with its byte-order mark; a declared encoding that agrees; no NUL; one that can't be refuses
  the file); one that mentions field markup (`fldChar`, `fldSimple`, `instrText`, or `<!`:
  element names are never escaped) is then parsed with defusedxml's expat parser, streamed (no
  DTD or entities). **Bounds** per document, past which it is refused ("This document is too
  large to check …"): 64 MiB of decompressed story XML; 1 million units of parsing work (64
  bytes parsed are 1, review round 2; a member opened 16 and a parser 48, review round 3; an
  element is 1 plus 1 per attribute; a namespace declaration, a comment, an `mc:Ignorable`
  prefix 1; field content 3 per reading; judging a code 1 per 8 of its characters, per reading:
  1,000 `INCLUDETEXT` codes of 32,000 arguments took 71 s before, independent review round 2);
  and, in a part, nothing expat would hold whole over 1 MiB (a start tag, an attribute value, a
  comment), at most 1,000 attributes on an element, 1,000 levels of nesting, 32 nested
  `mc:Ignorable` scopes that add a namespace, and 10,000 distinct names (expat keeps an entry
  for each; independent review P1-1 and P1-2: 30,000 nested `mc:Ignorable` cost 45 s, one start
  tag of a million attributes 140 MB). These units come from the upload's one work pool (see
  Workbooks, below). A real document with a field in every paragraph reaches the work budget at
  about 10.6 MiB of XML (some 500 pages; about 15 MiB before review round 2 charged the bytes
  parsed), in about 0.6 s. Measured before review round 2 (i7-13700HX laptop; three to four times longer
  once it throttles under sustained load): parsing costs under a microsecond per unit of a real
  document, and no shape of part costs more per unit (empty elements, a zip bomb's shape: 0.4
  of it; tags of 900 attributes: 0.15; long fetching codes: 0.3-0.4; 9,000 distinct tags
  cycling: 0.8), so a document costs at most about a second (measured worst: 0.7-0.95 s, a 63
  MiB document read whole and parsed to the budget) of CPU in the upload request (a 64 KB zip
  bomb holding 63 MiB of XML and one field: about 0.6 s, then refused; without a field it is
  only read: 0.25 s); the largest traced memory any shape needed was 3.2 MiB (one 5 MiB start
  tag, refused once 1 MiB of it was still unparsed). Ordinary fields pass: `PAGE`, `NUMPAGES`,
  `TOC`, `PAGEREF`, `REF`, `HYPERLINK` (followed only on a click), `MERGEFIELD`, `DATE`,
  `FORMTEXT`, `SEQ`, `IF`, formulas, citations. Known false positives, accepted: a picture
  pasted from a web page that Word keeps as `INCLUDEPICTURE "https://…"` (Word for Mac
  especially), mail-merge pictures (`INCLUDEPICTURE` with a nested `MERGEFIELD`), files linked
  from a network share, any field whose name starts `DDE`; the fix is to select all and press
  Ctrl+Shift+F9 (fields become text). **Workbooks**: Excel saves a DDE formula (`=cmd|' /C
  calc'!A0`) as an external-link part holding a `ddeLink`, refused already by its part name,
  type and relationship. Cell formulas themselves are also read now (adversarial review of
  73ff1e3, L8, R160, and its review round 1): every part `[Content_Types].xml` declares as a
  worksheet, table, dialog sheet, chart sheet, chart, pivot cache or pivot table, or the
  workbook (a template's too: review round 2) — the parts Excel loads and evaluates, bound by
  their declared type (an Override or a Default), not their path — is read, and a formula that
  fetches content or calls out is
  refused: in a cell's `<f>` (shared and array formulas too), a defined name, a table's
  calculated-column or totals-row formula, a conditional format's or data validation's
  formula, a pivot cache's calculated field; with any namespace prefix on the element
  (`<x:f>`); `WEBSERVICE`, the modern `IMAGE`, `RTD`, `CALL`, `REGISTER`/`REGISTER.ID`, `EXEC`,
  and the Google-Sheets `IMPORTDATA`/`XML`/`HTML`/`FEED`/`RANGE`, in any letter case, behind a
  `_xlfn.`/`_xlws.` prefix, a character reference, a comment, CDATA or a processing
  instruction, called (blanks allowed before the bracket) or passed by name (review round 2,
  PARSERS2-6: an `_xleta.` prefix, `MAP(A1:A3,WEBSERVICE)`, or a defined name that is only the
  function, `Fetch` = `_xleta.WEBSERVICE` used as `=Fetch(url)`); `WEBSERVICE`, `RTD`, `CALL`,
  `EXEC`, `REGISTER.ID` and the `IMPORT` functions also wherever they stand as a value that can
  then be called (review round 3, PARSERS3-3: after `(`, an operator or `@`, or at the start, and
  before `)`, `,` or `;`: `MAP(WEBSERVICE,A1:A2)`, `(WEBSERVICE)`, `IF(TRUE,(RTD),0)`; in a
  workbook also before the formula's end, so a defined name `+WEBSERVICE`), but not `IMAGE` or
  `REGISTER`, which are plausible range names (`=SUM(Register)`, `=Image*2` pass: an owner
  decision); outside string literals, quoted sheet or workbook names and structured references
  (review round 2, PARSERS2-5: `SUM('Purchase Register (2024-25)'!B2:B500)` and
  `Calls[[#This Row],[Call (min)]]` were refused) ("This file has a cell formula that fetches
  content from elsewhere or calls out …"; a name before `!` or `[` is a sheet or table).
  `HYPERLINK` is kept: it
  fetches nothing until someone clicks it (the one deliberate exception to "nothing fetched
  from elsewhere", in documents, workbooks and CSVs alike). A part is parsed only when its text
  could hold a call at all (a fetching name followed, blanks aside, by `(`, `,`, `;` or `)`, an
  `_xleta.` prefix, `&#`, `<!`, or `<?` after the declaration: plain byte searches, each
  character one byte, letter case folded), so a sheet of numbers or of ordinary formulas is only
  read: a workbook of 400,000 numeric cells and a `SUM` passes in about 0.3 s. **The upload's
  work pool** (review round 2, PARSERS2-2: a 70-260 KB upload cost 9-13 s of CPU by spending
  each check's budget in turn at its most expensive unit): everything one upload's check does,
  its embedded packages' included, is paid for from 1 million units, a unit about a microsecond
  at the slowest shape measured (0.1-1.0 µs on the development laptop, measured again in review
  round 3): 160 decompressed bytes read, 64 bytes parsed, an element parsed 1 plus 1 per
  attribute, a parser 48, a member opened 16, an entry 4, an embedded package 96, a formula
  judged 4 plus 1 per 8 characters plus 2 per fetching name in it; character data reaches Python
  only inside a formula element, and judging a formula is linear in its length (review round 3,
  PARSERS3-1: the test for a defined name that is only a function copied the text before every
  fetching name, so 35 formulas of 64K characters in a 2-7 KB upload, a sheet, an embedded
  workbook or a pivot cache, cost 17-31 s and passed; now 0.5-0.8 s). The scan also keeps its
  own caps (review round 1: it shared the Word fields' budget and refused large ordinary
  workbooks): 256 MiB of decompressed XML read and 1 million units. Past any of them the file is
  "too large to check": every adversarial shape the reviews found (a sheet parsed to the budget,
  1.9 million spaced brackets, the worst document with the worst workbook embedded, 10,000 tiny
  members each opened and parsed, element-dense content types) now costs 0.03-0.9 s of CPU and
  under 25 MiB of traced memory. **The CPU deadline** (review round 3, structural: each round
  found a path the units did not price): every check of one upload, packages and CSVs alike, also
  runs against 5 s of its thread's CPU (`UPLOAD_CPU_SECONDS`), looked at once a member, a chunk
  read or parsed, a formula or field code judged, an embedded package and a CSV cell judged;
  past it the upload is "too large to check". The units stay the primary bound, the same answer
  on any machine; the deadline is met only by a path they fail to price, or on a core several
  times slower than the development laptop's (ordinary uploads cost a fraction of a second
  there). DDE syntax written inside a cell's formula is still not judged (R132). A **CSV** is
  refused when a cell Excel would evaluate (after blanks, tabs or line breaks it starts with
  `=`, `+`, `-` or `@`) has DDE's shape — a `|` outside its string literals and quoted names,
  with a `!` after it ("This file has a cell that Excel would run as a command …") — or calls
  one of the same fetching functions the way Excel calls one: the name right before its `(`,
  no blank between, where an operand can start, or passed by name as above (`_xleta.`, an
  argument, a value-only name inside a call; a CSV has no defined names, so "- call, then
  email" is a note), outside string literals, quoted names and structured references ("This file has a
  cell formula that fetches content …"; `HYPERLINK` kept; review round 1: "-Call (follow-up)
  with lab manager" or "+Image (brochure) attached" are notes, not calls). A quoted formula cell
  with text after its closing quote (malformed CSV that joins the two, so a name could be split
  by the quote) is refused as calling out. Only a file whose bytes, quotes and blanks removed,
  hold a fetching name followed by `(`, `,`, `;` or `)` (or `_xleta.`) is read cell by cell for
  this (plain byte searches: a 10 MB CSV of ordinary formulas costs under 0.1 s); then the
  fields holding such a name are found in bytes (one a character, letter case folded, kept as
  the text arrives: review round 2, PARSERS2-1, re-lowering the buffer cost 3-8 s on accented or
  Hindi text) and judged, within a counted budget of 1 million units: 2 a field that can't be a
  formula (its first character, or the first after an opening quote, isn't `=` `+` `-` `@`:
  review round 2, PARSERS2-7, a call log with "Call(s)" on every row ran out at 20,000 rows; a
  10 MB one passes now), 12 a formula field plus 1 per 8 characters plus 2 per fetching name in
  it; past it, "too large to check". Every 10 MB shape tried, adversarial ones included, costs
  0.2-1.1 s of CPU on a loaded laptop (an accented export with one "Image(s)" note: 0.3 s, 2.9 s
  before; 125,000-character Hindi fields: 0.5-0.9 s, 5.4 s before). Its fields are read as
  Python's csv module reads them (a quoted field runs to its closing quote, `""` inside is a
  quote, text after the closing quote joins the field, and a formula's string literal or quoted
  name open at that quote goes on in it: a differential fuzz against the csv module, 4
  million inputs, finds no DDE cell it misses), separated by a comma, a semicolon (Excel's
  separator in many regions), a tab and whatever a first `sep=` line names (a separator the
  file doesn't hold: whole lines, read once), by regular expressions over a stream: every field
  passes through C, memory holds a chunk and a field's first 128 Ki characters (Excel holds
  32,767 in a cell and 8,192 in a formula; for the DDE check a longer field that starts like a
  formula, or is blanks that far, is refused; for the fetching check it is judged on what was
  read and skipped, so a long ordinary `=SUM` line passes). Only a file holding a `|` and a `!`
  is read for DDE that way. A 10 MB
  CSV costs 0.5-0.7 s at full clock with a `|` in every row and every separator present
  (independent review P3-6: the csv module took up to 4 s and held up to 140 MB for one row of
  millions of cells); traced memory stays under 1 MiB. `.txt` files aren't checked: a text
  editor opens them, and Excel only through its import wizard.
- **Names** are display text only: path components dropped, control and bidi characters
  refused, and the characters that render blank (every Unicode default-ignorable character but
  the zero-width joiner and non-joiner, the Braille blank, the Egyptian hieroglyph blanks
  U+13441 and U+13442, the Khitan filler U+16FE4, the ideographic half fill space U+303F, the
  Tifinagh consonant joiner U+2D7F anywhere but between two Tifinagh letters (checked one
  character either side: review round 4, PARSERS4-2, the check copied the rest of the text for
  each joiner, 1.6 s for a 1 MiB value; cleaning is linear, 0.1 s for it), a code point
  unassigned in the server's Unicode data outside the emoji blocks (newer emoji are kept), and a
  variation selector except after a base it is defined for: a keycap digit, `#` or `*` with
  U+20E3 after it, one of the 359 emoji with variation sequences (`↔`, `⭐` or `✔` is such a
  base; `→`, `★`, `✓` or `😀` is not: review round 3), `0` and a few mathematical symbols
  with standardized variants, the CJK ideograph a compatibility variant is defined for (U+FE00-
  FE02, 1,002 sequences), a CJK ideograph with an ideographic variation selector in use
  (U+E0100-E013F), a Mongolian letter: adversarial review of 73ff1e3, L12, R164, and its review
  rounds 1-3, [leads.md](leads.md#names)), ≤ 200 characters. Objects live under generated keys (`YYYY/MM/<uuid>`), never the
  uploaded name.
- **Limits**: `ATTACHMENT_MAX_BYTES` (10 MB; checked from Content-Length and again while
  reading: 413), `ATTACHMENT_MAX_PER_NOTE` (10; counted under the note's lock: 422), empty
  files refused. The proxy allows 11 MB on the upload route only (and 4 uploads in flight per
  client, deep security audit H-1). `ATTACHMENT_MAX_BYTES_PER_USER` (2 GiB; 0 = no quota): the
  total size of the files a person uploaded that are still in storage (stored, being
  uploaded, and deleted or failed until the purge job removes their object); an administrator
  adding a file in someone's workspace is charged for it. Checked under a per-person advisory
  lock (`ARKQ`, taken first in the upload's transaction, before the lead and note locks), so
  concurrent uploads can't overshoot it: 422 `attachment_quota_exceeded` (deep security audit
  L-7).
- **Upload** (`POST …/activities/{note}/attachments`, the raw file, `X-Filename`
  percent-encoded): streamed in 64 KB chunks, hashed (SHA-256), spooled to a temporary file
  above 1 MB; then the uploader's quota is checked under their advisory lock, the note is
  locked (lead first: the lock order) and a row written
  (`uploading`, generated key); then the object stored (no lock held); then the row marked
  `stored` and `attachment.uploaded` audited (extension and a size bucket, never the name).
  Storage down: 503 `storage_unavailable`, the row `failed`; a crash in between leaves an
  `uploading` row the hourly housekeeping resolves (object deleted if present).
- **Who**: visible exactly when its note is; adding or deleting files changes the note, so
  the note's author or an administrator managing the workspace (403 otherwise).
- **Download** (`GET …/attachments/{id}/download`): the note is re-checked through the
  caller's scope on every request (knowing the id grants nothing; another workspace: 404);
  streamed with `Content-Disposition: attachment` (RFC 6266 `filename*`),
  `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'none'; sandbox`,
  `Cross-Origin-Resource-Policy: same-origin`, `Cache-Control: private, no-store`.
  **Preview** (`…/preview`): images only (PNG, JPEG, WebP, GIF, validated at upload), inline,
  same headers. Never HTML or SVG as active content.
- **Virus scanning** (`ATTACHMENT_SCANNER`): none configured — files are `not_scanned` and
  downloadable (the UI says nothing extra); `clamd://host:3310` — files are `pending` until a
  job scans them over clamd's INSTREAM protocol: `clean` files download, `rejected` ones are
  blocked and their object deleted (`attachment.rejected`). A scanner outage keeps files
  pending (the job retries, then goes dead: an alert). No pretend scanner. **Changing the
  setting**: configuring a scanner later queues earlier files for scanning (500 per hourly
  housekeeping run; downloadable once clean); removing it releases files still waiting
  ("not scanned").
- **Delete** (`DELETE …/attachments/{id}`): hidden at once (`deleted_at`, `deleted_by`,
  `attachment.deleted`), the object removed by a job and, failing that, the hourly
  housekeeping (`activities.housekeeping`); idempotent (a missing object counts as removed).
  Once the object is gone the file's **name and content hash go too** (the name becomes
  "removed"; type, size and who/when stay as evidence), likewise for a blocked file and an
  upload that never finished, and the purge is recorded in the erasure ledger. A legal hold
  on the lead defers the purge; `ATTACHMENT_S3_PURGE_VERSIONS` removes a versioned bucket's
  earlier versions too (privacy remediation; [privacy.md](privacy.md#attachments)).
- **Not indexed** for search or Ask Arkray: file contents never reach a language model.
- **Erasure** (`erase_lead`): the files of the lead's notes are deleted, names and content
  hashes blanked, objects queued for removal. An upload still being written when the lead is
  erased is removed when it finishes (the purge leaves uploading rows alone; the upload then
  finds its row deleted, queues the purge and answers 404). Afterwards the lead is archived:
  **an archived lead's notes take no new files or text** (422), so nothing personal can be
  added beyond the erasure's reach.

## Ownership

| Who creates | In workspace | The activity's owner | `created_by` | Needs |
|---|---|---|---|---|
| a salesperson | own (`me`) | the lead's owner = themselves | them | `crm.access_own` |
| an administrator | Rahul's (`{id}`) | Rahul (the lead's owner) | the admin | `crm.manage_any` |
| an administrator | organisation (`all`) | the lead's owner | the admin | `crm.manage_any` |

Nobody chooses an owner: the API has no `owner` field (a 400), and the services have no
parameter for one. Current work is always the lead's owner's, which PostgreSQL enforces
([ADR-0020](adr/0020-activity-integrity.md)). A lead owned by a deactivated user takes no new
activities (422 "…Change the owner to an active user first.": the deal's *Change owner*).

**Historical attribution never changes**: `created_by`, `completed_by`, `cancelled_by`,
timeline actors and audit actors stay as they were, through reassignments, renames and
reopening.

## Lead reassignment

`LeadReassigned` is the one mechanism (Phase 2 publishes it; Phase 3's pipeline and Phase 4's
activities subscribe). In **one transaction**, Lead A → B:

| Record | Follows to B? |
|---|---|
| the lead | yes (the reassignment itself) |
| open opportunities (pipeline) | yes |
| **open tasks** (archived ones too) | **yes** |
| **scheduled meetings** | **yes** |
| **notes** | **yes** (author unchanged) |
| completed / cancelled tasks and meetings | no: they stay with whoever had them |
| won / lost opportunities | no |

Moved activities get a new version, one `{type}.owner_changed` audit event each (actor = the
admin, subject = the previous owner, reason `lead_reassigned`) and an `ActivityOwnerChanged`
event; the lead's timeline records one `lead.reassigned` entry. Constant queries however
much moves (one lock query, one update, one audit insert). If anything fails, everything
rolls back (tested with a failing later subscriber). There is no eventual consistency:
nothing goes through Celery. The database refuses to commit current work left with the
previous owner.

Consequence of owner-based visibility: after the move B sees the lead, its notes and open
work, and the lead's own history; A keeps seeing the meetings A completed (the lead shown as
*in another workspace*); administrators see everything organisation-wide.

## Opportunity integration

- An activity on an opportunity is also about the opportunity's lead (`lead_id` derived;
  `(opportunity_id, lead_id)` is a composite foreign key onto the opportunity).
- Sending a lead and an opportunity of another lead is a 400; someone else's lead or
  opportunity is a 404 (non-enumerating, below).
- Activities may follow up closed opportunities (a thank-you call after a win). New work on
  a closed opportunity that its closer kept after the lead moved on would belong to the
  lead's new owner, so the closer is refused (422 "…ask an administrator").
- Open work on an open opportunity follows the lead with it: never "lead B, open
  opportunity B, open task A".
- A deactivated owner takes no new, reopened or restored current work: restoring an
  archived open task or scheduled meeting is refused (422) like creating one (Phase 6
  review). Closed work stays restorable.
- An archived lead or opportunity takes no new, reopened or restored work (422). Its open
  tasks and meetings can still be edited, completed or cancelled, like its open
  opportunities: archiving a lead doesn't strand work someone is finishing (decided in the
  Phase 4 review, D-5; tested).

## Last contacted

`Lead.last_contacted_at` means **the last completed customer interaction**. Phase 4 rule
(conservative):

| Event | Updates last contact? |
|---|---|
| a meeting **completed** | **yes**: to the meeting's **start time** |
| a meeting scheduled (even in the past), edited, cancelled, reopened | no |
| a task created or completed | no |
| a note added or edited | no |
| a manual edit on the lead (Phase 2 field) | yes, as typed: an explicit correction |

- **MAX semantics**: completing an older meeting after a newer one never moves it back
  (1 Oct, then 3 Oct, then a 25 Sep meeting: it stays 3 Oct). Implemented by
  `leads.services.record_contact`, under the lead's lock (a no-op if not later).
- When it changes, the lead's version is bumped (an edit form opened earlier gets a 409,
  never silently overwrites it), `lead.updated {fields: [last_contacted_at], via:
  meeting_completed, via_id}` is audited and `LeadUpdated` published.
- Reopening a completed meeting keeps the recorded contact (it may have happened; the lead's
  last contact can be corrected on the lead).
- Only authorised domain operations change it: completing someone else's meeting is a 404
  and changes nothing (tested).

## Timeline

Design: [ADR-0021](adr/0021-materialized-timeline.md). An append-only table,
`activities_timeline_entry`, written **in the transaction of each change**:

| Kind | Written by | Snapshot (`details`) |
|---|---|---|
| `lead.created`, `.status_changed`, `.reassigned`, `.archived`, `.restored` | subscribers to the lead events | status key and name; previous and new owner |
| `opportunity.created` (incl. by conversion), `.stage_changed`, `.won`, `.lost`, `.reopened` | subscribers to the pipeline events | stage names as they were; conversion flag |
| `task.created`, `.completed`, `.cancelled`, `.reopened` | activity services | — |
| `meeting.scheduled`, `.rescheduled`, `.completed`, `.cancelled`, `.reopened` | activity services | times when scheduled / rescheduled |
| `note.added` | activity services | — |

- Nothing is fabricated: every entry records something that happened, written by the code
  that did it. Lead edits stay in the audit log (not timeline events). Pre-Phase-4 history
  was backfilled from the audit trail and the stage history (`activities.0003`).
- **Historical integrity**: stage and status names are snapshots (renaming a stage doesn't
  change the past); people are stable ids shown by their current name; the actor is whoever
  acted (the admin, not the workspace owner). Titles and note text are read from the live
  record, so an archived or edited note never lives on in the timeline.
- **Lead timeline** (`GET …/leads/{id}/timeline`): lead events, the lead's opportunities'
  events and its activities, newest first. **Opportunity timeline**
  (`GET …/opportunities/{id}/timeline`): the opportunity's own events and its activities
  (no lead events).
- **Visibility, decided when read** (one SQL query): the lead (or opportunity) must be
  visible (404 otherwise); lead events are visible to whoever sees the lead; an activity's
  or opportunity's entry only while that record is in the same scope and not archived.
  A visible task on an opportunity the reader can't see shows the opportunity as
  restricted (`{"id": null, "restricted": true}`).

## Pagination

Signed composite keyset cursors ([ADR-0016](adr/0016-composite-keyset-pagination.md)).
Activities: `-created_at` (default), `created_at`, `scheduled` (due/start soonest first,
undated last), `-scheduled`; 1–100 per page. Timelines: `(occurred_at, id)` newest first,
1–50 per page (default 20). Malformed, forged or other-ordering cursors are a 400; cursors
carry only timestamps and ids (no titles or text); the scope always comes from the URL,
so a replayed cursor can't widen what anyone sees (tested across workspaces). Responses
carry no counts.

## API

All under `/api/v1/workspaces/{me|all|user_id}/`, every route in `tests/authz_matrix.py`:

| Route | |
|---|---|
| `GET activities` | list: `type`, `status`, `lead`, `opportunity`, `owner` (organisation-wide only), `date_from`/`date_to` (inclusive business dates of the due time / start / creation), `overdue`, `current` (open tasks and scheduled meetings), `upcoming` (current work from now on; undated tasks included), `cancelled` (`false` leaves cancelled tasks and meetings out, `true` keeps only them), `archived`, `ordering`, `cursor`, `page_size`. Unknown parameters and contradictions (`type=meeting&status=open`, `upcoming` with `overdue`, `cancelled` with `status`) are a 400 |
| `POST activities` | create `{type, lead?, opportunity?, …the type's fields}` (`Idempotency-Key` supported) |
| `GET`/`PATCH activities/{id}` | detail; edit `{version, …the type's fields}` |
| `POST activities/{id}/complete` · `cancel` · `reopen` · `archive` · `restore` | `{version}` |
| `GET activity-summary` | open tasks, tasks due today, overdue tasks, meetings today, upcoming meetings |
| `GET leads/{id}/timeline`, `GET opportunities/{id}/timeline` | `cursor`, `page_size` |

Input is strict: undeclared keys are a 400 (`owner`, `created_by`, `status`,
`completed_at`, `version` in a create, `type`/`lead` in an edit, `is_superuser`, …), and each
type accepts only its own fields (a task with a `location` is a 400, not an ignored key).
Output is an explicit allowlist: people as `{id, full_name, is_active}`, related records
only when visible, text previews in lists, `is_overdue` and `completable` computed with the
server's clock.

**Write budgets** (adversarial review of 73ff1e3, M6; R153). Creating is budgeted, inside
the creating transaction: at most `ACTIVITY_MAX_CREATED_PER_USER_PER_DAY` (1,000) tasks,
meetings and notes per person in any 24 hours (429 `rate_limited` with `Retry-After`; counted
from the activities themselves, which are never deleted, in one statement over
`activities_created_by_idx` whatever else the person did (review round 1, QUEUES-3), under a
per-person advisory lock of its own taken first; the creator pays, also in someone else's
workspace, and archiving gives nothing back),
and `ACTIVITY_MAX_PER_CUSTOMER` (5,000) activities per customer, archived ones included,
counted under the lead's lock (422 `activity_limit_reached`). `0` turns either off; nothing is
written when one refuses. Edits, completions and archives aren't budgeted (they create
nothing). One account posting long notes had kept the indexing worker hours behind for
everyone; details and the reasons for the numbers:
[reliability.md](reliability.md#write-budgets).

## Idempotency

| Operation | Protection |
|---|---|
| create task / meeting / note | `Idempotency-Key` (existing `core.idempotency`, operation `activities.create`): a retry replays the first result; the UI derives one key per identical request body |
| complete, cancel, reopen, archive, restore | naturally idempotent: repeating a change that already happened is a no-op success |
| edit | the version: a duplicate submit after a success is a 409, and the UI's busy button prevents it |

## Authorization

The same four layers as everything else ([authorization.md](authorization.md)): signed-in
user, capability, workspace → `AccessScope`, object lookups through `scope.apply()` (404
outside). Writes call `identity.workspaces.authorize_write` (own: `crm.access_own`; another
user's or `all`: `crm.manage_any`). Services apply the scope themselves, so a direct call is
as constrained as HTTP.

- **Relationship fields are scope-aware and non-enumerating**: linking your task to someone
  else's lead or opportunity answers exactly like a lead or opportunity that doesn't exist
  (404, identical bodies; tested). It can't be used to confirm that a record exists.
- **Counts are scoped like rows**: the summary is computed from `scope.apply()` first, and
  is identical whether or not other users have records (tested).
- Timelines: see [Visibility](#timeline). The cross-user suite is
  [`tests/security/test_activities_cross_user.py`](../backend/tests/security/test_activities_cross_user.py)
  (131 cases, both directions).

## Lock order

Extends [ADR-0018](adr/0018-pipeline-integrity-by-composite-keys.md). Every operation takes
its locks in this order, so no two can wait for each other in a cycle:

1. **the lead**: `FOR NO KEY UPDATE` (every activity write, every opportunity write, and the
   last-contact update a meeting completion makes: a non-key update under the same lock,
   never an upgrade), or `FOR UPDATE` from the start when the operation changes the lead's
   owner or status (reassignment, conversion);
2. **opportunities**: `FOR NO KEY UPDATE OF` the opportunity only, several in ascending id
   order. Activity writes never lock opportunities (their owner and archive state only
   change under the lead's lock, which they hold);
3. **activities**: `FOR NO KEY UPDATE OF` the activity only, several in ascending id order
   (the reassignment subscriber, which runs after the pipeline's: pinned by a test);
4. **user rows**: `FOR SHARE` (`lock_assignable_user`), leaf locks;
5. inserts: timeline entries, audit, idempotency records.

An activity's `lead_id` is read first (unlocked: it never changes), then the lead is locked,
then the activity is locked through the scope and re-checked. Configuration rows (stages,
statuses) are never locked. A deliberate mutation (locking the activity before its lead)
made the concurrency suite report `deadlock detected` in the reassignment races and the
storm test.

## Concurrency

Every change requires the version the client saw (409 otherwise) and bumps it.

| Race (real threads, own connections) | Outcome |
|---|---|
| complete × complete (×3) | all succeed (repeats are no-ops); one completion, one timeline entry, one audit event |
| edit × complete, cancel × complete, edit × cancel (tasks and meetings) | one wins, the other 409; exactly one change applied |
| note edit × lead reassignment | the edit lands then the note moves, or the edit is 404; never lost, author unchanged |
| task / meeting / note creation × reassignment | created then moved to the new owner, or 404 |
| creation × the opportunity closing | both succeed |
| completion × reassignment | completed first (stays with the person who did it), or 404 and the open task moves |
| reassignment holding the lead's lock × meeting scheduling | the scheduling waits, then 404; no deadlock |
| older × newer meeting completions (4 at once, shuffled, 4 rounds) | last contact = the newest meeting's start, always |
| a storm of 8 mixed writers × 3 rounds on one lead (creates, completions, cancels, reassignment, an opportunity edit) | no deadlock; current work always with the lead's owner |
| two users on their own leads, both holding their locks at once | both succeed |
| the same create ×3 with one `Idempotency-Key` | one activity, one timeline entry |
| reopen × reassignment | the reopened task ends with the new owner |

## Audit and domain events

| Operation | Audit (`target_type=activity`) | Metadata (never text) | Event |
|---|---|---|---|
| create | `task.created` / `meeting.created` / `note.created` | workspace, owner, lead, opportunity | `ActivityCreated` |
| edit | `{type}.updated` | changed field **names** | `ActivityUpdated` |
| complete / cancel / reopen | `{type}.completed` / `.cancelled` / `.reopened` | (reopen: previous status) | `ActivityStatusChanged` |
| archive / restore | `{type}.archived` / `.restored` | — | `ActivityArchived` / `ActivityRestored` |
| moved with its lead, or reopened for the lead's new owner | `{type}.owner_changed` | from/to owner, lead, reason | `ActivityOwnerChanged` |
| a meeting completion advancing the lead's last contact | `lead.updated` (on the lead) | `fields`, `via`, `via_id` | `LeadUpdated` |

Actor = whoever acted; subject = the owner when that is someone else. No-ops and refusals
write nothing. Events carry identifiers, types and status keys only. They have **no
subscribers in Phase 4** (the timeline is written directly): the consumer is Ask Arkray
(Phase 8), which re-indexes notes and descriptions when they are created, edited, moved,
archived or restored ([rag-architecture.md](rag-architecture.md#indexing-pipeline)).

## Dashboard figures (for Phase 5)

`selectors.activity_summary(scope, now=…)` (two aggregate queries since Phase 5),
`selectors.upcoming_meetings(scope, now=…, limit=5)` and (added in Phase 5)
`selectors.next_open_tasks(scope, now=…, limit=5)` (the first rows of the Tasks tab: open,
soonest due first) are the authoritative definitions; the Phase 5 dashboard uses them
unchanged ([dashboard.md](dashboard.md)):

| Figure | Definition |
|---|---|
| open tasks | open, not archived |
| tasks due today | open, due during today's business day (earlier today included: also overdue) |
| overdue tasks | open, due before now |
| meetings today | scheduled or completed, starting during today's business day |
| upcoming meetings | scheduled, starting now or later |

"Today" is the business day in `CRM_TIME_ZONE` (`core.business_time`, [ADR-0012](adr/0012-business-day-time-zone.md)):
00:15 IST on 3 October belongs to 3 October even though UTC is still 2 October (tested at
the boundary). No separate analytics store. The Activities page shows the same figures as
shortcuts (`GET …/activity-summary`), and each shortcut opens a list of exactly the activities its
figure counts (meetings today: `cancelled=false` and today's dates; upcoming meetings:
`upcoming=true`; tested against the selector, figure by figure).

## Frontend

| Route | Workspace |
|---|---|
| `/activities`, `/activities/{id}` | own (sales users); organisation-wide (admins) |
| `/admin/users/{id}/activities`, `…/activities/{activityId}` | that user's, under the "Viewing CRM for" banner |

- One implementation: the route files render the shared views; the workspace comes from the
  URL. Views are keyed by workspace (and activity), and every query key starts with
  `["activities" | "timeline", kind, <workspace>]`; "keep the previous data while loading"
  applies only within the same workspace. Tested: Rahul's activities → Priya's, Back, Rahul's
  opportunity timeline → Priya's: nothing of Rahul's ever shows. A timeline refetch the
  server refuses (403/404) removes the entries from the screen.
- **Activities page**: summary shortcuts (open, overdue, due today, today's meetings,
  upcoming), tabs All / Tasks / Meetings / Notes (each starting from a sensible status and
  sort), status and sort, and under "More filters" the date range (an inverted range is
  explained and never sent), opportunity (the opportunity picker below), owner
  (organisation-wide) and the archived view. Filters
  live in memory per workspace (never in the URL or storage). Table on tablets and desktops,
  cards on phones; type, status, "Overdue" and "Awaiting outcome" are written out. Complete
  is a button on each row (keyboard and touch, using the row's version; a 409 is explained
  and the list reloads); cancel and archive ask first. After any write, the activity's row
  is updated wherever this workspace's lists and open-work cards hold it, so a list shown
  again before its reload offers the right actions with the right version; a row's menu
  doesn't open while an action on it runs. A scheduled meeting shows Complete within 30 s
  of its start on a page left open (one shared clock; the server decides).
- **Calendar** (the first of the page's tabs; the list stays the default): tasks at their
  due time and meetings from start to end, by month, week (Monday to Sunday) or day, in
  India time like every time on screen (a meeting running past midnight is drawn to the end
  of its day). Cancelled and archived ones are left out; completed ones stay, struck through;
  overdue tasks and meetings awaiting an outcome are marked in words and by icon, not colour
  alone. Organisation-wide, *My calendar* narrows it to the viewer's own and *Everyone*
  (the default) names each entry's owner. A month cell lists three entries, or two and
  "+N more", which opens the day; on phones a cell shows dots and opens the day. Each day's
  **+** (and, with a mouse, a click on an empty part of a day, or on a time in the week and
  day views, to the half hour) opens the task/meeting dialog prefilled for then (a whole day:
  a meeting at 9:00 for 30 minutes, a task due at 18:00) with a Meeting / Task switch that
  keeps what was typed; a prefilled form closes without asking. An entry opens its activity.
  It reads the list API per type (`type`, `date_from`/`date_to` = the days on screen,
  `cancelled=false`, `ordering=scheduled`, `page_size=100`, `owner` for My calendar),
  following `next` with the same parameters, at most 5 pages per type; beyond that it says
  the period is cut short and suggests a narrower one. Its query key is under
  `["activities", "calendar", <workspace>]`, so every activity write marks it stale. The
  view and the calendar's position are remembered per workspace in memory, like the
  filters; a summary shortcut or a dashboard figure shows the list.
- **+ Task / + Meeting** open a dialog (from the Activities page or the calendar: an
  opportunity picker limited to this workspace, required: recent open opportunities, or any
  found by typing its title or customer, through global search; from an opportunity: that
  one, fixed. Then due date and time or start and end in India time, priority,
  location, https link, description/agenda). Errors sit under their fields and focus moves
  to the first; an identical retry reuses its idempotency key; an edit conflict keeps the
  typing and offers "Apply my changes to the latest version" or "Discard"; a refused create
  shows the server's reason. Moving a meeting's start moves its end (the length is kept).
  Escape, a click outside, Close or Cancel with unsaved typing asks "Discard what you
  typed?" first.
- **Opportunity page**: *Open work* (open tasks and scheduled meetings with Complete, + Task,
  + Meeting), its *Timeline* (History tab) and its notes (Notes tab). Activity rows, pages and
  the dashboard name the customer as text (there is no customer page to link to).
- **Activity page**: details, record (created by, completed/cancelled by and when), actions
  (complete, cancel, reopen, edit, archive/restore); meeting links open in a new tab with
  `rel="noopener noreferrer"`; a note is edited in place by its author only, and a conflict
  keeps the text on screen with the other version underneath (or, if the note was archived
  meanwhile, says so and disables saving). Note boxes are read-only while a save runs. A
  refetch refused with 403 removes the activity from the screen.
- Accessibility: labelled controls, errors associated with inputs, focus on the first
  invalid field, dialogs trap focus and return it, Escape closes, the timeline is an ordered
  list with `<time>` elements, nothing relies on colour alone. When an action removes the
  focused control, focus moves to what replaced it: the message saying what happened
  (Complete, Reopen), "Edit note" after saving a note, the first entry "Show older" added.
  Controls repeated per row are told apart ("Actions for note: Prefers morning calls";
  "New task", "New meeting").
- After *Change owner* hands a deal out of the user's workspace being viewed, the deal page
  marks that workspace's data stale without refetching what it shows (a 404 that flashed up
  as an error in the Phase 6 live walkthrough of lead reassignment) and goes to its Pipeline.

## Performance

Query counts per request are pinned (identical at 10 and 100 rows,
[`test_query_counts.py`](../backend/arkray/activities/tests/test_query_counts.py)):

| Endpoint | Queries |
|---|---|
| list (own / organisation), any filter or sort, following a cursor (both directions) | 3: session, user, activities (lead, opportunity, owner, author joined, text cut to a preview in SQL) |
| list in a user's workspace | 4 (+ the workspace check) |
| detail | 3 |
| summary | 4 (two aggregates since Phase 5: open tasks; meetings from today on) |
| lead timeline | 5: session, user, lead visibility, entries (actor, activity, opportunity joined), the people named in the page (one query for all; 4 when nobody is named) |
| opportunity timeline | 4 |
| create a task | 10, whatever else exists |
| complete a task | 11; a meeting 14 (+ the last-contact update and its audit) |
| lead reassignment moving 1 or 25 tasks and 1 or 25 notes | 12 either way |

Benchmark ([`tests/performance/bench_activities.py`](../backend/tests/performance/bench_activities.py)):
EXPLAIN ANALYZE of the exact SQL the selectors run, best of three, PostgreSQL 16 in Docker on
a laptop. **403k**: 403,000 activities (180,000 tasks, 120,000 meetings, 103,000 notes) over
100,000 leads and 60 owners (the heaviest with 29,800), 50,000 opportunities, 733,000
timeline entries, one lead with 3,000 notes. **2M**: the same plus four older 60-day periods
of closed history (1,998,067 activities; the heaviest owner 136,696), the performance
reviewer's method for testing growth.

| Query | Heaviest owner 403k / 2M | Typical owner 403k / 2M | Organisation 403k / 2M |
|---|---|---|---|
| all, newest / oldest first | 0.17–0.21 / 0.20–0.22 ms | 0.18–0.21 / 0.23–0.24 ms | 0.28–0.31 / 0.26–0.27 ms |
| all, by due/start; one week by due/start | 0.15–0.20 / 0.11–0.26 ms | 0.13–0.14 / 0.15–0.23 ms | 0.23 / 0.35–0.36 ms |
| open tasks, soonest / latest due; overdue; scheduled meetings | 0.21–0.28 / 0.14–0.20 ms | 0.15–0.20 / 0.15–0.21 ms | 0.28–0.40 / 0.30–0.57 ms |
| one type of any status by due/start | 0.34–0.35 / 0.20–0.30 ms | 0.19 / 0.21–0.23 ms | 0.34–0.35 / 0.34–0.49 ms |
| open and scheduled work; upcoming meetings list | 0.17–0.25 / 0.16–0.19 ms | 0.13–0.17 / 0.16 ms | 0.22–0.37 / 0.25–0.73 ms |
| completed tasks, notes, archived: newest first | 0.28–1.06 / 0.20–0.56 ms | 0.22–0.52 / 0.21–0.71 ms | 0.28–0.36 / 0.34–1.06 ms |
| rare or old matches, newest first (cancelled meetings, archived notes, a week 50 days ago) | 0.06–2.46 / 0.04–1.98 ms | 0.03–1.71 / 0.04–1.64 ms | 0.03–0.68 / 0.06–1.52 ms |
| an administrator's owner filter, newest / oldest | — | — | 0.15–0.27 / 0.29–0.39 ms |
| summary (one aggregate, Phase 4) | 4.1 / 12.1 ms | 0.9 / 4.0 ms | 25 / 31 ms |
| summary (two aggregates, Phase 5; 2M) | 0.8 ms | 0.03 ms | 12 ms |
| upcoming meetings (5) | 0.07 / 0.08 ms | 0.07 ms | 0.11–0.12 ms |
| deep cursor (3,000 rows in) | 0.40 / 0.49 ms | 0.21–0.23 ms | 2.4–2.5 ms |
| a lead's activities / its open work / an opportunity's | — | — | 0.04–0.08 / 0.05–0.13 ms |
| timeline: typical lead, 3,000-note lead (first page, page 125), opportunity | — | — | 0.08–0.11 / 0.09–0.15 ms |
| reassignment's lock query; the ownership key's check on a lead owner change | — | — | 0.02 / 0.04 ms; 0.43 / 0.48 ms |

What the benchmarks changed (each pinned by
[`test_activity_query_plans.py`](../backend/tests/performance/test_activity_query_plans.py),
which forbids sequential scans and sorts and names the index each shape must use):

- Lists by due/start without a status filter took 9–24 ms per owner and **261 ms**
  organisation-wide (bitmap or sequential scan plus a sort) before `activities_owner_when_idx`
  and `activities_when_idx` (migration 0004).
- **Performance review, P1**: newest/oldest-first lists of one owner could be served by
  walking the organisation-wide created index and filtering: 48 ms at 403k, 380–500 ms at 2M
  for an owner whose matches were rare or old (cancelled meetings, archived notes, a past
  week). The organisation-wide orderings now sort by their own generated column
  (`created_sort`, index `activities_created_idx`), used only when the list is
  organisation-wide with no owner, lead or opportunity filter; every narrower list sorts by
  `created_at`, which only the owner, lead and opportunity indexes cover. The choice is
  deterministic, not left to statistics. Extended statistics on `(owner_id, type, status)`
  and `(type, status)` (migration 0006) fix the planner's estimates for rare combinations.
- The lead index became `(lead, schedule_sort, id)` (a lead's open work in order; 64 ms
  organisation-wide with the lead filter before); the organisation-wide created index is
  ascending (rows arrive newest last; a descending index split pages on every insert: 28 MB
  against 15 MB at 400k).
- **At 2M** (found re-benchmarking the review fixes): open-and-scheduled work and one type
  of any status by due/start walked the all-statuses schedule index past the closed history
  (124 ms and 38 ms for the heaviest owner, 0.5 s organisation-wide). Partial indexes on
  current work only (`activities_owner_current_idx`, `activities_current_idx`: open tasks
  and scheduled meetings, a set that doesn't grow with history) and `(owner, type,
  schedule_sort)` / `(type, schedule_sort)` indexes (migration 0007) bring them to 0.2–0.7 ms.
  The "current" filter is written as `status IN ('open', 'scheduled')`, exactly the indexes'
  condition (equivalent by `activities_activity_status_valid`).
- The summary only reads meetings from today's start on (a past meeting can't be "today" or
  "upcoming"): organisation-wide it was a sequential scan (34 ms at 403k); it now follows
  open work (25 ms at 403k, 31 ms at 2M, five times the rows). One owner's summary reads
  that owner's entries of `activities_owner_sched_idx` (4 ms / 12 ms for the heaviest).

- **Phase 5 performance review (P2):** the one aggregate over "open task OR meeting from
  today" still read the heaviest owner's whole history (13–25 ms) and, with 20,000 more
  open tasks and 20,000 stale scheduled meetings, flipped organisation-wide to a sequential
  scan (135 ms; 154–243 ms with JIT). It is now two aggregates, open tasks and meetings from
  today's start on, each an index-only range of the schedule index: 0.8 ms / 12 ms at 2M,
  4.8 ms / 70 ms on the grown copy, figures identical
  ([dashboard.md](dashboard.md#what-the-benchmark-and-the-review-changed)).

Risk R44 is resolved by that split. Index count: 12 on `activities_activity` besides the primary and
timeline keys, nine of them partial (archived rows left out; the current-work ones hold open
work only). Writes are human-paced, so the extra index maintenance per write is cheap.

## Reliability

Activities depend on PostgreSQL only. With Redis down (the cache) or the broker down, creating,
completing, listing, the summary and timelines keep working and nothing is queued (tested in
`tests/security/test_outages.py`); AI is not involved.

## Adding a type

Call, email or WhatsApp: an `ActivityType` value; its statuses and columns (for example
`direction`, `duration_seconds`) with NULL-safe CHECKs in one migration; a `TypeSpec` in
[`spec.py`](../backend/arkray/activities/spec.py) (fields, required fields, initial status,
timeline kinds); timeline kinds and renderers; whether completing it counts as contact.
No new tables and no API redesign.
