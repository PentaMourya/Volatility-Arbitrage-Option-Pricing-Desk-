#!/usr/bin/env python3
"""
Options Mispricing & Volatility Arbitrage Desk – Full Live Engine
Architecture: Object-Oriented, Modular, Fault-Tolerant

- Data Layer: Live (yfinance) or Mock fallback
- Execution Layer: Simulation (active) or Production (commented, safe to uncomment)
- Core Engine: Pure math and Decision State Machine
- Optimized for online compilers: 1 iteration, no sleep
"""

import time
import numpy as np
import pandas as pd
from scipy.stats import norm
from scipy.optimize import brentq
from datetime import datetime, timedelta

# =====================================================================
# 0. MATH UTILITIES (Pure Functions)
# =====================================================================
class VolMath:
    @staticmethod
    def black_scholes_price(S, K, T, r, sigma, option_type='call'):
        d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
        d2 = d1 - sigma * np.sqrt(T)
        if option_type == 'call':
            return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
        else:
            return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

    @staticmethod
    def implied_volatility(price, S, K, T, r, option_type='call', tol=1e-6):
        if price <= 0: return np.nan
        def obj(sigma): return VolMath.black_scholes_price(S, K, T, r, sigma, option_type) - price
        try: return brentq(obj, 1e-4, 5.0, xtol=tol, maxiter=100)
        except (ValueError, RuntimeError): return np.nan

# =====================================================================
# 1. DATA LAYER (Abstract Base + Implementations)
# =====================================================================
class DataProvider:
    def get_underlying_price(self, ticker: str) -> float: raise NotImplementedError
    def get_realized_volatility(self, ticker: str, window_days: int = 30) -> float: raise NotImplementedError
    def get_options_chain(self, ticker: str, expiration_date: str = None) -> (pd.DataFrame, str): raise NotImplementedError

class LiveDataProvider(DataProvider):
    def __init__(self):
        import yfinance as yf
        self.yf = yf

    def get_underlying_price(self, ticker: str) -> float:
        stock = self.yf.Ticker(ticker)
        tick_data = stock.history(period='1d', interval='1m')
        if not tick_data.empty: return tick_data['Close'].iloc[-1]
        info = stock.info
        return info.get('regularMarketPrice') or info.get('previousClose')

    def get_realized_volatility(self, ticker: str, window_days: int = 30) -> float:
        end = datetime.now()
        start = end - timedelta(days=window_days * 2)
        data = self.yf.download(ticker, start=start, end=end, progress=False)
        if len(data) < 2: return np.nan
        prices = data['Adj Close'].dropna()
        if len(prices) < window_days: window_days = max(1, len(prices) - 1)
        prices = prices.iloc[-window_days:]
        returns = np.log(prices / prices.shift(1)).dropna()
        return np.std(returns) * np.sqrt(252) if len(returns) > 0 else np.nan

    def get_options_chain(self, ticker: str, expiration_date: str = None) -> (pd.DataFrame, str):
        stock = self.yf.Ticker(ticker)
        if expiration_date is None:
            expirations = stock.options
            if not expirations: return pd.DataFrame(), None
            now = datetime.now().date()
            for exp in expirations:
                exp_date = datetime.strptime(exp, '%Y-%m-%d').date()
                if (exp_date - now).days >= 7:
                    expiration_date = exp
                    break
            else: expiration_date = expirations[0]
        chain = stock.option_chain(expiration_date)
        calls, puts = chain.calls, chain.puts
        for df in (calls, puts):
            df['mid'] = (df['bid'] + df['ask']) / 2
            df['mid'] = df['mid'].fillna(df['lastPrice']).fillna(0)
        calls['type'] = 'call'
        puts['type'] = 'put'
        options = pd.concat([calls, puts], ignore_index=True)
        keep = ['strike', 'type', 'mid', 'lastPrice', 'bid', 'ask', 'volume', 'openInterest']
        options = options[keep].rename(columns={'mid': 'price'})
        return options, expiration_date

