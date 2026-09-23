"""Response samples copied from btcid/indodax-official-api-docs Public-RestAPI.md."""

SERVER_TIME = {"timezone": "UTC", "server_time": 1571205969552}

PAIRS = [
    {
        "id": "btcidr", "symbol": "BTCIDR", "base_currency": "idr", "traded_currency": "btc",
        "traded_currency_unit": "BTC", "description": "BTC/IDR", "ticker_id": "btc_idr",
        "volume_precision": 0, "price_precision": 1000, "price_round": 8, "pricescale": 1000,
        "quantity_increment": "0.00000001", "trade_min_base_currency": 50000,
        "trade_min_traded_currency": 0.0001, "has_memo": False, "memo_name": False,
    },
    {
        "id": "catidr", "symbol": "CATIDR", "base_currency": "idr", "traded_currency": "cat",
        "traded_currency_unit": "CAT", "description": "CAT/IDR", "ticker_id": "cat_idr",
        "volume_precision": 0, "price_round": 6, "trade_min_base_currency": 10000,
        "is_maintenance": 0, "is_market_suspended": 0, "pricescale": 0.000001,
        "quantity_increment": 1, "price_precision": 0.000001,
        "trade_min_traded_currency": 295787.97917653, "trade_fee_percent": 0.2,
        "trade_fee_percent_taker": 0.2, "trade_fee_percent_maker": 0.1,
    },
]

PRICE_INCREMENTS = {"increments": {"btc_idr": "1000", "ten_idr": "1", "cat_idr": "0.000001"}}

SUMMARIES = {
    "tickers": {
        "btc_idr": {
            "high": "120009000", "low": "116735000", "vol_btc": "218.31103295",
            "vol_idr": "25831203178", "last": "117136000", "buy": "116938000",
            "sell": "117136000", "server_time": 1571206340, "name": "Bitcoin",
        }
    },
    "prices_24h": {"btcidr": "120002000", "tenidr": "521"},
    "prices_7d": {"btcidr": "116001000", "tenidr": "517"},
}

TICKER_TEN = {
    "ticker": {
        "high": "523", "low": "505", "vol_ten": "153588.49847928", "vol_idr": "78884203",
        "last": "511", "buy": "511", "sell": "512", "server_time": 1571207668,
    }
}

TICKER_ALL = {
    "tickers": {
        "btc_idr": {
            "high": "120009000", "low": "116735000", "vol_btc": "218.13777777",
            "vol_idr": "25800033297", "last": "117088000", "buy": "117002000",
            "sell": "117078000", "server_time": 1571207881,
        }
    }
}

TRADES = [
    {"date": "1571207255", "price": "511", "amount": "123.19523759", "tid": "1623490", "type": "sell"},
    {"date": "1571207236", "price": "512", "amount": "121.42187500", "tid": "1623489", "type": "buy"},
]

DEPTH = {
    "buy": [[511, "176.61056751"], [510, "100.00000000"]],
    "sell": [[512, "1591.21213341"], [513, "0.88109162"]],
}

OHLC = [
    {"Time": 1699328700, "Open": 0.9999, "High": 0.9999, "Low": 0.9999, "Close": 0.9999, "Volume": "14814.00000000"},
    {"Time": 1699329600, "Open": 0.9996, "High": 0.9996, "Low": 0.9996, "Close": 0.9996, "Volume": "12359.00000000"},
    {"Time": 1699330500, "Open": 0.9996, "High": 0.9996, "Low": 0.9996, "Close": 0.9996, "Volume": "0"},
]
