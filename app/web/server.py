import datetime
import hashlib
import json
import time
import traceback
import urllib.request
from urllib.parse import quote, urlencode
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import yfinance as yf
from fastapi import FastAPI, Depends, Request, Form, Body
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.database import get_session, SessionLocal
from app.auth.models import User, UserSettings
from app.auth.crypto import encrypt, decrypt
from app.auth.oauth import oauth
from app.models.ai_portfolio_analysis_cache import AIPortfolioAnalysisCache

app = FastAPI(title="Trading 212 Dashboard")
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "static")),
    name="static",
)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    same_site="lax",       # blocks cross-site POST — effective CSRF mitigation
    https_only=settings.https_only,  # set HTTPS_ONLY=true in production
    session_cookie="session",
)


@app.on_event("startup")
def _on_startup():
    """Reset stuck syncs from a previous crash, then start the background scheduler."""
    # ── Reset any syncs left in 'running' state by a previous server crash ──
    db = SessionLocal()
    try:
        stuck = db.query(UserSettings).filter_by(sync_status="running").all()
        for s in stuck:
            s.sync_status = "idle"
            s.sync_message = "Sync was interrupted by a server restart."
        if stuck:
            db.commit()
            print(f"  Reset {len(stuck)} stuck sync(s) to idle on startup.")
    finally:
        db.close()

    # ── Start the nightly scheduler (03:00 UTC daily) ───────────────────────
    _scheduler.add_job(
        _scheduled_sync_all_users,
        CronTrigger(hour=3, minute=0, timezone="UTC"),
        id="nightly_sync",
        replace_existing=True,
    )
    # Progressive catalogue enrichment runs at 04:00 UTC — after user syncs
    # have completed. Enriches up to 300 stale instruments per night.
    _scheduler.add_job(
        _scheduled_enrich_catalogue,
        CronTrigger(hour=4, minute=0, timezone="UTC"),
        id="nightly_enrich",
        replace_existing=True,
    )
    _scheduler.start()
    print("  Scheduler started — nightly sync 03:00 UTC, catalogue enrich 04:00 UTC.")


@app.on_event("shutdown")
def _on_shutdown():
    """Cleanly stop the background scheduler on server exit."""
    _scheduler.shutdown(wait=False)
    print("  Scheduler stopped.")

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["urlencode_val"] = lambda v: quote(str(v or ""), safe="")
templates.env.globals["now"] = datetime.datetime.utcnow

_sync_executor = ThreadPoolExecutor(max_workers=4)  # used directly for sync tasks
_scheduler = BackgroundScheduler(timezone="UTC")

# In-memory state for admin background jobs (single global each — only one of each runs at a time)
_enrich_state:    dict = {"status": "idle", "done": 0, "total": 0, "current": "", "result": None}
_t212_state:      dict = {"status": "idle", "done": 0, "total": 0, "current": "", "result": None}
_catalogue_state: dict = {"status": "idle", "done": 0, "total": 0, "current": "", "result": None}
_figi_state:      dict = {"status": "idle", "done": 0, "total": 0, "current": "", "result": None}
_yf_fix_state:    dict = {"status": "idle", "done": 0, "total": 0, "current": "", "result": None}


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

    # Filter out NULL placeholders and known non-currency values before FX lookup
    needed = {c for c in currencies if c and c not in {"GBP", "GBX", "—", "-"}}
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


def _resolve_display_currency(user_settings: UserSettings | None, account: str) -> str:
    if not user_settings:
        return "GBP"
    if account == "ISA":
        return user_settings.isa_currency_code or user_settings.trading_currency_code or "GBP"
    if account == "Trading":
        return user_settings.trading_currency_code or user_settings.isa_currency_code or "GBP"
    return user_settings.trading_currency_code or user_settings.isa_currency_code or "GBP"


def _convert_gbp_to_currency(value: float | int | None, currency: str, fx_to_gbp: dict[str, float] | None = None) -> float:
    if value is None:
        return 0.0
    if currency in (None, "", "GBP"):
        return float(value)
    if currency == "GBX":
        return float(value) * 100
    rates = fx_to_gbp or _get_fx_rates_to_gbp({currency})
    rate = float(rates.get(currency, 1.0) or 1.0)
    return float(value) / rate if rate else float(value)


def _native_to_display_rate(native_currency: str, display_currency: str, fx_to_gbp: dict[str, float]) -> float:
    native = native_currency or "GBP"
    display = display_currency or "GBP"
    if native == display:
        return 1.0
    native_to_gbp = float(fx_to_gbp.get(native, 1.0) or 1.0)
    if display == "GBP":
        return native_to_gbp
    if display == "GBX":
        return native_to_gbp * 100
    display_to_gbp = float(fx_to_gbp.get(display, 1.0) or 1.0)
    if not display_to_gbp:
        return native_to_gbp
    return native_to_gbp / display_to_gbp


def _convert_amount_between_currencies(
    value: float | int | None,
    source_currency: str | None,
    target_currency: str,
    fx_to_gbp: dict[str, float],
) -> float:
    if value is None:
        return 0.0
    source = source_currency or "GBP"
    if source == target_currency:
        return float(value)
    value_gbp = float(value) * float(fx_to_gbp.get(source, 1.0) or 1.0)
    return _convert_gbp_to_currency(value_gbp, target_currency, fx_to_gbp)


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


def _is_admin(user: User) -> bool:
    """Return True if the user's email is in the ADMIN_EMAILS config list."""
    return user.email.lower() in settings.admin_email_set


def _require_admin(request: Request, db: Session) -> User | RedirectResponse:
    """Require an authenticated admin user, redirecting to / if not authorised."""
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not _is_admin(user):
        return _redirect("/")
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

def _run_sync(user_id: int, full_catalogue: bool = False) -> None:
    """Synchronous sync task — runs in thread pool.

    full_catalogue=True is passed by the nightly scheduler for the first user
    so that the complete T212 instrument catalogue is loaded into the DB once
    per day, keeping the instruments research page up to date.
    """
    db = SessionLocal()
    try:
        from app.sync.runner import SyncRunner
        from app.sync.dividend_sync import DividendSync
        from app.models import Position, PieHolding

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
            runner.sync_all(full_catalogue=full_catalogue)

        held_tickers = {
            r[0] for r in db.query(Position.ticker).filter(Position.user_id == user_id).all()
        } | {
            r[0] for r in db.query(PieHolding.ticker).filter(PieHolding.user_id == user_id).all()
        }
        if held_tickers:
            user_settings.sync_message = "Refreshing dividend history and forecasts…"
            db.commit()
            div_sync = DividendSync(
                session=db,
                fmp_api_key=settings.fmp_api_key,
            )
            div_sync.sync_all(tickers=sorted(held_tickers))

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
                # Use type + short message only — full traceback goes to server logs, not the UI
                user_settings.sync_message = f"{type(exc).__name__}: {exc.args[0] if exc.args else 'unknown error'}"
                db.commit()
        except Exception:
            pass
        traceback.print_exc()
    finally:
        db.close()


# ── Nightly scheduled sync ────────────────────────────────────────────────

def _scheduled_sync_all_users() -> None:
    """Run at 03:00 UTC daily — sync every user who has API keys and hasn't opted out.

    The first user to sync also triggers a full T212 catalogue load so the
    instruments research page stays populated with all available instruments.
    """
    now_str = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    print(f"[scheduler] Nightly sync triggered at {now_str} UTC")
    db = SessionLocal()
    try:
        all_settings = (
            db.query(UserSettings)
            .filter(
                UserSettings.auto_sync_enabled.is_(True),
                UserSettings.sync_status != "running",
            )
            .all()
        )
        queued = 0
        for us in all_settings:
            has_key = bool(us.t212_api_key_enc or us.t212_isa_api_key_enc)
            if not has_key:
                continue
            us.sync_status = "running"
            us.sync_message = "Scheduled nightly sync…"
            # First user's sync loads the full catalogue; others do the normal held+stubs
            _sync_executor.submit(_run_sync, us.user_id, queued == 0)
            queued += 1
        db.commit()
        print(f"[scheduler] Queued nightly sync for {queued} user(s).")
    except Exception:
        traceback.print_exc()
    finally:
        db.close()


def _scheduled_enrich_catalogue() -> None:
    """Run at 04:00 UTC daily — progressively enrich stale instruments in batches."""
    print(f"[scheduler] Catalogue enrichment triggered at "
          f"{datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC")
    db = SessionLocal()
    try:
        from app.sync.runner import SyncRunner
        runner = SyncRunner(db, user_id=None)
        # yfinance enrichment (sector, industry, description, country)
        count = runner.enrich_stale_instruments_batch(batch_size=300)
        print(f"[scheduler] yfinance enrichment complete — {count} instruments updated.")
        # OpenFIGI enrichment (FIGI, MIC, security type) — fast batch operation
        figi_result = runner.enrich_instruments_openfigi(batch_size=1000)
        print(f"[scheduler] OpenFIGI enrichment complete — {figi_result}.")
    except Exception:
        traceback.print_exc()
    finally:
        db.close()


# ── Public well-known files ────────────────────────────────────────────────

@app.get("/ads.txt", response_class=PlainTextResponse)
def ads_txt():
    """Serve ads.txt from the root for AdSense verification."""
    ads_file = Path(__file__).parent / "static" / "ads.txt"
    return ads_file.read_text()


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
        # Do NOT merge across providers by email — email ownership can transfer.
        # Each (provider, provider_id) pair is a distinct identity.
        pass
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


# ── Admin routes ──────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, db: Session = Depends(get_session)):
    user = _require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user

    # Gather all users with their settings and basic stats
    rows = db.execute(text("""
        SELECT
            u.id,
            u.email,
            u.name,
            u.provider,
            u.created_at,
            u.last_login_at,
            s.sync_status,
            s.last_sync_at,
            s.auto_sync_enabled,
            (s.t212_api_key_enc IS NOT NULL AND s.t212_api_key_enc != '') AS has_trading_key,
            (s.t212_isa_api_key_enc IS NOT NULL AND s.t212_isa_api_key_enc != '') AS has_isa_key,
            (SELECT COUNT(*) FROM positions p WHERE p.user_id = u.id) AS position_count,
            (SELECT COUNT(*) FROM orders o WHERE o.user_id = u.id) AS order_count,
            (SELECT COUNT(*) FROM dividend_payments dp WHERE dp.user_id = u.id) AS dividend_count
        FROM users u
        LEFT JOIN user_settings s ON s.user_id = u.id
        ORDER BY u.created_at DESC
    """)).mappings().all()

    return templates.TemplateResponse(request, "admin.html", {
        "user": user,
        "users": rows,
        "admin_emails": sorted(settings.admin_email_set),
    })


