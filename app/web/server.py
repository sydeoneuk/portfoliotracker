import datetime
import time
import traceback
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import yfinance as yf
from fastapi import FastAPI, Depends, Request, BackgroundTasks, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.database import get_session, SessionLocal
from app.auth.models import User, UserSettings
from app.auth.crypto import encrypt, decrypt
from app.auth.oauth import oauth

app = FastAPI(title="Trading 212 Dashboard")
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_sync_executor = ThreadPoolExecutor(max_workers=4)


# ── Formatting helpers ─────────────────────────────────────────────────────

def _fmt(value, decimals=2):
    if value is None:
        return "—"
    try:
        return f"{float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return "—"


_CCY_PREFIX = {"GBP": "£", "USD": "$", "EUR": "€", "CAD": "C$", "AUD": "A$", "CHF": "CHF "}
_CCY_SUFFIX = {"GBX": "p"}


def _cfmt(value, currency, decimals=2):
    if value is None:
        return "—"
    try:
        n = f"{float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return "—"
    prefix = _CCY_PREFIX.get(currency, "")
    suffix = _CCY_SUFFIX.get(currency, "")
    if prefix:
        return f"{prefix}{n}"
    if suffix:
        return f"{n}{suffix}"
    return f"{n} {currency}"


# ── Instrument classification ──────────────────────────────────────────────

def _classify(instrument_type: str, sector: str, industry: str,
              name: str, instrument_class: str | None) -> str:
    if instrument_class:
        return instrument_class

    t = (instrument_type or "").upper()
    s = (sector or "").lower()
    ind = (industry or "").lower()
    n = (name or "").lower()

    if t == "ETF":
        return "ETF"
    if t == "WARRANT":
        return "Warrant"
    if "reit" in ind or s == "real estate":
        return "REIT"

    _bdc_names = {"ares capital", "main street capital", "capital southwest",
                  "blue owl capital", "blackstone secured lending", "oxford lane",
                  "agnc investment", "cvc income"}
    if any(b in n for b in _bdc_names):
        return "BDC"

    _trust_keywords = ("investment trust", "inv trust", "inv. trust")
    if any(k in n for k in _trust_keywords):
        return "Inv. Trust"
    if " trust" in n and s in ("", "financial services") and t == "STOCK":
        return "Inv. Trust"

    if "fund" in n and t == "STOCK":
        return "Fund"

    if ind in ("asset management", "capital markets"):
        return "Asset Mgmt"

    if t == "STOCK":
        return "Stock"

    return t or "—"


# ── FX rate cache (1-hour TTL) ─────────────────────────────────────────────

_fx_cache: dict[str, float] = {}
_fx_cache_ts: float = 0.0
_FX_TTL = 3600


def _get_fx_rates_to_gbp(currencies: set[str]) -> dict[str, float]:
    global _fx_cache, _fx_cache_ts

    needed = currencies - {"GBP", "GBX"}
    now = time.time()

    if now - _fx_cache_ts < _FX_TTL and needed.issubset(_fx_cache):
        rates = dict(_fx_cache)
        rates["GBP"] = 1.0
        rates["GBX"] = 0.01
        return rates

    rates: dict[str, float] = {"GBP": 1.0, "GBX": 0.01}
    for ccy in needed:
        try:
            ticker = yf.Ticker(f"{ccy}GBP=X")
            rate = ticker.fast_info["last_price"]
            rates[ccy] = float(rate)
            _fx_cache[ccy] = float(rate)
        except Exception:
            if ccy in _fx_cache:
                rates[ccy] = _fx_cache[ccy]

    _fx_cache_ts = now
    return rates


# ── Auth helpers ───────────────────────────────────────────────────────────

def _get_current_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.query(User).filter(User.id == user_id).first()


def _require_user(request: Request, db: Session) -> User | RedirectResponse:
    user = _get_current_user(request, db)
    if not user:
        return _redirect("/login")
    return user


def _redirect(url: str, status_code: int = 302):
    return RedirectResponse(url, status_code=status_code)


def _get_or_create_settings(db: Session, user_id: int) -> UserSettings:
    s = db.query(UserSettings).filter_by(user_id=user_id).first()
    if not s:
        s = UserSettings(user_id=user_id, sync_status="idle")
        db.add(s)
        db.commit()
        db.refresh(s)
    return s


