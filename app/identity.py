"""Provider namespaces are shared by delivery methods, scoped by account."""
T212 = "t212"
II = "ii"
T212_PIE_CSV = "t212_csv_pie"


class UnresolvedIdentityError(ValueError):
    pass


def require_resolved_identity(db, model, user_id, key, value, platform_id=None):
    """Do not create a second copy of a legacy event with unknown account."""
    from sqlalchemy import or_, and_
    unresolved = model.platform_id.is_(None)
    if platform_id is not None:
        unresolved = or_(unresolved, and_(model.platform_id == platform_id, model.source_namespace == "legacy"))
    row = db.query(model).filter(
        model.user_id == user_id, getattr(model, key) == value,
        unresolved,
    ).first()
    if row is not None:
        raise UnresolvedIdentityError("Matching legacy identifier has unresolved account or source; reconcile it before importing or syncing")
