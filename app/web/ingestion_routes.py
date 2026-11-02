from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from urllib.parse import urlsplit
from app.database import get_session
from app import ingestion
from app.models import IngestionBatch


def check_origin(request):
    origin = request.headers.get('origin')
    if origin and urlsplit(origin).netloc != request.headers.get('host'):
        raise HTTPException(403, 'Cross-origin imports are not allowed')


def stage_upload(db, user_id, platform, source, content, filename, form):
    from fastapi.responses import JSONResponse
    try:
        batch = ingestion.stage(db, user_id, platform.id, source, content, filename,
            effective_date=form.get('effective_date'), history_complete=form.get('history_complete') == 'true',
            coverage_start=form.get('coverage_start'), coverage_end=form.get('coverage_end'), pie_name=form.get('pie_name'))
        return JSONResponse(ingestion.describe(batch))
    except (ValueError, LookupError) as exc:
        db.rollback()
        return JSONResponse({'error': str(exc)}, status_code=400)


class ApplyInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    batch_id: int = Field(gt=0)
    reviewed: bool


class MatchInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    ticker: str = Field(min_length=1, max_length=120)


class HoldingInput(MatchInput):
    platform_id: int = Field(gt=0)


class HoldingReviewInput(HoldingInput):
    isin: str = Field(min_length=1, max_length=12)
    currency: str = Field(min_length=1, max_length=10)
    mic: str = Field(default='', max_length=4)
    symbol: str = Field(default='', max_length=40)
    statement_reference: str = Field(min_length=1, max_length=300)
    confirmed: bool


class HoldingApplyInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    review_token: str = Field(min_length=1, max_length=16000)
    reviewed: bool


