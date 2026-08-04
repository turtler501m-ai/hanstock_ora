"""QuantConnect dashboard orchestration extracted from dashboard core."""

def _refresh_dependencies() -> None:
    from src.dashboard import core
    protected = {name for name in globals() if name.startswith("_quantconnect") or name in {
        "_refresh_dependencies", "_public_override", "_first_item", "_clear_quantconnect_cloud_cache",
        "_select_quantconnect_live_node", "_wait_for_quantconnect_compile",
    }}
    globals().update({name: value for name, value in vars(core).items() if name not in protected})


def _public_override(name: str, current):
    import sys
    from src.dashboard import core

    module = sys.modules.get("src.dashboard")
    value = getattr(module, name, None) if module is not None else None
    core_wrapper = getattr(core, name, None)
    if value is not None and value is not current and value is not core_wrapper:
        return value
    return None

def _quantconnect_auth_status(credentials: QuantConnectCredentials) -> dict:
    return _external_integration_service.quantconnect_auth_status(credentials)


def _first_item(value):
    if isinstance(value, list):
        return value[0] if value else {}
    if isinstance(value, dict):
        return value
    return {}


def _quantconnect_errors(*payloads: dict) -> list[str]:
    errors = []
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for error in payload.get("errors") or []:
            if error:
                errors.append(str(error))
        if payload.get("error"):
            errors.append(str(payload["error"]))
        if payload.get("message") and payload.get("success") is False:
            errors.append(str(payload["message"]))
    return list(dict.fromkeys(errors))


def _quantconnect_order_rows(payload: dict) -> list[dict]:
    orders = payload.get("orders") or payload.get("Orders") or []
    if isinstance(orders, dict):
        orders = list(orders.values())
    rows = []
    for order in orders if isinstance(orders, list) else []:
        if not isinstance(order, dict):
            continue
        symbol = order.get("symbol") or order.get("Symbol") or "MNQ"
        if isinstance(symbol, dict):
            symbol = symbol.get("value") or symbol.get("id") or symbol.get("permtick") or "MNQ"
        direction = order.get("direction") or order.get("side") or order.get("Direction")
        if direction in {0, "0"}:
            direction = "Buy"
        elif direction in {1, "1"}:
            direction = "Sell"
        elif direction is None and (order.get("quantity") or order.get("Quantity") or 0):
            direction = "Buy" if float(order.get("quantity") or order.get("Quantity") or 0) > 0 else "Sell"
        rows.append({
            "id": order.get("id") or order.get("orderId") or order.get("OrderId"),
            "time": order.get("time") or order.get("createdTime") or order.get("lastFillTime") or order.get("Time"),
            "symbol": symbol,
            "side": direction,

            "quantity": order.get("quantity") or order.get("Quantity"),
            "price": order.get("price") or order.get("Price") or order.get("averageFillPrice"),
            "status": order.get("status") or order.get("Status"),
        })
    return rows


def _quantconnect_portfolio_state(payload: dict) -> dict:
    portfolio = payload.get("portfolio") or payload.get("Portfolio") or {}
    holdings_raw = portfolio.get("holdings") if isinstance(portfolio, dict) else {}
    cash_raw = portfolio.get("cash") if isinstance(portfolio, dict) else {}
    holdings = []
    if isinstance(holdings_raw, dict):
        iterator = holdings_raw.items()
    elif isinstance(holdings_raw, list):
        iterator = enumerate(holdings_raw)
    else:
        iterator = []
    for key, value in iterator:
        if not isinstance(value, dict):
            continue
        holdings.append({
            "symbol": value.get("symbol") or value.get("Symbol") or str(key),
            "quantity": value.get("quantity") or value.get("Quantity") or value.get("holdings") or value.get("q"),
            "average_price": value.get("averagePrice") or value.get("AveragePrice") or value.get("a"),
            "market_price": value.get("price") or value.get("Price") or value.get("p"),
            "market_value": value.get("marketValue") or value.get("MarketValue") or value.get("value") or value.get("v"),
            "unrealized_pnl": value.get("unrealizedProfit") or value.get("UnrealizedProfit") or value.get("u"),
        })
    return {
        "raw": portfolio if isinstance(portfolio, dict) else {},
        "holdings": holdings,
        "cash": cash_raw if isinstance(cash_raw, dict) else {},
        "total_portfolio_value": portfolio.get("totalPortfolioValue") if isinstance(portfolio, dict) else None,
    }


