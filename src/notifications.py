from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable


KST = timezone(timedelta(hours=9))


def format_kst_timestamp(value: datetime | None = None, fmt: str = "%Y-%m-%d %H:%M KST") -> str:
    current = value or datetime.now(KST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=KST)
    else:
        current = current.astimezone(KST)
    return current.strftime(fmt)


def build_slack_payload(
    text: str = "",
    blocks: list[dict[str, Any]] | None = None,
    color: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if text:
        payload["text"] = text
    if color:
        attachment: dict[str, Any] = {"color": color}
        if blocks:
            attachment["blocks"] = blocks
        if text:
            attachment["fallback"] = text
        payload["attachments"] = [attachment]
        return payload
    if blocks:
        payload["blocks"] = blocks
    return payload


def post_slack_payload(
    webhook_url: str,
    payload: dict[str, Any],
    session: Any,
    timeout: int = 10,
    log_fn: Callable[[str], None] | None = None,
) -> bool:
    from src.online_access import is_online_access_blocked

    if is_online_access_blocked():
        return False
    if not webhook_url:
        return False
    try:
        response = session.post(webhook_url, json=payload, timeout=timeout)
        if response.status_code != 200:
            if log_fn:
                log_fn(f"[WARN] Slack send failed HTTP {response.status_code}: {response.text[:100]}")
            return False
        return True
    except Exception as exc:  # pragma: no cover - exercised via tests with a fake session
        if log_fn:
            log_fn(f"[WARN] Slack exception: {exc}")
        return False


def send_slack_message(
    webhook_url: str,
    session: Any,
    text: str = "",
    blocks: list[dict[str, Any]] | None = None,
    color: str | None = None,
    timeout: int = 10,
    log_fn: Callable[[str], None] | None = None,
) -> bool:
    payload = build_slack_payload(text=text, blocks=blocks, color=color)
    return post_slack_payload(
        webhook_url=webhook_url,
        payload=payload,
        session=session,
        timeout=timeout,
        log_fn=log_fn,
    )


def build_session_start_payload(
    cash: int,
    total: int,
    stock_count: int,
    *,
    now: datetime | None = None,
    mode: str,
    trading_env: str,
) -> dict[str, Any]:
    ts = format_kst_timestamp(now)
    text = (
        f"*세븐 스플릿 자동매매 시작* | {ts}\n"
        f"모드: {mode} | 환경: {trading_env} | 예수금: {cash:,}원 | 평가금액: {total:,}원 | 보유종목: {stock_count}개"
    )
    return build_slack_payload(
        text=text.replace("*", ""),
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": text}}
        ],
        color="#2196F3",
    )


def build_order_payload(
    name: str,
    symbol: str,
    action: str,
    qty: float,
    price: float,
    reason: str,
    ok: bool,
    indicators: dict[str, Any] | None = None,
    exchange_rate: float | None = None,
) -> dict[str, Any]:
    details = indicators or {}
    action_label = "매수" if action == "buy" else "매도"
    status = "성공" if ok else "실패"
    
    # US stock symbols typically have letters, while Korean stocks are numeric digits
    is_us = not symbol.isdigit()
    
    if is_us:
        if exchange_rate:
            krw_price = price * exchange_rate
            krw_amount = qty * price * exchange_rate
            price_str = f"${price:,.2f} (₩{int(krw_price):,}원)" if price else "시장가"
            amount_str = f"${qty * price:,.2f} (₩{int(krw_amount):,}원)" if price else "-"
        else:
            price_str = f"${price:,.2f}" if price else "시장가"
            amount_str = f"${qty * price:,.2f}" if price else "-"
        qty_str = f"{qty}주" if float(qty).is_integer() else f"{qty:.4f}주"
    else:
        price_str = f"{int(price):,}원" if price else "시장가"
        amount_str = f"{int(qty * price):,}원" if price else "-"
        qty_str = f"{int(qty)}주" if float(qty).is_integer() else f"{qty}주"

    rsi_value = details.get("rsi", "-")
    rsi_str = f"{rsi_value:.1f}" if isinstance(rsi_value, float) else str(rsi_value)
    rt_value = details.get("rt", 0)
    rt_str = f"{rt_value:+.2f}%" if isinstance(rt_value, (int, float)) else str(rt_value)
    summary_text = (
        f"*{action_label} {status}* | {name} (`{symbol}`) | {qty_str} @ {price_str} (총 {amount_str})\n"
        f"└ 사유: {reason} | RSI: {rsi_str} | 수익률: {rt_str}"
    )
    return build_slack_payload(
        text=summary_text.replace("*", ""),
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": summary_text}}
        ],
        color="#36a64f" if ok else "#e74c3c",
    )


