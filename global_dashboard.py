import tkinter as tk
from tkinter import ttk
import pandas as pd
import pandas_ta as ta
import numpy as np
from scipy.optimize import minimize
import threading
import time
import random
import yfinance as yf
import requests
import os
import sqlite3

# ==========================================
# 【系统级全局代理强制接管】
PROXY_PORT = "7890" 
os.environ['HTTP_PROXY'] = f"http://127.0.0.1:{PROXY_PORT}"
os.environ['HTTPS_PROXY'] = f"http://127.0.0.1:{PROXY_PORT}"
# ==========================================

# 彻底统一为 yfinance 雅虎财经底层代码格式
INDEX_DICT = {
    "恒生科技 (3033.HK)": {"code": "3033.HK", "lot_size": 200}, 
    "纳斯达克 (QQQ)": {"code": "QQQ", "lot_size": 1},      
    "标普500 (SPY)": {"code": "SPY", "lot_size": 1},
    "全球基石 (VT)": {"code": "VT", "lot_size": 1},
    "离岸黄金 (GLD)": {"code": "GLD", "lot_size": 1}
}

GLOBAL_ATR = {}
GLOBAL_RETURNS = {}
GLOBAL_OPTIMAL_WEIGHTS = {}
GLOBAL_BLENDED_SIGNAL = {}
GLOBAL_PRICE_LOCAL = {}
GLOBAL_PRICE_USD = {}
CURRENT_HKD_RATE = 7.82 
GLOBAL_GOLD_PREMIUM = 0.0 # 全局溢价率缓存