@app.get("/admin/instruments", response_class=HTMLResponse)
def admin_instruments_page(request: Request, db: Session = Depends(get_session)):
    user = _require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user

    # Compute summary stats via SQL aggregation — never load all rows into Python
    stats = db.execute(text("""
        SELECT
            COUNT(*)                                                               AS total,
            COUNT(*) FILTER (WHERE yf_ticker IS NULL)                             AS missing_yf,
            COUNT(*) FILTER (WHERE sector IS NULL)                                AS missing_sector,
            COUNT(*) FILTER (WHERE country IS NULL)                               AS missing_country,
            COUNT(*) FILTER (WHERE currency_code IS NULL)                         AS missing_currency,
            COUNT(*) FILTER (WHERE last_enriched_at IS NULL)                      AS never_enriched,
            COUNT(*) FILTER (WHERE last_enriched_at IS NOT NULL
                                AND yf_ticker IS NOT NULL
                                AND sector IS NOT NULL
                                AND country IS NOT NULL)                           AS fully_enriched
        FROM instruments
    """)).mappings().one()

    return templates.TemplateResponse(request, "admin_instruments.html", {
        "user": user,
        "total":          stats["total"],
        "missing_yf":     stats["missing_yf"],
        "missing_sector": stats["missing_sector"],
        "missing_country":  stats["missing_country"],
        "missing_currency": stats["missing_currency"],
        "never_enriched":   stats["never_enriched"],
        "fully_enriched":   stats["fully_enriched"],
    })


_ADMIN_INST_SORT_COLS = {
    "ticker":          "i.ticker",
    "short_name":      "i.short_name",
    "name":            "i.name",
    "instrument_type": "i.instrument_type",
    "exchange":        "i.exchange",
    "currency_code":   "i.currency_code",
    "yf_ticker":       "i.yf_ticker",
    "sector":          "i.sector",
    "country":         "i.country",
    "market_cap":      "i.market_cap",
    "last_enriched_at":"i.last_enriched_at",
    "updated_at":      "i.updated_at",
    "holder_count":    "holder_count",
}


@app.get("/admin/api/instruments")
def admin_api_instruments(
    request: Request,
    db: Session = Depends(get_session),
    search: str = "",
    filter: str = "all",
    sort: str = "name",
    order: str = "asc",
    offset: int = 0,
    limit: int = 200,
):
    user = _require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return JSONResponse({"error": "Unauthorised"}, status_code=401)

    search = search.strip()
    offset = max(0, offset)
    limit = min(max(1, limit), 200)
    sort_col  = _ADMIN_INST_SORT_COLS.get(sort, "i.name")
    order_dir = "DESC" if order.lower() == "desc" else "ASC"

    where_parts  = []
    having_parts = []
    params: dict = {}

    if search:
        where_parts.append(
            "(i.name ILIKE :search OR i.short_name ILIKE :search "
            "OR i.ticker ILIKE :search OR i.isin ILIKE :search)"
        )
        params["search"] = f"%{search}%"

    filter_where = {
        "no-yf":          "i.yf_ticker IS NULL",
        "no-sector":      "i.sector IS NULL",
        "no-country":     "i.country IS NULL",
        "no-currency":    "i.currency_code IS NULL",
        "never-enriched": "i.last_enriched_at IS NULL",
    }
    if filter in filter_where:
        where_parts.append(filter_where[filter])
    elif filter == "held":
        having_parts.append("COUNT(DISTINCT p.user_id) > 0")

    where_sql  = ("WHERE "  + " AND ".join(where_parts))  if where_parts  else ""
    having_sql = ("HAVING " + " AND ".join(having_parts)) if having_parts else ""

    base_cte = f"""
        SELECT
            i.ticker, i.short_name, i.name, i.currency_code, i.isin,
            i.instrument_type, i.exchange, i.yf_ticker, i.sector, i.industry,
            i.country, i.market_cap, i.last_enriched_at, i.updated_at,
            COUNT(DISTINCT p.user_id) AS holder_count
        FROM instruments i
        LEFT JOIN positions p ON p.ticker = i.ticker
        {where_sql}
        GROUP BY i.ticker
        {having_sql}
    """

    total = db.execute(
        text(f"SELECT COUNT(*) FROM ({base_cte}) _sub"), params
    ).scalar() or 0

    rows = db.execute(
        text(f"""
            {base_cte}
            ORDER BY {sort_col} {order_dir} NULLS LAST
            LIMIT :limit OFFSET :offset
        """),
        {**params, "limit": limit, "offset": offset},
    ).mappings().all()

    items = []
    for r in rows:
        items.append({
            "ticker":          r["ticker"],
            "short_name":      r["short_name"],
            "name":            r["name"],
            "currency_code":   r["currency_code"],
            "isin":            r["isin"],
            "instrument_type": r["instrument_type"],
            "exchange":        r["exchange"],
            "yf_ticker":       r["yf_ticker"],
            "sector":          r["sector"],
            "industry":        r["industry"],
            "country":         r["country"],
            "market_cap":      float(r["market_cap"]) if r["market_cap"] else None,
            "last_enriched_at": r["last_enriched_at"].strftime("%d %b %Y") if r["last_enriched_at"] else None,
            "updated_at":      r["updated_at"].strftime("%d %b %Y") if r["updated_at"] else None,
            "holder_count":    r["holder_count"],
        })

    return JSONResponse({"items": items, "total": total, "offset": offset, "limit": limit})


def _run_force_enrich() -> None:
    """Background task: enrich all instruments, updating _enrich_state as it goes."""
    global _enrich_state
    db = SessionLocal()
    try:
        from app.sync.runner import SyncRunner
        from app.sync.dividend_sync import DividendSync
        from app.models import Instrument
        runner = SyncRunner(db, user_id=None)

        def _progress(done, total, ticker):
            _enrich_state.update({"done": done, "total": total, "current": ticker})

        _enrich_state.update({"status": "running", "done": 0, "total": 0, "current": "", "result": None})
        result = runner.enrich_all_instruments(progress_callback=_progress)
        all_tickers = [r[0] for r in db.query(Instrument.ticker).order_by(Instrument.ticker).all()]
        dividend_result = {"requested": len(all_tickers)}
        if all_tickers:
            _enrich_state.update({
                "current": "Refreshing dividend history and forecasts…",
                "done": len(all_tickers),
                "total": len(all_tickers),
            })
            div_sync = DividendSync(
                session=db,
                fmp_api_key=settings.fmp_api_key,
            )
            div_sync.sync_all(tickers=all_tickers)
        result["dividends"] = dividend_result
        _enrich_state.update({"status": "done", "result": result})
    except Exception as exc:
        traceback.print_exc()
        _enrich_state.update({"status": "error", "result": {"error": str(exc)}})
    finally:
        db.close()


@app.post("/admin/instruments/enrich")
def trigger_force_enrich(request: Request, db: Session = Depends(get_session)):
    user = _require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if _enrich_state["status"] == "running":
        return JSONResponse({"error": "Enrichment already running"}, status_code=409)
    _sync_executor.submit(_run_force_enrich)
    return JSONResponse({"queued": True})


@app.get("/admin/instruments/enrich/status")
def force_enrich_status(request: Request, db: Session = Depends(get_session)):
    user = _require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return JSONResponse(_enrich_state)


def _run_t212_reload(api_key: str, api_secret: str) -> None:
    """Background task: reload T212 base data for all DB instruments."""
    global _t212_state
    db = SessionLocal()
    try:
        from app.sync.runner import SyncRunner
        runner = SyncRunner(db, api_key=api_key, api_secret=api_secret, user_id=None)

        def _progress(done, total, ticker):
            _t212_state.update({"done": done, "total": total, "current": ticker})

        _t212_state.update({"status": "running", "done": 0, "total": 0, "current": "", "result": None})
        result = runner.reload_all_instruments_from_t212(progress_callback=_progress)
        _t212_state.update({"status": "done", "result": result})
    except Exception as exc:
        traceback.print_exc()
        _t212_state.update({"status": "error", "result": {"error": str(exc)}})
    finally:
        db.close()


@app.post("/admin/instruments/t212-reload")
def trigger_t212_reload(request: Request, db: Session = Depends(get_session)):
    user = _require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if _t212_state["status"] == "running":
        return JSONResponse({"error": "T212 reload already running"}, status_code=409)

    # Use the requesting admin's own T212 credentials to fetch the catalogue
    user_settings = db.query(UserSettings).filter_by(user_id=user.id).first()
    api_key    = decrypt(user_settings.t212_api_key_enc)    if user_settings and user_settings.t212_api_key_enc    else None
    api_secret = decrypt(user_settings.t212_api_secret_enc) if user_settings and user_settings.t212_api_secret_enc else None
    if not api_key:
        api_key    = decrypt(user_settings.t212_isa_api_key_enc)    if user_settings and user_settings.t212_isa_api_key_enc    else None
        api_secret = decrypt(user_settings.t212_isa_api_secret_enc) if user_settings and user_settings.t212_isa_api_secret_enc else None
    if not api_key:
        return JSONResponse({"error": "No T212 API credentials found for your account"}, status_code=400)

    _sync_executor.submit(_run_t212_reload, api_key, api_secret)
    return JSONResponse({"queued": True})


@app.get("/admin/instruments/t212-reload/status")
def t212_reload_status(request: Request, db: Session = Depends(get_session)):
    user = _require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return JSONResponse(_t212_state)


def _run_openfigi_enrich() -> None:
    """Background task: enrich all instruments with OpenFIGI data."""
    global _figi_state
    db = SessionLocal()
    try:
        from app.sync.runner import SyncRunner
        _figi_state.update({"status": "running", "done": 0, "total": 0,
                             "current": "Querying OpenFIGI…", "result": None})
        runner = SyncRunner(db, user_id=None)
        # Process in multiple passes until all instruments are covered
        total_enriched = total_no_match = 0
        while True:
            result = runner.enrich_instruments_openfigi(batch_size=500)
            total_enriched  += result["enriched"]
            total_no_match  += result["no_match"]
            _figi_state.update({
                "done":    total_enriched,
                "current": f"Enriched {total_enriched} so far…",
            })
            if result["total"] < 500:
                break  # last batch — nothing left to process
        _figi_state.update({
            "status": "done",
            "result": {"enriched": total_enriched, "no_match": total_no_match},
        })
    except Exception as exc:
        traceback.print_exc()
        _figi_state.update({"status": "error", "result": {"error": str(exc)}})
    finally:
        db.close()


@app.post("/admin/instruments/openfigi-enrich")
def trigger_openfigi_enrich(request: Request, db: Session = Depends(get_session)):
    user = _require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if _figi_state["status"] == "running":
        return JSONResponse({"error": "OpenFIGI enrichment already running"}, status_code=409)
    _sync_executor.submit(_run_openfigi_enrich)
    return JSONResponse({"queued": True})


@app.get("/admin/instruments/openfigi-enrich/status")
def openfigi_enrich_status(request: Request, db: Session = Depends(get_session)):
    user = _require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return JSONResponse(_figi_state)


def _run_yf_ticker_fix() -> None:
    """Background task: bulk-fix persisted Yahoo Finance tickers."""
    global _yf_fix_state
    db = SessionLocal()
    try:
        from app.sync.runner import SyncRunner

        runner = SyncRunner(db, user_id=None)

        def _progress(done, total, ticker):
            _yf_fix_state.update({"done": done, "total": total, "current": ticker})

        _yf_fix_state.update({"status": "running", "done": 0, "total": 0, "current": "", "result": None})
        result = runner.revalidate_yf_tickers(progress_callback=_progress)
        _yf_fix_state.update({"status": "done", "result": result})
    except Exception as exc:
        traceback.print_exc()
        _yf_fix_state.update({"status": "error", "result": {"error": str(exc)}})
    finally:
        db.close()


