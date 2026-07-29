ISOLATED_STOCK_STRATEGY_IDS = frozenset(
    {
        "plunge_bounce_strategy",
        "heikin_ashi_scalping_strategy",
    }
)

AI_STOCK_SCHEDULE_ID = "ai_stock_default_v1"


def resolve_ai_schedule_strategy_ids(
    strategy_ids,
    *,
    strategies=None,
) -> list[str]:
    """Replace the stable AI schedule slot with currently applied strategies.

    The schedule registration remains stable while the AI-strategy screen can
    change the concrete strategy executed by that slot. Explicitly scheduled
    strategy IDs win and duplicates are removed without changing order.
    """
    requested = [
        str(strategy_id).strip()
        for strategy_id in strategy_ids
        if str(strategy_id).strip()
    ]
    if AI_STOCK_SCHEDULE_ID not in requested:
        return list(dict.fromkeys(requested))

    if strategies is None:
        try:
            from src.db.repository import load_ai_strategies

            strategies = load_ai_strategies()
        except Exception:
            strategies = []

    applied = [
        str(item.get("id") or "").strip()
        for item in strategies or []
        if item.get("selected")
        and str(item.get("status") or "") == "approved"
        and str(item.get("id") or "").strip()
    ]
    replacements = applied

    resolved = []
    for strategy_id in requested:
        values = replacements if strategy_id == AI_STOCK_SCHEDULE_ID else [strategy_id]
        for value in values:
            if value not in resolved:
                resolved.append(value)
    return resolved
