"""McTrader_Market_Data_Service — the market data micro-service.

Four data endpoints (GET /price/{symbol}, POST /prices, POST /atr, POST /bars) over one
vendor key, one price cache, one daily bar cache and one ATR definition.

See README.md for scope, and atr.py for why ATR means gap-inclusive true range here.
"""

__version__ = "0.1.0"
