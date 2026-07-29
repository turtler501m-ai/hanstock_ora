from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Protocol


class HTTPSession(Protocol):
    def get(self, url: str, **kwargs: Any) -> Any:
        ...

    def post(self, url: str, **kwargs: Any) -> Any:
        ...


def _default_session() -> HTTPSession:
    import requests
    from urllib3.util import Retry
    from requests.adapters import HTTPAdapter

    session = requests.Session()
    session.trust_env = False

    _adapter = HTTPAdapter(
        max_retries=Retry(
            total=3,
            backoff_factor=1.0,
            status_forcelist=[500, 502, 503, 504],
            raise_on_status=False
        )
    )
    session.mount("http://", _adapter)
    session.mount("https://", _adapter)
    return session


@dataclass(frozen=True)
class KISClientConfig:
    base_url: str
    app_key: str
    app_secret: str
    account_no: str = ""
    trading_env: str = "demo"
    customer_type: str = "P"
    token_cache_path: Path | None = None
    token_ttl: timedelta = timedelta(hours=23)
    token_refresh_margin: timedelta = timedelta(minutes=5)
    request_timeout_seconds: int = 15
    circuit_cooldown_seconds: int = 60
    circuit_max_errors: int = 5
    etf_market_codes: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "102110",
                "133690",
                "148020",
                "152100",
                "157490",
                "229200",
                "251340",
                "261240",
                "273130",
                "278530",
                "305720",
                "381170",
                "448290",
                "481190",
            }
        )
    )

    @property
    def account_prefix(self) -> str:
        return self.account_no[:8]

    @property
    def account_suffix(self) -> str:
        return self.account_no[8:] if len(self.account_no) > 8 else "01"

    @property
    def is_demo(self) -> bool:
        return self.trading_env == "demo"


@dataclass(frozen=True)
class TokenCacheEntry:
    token: str
    expires_at: datetime
    trading_env: str
    base_url: str
    app_key_prefix: str

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "TokenCacheEntry":
        return cls(
            token=str(payload["token"]),
            expires_at=datetime.fromisoformat(str(payload["expires_at"])),
            trading_env=str(payload["trading_env"]),
            base_url=str(payload["base_url"]),
            app_key_prefix=str(payload["app_key_prefix"]),
        )

    @classmethod
    def from_file(cls, path: Path) -> "TokenCacheEntry":
        return cls.from_mapping(json.loads(path.read_text(encoding="utf-8")))

    def matches(self, config: KISClientConfig) -> bool:
        return (
            self.trading_env == config.trading_env
            and self.base_url == config.base_url
            and self.app_key_prefix == config.app_key[:8]
        )

    def is_usable(self, now: datetime, refresh_margin: timedelta) -> bool:
        return self.expires_at > now + refresh_margin

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "token": self.token,
                    "expires_at": self.expires_at.isoformat(),
                    "trading_env": self.trading_env,
                    "base_url": self.base_url,
                    "app_key_prefix": self.app_key_prefix,
                }
            ),
            encoding="utf-8",
        )


@dataclass
class CircuitBreakerState:
    error_count: int = 0
    opened_at: datetime | None = None

    def reset(self) -> None:
        self.error_count = 0
        self.opened_at = None

    def record_success(self) -> None:
        self.reset()

    def record_failure(self, now: datetime, max_errors: int) -> None:
        self.error_count += 1
        if self.error_count >= max_errors and self.opened_at is None:
            self.opened_at = now

    def ensure_can_proceed(self, now: datetime, max_errors: int, cooldown_seconds: int) -> None:
        if self.error_count < max_errors:
            return
        if self.opened_at is None:
            self.opened_at = now
        elapsed = (now - self.opened_at).total_seconds()
        if elapsed >= cooldown_seconds:
            self.reset()
            return
        retry_after = max(1, int(cooldown_seconds - elapsed))
        raise RuntimeError(
            f"Circuit breaker opened after {self.error_count} consecutive API errors; "
            f"retry after {retry_after}s"
        )

    def status(self, now: datetime, max_errors: int, cooldown_seconds: int) -> dict[str, Any]:
        opened = self.error_count >= max_errors
        opened_at = None
        retry_after = 0
        if opened:
            if self.opened_at is None:
                self.opened_at = now
            opened_at = self.opened_at.isoformat()
            elapsed = (now - self.opened_at).total_seconds()
            retry_after = max(0, int(cooldown_seconds - elapsed))
            if retry_after <= 0:
                self.reset()
                opened = False
                opened_at = None
        return {
            "opened": opened,
            "error_count": self.error_count,
            "max_errors": max_errors,
            "cooldown_seconds": cooldown_seconds,
            "retry_after_seconds": retry_after,
            "opened_at": opened_at,
        }