# ==========================================
# 【新增】：SQLite 顶层风控数据库
# ==========================================
def init_db():
    conn = sqlite3.connect('offshore_portfolio.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS nav_history
                 (timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, nav REAL)''')
    conn.commit()
    conn.close()

def get_hwm_and_record(current_nav):
    if current_nav <= 0: return 1.0 # 防止初始空载报错
    conn = sqlite3.connect('offshore_portfolio.db')
    c = conn.cursor()
    c.execute("INSERT INTO nav_history (nav) VALUES (?)", (current_nav,))
    conn.commit()
    c.execute("SELECT MAX(nav) FROM nav_history")
    hwm = c.fetchone()[0]
    conn.close()
    return hwm if hwm else current_nav

# 【新增】：雷达专用的东财直连（抗雅虎周末断流）
def fetch_eastmoney_data(secid, days=5):
    url = f"http://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56&klt=101&fqt=1&end=20500101&lmt={days}"
    try:
        session = requests.Session()
        session.trust_env = False 
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        proxies = {"http": None, "https": None}
        res = session.get(url, headers=headers, proxies=proxies, timeout=5).json()
        klines = res['data']['klines']
        data = []
        for k in klines:
            parts = k.split(',')
            data.append([parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]])
        df = pd.DataFrame(data, columns=['date', 'open', 'close', 'high', 'low', 'volume'])
        df['close'] = pd.to_numeric(df['close'])
        return df
    except Exception as e:
        return pd.DataFrame()

def get_macd_mult(macd_hist, macd_dif):
    if macd_hist > 0 and macd_dif > 0: return 1.2
    elif macd_hist > 0 > macd_dif: return 0.8
    elif macd_hist < 0 and macd_dif > 0: return 0.5
    else: return 0.0

def process_dataframe(df, name, hkd_series=None):
    df['close'] = pd.to_numeric(df['close'])
    df['high'] = pd.to_numeric(df['High']) if 'High' in df.columns else df['close']
    df['low'] = pd.to_numeric(df['Low']) if 'Low' in df.columns else df['close']
    
    df.ta.macd(close='close', fast=12, slow=26, signal=9, append=True)
    df.ta.atr(high='high', low='low', close='close', length=14, append=True)
    df.ta.adx(high='high', low='low', close='close', length=14, append=True)
    df.ta.rsi(close='close', length=14, append=True)
    df.ta.sma(close='close', length=200, append=True) 
    
    latest_d = df.iloc[-1]
    close_local = round(latest_d['close'], 3)
    
    sma_cols = [col for col in df.columns if col.startswith('SMA')]
    bias = (close_local / latest_d[sma_cols[0]]) - 1 if sma_cols and not pd.isna(latest_d[sma_cols[0]]) else 0.0
        
    adx_cols = [col for col in df.columns if col.startswith('ADX')]
    rsi_cols = [col for col in df.columns if col.startswith('RSI')]
    macd_h_cols = [col for col in df.columns if col.startswith('MACDh')]
    macd_d_cols = [col for col in df.columns if col.startswith('MACD_')]
    
    adx_val = latest_d[adx_cols[0]] if adx_cols else 20.0
    rsi_val = latest_d[rsi_cols[0]] if rsi_cols else 50.0
    
    w_trend = max(0.0, min((adx_val - 15) / 25.0, 1.0))
    trend_mult = get_macd_mult(latest_d[macd_h_cols[0]], latest_d[macd_d_cols[0]]) if macd_h_cols else 1.0
    mr_mult = 1.2 if rsi_val < 40 else (0.5 if rsi_val > 65 else 1.0)
    blended_mult = w_trend * trend_mult + (1.0 - w_trend) * mr_mult
    
    if w_trend > 0.8: signal_text = "🟢 强趋势主导"
    elif w_trend < 0.2: signal_text = "🟡 震荡回归主导"
    else: signal_text = "🔵 趋势震荡过渡"
        
    GLOBAL_BLENDED_SIGNAL[name] = (blended_mult, signal_text, bias)
    
    atr_cols = [col for col in df.columns if col.startswith('ATRr')]
    atr_val = latest_d[atr_cols[0]] if atr_cols else np.nan
    atr_pct = atr_val / close_local if close_local > 0 and not pd.isna(atr_val) else 0.02
    GLOBAL_ATR[name] = max(atr_pct, 0.005) 
    
    if hkd_series is not None and "HK" in name:
        df['date_norm'] = df['date'].dt.normalize()
        df['hkd_rate'] = df['date_norm'].map(hkd_series).ffill().fillna(7.82)
        df['close_usd'] = df['close'] / df['hkd_rate']
        close_usd = close_local / df['hkd_rate'].iloc[-1]
        returns_series = df['close_usd'].pct_change()
    else:
        close_usd = close_local
        returns_series = df['close'].pct_change()
    
    return close_local, close_usd, returns_series

def optimize_erc_weights(returns_df):
    num_assets = len(returns_df.columns)
    if num_assets == 0: return {}
    
    ewma_cov = returns_df.ewm(span=21).cov()
    cov_matrix = ewma_cov.iloc[-num_assets:].values * 252 
    
    def objective(weights):
        port_var = np.dot(weights.T, np.dot(cov_matrix, weights))
        mrc = np.dot(cov_matrix, weights)
        rc = weights * mrc
        rc_target = port_var / num_assets
        return np.sum(np.square(rc - rc_target))

    constraints = ({'type': 'eq', 'fun': lambda x: np.sum(x) - 1.0})
    bounds = tuple((0.05, 0.40) for _ in range(num_assets)) 
    init_guess = np.ones(num_assets) / num_assets

    res = minimize(objective, init_guess, method='SLSQP', bounds=bounds, constraints=constraints)
    optimal_weights = res.x if res.success else init_guess
    return {col: weight for col, weight in zip(returns_df.columns, optimal_weights)}

# 【UI线程安全更新】
def safe_update_entry(widget, val):
    widget.delete(0, tk.END)
    widget.insert(0, val)

def safe_update_radar(msg, color):
    radar_var.set(msg)
    radar_label.config(fg=color)

def safe_update_hwm(msg, color):
    hwm_var.set(msg)
    hwm_label.config(fg=color)

def fetch_macro_data():
    try:
        # 已升级为 5d 缓冲，防周末断流
        vix_data = yf.Ticker("^VIX").history(period="5d").dropna(subset=['Close'])
        if not vix_data.empty:
            root.after(0, safe_update_entry, vix_entry, f"{vix_data['Close'].iloc[-1]:.2f}")
            
        tnx_data = yf.Ticker("^TNX").history(period="5d").dropna(subset=['Close'])
        if not tnx_data.empty:
            root.after(0, safe_update_entry, us10y_entry, f"{tnx_data['Close'].iloc[-1]:.2f}")
            
        dxy_data = yf.Ticker("DX-Y.NYB").history(period="5d").dropna(subset=['Close'])
        if not dxy_data.empty:
            root.after(0, safe_update_entry, dxy_entry, f"{dxy_data['Close'].iloc[-1]:.2f}")
    except Exception as e: pass 

def fetch_gold_radar():
    global GLOBAL_GOLD_PREMIUM
    try:
        time.sleep(2.0)
        # 强制走东财通道拉取 A股 518880 防雅虎崩溃
        dom_df = fetch_eastmoney_data("1.518880", days=5)
        xau_df = yf.Ticker("GC=F").history(period="5d").dropna(subset=['Close'])
        cny_df = yf.Ticker("CNY=X").history(period="5d").dropna(subset=['Close'])
        
        if dom_df.empty or xau_df.empty or cny_df.empty:
            raise ValueError("底层 API 返回空数据(请检查网络)")
            
        dom_price = dom_df['close'].iloc[-1]
        xau = xau_df['Close'].iloc[-1]
        cny = cny_df['Close'].iloc[-1]
        
        fair_value = (xau * cny) / 31.1034768 * 0.01
        GLOBAL_GOLD_PREMIUM = (dom_price - fair_value) / fair_value
        
        if GLOBAL_GOLD_PREMIUM < -0.015:
            msg = f"💎 [跨境雷达] 国内折价 {GLOBAL_GOLD_PREMIUM:.2%}！离岸 GLD 将强制清仓，请转回国内抢筹！"
            color = "#2E7D32"
        elif GLOBAL_GOLD_PREMIUM > 0.035:
            msg = f"🚨 [跨境雷达] 国内严重溢价 {GLOBAL_GOLD_PREMIUM:.2%}！请清仓国内黄金，将美金填入下方专线承接！"
            color = "#D32F2F"
        else:
            msg = f"⚖️ [跨境雷达] 定价合理摩擦带内，当前溢价率 {GLOBAL_GOLD_PREMIUM:.2%}"
            color = "#1565C0"
        
        root.after(0, safe_update_radar, msg, color)
    except Exception as e:
        GLOBAL_GOLD_PREMIUM = 0.0
        err_msg = str(e)[:45]
        root.after(0, safe_update_radar, f"⚠️ [跨境雷达受阻] 报错详情: {err_msg}", "#E65100")

def fetch_and_calculate():
    global CURRENT_HKD_RATE
    update_status("正在拉取离岸数据...")
    for item in tree.get_children(): tree.delete(item)
    GLOBAL_RETURNS.clear()
    
    threading.Thread(target=fetch_macro_data, daemon=True).start()
    threading.Thread(target=fetch_gold_radar, daemon=True).start()
    
    try:
        hkd_df = yf.Ticker("HKD=X").history(period="1y")
        hkd_df.index = hkd_df.index.tz_localize(None).normalize()
        hkd_series = hkd_df['Close']
        CURRENT_HKD_RATE = hkd_series.iloc[-1]
    except:
        hkd_series = None
        
    for name, info in INDEX_DICT.items():
        code = info["code"]
        max_retries = 3
        for attempt in range(max_retries):
            try:
                time.sleep(random.uniform(0.5, 1.5)) 
                df_yf = yf.Ticker(code).history(period="1y") 
                if df_yf.empty: raise ValueError("无数据")
                df_yf.dropna(subset=['Close'], inplace=True)
                df_yf.reset_index(inplace=True)
                df = pd.DataFrame({'date': pd.to_datetime(df_yf['Date']).dt.tz_localize(None), 
                                   'close': df_yf['Close'], 'High': df_yf['High'], 'Low': df_yf['Low']})
                
                if "HK" in name:
                    p_local, p_usd, ret = process_dataframe(df, name, hkd_series)
                else:
                    p_local, p_usd, ret = process_dataframe(df, name)
                
                GLOBAL_PRICE_LOCAL[name] = p_local
                GLOBAL_PRICE_USD[name] = p_usd
                GLOBAL_RETURNS[name] = ret.tail(60).reset_index(drop=True)
                break 
                
            except Exception as e:
                if attempt < max_retries - 1: time.sleep(2)
                else: GLOBAL_RETURNS[name] = pd.Series([0]*60)

    returns_df = pd.DataFrame(GLOBAL_RETURNS).fillna(0)
    optimized_w = optimize_erc_weights(returns_df)
    
    for name in INDEX_DICT.keys():
        if name in optimized_w:
            GLOBAL_OPTIMAL_WEIGHTS[name] = optimized_w[name]
            blended_mult, sig_text, _ = GLOBAL_BLENDED_SIGNAL.get(name, (1.0, "异常", 0))
            p_local = GLOBAL_PRICE_LOCAL.get(name, 0.0)
            symbol = "HK$" if "HK" in name else "$"
            tree.insert("", tk.END, values=(name, f"{symbol}{p_local:.2f}", sig_text, f"{optimized_w[name]:.2%}", "-", "-", "-"))
            
    update_status("✅ 离岸数据与矩阵求解完毕！请核对宏观参数后，生成交易清单。")

def calculate_basket_trade():
    try:
        vix_val = float(vix_entry.get())
        us10y_val = float(us10y_entry.get())
        us2y_val = float(us2y_entry.get())
        dxy_val = float(dxy_entry.get())           
        total_equity = float(equity_entry.get()) 
        gld_shift_val = float(gld_shift_entry.get()) 
    except ValueError:
        update_status("⚠️ 宏观参数或总净值输入错误，请检查。")
        return

    # ==========================================
    # 【核心注入】：顶层 SQLite HWM 回撤风控模型
    # ==========================================
    hwm = get_hwm_and_record(total_equity)
    drawdown = (hwm - total_equity) / hwm if hwm > 0 else 0.0
    
    if drawdown <= 0.05:
        dd_mult = 1.0
        hwm_msg = f"🛡️ [顶层风控] 历史最高净值: ${hwm:.2f} | 账户回撤: {drawdown:.2%} | 状态: 🟢 绝对安全 (暴露乘数 1.0)"
        hwm_color = "#2E7D32"
    elif drawdown >= 0.15:
        dd_mult = 0.0
        hwm_msg = f"🛡️ [顶层风控] 历史最高净值: ${hwm:.2f} | 账户回撤: {drawdown:.2%} | 状态: 🚨 触及死线！强制清仓保命 (乘数 0.0)"
        hwm_color = "#C62828"
    else:
        dd_mult = (0.15 - drawdown) / (0.15 - 0.05)
        hwm_msg = f"🛡️ [顶层风控] 历史最高净值: ${hwm:.2f} | 账户回撤: {drawdown:.2%} | 状态: ⚠️ 回撤失血中 (强制降仓乘数 {dd_mult:.2f})"
        hwm_color = "#E65100"
        
    safe_update_hwm(hwm_msg, hwm_color)
    # ==========================================
        
    spread = us10y_val - us2y_val
    circuit_breaker_active = False
    
    if vix_val >= 35.0:
        circuit_breaker_active = True
        update_status("🚨 警告：检测到极端恐慌情绪 (VIX > 35)！触发熔断，清仓指令已下达。")
    
    for item in tree.get_children():
        values = tree.item(item, "values")
        name = values[0]
        currency_symbol = "HK$" if "HK" in name else "$"
        
        try:
            current_holding_usd = float(holding_entries[name].get())
        except ValueError:
            current_holding_usd = 0.0
            
        blended_mult, _, bias = GLOBAL_BLENDED_SIGNAL.get(name, (1.0, "", 0.0))
        opt_weight = GLOBAL_OPTIMAL_WEIGHTS.get(name, 0.2)
        lot_size = INDEX_DICT[name]["lot_size"]
        price_local = GLOBAL_PRICE_LOCAL.get(name, 1e9)
        
        if circuit_breaker_active:
            if "GLD" in name:
                target_exposure_usd = total_equity * 0.10 + gld_shift_val 
            else:
                target_exposure_usd = 0.0
        else:
            if not "GLD" in name:
                if bias > 0.15: blended_mult *= 0.2 
                elif bias > 0.08: blended_mult *= 0.6 
                elif bias < -0.10: blended_mult *= 1.3 

            if "GLD" in name:
                macro_multiplier = max(0.2, min((1+(104-dxy_val)/40)*(1+(4.2-us10y_val)/5.0)*(1+(vix_val-20)/40)*(1.3 if spread<-0.2 else 1.0), 3.0)) 
            else:
                macro_multiplier = max(0.2, min((1+(vix_val-20)/40)*(0.6 if spread<-0.2 else (1.2 if spread>0.5 else 1.0)), 3.0)) 

            # 同步最新阈值 -1.5% (-0.015)
            if "GLD" in name and GLOBAL_GOLD_PREMIUM < -0.015:
                target_exposure_usd = 0.0
            else:
                target_exposure_usd = total_equity * opt_weight * macro_multiplier * blended_mult
                target_exposure_usd = min(target_exposure_usd, total_equity * 0.40)
                
                # 【执行 HWM 顶层纪律】：根据回撤削减敞口
                target_exposure_usd *= dd_mult
                
                if "GLD" in name:
                    target_exposure_usd += gld_shift_val 
            
        trade_delta_usd = target_exposure_usd - current_holding_usd
        
        if abs(trade_delta_usd) < total_equity * 0.02 and not circuit_breaker_active and ("GLD" not in name or (gld_shift_val == 0 and GLOBAL_GOLD_PREMIUM >= -0.015)): 
            action, actual_delta_usd, display_shares = "⚪ 敞口匹配", 0.0, "0"
        else:
            trade_delta_local = trade_delta_usd * CURRENT_HKD_RATE if "HK" in name else trade_delta_usd
            target_lots = int(trade_delta_local / (price_local * lot_size))
            actual_shares = target_lots * lot_size
            actual_delta_local = actual_shares * price_local
            actual_delta_usd = actual_delta_local / CURRENT_HKD_RATE if "HK" in name else actual_delta_local
            
            if circuit_breaker_active and "GLD" not in name and current_holding_usd > 0:
                 action = "🚨 极值熔断清仓"
            elif "GLD" in name and GLOBAL_GOLD_PREMIUM < -0.015 and current_holding_usd > 0:
                 action = "🚨 折价极值清仓"
            elif target_lots > 0: action = "🟢 买入"
            elif target_lots < 0: action = "🔴 卖出"
            else: action = "⚪ 不足一手"
                
            display_shares = f"{actual_shares:+} 股 ({abs(target_lots)}手)" if lot_size > 1 else f"{actual_shares:+} 股"

        tree.item(item, values=(name, f"{currency_symbol}{price_local:.2f}", values[2], values[3], action, f"${actual_delta_usd:+.2f}", display_shares))
        
    if not circuit_breaker_active:
        update_status("✅ 自动提取与交易生成完毕！HWM 风控检测通过。")

def update_status(msg):
    status_var.set(msg)
    root.update_idletasks()

def run_in_thread(): threading.Thread(target=fetch_and_calculate, daemon=True).start()

# === GUI 界面布局 ===
init_db() # 启动时初始化数据库
root = tk.Tk()
root.title("LTR发财树（港美）")
root.geometry("1150x780")
root.attributes('-topmost', True) 

radar_var = tk.StringVar(value="⏳ [跨境雷达] 等待执行全景扫描获取黄金溢价率...")
radar_label = tk.Label(root, textvariable=radar_var, fg="#C62828", font=("Arial", 11, "bold"))
radar_label.pack(pady=2)

# 新增的 HWM 风控状态栏
hwm_var = tk.StringVar(value="🛡️ [顶层风控] 数据库已连接。等待净值录入...")
hwm_label = tk.Label(root, textvariable=hwm_var, fg="#4E342E", font=("Arial", 10, "bold"))
hwm_label.pack(pady=2)

columns = ("标的", "最新现价", "混合动能引擎", "EWMA平价权重", "执行指令", "调仓额(USD)", "执行股数")
tree = ttk.Treeview(root, columns=columns, show="headings", height=6)
for col in columns:
    tree.heading(col, text=col)
    tree.column(col, anchor="center", width=120 if col not in ("执行指令", "调仓额(USD)") else 140)
tree.pack(pady=10, padx=15, fill="x")

# 下方控制台分三个独立区域
control_frame = tk.Frame(root)
control_frame.pack(pady=5, fill="x", padx=15)

# 区域 1：宏观参数
macro_frame = tk.LabelFrame(control_frame, text=" [ 宏观环境与跨境指令 ] ", font=("Arial", 10, "bold"), fg="#1E88E5")
macro_frame.pack(side="left", padx=10, fill="y")

tk.Label(macro_frame, text="VIX恐慌指数:").grid(row=0, column=0, sticky="e", pady=3)
vix_entry = tk.Entry(macro_frame, width=10); vix_entry.insert(0, "18"); vix_entry.grid(row=0, column=1, padx=5)

tk.Label(macro_frame, text="US10Y美债(%):").grid(row=1, column=0, sticky="e", pady=3)
us10y_entry = tk.Entry(macro_frame, width=10); us10y_entry.insert(0, "4.2"); us10y_entry.grid(row=1, column=1, padx=5)

tk.Label(macro_frame, text="US 2Y美债(%):").grid(row=2, column=0, sticky="e", pady=3)
us2y_entry = tk.Entry(macro_frame, width=10); us2y_entry.insert(0, "4.6"); us2y_entry.grid(row=2, column=1, padx=5)

tk.Label(macro_frame, text="DXY美元指数:").grid(row=3, column=0, sticky="e", pady=3)
dxy_entry = tk.Entry(macro_frame, width=10); dxy_entry.insert(0, "104.0"); dxy_entry.grid(row=3, column=1, padx=5)

tk.Label(macro_frame, text="账户总净值(USD):").grid(row=4, column=0, sticky="e", pady=3)
equity_entry = tk.Entry(macro_frame, width=10); equity_entry.insert(0, "5000"); equity_entry.grid(row=4, column=1, padx=5)

tk.Label(macro_frame, text="国内严重溢价转入额(USD):", fg="#D32F2F").grid(row=5, column=0, sticky="e", pady=5)
gld_shift_entry = tk.Entry(macro_frame, width=10); gld_shift_entry.insert(0, "0.0"); gld_shift_entry.grid(row=5, column=1, padx=5)

# 区域 2：持仓矩阵
holdings_frame = tk.LabelFrame(control_frame, text=" [ 当前各标的持仓市值 (单位: USD) ] ", font=("Arial", 10, "bold"), fg="#43A047")
holdings_frame.pack(side="left", padx=10, fill="y")

holding_entries = {}
for idx, name in enumerate(INDEX_DICT.keys()):
    tk.Label(holdings_frame, text=f"{name.split(' ')[0]}:").grid(row=idx, column=0, sticky="e", pady=5)
    ent = tk.Entry(holdings_frame, width=12)
    ent.insert(0, "0")
    ent.grid(row=idx, column=1, padx=10)
    holding_entries[name] = ent

# 区域 3：操作中枢
action_frame = tk.Frame(control_frame)
action_frame.pack(side="right", padx=20)

tk.Button(action_frame, text="1. 底层扫描", command=run_in_thread, bg="#1E88E5", fg="white", font=("Arial", 12, "bold"), height=2, width=20).pack(pady=10)
tk.Button(action_frame, text="2. 生成交易清单", command=calculate_basket_trade, bg="#43A047", fg="white", font=("Arial", 12, "bold"), height=2, width=20).pack(pady=10)

status_var = tk.StringVar(); status_var.set("系统就绪。")
tk.Label(root, textvariable=status_var, fg="gray", font=("Arial", 10)).pack(pady=10)
root.mainloop()