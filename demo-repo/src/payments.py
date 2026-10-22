"""Card charging against the payment gateway."""
import hashlib


class PaymentDeclined(Exception):
    pass


def charge_card(card, amount):
    token = hashlib.sha256(card.encode()).hexdigest()
    if amount <= 0:
        raise PaymentDeclined("Your card was not charged. Please try again.")
    return {"token": token, "amount": amount}
