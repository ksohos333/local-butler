"""Tests for Money Tracker CRUD."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from local_butler.apps.money_tracker import crud, models
from local_butler.core.database import Base
from local_butler.core.schemas import MoneyCategoryCreate, MoneyTransactionCreate


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def test_create_category(db):
    cat = crud.get_or_create_category(
        db, MoneyCategoryCreate(name="Salary", kind="income")
    )
    assert cat.id is not None
    assert cat.name == "Salary"
    assert cat.kind == "income"


def test_create_transaction(db):
    cat = crud.get_or_create_category(
        db, MoneyCategoryCreate(name="Food", kind="expense")
    )
    txn = crud.create_transaction(
        db,
        MoneyTransactionCreate(
            date=date(2026, 9, 20),
            amount=Decimal("12.50"),
            description="Lunch",
            category_id=cat.id,
        ),
    )
    assert txn.id is not None
    assert txn.amount == Decimal("12.50")


def test_summary(db):
    inc = crud.get_or_create_category(db, MoneyCategoryCreate(name="Salary", kind="income"))
    exp = crud.get_or_create_category(db, MoneyCategoryCreate(name="Food", kind="expense"))
    crud.create_transaction(
        db,
        MoneyTransactionCreate(
            date=date(2026, 9, 20),
            amount=Decimal("1000.00"),
            description="Paycheck",
            category_id=inc.id,
        ),
    )
    crud.create_transaction(
        db,
        MoneyTransactionCreate(
            date=date(2026, 9, 20),
            amount=Decimal("50.00"),
            description="Groceries",
            category_id=exp.id,
        ),
    )
    summary = crud.summary(db)
    assert summary.total_income == Decimal("1000.00")
    assert summary.total_expense == Decimal("50.00")
    assert summary.net == Decimal("950.00")
