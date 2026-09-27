# Gap report — AgentSwitch Email vs AI-native email

> **Purpose:** week-one deliverable, the brief's three questions. **Audience:** course instructors.
> **Hand-written**, 2026-09-27. AgentSwitch facts were checked live (web screens, REST, MCP tools) between
> 24 and 27 Sep. Competitor facts come from the vendors' own pages (links at the end). Background:
> [gap-report-research.md](gap-report-research.md).

**Who we compare against.** **Shortwave** is the main benchmark. It rebuilt email around AI instead of adding a
chat window to an old inbox, and costs $30–$120 per user per month. Three others add features Shortwave lacks:
**Superhuman** (automatic follow-up reminders, one-line summaries that update, and an assistant that works inside
Gmail or Outlook), **Fyxer** (lifts the one mail that matters to the top; tracks promises made in mail) and
**Inbox Zero** (open source; tracks who owes whom a reply; blocks cold emails; briefs you before meetings).

**What AgentSwitch has.** The Email app already covers the classic inbox: folders, stars, flags with due dates,
labels, snooze, send later, undo send, read receipts, templates, snippets, block sender, VIP senders, bulk
unsubscribe, vacation replies and campaigns. It also has three views most inboxes lack: **Attachments** (every file,
filterable by type and sender), **Contact Graph** (who we know and how they connect) and **Timeline** (a
conversation history over time). The wider AgentSwitch holds what email is *about*: customers, deals,
sales orders, quotations, invoices, calendar, helpdesk tickets, approvals, projects, a knowledge base, contracts
and e-sign, all in the same database. What is missing is the AI layer that reads the mail and does the work.

## 1. What do they do that we do not?

| Feature | Who has it | AgentSwitch Email today |
|---|---|---|
| **"Needs you" list**: puts what needs you first; tracks who owes a reply and who you are waiting on | Fyxer, Inbox Zero (Reply Zero), Shortwave todos | Folders and counters only. The counters disagree with the lists (Suryodaya: Sent shows 7, lists 16). The stored "last sender" is not updated when messages arrive |
| **Automatic sorting**: labels, categories and archiving decided by AI | Superhuman Auto Labels / Auto Archive, Shortwave AI filters, Fyxer | Important/Team/VIP/News/Other tabs exist but nothing fills them (all empty on Suryodaya) |
| **Rules in plain English** ("archive cold sales emails") | Shortwave, Inbox Zero | Rules exist only in a technical format; none are set |
| **Summaries** on every conversation, updated as mail arrives | Shortwave, Superhuman Auto Summarize | A summary field exists but is empty on all 87 conversations |
| **Ask questions** of the whole mailbox ("what price did Cardinal agree?") | Shortwave, Superhuman Ask AI | Word search in subjects only |
| **Replies drafted before you open the mail**, in your style | Superhuman Instant Reply, Fyxer, Inbox Zero | Three fixed quick replies ("Thanks!", "Got it, will do.", "Looking into it.") |
| **Automatic follow-up reminders** with a draft nudge | Superhuman Auto Reminders, Fyxer | Reminders ("remind me if no reply") can be stored, but nothing creates them |
| **Blocks cold email; one-click unsubscribe** | Inbox Zero | Block sender and bulk unsubscribe exist, but nothing decides what to block |
| **Meetings from mail**: find a time, create the event, brief before it | Shortwave, Superhuman, Inbox Zero | Calendar exists in AgentSwitch; the email seat has no access |
| **Acts on new mail as it arrives** | Shortwave (Tasklet), Inbox Zero | Nothing tells the email seat that mail has arrived |
| **Other AI tools can drive the mailbox (MCP)** | Superhuman, Shortwave | ✅ Already here: 336 tools over MCP |

## 2. Which gaps can an agent close with the tools our seat already has?

**A** = our agent can build it now with today's email-seat tools. **B** = needs access to another AgentSwitch
app (it exists; our seat is not allowed in). **C** = needs development by the platform team (new field,
endpoint or screen).