# ── Background sync ────────────────────────────────────────────────────────

def _run_sync(user_id: int) -> None:
    """Synchronous sync task — runs in thread pool."""
    db = SessionLocal()
    try:
        from app.sync.runner import SyncRunner

        user_settings = db.query(UserSettings).filter_by(user_id=user_id).first()
        if not user_settings:
            return

        user_settings.sync_status = "running"
        user_settings.sync_message = "Sync started…"
        db.commit()

        api_key = decrypt(user_settings.t212_api_key_enc)
        api_secret = decrypt(user_settings.t212_api_secret_enc)
        isa_key = decrypt(user_settings.t212_isa_api_key_enc)
        isa_secret = decrypt(user_settings.t212_isa_api_secret_enc)

        accounts = []
        if api_key:
            accounts.append(("Trading", api_key, api_secret))
        if isa_key:
            accounts.append(("ISA", isa_key, isa_secret))

        if not accounts:
            user_settings.sync_status = "error"
            user_settings.sync_message = "No API keys configured."
            db.commit()
            return

        for account_label, key, secret in accounts:
            runner = SyncRunner(db, account=account_label, api_key=key,
                                api_secret=secret, user_id=user_id)
            runner.sync_all()

        user_settings.last_sync_at = datetime.datetime.utcnow()
        user_settings.sync_status = "done"
        user_settings.sync_message = (
            f"Synced {len(accounts)} account(s) at "
            f"{user_settings.last_sync_at.strftime('%d %b %Y %H:%M')} UTC"
        )
        db.commit()
    except Exception as exc:
        try:
            user_settings = db.query(UserSettings).filter_by(user_id=user_id).first()
            if user_settings:
                user_settings.sync_status = "error"
                user_settings.sync_message = str(exc)
                db.commit()
        except Exception:
            pass
        traceback.print_exc()
    finally:
        db.close()


# ── Auth routes ────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, db: Session = Depends(get_session)):
    if _get_current_user(request, db):
        return _redirect("/")
    providers = []
    if settings.google_client_id:
        providers.append("google")
    if settings.microsoft_client_id:
        providers.append("microsoft")
    return templates.TemplateResponse(request, "login.html", {"providers": providers})


@app.get("/auth/google")
async def auth_google(request: Request):
    redirect_uri = f"{settings.app_base_url}/auth/google/callback"
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/auth/google/callback")
async def auth_google_callback(request: Request, db: Session = Depends(get_session)):
    token = await oauth.google.authorize_access_token(request)
    user_info = token.get("userinfo") or await oauth.google.userinfo(token=token)
    return _handle_oauth_callback(request, db, "google",
                                  str(user_info["sub"]),
                                  user_info.get("email", ""),
                                  user_info.get("name", ""),
                                  user_info.get("picture"))


@app.get("/auth/microsoft")
async def auth_microsoft(request: Request):
    redirect_uri = f"{settings.app_base_url}/auth/microsoft/callback"
    return await oauth.microsoft.authorize_redirect(request, redirect_uri)


@app.get("/auth/microsoft/callback")
async def auth_microsoft_callback(request: Request, db: Session = Depends(get_session)):
    token = await oauth.microsoft.authorize_access_token(
        request, claims_options={"iss": {"essential": False}}
    )
    user_info = token.get("userinfo") or await oauth.microsoft.userinfo(token=token)
    return _handle_oauth_callback(request, db, "microsoft",
                                  str(user_info.get("oid") or user_info["sub"]),
                                  user_info.get("email") or user_info.get("preferred_username", ""),
                                  user_info.get("name", ""),
                                  user_info.get("picture"))


def _handle_oauth_callback(request: Request, db: Session, provider: str,
                            provider_id: str, email: str, name: str,
                            avatar_url: str | None) -> RedirectResponse:
    user = db.query(User).filter_by(provider=provider, provider_id=provider_id).first()
    if not user:
        # Check if email already exists under a different provider
        user = db.query(User).filter_by(email=email).first()
    if not user:
        user = User(email=email, name=name, provider=provider,
                    provider_id=provider_id, avatar_url=avatar_url)
        db.add(user)
        db.flush()
        db.add(UserSettings(user_id=user.id, sync_status="idle"))
    else:
        user.name = name
        user.avatar_url = avatar_url
        user.last_login_at = datetime.datetime.utcnow()
    db.commit()
    request.session["user_id"] = user.id
    return _redirect("/")


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return _redirect("/login")