def _quantconnect_cloud_snapshot(credentials: QuantConnectCredentials, *, force_refresh: bool = False) -> dict:
    override = _public_override("_quantconnect_cloud_snapshot", _quantconnect_cloud_snapshot)
    if override is not None:
        return override(credentials, force_refresh=force_refresh)
    if not credentials.configured or not credentials.project_configured:
        return {
            "enabled": False,
            "errors": [],
            "project": {},
            "live": {},
            "portfolio": {},
            "orders": [],
        }


    now = trader.datetime.now(trader.KST)
    cached = _read_json_file(QUANTCONNECT_CLOUD_CACHE, {})
    if not force_refresh and isinstance(cached, dict) and cached.get("checked_at"):
        try:
            age = (now - trader.datetime.fromisoformat(cached["checked_at"])).total_seconds()
        except ValueError:
            age = None
        if age is not None and age < 60 and isinstance(cached.get("snapshot"), dict):
            snapshot = cached["snapshot"]
            snapshot["cached"] = True
            return snapshot

    api = QuantConnectAPI(credentials)
    project_payload = api.read_project(credentials.project_id, timeout=8.0)
    live_list_payload = api.list_live_algorithms(credentials.project_id, timeout=8.0)
    live_payload = api.read_live_algorithm(credentials.project_id, timeout=8.0)
    portfolio_payload = api.read_live_portfolio(credentials.project_id, timeout=8.0)

    projects = project_payload.get("projects") if isinstance(project_payload, dict) else []
    project = _first_item(projects)
    live_algorithms = (
        live_list_payload.get("live") or
        live_list_payload.get("algorithms") or
        live_list_payload.get("liveAlgorithms") or
        []
    )
    live_algorithm = _first_item(live_algorithms)
    deploy_id = (
        live_payload.get("deployId") or
        live_payload.get("algorithmId") or
        live_algorithm.get("deployId") or
        live_algorithm.get("algorithmId")
        if isinstance(live_payload, dict)
        else None
    )

    orders_payload = {}
    if deploy_id:
        orders_payload = api.read_live_orders(credentials.project_id, deploy_id, start=0, end=100, timeout=8.0)

    portfolio = _quantconnect_portfolio_state(portfolio_payload if isinstance(portfolio_payload, dict) else {})
    snapshot = {
        "enabled": True,
        "cached": False,
        "project": {
            "id": project.get("projectId") or credentials.project_id,
            "name": project.get("name") or project.get("Name") or "",
            "modified": project.get("modified") or project.get("Modified") or "",
            "language": project.get("language") or project.get("Language") or "",

        },
        "live": {
            "status": live_payload.get("status") or live_algorithm.get("status"),
            "deploy_id": deploy_id,
            "message": live_payload.get("message") or live_algorithm.get("message"),
            "launched": live_payload.get("launched") or live_algorithm.get("launched"),
            "stopped": live_payload.get("stopped") or live_algorithm.get("stopped"),
            "brokerage": live_payload.get("brokerage") or live_algorithm.get("brokerage"),
        },
        "portfolio": portfolio,
        "orders": _quantconnect_order_rows(orders_payload if isinstance(orders_payload, dict) else {}),
        "api_errors": _quantconnect_errors(project_payload, live_list_payload, live_payload, portfolio_payload, orders_payload),
    }
    QUANTCONNECT_CLOUD_CACHE.parent.mkdir(parents=True, exist_ok=True)
    QUANTCONNECT_CLOUD_CACHE.write_text(
        json.dumps({"checked_at": now.isoformat(), "snapshot": snapshot}, ensure_ascii=False),
        encoding="utf-8",
    )
    return snapshot