def build_ingestion_router(require_user):
    router = APIRouter(prefix='/imports')

    def user_for(request, db):
        user = require_user(request, db)
        if isinstance(user, RedirectResponse):
            raise HTTPException(401, 'Sign in to view imports')
        check_origin(request)
        return user

    @router.get('')
    def history(request: Request, offset: int = 0, db: Session = Depends(get_session)):
        user = user_for(request, db)
        rows = db.query(IngestionBatch).filter_by(user_id=user.id).order_by(IngestionBatch.id.desc()).offset(max(offset, 0)).limit(20).all()
        return {'items': [{key: value for key, value in ingestion.describe(row).items() if key not in {'before', 'proposed'}} for row in rows]}

    @router.get('/{batch_id}')
    def review(batch_id: int, request: Request, db: Session = Depends(get_session)):
        user = user_for(request, db)
        batch = db.query(IngestionBatch).filter_by(id=batch_id, user_id=user.id).first()
        if batch is None:
            raise HTTPException(404, 'Import not found')
        return ingestion.describe(batch)

    @router.post('/apply')
    def apply(payload: ApplyInput, request: Request, db: Session = Depends(get_session)):
        user = user_for(request, db)
        if not payload.reviewed:
            raise HTTPException(400, 'Review the source and holding differences before applying')
        try:
            return ingestion.describe(ingestion.apply(db, user.id, payload.batch_id))
        except LookupError as exc:
            db.rollback()
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            db.rollback()
            raise HTTPException(409, str(exc)) from exc

    @router.get('/{batch_id}/matching')
    def matching(batch_id: int, request: Request, db: Session = Depends(get_session)):
        user = user_for(request, db)
        from app.import_matching import batch_for, matching_tickers
        try:
            batch = batch_for(db, user.id, batch_id)
            return {'tickers': matching_tickers(db, batch)}
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post('/{batch_id}/matching')
    def match(batch_id: int, payload: MatchInput, request: Request, db: Session = Depends(get_session)):
        user = user_for(request, db)
        from app.import_matching import match_one
        try:
            result = match_one(db, user.id, batch_id, payload.ticker)
            if result['status'] in {'matched', 'reused'}:
                from app.account_pricing import refresh_import_price
                try:
                    result['pricing'] = refresh_import_price(db, user.id, batch_id, payload.ticker)
                except Exception:
                    db.rollback()
                    result['pricing'] = {'status': 'unavailable', 'reason': 'Match saved; price refresh failed. Retry refreshing prices.'}
            return result
        except LookupError as exc:
            db.rollback()
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            db.rollback()
            raise HTTPException(409, str(exc)) from exc

    @router.post('/pricing/refresh')
    def refresh_prices(request: Request, db: Session = Depends(get_session)):
        user = user_for(request, db)
        from app.account_pricing import refresh_due_prices
        return refresh_due_prices(db, user.id)

    @router.get('/pricing/work')
    def pricing_work(request: Request, db: Session = Depends(get_session)):
        user = user_for(request, db)
        from sqlalchemy import text
        from app.valuation import effective_price_sql, account_price_scope_sql
        scope = account_price_scope_sql('p.ticker', 'p.user_id', 'p.platform_id')
        rows = db.execute(text(f'''SELECT p.platform_id,p.ticker FROM positions p
            JOIN platforms pl ON pl.id=p.platform_id AND pl.user_id=p.user_id
            WHERE p.user_id=:u AND p.quantity>0 AND pl.is_active
            AND (({effective_price_sql()}) IS NULL OR EXISTS(SELECT 1 {scope}))
            ORDER BY p.platform_id,p.ticker'''), {'u': user.id}).mappings().all()
        return {'items': [dict(row) for row in rows]}

    @router.get('/attention/list')
    def attention(request: Request, search: str = '', offset: int = 0, db: Session = Depends(get_session)):
        user = user_for(request, db)
        from app.holding_attention import attention_list
        return attention_list(db, user.id, search, offset)

    @router.post('/attention/verify')
    def verify_holding(payload: HoldingReviewInput, request: Request, db: Session = Depends(get_session)):
        user = user_for(request, db)
        if not payload.confirmed:
            raise HTTPException(400, 'Confirm these identity details against your statement before verification')
        from app.holding_attention import verify
        try:
            return verify(db, user.id, **payload.model_dump(exclude={'confirmed'}))
        except LookupError as exc:
            db.rollback()
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            db.rollback()
            raise HTTPException(409, str(exc)) from exc

    @router.post('/attention/apply')
    def apply_holding(payload: HoldingApplyInput, request: Request, db: Session = Depends(get_session)):
        user = user_for(request, db)
        if not payload.reviewed:
            raise HTTPException(400, 'Review the verified listing before applying')
        from app.holding_attention import apply
        try:
            return apply(db, user.id, payload.review_token)
        except LookupError as exc:
            db.rollback()
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            db.rollback()
            raise HTTPException(409, str(exc)) from exc

    @router.post('/attention/refresh')
    def refresh_holding(payload: HoldingInput, request: Request, db: Session = Depends(get_session)):
        user = user_for(request, db)
        from app.holding_attention import refresh
        try:
            return refresh(db, user.id, **payload.model_dump())
        except LookupError as exc:
            db.rollback()
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            db.rollback()
            raise HTTPException(409, str(exc)) from exc

    @router.post('/attention/auto')
    def auto_holding(payload: HoldingInput, request: Request, db: Session = Depends(get_session)):
        user = user_for(request, db)
        from app.automatic_matching import match_holding, CachedLookup
        from app.holding_attention import refresh
        try:
            lookup = CachedLookup()
            result = match_holding(db, user.id, payload.platform_id, payload.ticker, lookup)
            if result['status'] in {'matched', 'reused'}:
                # The match is committed independently; a provider failure must
                # leave it saved and report only price availability.
                result['pricing'] = refresh(db, user.id, payload.platform_id, payload.ticker, lookup=lookup)
            return result
        except LookupError as exc:
            db.rollback()
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            db.rollback()
            raise HTTPException(409, str(exc)) from exc
    return router