# ── Settings routes ────────────────────────────────────────────────────────

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_session)):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    user_settings = _get_or_create_settings(db, user.id)
    return templates.TemplateResponse(request, "settings.html", {
        "user": user,
        "user_settings": user_settings,
        "has_trading_key": bool(user_settings.t212_api_key_enc),
        "has_trading_secret": bool(user_settings.t212_api_secret_enc),
        "has_isa_key": bool(user_settings.t212_isa_api_key_enc),
        "has_isa_secret": bool(user_settings.t212_isa_api_secret_enc),
    })


@app.post("/settings")
async def save_settings(
    request: Request,
    db: Session = Depends(get_session),
    t212_api_key: str = Form(default=""),
    t212_api_secret: str = Form(default=""),
    t212_isa_api_key: str = Form(default=""),
    t212_isa_api_secret: str = Form(default=""),
):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    user_settings = _get_or_create_settings(db, user.id)

    if t212_api_key.strip():
        user_settings.t212_api_key_enc = encrypt(t212_api_key.strip())
    if t212_api_secret.strip():
        user_settings.t212_api_secret_enc = encrypt(t212_api_secret.strip())
    if t212_isa_api_key.strip():
        user_settings.t212_isa_api_key_enc = encrypt(t212_isa_api_key.strip())
    if t212_isa_api_secret.strip():
        user_settings.t212_isa_api_secret_enc = encrypt(t212_isa_api_secret.strip())

    db.commit()
    return _redirect("/settings", status_code=303)


@app.post("/settings/clear-key")
async def clear_key(
    request: Request,
    db: Session = Depends(get_session),
    key_type: str = Form(...),
):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    user_settings = _get_or_create_settings(db, user.id)
    if key_type == "trading":
        user_settings.t212_api_key_enc = None
        user_settings.t212_api_secret_enc = None
    elif key_type == "isa":
        user_settings.t212_isa_api_key_enc = None
        user_settings.t212_isa_api_secret_enc = None
    db.commit()
    return _redirect("/settings", status_code=303)


# ── Sync routes ────────────────────────────────────────────────────────────

@app.post("/sync")
async def trigger_sync(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_session),
):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    user_settings = _get_or_create_settings(db, user.id)

    if user_settings.sync_status == "running":
        return _redirect("/settings", status_code=303)

    user_settings.sync_status = "running"
    user_settings.sync_message = "Queued…"
    db.commit()

    background_tasks.add_task(_run_sync, user.id)
    return _redirect("/settings", status_code=303)


@app.get("/sync/status")
def sync_status(request: Request, db: Session = Depends(get_session)):
    from fastapi.responses import JSONResponse
    user = _get_current_user(request, db)
    if not user:
        return JSONResponse({"status": "unauthenticated"})
    user_settings = _get_or_create_settings(db, user.id)
    return JSONResponse({
        "status": user_settings.sync_status,
        "message": user_settings.sync_message or "",
        "last_sync_at": (
            user_settings.last_sync_at.isoformat()
            if user_settings.last_sync_at else None
        ),
    })


# ── Main dashboard ─────────────────────────────────────────────────────────

