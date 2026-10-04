---
name: find-price-agreement
description: Find the mail where the other side agreed our price (an award or acceptance of our quote), save each agreement as a fact in agent memory and star the conversation. Not for drafting quotes, sending mail or changing prices.
tools:
  - price_overview
  - EmailMessage.list
  - Party.get
  - record_price_agreements
---
# Find where they agreed the price

## How to search
1. Call `price_overview`.
   - If the request names a party, part or reference, pass its most distinctive word as `search`.
   - Otherwise read every page: call again with `offset` = `next_offset` until `next_offset` is null.
   - Each item gives the references, the price lines from **our** messages, the other side's newest messages, and
     the linked deal.
2. Only if an item's text is cut short and you cannot decide, read that conversation with `EmailMessage.list` and
   its `thread_id`.

## What counts as an agreed price
- **Agreed:** the other side accepts **our** quoted price in writing. For example: "we are awarding … per your
  quote …", "we accept your quotation …", "go ahead at your price".
  - The figures come from **our** price line in the same conversation: item, quantity, unit price, line total
    (quantity × unit price, before tax), and our quote reference.
- **Not agreed:**
  - they chose someone else ("went with another …", "incumbent");
  - it is still open ("under review", "decision expected");
  - a request for a quote with no acceptance;
  - our own follow-ups;
  - a purchase order that does not name our quote.
- **When the record disagrees with the mail,** the mail decides.
  - If the item has a `deal`, compare its line `rate` with our unit price. Write "agrees" or "differs: deal
    <rate> vs quote <price>" in `record_check`.
  - A deal whose stage or notes say "agreed" when the mail shows no acceptance is **not** an agreement. Name it
    in your answer as "the deal says agreed, the mail does not".

## What to write
After reading each page, call `record_price_agreements` once for the agreements on **that page** (older pages
are shortened in your history, so record before moving on). One item per agreement:
- `thread_id`;
- `agreement_message_id`: the id of **their** accepting message;
- `party_id`: from the item;
- `reference`: our quote reference;
- `item`, `quantity`, `unit_price` and `total`, copied from our price line;
- `currency`: the code from the run facts;
- `agreed_on`: the date of their message (YYYY-MM-DD);
- `record_check`.

The tool saves each agreement to agent memory and stars its conversation. Write nothing for conversations that
are not agreements.

## When to stop
Once the last page has been read (`next_offset` is null) and each page's agreements are recorded, **answer**. Do
not read pages again. Your earlier `record_price_agreements` calls (their arguments) list everything you found.

## When to refuse
- The request asks about one specific agreement and the mail does not show an acceptance (still open, lost, or
  only a quote): `refuse` with `no_evidence`. Say what the mail does show.
- The named reference, party or conversation is not in our mailboxes at all: `refuse` with `unknown_record`.
- No agreement at all in our mailboxes: `refuse` with `no_evidence`.

## What to report
- One line per agreement: who agreed, our reference, item, quantity @ unit price = line total (company currency
  and date format), the date of their message, and the record check.
- Then one line listing what looked like an agreement but was not, and why.
