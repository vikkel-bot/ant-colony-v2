"""
scripts/test_bitvavo.py

Smoke test voor BitvavoAdapter — alleen lezen, geen orders.

Zet BITVAVO_PAPER_MODE=true voor aanmaak van de adapter zodat
place_order() altijd simuleert. Leest API keys uit environment.

Gebruik:
    set BITVAVO_API_KEY=jouw_key
    set BITVAVO_API_SECRET=jouw_secret
    python scripts/test_bitvavo.py
"""

from __future__ import annotations

import os
import sys

# BITVAVO_PAPER_MODE=true garandeert dat geen enkel order de exchange bereikt,
# ook als dit script per ongeluk wordt uitgebreid met een place_order()-aanroep.
os.environ["BITVAVO_PAPER_MODE"] = "true"

_REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ant_colony.biome.adapters.bitvavo_adapter import BitvavoAdapter  # noqa: E402


def _check(label: str, ok: bool) -> None:
    print(f"  [{'OK  ' if ok else 'FOUT'}] {label}")


def main() -> None:
    if not os.getenv("BITVAVO_API_KEY") or not os.getenv("BITVAVO_API_SECRET"):
        print("FOUT: stel API keys in voor je dit script draait:")
        print("  set BITVAVO_API_KEY=jouw_key")
        print("  set BITVAVO_API_SECRET=jouw_secret")
        sys.exit(1)

    print("=" * 56)
    print("  BitvavoAdapter smoke test  —  PAPER MODE  —  read-only")
    print("=" * 56)

    adapter = BitvavoAdapter()
    print(f"\nAdapter aangemaakt   biome_id={adapter.biome_id!r}   paper_only={adapter._paper_only}\n")

    # ------------------------------------------------------------------
    # 1. is_available
    # ------------------------------------------------------------------
    print("── is_available() ──────────────────────────────────────")
    available = adapter.is_available()
    _check(f"API bereikbaar: {available}", available)

    if not available:
        print("\nBitvavo niet bereikbaar — controleer netwerk en API keys.")
        sys.exit(1)

    # ------------------------------------------------------------------
    # 2. get_account_state
    # ------------------------------------------------------------------
    print("\n── get_account_state() ─────────────────────────────────")
    state = adapter.get_account_state()
    if state is None:
        _check("get_account_state() → None (auth-fout of rate-limit)", False)
    else:
        _check("get_account_state() geslaagd", True)
        print(f"     currency        : {state.currency}")
        print(f"     balance         : {state.balance:>12.2f} EUR  (vrij beschikbaar)")
        print(f"     positions_value : {state.positions_value:>12.2f} EUR  (vergrendeld in orders)")
        print(f"     equity          : {state.equity:>12.2f} EUR")
        print(f"     timestamp       : {state.timestamp.isoformat()}")
        print(f"     stale (<60s)    : {state.is_stale()}")

    # ------------------------------------------------------------------
    # 3. get_market_data
    # ------------------------------------------------------------------
    print("\n── get_market_data('BTC-EUR', '1h') ────────────────────")
    data = adapter.get_market_data("BTC-EUR", "1h")
    if data is None:
        _check("get_market_data() → None", False)
    else:
        _check("get_market_data() geslaagd", True)
        print(f"     symbol          : {data.symbol}")
        print(f"     timeframe       : {data.timeframe}")
        print(f"     timestamp       : {data.timestamp.isoformat()}")
        print(f"     open            : {data.open:>12.2f} EUR")
        print(f"     high            : {data.high:>12.2f} EUR")
        print(f"     low             : {data.low:>12.2f} EUR")
        print(f"     close           : {data.close:>12.2f} EUR")
        print(f"     volume          : {data.volume:>14.4f} BTC")
        print(f"     valid_price     : {data.is_valid_price}")
        print(f"     stale (<5m)     : {data.is_stale()}")

    # ------------------------------------------------------------------
    # 4. get_positions
    # ------------------------------------------------------------------
    print("\n── get_positions() ─────────────────────────────────────")
    positions = adapter.get_positions()
    if positions is None:
        _check("get_positions() → None (auth-fout)", False)
    elif not positions:
        _check("get_positions() geslaagd — geen crypto holdings gevonden", True)
    else:
        _check(f"get_positions() geslaagd — {len(positions)} holding(s)", True)
        for pos in positions:
            pnl = pos.unrealized_pnl
            print(f"\n     {pos.symbol}")
            print(f"       position_id   : {pos.position_id}")
            print(f"       quantity      : {pos.quantity:.6f}")
            print(f"       current_price : {pos.current_price:>12.2f} EUR")
            print(f"       market_value  : {pos.market_value:>12.2f} EUR")
            print(f"       unrealized_pnl: {pnl:>+12.2f} EUR")

    # ------------------------------------------------------------------
    # Samenvatting
    # ------------------------------------------------------------------
    print("\n" + "=" * 56)
    print("  Klaar — geen orders geplaatst.")
    print("=" * 56)


if __name__ == "__main__":
    main()