@app.post("/admin/instruments/fix-yf-tickers")
def trigger_yf_ticker_fix(request: Request, db: Session = Depends(get_session)):
    user = _require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if _yf_fix_state["status"] == "running":
        return JSONResponse({"error": "Yahoo ticker fix already running"}, status_code=409)
    _sync_executor.submit(_run_yf_ticker_fix)
    return JSONResponse({"queued": True})


@app.get("/admin/instruments/fix-yf-tickers/status")
def yf_ticker_fix_status(request: Request, db: Session = Depends(get_session)):
    user = _require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return JSONResponse(_yf_fix_state)


def _run_full_catalogue_load(api_key: str, api_secret: str) -> None:
    """Background task: load the complete T212 instrument catalogue into the DB.

    Uses sync_instruments(full_catalogue=True) which upserts every instrument
    in the T212 catalogue. The ON CONFLICT clause only updates T212 base fields;
    all enrichment data (sector, country, description, yf_ticker, etc.) is
    preserved unchanged on rows that already exist.
    """
    global _catalogue_state
    db = SessionLocal()
    try:
        from app.sync.runner import SyncRunner

        _catalogue_state.update({"status": "running", "done": 0, "total": 0,
                                  "current": "Fetching T212 catalogue…", "result": None})

        # We need a user_id for the held-ticker query inside sync_instruments;
        # use the first user that has this API key configured
        user_settings = db.query(UserSettings).filter(
            UserSettings.t212_api_key_enc.isnot(None)
        ).first()
        if not user_settings:
            user_settings = db.query(UserSettings).filter(
                UserSettings.t212_isa_api_key_enc.isnot(None)
            ).first()
        user_id = user_settings.user_id if user_settings else None

        runner = SyncRunner(db, api_key=api_key, api_secret=api_secret, user_id=user_id)
        runner.sync_instruments(full_catalogue=True)

        from app.models import Instrument
        total = db.query(Instrument).count()
        _catalogue_state.update({
            "status": "done",
            "result": {"total_in_db": total},
        })
    except Exception as exc:
        traceback.print_exc()
        _catalogue_state.update({"status": "error", "result": {"error": str(exc)}})
    finally:
        db.close()


@app.post("/admin/instruments/load-catalogue")
def trigger_full_catalogue_load(request: Request, db: Session = Depends(get_session)):
    user = _require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if _catalogue_state["status"] == "running":
        return JSONResponse({"error": "Catalogue load already running"}, status_code=409)

    user_settings = db.query(UserSettings).filter_by(user_id=user.id).first()
    api_key    = decrypt(user_settings.t212_api_key_enc)    if user_settings and user_settings.t212_api_key_enc    else None
    api_secret = decrypt(user_settings.t212_api_secret_enc) if user_settings and user_settings.t212_api_secret_enc else None
    if not api_key:
        api_key    = decrypt(user_settings.t212_isa_api_key_enc)    if user_settings and user_settings.t212_isa_api_key_enc    else None
        api_secret = decrypt(user_settings.t212_isa_api_secret_enc) if user_settings and user_settings.t212_isa_api_secret_enc else None
    if not api_key:
        return JSONResponse({"error": "No T212 API credentials found for your account"}, status_code=400)

    _sync_executor.submit(_run_full_catalogue_load, api_key, api_secret)
    return JSONResponse({"queued": True})


@app.get("/admin/instruments/load-catalogue/status")
def full_catalogue_load_status(request: Request, db: Session = Depends(get_session)):
    user = _require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return JSONResponse(_catalogue_state)


# ── Help route ────────────────────────────────────────────────────────────

@app.get("/help", response_class=HTMLResponse)
def help_page(request: Request, db: Session = Depends(get_session)):
    user = _get_current_user(request, db)  # public — no auth required
    return templates.TemplateResponse(request, "help.html", {"user": user})


# ── Settings routes ────────────────────────────────────────────────────────

_server_ip_cache: dict = {"ip": None, "fetched_at": 0}

def _get_server_ip() -> str | None:
    """Return the server's public IP, cached for 6 hours."""
    if time.time() - _server_ip_cache["fetched_at"] < 21600 and _server_ip_cache["ip"]:
        return _server_ip_cache["ip"]
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=5) as resp:
            ip = resp.read().decode().strip()
        _server_ip_cache["ip"] = ip
        _server_ip_cache["fetched_at"] = time.time()
        return ip
    except Exception:
        return _server_ip_cache.get("ip")


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
        "auto_sync_enabled": user_settings.auto_sync_enabled if user_settings.auto_sync_enabled is not None else True,
        "is_admin": _is_admin(user),
        "server_ip": _get_server_ip(),
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
    if key_type not in ("trading", "isa"):
        from fastapi.responses import Response
        return Response(status_code=400)
    user_settings = _get_or_create_settings(db, user.id)
    if key_type == "trading":
        user_settings.t212_api_key_enc = None
        user_settings.t212_api_secret_enc = None
    else:
        user_settings.t212_isa_api_key_enc = None
        user_settings.t212_isa_api_secret_enc = None
    db.commit()
    return _redirect("/settings", status_code=303)


@app.post("/settings/delete-account")
async def delete_account(request: Request, db: Session = Depends(get_session)):
    """Permanently delete all user data and the account itself, then force logout."""
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    uid = user.id

    # Delete all portfolio data
    db.execute(text("DELETE FROM positions         WHERE user_id = :uid"), {"uid": uid})
    db.execute(text("DELETE FROM orders            WHERE user_id = :uid"), {"uid": uid})
    db.execute(text("DELETE FROM dividend_payments WHERE user_id = :uid"), {"uid": uid})
    db.execute(text("DELETE FROM transactions      WHERE user_id = :uid"), {"uid": uid})
    db.execute(text("DELETE FROM pies              WHERE user_id = :uid"), {"uid": uid})
    # Delete user settings (API keys, sync state) and the user record itself
    db.execute(text("DELETE FROM user_settings     WHERE user_id = :uid"), {"uid": uid})
    db.execute(text("DELETE FROM users             WHERE id       = :uid"), {"uid": uid})
    db.commit()

    # Clear the session so the user is fully logged out
    request.session.clear()
    return _redirect("/login", status_code=303)


@app.post("/settings/clear-data")
async def clear_data(request: Request, db: Session = Depends(get_session)):
    """Delete all synced portfolio data for the current user and reset sync state."""
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    uid = user.id
    db.execute(text("DELETE FROM positions         WHERE user_id = :uid"), {"uid": uid})
    db.execute(text("DELETE FROM orders            WHERE user_id = :uid"), {"uid": uid})
    db.execute(text("DELETE FROM dividend_payments WHERE user_id = :uid"), {"uid": uid})
    db.execute(text("DELETE FROM transactions      WHERE user_id = :uid"), {"uid": uid})
    # Deleting pies cascades to pie_holdings via the FK ondelete="CASCADE"
    db.execute(text("DELETE FROM pies              WHERE user_id = :uid"), {"uid": uid})
    # Reset sync state
    user_settings = _get_or_create_settings(db, uid)
    user_settings.last_sync_at = None
    user_settings.sync_status  = "idle"
    user_settings.sync_message = "Data cleared."
    db.commit()
    return _redirect("/settings", status_code=303)


# ── Sync routes ────────────────────────────────────────────────────────────

@app.post("/settings/auto-sync")
async def save_auto_sync(
    request: Request,
    db: Session = Depends(get_session),
    auto_sync_enabled: str = Form(default="off"),
):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    user_settings = _get_or_create_settings(db, user.id)
    user_settings.auto_sync_enabled = (auto_sync_enabled == "on")
    db.commit()
    return _redirect("/settings")


@app.post("/sync")
async def trigger_sync(
    request: Request,
    db: Session = Depends(get_session),
):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    # Atomically claim the "running" slot — only proceeds if current status is not "running"
    result = db.execute(
        text("""
            UPDATE user_settings
            SET sync_status = 'running', sync_message = 'Queued…'
            WHERE user_id = :uid AND sync_status != 'running'
        """),
        {"uid": user.id},
    )
    db.commit()

    if result.rowcount == 0:
        # Another sync is already running for this user — ignore the duplicate request
        return _redirect("/settings", status_code=303)

    # Submit to dedicated thread pool — independent of the request lifecycle
    _sync_executor.submit(_run_sync, user.id)
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
        p.ticker                                               AS ticker,
        COALESCE(NULLIF(i.name, ''), i.short_name, p.ticker)  AS name,
        COALESCE(NULLIF(i.short_name, ''), p.ticker)           AS short_name,
        COALESCE(i.exchange, '—')                              AS exchange,
        COALESCE(i.currency_code, '—')                         AS currency,
        COALESCE(i.instrument_type, '—')                       AS instrument_type,
        i.sector,
        i.industry,
        i.instrument_class,
        i.country,
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
        i.eps_ttm,
        (SELECT COUNT(*)
         FROM pie_holdings ph WHERE ph.ticker = p.ticker
           AND ph.user_id = :user_id)                               AS pie_count
    FROM positions p
    JOIN instruments i ON p.ticker = i.ticker
    WHERE p.user_id = :user_id
    GROUP BY p.ticker, i.name, i.short_name, i.exchange, i.currency_code,
             i.instrument_type, i.sector, i.industry, i.instrument_class, i.country,
             i.fcf_per_share_3y_avg, i.eps_ttm
    ORDER BY SUM(p.quantity * COALESCE(p.current_price, p.average_price)) DESC NULLS LAST
