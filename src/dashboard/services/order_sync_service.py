"""Order reconciliation services extracted from the dashboard application module."""

MIN_ORDER_HISTORY_SYNC_DAYS = 30

def _refresh_dependencies() -> None:
    from src.dashboard import core
    protected = {
        "_refresh_dependencies", "_sync_filled_trades_from_history",
        "_order_history_window", "_load_trackable_order_trades",
        "_sync_order_status_from_history", "_sync_order_status_from_balance",
    }
    globals().update({name: value for name, value in vars(core).items() if name not in protected})


def _sync_filled_trades_from_history(
    api,
    *,
    days: int = 90,
    history: list[dict] | None = None,
) -> dict:
    _refresh_dependencies()
    from src.db.performance_repository import account_scope_key
    start_date, end_date = _order_history_window(days)
    if history is None:
        history = api.get_trade_history(start_date, end_date)
    trader.init_db()

    merged_trades = _load_merged_trades()
    existing = {_history_trade_key(item): item for item in merged_trades}
    def broker_history_key(item: dict) -> tuple[str, str, str, str, str]:
        return (
            str(item.get("env") or trader.runtime_flags().trading_env),
            str(item.get("ts") or "")[:10],
            str(item.get("broker_order_id") or "").strip(),
            str(item.get("symbol") or ""),
            str(item.get("action") or ""),
        )

    existing_by_broker_order_id = {
        broker_history_key(item): item
        for item in merged_trades
        if str(item.get("broker_order_id") or "").strip()
    }
    active_by_broker_order_id = {
        (
            str(item.get("env") or trader.runtime_flags().trading_env),
            str(item.get("broker_order_id") or "").strip(),
            str(item.get("symbol") or ""),
            str(item.get("action") or ""),
        ): item
        for item in merged_trades
        if str(item.get("broker_order_id") or "").strip()
        and str(item.get("order_status") or "") in {"submitted", "open", "partial"}
    }
    imported_count = 0
    skipped_count = 0
    updated_count = 0
    items = []

    with trader.connect_db() as conn:
        for row in history:
            trade = _history_row_to_trade(row)
            if not trade:
                skipped_count += 1
                items.append({
                    "sync_type": "history",
                    "sync_result": "skipped",
                    "ts": _history_timestamp(row),
                    "symbol": _history_symbol(row),
                    "name": _history_name(row),
                    "action": _history_action(row),
                    "qty": _history_fill_qty(row),
                    "price": _history_fill_price(row),
                    "broker_order_id": _broker_order_id_from_history(row),
                    "order_status": "unrecognized",
                    "message": "체결 거래로 해석할 수 없어 제외",
                })
                continue

            key = _history_trade_key(trade)
            broker_order_id = str(trade.get("broker_order_id") or "").strip()
            stored = existing.get(key) or existing_by_broker_order_id.get(
                broker_history_key(trade)
            )
            if stored is None:
                stored = active_by_broker_order_id.get((
                    str(trade.get("env") or trader.runtime_flags().trading_env),
                    broker_order_id,
                    str(trade.get("symbol") or ""),
                    str(trade.get("action") or ""),
                ))
            if stored is not None:
                item_result = "skipped"
                item_message = "이미 저장된 체결 기록"
                stored_state = (
                    str(stored.get("order_status") or ""),
                    _to_int(stored.get("filled_qty")),
                    _to_int(stored.get("filled_price")),
                )
                requested_qty = _to_int(stored.get("qty"))
                filled_qty = _to_int(trade.get("filled_qty"))
                remaining_qty = _history_remaining_qty(row)
                if _history_order_is_canceled(row):
                    incoming_status = "canceled"
                elif _history_order_is_rejected(row) and filled_qty <= 0:
                    incoming_status = "failed"
                elif remaining_qty > 0 and filled_qty <= 0:
                    incoming_status = "open"
                elif remaining_qty > 0 or (
                    requested_qty > 0 and filled_qty < requested_qty
                ):
                    incoming_status = "partial"
                else:
                    incoming_status = "filled"
                incoming_state = (
                    incoming_status,
                    filled_qty,
                    _to_int(trade.get("filled_price")),
                )
                needs_account_key = not str(stored.get("account_key") or "").strip()
                if trade["broker_order_id"] and (stored_state != incoming_state or needs_account_key):
                    stored_id = _to_int(stored.get("id"))
                    where_sql = (
                        "id = ?"
                        if stored_id > 0
                        else "broker_order_id = ? AND symbol = ? AND action = ? AND env = ? AND substr(ts, 1, 10) = ?"
                    )
                    where_values = (
                        (stored_id,)
                        if stored_id > 0
                        else (
                            trade["broker_order_id"],
                            trade["symbol"],
                            trade["action"],
                            trade["env"],
                            str(trade["ts"])[:10],
                        )
                    )
                    cursor = conn.execute(
                        f"""
                        UPDATE trades
                        SET order_status = ?,
                            filled_qty = ?,
                            filled_price = ?,
                            response_msg = ?,
                            broker_result = ?,
                            account_key = COALESCE(NULLIF(account_key, ''), ?)
                        WHERE {where_sql}
                        """,
                        (
                            incoming_status,
                            trade["filled_qty"],
                            trade["filled_price"],
                            trade["response_msg"],
                            trade["broker_result"],
                            account_scope_key(),
                            *where_values,
                        ),
                    )
                    updated_count += int(cursor.rowcount)
                    if cursor.rowcount:
                        item_result = "updated"
                        item_message = "기존 거래의 체결 상태 갱신"
                        logger.info(
                            "[TRADE_IMPORT_UPDATE] "
                            f"symbol={trade['symbol']} action={trade['action']} "
                            f"qty={trade['qty']} status={trade['order_status']} "
                            f"filled_qty={trade['filled_qty']} filled_price={trade['filled_price']} "
                            f"broker_order_id={trade['broker_order_id'] or '-'}"
                        )
                items.append({
                    "sync_type": "history",
                    "sync_result": item_result,
                    "ts": trade["ts"],
                    "symbol": trade["symbol"],
                    "name": trade["name"],
                    "action": trade["action"],
                    "qty": trade["filled_qty"] or trade["qty"],
                    "price": trade["filled_price"] or trade["price"],
                    "broker_order_id": trade["broker_order_id"],
                    "order_status": trade["order_status"],
                    "message": item_message,
                })
                skipped_count += 1
                continue

            conn.execute(
                """
                INSERT INTO trades (
                    ts, symbol, name, action, qty, price, reason, ok, env, dry_run,
                    broker_order_id, order_status, filled_qty, filled_price, pre_order_qty, response_msg, broker_result,
                    account_key, fee, tax, cost_source
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade["ts"],
                    trade["symbol"],
                    trade["name"],
                    trade["action"],
                    trade["qty"],
                    trade["price"],
                    trade["reason"],
                    trade["ok"],
                    trade["env"],
                    trade["dry_run"],
                    trade["broker_order_id"],
                    trade["order_status"],
                    trade["filled_qty"],
                    trade["filled_price"],
                    0,
                    trade["response_msg"],
                    trade["broker_result"],
                    account_scope_key(),
                    None,
                    None,
                    "unavailable",
                ),
            )
            logger.info(
                "[TRADE_IMPORT] "
                f"symbol={trade['symbol']} action={trade['action']} qty={trade['qty']} "
                f"price={trade['price']} status={trade['order_status']} "
                f"filled_qty={trade['filled_qty']} filled_price={trade['filled_price']} "
                f"broker_order_id={trade['broker_order_id'] or '-'}"
            )
            existing[key] = trade
            imported_count += 1
            items.append({
                "sync_type": "history",
                "sync_result": "imported",
                "ts": trade["ts"],
                "symbol": trade["symbol"],
                "name": trade["name"],
                "action": trade["action"],
                "qty": trade["filled_qty"] or trade["qty"],
                "price": trade["filled_price"] or trade["price"],
                "broker_order_id": trade["broker_order_id"],
                "order_status": trade["order_status"],
                "message": "증권사 체결 기록 신규 추가",
            })

    return {
        "ok": True,
        "start_date": start_date,
        "end_date": end_date,
        "history_count": len(history),
        "imported_count": imported_count,
        "updated_count": updated_count,
        "skipped_count": skipped_count,
        "items": items,
    }


def _order_history_window(days: int = MIN_ORDER_HISTORY_SYNC_DAYS) -> tuple[str, str]:
    _refresh_dependencies()
    end = trader.datetime.now(trader.KST)
    start = end - trader.timedelta(days=max(MIN_ORDER_HISTORY_SYNC_DAYS, days))
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def _load_trackable_order_trades(days: int = MIN_ORDER_HISTORY_SYNC_DAYS) -> list[dict]:
    _refresh_dependencies()
    trader.init_db()
    cutoff = (trader.datetime.now(trader.KST) - trader.timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d")
    with trader.connect_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT *
            FROM trades
            WHERE broker_order_id IS NOT NULL
              AND broker_order_id != ''
              AND (
                    COALESCE(order_status, '') IN ('submitted', 'partial', 'open')
                    OR (
                        COALESCE(order_status, '') = 'filled'
                        AND action = 'sell'
                        AND source_approval_id IS NOT NULL
                    )
                  )
              AND substr(COALESCE(ts, ''), 1, 10) >= ?
            ORDER BY ts ASC
            """,
            (cutoff,),
        ).fetchall()
    return [dict(row) for row in rows]


