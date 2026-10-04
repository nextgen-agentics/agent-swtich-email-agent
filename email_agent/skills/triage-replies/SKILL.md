---
name: triage-replies
description: Find the conversations that need my reply today, say why each one does, and flag each with today's due date so it shows as Due in the inbox. Not for summaries, reminders or sorting.
tools:
  - mailbox_overview
  - EmailThread.get
  - EmailMessage.list
  - EmailMessage.get
  - EmailThread.update
---
# Triage: what needs my reply today

## How to decide
1. Start with `mailbox_overview`. It lists every conversation in our mailboxes with plain facts worked out from
   the messages: who wrote the newest real message (`newest_real_from`), when, its text, and whether we replied
   after it. Trust these facts over the conversation's own "last sender" field, which is not kept up to date.
   A message marked as a mirror only repeats an earlier message word for word; it is not a reply.
2. A conversation **needs my reply** when all of these hold:
   - the newest real message is from them (`newest_real_from: them`) and we have not replied after it;
   - it asks something of us: a question, a request, a decision, an approval, a quote, a deadline, a complaint,
     a payment chase, or a problem to fix.
3. It does **not** need my reply when:
   - it only informs or thanks us ("payment settled", "batch released", "thanks — confirmed");
   - we wrote last and are waiting on them;
   - there is no real message from anyone else (only our own sends).
4. If the overview text is not enough to decide, read the conversation's messages
   (`EmailMessage.list` with its `thread_id`). Do not guess.

5. If the request is about a mailbox that is not one of ours, `refuse` with `not_our_mailbox`.

## What to write
- For each conversation that needs my reply, call `EmailThread.update` with only
  `{"id": <thread id>, "flag_status": "flagged", "flag_due_date": <today>}`. Use the date given as today.
- Skip a conversation that is already flagged with a due date on or before today.
- Change nothing else: no other fields, no other conversations.

## What to report
Answer with one line per conversation that needs my reply, most urgent first (overdue or deadline-driven first,
then the oldest waiting): the subject, who is waiting, and in a few words why it needs a reply. Then one line
listing how many conversations you checked and how many need no reply. Use the company's own words and date
format; do not invent facts that are not in the messages.
