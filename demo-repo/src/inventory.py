"""Stock checks."""


def in_stock(cart):
    return all(line.qty > 0 for line in cart.lines)
