import datetime
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_session
from app import reconciliation as service
from app.models.instrument_mapping import InstrumentMappingProposal, InstrumentMappingAudit
from app.valuation import effective_price_sql


class ProposalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: str = Field(min_length=1, max_length=120)
    symbol: str | None = Field(default=None, max_length=40)
    allow_currency_conversion: bool = False


class ApplyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_id: int = Field(gt=0)


class MetadataInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    ticker: str = Field(min_length=1, max_length=120)
    symbol: str = Field(min_length=1, max_length=40)
    isin: str = Field(min_length=12, max_length=12)
    currency: str = Field(min_length=3, max_length=3)
    statement_reference: str = Field(min_length=1, max_length=240)


class UndoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    audit_id: int = Field(gt=0)


class RefreshInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mapping_id: int = Field(gt=0)


def proposal_json(proposal):
    expired = proposal.expires_at < datetime.datetime.utcnow()
    return dict(id=proposal.id, status="expired" if expired and proposal.status == "verified" else proposal.status,
                candidate=proposal.candidate, baseline=proposal.baseline,
                expires_at=proposal.expires_at.isoformat(), can_apply=proposal.status == "verified" and not expired)


def build_reconciliation_router(require_user, is_admin):
    router = APIRouter(prefix="/settings/reconciliation")
    admin_router = APIRouter()

    def require_metadata_admin(user):
        if not is_admin(user):
            raise HTTPException(403, 'Shared instrument metadata corrections require administrator access')

    def user_for(request, db):
        user = require_user(request, db)
        if isinstance(user, RedirectResponse):
            raise HTTPException(401, "Sign in to reconcile your holdings")
        origin = request.headers.get("origin")
        if request.method != "GET" and origin and urlsplit(origin).netloc != request.headers.get("host"):
            raise HTTPException(403, "Cross-origin changes are not allowed")
        return user

    def run(db, action):
        try:
            return action()
        except LookupError as exc:
            db.rollback()
            raise HTTPException(404, str(exc)) from exc
        except service.ReconciliationConflict as exc:
            db.rollback()
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            db.rollback()
            raise HTTPException(400, str(exc)) from exc

    @router.get("")
    def list_holdings(request: Request, search: str = "", missing_only: bool = True,
                      offset: int = 0, limit: int = 10, db: Session = Depends(get_session)):
        user = user_for(request, db)
        params = dict(user_id=user.id, search="%" + search[:80] + "%", missing=missing_only,
                      limit=max(1, min(limit, 25)), offset=max(0, offset))
        held_sql = f"""WITH held AS (
            SELECT i.ticker, i.name, i.isin, i.currency_code, i.yf_ticker, i.exchange, i.mic_code,
                SUM(p.quantity) AS quantity,
                COALESCE(BOOL_AND(({effective_price_sql()}) IS NOT NULL AND i.currency_code IS NOT NULL), false) AS quote_available,
                COALESCE(BOOL_AND(p.cost_basis_amount IS NOT NULL AND p.cost_basis_currency IS NOT NULL
                         AND p.cost_basis_status IN ('reported','estimated')), false) AS costs_available
            FROM positions p JOIN instruments i ON i.ticker=p.ticker
            WHERE p.user_id=:user_id AND p.quantity > 0
              AND (i.ticker ILIKE :search OR i.name ILIKE :search OR i.isin ILIKE :search)
            GROUP BY i.ticker
        ) """
        total = db.execute(text(held_sql + "SELECT COUNT(*) FROM held WHERE NOT :missing OR NOT quote_available"), params).scalar_one()
        rows = db.execute(text(held_sql + "SELECT * FROM held WHERE NOT :missing OR NOT quote_available ORDER BY ticker LIMIT :limit OFFSET :offset"), params).mappings().all()
        items = []
        for row in rows:
            mapping = service.mapping_for(db, user.id, row["ticker"])
            proposals = db.query(InstrumentMappingProposal).filter(
                InstrumentMappingProposal.user_id == user.id, InstrumentMappingProposal.ticker == row["ticker"],
                InstrumentMappingProposal.status != "superseded",
            ).order_by(InstrumentMappingProposal.id.desc()).limit(4).all()
            latest = db.query(InstrumentMappingAudit).filter_by(user_id=user.id, ticker=row["ticker"]).order_by(InstrumentMappingAudit.id.desc()).first()
            items.append({**dict(row), "quantity": float(row["quantity"]),
                          "mapping": {"id": mapping.id, **service.snapshot(mapping)} if mapping else None,
                          "proposals": [proposal_json(p) for p in proposals],
                          "undo_audit_id": latest.id if latest and latest.action == "apply" and mapping and latest.after["revision"] == mapping.revision else None})
        return dict(items=items, total=total, offset=params["offset"], limit=params["limit"])

    @router.post("/propose")
    def propose(request: Request, payload: ProposalInput, db: Session = Depends(get_session)):
        user = user_for(request, db)
        proposals = run(db, lambda: service.propose(db, user.id, payload.ticker,
                        symbol=payload.symbol.strip() if payload.symbol else None,
                        allow_currency_conversion=payload.allow_currency_conversion))
        return {"proposals": [proposal_json(p) for p in proposals]}

    @admin_router.post('/admin/reconciliation/metadata/propose')
    def metadata_propose(request: Request, payload: MetadataInput, db: Session = Depends(get_session)):
        user = user_for(request, db)
        require_metadata_admin(user)
        proposals = run(db, lambda: service.propose_metadata(db, user.id, payload.ticker,
            payload.symbol, payload.isin, payload.currency, payload.statement_reference))
        return {'proposals': [proposal_json(p) for p in proposals]}

    @router.post("/apply")
    def apply(request: Request, payload: ApplyInput, db: Session = Depends(get_session)):
        user = user_for(request, db)
        proposal = db.query(InstrumentMappingProposal).filter_by(id=payload.proposal_id, user_id=user.id).first()
        if proposal and proposal.candidate.get('metadata_correction'):
            require_metadata_admin(user)
        mapping, audit = run(db, lambda: service.apply_proposal(db, user.id, payload.proposal_id))
        return dict(mapping_id=mapping.id, audit_id=audit.id, message="Verified price mapping applied to your holdings")

    @router.post("/undo")
    def undo(request: Request, payload: UndoInput, db: Session = Depends(get_session)):
        user = user_for(request, db)
        audit = db.query(InstrumentMappingAudit).filter_by(id=payload.audit_id, user_id=user.id).first()
        if audit and audit.after and audit.after.get('metadata_correction'):
            require_metadata_admin(user)
        audit = run(db, lambda: service.undo_change(db, user.id, payload.audit_id))
        return dict(audit_id=audit.id, message="Previous price mapping restored")

    @router.post("/refresh")
    def refresh(request: Request, payload: RefreshInput, db: Session = Depends(get_session)):
        user = user_for(request, db)
        try:
            mapping = run(db, lambda: service.refresh_mapping(db, user.id, payload.mapping_id))
        except HTTPException:
            raise
        except Exception as exc:
            db.rollback()
            raise HTTPException(502, "Market data provider unavailable; previous mapping and quote retained") from exc
        return dict(mapping_id=mapping.id, message="Verified quote refreshed", checked_at=mapping.checked_at.isoformat())

    @router.get("/history")
    def history(request: Request, db: Session = Depends(get_session)):
        user = user_for(request, db)
        rows = db.query(InstrumentMappingAudit).filter_by(user_id=user.id).order_by(InstrumentMappingAudit.id.desc()).limit(50).all()
        items = []
        for row in rows:
            mapping = service.mapping_for(db, user.id, row.ticker)
            undone = db.query(InstrumentMappingAudit.id).filter_by(user_id=user.id, undo_of=row.id).first()
            can_undo = row.action == "apply" and mapping is not None and not undone and row.after["revision"] == mapping.revision
            items.append(dict(id=row.id, ticker=row.ticker, action=row.action, before=row.before,
                              after=row.after, undo_of=row.undo_of, created_at=row.created_at.isoformat(), can_undo=bool(can_undo)))
        return {"items": items}

    router.routes.extend(admin_router.routes)
    return router