def _clear_quantconnect_cloud_cache() -> None:
    try:
        QUANTCONNECT_CLOUD_CACHE.unlink(missing_ok=True)
    except OSError:
        pass


def _quantconnect_live_nodes(nodes_payload: dict) -> list[dict]:
    nodes = nodes_payload.get("nodes") if isinstance(nodes_payload, dict) else {}
    live_nodes = nodes.get("live") if isinstance(nodes, dict) else []
    return [node for node in live_nodes if isinstance(node, dict)]


def _select_quantconnect_live_node(nodes_payload: dict, requested_node_id: str = "") -> dict:
    live_nodes = _quantconnect_live_nodes(nodes_payload)
    if requested_node_id:
        for node in live_nodes:
            if str(node.get("id") or "") == requested_node_id:
                return node
        raise HTTPException(status_code=400, detail=f"QuantConnect live node not found: {requested_node_id}")

    for node in live_nodes:
        if node.get("active") and not node.get("busy"):
            return node
    for node in live_nodes:
        if node.get("active"):
            return node
    if live_nodes:
        return live_nodes[0]

    raise HTTPException(status_code=409, detail="No QuantConnect live node is available for this project")


def _wait_for_quantconnect_compile(
    api: QuantConnectAPI,
    project_id: str,
    compile_payload: dict,
    *,
    attempts: int = 12,
    interval_seconds: float = 2.0,
) -> dict:
    compile_id = str(compile_payload.get("compileId") or "")
    if not compile_id:
        errors = compile_payload.get("errors") or [compile_payload.get("error") or "QuantConnect compile did not return compileId"]
        raise HTTPException(status_code=502, detail="; ".join(str(error) for error in errors if error))

    result = compile_payload
    for _ in range(attempts):
        state = str(result.get("state") or "").lower()
        if state == "buildsuccess":
            return result
        if state == "builderror":
            logs = result.get("logs") or result.get("errors") or ["QuantConnect build failed"]
            raise HTTPException(status_code=502, detail="; ".join(str(log) for log in logs if log))
        time.sleep(interval_seconds)
        result = api.read_compile(project_id, compile_id, timeout=10.0)

    raise HTTPException(status_code=504, detail=f"QuantConnect compile is still pending: {compile_id}")


