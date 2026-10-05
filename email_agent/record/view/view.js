/* Run and batch pages (Revision 15). Draws the page from the JSON block #page-data (a RunView or a BatchView,
   email_agent/contracts/view.py). No framework; the graph uses Cytoscape.js + dagre when the CDN loaded them. */
"use strict";

// ── small helpers ──────────────────────────────────────────────────────────
function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    e.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return e;
}
const k = (n) => (n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n || 0));
const secs = (s) => (s === null || s === undefined ? "—" : s < 60 ? s.toFixed(1) + " s" : (s / 60).toFixed(1) + " min");
const time = (t) => (t ? new Date(t).toISOString().replace("T", " ").slice(0, 19) + " UTC" : "—");
const clock = (t) => (t ? new Date(t).toISOString().slice(11, 19) : "—");
function pretty(x) {
  if (x === null || x === undefined) return "";
  if (typeof x === "string") {
    const t = x.trim();
    if (t.startsWith("{") || t.startsWith("[")) {
      try { return JSON.stringify(JSON.parse(t), null, 2); } catch (_) { /* not JSON: show as it is */ }
    }
    return x;
  }
  return JSON.stringify(x, null, 2);
}
const pre = (x) => h("pre", {}, pretty(x));

// Answers are Markdown: headings, bullet lists, **bold** and `code`. Built as DOM nodes, never as HTML, so a model's
// text cannot inject markup.
function inline(text) {
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`)/).filter(Boolean).map((part) =>
    part.startsWith("**") && part.endsWith("**") && part.length > 4 ? h("b", {}, part.slice(2, -2)) :
    part.startsWith("`") && part.endsWith("`") && part.length > 2 ? h("code", {}, part.slice(1, -1)) : part);
}
function md(text) {
  const out = [];
  let list = null, para = [];
  const flush = () => { if (para.length) { out.push(h("p", {}, inline(para.join(" ")))); para = []; } };
  for (const raw of String(text || "").split("\n")) {
    const line = raw.trimEnd();
    const head = line.match(/^#{1,4}\s+(.*)$/), item = line.match(/^\s*[-*]\s+(.*)$/);
    if (item) { flush(); if (!list) { list = h("ul", {}); out.push(list); } list.append(h("li", {}, inline(item[1]))); continue; }
    list = null;
    if (head) { flush(); out.push(h("h4", {}, inline(head[1]))); }
    else if (!line.trim()) flush();
    else para.push(line.trim());
  }
  flush();
  return h("div", { class: "md" }, out);
}

function fold(summary, body, opts) {
  const o = opts || {};
  return h("details", { class: o.cls || null, id: o.id || null, open: o.open || false },
           h("summary", {}, summary), h("div", { class: "body" }, body));
}
function badge(text, tone) { return h("span", { class: "badge " + (tone || "") }, text); }
const STOP_TONE = { done: "ok", interrupted: "warn", crashed: "bad", error: "bad", max_steps: "warn", waiting: "info",
                    unknown: "bad" };
const STATE_TONE = { succeeded: "ok", failed: "bad", cancelled: "warn", waiting: "info", running: "warn",
                     pending: "", fanned_out: "ok" };
const VERDICT_TONE = { approve: "ok", revise: "bad", unevaluated: "" };
function section(id, title, hint, ...body) {
  return h("section", { id }, h("h2", {}, title), hint ? h("p", { class: "hint" }, hint) : null, ...body);
}
function flash(id) {
  const el = document.getElementById(id);
  if (!el) return;
  if (el.tagName === "DETAILS") el.open = true;
  el.scrollIntoView({ behavior: "smooth", block: "start" });
  el.classList.add("flash");
  setTimeout(() => el.classList.remove("flash"), 1600);
}

// ── run page ───────────────────────────────────────────────────────────────
function renderRun(v) {
  const H = v.header;
  const byCall = new Map(v.calls.map((c) => [c.n, c]));
  const byWrite = new Map(v.writes.map((w) => [w.n, w]));
  const byNode = new Map(v.nodes.map((n) => [n.id, n]));
  const drawer = h("aside", { class: "drawer", id: "drawer" });
  document.title = `${H.stopped} · ${H.request.slice(0, 60)} · ${H.run_id}`;

  function callDetails(c, open) {
    const tone = !c.valid ? "bad" : c.fallback_from.length ? "warn" : "ok";
    const head = [badge(`call ${c.n}`, tone), h("b", {}, c.layer), c.node_id ? h("code", {}, c.node_id) : h("span", { class: "muted" }, "planner"),
                  h("span", { class: "muted" }, `round ${c.round} · ${clock(c.at)} · ${secs(c.seconds)} · ${k(c.usage.input_tokens)} in / ${k(c.usage.output_tokens)} out` +
                    (c.usage.thinking_tokens ? ` / ${k(c.usage.thinking_tokens)} thinking` : "")),
                  h("span", {}, c.answered_by || "no reply"), c.reused ? badge("reused on resume", "info") : null,
                  !c.valid ? badge("reply rejected", "bad") : null];
    const body = [
      c.fallback_from.length ? h("div", { class: "problem" }, h("div", { class: "title" }, "Tried first, could not answer:"), pre(c.fallback_from.join("\n"))) : null,
      c.error ? h("div", { class: c.valid ? "next" : "problem" }, h("div", { class: "title" }, c.valid ? "Note" : "Why the reply was rejected"), pre(c.error)) : null,
      fold("What the model was told (system prompt)", pre(c.system), { cls: "inner" }),
      ...c.messages.map((m, i) => fold(`Message ${i + 1} (${m.role === "user" ? "the question" : "earlier reply"})`, pre(m.text), { cls: "inner", open: i === c.messages.length - 1 })),
      c.response_schema ? fold("The answer shape it had to follow (JSON schema)", pre(c.response_schema), { cls: "inner" }) : null,
      h("h3", {}, "What it answered"), c.reply !== null ? pre(c.reply) : h("p", { class: "empty" }, "No reply."),
    ];
    return fold(head, body, { id: "call-" + c.n, open });
  }

  function writeRow(w) {
    const keys = Array.from(new Set([...Object.keys(w.fields || {}), ...Object.keys(w.before || {})])).filter((x) => x !== "id");
    const changes = keys.map((f) => h("div", { class: "change" }, h("code", {}, f), " ",
      f in (w.before || {}) ? h("span", { class: "old" }, JSON.stringify(w.before[f])) : null, " → ",
      h("span", { class: "new" }, JSON.stringify((w.fields || {})[f]))));
    const sent = w.dry_run ? badge("dry run, not sent", "info") : badge("sent", "ok");
    const st = w.status && !(w.dry_run && w.status === "completed") ? badge(w.status, w.status === "completed" ? "ok" : "bad") : null;
    return h("tr", { id: "write-" + w.n }, h("td", { class: "num" }, w.n), h("td", {}, h("code", {}, w.tool)),
      h("td", { class: "mono" }, w.row_id || "—"), h("td", {}, changes.length ? changes : pre(w.fields)),
      h("td", {}, sent, " ", st, w.error ? h("div", { class: "change old" }, w.error) : null),
      h("td", {}, w.node_id ? h("a", { href: "#", onclick: (e) => { e.preventDefault(); openNode(w.node_id); } }, w.node_id) : "—"),
      h("td", {}, w.undo ? badge(w.undo, w.undo.startsWith("failed") ? "bad" : "ok") : h("span", { class: "muted" }, "not undone")));
  }

  function openNode(id) {
    const n = byNode.get(id);
    if (!n) return;
    drawer.replaceChildren(
      h("button", { class: "close", onclick: () => drawer.classList.remove("open") }, "Close ✕"),
      h("h2", {}, n.id), h("div", {}, badge(n.state, STATE_TONE[n.state]), " ", h("code", {}, n.capability)),
      h("div", { class: "kv" },
        h("div", { class: "k" }, "Goal"), h("div", {}, n.goal_id || "—"),
        h("div", { class: "k" }, "Added by"), h("div", {}, n.added_by || "—"),
        h("div", { class: "k" }, "Started → ended"), h("div", {}, `${clock(n.started_at)} → ${clock(n.ended_at)} (${secs(n.seconds)})`),
        h("div", { class: "k" }, "Attempts"), h("div", {}, n.attempt),
        n.waiting_on ? h("div", { class: "k" }, "Waiting on") : null, n.waiting_on ? h("div", {}, n.waiting_on) : null),
      n.error ? h("div", { class: "problem" }, h("div", { class: "title" }, "What went wrong"), pre(n.error)) : null,
      h("h3", {}, "Input"), pre(n.input),
      h("h3", {}, "Result"), n.result ? pre(n.result) : h("p", { class: "empty" }, "No result."),
      h("h3", {}, `Model calls (${n.calls.length})`),
      n.calls.length ? n.calls.map((x) => callDetails(byCall.get(x), false)) : h("p", { class: "empty" }, "None."),
      h("h3", {}, `Writes (${n.writes.length})`),
      n.writes.length ? h("div", { class: "tablewrap" }, h("table", {}, h("tbody", {}, n.writes.map((x) => writeRow(byWrite.get(x)))))) : h("p", { class: "empty" }, "None."));
    drawer.classList.add("open");
    drawer.scrollTop = 0;
  }

  // summary
  const answered = Object.entries(H.served_by).map(([who, n]) => `${who} ×${n}`);
  const summary = section("summary", "Summary", null,
    h("div", {}, badge(H.stopped, STOP_TONE[H.stopped] || "warn"), " ",
      H.dry_run === true ? badge("dry run: nothing sent", "info") : H.dry_run === false ? badge("live: writes sent", "warn") : null),
    h("div", { class: "request" }, H.request),
    H.reason && H.stopped !== "done" ? h("p", {}, H.reason) : null,
    h("div", { class: "facts" },
      ...[["Book", H.instance], ["Mailboxes", H.mailboxes.join(", ") || "—"], ["Today (for the agent)", H.today || "—"],
          ["Started", time(H.started_at)], ["Took", secs(H.seconds)],
          ["Model calls", `${H.calls} · ${k(H.usage.input_tokens)} in / ${k(H.usage.output_tokens)} out`],
          ["Tasks", `${v.nodes.length} (${v.nodes.filter((n) => n.state === "succeeded").length} done)`],
          ["Writes", `${v.writes.length}`], ["Answered by", answered.join(", ") || "—"],
          ["First choice model", H.model || "—"]]
        .map(([a, b]) => h("div", { class: "fact" }, h("div", { class: "k" }, a), h("div", { class: "v" }, b)))),
    H.budgets.length ? h("div", { style: "margin-top:12px" }, h("div", { class: "muted" }, "Budgets (used / limit)"),
      H.budgets.map((b) => h("div", { class: "budget" }, h("span", { style: "width:130px" }, b.name),
        h("div", { class: "track" }, h("div", { class: "fill", style: `width:${Math.min(100, (100 * b.spent) / (b.limit || 1))}%` })),
        h("span", { class: "mono" }, `${b.spent} / ${b.limit}`)))) : null,
    H.next_step ? h("div", { class: "next" }, h("b", {}, "To continue: "), h("code", {}, H.next_step), " ",
      h("button", { onclick: () => navigator.clipboard && navigator.clipboard.writeText(H.next_step) }, "Copy")) : null,
    H.error ? h("div", { class: "errorbox" }, h("b", {}, `${H.error.type} in ${H.error.where}: `), H.error.message || "(no message)") : null,
    H.route ? h("p", { class: "muted" }, "Model order (first that answers is used): ", H.route) : null);

  // goals
  const goals = section("goals", "Goals and answers", "What the request was split into, and what came back for each.",
    v.goals.length ? h("div", { class: "cards" }, v.goals.map((g) => h("div", { class: "card" },
      h("div", { class: "top" }, h("span", { class: "title" }, g.id), g.skill ? h("span", { class: "chip" }, g.skill) : h("span", { class: "chip" }, "no skill"),
        g.refused ? badge("refused" + (g.refusal ? `: ${g.refusal}` : ""), "warn") : g.done ? badge("answered", "ok") : badge("not finished", "bad")),
      h("div", {}, g.text),
      g.answer ? h("div", { style: "margin-top:8px" }, md(g.answer)) : null)))
      : h("p", { class: "empty" }, "No goals were set."),
    v.answer && !v.goals.length ? md(v.answer) : null);

  // graph
  const cyBox = h("div", { id: "cy" });
  const showRounds = h("input", { type: "checkbox", checked: true, id: "rounds-toggle" });
  const legend = h("div", { class: "legend" },
    [["done", "ok"], ["failed", "bad"], ["cancelled", "warn"], ["waiting", "info"], ["never ran", "idle"], ["planner round", "round"]]
      .map(([t, c]) => h("span", {}, h("i", { style: `background:var(--${c === "round" ? "info" : c}-bg, var(--idle-bg));border-color:var(--${c === "round" ? "info" : c === "idle" ? "idle" : c})` }), t)));
  const taskTable = h("div", { class: "tablewrap" }, h("table", {},
    h("thead", {}, h("tr", {}, ["task", "type", "goal", "state", "time", "added by", "calls", "writes"].map((x) => h("th", {}, x)))),
    h("tbody", {}, v.nodes.map((n) => h("tr", { class: "click", onclick: () => openNode(n.id) },
      h("td", {}, h("code", {}, n.id)), h("td", {}, n.capability), h("td", {}, n.goal_id || "—"),
      h("td", {}, badge(n.state, STATE_TONE[n.state]), n.error ? h("div", { class: "change old" }, n.error.slice(0, 160)) : null),
      h("td", { class: "num" }, secs(n.seconds)), h("td", {}, n.added_by || "—"),
      h("td", { class: "num" }, n.calls.length), h("td", { class: "num" }, n.writes.length))))));
  const graph = section("graph", "Graph", "How the run was built: the planner adds tasks round by round (dotted lines); a judge task splits the mailbox into batches (dashed); solid lines mean \"runs after\". Click a task for its input, result, model calls and writes.",
    v.header.has_graph ? h("div", { class: "graphbar" }, legend, h("label", {}, showRounds, " show planner rounds"),
      h("button", { onclick: () => window.__cy && window.__cy.fit(undefined, 30) }, "Fit")) : null,
    v.header.has_graph ? cyBox : h("p", { class: "empty" }, "No graph: this run is from the old loop (before Revision 12), which kept no task graph."),
    h("h3", {}, `Tasks (${v.nodes.length})`), v.nodes.length ? taskTable : h("p", { class: "empty" }, "No tasks."));

  // planner rounds
  const rounds = section("rounds", "Planner rounds", "Each time the planner woke up: why, what it decided, and what it added.",
    v.rounds.length ? v.rounds.map((r) => fold([badge(`round ${r.n}`, r.rejected.length ? "warn" : "info"), h("span", { class: "muted" }, clock(r.at)),
      h("span", {}, "woken by ", h("code", {}, r.trigger)), r.finished ? badge("finished the run", "ok") : null,
      r.added.length ? h("span", { class: "muted" }, `added ${r.added.length}`) : null,
      r.rejected.length ? badge(`${r.rejected.length} plan(s) rejected`, "bad") : null],
      [h("p", {}, r.reason || h("span", { class: "empty" }, "No reason given.")),
       r.goals.length ? h("div", {}, h("b", {}, "Goals set: "), r.goals.map((g) => h("span", { class: "chip" }, `${g.id} → ${g.skill || "no skill" + (g.refusal ? ` (${g.refusal})` : "")}`))) : null,
       r.added.length ? h("div", {}, h("b", {}, "Added: "), r.added.map((a) => h("div", {}, h("code", {}, a)))) : null,
       r.cancelled.length ? h("div", {}, h("b", {}, "Cancelled: "), r.cancelled.map((a) => h("span", { class: "chip" }, a))) : null,
       r.rejected.map((why) => h("div", { class: "problem" }, h("div", { class: "title" }, "Rejected, asked again"), pre(why))),
       r.calls.length ? h("div", {}, h("b", {}, "Model calls: "), r.calls.map((n) => h("a", { href: "#call-" + n, onclick: (e) => { e.preventDefault(); flash("call-" + n); } }, `call ${n} `))) : null],
      { open: v.rounds.length <= 6 })) : h("p", { class: "empty" }, "No planner rounds were logged."));

  // model calls
  const calls = section("calls", "Model calls", "Every question sent to a model, in order: the full prompt, the full reply, who answered, and any failover. Click to unfold.",
    v.calls.length ? v.calls.map((c) => callDetails(c, false)) : h("p", { class: "empty" }, "No model calls."));

  // writes
  const writes = section("writes", "Writes", "What the run set on the platform (or would have, in a dry run): each field's value before → after.",
    v.writes.length ? h("div", { class: "tablewrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["#", "tool", "row", "change", "sent?", "task", "undo"].map((x) => h("th", {}, x)))),
      h("tbody", {}, v.writes.map(writeRow)))) : h("p", { class: "empty" }, "Nothing was written."));

  // checks
  const checks = section("checks", "Checks", "The evidence check before each answer, the second model's check of the verdicts, and the answer score.",
    h("h3", {}, "Evidence check"), v.critics.length ? h("div", { class: "cards" }, v.critics.map((c) => h("div", { class: "card" + (c.ready ? "" : " bad") },
      h("div", { class: "top" }, badge(c.ready ? "ready" : "not ready", c.ready ? "ok" : "bad"), c.overruled ? badge("overruled", "warn") : null,
        h("code", {}, c.node_id || "—"), h("span", { class: "muted" }, `${c.goal_id || ""} · ${clock(c.at)}`)),
      c.ending ? h("div", {}, h("b", {}, "Ending checked: "), c.ending) : null, h("div", { class: "muted" }, c.reason),
      c.missing.length ? h("div", {}, h("b", {}, "Missing: "), c.missing.join("; ")) : null)))
      : h("p", { class: "empty" }, "None in this run."),
    h("h3", {}, "Second model's check"), v.validators.length ? h("div", { class: "cards" }, v.validators.map((x) => h("div", { class: "card" + (x.held.length || x.possible_misses.length ? " bad" : "") },
      h("div", { class: "top" }, badge(`${x.agreed}/${x.checked} agreed`, x.agreed === x.checked ? "ok" : "warn"), h("code", {}, x.node_id || "—"), h("span", { class: "chip" }, x.skill || "")),
      h("div", { class: "muted" }, `judged by ${x.judge_model || "?"} · checked by ${x.validator_model || "?"}`),
      x.held.length ? fold(`${x.held.length} verdict(s) held back (not written)`, pre(x.held), { cls: "inner" }) : null,
      x.possible_misses.length ? fold(`${x.possible_misses.length} possible miss(es)`, pre(x.possible_misses), { cls: "inner" }) : null)))
      : h("p", { class: "empty" }, "None in this run."),
    h("h3", {}, "Answer score"), v.scores.length ? h("div", { class: "cards" }, v.scores.map((s) => h("div", { class: "card" },
      h("div", { class: "top" }, badge(s.score === null ? "no score" : `${s.score}/100`, s.score >= 80 ? "ok" : "warn"), h("code", {}, s.node_id || "—")),
      s.issues.length ? h("ul", {}, s.issues.map((i) => h("li", {}, i))) : h("div", { class: "muted" }, "No issues."))))
      : h("p", { class: "empty" }, "None in this run."));

  // problems
  function refLink(p) {
    if (!p.ref) return null;
    if (p.where === "task" || p.where === "check") return byNode.has(p.ref) ? h("a", { href: "#", onclick: (e) => { e.preventDefault(); openNode(p.ref); } }, p.ref) : h("code", {}, p.ref);
    if (p.where === "call") return h("a", { href: "#", onclick: (e) => { e.preventDefault(); flash("call-" + p.ref.split(" ")[1]); } }, p.ref);
    if (p.where === "write") return h("a", { href: "#", onclick: (e) => { e.preventDefault(); flash("write-" + p.ref.split(" ")[1]); } }, p.ref);
    return h("code", {}, p.ref);
  }
  const problems = section("problems", "What went wrong", "Everything that failed, was rejected, fell back or was held, in one list.",
    v.problems.length ? v.problems.map((p) => h("div", { class: "problem" },
      h("div", { class: "title" }, badge(p.where, "bad"), " ", p.title, " ", refLink(p)), p.detail ? pre(p.detail) : null))
      : h("div", { class: "allgood" }, "Nothing went wrong in this run."));

  // timeline
  const total = Math.max(1, ...v.spans.map((s) => s.end_ms));
  const timeline = section("timeline", "Timeline", `When each part ran (total ${secs(total / 1000)}). Grey: the run · teal: planner round · blue: task · purple: model call · orange: write · red: failed.`,
    v.spans.length ? h("div", { class: "tl" }, v.spans.map((s) => h("div", { class: "row", title: `${s.name}\n${secs((s.end_ms - s.start_ms) / 1000)}${s.message ? "\n" + s.message : ""}` },
      h("div", { class: "name", style: `padding-left:${s.depth * 12}px` }, s.name),
      h("div", { class: "lane" }, h("div", { class: `bar ${s.status === "error" ? "error" : s.kind}`, style: `left:${(100 * s.start_ms) / total}%;width:${Math.max(0.15, (100 * (s.end_ms - s.start_ms)) / total)}%` })),
      h("div", { class: "dur" }, secs((s.end_ms - s.start_ms) / 1000)))))
      : h("p", { class: "empty" }, "No timeline (an old-loop run, or the trace could not be read)."));

  // memory and setup
  const memory = section("memory", "Memory and setup", "What the planner saw from memory, and how the local mailbox copy was brought up to date.",
    h("h3", {}, "House rules"), v.house_rules ? pre(v.house_rules) : h("p", { class: "empty" }, "None for this book."),
    h("h3", {}, "Earlier runs the planner saw"), v.history.length ? h("ul", {}, v.history.map((x) => h("li", {}, x))) : h("p", { class: "empty" }, "None."),
    h("h3", {}, "Local mailbox copy"), v.sync ? h("div", {},
      h("p", {}, `${v.sync.calls} call(s) · ${secs(v.sync.seconds)} · ${v.sync.threads_total} conversations / ${v.sync.messages_total} messages · ${v.sync.facts_recomputed} conversations re-worked`),
      h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["table", "mailbox", "full?", "calls", "fetched", "changed", "deleted"].map((x) => h("th", {}, x)))),
        h("tbody", {}, v.sync.tables.map((t) => h("tr", {}, h("td", {}, t.entity), h("td", {}, t.mailbox || "—"), h("td", {}, t.full ? "full" : "incremental"),
          h("td", { class: "num" }, t.calls), h("td", { class: "num" }, t.fetched), h("td", { class: "num" }, t.changed), h("td", { class: "num" }, t.deleted)))))))
      : h("p", { class: "empty" }, "Not synced in this run."),
    v.older_lines.length ? fold(`${v.older_lines.length} log line(s) in the old loop's format`, pre(v.older_lines), { cls: "inner" }) : null,
    h("p", { class: "muted" }, `Page made ${time(v.generated_at)} from the files in the run folder.`));

  const nav = h("nav", { class: "side" }, h("div", { class: "brand" }, "Email agent run"), h("div", { class: "sub" }, H.run_id),
    [["summary", "Summary", ""], ["problems", "What went wrong", v.problems.length], ["goals", "Goals and answers", v.goals.length],
     ["graph", "Graph", v.nodes.length], ["rounds", "Planner rounds", v.rounds.length], ["calls", "Model calls", v.calls.length],
     ["writes", "Writes", v.writes.length], ["checks", "Checks", v.critics.length + v.validators.length + v.scores.length],
     ["timeline", "Timeline", ""], ["memory", "Memory and setup", ""]]
      .map(([id, t, n]) => h("a", { href: "#" + id }, t, h("span", { class: "count" + (id === "problems" && n ? " bad" : "") }, n))));
  document.getElementById("app").replaceChildren(h("div", { class: "layout" }, nav,
    h("main", {}, summary, problems, goals, graph, rounds, calls, writes, checks, timeline, memory)), drawer);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") drawer.classList.remove("open"); });

  if (v.header.has_graph) drawGraph(v, cyBox, showRounds, openNode);
}