def build_order_summary_payload(
    name: str,
    symbol: str,
    action: str,
    qty: float,
    price: float,
    reason: str,
    ok: bool,
    indicators: dict[str, Any] | None = None,
    exchange_rate: float | None = None,
) -> dict[str, Any]:
    details = indicators or {}
    action_label = "매수" if action == "buy" else "매도"
    status = "성공" if ok else "실패"
    is_us = not symbol.isdigit()

    if is_us:
        if exchange_rate:
            krw_price = price * exchange_rate
            krw_amount = qty * price * exchange_rate
            price_str = f"${price:,.2f} (₩{int(krw_price):,}원)" if price else "시장가"
            amount_str = f"${qty * price:,.2f} (₩{int(krw_amount):,}원)" if price else "-"
        else:
            price_str = f"${price:,.2f}" if price else "시장가"
            amount_str = f"${qty * price:,.2f}" if price else "-"
        qty_str = f"{int(qty)}주" if float(qty).is_integer() else f"{qty:.4f}주"
    else:
        price_str = f"{int(price):,}원" if price else "시장가"
        amount_str = f"{int(qty * price):,}원" if price else "-"
        qty_str = f"{int(qty)}주" if float(qty).is_integer() else f"{qty}주"

    rsi_value = details.get("rsi", "-")
    rsi_str = f"{rsi_value:.1f}" if isinstance(rsi_value, (int, float)) else str(rsi_value)
    rt_value = details.get("rt", 0)
    rt_str = f"{rt_value:+.2f}%" if isinstance(rt_value, (int, float)) else str(rt_value)
    first_line = f"{status} | {action_label} {name}({symbol}) {qty_str} @ {price_str} / {amount_str}"
    second_line = f"사유: {reason} | RSI {rsi_str}, 수익률 {rt_str}"

    return build_slack_payload(
        text=f"{first_line}\n{second_line}",
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{first_line}*\n{second_line}"}},
        ],
        color="#36a64f" if ok else "#e74c3c",
    )


def build_candidates_payload(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not candidates:
        return None

    visible = candidates[:5]
    lines = []
    for item in visible:
        ticker = item["ticker"]
        label = item.get("name") or ticker
        reasons = ", ".join(list(item.get("reasons") or [])[:2]) or "-"
        lines.append(
            f"*{label}* (`{ticker}`) {item['current_price']:,.0f}원 "
            f"| 점수 {item['score']} | {reasons}"
        )
    hidden = len(candidates) - len(visible)
    if hidden:
        lines.append(f"• 외 {hidden}종목")
    text = f"매수 후보 {len(candidates)}종목\n" + "\n".join(lines)
    return build_slack_payload(
        text=text.replace("*", ""),
        blocks=[
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*매수 후보 {len(candidates)}종목*"},
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
        ],
        color="#9C27B0",
    )


def build_session_end_payload(
    results: list[dict[str, Any]],
    cash: int,
    total: int,
    pnl: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    ts = format_kst_timestamp(now)
    actionable = [
        item
        for item in results
        if item.get("decision", "execute") in {"execute", "queue"}
    ]
    if not actionable:
        return build_slack_payload(
            text=f"자동매매 완료 | 주문 없음 | {ts}",
            color="#9E9E9E",
        )

    executed = [item for item in actionable if item.get("decision", "execute") == "execute"]
    queued_count = sum(1 for item in actionable if item.get("decision") == "queue")
    buy_count = sum(1 for item in executed if item["action"] == "buy" and item["ok"])
    sell_count = sum(1 for item in executed if item["action"] == "sell" and item["ok"])
    fail_count = sum(1 for item in executed if not item["ok"])
    
    summary_text = (
        f"*자동매매 완료* | 매수성공: {buy_count}건 | 매도성공: {sell_count}건 | "
        f"승인대기: {queued_count}건 | 실패: {fail_count}건\n"
        f"평가 {total:,}원 | 현금 {cash:,}원 | 손익 {pnl:+,}원 | {ts}"
    )
    detail_lines = []
    for item in actionable[:5]:
        if item.get("decision") == "queue":
            prefix = "승인대기"
        else:
            prefix = "매수" if item["action"] == "buy" else "매도"
        detail_lines.append(
            f"• {prefix} {item.get('name') or item.get('symbol', '-')} "
            f"{item.get('qty', 0)}주 - {item.get('reason', '-')}"
        )
    if len(actionable) > 5:
        detail_lines.append(f"… 외 {len(actionable) - 5}건")
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": summary_text}}]
    if detail_lines:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(detail_lines)}}
        )

    return build_slack_payload(
        text=summary_text.replace("*", ""),
        blocks=blocks,
        color="#36a64f" if pnl >= 0 else "#e74c3c",
    )


def build_error_payload(message: str) -> dict[str, Any]:
    return build_slack_payload(text=f"세븐 스플릿 오류: {message}", color="#e74c3c")