class KISClient:
    def __init__(
        self,
        config: KISClientConfig,
        *,
        session: HTTPSession | None = None,
        clock: Callable[[], datetime] | None = None,
        circuit: CircuitBreakerState | None = None,
        access_token: str | None = None,
    ) -> None:
        self.config = config
        self.session = session or _default_session()
        self._clock = clock or datetime.now
        self.circuit = circuit or CircuitBreakerState()
        self.access_token = access_token or self._load_or_fetch_token()

    def now(self) -> datetime:
        return self._clock()

    def _load_or_fetch_token(self) -> str:
        path = self.config.token_cache_path
        if path and path.exists():
            try:
                cached = TokenCacheEntry.from_file(path)
                if cached.matches(self.config) and cached.is_usable(
                    self.now(),
                    self.config.token_refresh_margin,
                ):
                    return cached.token
            except Exception:
                pass
        return self.fetch_token()

    def fetch_token(self) -> str:
        response = self.session.post(
            f"{self.config.base_url}/oauth2/tokenP",
            json={
                "grant_type": "client_credentials",
                "appkey": self.config.app_key,
                "appsecret": self.config.app_secret,
            },
            timeout=self.config.request_timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        token = data.get("access_token", "")
        if not token:
            raise RuntimeError(f"Token response did not include access_token: {data}")
        expires_at = self.now() + self.config.token_ttl
        if self.config.token_cache_path:
            TokenCacheEntry(
                token=token,
                expires_at=expires_at,
                trading_env=self.config.trading_env,
                base_url=self.config.base_url,
                app_key_prefix=self.config.app_key[:8],
            ).write(self.config.token_cache_path)
        return token

    def headers(self, tr_id: str, *, include_auth: bool = True, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "appkey": self.config.app_key,
            "appsecret": self.config.app_secret,
            "tr_id": tr_id,
            "custtype": self.config.customer_type,
            "Content-Type": "application/json",
        }
        if include_auth:
            headers["authorization"] = f"Bearer {self.access_token}"
        if extra:
            headers.update(extra)
        return headers

    def create_hashkey(self, payload: dict[str, Any]) -> str:
        try:
            response = self.session.post(
                f"{self.config.base_url}/uapi/hashkey",
                headers={
                    "content-type": "application/json",
                    "appkey": self.config.app_key,
                    "appsecret": self.config.app_secret,
                },
                json=payload,
                timeout=self.config.request_timeout_seconds,
            )
            return response.json().get("HASH", "")
        except Exception:
            return ""

    def check_circuit(self) -> None:
        self.circuit.ensure_can_proceed(
            self.now(),
            self.config.circuit_max_errors,
            self.config.circuit_cooldown_seconds,
        )

    def mark_success(self) -> None:
        self.circuit.record_success()

    def mark_failure(self, error: Any = None) -> None:
        import sys
        from src.utils.logger import logger
        err_str = ""
        if error is not None:
            err_str = str(error)
            if not err_str:
                err_str = repr(error)
            if err_str in {"''", '""'}:
                err_str = "unknown KIS API failure"
        else:
            exc_type, exc_value, exc_traceback = sys.exc_info()
            if exc_value is not None:
                err_str = str(exc_value)
                if not err_str:
                    err_str = f"{exc_type.__name__}: {exc_value!r}" if exc_type else repr(exc_value)
        if not err_str:
            err_str = "unknown KIS API failure"
        if "EGW00201" in err_str or "거래건수를 초과" in err_str:
            return
        self.circuit.record_failure(self.now(), self.config.circuit_max_errors)
        logger.error(f"[KIS CLIENT] API call failed: {err_str}. Circuit error count: {self.circuit.error_count}")

    def circuit_status(self) -> dict[str, Any]:
        return self.circuit.status(
            self.now(),
            self.config.circuit_max_errors,
            self.config.circuit_cooldown_seconds,
        )

    def get_balance(self) -> dict[str, Any]:
        self.check_circuit()
        tr_id = "VTTC8434R" if self.config.is_demo else "TTTC8434R"
        params = {
            "CANO": self.config.account_prefix,
            "ACNT_PRDT_CD": self.config.account_suffix,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        last_error = ""
        for _attempt in range(2):
            try:
                response = self.session.get(
                    f"{self.config.base_url}/uapi/domestic-stock/v1/trading/inquire-balance",
                    headers=self.headers(tr_id),
                    params=params,
                    timeout=self.config.request_timeout_seconds,
                )
                response.raise_for_status()
                data = response.json()
                if data.get("rt_cd") == "0":
                    self.mark_success()
                    return data
                last_error = str(data.get("msg1", "unknown KIS balance error"))
            except Exception as exc:
                last_error = str(exc)
        self.mark_failure(last_error)
        return {"output1": [], "output2": [{}], "_error": last_error or "unknown KIS balance error"}

    def get_quote(self, symbol: str) -> dict[str, float]:
        self.check_circuit()
        try:
            response = self.session.get(
                f"{self.config.base_url}/uapi/domestic-stock/v1/quotations/inquire-price",
                headers=self.headers("FHKST01010100"),
                params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
                timeout=self.config.request_timeout_seconds,
            )
            output = response.json().get("output", {})
            self.mark_success()
            return {
                "current": float(output.get("stck_prpr", 0)),
                "ask1": float(output.get("askp1", 0)),
                "bid1": float(output.get("bidp1", 0)),
            }
        except Exception:
            self.mark_failure()
            return {"current": 0.0, "ask1": 0.0, "bid1": 0.0}

    def get_volume_rank(self, top_n: int = 50) -> list[str]:
        self.check_circuit()
        try:
            response = self.session.get(
                f"{self.config.base_url}/uapi/domestic-stock/v1/quotations/volume-rank",
                headers=self.headers("FHPST01710000"),
                params={
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_COND_SCR_DIV_CODE": "20171",
                    "FID_INPUT_ISCD": "0000",
                    "FID_DIV_CLS_CODE": "0",
                    "FID_BLNG_CLS_CODE": "0",
                    "FID_TRGT_CLS_CODE": "111111111",
                    "FID_TRGT_EXLS_CLS_CODE": "0000000000",
                    "FID_INPUT_PRICE_1": "",
                    "FID_INPUT_PRICE_2": "",
                    "FID_VOL_CNT": "",
                    "FID_INPUT_DATE_1": "",
                },
                timeout=self.config.request_timeout_seconds,
            )
            if response.status_code != 200:
                self.mark_failure(f"Volume rank HTTP {response.status_code}: {self._response_text(response)}")
                return []
            data = response.json()
            if data.get("rt_cd") != "0":
                self.mark_failure(
                    "Volume rank KIS "
                    f"rt_cd={data.get('rt_cd', '')} "
                    f"msg_cd={data.get('msg_cd', '')} "
                    f"msg1={data.get('msg1', '')}"
                )
                return []
            self.mark_success()
            from src.market_metadata import normalize_kr_order_symbol

            return [
                normalize_kr_order_symbol(row.get("mksc_shrn_iscd", ""))
                for row in data.get("output", [])
                if row.get("mksc_shrn_iscd", "").strip()
            ][:top_n]
        except Exception as exc:
            self.mark_failure(f"Volume rank exception: {exc}")
            return []

    @staticmethod
    def _response_text(response: Any) -> str:
        text = getattr(response, "text", "")
        if text:
            return str(text)[:500]
        try:
            return json.dumps(response.json(), ensure_ascii=False)[:500]
        except Exception:
            return ""

    def get_daily(self, symbol: str, n: int = 60) -> list[dict[str, Any]]:
        self.check_circuit()
        now = self.now()
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_DATE_1": (now - timedelta(days=365 * 3)).strftime("%Y%m%d"),
            "FID_INPUT_DATE_2": now.strftime("%Y%m%d"),
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "0",
        }
        try:
            response = self.session.get(
                f"{self.config.base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                headers=self.headers("FHKST03010100"),
                params=params,
                timeout=self.config.request_timeout_seconds,
            )
            if response.status_code != 200:
                self.mark_failure(
                    f"Daily chart HTTP {response.status_code} symbol={symbol}: "
                    f"{self._response_text(response)}"
                )
                return []
            data = response.json()
            if data.get("rt_cd") != "0":
                self.mark_failure(
                    "Daily chart KIS "
                    f"symbol={symbol} "
                    f"rt_cd={data.get('rt_cd', '')} "
                    f"msg_cd={data.get('msg_cd', '')} "
                    f"msg1={data.get('msg1', '')}"
                )
                return []
            self.mark_success()
            return data.get("output2", [])[:n]
        except Exception as exc:
            self.mark_failure(f"Daily chart exception symbol={symbol}: {exc}")
            return []

    def place_order(self, symbol: str, order_type: str, price: int, qty: int, exchange_id: str = "KRX") -> dict[str, Any]:
        if self.config.is_demo:
            tr_id = "VTTC0802U" if order_type == "buy" else "VTTC0801U"
        else:
            tr_id = "TTTC0802U" if order_type == "buy" else "TTTC0801U"
        body = {
            "CANO": self.config.account_prefix,
            "ACNT_PRDT_CD": self.config.account_suffix,
            "PDNO": symbol,
            "ORD_DVSN": "01" if price == 0 else "00",
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(price),
        }
        if exchange_id:
            body["EXCG_ID_DVSN_CD"] = exchange_id
        headers = self.headers(tr_id)
        hashkey = self.create_hashkey(body)
        if hashkey:
            headers["hashkey"] = hashkey
        self.check_circuit()
        try:
            response = self.session.post(
                f"{self.config.base_url}/uapi/domestic-stock/v1/trading/order-cash",
                headers=headers,
                json=body,
                timeout=self.config.request_timeout_seconds,
            )
            response.raise_for_status()
            self.mark_success()
            return response.json()
        except Exception as exc:
            self.mark_failure()
            return {"rt_cd": "1", "msg1": str(exc)}

    def revise_domestic_order(
        self,
        original_order_no: str,
        *,
        qty: int,
        price: int,
        order_division: str = "00",
        original_order_branch: str = "",
        exchange_id: str = "KRX",
    ) -> dict[str, Any]:
        return self._revise_or_cancel_domestic_order(
            original_order_no,
            revision_type="01",
            qty=qty,
            price=price,
            order_division=order_division,
            original_order_branch=original_order_branch,
            exchange_id=exchange_id,
        )

    def cancel_domestic_order(
        self,
        original_order_no: str,
        *,
        qty: int = 0,
        order_division: str = "00",
        original_order_branch: str = "",
        exchange_id: str = "KRX",
        cancel_all: bool = True,
    ) -> dict[str, Any]:
        return self._revise_or_cancel_domestic_order(
            original_order_no,
            revision_type="02",
            qty=qty,
            price=0,
            order_division=order_division,
            original_order_branch=original_order_branch,
            exchange_id=exchange_id,
            all_order=cancel_all,
        )

    def _revise_or_cancel_domestic_order(
        self,
        original_order_no: str,
        *,
        revision_type: str,
        qty: int,
        price: int,
        order_division: str,
        original_order_branch: str,
        exchange_id: str,
        all_order: bool = False,
    ) -> dict[str, Any]:
        tr_id = "VTTC0803U" if self.config.is_demo else "TTTC0803U"
        body = {
            "CANO": self.config.account_prefix,
            "ACNT_PRDT_CD": self.config.account_suffix,
            "KRX_FWDG_ORD_ORGNO": original_order_branch,
            "ORGN_ODNO": str(original_order_no),
            "ORD_DVSN": order_division,
            "RVSE_CNCL_DVSN_CD": revision_type,
            "ORD_QTY": str(int(qty)),
            "ORD_UNPR": str(int(price)),
            "QTY_ALL_ORD_YN": "Y" if all_order else "N",
        }
        if exchange_id:
            body["EXCG_ID_DVSN_CD"] = exchange_id
        headers = self.headers(tr_id)
        hashkey = self.create_hashkey(body)
        if hashkey:
            headers["hashkey"] = hashkey
        self.check_circuit()
        try:
            response = self.session.post(
                f"{self.config.base_url}/uapi/domestic-stock/v1/trading/order-rvsecncl",
                headers=headers,
                json=body,
                timeout=self.config.request_timeout_seconds,
            )
            response.raise_for_status()
            self.mark_success()
            return response.json()
        except Exception as exc:
            self.mark_failure()
            return {"rt_cd": "1", "msg1": str(exc)}

    def get_overseas_balance(self) -> dict[str, Any]:
        self.check_circuit()
        tr_id = "VTRP6504R" if self.config.is_demo else "CTRP6504R"
        params = {
            "CANO": self.config.account_prefix,
            "ACNT_PRDT_CD": self.config.account_suffix,
            "WCRC_FRCR_DVSN_CD": "02",
            "NATN_CD": "840",
            "TR_MKET_CD": "00",
            "INQR_DVSN_CD": "00",
        }
        last_error = ""
        for _attempt in range(2):
            try:
                response = self.session.get(
                    f"{self.config.base_url}/uapi/overseas-stock/v1/trading/inquire-present-balance",
                    headers=self.headers(tr_id),
                    params=params,
                    timeout=self.config.request_timeout_seconds,
                )
                response.raise_for_status()
                data = response.json()
                if data.get("rt_cd") == "0":
                    self.mark_success()
                    return data
                last_error = str(data.get("msg1", "unknown KIS balance error"))
            except Exception as exc:
                last_error = str(exc)
        self.mark_failure(last_error)
        return {"output1": [], "output2": {}, "_error": last_error or "unknown KIS balance error"}

    @staticmethod
    def _parse_exchange_map(raw: str | None = None) -> dict[str, str]:
        text = raw if raw is not None else os.environ.get("MISTOCK_EXCHANGE_MAP", "")
        aliases = {
            "NAS": "NASD",
            "NASDAQ": "NASD",
            "NASD": "NASD",
            "NYS": "NYSE",
            "NYSE": "NYSE",
            "AMS": "AMEX",
            "AMEX": "AMEX",
        }
        mapping: dict[str, str] = {}
        for chunk in str(text or "").replace(";", ",").split(","):
            if "=" not in chunk:
                continue
            symbol, exchange = chunk.split("=", 1)
            symbol_key = symbol.strip().upper()
            exchange_code = aliases.get(exchange.strip().upper())
            if symbol_key and exchange_code:
                mapping[symbol_key] = exchange_code
        return mapping

    def _parse_us_symbol(self, symbol: str) -> tuple[str, str, str]:
        """
        Parses symbol and returns (clean_symbol, KIS_excd, KIS_ovrs_excg_cd)
        excd: NAS, NYS, AMS, etc. (for quotations)
        ovrs_excg_cd: NASD, NYSE, AMEX, etc. (for trading)
        """
        quotation_codes = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS"}
        symbol_upper = symbol.upper().strip()
        if ":" in symbol_upper:
            exch, sym = symbol_upper.split(":", 1)
            sym = sym.strip()
            if exch in {"NAS", "NASD", "NASDAQ"}:
                return sym, "NAS", "NASD"
            elif exch in {"NYS", "NYSE"}:
                return sym, "NYS", "NYSE"
            elif exch in {"AMS", "AMEX"}:
                return sym, "AMS", "AMEX"
            else:
                return sym, "NAS", "NASD"

        mapped_exchange = self._parse_exchange_map().get(symbol_upper)
        if mapped_exchange:
            return symbol_upper, quotation_codes[mapped_exchange], mapped_exchange

        # Check standard NASDAQ_UNIVERSE
        try:
            from src.mistock.strategy import NASDAQ_UNIVERSE
            is_nasdaq = symbol_upper in NASDAQ_UNIVERSE
        except Exception:
            is_nasdaq = True # default fallback
        
        if is_nasdaq:
            return symbol_upper, "NAS", "NASD"
        else:
            return symbol_upper, "NYS", "NYSE"

    def get_overseas_quote(self, symbol: str) -> dict[str, float]:
        self.check_circuit()
        clean_symbol, excd, _ = self._parse_us_symbol(symbol)
        try:
            response = self.session.get(
                f"{self.config.base_url}/uapi/overseas-stock/v1/quotations/price",
                headers=self.headers("HHDFS00000300"),
                params={"EXCD": excd, "SYMB": clean_symbol},
                timeout=self.config.request_timeout_seconds,
            )
            data = response.json()
            output = data.get("output", {})
            self.mark_success()
            return {
                "current": float(output.get("last", 0) or 0.0),
                "ask1": float(output.get("askp1", 0) or 0.0),
                "bid1": float(output.get("bidp1", 0) or 0.0),
            }
        except Exception:
            self.mark_failure()
            return {"current": 0.0, "ask1": 0.0, "bid1": 0.0}

    def get_overseas_volume_rank(self, excd: str = "NAS", cnt: int = 30) -> list[str]:
        self.check_circuit()
        try:
            response = self.session.get(
                f"{self.config.base_url}/uapi/overseas-stock/v1/ranking/trade-pbmn",
                headers=self.headers("HHDFS76320010"),
                params={
                    "EXCD": excd,
                    "CNT": str(cnt),
                },
                timeout=self.config.request_timeout_seconds,
            )
            data = response.json()
            output = data.get("output", [])
            self.mark_success()
            symbols = []
            for item in output:
                sym = item.get("symb", "").strip()
                if sym:
                    symbols.append(sym)
            return symbols
        except Exception as e:
            from src.utils.logger import logger
            logger.warning(f"Failed to fetch overseas volume rank (EXCD={excd}): {e}")
            self.mark_failure()
            return []

    def place_overseas_order(self, symbol: str, order_type: str, price: float, qty: float) -> dict[str, Any]:
        if self.config.is_demo:
            tr_id = "VTTT1002U" if order_type == "buy" else "VTTT1006U"
        else:
            tr_id = "JTTT1002U" if order_type == "buy" else "JTTT1006U"
        
        clean_symbol, _, ovrs_excg_cd = self._parse_us_symbol(symbol)
        if not float(qty).is_integer():
            return {"rt_cd": "1", "msg1": f"해외주식 실거래 주문은 정수 수량만 지원합니다. 요청 수량: {qty}"}
            
        body = {
            "CANO": self.config.account_prefix,
            "ACNT_PRDT_CD": self.config.account_suffix,
            "OVRS_EXCG_CD": ovrs_excg_cd,
            "PDNO": clean_symbol,
            "ORD_DVSN": "00",
            "ORD_QTY": str(int(qty)),
            "OVRS_ORD_UNPR": f"{price:.2f}",
            "ORD_SVR_DVSN_CD": "0",
        }
        headers = self.headers(tr_id)
        hashkey = self.create_hashkey(body)
        if hashkey:
            headers["hashkey"] = hashkey
        self.check_circuit()
        try:
            response = self.session.post(
                f"{self.config.base_url}/uapi/overseas-stock/v1/trading/order",
                headers=headers,
                json=body,
                timeout=self.config.request_timeout_seconds,
            )
            try:
                data = response.json()
            except Exception:
                data = {}
            status_code = getattr(response, "status_code", 0)
            if status_code and status_code >= 400:
                err_msg = data.get("msg1") if isinstance(data, dict) else ""
                if not err_msg:
                    err_msg = getattr(response, "text", "")
                self.mark_failure(f"HTTP {status_code}: {err_msg}")
                raw_text = getattr(response, "text", "")
                return {
                    "rt_cd": "1",
                    "msg_cd": data.get("msg_cd") if isinstance(data, dict) else None,
                    "msg1": (
                        data.get("msg1")
                        if isinstance(data, dict) and data.get("msg1")
                        else f"HTTP {status_code} from KIS overseas order API"
                    ),
                    "status_code": status_code,
                    "output": data.get("output") if isinstance(data, dict) else None,
                    "raw": data if isinstance(data, dict) else raw_text,
                    "request": {
                        "tr_id": tr_id,
                        "OVRS_EXCG_CD": ovrs_excg_cd,
                        "PDNO": clean_symbol,
                        "ORD_DVSN": body["ORD_DVSN"],
                        "ORD_QTY": body["ORD_QTY"],
                        "OVRS_ORD_UNPR": body["OVRS_ORD_UNPR"],
                        "ORD_SVR_DVSN_CD": body["ORD_SVR_DVSN_CD"],
                    },
                }
            self.mark_success()
            return data
        except Exception as exc:
            self.mark_failure()
            return {"rt_cd": "1", "msg1": str(exc)}

    def revise_overseas_order(self, symbol: str, original_order_no: str, *, price: float, qty: float) -> dict[str, Any]:
        return self._revise_or_cancel_overseas_order(symbol, original_order_no, revision_type="01", price=price, qty=qty)

    def cancel_overseas_order(self, symbol: str, original_order_no: str, *, qty: float = 0) -> dict[str, Any]:
        return self._revise_or_cancel_overseas_order(symbol, original_order_no, revision_type="02", price=0.0, qty=qty)

    def _revise_or_cancel_overseas_order(
        self,
        symbol: str,
        original_order_no: str,
        *,
        revision_type: str,
        price: float,
        qty: float,
    ) -> dict[str, Any]:
        tr_id = "VTTT1004U" if self.config.is_demo else "JTTT1004U"
        clean_symbol, _, ovrs_excg_cd = self._parse_us_symbol(symbol)
        body = {
            "CANO": self.config.account_prefix,
            "ACNT_PRDT_CD": self.config.account_suffix,
            "OVRS_EXCG_CD": ovrs_excg_cd,
            "PDNO": clean_symbol,
            "ORGN_ODNO": str(original_order_no),
            "RVSE_CNCL_DVSN_CD": revision_type,
            "ORD_QTY": str(int(qty)),
            "OVRS_ORD_UNPR": f"{price:.2f}",
        }
        headers = self.headers(tr_id)
        hashkey = self.create_hashkey(body)
        if hashkey:
            headers["hashkey"] = hashkey
        self.check_circuit()
        try:
            response = self.session.post(
                f"{self.config.base_url}/uapi/overseas-stock/v1/trading/order-rvsecncl",
                headers=headers,
                json=body,
                timeout=self.config.request_timeout_seconds,
            )
            response.raise_for_status()
            self.mark_success()
            return response.json()
        except Exception as exc:
            self.mark_failure()
            return {"rt_cd": "1", "msg1": str(exc)}

    def get_condition_search_list(self, user_id: str) -> list[dict[str, Any]]:
        self.check_circuit()
        params = {
            "user_id": user_id,
            "seq": "0"
        }
        try:
            response = self.session.get(
                f"{self.config.base_url}/uapi/domestic-stock/v1/quotations/inquire-condition-search",
                headers=self.headers("HHKST03900400"),
                params=params,
                timeout=self.config.request_timeout_seconds,
            )
            data = response.json()
            self.mark_success()
            return data.get("output", [])
        except Exception:
            self.mark_failure()
            return []

    def get_condition_search_result(self, user_id: str, condition_no: str, condition_name: str) -> list[str]:
        self.check_circuit()
        params = {
            "user_id": user_id,
            "seq": condition_no,
            "cond_nm": condition_name,
        }
        try:
            response = self.session.get(
                f"{self.config.base_url}/uapi/domestic-stock/v1/quotations/inquire-condition-search-result",
                headers=self.headers("HHKST03900300"),
                params=params,
                timeout=self.config.request_timeout_seconds,
            )
            data = response.json()
            self.mark_success()
            output = data.get("output", [])
            return [row["code"].strip() for row in output if row.get("code")]
        except Exception:
            self.mark_failure()
            return []