def _quantconnect_mnq_status() -> dict:
    load_dotenv(dotenv_path=ENV_PATH, override=True)
    algorithm_path = QUANTCONNECT_MNQ_DIR / "main.py"
    config_path = QUANTCONNECT_MNQ_DIR / "config.json"
    doc_path = BASE_DIR / "doc" / "S1.한스톡사용설명서.md"
    config = _read_json_file(config_path, {})
    if not isinstance(config, dict):
        config = {}
    results = _read_json_file(QUANTCONNECT_MNQ_RESULTS, {})
    if not isinstance(results, dict):
        results = {}
    qc_user_id = os.environ.get("QUANTCONNECT_USER_ID") or os.environ.get("QC_USER_ID")
    qc_api_token = os.environ.get("QUANTCONNECT_API_TOKEN") or os.environ.get("QC_API_TOKEN")
    qc_project_id = os.environ.get("QUANTCONNECT_PROJECT_ID") or os.environ.get("QC_PROJECT_ID")
    credentials = QuantConnectCredentials(
        user_id=qc_user_id or "",
        api_token=qc_api_token or "",
        project_id=qc_project_id or "",
    )
    auth = _quantconnect_auth_status(credentials)

    cloud_sync_configured = credentials.configured and credentials.project_configured
    cloud_snapshot = _quantconnect_cloud_snapshot(credentials)
    project_ready = algorithm_path.exists() and config_path.exists()

    deployment = results.get("deployment") if isinstance(results.get("deployment"), dict) else {}
    if not deployment:
        cloud_live = cloud_snapshot.get("live", {}) if isinstance(cloud_snapshot.get("live"), dict) else {}
        cloud_status = str(cloud_live.get("status") or "").strip()
        if cloud_status:
            if cloud_status.lower() == "running":
                deployment_status = "running"
                deployment_message = "QuantConnect Paper Live deployment is running."
            else:
                deployment_status = cloud_status.lower()
                deployment_message = (
                    f"QuantConnect project is configured, but the Paper Live deployment is {cloud_status}. "
                    "Start or redeploy it before sending dashboard orders."
                )
        elif not credentials.configured:
            deployment_status = "not_connected"
            deployment_message = "QuantConnect User Id and API Token are required."
        elif not credentials.project_configured:
            deployment_status = "not_connected"
            deployment_message = "QuantConnect Project Id is required for project/order sync."
        else:
            deployment_status = "ready_to_sync"
            deployment_message = "QuantConnect API and Project Id are configured. Deploy the project as Paper Live before sending dashboard orders."
        deployment = {
            "status": deployment_status,
            "message": deployment_message,
        }

    return {
        "as_of": trader.datetime.now(trader.KST).isoformat(),
        "feasible": True,
        "project_ready": project_ready,
        "cloud_sync_configured": cloud_sync_configured,
        "auth": {
            "configured": credentials.configured,
            "project_configured": credentials.project_configured,
            "success": bool(auth.get("success")),
            "status_code": auth.get("status_code"),
            "error": auth.get("error"),
        },
        "algorithm": {
            "path": str(algorithm_path),
            "exists": algorithm_path.exists(),
            "symbol": "MNQ",
            "quantconnect_symbol": "Futures.Indices.MICRO_NASDAQ_100_E_MINI",
            "brokerage": "QuantConnect Paper Trading",

            "max_contracts": config.get("parameters", {}).get("MAX_CONTRACTS", "1"),
        },
        "files": {
            "config": {"path": str(config_path), "exists": config_path.exists()},
            "documentation": {"path": str(doc_path), "exists": doc_path.exists()},
            "results": {"path": str(QUANTCONNECT_MNQ_RESULTS), "exists": QUANTCONNECT_MNQ_RESULTS.exists()},
        },
        "deployment": deployment,
        "account": cloud_snapshot.get("portfolio", {}).get("raw") or results.get("account", {}),
        "positions": cloud_snapshot.get("portfolio", {}).get("holdings") or results.get("positions", []),
        "orders": cloud_snapshot.get("orders") or results.get("orders", []),
        "metrics": results.get("metrics", {}),
        "cloud": cloud_snapshot,
        "sources": [
            "https://www.quantconnect.com/docs/v2/cloud-platform/live-trading/brokerages/quantconnect-paper-trading",
            "https://www.quantconnect.com/docs/v2/writing-algorithms/datasets/algoseek/us-futures",
        ],
    }


def _quantconnect_credentials() -> QuantConnectCredentials:
    override = _public_override("_quantconnect_credentials", _quantconnect_credentials)
    if override is not None:
        return override()
    return _external_integration_service.quantconnect_credentials()


