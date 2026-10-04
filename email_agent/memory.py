"""Memory for one run (no LLM): the typed history of answers and tool outcomes.

`read(goal)` returns what Decision should see: everything done for this goal, plus the last few
items of other goals. Long-term memory on the platform (AgentMemory) is reached through skill
tools; a vector memory like S7's FAISS index is Phase 2.
"""

from __future__ import annotations

from email_agent.contracts.agent import ActionResult, Goal, HistoryItem

OTHER_GOALS_KEPT = 3


class RunMemory:
    def __init__(self) -> None:
        self.items: list[HistoryItem] = []

    def read(self, goal: Goal | None = None) -> list[HistoryItem]:
        if goal is None:
            return list(self.items)
        mine = [h for h in self.items if h.goal_id == goal.id]
        others = [h for h in self.items if h.goal_id != goal.id][-OTHER_GOALS_KEPT:]
        return others + mine

    def record_answer(self, it: int, goal: Goal, answer: str) -> HistoryItem:
        item = HistoryItem(iter=it, goal_id=goal.id, kind="answer", text=answer)
        self.items.append(item)
        return item

    def record_action(self, it: int, goal: Goal, result: ActionResult) -> HistoryItem:
        text = result.preview if result.ok else f"FAILED ({result.kind}): {result.message}"
        item = HistoryItem(iter=it, goal_id=goal.id, kind="action", tool=result.tool,
                           arguments=result.arguments, ok=result.ok, text=text)
        self.items.append(item)
        return item