""")

_ACCOUNT_SQL = text("""
    SELECT
        p.ticker                                               AS ticker,
        COALESCE(NULLIF(i.name, ''), i.short_name, p.ticker)  AS name,
        COALESCE(NULLIF(i.short_name, ''), p.ticker)           AS short_name,
        COALESCE(i.exchange, '—')                              AS exchange,
        COALESCE(i.currency_code, '—')                         AS currency,
        COALESCE(i.instrument_type, '—')                       AS instrument_type,
        i.sector,
        i.industry,
        i.instrument_class,
        i.country,
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
        i.eps_ttm,
        (SELECT COUNT(*)
         FROM pie_holdings ph
         JOIN pies pie ON pie.pk = ph.pie_id
         WHERE ph.ticker = p.ticker
           AND ph.user_id = :user_id
           AND (pie.account = :account OR pie.account IS NULL))     AS pie_count
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
        SELECT DISTINCT pie.pk AS id, pie.name
        FROM pies pie
        JOIN pie_holdings ph ON ph.pie_id = pie.pk
        JOIN positions pos ON pos.ticker = ph.ticker AND pos.user_id = :user_id
        WHERE pie.user_id = :user_id
        {account_filter}
        ORDER BY pie.name
    """), params).fetchall()
    return [{"id": r.id, "name": r.name} for r in rows]


def _resolve_selected_pie_ids(available_pies: list[dict], pies: str) -> list[int]:
    all_pie_ids = {p["id"] for p in available_pies}
    if pies == "all":
        return sorted(all_pie_ids)
    if not pies:
        return []
    return [
        int(p)
        for p in pies.split(",")
        if p.strip().isdigit() and int(p) in all_pie_ids
    ]


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
            JOIN pies pie ON pie.pk = ph.pie_id
            WHERE ph.pie_id IN ({safe_ids})
              AND ph.user_id = :user_id
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
            pa.ticker                                              AS ticker,
            COALESCE(NULLIF(i.name, ''), i.short_name, pa.ticker)  AS name,
            COALESCE(NULLIF(i.short_name, ''), pa.ticker)           AS short_name,
            COALESCE(i.exchange, '—')                              AS exchange,
            COALESCE(i.currency_code, '—')                         AS currency,
            COALESCE(i.instrument_type, '—')                       AS instrument_type,
            i.sector,
            i.industry,
            i.instrument_class,
            i.country,
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
            i.eps_ttm,
            (SELECT COUNT(*) FROM pie_holdings ph2
             WHERE ph2.ticker = pa.ticker
               AND ph2.user_id = :user_id)                          AS pie_count
        FROM pie_agg pa
        JOIN instruments i ON pa.ticker = i.ticker
        JOIN positions pos ON pos.ticker = pa.ticker AND pos.user_id = :user_id
            {pos_account_join}
        LEFT JOIN div_totals dt ON dt.ticker = pa.ticker AND dt.account = pos.account
        GROUP BY pa.ticker,
                 i.name, i.short_name, i.exchange, i.currency_code, i.instrument_type,
                 i.sector, i.industry, i.instrument_class, i.country, i.fcf_per_share_3y_avg, i.eps_ttm
        ORDER BY SUM(pa.quantity * pos.current_price) DESC NULLS LAST
    """


def _fetch_filtered_positions(
    db: Session,
    user_id: int,
    account: str,
    selected_pie_ids: list[int],
) -> list[dict]:
    params: dict = {"user_id": user_id}
    if selected_pie_ids:
        if account in ("ISA", "Trading"):
            params["account"] = account
        rows = db.execute(text(_pie_query(selected_pie_ids, account)), params).fetchall()
    elif account in ("ISA", "Trading"):
        params["account"] = account
        rows = db.execute(_ACCOUNT_SQL, params).fetchall()
    else:
        rows = db.execute(_COMBINED_SQL, params).fetchall()
    return [dict(r._mapping) for r in rows]


