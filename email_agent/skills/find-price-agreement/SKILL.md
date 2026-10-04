---
name: find-price-agreement
description: Find the mail where the other side agreed our price (an award or acceptance of our quote), save each agreement as a fact in agent memory and star the conversation. Not for drafting quotes, sending mail or changing prices.
tools:
  - judge_threads
  - price_overview
  - EmailMessage.list
  - Party.get
---
# Find where they agreed the price

## How it runs
- One `judge_threads` task with `skill: find-price-agreement`.
  - If the request names a party, part or reference, pass its most distinctive word as `search`.
  - Code picks the candidates: every conversation that talks about prices, quotes or orders. Each comes with the
    references, the price lines from **our** messages, the other side's newest messages, and the linked deal.
  - Each candidate is judged with the rules below: `status` agreed / lost / open / quote_only / not_price. For an
    agreed one, the figures are copied from our price line.
  - Code checks the figures (total = quantity × unit price) and writes (step "What is written").
- Only for one named conversation whose text is cut short: `EmailMessage.list` with its `thread_id`.

## What counts as an agreed price
- **Agreed:** the other side accepts **our** quoted price in writing. For example: "we are awarding … per your
  quote …", "we accept your quotation …", "go ahead at your price".
  - The figures come from **our** price line in the same conversation: item, quantity, unit price, line total
    (quantity × unit price, before tax), and our quote reference. `agreement_message_id` is **their** accepting
    message's id; `agreed_on` its date (YYYY-MM-DD).
- **Not agreed:**
  - they chose someone else ("went with another …", "incumbent") → `lost`;
  - it is still open ("under review", "decision expected") → `open`;
  - a request for a quote with no acceptance, or our own follow-ups → `quote_only`;
  - a purchase order that does not name our quote → `open`.
- **When the record disagrees with the mail,** the mail decides.
  - If the item has a `deal`, compare its line `rate` with our unit price. Write "agrees" or "differs: deal
    <rate> vs quote <price>" in `record_check`.
  - A deal whose stage or notes say "agreed" when the mail shows no acceptance is **not** an agreement. Say so in
    `why` ("the deal says agreed, the mail does not").

## What is written (by code, from the verdicts)
For each agreement: one fact in agent memory, linked to the other side's party (its first line is machine-readable:
run, conversation, message, reference, quantity, unit price, total, currency, date), and the conversation is starred.
Nothing is written for conversations that are not agreements, or whose figures fail the check.

## When to refuse (the planner, after the judge step)
- The request asks about one specific agreement and the judge step found it, but not agreed (still open, lost, or only
  a quote): `refuse` with `no_evidence`, saying what the mail does show.
- The named reference, party or conversation is not in our mailboxes at all (no candidates for the search):
  `refuse` with `unknown_record`.
- No agreement at all in our mailboxes: `refuse` with `no_evidence`.

## What to report
- One line per agreement: who agreed, our reference, item, quantity @ unit price = line total (company currency
  and date format), the date of their message, and the record check.
- Then one line listing what looked like an agreement but was not, and why.
