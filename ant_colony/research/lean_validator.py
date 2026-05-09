"""
Optional QuantConnect Lean validator for ResearchAnt candidates.

Lean is intentionally a soft dependency. If the CLI, Docker, or the backtest
runtime is unavailable, the colony keeps running and records an
``unavailable`` validation result for the candidate.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

LeanResult = dict[str, Any]


class LeanValidator:
    """Run a minimal Lean backtest for a StrategyCandidate dict."""

    def __init__(
        self,
        logs_root: Path | str | None = None,
        *,
        timeout_seconds: int = 120,
        lean_executable: str = "lean",
        template_name: str = "sma_crossover",
    ) -> None:
        self.logs_root = Path(logs_root) if logs_root is not None else Path(
            os.getenv("ANT_LOGS", "logs")
        )
        self.timeout_seconds = timeout_seconds
        self.lean_executable = lean_executable
        self.template_name = template_name
        self.repo_root = Path(__file__).resolve().parents[2]
        self.template_dir = self.repo_root / "lean_projects" / template_name

    def validate(self, candidate: dict[str, Any]) -> LeanResult:
        """
        Validate a StrategyCandidate with Lean.

        Returns a normalized result and writes it to
        ANT_LOGS/lean/<candidate_id>.json. All failures are contained.
        """
        candidate_id = str(candidate.get("candidate_id") or "unknown-candidate")
        try:
            availability = self._check_availability()
            if availability is not None:
                result = self._base_result("unavailable", availability)
                self._write_result(candidate_id, result)
                return result

            project_dir = self._prepare_project(candidate)
            output_dir = project_dir / "lean-output"
            output_dir.mkdir(parents=True, exist_ok=True)

            lean_command = self._resolve_lean_executable()
            command = [lean_command, "backtest", str(project_dir)]
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
            if proc.returncode != 0:
                reason = self._trim_reason(proc.stderr or proc.stdout or "Lean backtest failed")
                result = self._base_result("failed", reason)
                self._write_result(candidate_id, result)
                return result

            metrics = self._parse_output(project_dir)
            if metrics is None:
                result = self._base_result("failed", "Lean output JSON niet gevonden of onleesbaar")
                self._write_result(candidate_id, result)
                return result

            result = {
                **metrics,
                "lean_status": "passed",
                "lean_reason": "Lean backtest voltooid",
            }
            self._write_result(candidate_id, result)
            return result
        except subprocess.TimeoutExpired:
            result = self._base_result("failed", f"Lean backtest timeout na {self.timeout_seconds}s")
            self._write_result(candidate_id, result)
            return result
        except Exception as exc:  # pragma: no cover - final safety net
            _log.exception("Lean validatie onverwacht mislukt voor %s", candidate_id)
            result = self._base_result("unavailable", f"Lean validator fout: {exc}")
            self._write_result(candidate_id, result)
            return result

    def _check_availability(self) -> str | None:
        if self._resolve_lean_executable() is None:
            return "lean CLI niet gevonden"
        if shutil.which("docker") is None:
            return "Docker niet gevonden"
        ok, output = self._run_quick(["docker", "--version"])
        if not ok:
            return f"Docker niet beschikbaar: {output}"
        return None

    def _resolve_lean_executable(self) -> str | None:
        resolved = shutil.which(self.lean_executable)
        if resolved:
            return resolved
        explicit = Path(self.lean_executable)
        if explicit.exists():
            return str(explicit)
        if self.lean_executable != "lean":
            return None
        env_path = os.getenv("LEAN_CLI_PATH")
        if env_path and Path(env_path).exists():
            return env_path
        common_pc1_path = (
            Path.home()
            / "AppData"
            / "Local"
            / "Python"
            / "pythoncore-3.14-64"
            / "Scripts"
            / "lean.EXE"
        )
        if common_pc1_path.exists():
            return str(common_pc1_path)
        return None

    @staticmethod
    def _run_quick(command: list[str]) -> tuple[bool, str]:
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            return proc.returncode == 0, (proc.stdout or proc.stderr or "").strip()
        except Exception as exc:
            return False, str(exc)

    def _prepare_project(self, candidate: dict[str, Any]) -> Path:
        candidate_id = self._safe_id(str(candidate.get("candidate_id") or "candidate"))
        project_dir = self.logs_root / "lean" / "work" / candidate_id
        if not self.template_dir.exists():
            raise FileNotFoundError(f"Lean template niet gevonden: {self.template_dir}")
        shutil.copytree(self.template_dir, project_dir, dirs_exist_ok=True)
        config_path = project_dir / "config.json"
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            config = {"algorithm-language": "Python", "algorithm-location": "main.py"}
        params = dict(config.get("parameters") or {})
        params.update(self._candidate_parameters(candidate))
        config["parameters"] = {k: str(v) for k, v in params.items()}
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        return project_dir

    def _candidate_parameters(self, candidate: dict[str, Any]) -> dict[str, Any]:
        market_scope = candidate.get("market_scope") or {}
        parameters = candidate.get("parameters") or {}
        strategy_params = candidate.get("strategy_parameters") or {}
        symbol = (
            market_scope.get("symbol")
            or candidate.get("symbol")
            or candidate.get("asset")
            or parameters.get("symbol")
            or "BTCUSD"
        )
        return {
            "symbol": self._lean_symbol(str(symbol)),
            "short_period": _first_present(
                parameters,
                strategy_params,
                candidate,
                keys=("short_period", "short_sma", "fast_period", "fast_window"),
                default=10,
            ),
            "long_period": _first_present(
                parameters,
                strategy_params,
                candidate,
                keys=("long_period", "long_sma", "slow_period", "slow_window"),
                default=30,
            ),
            "cash": _first_present(parameters, candidate, keys=("cash", "starting_cash"), default=10000),
            "start_year": _first_present(parameters, candidate, keys=("start_year",), default=2021),
            "start_month": _first_present(parameters, candidate, keys=("start_month",), default=1),
            "start_day": _first_present(parameters, candidate, keys=("start_day",), default=1),
            "end_year": _first_present(parameters, candidate, keys=("end_year",), default=2024),
            "end_month": _first_present(parameters, candidate, keys=("end_month",), default=1),
            "end_day": _first_present(parameters, candidate, keys=("end_day",), default=1),
        }

    def _render_algorithm(self, candidate: dict[str, Any]) -> str:
        symbol = str((candidate.get("market_scope") or {}).get("symbol") or "SPY")
        direction = str((candidate.get("entry_conditions") or {}).get("direction") or "long").lower()
        exit_conditions = candidate.get("exit_conditions") or {}
        tp_pct = self._float_or_default(exit_conditions.get("take_profit_pct"), 0.06)
        sl_pct = self._float_or_default(exit_conditions.get("stop_loss_pct"), 0.03)
        lean_symbol = self._lean_symbol(symbol)
        is_crypto = "-" in symbol or str(candidate.get("biome") or "").lower() == "crypto"
        add_method = "AddCrypto" if is_crypto else "AddEquity"
        resolution = "Resolution.Hour" if is_crypto else "Resolution.Daily"
        start_year, start_month, start_day = 2023, 1, 1
        end_year, end_month, end_day = 2026, 1, 1

        return f'''from AlgorithmImports import *


class CandidateValidationAlgorithm(QCAlgorithm):
    def Initialize(self):
        self.SetStartDate({start_year}, {start_month}, {start_day})
        self.SetEndDate({end_year}, {end_month}, {end_day})
        self.SetCash(100000)
        self.symbol = self.{add_method}("{lean_symbol}", {resolution}).Symbol
        self.fast = self.SMA(self.symbol, 20)
        self.slow = self.SMA(self.symbol, 50)
        self.entry_price = None
        self.direction = "{direction}"
        self.tp_pct = {tp_pct!r}
        self.sl_pct = {sl_pct!r}

    def OnData(self, data):
        if not self.fast.IsReady or not self.slow.IsReady:
            return
        if self.symbol not in data or data[self.symbol] is None:
            return
        price = data[self.symbol].Close
        if price <= 0:
            return
        if not self.Portfolio.Invested:
            if self.fast.Current.Value > self.slow.Current.Value:
                weight = -1 if self.direction == "short" else 1
                self.SetHoldings(self.symbol, weight)
                self.entry_price = price
            return
        if self.entry_price is None:
            return
        if self.direction == "short":
            if price <= self.entry_price * (1 - self.tp_pct):
                self.Liquidate(self.symbol, "take_profit")
            elif price >= self.entry_price * (1 + self.sl_pct):
                self.Liquidate(self.symbol, "stop_loss")
        else:
            if price >= self.entry_price * (1 + self.tp_pct):
                self.Liquidate(self.symbol, "take_profit")
            elif price <= self.entry_price * (1 - self.sl_pct):
                self.Liquidate(self.symbol, "stop_loss")
'''

    @staticmethod
    def _lean_symbol(symbol: str) -> str:
        upper = symbol.upper()
        crypto_map = {
            "BTC-EUR": "BTCEUR",
            "ETH-EUR": "ETHEUR",
            "SOL-EUR": "SOLEUR",
            "XRP-EUR": "XRPEUR",
            "ADA-EUR": "ADAEUR",
            "LTC-EUR": "LTCEUR",
            "LINK-EUR": "LINKEUR",
            "DOT-EUR": "DOTEUR",
        }
        return crypto_map.get(upper, re.sub(r"[^A-Z0-9]", "", upper))

    @staticmethod
    def _safe_id(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]", "_", value)[:120] or "candidate"

    @staticmethod
    def _float_or_default(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _parse_output(self, output_dir: Path) -> LeanResult | None:
        for path in sorted(output_dir.rglob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            stats = self._find_statistics(data)
            if stats:
                return self._metrics_from_statistics(stats)
        return None

    def _find_statistics(self, data: Any) -> dict[str, Any] | None:
        if not isinstance(data, dict):
            return None
        for key in ("statistics", "Statistics", "runtimeStatistics", "RuntimeStatistics"):
            value = data.get(key)
            if isinstance(value, dict):
                return value
        for value in data.values():
            nested = self._find_statistics(value)
            if nested:
                return nested
        return None

    def _metrics_from_statistics(self, stats: dict[str, Any]) -> LeanResult:
        lookup = {self._normalize_key(k): v for k, v in stats.items()}
        return {
            "lean_sharpe": self._parse_number(
                self._lookup(lookup, "sharpe ratio", "sharperatio", "sharpe")
            ),
            "lean_max_drawdown": self._parse_number(
                self._lookup(lookup, "max drawdown", "drawdown", "maximum drawdown"),
                percent_to_fraction=True,
            ),
            "lean_win_rate": self._parse_number(
                self._lookup(lookup, "win rate", "winrate", "probabilistic sharpe ratio"),
                percent_to_fraction=True,
            ),
            "lean_trades": self._parse_int(
                self._lookup(
                    lookup,
                    "total trades",
                    "total number of trades",
                    "total orders",
                    "total number of orders",
                    "totalorders",
                )
            ),
        }

    @staticmethod
    def _normalize_key(key: str) -> str:
        return re.sub(r"\s+", " ", str(key).strip().lower())

    @staticmethod
    def _lookup(lookup: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            if key in lookup:
                return lookup[key]
        return None

    @staticmethod
    def _parse_number(value: Any, *, percent_to_fraction: bool = False) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            number = float(value)
            return number / 100.0 if percent_to_fraction and abs(number) > 1 else number
        text = str(value).strip()
        is_percent = "%" in text
        text = text.replace("%", "").replace(",", "")
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        if not match:
            return None
        number = float(match.group(0))
        if percent_to_fraction and (is_percent or abs(number) > 1):
            return number / 100.0
        return number

    @staticmethod
    def _parse_int(value: Any) -> int | None:
        number = LeanValidator._parse_number(value)
        return int(number) if number is not None else None

    @staticmethod
    def _base_result(status: str, reason: str) -> LeanResult:
        return {
            "lean_sharpe": None,
            "lean_max_drawdown": None,
            "lean_win_rate": None,
            "lean_trades": None,
            "lean_status": status,
            "lean_reason": reason,
        }

    def _write_result(self, candidate_id: str, result: LeanResult) -> None:
        path = self.logs_root / "lean" / f"{self._safe_id(candidate_id)}.json"
        payload = {
            "candidate_id": candidate_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            **result,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        except OSError:
            _log.exception("Kon Lean resultaat niet schrijven: %s", path)

    @staticmethod
    def _trim_reason(value: str, max_len: int = 500) -> str:
        text = " ".join(str(value).split())
        return text[:max_len] if len(text) > max_len else text


def _first_present(*sources: dict[str, Any], keys: tuple[str, ...], default: Any) -> Any:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in keys:
            value = source.get(key)
            if value is not None:
                return value
    return default