def _quantconnect_mnq_deploy(payload: dict | None = None) -> dict:
    payload = payload or {}
    credentials = _quantconnect_credentials()
    if not credentials.configured:
        raise HTTPException(status_code=400, detail="QuantConnect User Id and API Token are required")
    if not credentials.project_configured:
        raise HTTPException(status_code=400, detail="QUANTCONNECT_PROJECT_ID is required")

    api = QuantConnectAPI(credentials)
    payload_node_id = str(payload.get("node_id") or "").strip()
    requested_node_id = (
        payload_node_id
        or os.environ.get("QUANTCONNECT_LIVE_NODE_ID", "").strip()
        or os.environ.get("QC_LIVE_NODE_ID", "").strip()
    )

    nodes_payload = api.read_project_nodes(credentials.project_id, timeout=10.0)
    if not nodes_payload.get("success", False):
        errors = nodes_payload.get("errors") or [nodes_payload.get("error") or "QuantConnect live node lookup failed"]
        raise HTTPException(status_code=502, detail="; ".join(str(error) for error in errors if error))
    try:
        node = _select_quantconnect_live_node(nodes_payload, requested_node_id)
    except HTTPException:

        if payload_node_id:
            raise
        node = _select_quantconnect_live_node(nodes_payload, "")

    compile_payload = api.create_compile(credentials.project_id, timeout=10.0)
    if not compile_payload.get("success", False):
        errors = compile_payload.get("errors") or [compile_payload.get("error") or "QuantConnect compile failed"]
        raise HTTPException(status_code=502, detail="; ".join(str(error) for error in errors if error))
    compile_result = _wait_for_quantconnect_compile(api, credentials.project_id, compile_payload)

    config = _read_json_file(QUANTCONNECT_MNQ_DIR / "config.json", {})
    parameters = config.get("parameters", {}) if isinstance(config, dict) else {}
    live_payload = api.create_live_algorithm(
        credentials.project_id,
        str(compile_result.get("compileId")),
        str(node.get("id")),
        parameters=parameters,
        timeout=20.0,
    )
    if not live_payload.get("success", False):
        errors = live_payload.get("errors") or [live_payload.get("error") or "QuantConnect Paper Live deployment failed"]
        raise HTTPException(status_code=502, detail="; ".join(str(error) for error in errors if error))

    _clear_quantconnect_cloud_cache()
    snapshot = _quantconnect_cloud_snapshot(credentials, force_refresh=True)
    return {
        "success": True,
        "project_id": credentials.project_id,
        "compile_id": compile_result.get("compileId"),
        "node": {
            "id": node.get("id"),
            "name": node.get("name"),
            "sku": node.get("sku"),
        },
        "deploy_id": live_payload.get("deployId") or live_payload.get("algorithmId"),
        "raw": live_payload,
        "cloud": snapshot,
    }


def _quantconnect_mnq_order(payload: dict) -> dict:
    credentials = _quantconnect_credentials()
    side = str(payload.get("side") or "").strip().lower()
    signal_id = str(payload.get("signal_id") or "").strip()
    provider = str(payload.get("provider") or "").strip()
    try:
        quantity = int(payload.get("quantity") or payload.get("qty") or 0)
    except (TypeError, ValueError):
        quantity = 0


    if side not in {"buy", "sell"}:
        raise HTTPException(status_code=400, detail="side must be buy or sell")
    if quantity < 1:
        raise HTTPException(status_code=400, detail="quantity must be at least 1")
    if quantity > 3:
        raise HTTPException(status_code=400, detail="MNQ paper dashboard orders are limited to 3 contracts")
    if not credentials.configured:
        raise HTTPException(status_code=400, detail="QuantConnect User Id and API Token are required")
    if not credentials.project_configured:
        raise HTTPException(status_code=400, detail="QUANTCONNECT_PROJECT_ID is required")

    cloud_snapshot = _quantconnect_cloud_snapshot(credentials, force_refresh=True)
    live = cloud_snapshot.get("live", {}) if isinstance(cloud_snapshot.get("live"), dict) else {}
    live_status = str(live.get("status") or "").strip()
    if live_status.lower() != "running":
        detail = (
            f"QuantConnect project {credentials.project_id} has no running Paper Live instance"
        )
        if live_status:
            detail += f" (current status: {live_status})"
        detail += ". Start or redeploy the project in QuantConnect before sending dashboard orders."
        raise HTTPException(status_code=409, detail=detail)

    order_tag = "hanstock-dashboard-mnq-paper"
    if signal_id:
        tag_source = re.sub(r"[^A-Za-z0-9_-]+", "-", provider or "telegram").strip("-") or "telegram"
        signal_ref = re.sub(r"[^A-Za-z0-9_-]+", "-", signal_id).strip("-") or "signal"
        order_tag = f"hanstock-signal-{tag_source}-{signal_ref}"[:80]

    command = {
        "command_type": "order",
        "symbol": "MNQ",
        "side": side,
        "quantity": quantity,
        "tag": order_tag,
    }
    result = QuantConnectAPI(credentials).create_live_command(credentials.project_id, command, timeout=10.0)
    return {
        "success": bool(result.get("success")),
        "command": command,
        "status_code": result.get("status_code"),
        "error": result.get("error"),
        "errors": result.get("errors", []),
    }