class MockDataProvider(DataProvider):
    def get_underlying_price(self, ticker: str) -> float:
        return 500.0 + np.random.uniform(-10, 10)

    def get_realized_volatility(self, ticker: str, window_days: int = 30) -> float:
        return np.random.uniform(0.15, 0.25)

    def get_options_chain(self, ticker: str, expiration_date: str = None) -> (pd.DataFrame, str):
        mock_expiry = (datetime.now() + timedelta(days=14)).strftime('%Y-%m-%d')
        S = 500.0
        strikes = np.linspace(S - 20, S + 20, 9)
        data = []
        state_scenario = np.random.choice(['WORST', 'CAUTION', 'HEDGE', 'OPTIMAL'])
        rv_mock = np.random.uniform(0.15, 0.25)
        
        for K in strikes:
            for opt_type in ['call', 'put']:
                if state_scenario == 'WORST': mock_spread = np.random.uniform(0.07, 0.12)
                elif state_scenario == 'CAUTION': mock_spread = np.random.uniform(0.04, 0.06)
                elif state_scenario == 'HEDGE': mock_spread = np.random.uniform(-0.10, -0.05)
                else: mock_spread = np.random.uniform(-0.02, 0.02)
                
                mock_iv = max(0.05, rv_mock + mock_spread)
                price = max(0.05, VolMath.black_scholes_price(S, K, T=0.04, r=0.045, sigma=mock_iv, option_type=opt_type))
                data.append({
                    'strike': K, 'type': opt_type, 'price': price, 'lastPrice': price,
                    'bid': max(0.01, price - 0.1), 'ask': price + 0.1,
                    'volume': np.random.randint(1, 500), 'openInterest': np.random.randint(1, 1000)
                })
        return pd.DataFrame(data), mock_expiry

# =====================================================================
# 2. EXECUTION LAYER (Abstract Base + Implementations)
# =====================================================================
class ActionExecutor:
    def set_speed(self, speed_percent: float): raise NotImplementedError
    def toggle_pin(self, pin_id: int, state: bool): raise NotImplementedError
    def send_alert(self, level: str, message: str): raise NotImplementedError
    def execute_trade(self, side: str, quantity: int, strike: float, option_type: str): raise NotImplementedError

class SimulationExecutor(ActionExecutor):
    def set_speed(self, speed_percent: float): print(f"⚡ [SIM] Speed command sent: {speed_percent:.1f}%")
    def toggle_pin(self, pin_id: int, state: bool): print(f"💡 [SIM] Pin {pin_id} set to {'HIGH (ON)' if state else 'LOW (OFF)'}")
    def send_alert(self, level: str, message: str): print(f"🚨 [SIM][ALERT:{level}] {message}")
    def execute_trade(self, side: str, quantity: int, strike: float, option_type: str): print(f"📈 [SIM][EXECUTION] {side} {quantity} contracts @ strike {strike} ({option_type})")

# =====================================================================
# 2b. PRODUCTION EXECUTOR (Commented Out by default)
#     Safe to uncomment! Will silently fall back to Mock Hardware 
#     if run on a non-Raspberry Pi environment.
# =====================================================================
# class ProductionExecutor(ActionExecutor):
#     def __init__(self):
#         self.hardware_available = False
#         try:
#             import RPi.GPIO as GPIO
#             import serial, requests, logging
#             logging.basicConfig(filename='vol_arb_desk.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
#             
#             GPIO.setmode(GPIO.BCM)
#             GPIO.setwarnings(False)
#             self.PIN_SPEED_PWM, self.PIN_LED_GREEN, self.PIN_LED_RED, self.PIN_HEARTBEAT = 12, 17, 18, 25
#             for pin in [self.PIN_LED_GREEN, self.PIN_LED_RED, self.PIN_HEARTBEAT]: GPIO.setup(pin, GPIO.OUT)
#             GPIO.setup(self.PIN_SPEED_PWM, GPIO.OUT)
#             self.pwm = GPIO.PWM(self.PIN_SPEED_PWM, 100)
#             self.pwm.start(0)
#             
#             self.GPIO = GPIO
#             self.requests = requests
#             self.hardware_available = True
#         except ImportError:
#             class MockPWM:
#                 def ChangeDutyCycle(self, val): pass
#                 def start(self, val): pass
#             class MockGPIO:
#                 HIGH, LOW, BCM, OUT = 1, 0, 11, 0
#                 def setmode(self, mode): pass
#                 def setwarnings(self, val): pass
#                 def setup(self, pin, mode): pass
#                 def output(self, pin, state): pass
#             self.pwm = MockPWM()
#             self.GPIO = MockGPIO()
#             self.requests = type('obj', (object,), {'post': lambda *a, **k: type('obj', (object,), {'status_code': 200})()})()
#             import logging
#             logging.basicConfig(level=logging.INFO)
#             
#     def set_speed(self, speed_percent: float):
#         self.pwm.ChangeDutyCycle(speed_percent)
#         import logging; logging.info(f"Speed set to {speed_percent:.1f}%")
#     def toggle_pin(self, pin_id: int, state: bool):
#         self.GPIO.output(pin_id, self.GPIO.HIGH if state else self.GPIO.LOW)
#         import logging; logging.info(f"Pin {pin_id} set to {'ON' if state else 'OFF'}")
#     def send_alert(self, level: str, message: str):
#         import logging; logging.warning(f"ALERT [{level}]: {message}")
#     def execute_trade(self, side: str, quantity: int, strike: float, option_type: str):
#         payload = {"symbol": "SPY", "side": side.lower(), "quantity": quantity, "strike": strike, "type": option_type}
#         try:
#             response = self.requests.post("https://api.broker.com/v1/orders", json=payload, timeout=2)
#             import logging; logging.info(f"PRODUCTION ORDER SENT: {payload} | Status: {response.status_code}")
#         except Exception as e:
#             import logging; logging.error(f"Order failed: {e}")

