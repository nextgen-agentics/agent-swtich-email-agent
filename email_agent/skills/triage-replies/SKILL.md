---
name: triage-replies
description: Find the conversations that need my reply today, say why each one does, and flag each with today's due date so it shows as Due in the inbox. Not for summaries, reminders or sorting.
tools:
  - judge_threads
  - mailbox_overview
  - EmailThread.get
  - EmailMessage.list
  - EmailMessage.get
  - EmailThread.update
---
# Triage: what needs my reply today

## How it runs
- **Whole mailbox (the usual case):** one `judge_threads` task with `skill: triage-replies`. If the request names a party,
  subject or reference, add its most distinctive word as `search`.
  - Code picks the candidates: conversations whose newest real message is theirs and that we have not answered
    (worked out from the messages; mirror copies — a message repeating an earlier one word for word — are not replies).
    Every other conversation needs no reply by rule 3 below.
  - Each candidate is judged with the rules below (`needs_reply` true or false, and a few words of why).
  - Code then flags each conversation that needs a reply (step "What is written").
- Only for one named conversation that needs a closer look: `EmailMessage.list` with its `thread_id`.
- If the request is about a mailbox that is not one of ours, `refuse` with `not_our_mailbox`.

## How to decide
1. Trust the facts worked out from the messages over the conversation's own "last sender" field, which is not kept
   up to date.
2. A conversation **needs my reply** when all of these hold:
   - the newest real message is from them and we have not replied after it;
   - it asks something of us: a question, a request, a decision, an approval, a quote, a deadline, a complaint,
     a payment chase, or a problem to fix.
3. It does **not** need my reply when:
   - it only informs or thanks us ("payment settled", "batch released", "thanks — confirmed");
   - it is a recommendation, offer or suggestion that asks nothing of us: no question, no decision or approval
     requested, no deadline ("recommending a grade change for the EN19 shafts");
   - we wrote last and are waiting on them;
   - there is no real message from anyone else (only our own sends).
4. If the text is not enough to decide, the answer is no. Do not guess.

## What is written (by code, from the verdicts)
- Each conversation that needs my reply gets only `{"flag_status": "flagged", "flag_due_date": <today>}`.
- A conversation already flagged with a due date on or before today is left as it is.
- Nothing else changes: no other fields, no other conversations.

## What to report
Answer with one line per conversation that needs my reply, most urgent first (overdue or deadline-driven first,
then the oldest waiting): the subject, who is waiting, and in a few words why it needs a reply. Then one line
listing how many conversations you checked and how many need no reply. Use the company's own words and date
format; do not invent facts that are not in the messages.
