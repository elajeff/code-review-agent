def withdraw(balance: int, amount: int) -> int:
    if amount > balance:
        raise ValueError("Insufficient funds")
    return balance - amount
