---
name: sort-inbox
description: Sort the conversations in our mailboxes by setting each one's importance (high/normal/low) and category tab (important/team/vip/news/social/other), so the inbox tabs fill. Changes only those two fields.
tools:
  - judge_threads
---
# Sort the inbox

## How it runs
- One `judge_threads` task with `skill: sort-inbox`: every conversation in our mailboxes is judged in parallel shards.
  If the request names conversations (a subject word or the other side), pass that word as `search`.
- Each item shows its current `importance` and `split_category`; decide both with the rules below.
- A mailbox that is not ours → `refuse` with `not_our_mailbox`.

## Importance
- **high:** the other side is waiting on us **and** it involves a deadline, money (payment, price, order), a
  quality problem or a complaint.
- **normal:** other real business: an open request, an order or quote in progress, or us waiting on them.
- **low:** information or thanks only; notices; nothing anyone needs to do.

## Category (the inbox tab)
- **important:** someone outside our company asking for or deciding something.
- **team:** only people at our own company (the same email domain as our mailboxes).
- **vip:** only for a party the request names as key.
- **news:** newsletters, portal and system notices, holiday or schedule announcements to many recipients.
- **social:** invitations or greetings with no business content.
- **other:** anything else.

## What is written (by code)
`importance` and `split_category`, only on conversations whose current values differ from the verdict. The ones
already right are left alone. Nothing else changes.

## What to report
- A short table: subject → importance, category, and a few words of why.
- Then how many conversations were changed and how many were already right.