@app.get("/", response_class=HTMLResponse)
def index(request: Request, account: str = "combined", pies: str = "",
          country: str = "", sector: str = "",
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
            for col in ("average_price", "current_price", "cost", "value", "forward_dividends", "annual_rate_per_share"):
                if p[col] is not None:
                    p[col] = round(float(p[col]) / 100, 2)
            p["currency"] = "GBP"

    # Apply hidden country / sector filters (activated from the Analysis page)
    if country:
        if country == "Unknown":
            positions = [p for p in positions if not (p.get("country") or "").strip()]
        else:
            positions = [p for p in positions if (p.get("country") or "").strip() == country]

    if sector:
        def _eff_sector(p):
            s = (p.get("sector") or "").strip()
            return s if s else p["display_class"]
        positions = [p for p in positions if _eff_sector(p) == sector]

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

    from app.auth.models import UserSettings as _UserSettings
    user_settings = db.query(_UserSettings).filter_by(user_id=user.id).first()
    display_currency = _resolve_display_currency(user_settings, account)

    fx_needed = set(totals.keys()) | {display_currency}
    if user_settings:
        if user_settings.trading_currency_code:
            fx_needed.add(user_settings.trading_currency_code)
        if user_settings.isa_currency_code:
            fx_needed.add(user_settings.isa_currency_code)

    fx = _get_fx_rates_to_gbp(fx_needed)
    grand_total = {"cost": 0.0, "value": 0.0, "ppl": 0.0, "dividends": 0.0, "fwd_dividends": 0.0}
    for ccy, t in totals.items():
        rate = fx.get(ccy, 1.0)
        grand_total["cost"] += t["cost"] * rate
        grand_total["value"] += t["value"] * rate
        grand_total["ppl"] += t["ppl"] * rate
        grand_total["dividends"] += t["dividends"] * rate
        grand_total["fwd_dividends"] += t["fwd_dividends"] * rate

    last_synced = db.execute(text(
        "SELECT MAX(last_synced_at) FROM positions WHERE user_id = :uid"
    ), {"uid": user.id}).scalar()

    # Cash positions: free cash per account + uninvested cash sitting in pies.
    free_cash_trading = float(user_settings.free_cash_trading or 0) if user_settings else 0.0
    free_cash_isa = float(user_settings.free_cash_isa or 0) if user_settings else 0.0
    pie_cash_trading = float(user_settings.pie_cash_trading or 0) if user_settings else 0.0
    pie_cash_isa = float(user_settings.pie_cash_isa or 0) if user_settings else 0.0
    trading_currency = user_settings.trading_currency_code if user_settings else None
    isa_currency = user_settings.isa_currency_code if user_settings else None

    if account == "ISA":
        free_cash = _convert_amount_between_currencies(free_cash_isa, isa_currency, display_currency, fx)
        total_pie_cash = _convert_amount_between_currencies(pie_cash_isa, isa_currency, display_currency, fx)
    elif account == "Trading":
        free_cash = _convert_amount_between_currencies(free_cash_trading, trading_currency, display_currency, fx)
        total_pie_cash = _convert_amount_between_currencies(pie_cash_trading, trading_currency, display_currency, fx)
    else:
        free_cash = (
            _convert_amount_between_currencies(free_cash_trading, trading_currency, display_currency, fx)
            + _convert_amount_between_currencies(free_cash_isa, isa_currency, display_currency, fx)
        )
        total_pie_cash = (
            _convert_amount_between_currencies(pie_cash_trading, trading_currency, display_currency, fx)
            + _convert_amount_between_currencies(pie_cash_isa, isa_currency, display_currency, fx)
        )

    grand_total_display = {
        key: _convert_gbp_to_currency(value, display_currency, fx)
        for key, value in grand_total.items()
    }
    fx_rates_display: dict[str, float] = {}
    for ccy in totals.keys():
        if ccy != display_currency:
            fx_rates_display[ccy] = _native_to_display_rate(ccy, display_currency, fx)

    for p in positions:
        native_to_display = _native_to_display_rate(p["currency"], display_currency, fx)
        p["avg_price_display"] = float(p["average_price"] or 0) * native_to_display
        p["current_price_display"] = float(p["current_price"] or 0) * native_to_display
        p["cost_display"] = float(p["cost"] or 0) * native_to_display
        p["value_display"] = float(p["value"] or 0) * native_to_display
        p["ppl_display"] = float(p["ppl"] or 0) * native_to_display
        p["dividends_display"] = _convert_gbp_to_currency(float(p["total_dividends"] or 0), display_currency, fx)
        p["forward_dividends_display"] = float(p["forward_dividends"] or 0) * native_to_display
        p["total_pnl_display"] = p["ppl_display"] + p["dividends_display"]
        p["total_pnl_pct_display"] = (p["total_pnl_display"] / p["cost_display"] * 100) if p["cost_display"] else 0
        p["div_return_pct_display"] = (p["dividends_display"] / p["cost_display"] * 100) if p["cost_display"] else 0
        p["fwd_yield_display"] = (p["forward_dividends_display"] / p["cost_display"] * 100) if p["cost_display"] else 0

    return templates.TemplateResponse(request, "index.html", {
        "user": user,
        "positions": positions,
        "totals": totals,
        "grand_total": grand_total_display,
        "fx_rates_used": fx_rates_display,
        "last_synced": last_synced,
        "account_filter": account,
        "display_currency": display_currency,
        "available_pies": available_pies,
        "selected_pie_ids": {str(i) for i in selected_pie_ids},
        "pies_param": pies,
        "country_filter": country,
        "sector_filter": sector,
        "free_cash": free_cash,
        "total_pie_cash": total_pie_cash,
        "fmt": _fmt,
        "cfmt": _cfmt,
    })


# ── Instruments research page ──────────────────────────────────────────────

@app.get("/instruments", response_class=HTMLResponse)
def instruments_page(request: Request, db: Session = Depends(get_session)):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(request, "instruments.html", {"user": user})


@app.get("/api/instruments")
def api_instruments(
    request: Request,
    db: Session = Depends(get_session),
    search: str = "",
    sector: str = "",
    country: str = "",
    exchange: str = "",
    instrument_type: str = "",
    share_fwd_yield_min: float | None = None,
    share_fwd_yield_max: float | None = None,
    pe_ratio_min: float | None = None,
    pe_ratio_max: float | None = None,
    div_cover_min: float | None = None,
    div_cover_max: float | None = None,
    fcf_div_cover_min: float | None = None,
    fcf_div_cover_max: float | None = None,
    held_only: bool = False,
    sort: str = "name",
    order: str = "asc",
    limit: int = 50,
    offset: int = 0,
):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return JSONResponse({"error": "unauthenticated"}, status_code=401)

    from sqlalchemy import func, or_, case, and_, select
    from app.models import Instrument, Position, DividendForecast

    # Tickers held by this user (across all accounts) — used for the
    # "In portfolio" badge and the held_only filter
    held_rows = (
        db.query(Position.ticker, Position.account)
        .filter(Position.user_id == user.id)
        .all()
    )
    held_map: dict[str, list[str]] = {}
    for ticker, acct in held_rows:
        held_map.setdefault(ticker, []).append(acct)

    price_sq = (
        db.query(
            Position.ticker.label("ticker"),
            func.max(Position.current_price).label("current_price"),
        )
        .filter(Position.user_id == user.id)
        .group_by(Position.ticker)
        .subquery()
    )

    annual_rate_sq = (
        select(func.max(DividendForecast.annual_rate))
        .where(DividendForecast.ticker == Instrument.ticker)
        .scalar_subquery()
    )
    current_price_expr = func.coalesce(price_sq.c.current_price, Instrument.fallback_price)
    share_fwd_yield_expr = case(
        (
            and_(annual_rate_sq.is_not(None), annual_rate_sq > 0, current_price_expr.is_not(None), current_price_expr > 0),
            annual_rate_sq / current_price_expr * 100.0,
        ),
        else_=None,
    )
    pe_ratio_expr = case(
        (
            and_(Instrument.eps_ttm.is_not(None), Instrument.eps_ttm > 0, current_price_expr.is_not(None), current_price_expr > 0),
            current_price_expr / Instrument.eps_ttm,
        ),
        else_=None,
    )
    div_cover_expr = case(
        (
            and_(Instrument.eps_ttm.is_not(None), Instrument.eps_ttm > 0, annual_rate_sq.is_not(None), annual_rate_sq > 0),
            Instrument.eps_ttm / annual_rate_sq,
        ),
        else_=None,
    )
    fcf_div_cover_expr = case(
        (
            and_(Instrument.fcf_per_share_3y_avg.is_not(None), Instrument.fcf_per_share_3y_avg > 0, annual_rate_sq.is_not(None), annual_rate_sq > 0),
            Instrument.fcf_per_share_3y_avg / annual_rate_sq,
        ),
        else_=None,
    )

    q = (
        db.query(
            Instrument,
            current_price_expr.label("current_price"),
            annual_rate_sq.label("annual_rate_per_share"),
            share_fwd_yield_expr.label("share_fwd_yield"),
            pe_ratio_expr.label("pe_ratio"),
            div_cover_expr.label("div_cover"),
            fcf_div_cover_expr.label("fcf_div_cover"),
        )
        .outerjoin(price_sq, price_sq.c.ticker == Instrument.ticker)
    )

    # Text search across name, short_name, ISIN and ticker
    if search.strip():
        term = f"%{search.strip()}%"
        q = q.filter(
            or_(
                Instrument.name.ilike(term),
                Instrument.short_name.ilike(term),
                Instrument.ticker.ilike(term),
                Instrument.isin.ilike(term),
            )
        )

    if sector.strip():
        q = q.filter(Instrument.sector == sector.strip())
    if country.strip():
        q = q.filter(Instrument.country == country.strip())
    if exchange.strip():
        q = q.filter(Instrument.exchange == exchange.strip())
    if instrument_type.strip():
        q = q.filter(Instrument.instrument_type == instrument_type.strip())
    if share_fwd_yield_min is not None:
        q = q.filter(share_fwd_yield_expr >= share_fwd_yield_min)
    if share_fwd_yield_max is not None:
        q = q.filter(share_fwd_yield_expr <= share_fwd_yield_max)
    if pe_ratio_min is not None:
        q = q.filter(pe_ratio_expr >= pe_ratio_min)
    if pe_ratio_max is not None:
        q = q.filter(pe_ratio_expr <= pe_ratio_max)
    if div_cover_min is not None:
        q = q.filter(div_cover_expr >= div_cover_min)
    if div_cover_max is not None:
        q = q.filter(div_cover_expr <= div_cover_max)
    if fcf_div_cover_min is not None:
        q = q.filter(fcf_div_cover_expr >= fcf_div_cover_min)
    if fcf_div_cover_max is not None:
        q = q.filter(fcf_div_cover_expr <= fcf_div_cover_max)
    if held_only:
        if held_map:
            q = q.filter(Instrument.ticker.in_(list(held_map.keys())))
        else:
            # User holds nothing — return empty
            return JSONResponse({"total": 0, "items": [], "filter_options": _instrument_filter_options(db)})

    total = q.count()

    # Sorting
    _sort_cols = {
        "name":            Instrument.name,
        "sector":          Instrument.sector,
        "country":         Instrument.country,
        "exchange":        Instrument.exchange,
        "instrument_type": Instrument.instrument_type,
        "market_cap":      Instrument.market_cap,
        "currency_code":   Instrument.currency_code,
        "share_fwd_yield": share_fwd_yield_expr,
        "pe_ratio":        pe_ratio_expr,
        "div_cover":       div_cover_expr,
        "fcf_div_cover":   fcf_div_cover_expr,
    }
    col = _sort_cols.get(sort, Instrument.name)
    q = q.order_by(col.asc().nullslast() if order == "asc" else col.desc().nullslast())

    rows = q.offset(offset).limit(min(limit, 200)).all()

    items = []
    for inst, current_price, annual_rate_per_share, share_fwd_yield, pe_ratio, div_cover, fcf_div_cover in rows:
        accounts = held_map.get(inst.ticker, [])
        items.append({
            "ticker":          inst.ticker,
            "name":            inst.name,
            "short_name":      inst.short_name,
            "exchange":        inst.exchange,
            "sector":          inst.sector,
            "country":         inst.country,
            "instrument_type": inst.instrument_type,
            "currency_code":   inst.currency_code,
            "market_cap":      inst.market_cap,
            "current_price":   float(current_price) if current_price is not None else None,
            "annual_rate_per_share": float(annual_rate_per_share) if annual_rate_per_share is not None else None,
            "share_fwd_yield": float(share_fwd_yield) if share_fwd_yield is not None else None,
            "pe_ratio":        float(pe_ratio) if pe_ratio is not None else None,
            "div_cover":       float(div_cover) if div_cover is not None else None,
            "fcf_div_cover":   float(fcf_div_cover) if fcf_div_cover is not None else None,
            "fallback_price":  inst.fallback_price,
            "fallback_price_source": inst.fallback_price_source,
            "fallback_price_updated_at": (
                inst.fallback_price_updated_at.isoformat() if inst.fallback_price_updated_at else None
            ),
            "isin":            inst.isin,
            "yf_ticker":       inst.yf_ticker,
            "figi":            inst.figi,
            "composite_figi":  inst.composite_figi,
            "mic_code":        inst.mic_code,
            "security_type":   inst.security_type,
            "security_type2":  inst.security_type2,
            "market_sector":   inst.market_sector,
            "is_held":         bool(accounts),
            "held_accounts":   accounts,
        })

    return JSONResponse({
        "total":          total,
        "items":          items,
        "filter_options": _instrument_filter_options(db),
    })


def _instrument_filter_options(db: Session) -> dict:
    """Return distinct non-null values for each filterable dimension."""
    from app.models import Instrument

    def _distinct(col):
        return sorted(
            r[0] for r in db.query(col).filter(col.isnot(None)).distinct().all()
        )

    return {
        "sectors":          _distinct(Instrument.sector),
        "countries":        _distinct(Instrument.country),
        "exchanges":        _distinct(Instrument.exchange),
        "instrument_types": _distinct(Instrument.instrument_type),
        "security_types":   _distinct(Instrument.security_type),
        "mic_codes":        _distinct(Instrument.mic_code),
    }


# ── Transactions page ──────────────────────────────────────────────────────

_INTERNAL_TICKERS = {"PSRU_EQ", "PSEU_EQ", "PSUSA_EQ"}


@app.get("/transactions", response_class=HTMLResponse)
def transactions_page(request: Request, show_internal: bool = False,
                      db: Session = Depends(get_session)):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    rows = db.execute(text("""
        SELECT
            o.filled_at,
            o.ticker,
            COALESCE(NULLIF(i.name, ''), i.short_name, o.ticker)  AS name,
            COALESCE(NULLIF(i.short_name, ''), o.ticker)           AS short_name,
            o.account,
            o.side,
            o.order_type,
            o.filled_quantity                                       AS quantity,
            o.fill_price,
            COALESCE(i.currency_code, 'GBP')                       AS currency
        FROM orders o
        LEFT JOIN instruments i ON i.ticker = o.ticker
        WHERE o.user_id = :user_id AND o.status = 'FILLED'
        ORDER BY o.filled_at DESC
    """), {"user_id": user.id}).fetchall()

    orders = [dict(r._mapping) for r in rows]

    if not show_internal:
        orders = [o for o in orders if o["ticker"] not in _INTERNAL_TICKERS]

    currencies = {o["currency"] for o in orders if o["currency"]}
    fx = _get_fx_rates_to_gbp(currencies)

    for o in orders:
        ccy = o["currency"] or "GBP"
        if ccy == "GBX":
            price_gbp = float(o["fill_price"] or 0) * 0.01
        else:
            price_gbp = float(o["fill_price"] or 0) * fx.get(ccy, 1.0)
        o["fill_price_gbp"] = price_gbp
        o["value_gbp"] = float(o["quantity"] or 0) * price_gbp

    total_buy_gbp  = sum(o["value_gbp"] for o in orders if o["side"] == "BUY")
    total_sell_gbp = sum(o["value_gbp"] for o in orders if o["side"] == "SELL")

    last_synced = db.execute(text(
        "SELECT MAX(last_synced_at) FROM positions WHERE user_id = :uid"
    ), {"uid": user.id}).scalar()

    return templates.TemplateResponse(request, "transactions.html", {
        "user": user,
        "orders": orders,
        "total_buy_gbp": total_buy_gbp,
        "total_sell_gbp": total_sell_gbp,
        "show_internal": show_internal,
        "last_synced": last_synced,
        "fmt": _fmt,
        "cfmt": _cfmt,
    })


# ── Dividends page ──────────────────────────────────────────────────────────

@app.get("/dividends", response_class=HTMLResponse)
def dividends_page(request: Request, month: str = "", account: str = "",
                   db: Session = Depends(get_session)):
    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    # Validate params
    import re as _re
    if month and not _re.match(r"^\d{4}-\d{2}$", month):
        month = ""
    if account not in ("ISA", "Trading"):
        account = ""

    # Build filter clauses
    month_clause   = "AND TO_CHAR(dp.paid_on, 'YYYY-MM') = :month"   if month   else ""
    account_clause = "AND dp.account = :account"                       if account else ""
    params: dict = {"user_id": user.id}
    if month:
        params["month"] = month
    if account:
        params["account"] = account

    rows = db.execute(text(f"""
        SELECT
            dp.paid_on,
            dp.ticker,
            COALESCE(NULLIF(i.name, ''), i.short_name, dp.ticker)  AS name,
            COALESCE(NULLIF(i.short_name, ''), dp.ticker)           AS short_name,
            dp.account,
            dp.amount,
            dp.quantity,
            dp.gross_amount_per_share,
            dp.type,
            COALESCE(i.currency_code, 'GBP')                        AS currency
        FROM dividend_payments dp
        LEFT JOIN instruments i ON i.ticker = dp.ticker
        WHERE dp.user_id = :user_id
          {month_clause}
          {account_clause}
        ORDER BY dp.paid_on DESC
    """), params).fetchall()

    payments = [dict(r._mapping) for r in rows]
    total_gbp = sum(float(p["amount"] or 0) for p in payments)

    # Available months scoped to the active account filter so the dropdown
    # only shows months that actually have payments for the selected account
    month_params: dict = {"user_id": user.id}
    month_account_clause = ""
    if account:
        month_account_clause = "AND dp.account = :account"
        month_params["account"] = account
    month_rows = db.execute(text(f"""
        SELECT DISTINCT
            TO_CHAR(dp.paid_on, 'YYYY-MM')   AS month_val,
            TO_CHAR(dp.paid_on, 'Mon YYYY')  AS month_label
        FROM dividend_payments dp
        WHERE dp.user_id = :user_id
          AND dp.paid_on IS NOT NULL
          {month_account_clause}
        ORDER BY 1 DESC
    """), month_params).fetchall()
    available_months = [{"val": r.month_val, "label": r.month_label} for r in month_rows]

    last_synced = db.execute(text(
        "SELECT MAX(last_synced_at) FROM positions WHERE user_id = :uid"
    ), {"uid": user.id}).scalar()

    return templates.TemplateResponse(request, "dividends.html", {
        "user": user,
        "payments": payments,
        "total_gbp": total_gbp,
        "month_filter": month,
        "account_filter": account,
        "available_months": available_months,
        "last_synced": last_synced,
        "fmt": _fmt,
        "cfmt": _cfmt,
    })


# ── Portfolio Analysis ─────────────────────────────────────────────────────

_PALETTE = [
    '#00c896', '#3b82f6', '#f59e0b', '#ef4444', '#8b5cf6',
    '#06b6d4', '#f97316', '#84cc16', '#ec4899', '#6b7280',
    '#14b8a6', '#a855f7', '#eab308', '#f43f5e', '#0ea5e9',
]

_ANALYSIS_COMBINED_SQL = text("""
    SELECT
        p.ticker,
        COALESCE(NULLIF(i.name, ''), i.short_name, p.ticker)  AS name,
        COALESCE(i.currency_code, 'GBP')                       AS currency,
        COALESCE(i.instrument_type, '')                        AS instrument_type,
        i.sector, i.industry, i.instrument_class, i.country,
        SUM(p.quantity * COALESCE(p.current_price, p.average_price)) AS value
    FROM positions p
    JOIN instruments i ON p.ticker = i.ticker
    WHERE p.user_id = :user_id
    GROUP BY p.ticker, i.name, i.short_name, i.currency_code,
             i.instrument_type, i.sector, i.industry, i.instrument_class, i.country
""")

_ANALYSIS_ACCOUNT_SQL = text("""
    SELECT
        p.ticker,
        COALESCE(NULLIF(i.name, ''), i.short_name, p.ticker)  AS name,
        COALESCE(i.currency_code, 'GBP')                       AS currency,
        COALESCE(i.instrument_type, '')                        AS instrument_type,
        i.sector, i.industry, i.instrument_class, i.country,
        p.quantity * COALESCE(p.current_price, p.average_price) AS value
    FROM positions p
    JOIN instruments i ON p.ticker = i.ticker
    WHERE p.user_id = :user_id AND p.account = :account
""")


def _build_ai_pie_filter_key(pies: str, selected_pie_ids: list[int]) -> str:
    if pies == "all":
        return "all"
    if selected_pie_ids:
        return ",".join(str(i) for i in selected_pie_ids)
    return "none"


def _format_analysis_timestamp(ts: datetime.datetime) -> str:
    return ts.strftime("%d %b %Y %H:%M UTC")


def _format_analysis_timestamp_iso(ts: datetime.datetime) -> str:
    return ts.replace(microsecond=0).isoformat() + "Z"


def _format_analysis_age(ts: datetime.datetime) -> str:
    age_seconds = max(int((datetime.datetime.utcnow() - ts).total_seconds()), 0)
    if age_seconds < 60:
        return "just now"
    if age_seconds < 3600:
        minutes = age_seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = age_seconds // 3600
    return f"{hours} hour{'s' if hours != 1 else ''} ago"


def _get_latest_ai_analysis_cache(
    db: Session,
    user_id: int,
    provider: str,
    account: str,
    pie_filter_key: str,
) -> AIPortfolioAnalysisCache | None:
    return (
        db.query(AIPortfolioAnalysisCache)
        .filter_by(
            user_id=user_id,
            provider=provider,
            account_filter=account,
            pie_filter_key=pie_filter_key,
        )
        .order_by(AIPortfolioAnalysisCache.updated_at.desc())
        .first()
    )


def _get_available_ai_providers() -> list[dict]:
    providers: list[dict] = []
    if settings.anthropic_api_key:
        providers.append({"id": "anthropic", "label": "Claude"})
    if settings.openai_api_key:
        providers.append({"id": "openai", "label": "OpenAI"})
    return providers


def _resolve_ai_provider(requested_provider: str | None) -> str:
    available = _get_available_ai_providers()
    if not available:
        return "anthropic"
    available_ids = {p["id"] for p in available}
    if requested_provider in available_ids:
        return requested_provider
    return available[0]["id"]


def _build_ai_system_prompt() -> str:
    return (
        "You are writing a detailed educational portfolio review for a private investor dashboard. "
        "Be rigorous, balanced, and practical. Do not claim certainty or provide regulated financial advice. "
        "Use only the supplied data and call out missing data clearly. "
        "This portfolio is held on Trading 212, so avoid overemphasising traditional platform dealing fees, "
        "custody charges, or other legacy broker transaction-cost structures unless they are directly relevant."
    )


def _build_ai_user_prompt(holdings_payload: dict) -> str:
    return (
        "Analyse this portfolio as deeply as possible.\n\n"
        "Requirements:\n"
        "- Give the most in-depth investment analysis possible from the supplied holdings data.\n"
        "- Assess concentration, diversification quality, sector exposure, country exposure, "
        "position sizing, income dependence, overlap risk, and any obvious valuation or "
        "quality signals from the available metrics.\n"
        "- Highlight strengths as well as vulnerabilities.\n"
        "- Give concrete recommendations for risks to monitor and potential portfolio changes.\n"
        "- Be specific and avoid generic filler.\n"
        "- Format the answer in Markdown with these sections: Executive Summary, Portfolio Snapshot, "
        "Strengths, Risks and Weaknesses, Recommendations, Questions to Consider, Disclaimer.\n"
        "- This is a Trading 212 portfolio, so do not spend much time discussing traditional broker fee models, "
        "ticket charges, or custody-fee structures unless the supplied data makes that specifically relevant.\n"
        "- Do not stop mid-section. If you need more space, continue from exactly where you left off.\n\n"
        f"Portfolio data JSON:\n{json.dumps(holdings_payload, indent=2)}"
    )


def _build_ai_holdings_payload(
    positions: list[dict],
    fx_to_gbp: dict[str, float],
    account: str,
    selected_pie_ids: list[int],
    pies_param: str,
) -> dict:
    holdings: list[dict] = []
    total_value_gbp = 0.0

    for row in positions:
        currency = row.get("currency") or "GBP"
        rate_to_gbp = float(fx_to_gbp.get(currency, 1.0) or 1.0)
        value_native = float(row.get("value") or 0.0)
        cost_native = float(row.get("cost") or 0.0)
        value_gbp = value_native * rate_to_gbp
        cost_gbp = cost_native * rate_to_gbp
        total_value_gbp += value_gbp
        holdings.append({
            "ticker": row.get("ticker"),
            "name": row.get("name"),
            "account": row.get("account") or account,
            "quantity": round(float(row.get("quantity") or 0.0), 6),
            "currency": currency,
            "current_price": round(float(row.get("current_price") or 0.0), 6),
            "average_price": round(float(row.get("average_price") or 0.0), 6),
            "cost_native": round(cost_native, 2),
            "value_native": round(value_native, 2),
            "cost_gbp": round(cost_gbp, 2),
            "value_gbp": round(value_gbp, 2),
            "pnl_native": round(float(row.get("ppl") or 0.0), 2),
            "return_pct": round(float(row.get("result_coef") or 0.0) * 100, 2),
            "total_dividends_gbp": round(float(row.get("total_dividends") or 0.0), 2),
            "forward_dividends_native": round(float(row.get("forward_dividends") or 0.0), 2),
            "annual_dividend_per_share_native": round(float(row.get("annual_rate_per_share") or 0.0), 4),
            "exchange": row.get("exchange"),
            "country": row.get("country") or "Unknown",
            "sector": row.get("sector") or "",
            "industry": row.get("industry") or "",
            "instrument_type": row.get("instrument_type") or "",
            "instrument_class": row.get("instrument_class") or "",
            "pie_count": int(row.get("pie_count") or 0),
            "fcf_per_share_3y_avg": row.get("fcf_per_share_3y_avg"),
            "eps_ttm": row.get("eps_ttm"),
        })

    total_value_gbp = round(total_value_gbp, 2)
    for holding in holdings:
        holding["weight_pct"] = round(
            (holding["value_gbp"] / total_value_gbp * 100) if total_value_gbp else 0.0,
            2,
        )

    holdings.sort(key=lambda h: h["value_gbp"], reverse=True)
    return {
        "filter": {
            "account": account,
            "pies": pies_param or "",
            "selected_pie_ids": selected_pie_ids,
        },
        "portfolio": {
            "total_value_gbp": total_value_gbp,
            "holdings_count": len(holdings),
        },
        "holdings": holdings,
    }


def _build_ai_holdings_hash(account: str, pie_filter_key: str, holdings_payload: dict) -> str:
    canonical = json.dumps(
        {
            "account": account,
            "pie_filter_key": pie_filter_key,
            "payload": holdings_payload,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _request_anthropic_portfolio_analysis(holdings_payload: dict) -> tuple[str, str, str]:
    if not settings.anthropic_api_key:
        raise RuntimeError("Anthropic API key is not configured.")

    import anthropic

    model = settings.anthropic_analysis_model
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    system_prompt = _build_ai_system_prompt()
    user_prompt = _build_ai_user_prompt(holdings_payload)
    messages = [{"role": "user", "content": user_prompt}]
    collected_parts: list[str] = []

    for attempt in range(3):
        message = client.messages.create(
            model=model,
            max_tokens=7000,
            system=system_prompt,
            messages=messages,
        )
        text_blocks = [
            block.text.strip()
            for block in (message.content or [])
            if getattr(block, "type", "") == "text" and getattr(block, "text", "").strip()
        ]
        part = "\n\n".join(text_blocks).strip()
        if part:
            collected_parts.append(part)

        stop_reason = getattr(message, "stop_reason", None)
        if stop_reason != "max_tokens":
            break

        messages.extend([
            {"role": "assistant", "content": part},
            {"role": "user", "content": "Continue exactly where you left off and finish the remaining sections without repeating earlier content."},
        ])

    analysis_text = "\n\n".join(p for p in collected_parts if p).strip()
    if not analysis_text:
        raise RuntimeError("Claude returned an empty analysis.")
    prompt_text = f"System:\n{system_prompt}\n\nUser:\n{user_prompt}"
    return analysis_text, model, prompt_text


def _request_openai_portfolio_analysis(holdings_payload: dict) -> tuple[str, str, str]:
    if not settings.openai_api_key:
        raise RuntimeError("OpenAI API key is not configured.")

    from openai import OpenAI

    model = settings.openai_analysis_model
    client = OpenAI(api_key=settings.openai_api_key)
    system_prompt = _build_ai_system_prompt()
    user_prompt = _build_ai_user_prompt(holdings_payload)
    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    analysis_text = (
        completion.choices[0].message.content.strip()
        if getattr(completion, "choices", None)
        and completion.choices[0].message
        and completion.choices[0].message.content
        else ""
    )
    if not analysis_text:
        raise RuntimeError("OpenAI returned an empty analysis.")
    prompt_text = f"System:\n{system_prompt}\n\nUser:\n{user_prompt}"
    return analysis_text, model, prompt_text


def _request_ai_portfolio_analysis(provider: str, holdings_payload: dict) -> tuple[str, str, str]:
    if provider == "openai":
        return _request_openai_portfolio_analysis(holdings_payload)
    return _request_anthropic_portfolio_analysis(holdings_payload)


def _build_portfolio_history(db: Session, user_id: int, account: str, selected_pie_ids: list[int]) -> str:
    params: dict = {"user_id": user_id}

    if selected_pie_ids:
        safe_ids = ", ".join(str(int(i)) for i in selected_pie_ids)
        if account in ("ISA", "Trading"):
            params["account"] = account
            account_filter = "AND ps.account = :account"
        else:
            account_filter = ""
        rows = db.execute(text(f"""
            SELECT ps.snapshot_date, SUM(ps.total_value_gbp) AS total_value_gbp
            FROM portfolio_snapshots ps
            WHERE ps.user_id = :user_id
              AND ps.scope_type = 'pie'
              AND ps.pie_id IN ({safe_ids})
              {account_filter}
            GROUP BY ps.snapshot_date
            ORDER BY ps.snapshot_date
        """), params).fetchall()
    else:
        if account in ("ISA", "Trading"):
            params["account"] = account
            account_filter = "AND ps.account = :account"
        else:
            account_filter = ""
        rows = db.execute(text(f"""
            SELECT ps.snapshot_date, SUM(ps.total_value_gbp) AS total_value_gbp
            FROM portfolio_snapshots ps
            WHERE ps.user_id = :user_id
              AND ps.scope_type = 'account'
              {account_filter}
            GROUP BY ps.snapshot_date
            ORDER BY ps.snapshot_date
        """), params).fetchall()

    values = [round(float(r.total_value_gbp or 0), 2) for r in rows]
    return json.dumps({
        "labels": [r.snapshot_date.strftime("%d %b %Y") for r in rows],
        "values": values,
        "latest": values[-1] if values else 0.0,
    })


def _analysis_pie_query(pie_ids: list[int], account: str) -> str:
    safe_ids = ", ".join(str(int(i)) for i in pie_ids)
    pos_account_filter = "AND pos.account = :account" if account in ("ISA", "Trading") else ""
    return f"""
        WITH pie_agg AS (
            SELECT ph.ticker, SUM(ph.owned_quantity) AS quantity
            FROM pie_holdings ph
            WHERE ph.pie_id IN ({safe_ids})
              AND ph.user_id = :user_id
            GROUP BY ph.ticker
        )
        SELECT
            pa.ticker,
            COALESCE(NULLIF(i.name, ''), i.short_name, pa.ticker) AS name,
            COALESCE(i.currency_code, 'GBP')  AS currency,
            COALESCE(i.instrument_type, '')    AS instrument_type,
            i.sector, i.industry, i.instrument_class, i.country,
            SUM(pa.quantity * COALESCE(pos.current_price, pos.average_price)) AS value
        FROM pie_agg pa
        JOIN instruments i ON pa.ticker = i.ticker
        JOIN positions pos ON pos.ticker = pa.ticker AND pos.user_id = :user_id
            {pos_account_filter}
        GROUP BY pa.ticker, i.name, i.short_name, i.currency_code,
                 i.instrument_type, i.sector, i.industry, i.instrument_class, i.country
    """


@app.get("/analysis", response_class=HTMLResponse)
def portfolio_analysis(request: Request, account: str = "combined", pies: str = "", ai_provider: str = "",
                       db: Session = Depends(get_session)):
    user = _get_current_user(request, db)
    if not user:
        return _redirect("/login")

    if account not in ("ISA", "Trading"):
        account = "combined"
    available_ai_providers = _get_available_ai_providers()
    selected_ai_provider = _resolve_ai_provider(ai_provider or None)

    available_pies = _load_pies(db, user.id, account)
    selected_pie_ids = _resolve_selected_pie_ids(available_pies, pies)
    positions = _fetch_filtered_positions(db, user.id, account, selected_pie_ids)

    currencies = {p["currency"] for p in positions if p.get("currency")}
    fx = _get_fx_rates_to_gbp(currencies)

    geo: dict[str, float] = {}
    sector_agg: dict[str, float] = {}

    for p in positions:
        ccy = p.get("currency") or "GBP"
        value_gbp = float(p.get("value") or 0) * fx.get(ccy, 1.0)

        country = (p.get("country") or "").strip() or "Unknown"
        geo[country] = geo.get(country, 0) + value_gbp

        s = (p.get("sector") or "").strip()
        if not s:
            s = _classify(
                p.get("instrument_type") or "",
                p.get("sector") or "",
                p.get("industry") or "",
                p.get("name") or "",
                p.get("instrument_class"),
            )
        sector_agg[s] = sector_agg.get(s, 0) + value_gbp

    total_value = sum(geo.values())

    def _build_rows(d: dict[str, float]) -> list[dict]:
        total = sum(d.values()) or 1
        rows_ = sorted(
            [{"label": k, "value": v, "pct": v / total * 100} for k, v in d.items()],
            key=lambda x: -x["value"],
        )
        for i, row in enumerate(rows_):
            row["color"] = _PALETTE[i % len(_PALETTE)]
        return rows_

    def _build_chart_json(rows_: list[dict], max_slices: int = 12) -> str:
        top = list(rows_[:max_slices])
        rest = rows_[max_slices:]
        if rest:
            other_val = sum(r["value"] for r in rest)
            other_pct = sum(r["pct"] for r in rest)
            top.append({"label": "Other", "value": other_val, "pct": other_pct,
                        "color": _PALETTE[len(top) % len(_PALETTE)]})
        return json.dumps({
            "labels": [r["label"] for r in top],
            "values": [round(r["value"], 2) for r in top],
            "pcts":   [round(r["pct"], 2) for r in top],
            "colors": [r["color"] for r in top],
        })

    geo_table    = _build_rows(geo)
    sector_table = _build_rows(sector_agg)

    # Add click-through hrefs — rows link to main portfolio with that filter applied
    _base = {"account": account}
    if pies:
        _base["pies"] = pies
    for row in geo_table:
        row["href"] = "/?" + urlencode({**_base, "country": row["label"]})
    for row in sector_table:
        row["href"] = "/?" + urlencode({**_base, "sector": row["label"]})

    selected_tickers = {p["ticker"] for p in positions}
    div_holdings_q = """
        SELECT
            dp.ticker AS ticker,
            COALESCE(NULLIF(i.name, ''), i.short_name, dp.ticker) AS name,
            SUM(dp.amount) AS total
        FROM dividend_payments dp
        LEFT JOIN instruments i ON i.ticker = dp.ticker
        WHERE dp.user_id = :user_id
    """
    div_holdings_params: dict = {"user_id": user.id}
    if account in ("ISA", "Trading"):
        div_holdings_q += " AND dp.account = :account"
        div_holdings_params["account"] = account

    div_holding_rows = db.execute(text(div_holdings_q + """
        GROUP BY dp.ticker, i.name, i.short_name
        ORDER BY SUM(dp.amount) DESC
    """), div_holdings_params).fetchall()

    div_holdings = []
    for row in div_holding_rows:
        if selected_tickers and row.ticker not in selected_tickers:
            continue
        div_holdings.append({
            "ticker": row.ticker,
            "label": row.name,
            "value": float(row.total or 0),
        })

    div_holdings_total = sum(r["value"] for r in div_holdings) or 1.0
    for i, row in enumerate(div_holdings):
        row["pct"] = row["value"] / div_holdings_total * 100
        row["color"] = _PALETTE[i % len(_PALETTE)]
        row["href"] = f"/holding/{quote(str(row['ticker']), safe='')}?account={account}"

    div_holdings_top = div_holdings[:15]

    # ── Dividends by month ────────────────────────────────────────────────
    # Fetch the last 24 months of dividend payments, scoped to the current
    # account filter. Amounts are already stored in GBP by T212.
    div_account_filter = "AND dp.account = :account" if account in ("ISA", "Trading") else ""
    div_rows = db.execute(text(f"""
        SELECT
            TO_CHAR(DATE_TRUNC('month', dp.paid_on), 'Mon YYYY') AS month_label,
            DATE_TRUNC('month', dp.paid_on)                       AS month_dt,
            SUM(dp.amount)                                        AS total
        FROM dividend_payments dp
        WHERE dp.user_id = :user_id
          {div_account_filter}
          AND dp.paid_on >= NOW() - INTERVAL '24 months'
        GROUP BY DATE_TRUNC('month', dp.paid_on)
        ORDER BY DATE_TRUNC('month', dp.paid_on)
    """), {"user_id": user.id, **({} if account == "combined" else {"account": account})}).fetchall()

    div_by_month = json.dumps({
        "labels": [r.month_label for r in div_rows],
        "values": [round(float(r.total), 2) for r in div_rows],
        "total":  round(sum(float(r.total) for r in div_rows), 2),
    })
    portfolio_history = _build_portfolio_history(db, user.id, account, selected_pie_ids)
    pie_filter_key = _build_ai_pie_filter_key(pies, selected_pie_ids)
    cached_ai_analysis = _get_latest_ai_analysis_cache(
        db, user.id, selected_ai_provider, account, pie_filter_key
    )
    initial_ai_analysis = None
    ai_analysis_header_text = "No cached analysis yet"
    ai_analysis_button_label = "Analyse now"
    if cached_ai_analysis:
        initial_ai_analysis = {
            "analysis": cached_ai_analysis.analysis_text,
            "cached": True,
            "provider": cached_ai_analysis.provider,
            "model": cached_ai_analysis.model,
            "prompt_text": cached_ai_analysis.prompt_text,
            "generated_at": _format_analysis_timestamp(cached_ai_analysis.updated_at),
            "generated_at_iso": _format_analysis_timestamp_iso(cached_ai_analysis.updated_at),
            "age": _format_analysis_age(cached_ai_analysis.updated_at),
            "holdings_count": cached_ai_analysis.holdings_count,
        }
        ai_analysis_header_text = "Last run previously saved"
        ai_analysis_button_label = "Reanalyse"

    return templates.TemplateResponse(request, "analysis.html", {
        "user": user,
        "account_filter": account,
        "available_pies": available_pies,
        "selected_pie_ids": [str(i) for i in selected_pie_ids],
        "pies_param": pies,
        "available_ai_providers": available_ai_providers,
        "selected_ai_provider": selected_ai_provider,
        "total_value": total_value,
        "geo_table": geo_table,
        "sector_table": sector_table,
        "geo_data": _build_chart_json(geo_table),
        "sector_data": _build_chart_json(sector_table),
        "div_holdings_table": div_holdings_top,
        "div_holdings_data": _build_chart_json(div_holdings_top),
        "div_by_month": div_by_month,
        "portfolio_history": portfolio_history,
        "initial_ai_analysis": initial_ai_analysis,
        "ai_analysis_header_text": ai_analysis_header_text,
        "ai_analysis_button_label": ai_analysis_button_label,
        "fmt": _fmt,
        "cfmt": _cfmt,
    })


# ── Holding detail ─────────────────────────────────────────────────────────

@app.post("/analysis/ai")
def portfolio_ai_analysis(
    request: Request,
    payload: dict = Body(default={}),
    db: Session = Depends(get_session),
):
    user = _get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "Authentication required."}, status_code=401)

    account = payload.get("account") or "combined"
    if account not in ("ISA", "Trading"):
        account = "combined"
    pies = str(payload.get("pies") or "")
    force_reanalyse = bool(payload.get("force_reanalyse"))
    provider = _resolve_ai_provider(payload.get("provider"))

    available_pies = _load_pies(db, user.id, account)
    selected_pie_ids = _resolve_selected_pie_ids(available_pies, pies)
    positions = _fetch_filtered_positions(db, user.id, account, selected_pie_ids)
    if not positions:
        return JSONResponse(
            {"error": "No holdings are available for the current filter."},
            status_code=400,
        )

    fx = _get_fx_rates_to_gbp({p["currency"] for p in positions if p.get("currency")})
    holdings_payload = _build_ai_holdings_payload(
        positions,
        fx,
        account,
        selected_pie_ids,
        pies,
    )
    pie_filter_key = _build_ai_pie_filter_key(pies, selected_pie_ids)
    holdings_hash = _build_ai_holdings_hash(account, pie_filter_key, holdings_payload)
    latest_cache_row = _get_latest_ai_analysis_cache(db, user.id, provider, account, pie_filter_key)

    exact_cache_row = (
        db.query(AIPortfolioAnalysisCache)
        .filter_by(
            user_id=user.id,
            provider=provider,
            account_filter=account,
            pie_filter_key=pie_filter_key,
            holdings_hash=holdings_hash,
        )
        .first()
    )
    now_utc = datetime.datetime.utcnow()
    if latest_cache_row and not force_reanalyse:
        return JSONResponse({
            "analysis": latest_cache_row.analysis_text,
            "cached": True,
            "provider": latest_cache_row.provider,
            "model": latest_cache_row.model,
            "prompt_text": latest_cache_row.prompt_text or _build_ai_user_prompt(holdings_payload),
            "generated_at": _format_analysis_timestamp(latest_cache_row.updated_at),
            "generated_at_iso": _format_analysis_timestamp_iso(latest_cache_row.updated_at),
            "age": _format_analysis_age(latest_cache_row.updated_at),
            "holdings_count": latest_cache_row.holdings_count,
        })

    try:
        analysis_text, model, prompt_text = _request_ai_portfolio_analysis(provider, holdings_payload)
    except Exception as exc:
        return JSONResponse(
            {"error": f"AI portfolio analysis failed: {exc}"},
            status_code=503,
        )

    cache_row = exact_cache_row or latest_cache_row
    if cache_row:
        cache_row.analysis_text = analysis_text
        cache_row.provider = provider
        cache_row.model = model
        cache_row.prompt_text = prompt_text
        cache_row.holdings_count = len(holdings_payload["holdings"])
        cache_row.holdings_hash = holdings_hash
        cache_row.updated_at = now_utc
    else:
        cache_row = AIPortfolioAnalysisCache(
            user_id=user.id,
            provider=provider,
            account_filter=account,
            pie_filter_key=pie_filter_key,
            holdings_hash=holdings_hash,
            model=model,
            prompt_text=prompt_text,
            analysis_text=analysis_text,
            holdings_count=len(holdings_payload["holdings"]),
            created_at=now_utc,
            updated_at=now_utc,
        )
        db.add(cache_row)

    db.commit()
    return JSONResponse({
        "analysis": analysis_text,
        "cached": False,
        "provider": provider,
        "model": model,
        "prompt_text": prompt_text,
        "generated_at": _format_analysis_timestamp(cache_row.updated_at),
        "generated_at_iso": _format_analysis_timestamp_iso(cache_row.updated_at),
        "age": "just now",
        "holdings_count": cache_row.holdings_count,
    })


@app.get("/holding/{ticker:path}", response_class=HTMLResponse)
def holding_detail(ticker: str, request: Request, account: str = "combined",
                   db: Session = Depends(get_session)):
    from app.models.instrument import Instrument
    from app.models.position import Position
    from app.models.dividend_payment import DividendPayment
    from app.models.dividend import DividendHistory, DividendForecast
    from app.models.order import Order

    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user

    instrument = db.query(Instrument).filter_by(ticker=ticker).first()
    if not instrument:
        return HTMLResponse("Instrument not found", status_code=404)

    # Positions for this user/ticker, optionally filtered by account
    q = db.query(Position).filter_by(ticker=ticker, user_id=user.id)
    if account in ("ISA", "Trading"):
        q = q.filter_by(account=account)
    positions_list = q.all()
    if not positions_list and account in ("ISA", "Trading"):
        # Fallback: show all accounts if none found for selected
        positions_list = db.query(Position).filter_by(ticker=ticker, user_id=user.id).all()

    currency = instrument.currency_code or "GBP"
    user_settings = db.query(UserSettings).filter_by(user_id=user.id).first()
    display_currency = _resolve_display_currency(user_settings, account)
    fx = _get_fx_rates_to_gbp({currency, display_currency})
    native_to_display = _native_to_display_rate(currency, display_currency, fx)
    # native_to_display converts listing-currency amounts into the active account display currency.

    total_quantity = sum(float(p.quantity or 0) for p in positions_list)
    total_cost_native = sum(float(p.quantity or 0) * float(p.average_price or 0) for p in positions_list)
    avg_price_native = total_cost_native / total_quantity if total_quantity else 0
    position_price = float(positions_list[0].current_price or 0) if positions_list else 0
    fallback_price = float(instrument.fallback_price or 0)
    current_price_native = position_price or fallback_price
    current_price_source = "t212" if position_price else (instrument.fallback_price_source if fallback_price else None)

    avg_price_display = avg_price_native * native_to_display
    current_price_display = current_price_native * native_to_display
    cost_display = total_cost_native * native_to_display
    value_display = total_quantity * current_price_native * native_to_display
    market_cap_display = (float(instrument.market_cap or 0) * native_to_display) if instrument.market_cap else 0
    cap_pnl_display = value_display - cost_display
    cap_pnl_pct = (cap_pnl_display / cost_display * 100) if cost_display else 0

    # Actual dividends received by user
    div_q = db.query(DividendPayment).filter_by(ticker=ticker, user_id=user.id)
    if account in ("ISA", "Trading"):
        div_q = div_q.filter_by(account=account)
    div_payments = div_q.order_by(DividendPayment.paid_on.desc()).all()
    total_divs_gbp = sum(float(dp.amount or 0) for dp in div_payments)
    total_divs_display = _convert_gbp_to_currency(total_divs_gbp, display_currency, fx)
    for dp in div_payments:
        dp.amount_display = _convert_gbp_to_currency(float(dp.amount or 0), display_currency, fx)

    total_pnl_display = cap_pnl_display + total_divs_display
    total_pnl_pct = (total_pnl_display / cost_display * 100) if cost_display else 0
    actual_yield = (total_divs_display / cost_display * 100) if cost_display else 0

    # Forward dividend metrics
    annual_rate = db.execute(
        text("SELECT MAX(annual_rate) FROM dividend_forecast WHERE ticker = :t"),
        {"t": ticker},
    ).scalar() or 0
    fwd_div_display = float(annual_rate) * total_quantity * native_to_display
    personal_fwd_yield = (fwd_div_display / cost_display * 100) if cost_display else 0
    share_fwd_yield = (float(annual_rate) / float(current_price_native) * 100) if (annual_rate and current_price_native) else 0

    # Valuation / coverage
    eps_ttm = float(instrument.eps_ttm or 0)
    annual_dps = float(annual_rate)
    pe_ratio = (current_price_native / eps_ttm) if (eps_ttm > 0 and current_price_native) else None
    fcf_ps = float(instrument.fcf_per_share_3y_avg or 0)
    fcf_cov = (annual_dps / fcf_ps * 100) if (fcf_ps > 0 and annual_dps > 0) else None
    div_cov = (eps_ttm / annual_dps) if (eps_ttm > 0 and annual_dps > 0) else None

    # Trade history (filled orders)
    orders_q = (
        db.query(Order)
        .filter(Order.ticker == ticker, Order.user_id == user.id, Order.status == "FILLED")
    )
    if account in ("ISA", "Trading"):
        orders_q = orders_q.filter(Order.account == account)
    trade_history = orders_q.order_by(Order.filled_at.desc()).all()

    # Historical per-share dividends
    hist_divs = (
        db.query(DividendHistory)
        .filter_by(ticker=ticker)
        .order_by(DividendHistory.ex_date.desc())
        .limit(24)
        .all()
    )

    # Upcoming dividend forecast
    forecast_divs = (
        db.query(DividendForecast)
        .filter_by(ticker=ticker)
        .order_by(DividendForecast.ex_date.asc())
        .all()
    )

    # Pies containing this ticker — scoped to the current user
    pie_rows = db.execute(text("""
        SELECT pie.pk AS id, pie.name, pie.account,
               ph.owned_quantity, ph.current_share, ph.expected_share
        FROM pies pie
        JOIN pie_holdings ph ON ph.pie_id = pie.pk
        WHERE ph.ticker = :ticker
          AND pie.user_id = :user_id
          AND (:account = 'combined' OR pie.account = :account OR pie.account IS NULL)
        ORDER BY pie.name
    """), {"ticker": ticker, "account": account, "user_id": user.id}).fetchall()
    pies_in = [dict(r._mapping) for r in pie_rows]

    return templates.TemplateResponse(request, "holding.html", {
        "user": user,
        "instrument": instrument,
        "ticker": ticker,
        "account_filter": account,
        "display_currency": display_currency,
        "positions": positions_list,
        "total_quantity": total_quantity,
        "avg_price_display": avg_price_display,
        "current_price_display": current_price_display,
        "current_price_source": current_price_source,
        "market_cap_display": market_cap_display,
        "cost_display": cost_display,
        "value_display": value_display,
        "cap_pnl_display": cap_pnl_display,
        "cap_pnl_pct": cap_pnl_pct,
        "total_divs_display": total_divs_display,
        "total_pnl_display": total_pnl_display,
        "total_pnl_pct": total_pnl_pct,
        "actual_yield": actual_yield,
        "fwd_div_display": fwd_div_display,
        "personal_fwd_yield": personal_fwd_yield,
        "share_fwd_yield": share_fwd_yield,
        "pe_ratio": pe_ratio,
        "fcf_cov": fcf_cov,
        "div_cov": div_cov,
        "annual_dps": annual_dps,
        "eps_ttm": eps_ttm,
        "fcf_ps": fcf_ps,
        "trade_history": trade_history,
        "div_payments": div_payments,
        "hist_divs": hist_divs,
        "forecast_divs": forecast_divs,
        "pies_in": pies_in,
        "fmt": _fmt,
        "cfmt": _cfmt,
    })


@app.post("/holding/{ticker:path}/refresh")
def refresh_holding_data(ticker: str, request: Request, db: Session = Depends(get_session)):
    from app.models.instrument import Instrument
    from app.sync.runner import SyncRunner
    from app.sync.dividend_sync import DividendSync

    user = _require_user(request, db)
    if isinstance(user, RedirectResponse):
        return JSONResponse({"error": "Unauthorised"}, status_code=401)

    instrument = db.query(Instrument).filter_by(ticker=ticker).first()
    if not instrument:
        return JSONResponse({"error": "Instrument not found"}, status_code=404)

    try:
        runner = SyncRunner(db, user_id=user.id)
        metadata_result = runner.refresh_single_instrument(ticker)

        div_sync = DividendSync(
            session=db,
            fmp_api_key=settings.fmp_api_key,
        )
        div_sync.sync_all(tickers=[ticker])

        annual_rate = db.execute(
            text("SELECT MAX(annual_rate) FROM dividend_forecast WHERE ticker = :t"),
            {"t": ticker},
        ).scalar()
        return JSONResponse({
            "ok": True,
            "ticker": ticker,
            "yf_ticker": metadata_result.get("yf_ticker"),
            "eps_ttm": metadata_result.get("eps_ttm"),
            "fcf_per_share_3y_avg": metadata_result.get("fcf_per_share_3y_avg"),
            "annual_dps": float(annual_rate) if annual_rate is not None else None,
        })
    except Exception as exc:
        traceback.print_exc()
        return JSONResponse({"error": str(exc)}, status_code=500)