_COMBINED_SQL = text("""
    SELECT
        COALESCE(NULLIF(i.name, ''), i.short_name, p.ticker)  AS name,
        COALESCE(NULLIF(i.short_name, ''), p.ticker)           AS short_name,
        COALESCE(i.exchange, '—')                              AS exchange,
        COALESCE(i.currency_code, '—')                         AS currency,
        COALESCE(i.instrument_type, '—')                       AS instrument_type,
        i.sector,
        i.industry,
        i.instrument_class,
        CASE WHEN COUNT(DISTINCT p.account) > 1 THEN 'Both'
             ELSE MAX(p.account) END                           AS account,
        SUM(p.quantity)                                        AS quantity,
        SUM(p.quantity * p.average_price)
            / NULLIF(SUM(p.quantity), 0)                       AS average_price,
        MAX(p.current_price)                                   AS current_price,
        ROUND(SUM(p.quantity * p.average_price)::numeric, 2)  AS cost,
        ROUND(SUM(p.quantity * p.current_price)::numeric, 2)  AS value,
        ROUND(SUM(p.ppl)::numeric, 2)                         AS ppl,
        ROUND(((SUM(p.quantity * p.current_price)
               - SUM(p.quantity * p.average_price))
               / NULLIF(SUM(p.quantity * p.average_price), 0))::numeric, 4) AS result_coef,
        ROUND(COALESCE((
            SELECT SUM(dp.amount) FROM dividend_payments dp
            WHERE dp.ticker = p.ticker AND dp.user_id = :user_id
        ), 0)::numeric, 2)                                     AS total_dividends,
        ROUND(
            COALESCE((SELECT MAX(df.annual_rate) FROM dividend_forecast df
                      WHERE df.ticker = p.ticker), 0)::numeric
            * SUM(p.quantity)::numeric, 2)                     AS forward_dividends,
        COALESCE((
            SELECT MAX(df.annual_rate) FROM dividend_forecast df
            WHERE df.ticker = p.ticker
        ), 0)                                                   AS annual_rate_per_share,
        i.fcf_per_share_3y_avg,
        i.eps_ttm
    FROM positions p
    JOIN instruments i ON p.ticker = i.ticker
    WHERE p.user_id = :user_id
    GROUP BY p.ticker, i.name, i.short_name, i.exchange, i.currency_code,
             i.instrument_type, i.sector, i.industry, i.instrument_class,
             i.fcf_per_share_3y_avg, i.eps_ttm
    ORDER BY SUM(p.quantity * COALESCE(p.current_price, p.average_price)) DESC NULLS LAST
""")

_ACCOUNT_SQL = text("""
    SELECT
        COALESCE(NULLIF(i.name, ''), i.short_name, p.ticker)  AS name,
        COALESCE(NULLIF(i.short_name, ''), p.ticker)           AS short_name,
        COALESCE(i.exchange, '—')                              AS exchange,
        COALESCE(i.currency_code, '—')                         AS currency,
        COALESCE(i.instrument_type, '—')                       AS instrument_type,
        i.sector,
        i.industry,
        i.instrument_class,
        p.account,
        p.quantity,
        p.average_price,
        p.current_price,
        ROUND((p.quantity * p.average_price)::numeric, 2)     AS cost,
        ROUND((p.quantity * p.current_price)::numeric, 2)     AS value,
        ROUND(p.ppl::numeric, 2)                               AS ppl,
        ROUND(p.result_coef::numeric, 4)                       AS result_coef,
        ROUND(COALESCE((
            SELECT SUM(dp.amount) FROM dividend_payments dp
            WHERE dp.ticker = p.ticker AND dp.account = p.account
              AND dp.user_id = p.user_id
        ), 0)::numeric, 2)                                     AS total_dividends,
        ROUND(COALESCE((
            SELECT MAX(df.annual_rate) * p.quantity
            FROM dividend_forecast df WHERE df.ticker = p.ticker
        ), 0)::numeric, 2)                                     AS forward_dividends,
        COALESCE((
            SELECT MAX(df.annual_rate) FROM dividend_forecast df
            WHERE df.ticker = p.ticker
        ), 0)                                                   AS annual_rate_per_share,
        i.fcf_per_share_3y_avg,
        i.eps_ttm
    FROM positions p
    JOIN instruments i ON p.ticker = i.ticker
    WHERE p.user_id = :user_id AND p.account = :account
    ORDER BY (p.quantity * COALESCE(p.current_price, p.average_price)) DESC NULLS LAST
""")


def _load_pies(db: Session, user_id: int, account: str) -> list[dict]:
    """Return pies relevant to the current user and account filter.

    Pies with a known account are filtered strictly; pies without an account
    (synced before migration 011) are shown in all views until re-synced.
    """
    if account in ("ISA", "Trading"):
        account_filter = "AND (pie.account = :account OR pie.account IS NULL)"
        params: dict = {"user_id": user_id, "account": account}
    else:
        account_filter = ""
        params = {"user_id": user_id}

    rows = db.execute(text(f"""
        SELECT DISTINCT pie.id, pie.name
        FROM pies pie
        JOIN pie_holdings ph ON ph.pie_id = pie.id
        JOIN positions pos ON pos.ticker = ph.ticker AND pos.user_id = :user_id
        {account_filter}
        ORDER BY pie.name
    """), params).fetchall()
    return [{"id": r.id, "name": r.name} for r in rows]


