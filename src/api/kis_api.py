import hashlib
import json
import threading
import time
from datetime import datetime, timedelta
import requests
from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential
from pathlib import Path

from src.config import config
from src.utils.logger import logger
from src.notifier.slack import slack_error
from src.online_access import require_online_access
from urllib3.util import Retry
from requests.adapters import HTTPAdapter

HTTP = requests.Session()
HTTP.trust_env = False

# Mount HTTPAdapter with automatic retries for transient errors (connection, 500, 502, 503, 504)
_adapter = HTTPAdapter(
    max_retries=Retry(
        total=3,
        backoff_factor=1.0,
        status_forcelist=[500, 502, 503, 504],
        raise_on_status=False
    )
)
HTTP.mount("http://", _adapter)
HTTP.mount("https://", _adapter)

# KIS API 전역 스로틀: 초당 최대 1회 요청 강제 (EGW00201 방지)
_KIS_THROTTLE_LOCK = threading.Lock()
_KIS_LAST_CALL: float = 0.0
_KIS_MIN_INTERVAL: float = 0.5  # 초 단위 (EGW00201 방지)


def _kis_throttle() -> None:
    """KIS API 호출 전 최소 간격을 보장합니다."""
    global _KIS_LAST_CALL
    with _KIS_THROTTLE_LOCK:
        elapsed = time.monotonic() - _KIS_LAST_CALL
        if elapsed < _KIS_MIN_INTERVAL:
            time.sleep(_KIS_MIN_INTERVAL - elapsed)
        _KIS_LAST_CALL = time.monotonic()


class KISConfigError(RuntimeError):
    """Non-retryable KIS app/environment mismatch."""


class KISRateLimitError(RuntimeError):
    """Non-retryable KIS API rate limit response."""


class KISAccountError(RuntimeError):
    """Non-retryable KIS account number/product code error."""


NON_RETRYABLE_KIS_ERRORS = (KISConfigError, KISAccountError)


