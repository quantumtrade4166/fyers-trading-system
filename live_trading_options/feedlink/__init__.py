"""feedlink - price-feed adapters between brokers (25-Nov-2026).

kotak_ticker.KotakTicker  KiteTicker drop-in that streams from Kotak Neo (Delta Neutral, Nifty Pivot).
kite_assist.KiteAssist    Kite prices (through the local tick bus) as a 2nd source for Kotak-keyed engines
                          (CE/PE + ALGO engines, MCX / NSE desks, order gateways).
kite_master               Kite's public instrument list <-> (segment, name, expiry, strike, kind).
"""