def _pie_query(pie_ids: list[int], account: str) -> str:
    """Build pie-filtered SQL using position pricing for consistency with non-pie views.

    Pie quantities (owned_quantity) replace position quantities, but average_price,
    current_price, and currency come from the positions table so that FX handling
    in the template is identical to the standard view.
    """
    safe_ids = ", ".join(str(int(i)) for i in pie_ids)
    # Join positions on pie account when known, otherwise any account for the user
    pos_account_join = "AND (pa.pie_account IS NULL OR pos.account = pa.pie_account)"
    if account in ("ISA", "Trading"):
        pos_account_join += " AND pos.account = :account"
    account_col = (
        "MAX(pos.account)"
        if account in ("ISA", "Trading")
        else "CASE WHEN COUNT(DISTINCT pos.account) > 1 THEN 'Both' ELSE MAX(pos.account) END"
    )
    return f"""
        WITH pie_agg AS (
            SELECT
                ph.ticker,
                pie.account                  AS pie_account,
                SUM(ph.owned_quantity)       AS quantity
            FROM pie_holdings ph
            JOIN pies pie ON pie.id = ph.pie_id
            WHERE ph.pie_id IN ({safe_ids})
            GROUP BY ph.ticker, pie.account
        ),
        div_totals AS (
            -- Account-level dividends only; cannot be split per-pie (T212 API has no pie link)
            SELECT ticker, account, SUM(amount) AS amount
            FROM dividend_payments
            WHERE user_id = :user_id
            GROUP BY ticker, account
        )
        SELECT
            COALESCE(NULLIF(i.name, ''), i.short_name, pa.ticker)  AS name,
            COALESCE(NULLIF(i.short_name, ''), pa.ticker)           AS short_name,
            COALESCE(i.exchange, '—')                              AS exchange,
            COALESCE(i.currency_code, '—')                         AS currency,
            COALESCE(i.instrument_type, '—')                       AS instrument_type,
            i.sector,
            i.industry,
            i.instrument_class,
            {account_col}                                          AS account,
            SUM(pa.quantity)                                       AS quantity,
            SUM(pa.quantity * pos.average_price)
                / NULLIF(SUM(pa.quantity), 0)                      AS average_price,
            MAX(pos.current_price)                                 AS current_price,
            ROUND(SUM(pa.quantity * pos.average_price)::numeric, 2) AS cost,
            ROUND(SUM(pa.quantity * pos.current_price)::numeric, 2) AS value,
            ROUND(SUM(pos.ppl * pa.quantity
                      / NULLIF(pos.quantity, 0))::numeric, 2)      AS ppl,
            ROUND(((SUM(pa.quantity * pos.current_price)
                    - SUM(pa.quantity * pos.average_price))
                   / NULLIF(SUM(pa.quantity * pos.average_price), 0))::numeric, 4)
                                                                   AS result_coef,
            -- Proportional estimate: scales account dividends by pie's share of position qty
            ROUND(COALESCE(SUM(
                COALESCE(dt.amount, 0) * pa.quantity / NULLIF(pos.quantity, 0)
            ), 0)::numeric, 2)                                     AS total_dividends,
            ROUND(
                COALESCE((SELECT MAX(df.annual_rate) FROM dividend_forecast df
                          WHERE df.ticker = pa.ticker), 0)::numeric
                * SUM(pa.quantity)::numeric, 2)                    AS forward_dividends,
            COALESCE((
                SELECT MAX(df.annual_rate) FROM dividend_forecast df
                WHERE df.ticker = pa.ticker
            ), 0)                                                   AS annual_rate_per_share,
            i.fcf_per_share_3y_avg,
            i.eps_ttm
        FROM pie_agg pa
        JOIN instruments i ON pa.ticker = i.ticker
        JOIN positions pos ON pos.ticker = pa.ticker AND pos.user_id = :user_id
            {pos_account_join}
        LEFT JOIN div_totals dt ON dt.ticker = pa.ticker AND dt.account = pos.account
        GROUP BY pa.ticker,
                 i.name, i.short_name, i.exchange, i.currency_code, i.instrument_type,
                 i.sector, i.industry, i.instrument_class, i.fcf_per_share_3y_avg, i.eps_ttm
        ORDER BY SUM(pa.quantity * pos.current_price) DESC NULLS LAST
    """


