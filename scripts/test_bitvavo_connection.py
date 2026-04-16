"""
scripts/test_bitvavo_connection.py

Handmatige connectietest voor de CryptoAdapter (Bitvavo).

Leest BITVAVO_API_KEY en BITVAVO_API_SECRET uit omgevingsvariabelen.
paper_only=True — er worden geen orders geplaatst.

Gebruik:
    set BITVAVO_API_KEY=jouw_key
    set BITVAVO_API_SECRET=jouw_secret
    python scripts/test_bitvavo_connection.py
"""

import os
import sys

from ant_colony.biome.crypto_adapter import CryptoAdapter


def main() -> None:
    api_key = os.environ.get("BITVAVO_API_KEY", "")
    api_secret = os.environ.get("BITVAVO_API_SECRET", "")

    if not api_key or not api_secret:
        print("FOUT: BITVAVO_API_KEY en BITVAVO_API_SECRET zijn vereist.")
        print("  set BITVAVO_API_KEY=jouw_key")
        print("  set BITVAVO_API_SECRET=jouw_secret")
        sys.exit(1)

    adapter = CryptoAdapter(api_key=api_key, api_secret=api_secret, paper_only=True)
    print(f"CryptoAdapter aangemaakt  biome_id={adapter.biome_id!r}  paper_only=True\n")

    # --- is_available ---
    available = adapter.is_available()
    status = "OK" if available else "FOUT"
    print(f"[{status}] is_available() -> {available}")

    if not available:
        print("\nBitvavo niet bereikbaar. Controleer API credentials en netwerk.")
        sys.exit(1)

    # --- get_market_data ---
    print()
    data = adapter.get_market_data("BTC-EUR", "1h")
    if data is None:
        print("[FOUT] get_market_data('BTC-EUR', '1h') -> None")
    else:
        print(f"[OK]   get_market_data('BTC-EUR', '1h')")
        print(f"       symbol    : {data.symbol}")
        print(f"       timeframe : {data.timeframe}")
        print(f"       timestamp : {data.timestamp.isoformat()}")
        print(f"       open      : {data.open:.2f}")
        print(f"       high      : {data.high:.2f}")
        print(f"       low       : {data.low:.2f}")
        print(f"       close     : {data.close:.2f}")
        print(f"       volume    : {data.volume:.4f}")
        print(f"       stale     : {data.is_stale()}")

    # --- get_account_state ---
    print()
    state = adapter.get_account_state()
    if state is None:
        print("[FOUT] get_account_state() -> None")
    else:
        print(f"[OK]   get_account_state()")
        print(f"       biome_id        : {state.biome_id}")
        print(f"       currency        : {state.currency}")
        print(f"       balance         : {state.balance:.2f} EUR")
        print(f"       positions_value : {state.positions_value:.2f} EUR  (in open orders)")
        print(f"       equity          : {state.equity:.2f} EUR")
        print(f"       timestamp       : {state.timestamp.isoformat()}")
        print(f"       stale           : {state.is_stale()}")

    print("\nKlaar.")


if __name__ == "__main__":
    main()
