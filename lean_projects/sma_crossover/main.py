from AlgorithmImports import *


class SmaCrossoverAlgorithm(QCAlgorithm):
    def Initialize(self):
        symbol = self.GetParameter("symbol") or "BTCUSD"
        short_period = self._int_parameter("short_period", 10)
        long_period = self._int_parameter("long_period", 30)
        cash = self._float_parameter("cash", 10000.0)

        self.SetStartDate(
            self._int_parameter("start_year", 2021),
            self._int_parameter("start_month", 1),
            self._int_parameter("start_day", 1),
        )
        self.SetEndDate(
            self._int_parameter("end_year", 2024),
            self._int_parameter("end_month", 1),
            self._int_parameter("end_day", 1),
        )
        self.SetCash(cash)

        self.symbol = self.AddCrypto(symbol, Resolution.Hour).Symbol
        self.short_sma = self.SMA(self.symbol, short_period, Resolution.Hour)
        self.long_sma = self.SMA(self.symbol, long_period, Resolution.Hour)
        self.SetWarmUp(long_period, Resolution.Hour)

    def OnData(self, data):
        if self.IsWarmingUp or not self.short_sma.IsReady or not self.long_sma.IsReady:
            return
        if self.symbol not in data or data[self.symbol] is None:
            return

        short_value = self.short_sma.Current.Value
        long_value = self.long_sma.Current.Value

        if short_value > long_value and not self.Portfolio[self.symbol].Invested:
            self.SetHoldings(self.symbol, 1.0)
        elif short_value < long_value and self.Portfolio[self.symbol].Invested:
            self.Liquidate(self.symbol, "sma_cross_exit")

    def _int_parameter(self, name, default):
        value = self.GetParameter(name)
        if value is None or value == "":
            return default
        return int(value)

    def _float_parameter(self, name, default):
        value = self.GetParameter(name)
        if value is None or value == "":
            return default
        return float(value)
