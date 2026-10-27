"""Historical decision policy retained for deterministic recording replay."""
from __future__ import annotations

from ..domain import Action, transition
from ..planning_core import (Budget, Context, Decision, Exhausted, search, validate,
                             legacy_explore as explore)


def decide(ctx: Context, previous: list[Action], policy: str, budget: Budget) -> Decision:
    result = Decision()
    try:
        if previous:
            valid, dirty, reason = validate(ctx, previous, budget)
            if valid:
                return Decision(previous, "retained", reason, retained=len(previous))
            result.reason = reason
            if policy == "Local":
                # Replace [0,r): completed actions are outside the remaining plan.
                # Whole-suffix validation is charged for every proposed splice.
                local_end = min(budget.limit, budget.used + max(1, (budget.limit-budget.used)//4))
                for r in sorted(set([min(len(previous), dirty + 1),
                                     min(len(previous), dirty + 3), len(previous)])):
                    suffix = previous[r:]
                    allowance = local_end - budget.used
                    if allowance <= 0:
                        break
                    def accept(s, path):
                        if budget.used >= local_end:
                            return False
                        ok, _, _ = validate(ctx, path + suffix, budget)
                        return ok
                    original_limit = budget.limit
                    try:
                        budget.limit = local_end
                        patch = search(ctx.state, ctx.goal, budget, accept, allowance)
                    except Exhausted:
                        patch = None
                    finally:
                        budget.limit = original_limit
                    if patch is not None:
                        return Decision(patch + suffix, "patched", reason, r, len(suffix), [0, r])
        path = search(ctx.state, ctx.goal, budget)
        if path is not None:
            valid, index, reason = validate(ctx, path, budget)
            if valid:
                return Decision(path, "fallback_full" if previous and policy == "Local" else "full",
                                result.reason or reason, len(previous))
            # Expiring remote evidence: certify only a prefix up to an observation.
            prefix = path[:index]
            s = ctx.state
            for a in prefix:
                s = transition(s, a, ctx.goal)
            return Decision(prefix + [Action(kind="inspect", target=s.location)],
                            "observe", reason, len(previous))
        observation = explore(ctx, budget)
        if observation:
            return Decision(observation, "observe", "unknown facts", len(previous))
        return Decision([], "unknown", "no_plan_in_current_belief", len(previous))
    except Exhausted:
        return Decision([], "budget_exhausted", "shared decision budget exhausted", len(previous))
