from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

from .exchange import Book


@dataclass(frozen=True)
class Fill:
    quantity: Decimal
    average_price: Decimal
    notional: Decimal
    fee: Decimal
    best_bid: Decimal
    best_ask: Decimal


def round_quantity(quantity, increment):
    increment = Decimal(str(increment))
    if increment <= 0:
        raise ValueError("quantity_increment harus positif")
    units = (Decimal(str(quantity)) / increment).to_integral_value(rounding=ROUND_DOWN)
    return units * increment


def simulate_market_order(
    side,
    quantity,
    book: Book,
    fee_rate,
    minimum_order_idr=25_000,
    minimum_quantity=0,
    quantity_increment=1e-8,
):
    """Walk asks for buys or bids for sells; never fill at mid or last price."""
    if side not in {"buy", "sell"}:
        raise ValueError("Sisi order harus buy atau sell")
    quantity = round_quantity(quantity, quantity_increment)
    if quantity <= 0 or quantity < Decimal(str(minimum_quantity)):
        return None

    levels = book.asks if side == "buy" else book.bids
    if not levels or not book.bids or not book.asks:
        return None

    remaining = quantity
    notional = Decimal(0)
    for price, available in levels:
        if price <= 0 or available <= 0:
            continue
        taken = min(remaining, available)
        notional += taken * price
        remaining -= taken
        if remaining <= 0:
            break
    if remaining > 0 or notional < Decimal(str(minimum_order_idr)):
        return None

    rate = Decimal(str(fee_rate))
    if rate < 0:
        raise ValueError("Fee tidak boleh negatif")
    return Fill(
        quantity=quantity,
        average_price=notional / quantity,
        notional=notional,
        fee=notional * rate,
        best_bid=book.best_bid,
        best_ask=book.best_ask,
    )