@app.get("/", response_class=HTMLResponse)
def index(request: Request, account: str = "combined", pies: str = "",
          db: Session = Depends(get_session)):
    user = _get_current_user(request, db)
    if not user:
        return _redirect("/login")

    if account not in ("ISA", "Trading"):
        account = "combined"

    available_pies = _load_pies(db, user.id, account)
    all_pie_ids = {p["id"] for p in available_pies}

    # Resolve selected pie IDs
    if pies == "all":
        selected_pie_ids = sorted(all_pie_ids)
    elif pies:
        selected_pie_ids = [int(p) for p in pies.split(",") if p.strip().isdigit()
                            and int(p) in all_pie_ids]
    else:
        selected_pie_ids = []

    if selected_pie_ids:
        sql_str = _pie_query(selected_pie_ids, account)
        params: dict = {"user_id": user.id}
        if account in ("ISA", "Trading"):
            params["account"] = account
        rows = db.execute(text(sql_str), params).fetchall()
    elif account in ("ISA", "Trading"):
        rows = db.execute(_ACCOUNT_SQL, {"user_id": user.id, "account": account}).fetchall()
    else:
        rows = db.execute(_COMBINED_SQL, {"user_id": user.id}).fetchall()

    positions = [dict(r._mapping) for r in rows]

    for p in positions:
        p["display_class"] = _classify(
            p["instrument_type"], p["sector"], p["industry"],
            p["name"], p["instrument_class"],
        )

    for p in positions:
        if p["currency"] == "GBX":
            for col in ("average_price", "current_price", "cost", "value"):
                if p[col] is not None:
                    p[col] = round(float(p[col]) / 100, 2)
            p["currency"] = "GBP"

    totals: dict[str, dict] = {}
    for p in positions:
        ccy = p["currency"]
        if ccy not in totals:
            totals[ccy] = {"cost": 0.0, "value": 0.0, "ppl": 0.0, "dividends": 0.0, "fwd_dividends": 0.0}
        totals[ccy]["cost"] += float(p["cost"] or 0)
        totals[ccy]["value"] += float(p["value"] or 0)
        totals[ccy]["ppl"] += float(p["ppl"] or 0)
        totals[ccy]["dividends"] += float(p["total_dividends"] or 0)
        totals[ccy]["fwd_dividends"] += float(p["forward_dividends"] or 0)

    fx = _get_fx_rates_to_gbp(set(totals.keys()))
    grand_total = {"cost": 0.0, "value": 0.0, "ppl": 0.0, "dividends": 0.0, "fwd_dividends": 0.0}
    fx_rates_used: dict[str, float] = {}
    for ccy, t in totals.items():
        rate = fx.get(ccy, 1.0)
        if ccy != "GBP":
            fx_rates_used[ccy] = rate
        grand_total["cost"] += t["cost"] * rate
        grand_total["value"] += t["value"] * rate
        grand_total["ppl"] += t["ppl"] * rate
        grand_total["dividends"] += t["dividends"] * rate
        grand_total["fwd_dividends"] += t["fwd_dividends"] * rate

    last_synced = db.execute(text(
        "SELECT MAX(last_synced_at) FROM positions WHERE user_id = :uid"
    ), {"uid": user.id}).scalar()

    return templates.TemplateResponse(request, "index.html", {
        "user": user,
        "positions": positions,
        "totals": totals,
        "grand_total": grand_total,
        "fx_rates_used": fx_rates_used,
        "last_synced": last_synced,
        "account_filter": account,
        "available_pies": available_pies,
        "selected_pie_ids": {str(i) for i in selected_pie_ids},
        "pies_param": pies,
        "fmt": _fmt,
        "cfmt": _cfmt,
    })
