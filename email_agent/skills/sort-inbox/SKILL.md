---
name: sort-inbox
description: Sort the conversations in our mailboxes by setting each one's importance (high/normal/low) and category tab (important/team/vip/news/social/other), so the inbox tabs fill. Changes only those two fields.
tools:
  - conversation_digest
  - sort_threads
---
# Sort the inbox

## Which conversations
- Every conversation in our mailboxes, a page at a time.
  1. Call `conversation_digest` with `limit: 6`.
  2. Decide that page, save it with `sort_threads`.
  3. Call again with `offset` = `next_offset` until it is null.
- If the request names conversations (a subject word or the other side), pass that word as `search`.
- A mailbox that is not ours → `refuse` with `not_our_mailbox`.
- Each item shows its current `importance` and `split_category`.

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

## What to write
For each page, call `sort_threads` **once**, with one item (`thread_id`, `importance`, `split_category`) for every
conversation on that page whose current values differ from what you decided.
- Skip the ones already right. If none on a page differ, call `sort_threads` with an empty `items` list.
- The next page can only be read once the current one is saved.
- Change nothing else.

When `next_offset` is null and the last page is done, answer. Do not read pages again.

## What to report
- A short table: subject → importance, category, and a few words of why.
- Then how many conversations were changed and how many were already right.
