"""Market routes for Geonews prediction markets."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from local_butler.apps.geonews.market.crud import (
    DEFAULT_USERNAME,
    debit_wallet,
    ensure_local_user_with_credits,
    get_or_create_user,
)
from local_butler.apps.geonews.market.engine import MarketEngine
from local_butler.apps.geonews.models import Bet, Market
from local_butler.core.database import get_db

router = APIRouter(prefix="/api/markets", tags=["markets"])


class MarketCreate(BaseModel):
    title: str = Field(..., min_length=5, max_length=512)
    description: str | None = None
    close_date: datetime | None = None
    oracle_source: str | None = None
    oracle_query: str | None = None


class MarketOut(BaseModel):
    id: int
    title: str
    description: str | None
    status: str
    yes_price: float
    no_price: float
    liquidity: float
    close_date: datetime | None
    created_at: datetime

    class Config:
        from_attributes = True


class BetCreate(BaseModel):
    side: str = Field(..., pattern="^(yes|no)$")
    shares: float = Field(..., gt=0)


class BetOut(BaseModel):
    id: int
    market_id: int
    side: str
    shares: float
    avg_price: float
    cost_basis: float
    placed_at: datetime

    class Config:
        from_attributes = True


class PortfolioOut(BaseModel):
    balance: int
    positions: list[BetOut]


def _market_out(market: Market) -> MarketOut:
    engine = MarketEngine.from_market(market)
    return MarketOut(
        id=market.id,
        title=market.title,
        description=market.description,
        status=market.status,
        yes_price=engine.state().yes_price,
        no_price=engine.state().no_price,
        liquidity=market.liquidity,
        close_date=market.close_date,
        created_at=market.created_at,
    )


@router.get("", response_model=list[MarketOut])
async def list_markets(
    status: str | None = None,
    db: Session = Depends(get_db),
):
    query = db.query(Market)
    if status:
        query = query.filter(Market.status == status)
    markets = query.order_by(Market.created_at.desc()).all()
    return [_market_out(m) for m in markets]


@router.post("", response_model=MarketOut)
async def create_market(payload: MarketCreate, db: Session = Depends(get_db)):
    market = Market(
        title=payload.title,
        description=payload.description,
        close_date=payload.close_date,
        oracle_source=payload.oracle_source,
        oracle_query=payload.oracle_query,
        status="open",
    )
    db.add(market)
    db.commit()
    db.refresh(market)
    return _market_out(market)


@router.get("/{market_id}", response_model=MarketOut)
async def get_market(market_id: int, db: Session = Depends(get_db)):
    market = db.query(Market).filter(Market.id == market_id).first()
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    return _market_out(market)


@router.post("/{market_id}/bets", response_model=BetOut)
async def place_bet(
    market_id: int,
    payload: BetCreate,
    db: Session = Depends(get_db),
):
    market = db.query(Market).filter(Market.id == market_id).first()
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    if market.status != "open":
        raise HTTPException(status_code=400, detail="Market is not open")

    user, wallet = ensure_local_user_with_credits(db)

    engine = MarketEngine.from_market(market)
    result = engine.buy(payload.side, payload.shares)
    cost_credits = int(round(result["cost"]))

    if wallet.balance < cost_credits:
        raise HTTPException(status_code=400, detail="Insufficient credits")

    # Deduct wallet
    ok = debit_wallet(db, user.id, cost_credits, tx_type="bet", market_id=market.id)
    if not ok:
        raise HTTPException(status_code=400, detail="Insufficient credits")

    bet = Bet(
        market_id=market.id,
        user_id=user.id,
        side=payload.side,
        shares=payload.shares,
        avg_price=result["avg_price"],
        cost_basis=cost_credits,
    )
    db.add(bet)
    db.commit()
    db.refresh(bet)
    return bet


@router.post("/{market_id}/settle")
async def settle_market(
    market_id: int,
    outcome: str,
    db: Session = Depends(get_db),
):
    if outcome not in ("yes", "no"):
        raise HTTPException(status_code=400, detail="outcome must be 'yes' or 'no'")

    market = db.query(Market).filter(Market.id == market_id).first()
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    if market.status != "open":
        raise HTTPException(status_code=400, detail="Market already settled")

    market.status = "settled"
    market.outcome = outcome
    market.resolved_at = datetime.utcnow()

    # Payout winners
    for bet in market.bets:
        if bet.side == outcome:
            payout_credits = int(round(bet.shares))
            from local_butler.apps.geonews.market.crud import credit_wallet
            credit_wallet(
                db,
                bet.user_id,
                payout_credits,
                tx_type="payout",
                market_id=market.id,
                bet_id=bet.id,
            )

    db.commit()
    db.refresh(market)
    return {"settled": True, "outcome": outcome, "market_id": market.id}


@router.get("/portfolio/me", response_model=PortfolioOut)
async def get_portfolio(db: Session = Depends(get_db)):
    user = get_or_create_user(db, DEFAULT_USERNAME)
    wallet = ensure_local_user_with_credits(db)[1]
    positions = (
        db.query(Bet)
        .filter(Bet.user_id == user.id)
        .order_by(Bet.placed_at.desc())
        .all()
    )
    return PortfolioOut(balance=wallet.balance, positions=positions)