---
name: follow-up-reminders
description: Set a "remind me if they don't reply" follow-up on each conversation where we wrote last and are waiting on the other side (or on the conversations the request names). Creates reminders only; never sends or drafts mail.
tools:
  - judge_threads
  - mailbox_overview
---
# Follow-up reminders: remind me if they don't reply

## How it runs
- One `judge_threads` task with `skill: follow-up-reminders`.
  - If the request names conversations or a party, pass the most distinctive word as `search`.
  - If the request names a day or date ("by Friday"), pass that date as `remind_on` (YYYY-MM-DD).
  - Code picks the candidates: conversations they have written to us in (`has_inbound`) where **we** wrote the
    newest real message.
  - Each candidate is judged: is it really waiting on them (`waiting_on_them`), a reminder date, and a few words of
    what we are waiting for.
- A mailbox that is not ours → `refuse` with `not_our_mailbox`.

## When to remind
- By default, 3 working days (Monday–Friday) after our last message (`our_last_at`).
- If the request names a day or date, use that instead.
- If the date is today or earlier, use the next working day after today. (Code checks this too.)
- A conversation where our last message closes the matter ("thanks, all done") is not waiting on them.

## What is written (by code)
One follow-up reminder ("remind me if no reply") per conversation waiting on them, pointing at our last message.
Conversations that already have an unfired follow-up reminder are skipped. Nothing else is created; mail is never
sent or drafted.

## What to report
- One line per reminder: subject, who we are waiting on, what for, and the reminder date in the company's date
  format.
- Then how many conversations were skipped (they already had a reminder), and how many are not waiting on anyone.
