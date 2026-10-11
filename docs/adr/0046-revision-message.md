# 0046. Revision message on apply

Status: Accepted
Date: 2026-10-11

## Context

A revision carried only the change set's `summary`, written when the draft was opened. Operators want to say why a
change was applied (a ticket, a reason) at the moment they apply it (06 planned-vs-built).

## Decisions

1. `POST .../change-sets/{cs}/apply` accepts an optional `message` (up to 2000 characters, trimmed). It is stored on
   the change set (`change_set.message`) and the pipeline copies it to the revision (`revision.message`, migration 0014).
2. `summary` stays what it is (the short title). `message` is the longer free text, empty when not given.
3. Revisions in `GET /revisions` and `GET /revisions/{n}` return `message`. The Git mirror commit puts it in the body
   between the title and the author line.
4. A failed apply that is retried takes the message of the retry; the message of an earlier attempt is overwritten.
