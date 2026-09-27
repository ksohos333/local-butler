"""Money Tracker FastAPI application."""

from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from local_butler.apps.money_tracker import crud, models
from local_butler.core.database import Base, engine, get_db
from local_butler.core.schemas import MoneyCategoryCreate, MoneyTransactionCreate

# Create tables
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Money Tracker")

# Templates
TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def render_index(request: Request, db: Session):
    summary = crud.summary(db)
    transactions = crud.list_transactions(db)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "summary": summary,
            "transactions": transactions,
            "today": date.today().isoformat(),
        },
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    return render_index(request, db)


@app.post("/transactions", response_class=HTMLResponse)
def create_transaction(
    request: Request,
    date: str = Form(...),
    amount: str = Form(...),
    kind: str = Form(...),
    category: str = Form(...),
    description: str = Form(default=""),
    db: Session = Depends(get_db),
):
    from datetime import datetime as dt

    try:
        parsed_date = dt.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date")

    try:
        parsed_amount = round(float(amount), 2)
        if parsed_amount <= 0:
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=400, detail="Amount must be a positive number")

    if kind not in ("income", "expense"):
        raise HTTPException(status_code=400, detail="kind must be income or expense")

    cat = crud.get_or_create_category(
        db, MoneyCategoryCreate(name=category.strip(), kind=kind)
    )

    txn = MoneyTransactionCreate(
        date=parsed_date,
        amount=parsed_amount,
        description=description.strip(),
        category_id=cat.id,
    )
    crud.create_transaction(db, txn)
    return render_index(request, db)


@app.post("/transactions/{txn_id}/delete", response_class=HTMLResponse)
def delete_transaction(
    request: Request,
    txn_id: int,
    db: Session = Depends(get_db),
):
    if not crud.delete_transaction(db, txn_id):
        raise HTTPException(status_code=404, detail="Transaction not found")
    return render_index(request, db)
