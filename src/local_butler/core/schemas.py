"""Shared Pydantic schemas for Local Butler apps."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class MoneyCategoryBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    kind: str = Field(..., pattern="^(income|expense)$")


class MoneyCategoryCreate(MoneyCategoryBase):
    pass


class MoneyCategory(MoneyCategoryBase):
    model_config = ConfigDict(from_attributes=True)
    id: int


class MoneyTransactionBase(BaseModel):
    date: date
    amount: Decimal = Field(..., gt=0, decimal_places=2)
    description: str = Field(default="", max_length=255)
    category_id: int


class MoneyTransactionCreate(MoneyTransactionBase):
    pass


class MoneyTransaction(MoneyTransactionBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime
    category: MoneyCategory | None = None


class MoneySummary(BaseModel):
    total_income: Decimal
    total_expense: Decimal
    net: Decimal
