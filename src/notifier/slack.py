from __future__ import annotations

import requests

from src.config import config
from src.notifications import (
    build_candidates_payload,
    build_error_payload,
    build_order_payload,
    build_order_summary_payload,
    build_session_end_payload,
    build_session_start_payload,
    post_slack_payload,
)
from src.utils.logger import logger


HTTP = requests.Session()


def send_slack(text: str = "", blocks: list | None = None, color: str | None = None) -> None:
    _send_slack_to(config.slack_webhook_url, text=text, blocks=blocks, color=color)


def send_mistock_slack(text: str = "", blocks: list | None = None, color: str | None = None) -> None:
    _send_slack_to(config.mistock_slack_webhook_url, text=text, blocks=blocks, color=color)


def _send_slack_to(webhook_url: str | None, text: str = "", blocks: list | None = None, color: str | None = None) -> None:
    payload = {}
    if text:
        payload["text"] = text
    if color:
        attachment = {"color": color}
        if blocks:
            attachment["blocks"] = blocks
        if text:
            attachment["fallback"] = text
        payload["attachments"] = [attachment]
    elif blocks:
        payload["blocks"] = blocks

    post_slack_payload(
        webhook_url=webhook_url or "",
        payload=payload,
        session=HTTP,
        log_fn=logger.warning,
    )


def _mode_label(order_submission_enabled: bool, real_orders_enabled: bool) -> str:
    if real_orders_enabled:
        return "실전 주문"
    if order_submission_enabled:
        return "모의투자 주문"
    return "DRY_RUN 점검"


def slack_session_start(
    cash: int,
    total: int,
    stock_count: int,
    order_submission_enabled: bool,
    real_orders_enabled: bool,
) -> None:
    payload = build_session_start_payload(
        cash=cash,
        total=total,
        stock_count=stock_count,
        mode=_mode_label(order_submission_enabled, real_orders_enabled),
        trading_env=config.trading_env,
    )
    post_slack_payload(config.slack_webhook_url, payload, HTTP, log_fn=logger.warning)


def slack_order(
    name: str,
    symbol: str,
    action: str,
    qty: int,
    price: int,
    reason: str,
    ok: bool,
    indicators: dict,
) -> None:
    payload = build_order_summary_payload(name, symbol, action, qty, price, reason, ok, indicators)
    post_slack_payload(config.slack_webhook_url, payload, HTTP, log_fn=logger.warning)


def mistock_slack_order(
    name: str,
    symbol: str,
    action: str,
    qty: float,
    price: float,
    reason: str,
    ok: bool,
    indicators: dict,
) -> None:
    from src.utils.exchange_rate import get_usd_krw_rate
    rate = get_usd_krw_rate()
    payload = build_order_summary_payload(
        name,
        symbol,
        action,
        qty,
        price,
        reason,
        ok,
        indicators,
        exchange_rate=rate,
    )
    post_slack_payload(config.mistock_slack_webhook_url or "", payload, HTTP, log_fn=logger.warning)


def slack_candidates(candidates: list[dict]) -> None:
    payload = build_candidates_payload(candidates)
    if payload is None:
        return
    post_slack_payload(config.slack_webhook_url, payload, HTTP, log_fn=logger.warning)


def slack_session_end(results: list[dict], cash: int, total: int, pnl: int) -> None:
    payload = build_session_end_payload(results=results, cash=cash, total=total, pnl=pnl)
    post_slack_payload(config.slack_webhook_url, payload, HTTP, log_fn=logger.warning)


def slack_error(msg: str) -> None:
    payload = build_error_payload(msg)
    post_slack_payload(config.slack_webhook_url, payload, HTTP, log_fn=logger.warning)