class KIStockAPI:
    TOKEN_CACHE = Path("data") / "kis_token.json"
    ETF_MARKET_CODES = {
        "102110", "133690", "148020", "152100", "157490",
        "229200", "251340", "261240", "273130", "278530",
        "305720", "381170", "448290", "481190",
    }
    
    _err_count = 0
    MAX_ERRORS = 5

    @staticmethod
    def _app_key_hash() -> str:
        return hashlib.sha256(config.kistock_app_key.encode("utf-8")).hexdigest()
    
    def __init__(self, notify_errors: bool = True) -> None:
        require_online_access("KIS API access")
        self.notify_errors = notify_errors
        self.base_url = "https://openapi.koreainvestment.com:9443" if config.trading_env == "real" else "https://openapivts.koreainvestment.com:29443"
        if config.trading_env == "real":
            HTTP.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"})
        else:
            HTTP.headers.update({"User-Agent": "python-requests/2.31.0"})
        self.access_token = self._load_or_fetch_token()

    def _load_or_fetch_token(self) -> str:
        if self.TOKEN_CACHE.exists():
            try:
                cached = json.loads(self.TOKEN_CACHE.read_text(encoding="utf-8"))
                expires_at = datetime.fromisoformat(cached["expires_at"])
                if (
                    cached.get("trading_env") == config.trading_env
                    and cached.get("app_key_hash") == self._app_key_hash()
                    and expires_at > datetime.now() + timedelta(minutes=5)
                ):
                    return cached["token"]
            except Exception:
                pass
        return self._fetch_token()

    def _fetch_token(self) -> str:
        _kis_throttle()
        url = f"{self.base_url}/oauth2/tokenP"
        body = {"grant_type": "client_credentials", "appkey": config.kistock_app_key, "appsecret": config.kistock_app_secret}
        try:
            r = HTTP.post(url, json=body, timeout=10)
            if r.status_code != 200:
                logger.error(f"Token fetch HTTP {r.status_code}: {r.text}")
                r.raise_for_status()
            data = r.json()
            token = data.get("access_token", "")
            expires_at = datetime.now() + timedelta(hours=23)
            self.TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
            self.TOKEN_CACHE.write_text(
                json.dumps({
                    "token": token,
                    "expires_at": expires_at.isoformat(),
                    "trading_env": config.trading_env,
                    "app_key_hash": self._app_key_hash(),
                    "base_url": self.base_url,
                    "app_key_prefix": config.kistock_app_key[:8]
                }),
                encoding="utf-8",
            )
            return token
        except Exception as e:
            logger.error(f"Failed to fetch KIS token: {e}")
            raise

    def _headers(self, tr_id: str) -> dict:
        return {
            "authorization": f"Bearer {self.access_token}",
            "appkey": config.kistock_app_key,
            "appsecret": config.kistock_app_secret,
            "tr_id": tr_id,
            "custtype": "P",
            "Content-Type": "application/json",
        }

    def _hashkey(self, payload: dict) -> str:
        try:
            response = HTTP.post(
                f"{self.base_url}/uapi/hashkey",
                headers={
                    "content-type": "application/json",
                    "appkey": config.kistock_app_key,
                    "appsecret": config.kistock_app_secret,
                },
                json=payload,
                timeout=10,
            )
            return response.json().get("HASH", "")
        except Exception:
            return ""

    @classmethod
    def circuit_status(cls) -> dict:
        return {
            "opened": cls._err_count >= cls.MAX_ERRORS,
            "error_count": cls._err_count,
            "max_errors": cls.MAX_ERRORS,
        }

    @classmethod
    def reset_circuit(cls) -> None:
        cls._err_count = 0

    @classmethod
    def _fail(cls) -> None:
        cls._err_count = min(cls.MAX_ERRORS, cls._err_count + 1)

    @classmethod
    def _success(cls) -> None:
        cls._err_count = 0

    def _record_result(self, data: dict) -> None:
        if data.get("rt_cd") == "0":
            self._success()
        else:
            self._fail()

    def _kis_error(self, data: dict, fallback: str) -> Exception:
        msg = data.get("msg1", fallback)
        if data.get("msg_cd") == "EGW02004":
            return KISConfigError(msg)
        if data.get("msg_cd") == "EGW00201":
            return KISRateLimitError(msg)
        if "CHECK_ACNO" in msg or "INVALID_CHECK_ACNO" in msg:
            return KISAccountError(msg)
        return Exception(msg)

    def _response_json(self, response: requests.Response, context: str) -> dict:
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code != 200:
            msg = data.get("msg1") or response.text
            logger.error(f"{context} HTTP {response.status_code}: {response.text}")
            if data.get("msg_cd") == "EGW02004":
                raise KISConfigError(msg)
            if data.get("msg_cd") == "EGW00201":
                raise KISRateLimitError(msg)
            if "CHECK_ACNO" in msg or "INVALID_CHECK_ACNO" in msg:
                raise KISAccountError(msg)
            raise RuntimeError(f"{context} HTTP {response.status_code}: {msg or 'KIS API request failed'}")
        return data

    @retry(
        retry=retry_if_not_exception_type(NON_RETRYABLE_KIS_ERRORS),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2, min=3, max=30),
        reraise=True,
    )
    def get_balance(self) -> dict:
        try:
            _kis_throttle()
            tr_id = "VTTC8434R" if config.trading_env == "demo" else "TTTC8434R"
            url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
            cano = config.kistock_account[:8]
            acnt = config.kistock_account[8:] if len(config.kistock_account) > 8 else "01"
            params = {"CANO": cano, "ACNT_PRDT_CD": acnt, "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
            r = HTTP.get(url, headers=self._headers(tr_id), params=params, timeout=15)
            data = self._response_json(r, "Balance")
            if data.get("rt_cd") != "0":
                raise self._kis_error(data, "unknown KIS balance error")
            self._success()
            return data
        except Exception:
            self._fail()
            raise

    @retry(
        retry=retry_if_not_exception_type(NON_RETRYABLE_KIS_ERRORS),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    def get_quote(self, symbol: str) -> dict:
        try:
            _kis_throttle()
            r = HTTP.get(f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price", headers=self._headers("FHKST01010100"), params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol}, timeout=10)
            data = self._response_json(r, "Quote")
            if data.get("rt_cd") != "0":
                raise self._kis_error(data, "KIS get_quote error")
            self._success()
            output = data.get("output", {})
            return {"current": float(output.get("stck_prpr", 0)), "ask1": float(output.get("askp1", 0)), "bid1": float(output.get("bidp1", 0))}
        except Exception:
            self._fail()
            raise

    @retry(
        retry=retry_if_not_exception_type(NON_RETRYABLE_KIS_ERRORS),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    def get_volume_rank(self, top_n: int = 50) -> list[str]:
        try:
            _kis_throttle()
            url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/volume-rank"
            params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_COND_SCR_DIV_CODE": "20171", "FID_INPUT_ISCD": "0000", "FID_DIV_CLS_CODE": "0", "FID_BLNG_CLS_CODE": "0", "FID_TRGT_CLS_CODE": "111111111", "FID_TRGT_EXLS_CLS_CODE": "0000000000", "FID_INPUT_PRICE_1": "", "FID_INPUT_PRICE_2": "", "FID_VOL_CNT": "", "FID_INPUT_DATE_1": ""}
            r = HTTP.get(url, headers=self._headers("FHPST01710000"), params=params, timeout=15)
            data = self._response_json(r, "Volume rank")
            self._record_result(data)
            if data.get("rt_cd") != "0":
                return []
            from src.market_metadata import normalize_kr_order_symbol

            codes = [
                normalize_kr_order_symbol(row.get("mksc_shrn_iscd", ""))
                for row in data.get("output", [])
                if row.get("mksc_shrn_iscd", "").strip()
            ]
            return codes[:top_n]
        except Exception:
            self._fail()
            raise

    @retry(
        retry=retry_if_not_exception_type(NON_RETRYABLE_KIS_ERRORS),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    def get_daily(self, symbol: str, n: int = 60) -> list:
        try:
            _kis_throttle()
            today = datetime.now().strftime("%Y%m%d")
            start = (datetime.now() - timedelta(days=365 * 3)).strftime("%Y%m%d")
            mrkt_div = "J"
            params = {"FID_COND_MRKT_DIV_CODE": mrkt_div, "FID_INPUT_ISCD": symbol, "FID_INPUT_DATE_1": start, "FID_INPUT_DATE_2": today, "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"}
            r = HTTP.get(f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice", headers=self._headers("FHKST03010100"), params=params, timeout=15)
            data = self._response_json(r, "Daily chart")
            self._record_result(data)
            if data.get("rt_cd") != "0":
                return []
            return data.get("output2", [])[:n]
        except Exception:
            self._fail()
            raise

    @retry(
        retry=retry_if_not_exception_type(NON_RETRYABLE_KIS_ERRORS),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    def place_order(self, symbol: str, order_type: str, price: int, qty: int) -> dict:
        require_online_access("KIS order submission")
        real_orders_enabled = (not config.dry_run) and config.trading_env == "real" and config.enable_live_trading
        order_submission_enabled = (not config.dry_run) and (config.trading_env == "demo" or real_orders_enabled)
        if not order_submission_enabled:
            return {"rt_cd": "0", "msg1": "DRY_RUN"}
        _kis_throttle()
        tr_id = ("VTTC0802U" if config.trading_env == "demo" else "TTTC0802U") if order_type == "buy" else ("VTTC0801U" if config.trading_env == "demo" else "TTTC0801U")
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        body = {"CANO": config.kistock_account[:8], "ACNT_PRDT_CD": config.kistock_account[8:] if len(config.kistock_account) > 8 else "01", "PDNO": symbol, "ORD_DVSN": "01" if price == 0 else "00", "ORD_QTY": str(qty), "ORD_UNPR": str(price)}
        try:
            headers = self._headers(tr_id)
            hashkey = self._hashkey(body)
            if hashkey:
                headers["hashkey"] = hashkey
            r = HTTP.post(url, headers=headers, json=body, timeout=15)
            data = self._response_json(r, "Order")
            self._record_result(data)
            return data
        except Exception:
            self._fail()
            raise

    @retry(
        retry=retry_if_not_exception_type(NON_RETRYABLE_KIS_ERRORS),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    def cancel_order(
        self,
        order_no: str,
        *,
        qty: int = 0,
        order_division: str = "00",
        original_order_branch: str = "",
        exchange_id: str = "KRX",
        cancel_all: bool = True,
    ) -> dict:
        """Cancel a domestic cash order in the configured demo/real account."""
        require_online_access("KIS order cancellation")
        real_orders_enabled = (
            not config.dry_run
            and config.trading_env == "real"
            and config.enable_live_trading
        )
        if config.dry_run or not (
            config.trading_env == "demo" or real_orders_enabled
        ):
            return {"rt_cd": "0", "msg1": "DRY_RUN"}
        order_no = str(order_no or "").strip()
        if not order_no:
            raise ValueError("order_no is required")
        body = {
            "CANO": config.kistock_account[:8],
            "ACNT_PRDT_CD": (
                config.kistock_account[8:]
                if len(config.kistock_account) > 8
                else "01"
            ),
            "KRX_FWDG_ORD_ORGNO": str(original_order_branch or ""),
            "ORGN_ODNO": order_no,
            "ORD_DVSN": str(order_division or "00"),
            "RVSE_CNCL_DVSN_CD": "02",
            "ORD_QTY": str(max(0, int(qty))),
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y" if cancel_all else "N",
            "EXCG_ID_DVSN_CD": str(exchange_id or "KRX"),
        }
        tr_id = "VTTC0803U" if config.trading_env == "demo" else "TTTC0803U"
        try:
            _kis_throttle()
            headers = self._headers(tr_id)
            hashkey = self._hashkey(body)
            if hashkey:
                headers["hashkey"] = hashkey
            response = HTTP.post(
                f"{self.base_url}/uapi/domestic-stock/v1/trading/order-rvsecncl",
                headers=headers,
                json=body,
                timeout=15,
            )
            data = self._response_json(response, "Order cancellation")
            self._record_result(data)
            return data
        except Exception:
            self._fail()
            raise

    def get_order_snapshot(
        self,
        order_no: str,
        *,
        order_date: str | None = None,
    ) -> dict:
        """Return one normalized domestic order snapshot for reconciliation."""
        order_no = str(order_no or "").strip()
        if not order_no:
            raise ValueError("order_no is required")
        day = str(order_date or datetime.now().strftime("%Y%m%d")).replace("-", "")
        rows = self.get_trade_history(day, day)
        row = next(
            (
                item
                for item in rows
                if str(item.get("odno") or item.get("ODNO") or "").strip()
                == order_no
            ),
            None,
        )
        if row is None:
            return {
                "status": "unknown",
                "outcome_unknown": True,
                "broker_order_id": order_no,
                "message": "KIS order was not found in the requested history window",
            }
        requested = _row_int(row, "ord_qty", "ORD_QTY")
        filled = _row_int(
            row, "tot_ccld_qty", "TOT_CCLD_QTY", "ccld_qty", "CCLD_QTY"
        )
        remaining_keys = ("rmn_qty", "RMN_QTY", "ord_psbl_qty")
        has_remaining = any(row.get(key) not in (None, "") for key in remaining_keys)
        remaining = _row_int(row, *remaining_keys)
        average = _row_float(
            row, "avg_prvs", "AVG_PRVS", "avg_ccld_unpr", "AVG_CCLD_UNPR"
        )
        canceled = str(
            row.get("cncl_yn")
            or row.get("CNCL_YN")
            or row.get("rvse_cncl_dvsn_name")
            or ""
        ).strip().upper()
        rejected = str(
            row.get("rjct_qty") or row.get("RJCT_QTY") or "0"
        ).strip()
        if filled > 0 and requested > 0 and filled >= requested:
            status = "filled"
        elif canceled in {"Y", "취소"} or (
            has_remaining and remaining == 0 and filled < requested
        ):
            status = "canceled"
        elif rejected not in {"", "0"} and filled == 0:
            status = "rejected"
        elif filled > 0:
            status = "partially_filled"
        else:
            status = "submitted"
        return {
            "status": status,
            "broker_order_id": order_no,
            "cumulative_filled_qty": filled,
            "average_fill_price": average,
            "requested_qty": requested,
            "remaining_qty": remaining,
            "payload": dict(row),
            "message": str(row.get("ord_dvsn_name") or status),
        }

    @retry(
        retry=retry_if_not_exception_type(NON_RETRYABLE_KIS_ERRORS),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    def get_trade_history(self, start_date: str, end_date: str) -> list:
        try:
            _kis_throttle()
            tr_ids = (
                ["VTTC0081R", "VTTC8001R"]
                if config.trading_env == "demo"
                else ["TTTC0081R", "TTTC8001R"]
            )
            url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
            cano = config.kistock_account[:8]
            acnt = config.kistock_account[8:] if len(config.kistock_account) > 8 else "01"
            params = {
                "CANO": cano,
                "ACNT_PRDT_CD": acnt,
                "INQR_STRT_DT": start_date,
                "INQR_END_DT": end_date,
                "SLL_BUY_DVSN_CD": "00",
                "INQR_DVSN": "00",
                "PDNO": "",
                "CCLD_DVSN": "00",
                "ORD_GNO_BRNO": "",
                "ODNO": "",
                "INQR_DVSN_3": "00",
                "INQR_DVSN_1": "",
                "EXCG_ID_DVSN_CD": "KRX",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": ""
            }
            last_error: Exception | None = None
            for tr_id in tr_ids:
                try:
                    rows = []
                    page_params = dict(params)
                    tr_cont = ""
                    seen_page_tokens: set[tuple[str, str]] = set()
                    page_count = 0
                    while True:
                        page_count += 1
                        if page_count > 100:
                            logger.warning(
                                "KIS trade history pagination stopped after 100 pages"
                            )
                            break
                        headers = self._headers(tr_id)
                        if tr_cont:
                            headers["tr_cont"] = tr_cont
                        try:
                            r = HTTP.get(url, headers=headers, params=page_params, timeout=15)
                            data = self._response_json(r, "Trade history")
                        except Exception as page_exc:
                            if rows:
                                logger.warning(
                                    "KIS trade history later page failed; "
                                    f"returning {len(rows)} rows already received: {page_exc}"
                                )
                                break
                            raise
                        if data.get("rt_cd") != "0":
                            raise self._kis_error(data, "unknown KIS trade history error")

                        next_fk = str(data.get("ctx_area_fk100") or data.get("CTX_AREA_FK100") or "").strip()
                        next_nk = str(data.get("ctx_area_nk100") or data.get("CTX_AREA_NK100") or "").strip()
                        response_headers = getattr(r, "headers", {}) or {}
                        tr_cont = str(response_headers.get("tr_cont") or response_headers.get("tr-cont") or "").strip()
                        page_token = (next_fk, next_nk)
                        if page_count > 1 and page_token in seen_page_tokens:
                            logger.warning(
                                "KIS trade history pagination returned a repeated token; "
                                f"stopping with {len(rows)} rows"
                            )
                            break
                        rows.extend(data.get("output1", []) or [])
                        if tr_cont not in {"M", "F"} or (not next_fk and not next_nk):
                            break
                        seen_page_tokens.add(page_token)
                        page_params["CTX_AREA_FK100"] = next_fk
                        page_params["CTX_AREA_NK100"] = next_nk
                        _kis_throttle()
                    self._success()
                    return rows
                except Exception as exc:
                    last_error = exc
            if last_error:
                raise last_error
            return []
        except Exception:
            self._fail()
            raise


    @retry(
        retry=retry_if_not_exception_type(NON_RETRYABLE_KIS_ERRORS),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    def get_condition_search_list(self, user_id: str) -> list[dict]:
        try:
            _kis_throttle()
            url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-condition-search"
            params = {"user_id": user_id, "seq": "0"}
            r = HTTP.get(url, headers=self._headers("HHKST03900400"), params=params, timeout=15)
            data = self._response_json(r, "HTS condition list")
            self._record_result(data)
            if data.get("rt_cd") != "0":
                return []
            return data.get("output", [])
        except Exception:
            self._fail()
            raise

    @retry(
        retry=retry_if_not_exception_type(NON_RETRYABLE_KIS_ERRORS),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    def get_condition_search_result(self, user_id: str, condition_no: str, condition_name: str) -> list[str]:
        try:
            _kis_throttle()
            url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-condition-search-result"
            params = {"user_id": user_id, "seq": condition_no, "cond_nm": condition_name}
            r = HTTP.get(url, headers=self._headers("HHKST03900300"), params=params, timeout=15)
            data = self._response_json(r, "HTS condition result")
            self._record_result(data)
            if data.get("rt_cd") != "0":
                return []
            output = data.get("output", [])
            return [row["code"].strip() for row in output if row.get("code")]
        except Exception:
            self._fail()
            raise


def _row_int(row: dict, *keys: str) -> int:
    for key in keys:
        if row.get(key) not in (None, ""):
            try:
                return max(0, int(float(str(row[key]).replace(",", ""))))
            except (TypeError, ValueError):
                continue
    return 0


def _row_float(row: dict, *keys: str) -> float:
    for key in keys:
        if row.get(key) not in (None, ""):
            try:
                return max(0.0, float(str(row[key]).replace(",", "")))
            except (TypeError, ValueError):
                continue
    return 0.0
