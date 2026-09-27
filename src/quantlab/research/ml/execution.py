"""Pre-submission admission using ONLY completed decision-session information.

No sale proceeds/position slots are promised to another order in the same close
auction. Filled orders are final; price gaps may cause reported risk breaches.
"""

from dataclasses import replace


def admit_orders(orders, book, marks, industries, config):
    amounts = {}
    for lot in book.lots:
        amounts[lot.instrument_id] = (
            amounts.get(lot.instrument_id, 0) + lot.quantity * marks[lot.instrument_id]
        )
    equity = book.cash_fen + sum(amounts.values())
    cash = book.cash_fen
    accepted, deferred = [], []
    for order in orders:
        if order.side == "sell":
            accepted.append(order)
            continue
        code = order.instrument_id
        reason = None
        if code not in amounts and len(amounts) >= config.max_positions:
            reason = "no_slot_without_assumed_sale"
            quantity = 0
        else:
            sector = sum(v for k, v in amounts.items() if industries[k] == industries[code])
            room = min(
                cash,
                max(0, config.gross_exposure * equity - sum(amounts.values())),
                max(0, config.max_weight * equity - amounts.get(code, 0)),
                max(0, config.max_industry_weight * equity - sector),
            )
            quantity = min(order.desired_quantity, int(room // marks[code]))
            if quantity < order.desired_quantity:
                reason = "decision_cash_or_exposure_budget"
        if quantity:
            accepted.append(replace(order, desired_quantity=quantity))
            value = quantity * marks[code]
            amounts[code] = amounts.get(code, 0) + value
            cash -= value
        if reason:
            deferred.append(
                {
                    "order_id": order.order_id,
                    "instrument_id": code,
                    "requested": order.desired_quantity,
                    "admitted": quantity,
                    "reason": reason,
                }
            )
    return tuple(accepted), deferred


def exposure_report(book, marks, industries, config, receivable_fen=0):
    amounts = {}
    for lot in book.lots:
        amounts[lot.instrument_id] = (
            amounts.get(lot.instrument_id, 0) + lot.quantity * marks[lot.instrument_id]
        )
    nav = book.cash_fen + sum(amounts.values()) + receivable_fen
    names = {k: v / nav for k, v in amounts.items()} if nav > 0 else {}
    sectors = {}
    for code, weight in names.items():
        sector = industries[code]
        sectors[sector] = sectors.get(sector, 0) + weight
    tolerance = config.execution_weight_tolerance
    breaches = []
    if len(names) > config.max_positions:
        breaches.append("position_count")
    if sum(names.values()) > config.gross_exposure + tolerance:
        breaches.append("gross_exposure")
    breaches.extend(f"name:{k}" for k, w in names.items() if w > config.max_weight + tolerance)
    breaches.extend(
        f"industry:{k}" for k, w in sectors.items() if w > config.max_industry_weight + tolerance
    )
    return {
        "name_weights": names,
        "industry_weights": sectors,
        "gross_exposure": sum(names.values()),
        "cash_weight": book.cash_fen / nav if nav > 0 else None,
        "breaches": breaches,
    }