| Feature | A: our agent now | B: needs access | C: needs development |
|---|---|---|---|
| "Needs you" list | Read each conversation's messages, decide who owes a reply, flag it with today's due date. It already shows as **Due** in the inbox | — | A "Needs you" screen with the agent's reason; keep "last sender" up to date |
| Automatic sorting | Set category, importance, label, archive or snooze on each conversation; a per-sender rule for next time | — | Label clicks in the screen do nothing (seen on Keystone) |
| Plain-English rules | Store the user's rules as agent memory; run them every few minutes as a scheduled agent task | — | An event when new mail arrives; a rule editor in plain English |
| Summaries | Write the empty summary field | — | Show the summary in the list and reading pane |
| Ask the mailbox | Search mail, follow the link each conversation already has to its customer (67 of 67 on Keystone) and deal (17), then answer with evidence | Quotations and invoices (sales and accounting apps) to check a price end to end | Pre-loaded Suryodaya mail has subjects but no text; memory cannot be searched by its text |
| Draft replies | Save a draft reply in the conversation | — | The Drafts list shows none of the saved drafts; failed sends are shown nowhere; the sample mailboxes cannot send |
| Follow-ups | Create "no reply" reminders; mark who we are waiting on | — | Confirm the reminder scheduler fires |
| Cold email | Mark senders blocked, archive cold pitches | — | Bulk unsubscribe as an agent tool (today it is a button only) |
| Find a file someone sent | List attachments by sender and conversation | — | Sample attachments are not linked to any message (Suryodaya's 5 have no message, type or real name; Keystone has none), so the Attachments view stays empty |
| Meetings | — | Calendar (scheduling app): read free/busy, create events, brief before meetings | — |
| Work that belongs to other teams | — | Helpdesk, approvals, projects: hand over by raising an escalation, which our seat can do | — |

**Screen changes to request** (all C): a "Needs you" view; summaries in the list; the reason the agent flagged
something, plus what it changed, with undo; a working Drafts list and a failed-send view; plain-English rules;
clickable labels.

## 3. What can our agent do that their product cannot?

Their products make a person faster at their inbox, and they only see mail. Our agent can do the whole job, and it
sits next to the business records. Take our seat's request on the US company: *"What needs my reply today, and
find the mail where they agreed the price."*

1. It reads all 26 inbox conversations from the messages themselves, because the stored "last sender" is stale.
2. It skips mails that need no answer ("Thanks — confirmed") and flags the rest with today's due date.
3. It finds the mail awarding RFQ-2026-0003 "per your quote QTN-2026-00003". It follows the conversation's link
   to the customer and deal, and checks the price against the deal and sales order.
4. It stars the mail and saves the agreed price as a memory other agents can read.

Throughout, it re-reads anything a colleague changed before writing. If the mail and the record disagree, it
reports both instead of trusting the mail. For the invoice it asks the accounting seat rather than guessing. The
same agent works unchanged for the Indian company in rupees, and every change it makes is signed with our login,
so a checker can verify it.

**It can use AgentSwitch's own views as tools, joined to the business.** Superhuman and Shortwave see a sender
as an email address. In AgentSwitch the **Contact Graph** ties each contact to the customer record (143 of 144
Keystone contacts), and records 28 links between customers. So the agent can answer "who do we know at
Cardinal, who introduced us, and what are we waiting on from them?" in one step. The **Attachments** view lets it
find "the rev C drawing Bharat EV sent" and check it against the open deal. The **Timeline** lets it rebuild
what was promised, and when, before it replies. A person would open three views and the CRM to do the same.

With access to the calendar and to quotations and invoices (column B), the same
approach extends to "book the follow-up call" and "does the invoice match what we agreed?". No other email product
can reach those records.

---
**Sources.** Shortwave: [pricing](https://www.shortwave.com/pricing/),
[AI assistant](https://www.shortwave.com/docs/guides/ai-assistant/), [plain-English filters](https://www.shortwave.com/).
Superhuman: [AI features](https://superhuman.com/ai), [plans](https://superhuman.com/plans/mail).
Fyxer: [fyxer.com](https://www.fyxer.com/). Inbox Zero: [GitHub](https://github.com/elie222/inbox-zero).
