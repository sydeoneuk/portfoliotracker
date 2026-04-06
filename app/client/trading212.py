import time
import threading
from datetime import datetime
from typing import Any, Optional
import requests
from app.config import settings

_last_request_time: float = 0.0
_next_delay: float = 1.0          # dynamically updated from rate-limit headers
_MIN_DELAY: float = 0.3           # never go faster than this regardless of headers
_request_lock = threading.Lock()


class Trading212Client:
    def __init__(self, api_key: str | None = None, api_secret: str | None = None):
        self.base_url = settings.t212_base_url
        self.session = requests.Session()
        self.session.auth = (
            api_key or settings.t212_api_key,
            api_secret or settings.t212_api_secret,
        )

    def _request(self, method: str, path: str, **kwargs) -> Any:
        global _last_request_time, _next_delay

        # Enforce computed delay before firing the request
        with _request_lock:
            elapsed = time.time() - _last_request_time
            if elapsed < _next_delay:
                time.sleep(_next_delay - elapsed)
            _last_request_time = time.time()

        url = f"{self.base_url}{path}"
        response = self.session.request(method, url, **kwargs)

        if response.status_code == 429:
            reset_ts = int(response.headers.get("x-ratelimit-reset", time.time() + 60))
            wait = max(reset_ts - time.time(), 1)
            reset_utc = datetime.utcfromtimestamp(reset_ts).strftime("%H:%M:%S UTC")
            print(
                f"Rate limited. Waiting {wait:.1f}s — "
                f"limit={response.headers.get('x-ratelimit-limit', '?')} "
                f"period={response.headers.get('x-ratelimit-period', '?')}s "
                f"used={response.headers.get('x-ratelimit-used', '?')} "
                f"remaining={response.headers.get('x-ratelimit-remaining', '?')} "
                f"reset={reset_utc}"
            )
            time.sleep(wait)
            with _request_lock:
                _last_request_time = time.time()
                _next_delay = 1.0   # conservative reset after a limit hit
            return self._request(method, path, **kwargs)

        response.raise_for_status()

        # Debug: show what was called and how many rows came back
        data = response.json()
        if isinstance(data, list):
            row_count = len(data)
        elif isinstance(data, dict) and "items" in data:
            row_count = len(data["items"])
        else:
            row_count = None
        row_info = f" → {row_count} rows" if row_count is not None else ""
        print(f"  API {method} {path}{row_info}")

        # Adaptively compute next delay from rate-limit headers on every response
        try:
            remaining = int(response.headers["x-ratelimit-remaining"])
            reset_ts   = int(response.headers["x-ratelimit-reset"])
            time_left  = max(reset_ts - time.time(), 0)

            if remaining <= 1:
                # Almost out of quota — pause until the window resets
                computed = time_left + 0.5
                print(f"  Rate limit nearly exhausted, pausing {computed:.1f}s")
            elif time_left > 0:
                # Spread the remaining quota evenly, with a 20 % safety margin
                computed = (time_left / remaining) * 1.2
            else:
                computed = _MIN_DELAY  # window already reset

            with _request_lock:
                _next_delay = max(computed, _MIN_DELAY)
        except (KeyError, ValueError):
            pass   # headers absent — keep whatever delay was set previously

        return data

    def get_account_info(self) -> dict:
        return self._request("GET", "/equity/account/info")

    def get_account_cash(self) -> dict:
        return self._request("GET", "/equity/account/cash")

    def get_positions(self) -> list[dict]:
        return self._request("GET", "/equity/portfolio")

    def get_open_orders(self) -> list[dict]:
        return self._request("GET", "/equity/orders")

    def get_pies(self) -> list[dict]:
        return self._request("GET", "/equity/pies")

    def get_pie(self, pie_id: int) -> dict:
        return self._request("GET", f"/equity/pies/{pie_id}")

    def get_instruments(self) -> list[dict]:
        return self._request("GET", "/equity/metadata/instruments")

    @staticmethod
    def _pagination_params(next_page: str) -> dict:
        """Parse a nextPagePath that may be a full URL, a path, or a bare query string."""
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(next_page)
        # urlparse puts the query string in .query when there is a '?' in the input;
        # if there is no '?', the whole string is a bare query string (fall back to it).
        query = parsed.query if parsed.query else next_page
        return {k: v[0] for k, v in parse_qs(query).items()}

    def get_transactions(self, next_page: Optional[str] = None, limit: int = 50,
                         newer_than: Optional[str] = None) -> dict:
        if next_page:
            params = self._pagination_params(next_page)
        else:
            params = {"limit": limit}
            if newer_than:
                params["time"] = newer_than
        return self._request("GET", "/history/transactions", params=params)

    def get_dividend_payments(self, next_page: Optional[str] = None, limit: int = 50,
                              newer_than: Optional[str] = None) -> dict:
        if next_page:
            params = self._pagination_params(next_page)
        else:
            params = {"limit": limit}
            if newer_than:
                params["time"] = newer_than
        return self._request("GET", "/history/dividends", params=params)

    def get_order_history(self, next_page: Optional[str] = None, limit: int = 50,
                          newer_than: Optional[str] = None) -> dict:
        if next_page:
            params = self._pagination_params(next_page)
        else:
            params = {"limit": limit}
            if newer_than:
                params["time"] = newer_than
        return self._request("GET", "/equity/history/orders", params=params)