function drawGraph(v, box, toggle, openNode) {
  if (typeof window.cytoscape !== "function") {
    box.classList.add("nograph");
    box.textContent = "The graph needs internet (Cytoscape.js loads from a CDN). The task table below has everything.";
    return;
  }
  const css = getComputedStyle(document.documentElement);
  const c = (name) => css.getPropertyValue(name).trim();
  const tone = { succeeded: ["--ok-bg", "--ok"], failed: ["--bad-bg", "--bad"], cancelled: ["--warn-bg", "--warn"],
                 waiting: ["--info-bg", "--info"], running: ["--warn-bg", "--warn"], pending: ["--idle-bg", "--idle"] };
  const els = [];
  const hasParent = new Set(v.edges.map(([, child]) => child));
  for (const n of v.nodes) {
    const [bg, border] = tone[n.state] || tone.pending;
    els.push({ data: { id: n.id, label: `${n.id}\n${n.capability}${n.goal_id ? " · " + n.goal_id : ""}`, bg: c(bg), border: c(border) }, classes: "task" });
  }
  for (const [a, b] of v.edges) els.push({ data: { id: `e:${a}>${b}`, source: a, target: b }, classes: "after" });
  for (const n of v.nodes) {
    if (n.fanned_from && !hasParent.has(n.id)) els.push({ data: { id: `f:${n.fanned_from}>${n.id}`, source: n.fanned_from, target: n.id }, classes: "fan" });
  }
  const roundEls = [];
  for (const r of v.rounds) {
    const added = v.nodes.filter((n) => n.added_by === `planner round ${r.n}`);
    if (!added.length && !r.finished) continue;
    roundEls.push({ data: { id: `round:${r.n}`, label: `planner\nround ${r.n}${r.finished ? " ✓ finish" : ""}`, bg: c("--info-bg"), border: c("--info") }, classes: "round" });
    for (const n of added) roundEls.push({ data: { id: `r:${r.n}>${n.id}`, source: `round:${r.n}`, target: n.id }, classes: "added" });
  }
  for (let i = 1; i < v.rounds.length; i++) {
    const a = `round:${v.rounds[i - 1].n}`, b = `round:${v.rounds[i].n}`;
    if (roundEls.some((e) => e.data.id === a) && roundEls.some((e) => e.data.id === b)) roundEls.push({ data: { id: `rr:${a}>${b}`, source: a, target: b }, classes: "next" });
  }
  const cy = window.cytoscape({
    container: box, elements: els.concat(roundEls), wheelSensitivity: 0.25,
    style: [
      { selector: "node", style: { shape: "round-rectangle", "background-color": "data(bg)", "border-color": "data(border)", "border-width": 2,
        label: "data(label)", "text-wrap": "wrap", "text-valign": "center", "text-halign": "center", "font-size": 13, color: c("--text"),
        width: 190, height: 50 } },
      { selector: "node.round", style: { shape: "round-diamond", width: 110, height: 60, "font-size": 10 } },
      { selector: "edge", style: { width: 1.6, "curve-style": "bezier", "target-arrow-shape": "triangle", "line-color": c("--muted"), "target-arrow-color": c("--muted") } },
      { selector: "edge.fan", style: { "line-style": "dashed", "line-color": c("--accent"), "target-arrow-color": c("--accent") } },
      { selector: "edge.added", style: { "line-style": "dotted", "line-color": c("--info"), "target-arrow-color": c("--info"), width: 1.2 } },
      { selector: "edge.next", style: { "line-color": c("--info"), "target-arrow-color": c("--info"), width: 1 } },
      { selector: "node:selected", style: { "border-width": 4, "border-color": c("--accent") } },
    ],
  });
  window.__cy = cy;
  const layout = () => cy.elements(":visible").layout({ name: typeof window.cytoscapeDagre === "function" ? "dagre" : "breadthfirst",
                                                       rankDir: "LR", nodeSep: 18, rankSep: 70, edgeSep: 8, directed: true, padding: 20, fit: true }).run();
  layout();
  cy.on("tap", "node.task", (e) => openNode(e.target.id()));
  toggle.addEventListener("change", () => {
    cy.$(".round, .added, .next").style("display", toggle.checked ? "element" : "none");
    layout();
  });
}

