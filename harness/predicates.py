"""Predicates: plain code that decides approve / revise / unevaluated from the DATABASE.

Each one reads the database state after the run (a DbSnapshot), the saved run (what it wrote, what was there
before, the server time it started), your answer key, and — for refusals — the run's structured goal list
(`final.json`: which goals were refused and why). None of them reads the agent's answer text. A check that
cannot decide returns `unevaluated`, which is never a pass. A task with several checks gets the worst verdict.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from email_agent.config import PROJECT_ROOT
from email_agent.contracts.agent import FinalAnswer, RunContext, RunOutcome, WriteRecord
from harness.contracts import (CheckResult, FollowUpAnswerKey, GroundTruth, PredicateName, PriceAnswerKey,
                               SavedRun, SortAnswerKey, ThreadFlag, VerdictStatus)
from harness.db import DbSnapshot, our_changes_since, when

GROUND_TRUTH = PROJECT_ROOT / "harness" / "ground_truth"
KeyT = TypeVar("KeyT", bound=BaseModel)


class Unevaluated(Exception):
    """Raised inside a check when it cannot decide; turned into an `unevaluated` result."""


def _result(name: PredicateName, status: VerdictStatus, reason: str, **details: list[str]) -> CheckResult:
    return CheckResult(name=name, status=status, reason=reason, details={k: v for k, v in details.items() if v})


# ── what every check needs ───────────────────────────────────────────────────

def _writes(run_dir: str) -> list[WriteRecord]:
    path = Path(run_dir) / "writes.jsonl"
    return [WriteRecord.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()]


def _dry_run(saved: SavedRun) -> bool:
    """Saved by the harness (since 2026-10-03), or recorded by the agent in outcome.json — so a dry run
    that wrote nothing is still recognised, also in batches saved before SavedRun.dry_run existed."""
    if saved.dry_run:
        return True
    path = Path(saved.run_dir) / "outcome.json" if saved.run_dir else None
    return bool(path and path.exists() and RunOutcome.model_validate_json(path.read_text()).dry_run)


def _completed(saved: SavedRun) -> None:
    if saved.error or not saved.run_dir:            # a crash is never judged (your choice, 2026-10-03)
        see = f" — see {saved.run_dir}/report.md" if saved.run_dir else ""
        raise Unevaluated(f"the run did not complete: {saved.error or 'no run directory'}{see}")


def _live(saved: SavedRun) -> list[WriteRecord]:
    _completed(saved)
    writes = _writes(saved.run_dir)
    if _dry_run(saved) or any(w.dry_run for w in writes):   # a dry run with nothing to write must not pass either
        raise Unevaluated("dry run: nothing was written, so there is nothing to check")
    return writes


def _scope(saved: SavedRun) -> list[str]:
    """The mailboxes the run worked in (its context.json)."""
    path = Path(saved.run_dir) / "context.json"
    if not path.exists():
        raise Unevaluated("the run has no context.json, so its mailboxes are unknown")
    return [m.email for m in RunContext.model_validate_json(path.read_text()).mailboxes]


def _scope_ids(saved: SavedRun) -> set[str]:
    path = Path(saved.run_dir) / "context.json"
    return {m.id for m in RunContext.model_validate_json(path.read_text()).mailboxes}


def _load_key(path: Path, model: type[KeyT]) -> KeyT:
    if not path.exists():
        raise Unevaluated(f"no answer key: {path.relative_to(PROJECT_ROOT)} is missing "
                          "(scripts/propose_ground_truth.py writes it; you decide each item)")
    try:
        return model.model_validate(yaml.safe_load(path.read_text()))
    except (ValidationError, yaml.YAMLError) as e:
        raise Unevaluated(f"answer key {path.name} does not validate: {e}") from e


def _covers(key_mailboxes: list[str], scope: list[str], key_name: str) -> None:
    missing = sorted(set(scope) - set(key_mailboxes))
    if missing:
        raise Unevaluated(f"the run worked in {missing}, which the {key_name} answer key does not cover yet; "
                          "re-run scripts/propose_ground_truth.py for them and decide the new items")


def _by_run(writes: list[WriteRecord], tool: str) -> list[WriteRecord]:
    return [w for w in writes if w.tool == tool and w.row_id]


def _guarded(name: PredicateName, check: Callable[[], CheckResult]) -> CheckResult:
    try:
        return check()
    except Unevaluated as e:
        return _result(name, "unevaluated", str(e))


# ── triage: needs_reply_flagged ──────────────────────────────────────────────

def _flagged_by(flag: ThreadFlag | None, today: str) -> bool:
    return bool(flag and flag.flag_status == "flagged" and flag.flag_due_date and flag.flag_due_date[:10] <= today)


def needs_reply_flagged(saved: SavedRun, snap: DbSnapshot, params: dict[str, Any]) -> CheckResult:
    name: PredicateName = "needs_reply_flagged"
    gt = _load_key(GROUND_TRUTH / f"{saved.task.instance}.yaml", GroundTruth)
    _completed(saved)
    scope = _scope(saved)
    _covers(gt.our_mailboxes, scope, "needs-reply")
    in_scope = [c for c in gt.needs_reply if gt.mailbox_of(c) in scope]
    undecided = [c.thread_id for c in in_scope if c.needs_reply is None]
    if undecided:
        raise Unevaluated(f"{len(undecided)} ground-truth items still undecided (needs_reply: null)")
    writes = _live(saved)
    today = saved.today.isoformat()
    after = {t.id: t for t in snap.threads}
    expected = {c.thread_id for c in in_scope if c.needs_reply}
    flagged_by_run = {w.row_id for w in _by_run(writes, "EmailThread.update") if w.fields.get("flag_status") == "flagged"}

    def now(thread_id: str) -> ThreadFlag | None:
        t = after.get(thread_id)
        return ThreadFlag(flag_status=t.flag_status, flag_due_date=t.flag_due_date) if t else None

    missing = sorted(t for t in expected
                     if not _flagged_by(now(t), today)
                     or not (t in flagged_by_run or _flagged_by(saved.before.get(t), today)))
    extra = sorted(flagged_by_run - expected)
    if missing or extra:
        return _result(name, "revise", f"{len(missing)} conversation(s) that need a reply are not flagged; "
                       f"{len(extra)} flagged that do not need one", missing=missing, extra=extra)
    return _result(name, "approve", f"all {len(expected)} conversations that need a reply are flagged with a due "
                   f"date on or before {today}; nothing else was flagged", flagged_by_run=sorted(flagged_by_run & expected))


# ── price: price_agreements_recorded ─────────────────────────────────────────

def _price_line(content: str) -> dict[str, str]:
    """The machine-readable first line record_price_agreements writes: `price-agreement | k=v | …`."""
    head = (content or "").splitlines()[0] if content else ""
    if not head.startswith("price-agreement"):
        return {}
    return dict(part.strip().split("=", 1) for part in head.split("|")[1:] if "=" in part)


def price_agreements_recorded(saved: SavedRun, snap: DbSnapshot, params: dict[str, Any]) -> CheckResult:
    name: PredicateName = "price_agreements_recorded"
    key = _load_key(GROUND_TRUTH / "price" / f"{saved.task.instance}.yaml", PriceAnswerKey)
    _completed(saved)
    scope = _scope(saved)
    _covers(key.our_mailboxes, scope, "price")
    in_scope = [c for c in key.candidates if c.mailbox in scope]
    undecided = [c.thread_id for c in in_scope if c.agreed is None]
    if undecided:
        raise Unevaluated(f"{len(undecided)} price answer-key items still undecided (agreed: null)")
    _live(saved)
    expected = {c.thread_id: c for c in in_scope if c.agreed}
    rows = {}
    for m in snap.memories:
        fields = _price_line(m.content or "")
        if fields.get("run") == saved.run_id and m.created_by == saved.me_id:
            rows[fields.get("thread", "")] = fields
    missing = sorted(set(expected) - set(rows))
    extra = sorted(set(rows) - set(expected))
    wrong = []
    for thread_id, c in expected.items():
        got = rows.get(thread_id)
        if not got:
            continue
        for field, want in (("unit", c.unit_price), ("total", c.total)):
            if want is None:
                continue
            try:
                ok = abs(float(got.get(field, "")) - want) <= 0.01
            except ValueError:
                ok = False
            if not ok:
                wrong.append(f"{thread_id} {field}={got.get(field)} (key {want})")
        if c.reference and got.get("ref") != c.reference:
            wrong.append(f"{thread_id} ref={got.get('ref')} (key {c.reference})")
    unstarred = sorted(t for t in expected if not (snap.thread(t) and snap.thread(t).is_starred))
    if missing or extra or wrong or unstarred:
        return _result(name, "revise", f"{len(missing)} agreement(s) not recorded, {len(extra)} recorded that are "
                       f"not agreements, {len(wrong)} wrong figure(s), {len(unstarred)} not starred",
                       missing=missing, extra=extra, wrong=wrong, not_starred=unstarred)
    return _result(name, "approve", f"all {len(expected)} agreed prices are recorded in agent memory with the right "
                   "figures, and their conversations are starred; nothing else was recorded",
                   recorded=sorted(rows))


# ── summaries_written ────────────────────────────────────────────────────────

SUMMARY_FIELDS = {"summary", "summary_updated_at"}


def summaries_written(saved: SavedRun, snap: DbSnapshot, params: dict[str, Any]) -> CheckResult:
    name: PredicateName = "summaries_written"
    writes = _live(saved)
    ids = _scope_ids(saved)
    threads = [t for t in snap.threads if t.mailbox_id in ids]
    by_run = _by_run(writes, "EmailThread.update")
    other_fields = sorted({f"{w.row_id}: {k}" for w in by_run for k in w.fields if k not in SUMMARY_FIELDS})
    missing, stale, bad_length = [], [], []
    for t in threads:
        if not t.summary:
            missing.append(t.id)
            continue
        if not 20 <= len(t.summary) <= 400:
            bad_length.append(f"{t.id} ({len(t.summary)} characters)")
        newest = snap.newest_message_at(t.id)
        if newest and (not t.summary_updated_at or t.summary_updated_at[:10] < newest[:10]):
            stale.append(t.id)
    if missing or stale or bad_length or other_fields:
        return _result(name, "revise", f"{len(missing)} conversation(s) without a summary, {len(stale)} with an "
                       f"older summary, {len(bad_length)} too short or long, {len(other_fields)} other field(s) "
                       "written", missing=missing, stale=stale, bad_length=bad_length, other_fields=other_fields)
    return _result(name, "approve", f"all {len(threads)} conversations have a current summary "
                   f"({len(by_run)} written by this run); no other field was written",
                   written_by_run=sorted({w.row_id for w in by_run}))


# ── followups_created ────────────────────────────────────────────────────────

def followups_created(saved: SavedRun, snap: DbSnapshot, params: dict[str, Any]) -> CheckResult:
    name: PredicateName = "followups_created"
    key = _load_key(GROUND_TRUTH / "follow-ups" / f"{saved.task.instance}.yaml", FollowUpAnswerKey)
    _completed(saved)
    scope = _scope(saved)
    _covers(key.our_mailboxes, scope, "follow-ups")
    in_scope = [c for c in key.candidates if c.mailbox in scope]
    undecided = [c.thread_id for c in in_scope if c.needs_follow_up is None]
    if undecided:
        raise Unevaluated(f"{len(undecided)} follow-up answer-key items still undecided (needs_follow_up: null)")
    writes = _live(saved)
    today = saved.today.isoformat()
    expected = {c.thread_id for c in in_scope if c.needs_follow_up}
    ours = [r for r in snap.reminders if r.created_by == saved.me_id and r.type == "follow_up" and not r.is_fired]
    covered = {r.thread_id for r in ours if (r.remind_at or "")[:10] > today}
    created = {w.row_id for w in _by_run(writes, "EmailReminder.create")}
    created_threads = {r.thread_id for r in snap.reminders if r.id in created}
    missing = sorted(expected - covered)
    extra = sorted(created_threads - expected)
    if missing or extra:
        return _result(name, "revise", f"{len(missing)} conversation(s) waiting on them have no follow-up reminder "
                       f"after today; {len(extra)} reminder(s) created where none belongs", missing=missing, extra=extra)
    return _result(name, "approve", f"all {len(expected)} conversations waiting on them have a follow-up reminder "
                   f"after {today}; none was created elsewhere", created_by_run=sorted(created_threads))


# ── memory_recorded ──────────────────────────────────────────────────────────

def memory_recorded(saved: SavedRun, snap: DbSnapshot, params: dict[str, Any]) -> CheckResult:
    """params: party (a name or part of it), categories (any of them is right), contains (words that must appear)."""
    name: PredicateName = "memory_recorded"
    _live(saved)
    if not saved.started_at_server or not saved.me_id:
        raise Unevaluated("the saved run has no server start time or user id (saved before 2026-10-03)")
    party = str(params.get("party", "")).lower()
    party_ids = {p.id for p in snap.parties if party and party in (p.name or p.company_name or "").lower()}
    if not party_ids:
        raise Unevaluated(f"no party named like {params.get('party')!r} on {saved.task.instance}")
    words = [str(w).lower() for w in params.get("contains", [])]
    found = [m for m in snap.memories if m.created_by == saved.me_id and (when(m.created_at) or saved.started_at_server)
             >= saved.started_at_server and m.party_id in party_ids and m.is_active is not False]
    categories = set(params.get("categories", []))
    right = [m for m in found if (not categories or m.category in categories)
             and all(w in (m.content or "").lower() for w in words)]
    if not right:
        return _result(name, "revise", f"no memory of ours since the run started for {params.get('party')!r} with "
                       f"a category in {sorted(categories)} containing {words}",
                       found=[f"{m.id} {m.category}: {(m.content or '')[:80]}" for m in found])
    return _result(name, "approve", f"memory saved for {params.get('party')!r} ({right[0].category}) "
                   f"containing {words}", rows=[m.id for m in right])


# ── inbox_sorted ─────────────────────────────────────────────────────────────

SORT_FIELDS = {"importance", "split_category"}


def inbox_sorted(saved: SavedRun, snap: DbSnapshot, params: dict[str, Any]) -> CheckResult:
    name: PredicateName = "inbox_sorted"
    key = _load_key(GROUND_TRUTH / "sort" / f"{saved.task.instance}.yaml", SortAnswerKey)
    _completed(saved)
    scope = _scope(saved)
    _covers(key.our_mailboxes, scope, "sort")
    in_scope = [c for c in key.candidates if c.mailbox in scope]
    undecided = [c.thread_id for c in in_scope if c.importance is None or c.split_category is None]
    if undecided:
        raise Unevaluated(f"{len(undecided)} sort answer-key items still undecided (importance or split_category null)")
    writes = _live(saved)
    other_fields = sorted({f"{w.row_id}: {k}" for w in _by_run(writes, "EmailThread.update") for k in w.fields
                           if k not in SORT_FIELDS})
    wrong = []
    for c in in_scope:
        t = snap.thread(c.thread_id)
        if t is None:
            continue
        for field, want in (("importance", c.importance), ("split_category", c.split_category)):
            if want != "any" and getattr(t, field) != want:
                wrong.append(f"{c.thread_id} {field}={getattr(t, field)} (key {want})")
    if wrong or other_fields:
        return _result(name, "revise", f"{len(wrong)} value(s) differ from the answer key; {len(other_fields)} other "
                       "field(s) written", wrong=wrong, other_fields=other_fields)
    return _result(name, "approve", f"all {len(in_scope)} conversations have the importance and category in the "
                   "answer key; no other field was written")


# ── refused_without_writes ───────────────────────────────────────────────────

def refused_without_writes(saved: SavedRun, snap: DbSnapshot, params: dict[str, Any]) -> CheckResult:
    """params: reasons (accepted refusal reasons), min_refused (default 1), no_writes (default true; false for a
    mixed request whose other goal is judged by its own check). Reads the run's structured goals (final.json:
    refused + reason), never its answer text, and confirms "no writes" from the database."""
    name: PredicateName = "refused_without_writes"
    _completed(saved)
    final_path = Path(saved.run_dir) / "final.json"
    if not final_path.exists():
        raise Unevaluated("the run has no final.json")
    final = FinalAnswer.model_validate_json(final_path.read_text())
    reasons = set(params.get("reasons", []))
    refused = [g for g in final.goals if g.refused and (not reasons or g.refusal in reasons)]
    others = [f"{g.id}: {g.refusal}" for g in final.goals if g.refused and g not in refused]
    problems: dict[str, list[str]] = {}
    if len(refused) < int(params.get("min_refused", 1)):
        problems["not_refused"] = [f"{g.id} refused={g.refused} reason={g.refusal}" for g in final.goals] or ["no goals"]
    if others:
        problems["other_reason"] = others
    if params.get("no_writes", True):
        writes = _writes(saved.run_dir)
        if writes:
            problems["run_wrote"] = [f"{w.tool} {w.row_id or ''}{' (dry run)' if w.dry_run else ''}" for w in writes]
        if not saved.started_at_server or not saved.me_id:
            raise Unevaluated("the saved run has no server start time or user id, so the database cannot confirm "
                              "that nothing was written")
        changed = our_changes_since(snap, saved.me_id, saved.started_at_server)
        if changed:
            problems["database_changed"] = changed
    if problems:
        return _result(name, "revise", "the request was not refused cleanly: " + ", ".join(problems), **problems)
    return _result(name, "approve", f"{len(refused)} goal(s) refused ({', '.join(sorted({str(g.refusal) for g in refused}))})"
                   + ("; nothing written by the run or seen changed in the database" if params.get("no_writes", True)
                      else ""), refused=[g.id for g in refused])


PREDICATES: dict[str, Callable[[SavedRun, DbSnapshot, dict[str, Any]], CheckResult]] = {
    "needs_reply_flagged": needs_reply_flagged,
    "price_agreements_recorded": price_agreements_recorded,
    "summaries_written": summaries_written,
    "followups_created": followups_created,
    "memory_recorded": memory_recorded,
    "inbox_sorted": inbox_sorted,
    "refused_without_writes": refused_without_writes,
}
ORDER = {"revise": 2, "unevaluated": 1, "approve": 0}


def check_all(saved: SavedRun, snap: DbSnapshot) -> list[CheckResult]:
    return [_guarded(spec.name, lambda spec=spec: PREDICATES[spec.name](saved, snap, spec.params))
            for spec in saved.task.predicates]


def worst(results: list[CheckResult]) -> VerdictStatus:
    return max((r.status for r in results), key=lambda s: ORDER[s], default="unevaluated")
