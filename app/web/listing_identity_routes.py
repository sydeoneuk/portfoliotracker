from urllib.parse import urlsplit
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from app.database import get_session
from app import listing_identity as service
from app.models import ListingIdentityAudit, InstrumentListingLink, Instrument


class VerifyInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    ticker: str = Field(min_length=1, max_length=120)
    symbol: str = Field(min_length=1, max_length=40)


class ApplyInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    review_token: str = Field(min_length=1, max_length=40000)


class UndoInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    audit_id: int = Field(gt=0)


def build_listing_identity_router(require_user, is_admin):
    router = APIRouter(prefix='/admin/listing-identity')

    def actor(request, db):
        user = require_user(request, db)
        if isinstance(user, RedirectResponse):
            raise HTTPException(401, 'Sign in to administer listing identity')
        if not is_admin(user):
            raise HTTPException(403, 'Administrator access required')
        origin = request.headers.get('origin')
        if request.method != 'GET' and origin and urlsplit(origin).netloc != request.headers.get('host'):
            raise HTTPException(403, 'Cross-origin catalogue changes are not allowed')
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
    def audit(request: Request, search: str = '', offset: int = 0, db: Session = Depends(get_session)):
        actor(request, db)
        return service.audit_report(db, search, offset)

    @router.post('/verify')
    def verify(payload: VerifyInput, request: Request, db: Session = Depends(get_session)):
        user = actor(request, db)
        return run(db, lambda: service.verify(db, user.id, payload.ticker, payload.symbol.strip()))

    @router.post('/apply')
    def apply(payload: ApplyInput, request: Request, db: Session = Depends(get_session)):
        user = actor(request, db)
        return run(db, lambda: service.apply(db, user.id, payload.review_token))

    @router.post('/undo')
    def undo(payload: UndoInput, request: Request, db: Session = Depends(get_session)):
        user = actor(request, db)
        return run(db, lambda: service.undo(db, user.id, payload.audit_id))

    @router.get('/history')
    def history(request: Request, db: Session = Depends(get_session)):
        actor(request, db)
        rows = db.query(ListingIdentityAudit).order_by(ListingIdentityAudit.id.desc()).limit(50).all()
        items = []
        for row in rows:
            link = db.get(InstrumentListingLink, row.ticker)
            instrument = db.get(Instrument, row.ticker)
            undone = db.query(ListingIdentityAudit.id).filter_by(undo_of=row.id).first()
            can_undo = row.action == 'link' and not undone and instrument is not None and link is not None and (
                service.snapshot(link) == row.after and service.fingerprint(instrument) == link.source_fingerprint)
            items.append({'id': row.id, 'ticker': row.ticker, 'actor_id': row.user_id, 'action': row.action,
                'before': row.before, 'after': row.after, 'created_at': row.created_at.isoformat(), 'can_undo': bool(can_undo)})
        return {'items': items}
    return router