# =====================================================================
# 3. CORE ENGINE (The "Soul" of the Code)
# =====================================================================
class VolArbDesk:
    def __init__(self, data_provider: DataProvider, executor: ActionExecutor, ticker: str = 'SPY'):
        self.data_provider = data_provider
        self.executor = executor
        self.ticker = ticker
        self.risk_free_rate = 0.045
        self.OVER_CRITICAL = 0.06
        self.OVER_WARNING = 0.03
        self.UNDER_HEDGE = -0.04

    def run_single_cycle(self):
        print(f"🚀 Starting Volatility Arbitrage Desk for {self.ticker}")
        print(f"Risk‑free rate: {self.risk_free_rate*100:.1f}%")
        print("Running single simulation cycle...\n")

        try:
            current_price = self.data_provider.get_underlying_price(self.ticker)
            rv = self.data_provider.get_realized_volatility(self.ticker)
            if np.isnan(rv): rv = 0.20

            options_df, exp_date = self.data_provider.get_options_chain(self.ticker)
            if options_df.empty:
                print(f"{datetime.now()}: No options data available.")
                return

            now = datetime.now()
            exp_date_obj = datetime.strptime(exp_date, '%Y-%m-%d')
            T = max((exp_date_obj - now).days / 365.0, 1/365.0)

            ivs = []
            for _, row in options_df.iterrows():
                if row['price'] <= 0:
                    ivs.append(np.nan)
                    continue
                iv = VolMath.implied_volatility(row['price'], current_price, row['strike'], T, self.risk_free_rate, row['type'])
                ivs.append(iv)

            options_df['iv'] = ivs
            options_df['rv'] = rv
            options_df['spread'] = options_df['iv'] - rv

            liquid = (options_df['volume'] > 0) | (options_df['openInterest'] > 0)
            filtered = options_df[liquid].copy()

            print("=" * 80)
            print(f"⏰ Live Update: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"📌 Underlying: {self.ticker} @ {current_price:.2f} | Expiry: {exp_date} (T={T:.2f}y)")
            print(f"📌 Realized Vol (30d): {rv*100:.2f}%")

            if filtered.empty:
                print("⚠️ No liquid contracts found.")
                self._analyze_and_act(pd.DataFrame(), current_price, rv)
            else:
                overpriced = filtered.sort_values('spread', ascending=False).head(5)
                print("\n🔥 TOP 5 OVERPRICED (Sell Signal) – highest IV‑RV")
                print(overpriced[['strike', 'type', 'price', 'iv', 'spread', 'volume']].to_string(index=False))

                underpriced = filtered.sort_values('spread', ascending=True).head(5)
                print("\n❄️ TOP 5 UNDEPRICED (Buy Signal) – lowest IV‑RV")
                print(underpriced[['strike', 'type', 'price', 'iv', 'spread', 'volume']].to_string(index=False))

                print("-" * 80)
                print(f"📊 Total liquid contracts: {len(filtered)}")
                print(f"📊 Avg spread (bps): {filtered['spread'].mean()*10000:.2f} bps")
                print(f"📊 Std spread (bps): {filtered['spread'].std()*10000:.2f} bps")
                print(f"📊 Max overpriced: {filtered['spread'].max()*100:.2f} vol pts")
                print(f"📊 Max underpriced: {filtered['spread'].min()*100:.2f} vol pts")

                self._analyze_and_act(filtered, current_price, rv)

        except Exception as e:
            print(f"⚠️ Error: {e}")

        print("\n🛑 Simulation complete. Shutting down desk gracefully.")
        self.executor.set_speed(0)
        self.executor.toggle_pin(17, False)
        self.executor.toggle_pin(18, False)
        self.executor.toggle_pin(25, False)

    def _analyze_and_act(self, filtered_df, current_price, rv):
        if filtered_df.empty:
            self.executor.toggle_pin(17, False)
            self.executor.toggle_pin(18, False)
            print("⏸️ No liquid data – all actuators idle.")
            return

        avg_spread = filtered_df['spread'].mean()
        max_over = filtered_df['spread'].max()
        min_under = filtered_df['spread'].min()

        if max_over > self.OVER_CRITICAL:
            print("\n🔴🔴🔴 STATE: WORST (Extreme Overpricing) 🔴🔴🔴")
            self.executor.set_speed(0)
            self.executor.toggle_pin(18, True)
            self.executor.toggle_pin(17, False)
            self.executor.send_alert("CRITICAL", f"Max spread {max_over*100:.2f}% > 6%. Halting.")
            worst = filtered_df.loc[filtered_df['spread'].idxmax()]
            self.executor.execute_trade("SELL", 10, worst['strike'], worst['type'])

        elif max_over > self.OVER_WARNING:
            print("\n🟡🟡🟡 STATE: CAUTION (Moderate Overpricing) 🟡🟡🟡")
            self.executor.set_speed(40)
            self.executor.toggle_pin(18, True)
            self.executor.toggle_pin(17, False)
            self.executor.send_alert("WARNING", f"Max spread {max_over*100:.2f}% > 3%.")
            worst = filtered_df.loc[filtered_df['spread'].idxmax()]
            self.executor.execute_trade("SELL", 2, worst['strike'], worst['type'])

        elif min_under < self.UNDER_HEDGE:
            print("\n🟣🟣🟣 STATE: HEDGE (Extreme Underpriced) 🟣🟣🟣")
            self.executor.set_speed(100)
            self.executor.toggle_pin(17, True)
            self.executor.toggle_pin(18, False)
            self.executor.send_alert("INFO", f"Min spread {min_under*100:.2f}% < -4%.")
            best = filtered_df.loc[filtered_df['spread'].idxmin()]
            self.executor.execute_trade("BUY", 15, best['strike'], best['type'])

        else:
            print("\n🟢🟢🟢 STATE: OPTIMAL (Fairly Priced) 🟢🟢🟢")
            self.executor.set_speed(80)
            self.executor.toggle_pin(17, True)
            self.executor.toggle_pin(18, False)

        self.executor.toggle_pin(25, True)
        self.executor.toggle_pin(25, False)

        print(f"📊 Underlying: {current_price:.2f} | RV: {rv*100:.2f}%")
        print(f"📊 Avg Spread: {avg_spread*10000:.2f} bps | Max Over: {max_over*100:.2f}% | Min Under: {min_under*100:.2f}%")
        print("-" * 80)

# =====================================================================
# 4. MAIN ENTRY POINT
# =====================================================================
if __name__ == "__main__":
    # --- Environment Detection ---
    try:
        import yfinance as yf
        data_provider = LiveDataProvider()
    except ImportError:
        data_provider = MockDataProvider()

    # --- Select Executor ---
    executor = SimulationExecutor()
    
    # To use Production, uncomment the line below AND uncomment the ProductionExecutor class above:
    # executor = ProductionExecutor()

    # --- Run the Desk ---
    desk = VolArbDesk(data_provider=data_provider, executor=executor, ticker='SPY')
    desk.run_single_cycle()