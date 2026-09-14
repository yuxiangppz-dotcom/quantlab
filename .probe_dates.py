from quantlab.research.s4_replay_admission import necessary_field_gaps

BASE = {
    "instrument_id": "000301.SZ",
    "execution_date": "2022-01-04",
    "next_session": "2022-01-05",
    "evidence_date": "2022-01-04",
    "calendar_verified": True,
    "market_open": None,
    "corporate_actions_processed": True,
    "raw_close_fen": 1000,
    "low_fen": 900,
    "high_fen": 1100,
    "down_limit_fen": 900,
    "up_limit_fen": 1100,
    "prior20_amount_fen": 10**12,
    "prior20_asof": "2021-12-31",
    "prior20_sessions": 20,
    "session_amount_fen": 10**12,
    "session_volume_shares": 1000,
    "participation": "0.05",
    "rules": {
        "scenario_id": "r",
        "effective_from": None,
        "effective_through": None,
        "buy_minimum": 100,
        "buy_increment": 100,
        "sell_minimum": 100,
        "sell_increment": 100,
        "max_order_quantity": 100000,
        "full_position_odd_exit": True,
    },
    "fees": {
        "scenario_id": "f",
        "effective_from": None,
        "effective_through": None,
        "commission_rate": "0.000086",
        "minimum_commission_fen": 500,
        "buy_stamp_rate": "0",
        "sell_stamp_rate": "0.001",
        "additional_fee_rate": None,
        "additional_fee_fixed_fen": None,
        "adverse_slippage_rate": "0.0005",
    },
}

gaps = necessary_field_gaps(BASE)
for g in gaps:
    if "effective" in g:
        print("GAP:", g)
