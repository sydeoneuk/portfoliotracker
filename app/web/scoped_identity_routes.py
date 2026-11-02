from urllib.parse import urlsplit
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from app.database import get_session
from app import scoped_identity as service
from app.models import AccountListingAudit


class VerifyInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    platform_id: int = Field(gt=0)
    namespace: str = Field(min_length=1, max_length=120)
    ticker: str = Field(min_length=1, max_length=120)
    symbol: str = Field(min_length=1, max_length=40)
    isin: str = Field(min_length=12, max_length=12)
    currency: str = Field(min_length=3, max_length=3)
    mic: str | None = Field(default=None, max_length=4)
    statement_reference: str = Field(min_length=1, max_length=240)


class ApplyInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    review_token: str = Field(min_length=1, max_length=40000)


class BatchInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    review_tokens: list[str] = Field(min_length=1, max_length=20)
    reviewed: bool


class UndoInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    audit_id: int = Field(gt=0)


def build_scoped_identity_router(require_user, is_admin):
    router = APIRouter(prefix='/admin/account-identity')

    def actor(request, db):
        user = require_user(request, db)
        if isinstance(user, RedirectResponse):
            raise HTTPException(401, 'Sign in to review account identity')
        if not is_admin(user):
            raise HTTPException(403, 'Administrator access required')
        origin = request.headers.get('origin')
        if request.method != 'GET' and origin and urlsplit(origin).netloc != request.headers.get('host'):
            raise HTTPException(403, 'Cross-origin changes are not allowed')
        return user

    def run(db, action):
        try:
            return action()
        except LookupError as exc:
            db.rollback()
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            db.rollback()
            raise HTTPException(409, str(exc)) from exc
        except Exception:
            db.rollback()
            raise

    @router.get('/audit')
    def audit(request: Request, search: str = '', offset: int = 0, held_only: bool = True, db: Session = Depends(get_session)):
        actor(request, db)
        return service.audit_report(db, search, offset, held_only)

    @router.post('/verify')
    def verify(payload: VerifyInput, request: Request, db: Session = Depends(get_session)):
        user = actor(request, db)
        return run(db, lambda: service.verify(db, user.id, payload.platform_id, payload.namespace,
            payload.ticker, payload.symbol, payload.isin, payload.currency, payload.mic, payload.statement_reference))

    @router.post('/apply')
    def apply(payload: ApplyInput, request: Request, db: Session = Depends(get_session)):
        user = actor(request, db)
        return run(db, lambda: service.apply(db, user.id, payload.review_token))

    @router.post('/apply-batch')
    def batch(payload: BatchInput, request: Request, db: Session = Depends(get_session)):
        user = actor(request, db)
        if not payload.reviewed:
            raise HTTPException(400, 'Review each account/source statement before applying a batch')
        if any(not token or len(token) > 40000 for token in payload.review_tokens):
            raise HTTPException(400, 'Invalid review token size')
        return run(db, lambda: service.apply_batch(db, user.id, payload.review_tokens))

    @router.post('/undo')
    def undo(payload: UndoInput, request: Request, db: Session = Depends(get_session)):
        user = actor(request, db)
        return run(db, lambda: service.undo(db, user.id, payload.audit_id))

    @router.get('/history')
    def history(request: Request, db: Session = Depends(get_session)):
        actor(request, db)
        rows = db.query(AccountListingAudit).order_by(AccountListingAudit.id.desc()).limit(50).all()
        items = []
        for row in rows:
            current = service.mapping(db, row.platform_id, row.source_namespace, row.ticker)
            can_undo = False
            if row.action == 'apply' and current and service.snapshot(current) == row.after:
                try:
                    ctx = service.context(db, row.platform_id, row.source_namespace, row.ticker)
                    can_undo = ctx['fingerprint'] == current.source_fingerprint and not db.query(AccountListingAudit.id).filter_by(undo_of=row.id).first()
                except LookupError:
                    pass
            items.append({'id': row.id, 'platform_id': row.platform_id, 'namespace': row.source_namespace,
                'ticker': row.ticker, 'actor_id': row.actor_id, 'owner_id': row.user_id, 'action': row.action,
                'before': row.before, 'after': row.after, 'created_at': row.created_at.isoformat(), 'can_undo': bool(can_undo)})
        return {'items': items}
    return router