def _sync_order_status_from_history(
    api,
    *,
    days: int = MIN_ORDER_HISTORY_SYNC_DAYS,
    history: list[dict] | None = None,
) -> dict:
    _refresh_dependencies()
    tracked = _load_trackable_order_trades(days)
    if not tracked:
        return {"ok": True, "checked_count": 0, "updated_count": 0, "orders": []}

    start_date, end_date = _order_history_window(days)
    if history is None:
        try:
            history = api.get_trade_history(start_date, end_date)
        except DashboardOperationError as exc:
            fallback = _sync_order_status_from_balance(api, tracked, reason=str(exc))
            return {
                **fallback,
                "ok": fallback.get("updated_count", 0) > 0,
                "history_error": str(exc),
                "history_count": 0,
                "fallback": "balance",
            }
    orders = []
    updated_count = 0
    unmatched = []
    for trade in tracked:
        order_id = str(trade.get("broker_order_id") or "")
        row = next((item for item in history if _history_matches_tracked_order(item, trade)), None)
        if row is None:
            unmatched.append(trade)
            continue

        requested_qty = _to_int(trade.get("qty"))
        filled_qty = _history_fill_qty(row)
        filled_price = _history_fill_price(row)
        remaining_qty = _history_remaining_qty(row)
        order_date = _history_timestamp(row)[:10]
        today = trader.datetime.now(trader.KST).strftime("%Y-%m-%d")
        expired_with_remainder = bool(order_date and order_date < today and remaining_qty > 0)
        if _history_order_is_canceled(row) or expired_with_remainder:
            order_status = "canceled"
        elif _history_order_is_rejected(row) and filled_qty <= 0:
            order_status = "failed"
        elif remaining_qty > 0 and filled_qty <= 0:
            order_status = "open"
        elif remaining_qty > 0 or (requested_qty > 0 and filled_qty < requested_qty):
            order_status = "partial"
        else:
            order_status = "filled"
        response_msg = f"KIS order history sync: {order_status}"
        status_changed = str(trade.get("order_status") or "") != order_status
        quantity_changed = _to_int(trade.get("filled_qty")) != filled_qty
        price_changed = filled_price > 0 and _to_int(trade.get("filled_price")) != filled_price
        if status_changed or quantity_changed or price_changed:
            updated_count += trader.update_trade_order_status(
                order_id,
                trade_id=_to_int(trade.get("id")) or None,
                order_status=order_status,
                filled_qty=filled_qty,
                filled_price=filled_price,
                response_msg=response_msg,
                broker_result=row,
            )
        orders.append({
            "broker_order_id": order_id,
            "symbol": trade.get("symbol", ""),
            "name": trade.get("name", ""),
            "action": trade.get("action", ""),
            "order_status": order_status,
            "filled_qty": filled_qty,
            "filled_price": filled_price,
        })

    balance_sync = _sync_order_status_from_balance(
        api,
        unmatched,
        reason="order absent from KIS history",
        close_unreserved_sells=True,
    ) if unmatched else {"ok": True, "checked_count": 0, "updated_count": 0, "orders": []}
    updated_count += int(balance_sync.get("updated_count", 0) or 0)
    orders.extend(balance_sync.get("orders", []) or [])

    return {
        "ok": bool(balance_sync.get("ok", True)),
        "checked_count": len(tracked),
        "updated_count": updated_count,
        "history_count": len(history),
        "unmatched_count": len(unmatched),
        "balance_checked_count": int(balance_sync.get("checked_count", 0) or 0),
        "orders": orders,
    }


