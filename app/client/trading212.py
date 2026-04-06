import time
from typing import Any, Optional
import requests
from app.config import settings


class Trading212Client:
    def __init__(self, api_key: str | None = None, api_secret: str | None = None):
        self.base_url = settings.t212_base_url
        self.session = requests.Session()
        self.session.auth = (
            api_key or settings.t212_api_key,
            api_secret or settings.t212_api_secret,
        )

    def _request(self, method: str, path: str, **kwargs) -> Any:
        url = f"{self.base_url}{path}"
        response = self.session.request(method, url, **kwargs)

        if response.status_code == 429:
            reset = int(response.headers.get("x-ratelimit-reset", time.time() + 60))
            wait = max(reset - time.time(), 1)
            print(f"Rate limited. Waiting {wait:.1f}s...")
            time.sleep(wait)
            return self._request(method, path, **kwargs)

        response.raise_for_status()
        return response.json()

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

    def get_transactions(self, next_page: Optional[str] = None, limit: int = 50) -> dict:
        # T212 returns nextPagePath as a full query string, e.g.
        # "limit=50&cursor=019d1a1c-...&time=2026-03-23T09:52:50.982Z"
        # Parse it directly rather than nesting it as a single param value.
        if next_page:
            from urllib.parse import parse_qs
            params = {k: v[0] for k, v in parse_qs(next_page).items()}
        else:
            params = {"limit": limit}
        return self._request("GET", "/history/transactions", params=params)

    def get_dividend_payments(self, next_page: Optional[str] = None, limit: int = 50) -> dict:
        if next_page:
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(next_page)
            params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        else:
            params = {"limit": limit}
        return self._request("GET", "/history/dividends", params=params)

    def get_order_history(self, next_page: Optional[str] = None, limit: int = 50) -> dict:
        if next_page:
            from urllib.parse import parse_qs
            params = {k: v[0] for k, v in parse_qs(next_page).items()}
        else:
            params = {"limit": limit}
        return self._request("GET", "/equity/history/orders", params=params)
