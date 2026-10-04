---
name: follow-up-reminders
description: Set a "remind me if they don't reply" follow-up on each conversation where we wrote last and are waiting on the other side (or on the conversations the request names). Creates reminders only; never sends or drafts mail.
tools:
  - mailbox_overview
  - create_follow_ups
---
# Follow-up reminders: remind me if they don't reply

## Which conversations
- Call `mailbox_overview`.
- **A conversation is waiting on them when:**
  - `has_inbound` is true (they have written to us at some point);
  - `newest_real_from` is `us` (we wrote the newest real message).
- If the request names conversations or a party, use only those.
- A mailbox that is not ours → `refuse` with `not_our_mailbox`.

## When to remind
- By default, 3 working days (Monday–Friday) after our last message (`our_last_at`).
- If the request names a day or date ("by Friday"), use that instead.
- If the date is today or earlier, use the next working day after today.

## What to write
Call `create_follow_ups` **once**, with one item per conversation waiting on them:
- `thread_id`;
- `message_id` = `our_last_message_id`;
- `remind_at` (YYYY-MM-DD);
- `note`: a few words on what we are waiting for.

The tool skips conversations that already have an unfired follow-up reminder, and creates nothing else. Never
send or draft mail.

## What to report
- One line per reminder: subject, who we are waiting on, what for, and the reminder date in the company's date
  format.
- Then how many conversations were skipped (they already had a reminder), and how many are not waiting on anyone.
