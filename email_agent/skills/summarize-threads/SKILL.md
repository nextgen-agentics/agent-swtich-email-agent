---
name: summarize-threads
description: Write a short summary on each conversation in our mailboxes (who, what they want or what was agreed, who owes the next step), or summarise the conversations the request names. Only fills the summary; changes nothing else.
tools:
  - judge_threads
---
# Summarise conversations

## How it runs
- One `judge_threads` task with `skill: summarize-threads`.
  - **The request names a conversation** (subject words, the other side, a reference): pass that word as `search`;
    only those are summarised.
  - **Otherwise** code picks the conversations with no summary, or a summary older than their newest message.
  - Each one gets a summary written with the rules below; code saves it (step "What is written").
- **A mailbox that is not one of ours:** `refuse` with `not_our_mailbox`.

## How to write a summary
- Use the messages given (mirror copies are already left out). When a message's text is empty, its preview is used.
- One or two plain sentences, at most 300 characters:
  - who the other side is;
  - what they want, or what was decided (quantities, prices, dates exactly as written);
  - who owes the next step (us or them) and by when.
- Use the company's date format and currency from the run facts. Do not invent anything that is not in the
  messages.

## What is written (by code)
Each conversation's `summary`, and its summary date set to today. Nothing else changes.

## What to report
How many conversations were summarised, then one line each: subject, then the summary.
