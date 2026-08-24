"""ICICI Breeze API — options historical data pipeline.

Separate from the Fyers options pipeline in `options/`. Breeze is used because it
serves 1-second candles and (unlike the current front-week-only dataset) lets us
request any expiry by date.
"""
