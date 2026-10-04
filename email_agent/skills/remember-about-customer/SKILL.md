---
name: remember-about-customer
description: Remember a preference, instruction or fact about one named party (anyone we deal with), saved as agent memory linked to that party; or say what we already remember about them.
tools:
  - Party.list
  - Party.get
  - EmailContact.list
  - AgentMemory.list
  - AgentMemory.create
---
# Remember something about a party

## Find the party
1. Search `Party.list` with `search` set to the name in the request (try the shortest distinctive word if the
   full name finds nothing).
   - If the request gives an email address, `EmailContact.list` with `search` finds the contact; its `party_id`
     is the party.
2. Exactly one match → use its `id`.
3. Several matches → pick the one whose name matches best, and say which you chose.
4. **No match → `refuse` with `unknown_record`.** Never create a party.

## Remember
1. Read what we already remember: `AgentMemory.list` with `party_id`. Searching memory by its text does not work.
2. If an active memory already says the same thing, do not add it again. Say it is already remembered.
3. Otherwise call `AgentMemory.create` with only:
   `{"party_id": <id>, "category": <preference | instruction | fact | relationship>, "content": <the thing to
   remember, in the person's words, one or two sentences>, "source": "manual", "is_active": true}`.
   - **preference:** how they like things done (e.g. which currency or format they want).
   - **instruction:** what we must always or never do for them.
   - **fact:** something true about them.
   - **relationship:** who is who.

## If the request only asks what we remember
List the active memories from `AgentMemory.list` and write nothing.

## What to report
The party, the category and the text remembered, or the memories found.