// ── batch page ─────────────────────────────────────────────────────────────
function renderBatch(b) {
  document.title = `Harness batch ${b.batch}`;
  const counts = Object.entries(b.counts).map(([s, n]) => badge(`${n} ${s}`, VERDICT_TONE[s]));
  const rows = b.rows.map((r) => h("tr", {},
    h("td", {}, h("b", {}, r.task_id)), h("td", {}, r.instance), h("td", {}, badge(r.status, VERDICT_TONE[r.status])),
    h("td", {}, r.reason, r.checks.length > 1 ? fold(`${r.checks.length} checks`, pre(r.checks), { cls: "inner" }) : null),
    h("td", {}, r.run_stopped ? badge(r.run_stopped, STOP_TONE[r.run_stopped] || "warn") : "—"),
    h("td", {}, r.served_by || "—"),
    h("td", {}, r.page ? h("a", { href: r.page }, "open run page →") : h("span", { class: "muted" }, r.run_id ? "no page" : "no run"))));
  document.getElementById("app").replaceChildren(h("main", { style: "margin:0 auto" },
    h("section", {}, h("h2", {}, `Harness batch ${b.batch}`),
      h("p", { class: "hint" }, `Scored ${time(b.scored_at)}. Each task's checks ran against the database after its run; "unevaluated" means the answer key is not marked yet, never a pass.`),
      h("div", {}, counts)),
    h("section", {}, h("div", { class: "tablewrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["task", "book", "verdict", "why", "run", "answered by", ""].map((x) => h("th", {}, x)))),
      h("tbody", {}, rows)))),
    h("p", { class: "muted" }, `Page made ${time(b.generated_at)}.`)));
}

document.addEventListener("DOMContentLoaded", () => {
  let data;
  try {
    data = JSON.parse(document.getElementById("page-data").textContent);
  } catch (err) {
    document.getElementById("app").textContent = "This page's data could not be read: " + err;
    return;
  }
  if (data.kind === "batch") renderBatch(data);
  else renderRun(data);
});
