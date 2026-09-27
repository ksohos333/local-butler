"""Money Tracker CRUD helpers."""

from datetime import date
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from local_butler.apps.money_tracker import models
from local_butler.core.schemas import (
    MoneyCategoryCreate,
    MoneySummary,
    MoneyTransactionCreate,
)


def get_or_create_category(db: Session, data: MoneyCategoryCreate) -> models.Category:
    existing = (
        db.query(models.Category)
        .filter_by(name=data.name, kind=data.kind)
        .first()
    )
    if existing:
        return existing
    cat = models.Category(name=data.name, kind=data.kind)
    db.add(cat)
    db.commit()
    db.refresh(cat)
    return cat


def create_transaction(
    db: Session, data: MoneyTransactionCreate
) -> models.Transaction:
    txn = models.Transaction(
        date=data.date,
        amount=data.amount,
        description=data.description,
        category_id=data.category_id,
    )
    db.add(txn)
    db.commit()
    db.refresh(txn)
    return txn


def list_transactions(db: Session, limit: int = 200, kind: str | None = None):
    query = db.query(models.Transaction).join(models.Category)
    if kind:
        query = query.filter(models.Category.kind == kind)
    return query.order_by(models.Transaction.date.desc()).limit(limit).all()


def list_categories(db: Session):
    return db.query(models.Category).order_by(models.Category.kind, models.Category.name).all()


def delete_transaction(db: Session, txn_id: int) -> bool:
    txn = db.query(models.Transaction).get(txn_id)
    if not txn:
        return False
    db.delete(txn)
    db.commit()
    return True


def summary(db: Session, start: date | None = None, end: date | None = None) -> MoneySummary:
    query = db.query(
        models.Category.kind,
        func.coalesce(func.sum(models.Transaction.amount), Decimal("0")).label("total"),
    ).join(models.Transaction)
    if start:
        query = query.filter(models.Transaction.date >= start)
    if end:
        query = query.filter(models.Transaction.date <= end)
    rows = query.group_by(models.Category.kind).all()

    totals = {kind: Decimal("0") for kind in ("income", "expense")}
    for kind, total in rows:
        totals[kind] = total or Decimal("0")

    return MoneySummary(
        total_income=totals["income"],
        total_expense=totals["expense"],
        net=totals["income"] - totals["expense"],
    )
