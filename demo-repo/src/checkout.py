"""Checkout flow for the Northwind storefront."""
from .payments import charge_card
from . import inventory

TOTAL_LABEL = "Order total"
SUBTOTAL_LABEL = "Subtotal"


def finalise(cart, card):
    if not inventory.in_stock(cart):
        raise ValueError("Item is no longer available")
    try:
        charge_card(card, cart.total)
    except PaymentDeclined:
        raise RuntimeError("Coudn't complete yor order")
