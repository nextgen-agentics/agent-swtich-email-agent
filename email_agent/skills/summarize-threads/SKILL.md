---
name: summarize-threads
description: Write a short summary on each conversation in our mailboxes (who, what they want or what was agreed, who owes the next step), or summarise the conversations the request names. Only fills the summary; changes nothing else.
tools:
  - conversation_digest
  - save_summaries
---
# Summarise conversations

## Which conversations
- **The request names a conversation** (subject words, the other side, a reference): call `conversation_digest`
  with that word as `search`, and summarise only those.
- **Otherwise:** call `conversation_digest` with `only_stale_summaries: true`. Those are the conversations with no
  summary, or a summary older than their newest message.
- **A mailbox that is not one of ours:** `refuse` with `not_our_mailbox`.

## How to write a summary
- Use the messages in the digest (mirror copies are already left out). When a message's text is empty, its
  preview is used.
- One or two plain sentences, at most 300 characters:
  - who the other side is;
  - what they want, or what was decided (quantities, prices, dates exactly as written);
  - who owes the next step (us or them) and by when.
- Use the company's date format and currency from the run facts. Do not invent anything that is not in the
  messages.

## What to write
1. For each page, call `save_summaries` once, with one item (`thread_id`, `summary`) per conversation on that
   page. The tool sets the summary date to today and changes nothing else.
2. If the digest has a `next_offset`, call `conversation_digest` again with that `offset` (same arguments),
   summarise, save, and repeat until `next_offset` is null.

When `next_offset` is null and the last page is saved, answer. Do not read pages again.

## What to report
How many conversations were summarised, then one line each: subject, then the summary.