def _sync_order_status_from_balance(
    api,
    tracked: list[dict],
    *,
    reason: str = "",
    close_unreserved_sells: bool = False,
) -> dict:
    _refresh_dependencies()
    try:
        parsed = _parse_balance(_get_balance_data(api, allow_cache=False))
    except DashboardOperationError as exc:
        return {
            "ok": False,
            "checked_count": len(tracked),
            "updated_count": 0,
            "orders": [],
            "balance_error": str(exc),
            "history_error": reason,
        }

    holdings = {str(item.get("symbol") or ""): item for item in parsed.get("holdings", [])}
    orders = []
    updated_count = 0
    for trade in tracked:
        order_id = str(trade.get("broker_order_id") or "")
        symbol = str(trade.get("symbol") or "")
        action = str(trade.get("action") or "").lower()
        requested_qty = _to_int(trade.get("qty"))
        pre_order_qty = _to_int(trade.get("pre_order_qty"))
        current = holdings.get(symbol, {})
        current_qty = _to_int(current.get("qty"))
        sellable_qty = _to_int(current.get("sellable_qty"))
        current_price = _to_int(current.get("price")) or _to_int(trade.get("price"))

        filled = False
        if action == "buy" and requested_qty > 0:
            filled = current_qty >= pre_order_qty + requested_qty
        elif action == "sell" and requested_qty > 0:
            filled = current_qty <= max(0, pre_order_qty - requested_qty)

        if not filled:
            inferred_filled_qty = (
                min(requested_qty, max(_to_int(trade.get("filled_qty")), pre_order_qty - current_qty))
                if action == "sell"
                else _to_int(trade.get("filled_qty"))
            )
            sell_is_unreserved = (
                close_unreserved_sells
                and action == "sell"
                and bool(current)
                and current_qty > 0
                and sellable_qty >= current_qty
            )
            if sell_is_unreserved:
                order_status = "partial" if inferred_filled_qty > 0 else "canceled"
                response_msg = f"Balance reconciliation: {order_status} (no active sell reservation)"
                updated_count += trader.update_trade_order_status(
                    order_id,
                    trade_id=_to_int(trade.get("id")) or None,
                    order_status=order_status,
                    filled_qty=inferred_filled_qty,
                    filled_price=current_price if inferred_filled_qty > 0 else 0,
                    response_msg=response_msg,
                    broker_result={
                        "fallback": "balance",
                        "history_error": reason,
                        "pre_order_qty": pre_order_qty,
                        "current_qty": current_qty,
                        "sellable_qty": sellable_qty,
                    },
                )
                orders.append({
                    "broker_order_id": order_id,
                    "symbol": symbol,
                    "name": trade.get("name", ""),
                    "action": action,
                    "order_status": order_status,
                    "filled_qty": inferred_filled_qty,
                    "filled_price": current_price if inferred_filled_qty > 0 else 0,
                    "balance_confirmed": True,
                    "sell_reservation_active": False,
                })
                continue
            orders.append({
                "broker_order_id": order_id,
                "symbol": symbol,
                "name": trade.get("name", ""),
                "action": action,
                "order_status": trade.get("order_status") or "submitted",
                "balance_confirmed": False,
            })
            continue

        response_msg = "Balance fallback sync: filled"
        updated_count += trader.update_trade_order_status(
            order_id,
            trade_id=_to_int(trade.get("id")) or None,
            order_status="filled",
            filled_qty=requested_qty,
            filled_price=current_price,
            response_msg=response_msg,
            broker_result={
                "fallback": "balance",
                "history_error": reason,
                "pre_order_qty": pre_order_qty,
                "current_qty": current_qty,
            },
        )
        orders.append({
            "broker_order_id": order_id,
            "symbol": symbol,
            "name": trade.get("name", ""),
            "action": action,
            "order_status": "filled",
            "filled_qty": requested_qty,
            "filled_price": current_price,
            "balance_confirmed": True,
        })

    return {
        "ok": True,
        "checked_count": len(tracked),
        "updated_count": updated_count,
        "orders": orders,
    }



# =============================================================================
