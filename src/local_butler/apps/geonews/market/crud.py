"""Database operations for prediction markets and wallets."""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from local_butler.apps.geonews.models import Bet, Market, Transaction, User, Wallet


DEFAULT_STARTING_BALANCE = 10_000
DEFAULT_USERNAME = "local_user"


def get_or_create_user(db: Session, username: str = DEFAULT_USERNAME) -> User:
    user = db.query(User).filter(User.username == username).first()
    if user:
        return user
    user = User(username=username)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def get_or_create_wallet(db: Session, user_id: int) -> Wallet:
    wallet = db.query(Wallet).filter(Wallet.user_id == user_id).first()
    if wallet:
        return wallet
    wallet = Wallet(user_id=user_id, balance=DEFAULT_STARTING_BALANCE)
    db.add(wallet)
    db.commit()
    db.refresh(wallet)
    return wallet


def ensure_local_user_with_credits(db: Session) -> tuple[User, Wallet]:
    """Get or create the default local user and seed their wallet."""
    user = get_or_create_user(db)
    wallet = get_or_create_wallet(db, user.id)
    return user, wallet


def credit_wallet(db: Session, user_id: int, amount: int, tx_type: str,
                  market_id: int | None = None, bet_id: int | None = None) -> None:
    """Add credits to a wallet and record the transaction."""
    wallet = get_or_create_wallet(db, user_id)
    wallet.balance += amount
    wallet.updated_at = datetime.now(timezone.utc)
    db.add(wallet)

    tx = Transaction(
        user_id=user_id,
        amount=amount,
        type=tx_type,
        market_id=market_id,
        bet_id=bet_id,
    )
    db.add(tx)
    db.commit()


def debit_wallet(db: Session, user_id: int, amount: int, tx_type: str,
                 market_id: int | None = None, bet_id: int | None = None) -> bool:
    """Deduct credits from a wallet if sufficient balance exists."""
    wallet = get_or_create_wallet(db, user_id)
    if wallet.balance < amount:
        return False

    wallet.balance -= amount
    wallet.updated_at = datetime.now(timezone.utc)
    db.add(wallet)

    tx = Transaction(
        user_id=user_id,
        amount=-amount,
        type=tx_type,
        market_id=market_id,
        bet_id=bet_id,
    )
    db.add(tx)
    db.commit()
    return True
