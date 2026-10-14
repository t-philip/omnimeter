# Publishing rule — what may and may not appear in this repo

This project documents its own reasoning in comments and in
`docs/DESIGN_SPEC.md`, and some of its numbers are justified by measurement,
which is worth more than a threshold asserted. Documenting measured evidence
is also how a specific home's dates, absences and occupants can end up in a
public repository.

## The rule

> **A comment may describe behaviour and reasoning. It may not describe a
> specific installation.**

Evidence is cited **generalised**, never **particular**: no real dates, no
event labels, no host names, no date paired with a reading.

The test to apply to any line you are about to write:

> *Could this have been written by someone who has never seen the author's
> home?*

## Why generalising costs nothing

Each of the shapes below was rewritten, and the documentation got **better** —
the general form states the behaviour a reader needs, rather than an anecdote
they cannot verify. The "before" column deliberately *describes* the offending
shape rather than reproducing it, since quoting the real text here would
republish the very thing that was removed:

| Shape that leaked | Rewritten as |
|---|---|
| an absence, given its exact length in days, with its gas rate | `a long absence can run at a small fraction of normal gas use` |
| a day labelled with who was working in the house, plus that day's litres | `a day with someone genuinely working in the house runs tens of litres` |
| a months-long stay quoting a free-text occupancy note | `a months-long occupied span` |
| a named internal container and its resource allocation | `a 1-core/2GB host` |
| a flagged period given as a real date range, annotated `(away)` | `a fortnight of low gas across an absence` |

## Where the specific evidence goes instead

Into a private record kept outside the repository — not here. A date, a
volume and a time of day together are exactly the right level of detail for
that record and exactly the wrong level for this one. Nothing is lost by
keeping them separate; the detail stays available where it is useful and
absent where it is a disclosure.

## Specifics

1. **Code comments and docstrings** — general behaviour only. Never a real
   date, event, name, place, host, or date-plus-reading pair.
2. **Test fixtures — the fiction window.** Every absolute date in test data
   uses **a year before 2002**, and nothing else.

   The existing suite was migrated by shifting every date back exactly **28
   years** — the Gregorian repeat cycle, which preserves weekday,
   day-of-year, month/day and leap-year structure, so nothing the code can
   observe changes but the year digit. That precision matters: the anomaly
   model's features include `dow_sin`/`dow_cos` and `doy_sin`/`doy_cos`, and
   an arbitrary offset would have silently moved every weekday and shifted
   summer fixtures into winter.

   This is the rule that makes the whole check work. Real data sources
   begin well after 2001, so a date inside the window provably records
   nothing, and a date outside it is a **hard signal** rather than a guess.
   Without a declared window there is no mechanical way to tell a fixture
   date from a real one — which is how real dates can sit
   in a test suite while every scan passes.

   Two carve-outs, both deliberate:

   - **Relative dates stay relative.** A fixture built from
     `_START + timedelta(...)`, or logic that works from "today", carries no
     calendar fact to leak, and pinning it would break the test.
   - **Values may survive when the shape is the thing under test.** A
     regression fixture exists *because* a real curve must produce a real
     outcome; randomising it makes the test assert nothing. A litre series
     under a 1999 date is a burst-shaped curve. The same series under its
     real date is a record of a house. Fictionalise the date and the label,
     always; keep the numbers only where their shape is the point.

3. **Free text defaults to a placeholder.** Any field a person or the
   application can fill — occupancy notes, device names, descriptions,
   labels, and the `.example` config files — carries `"Test Data ..."` or
   similar, never an invented string that reads like a real entry. This is
   where a verbatim note from a real database ends up, and it is precisely
   where some of the worst leaks come from.
4. **`docs/DESIGN_SPEC.md`** — undated aggregates are fine ("a bath is fast
   but short"); a dated or labelled observation is not.
5. **Network examples** — RFC 5737 documentation ranges only
   (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`).

## Checking

Two halves, and **both** are required.

**Structural** — `scripts/check_privacy.sh`, run automatically by the
`pre-push` hook. The hook lives in the tracked `.githooks/` directory, not
in `.git/hooks`, because `.git/` is never cloned — a hook kept there exists
only on the machine that wrote it, and a fresh clone silently loses the one
check that actually *prevents* a leak. Activate it once per clone:

```sh
git config core.hooksPath .githooks
```

It looks for bare dates in prose, month-years, durations attached to
household events, absolute dates at or after 2002, free-text fields carrying
something other than a placeholder, internal host names, real IPs, emails
and quoted log notes. It checks the *diff*, not the tree: flagging every
date in a suite this size would bury the signal until someone switched the
check off.

**Semantic** — a read of the diff by someone, or something, with no memory
of writing it, answering the one question above.

The second is not optional, and the script says so on every pass. The
leaks that matter are phrases no denylist would have contained in advance:
a quoted free-text note naming a person, or a description of who was working
in the house. Neither is a word anyone would have thought to list.

The structural rules fire across every category of leak they were written
for. What they cannot catch is a **single quoted proper noun** — a place name on its own is
indistinguishable from a column label, and matching it would flag every
capitalised string literal in the codebase. That gap is not closeable
mechanically, which is the whole argument for the second half.

The check also excludes itself from its own scan: it necessarily contains
the very patterns it looks for.

**A pass from the script does not mean the diff is safe to publish.** It
means the cheap half found nothing.
