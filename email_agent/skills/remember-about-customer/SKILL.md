---
name: remember-about-customer
description: Remember a preference, instruction or fact about one named party (anyone we deal with), saved as agent memory linked to that party; or say what we already remember about them.
tools:
  - Party.list
  - Party.get
  - EmailContact.list
  - recall_memory
  - remember_fact
---
# Remember something about a party

## Find the party
1. Search `Party.list` with `search` set to the **full name** as the request writes it (e.g. "Cardinal Tillage
   Works"). Only if that finds nothing, try the shortest distinctive word.
   - If the request gives an email address, `EmailContact.list` with `search` finds the contact; its `party_id`
     is the party.
2. Exactly one match → use its `id`.
3. Several matches → pick the one whose `name` matches the request best (a company named in the request is the
   company, not a person who works there), and say which you chose.
4. **No match → `refuse` with `unknown_record`.** Never create a party.

## Remember
1. Read what we already remember: `recall_memory` with the party's `party_id`. It returns only active memories
   (switched-off and expired ones do not count).
2. If a memory it returns already says the same thing (even in other words), do not add it again. Say it is
   already remembered. Otherwise go on to step 3.
3. Otherwise call `remember_fact` with `party_id`, `category` and `content` (the thing to remember, in the person's
   words, one or two sentences). Add `source_message_id` only when the fact comes from an email, not from the request.
   - **preference:** how they like things done (e.g. which currency or format they want).
   - **instruction:** what we must always or never do for them (e.g. a report, a deadline, a check they require).
   - **fact:** something true about them that asks nothing of us (e.g. their GST number, where their plant is).
   - **relationship:** who is who.
   Anything they **want, require or expect from us** is a preference or an instruction, never a fact.
   `remember_fact` itself skips an exact repeat ("already_remembered": nothing written).

## If the request only asks what we remember
Call `recall_memory` with the party's `party_id` and list what it returns. Write nothing.

## What to report
The party, the category and the text remembered (or that it was already remembered), or the memories found.
