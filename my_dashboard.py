import tkinter as tk
from tkinter import ttk
import pandas as pd
import pandas_ta as ta
import numpy as np
from scipy.optimize import minimize
import threading
import time
import requests
import os
import yfinance as yf
import sqlite3

# ==========================================
# 仅对海外 API (黄金宏观、外汇) 保持代理，国内东财直连彻底免疫
PROXY_PORT = "7890" 
os.environ['HTTP_PROXY'] = f"http://127.0.0.1:{PROXY_PORT}"
os.environ['HTTPS_PROXY'] = f"http://127.0.0.1:{PROXY_PORT}"
# ==========================================

ASSET_POOL = {
    "红利低波 (Core)": {"secid": "1.512890", "lot_size": 100, "role": "core"},
    "中证A500 (Sat)": {"secid": "0.159338", "lot_size": 100, "role": "satellite"},
    "科创50 (Sat)": {"secid": "1.588000", "lot_size": 100, "role": "satellite"},
    "有色金属 (Sat)": {"secid": "1.512400", "lot_size": 100, "role": "satellite"},
    "国内黄金 (Sat)": {"secid": "1.518880", "lot_size": 100, "role": "satellite"}
}

GLOBAL_RETURNS = {}
GLOBAL_PRICE_LOCAL = {}
GLOBAL_SIGNALS = {}
GLOBAL_GOLD_PREMIUM = 0.0 
GLOBAL_SAT_WEIGHTS = {}

# ==========================================
# 【新增】：SQLite 国内顶层风控数据库
# ==========================================
def init_db():
    conn = sqlite3.connect('domestic_portfolio.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS nav_history
                 (timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, nav REAL)''')
    conn.commit()
    conn.close()

def get_hwm_and_record(current_nav):
    if current_nav <= 0: return 1.0 
    conn = sqlite3.connect('domestic_portfolio.db')
    c = conn.cursor()
    c.execute("INSERT INTO nav_history (nav) VALUES (?)", (current_nav,))
    conn.commit()
    c.execute("SELECT MAX(nav) FROM nav_history")
    hwm = c.fetchone()[0]
    conn.close()
    return hwm if hwm else current_nav

def fetch_eastmoney_data(secid, days=250):
    # 狡兔三窟：准备多个东财的备用底层数据节点
    domains = [
        "push2his.eastmoney.com",
        "1.push2his.eastmoney.com",
        "79.push2his.eastmoney.com"
    ]
    
    for domain in domains:
        url = f"http://{domain}/api/qt/stock/kline/get?secid={secid}&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56&klt=101&fqt=1&end=20500101&lmt={days}"
        try:
            session = requests.Session()
            # 【终极杀招】：绝对无视系统的 os.environ 全局代理设置
            session.trust_env = False 
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Referer': 'http://quote.eastmoney.com/',
                'Connection': 'keep-alive'
            }
            
            # proxies 强制设为 None，加上 trust_env=False，彻底斩断代理干扰
            resp = session.get(url, headers=headers, proxies={"http": None, "https": None}, timeout=6)
            
            if resp.status_code == 200:
                res = resp.json()
                # 侦测是否成功拿到了真实数据
                if res and 'data' in res and res['data'] and 'klines' in res['data']:
                    klines = res['data']['klines']
                    data = []
                    for k in klines:
                        parts = k.split(',')
                        data.append([parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]])
                    df = pd.DataFrame(data, columns=['date', 'open', 'close', 'high', 'low', 'volume'])
                    df['date'] = pd.to_datetime(df['date'])
                    for col in ['open', 'close', 'high', 'low', 'volume']:
                        df[col] = pd.to_numeric(df[col])
                    return df
                    
        except Exception as e:
            # 如果当前节点被挂断，静默打印并自动尝试下一个节点
            print(f"[{secid}] 节点 {domain} 连接受阻: {e}，正在切换备用节点...")
            continue 
            
    # 如果三个节点全部被挂断，说明绝对是东财周日夜间在拔网线维护了
    print(f"❌ [{secid}] 所有东财节点均拒绝响应！确认为周日夜间服务器物理断网维护，请周一重试。")
    return pd.DataFrame()

def process_dataframe(df, name):
    df.ta.macd(close='close', fast=12, slow=26, signal=9, append=True)
    df.ta.rsi(close='close', length=14, append=True)
    df.ta.sma(close='close', length=60, append=True) 
    
    latest_d = df.iloc[-1]
    close_local = round(latest_d['close'], 3)
    
    sma_60 = latest_d['SMA_60'] if 'SMA_60' in df.columns and not pd.isna(latest_d['SMA_60']) else close_local
    trend_bias = (close_local / sma_60) - 1 if sma_60 > 0 else 0.0
    
    vol_20ma = df['volume'].rolling(20).mean().iloc[-1]
    latest_vol = latest_d['volume']
    vol_ratio = latest_vol / vol_20ma if vol_20ma > 0 else 1.0
    
    trend_status = "🟢 多头排列" if trend_bias > 0 else "🔴 均线压制"
    vol_status = "⚠️ 天量派发" if vol_ratio > 2.0 else ("🧊 地量冰点" if vol_ratio < 0.6 else "⚪ 量能平稳")
    
    penalty_mult = 1.0
    if trend_bias < 0: penalty_mult *= 0.5  
    if vol_ratio > 2.0: penalty_mult *= 0.8
    if vol_ratio < 0.6 and trend_bias > 0: penalty_mult *= 1.2 
    
    GLOBAL_SIGNALS[name] = (trend_bias, trend_status, vol_status, penalty_mult)
    returns_series = df['close'].pct_change()
    
    return close_local, returns_series

def optimize_erc_weights(returns_df, penalty_dict):
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
    
    adjusted_weights = {}
    total_adj = 0
    for i, col in enumerate(returns_df.columns):
        w = optimal_weights[i] * penalty_dict.get(col, 1.0)
        adjusted_weights[col] = w
        total_adj += w
        
    for col in adjusted_weights:
        adjusted_weights[col] /= total_adj
        
    return adjusted_weights

def safe_update_entry(widget, val):
    widget.delete(0, tk.END)
    widget.insert(0, val)

def safe_update_radar(msg, color):
    radar_var.set(msg)
    radar_label.config(fg=color)

def safe_update_hwm(msg, color):
    hwm_var.set(msg)
    hwm_label.config(fg=color)

def fetch_macro_and_radar(dom_price):
    global GLOBAL_GOLD_PREMIUM
    try:
        time.sleep(2.0)
        
        vix_data = yf.Ticker("^VIX").history(period="5d").dropna(subset=['Close'])
        if not vix_data.empty:
            root.after(0, safe_update_entry, vix_entry, f"{vix_data['Close'].iloc[-1]:.2f}")

        tnx_data = yf.Ticker("^TNX").history(period="5d").dropna(subset=['Close'])
        if not tnx_data.empty:
            root.after(0, safe_update_entry, us10y_entry, f"{tnx_data['Close'].iloc[-1]:.2f}")
            
        dxy_data = yf.Ticker("DX-Y.NYB").history(period="5d").dropna(subset=['Close'])
        if not dxy_data.empty:
            root.after(0, safe_update_entry, dxy_entry, f"{dxy_data['Close'].iloc[-1]:.2f}")

        xau_df = yf.Ticker("GC=F").history(period="5d").dropna(subset=['Close'])
        cny_df = yf.Ticker("CNY=X").history(period="5d").dropna(subset=['Close'])
        
        if xau_df.empty or cny_df.empty:
            raise ValueError("雅虎外汇/期金 API 返回空数据")
            
        xau = xau_df['Close'].iloc[-1]
        cny = cny_df['Close'].iloc[-1]
        
        if dom_price <= 0:
            raise ValueError("主系统未能获取国内黄金现价")
        
        fair_value = (xau * cny) / 31.1034768 * 0.01
        GLOBAL_GOLD_PREMIUM = (dom_price - fair_value) / fair_value
        
        if GLOBAL_GOLD_PREMIUM > 0.035:
            msg = f"🚨 [黄金雷达] 严重溢价 {GLOBAL_GOLD_PREMIUM:.2%}！强平A股黄金，腾出美金配额！"
            color = "#D32F2F"
        elif GLOBAL_GOLD_PREMIUM < -0.015:
            msg = f"💎 [黄金雷达] 国内折价 {GLOBAL_GOLD_PREMIUM:.2%}！覆盖摩擦成本，海外套现回国！"
            color = "#2E7D32"
        else:
            msg = f"⚖️ [黄金雷达] 定价合理摩擦带内，当前溢价率 {GLOBAL_GOLD_PREMIUM:.2%}"
            color = "#1565C0"
            
        root.after(0, safe_update_radar, msg, color)
        
    except Exception as e:
        GLOBAL_GOLD_PREMIUM = 0.0
        err_msg = str(e)[:45] 
        root.after(0, safe_update_radar, f"⚠️ [雷达受阻] 报错详情: {err_msg}", "#E65100")

def fetch_and_calculate():
    update_status("正在获取数据...")
    for item in tree.get_children(): tree.delete(item)
    GLOBAL_RETURNS.clear()

    satellite_returns = {}
    penalty_dict = {}

    for name, info in ASSET_POOL.items():
        try:
            df = fetch_eastmoney_data(info["secid"])
            if df.empty: raise ValueError("无数据")
            
            p_local, ret = process_dataframe(df, name)
            GLOBAL_PRICE_LOCAL[name] = p_local
            
            _, _, _, penalty = GLOBAL_SIGNALS[name]
            
            if info["role"] == "satellite":
                satellite_returns[name] = ret.tail(60).reset_index(drop=True)
                penalty_dict[name] = penalty
                
            GLOBAL_RETURNS[name] = ret.tail(60).reset_index(drop=True)
            
        except Exception as e:
            print(f"Error processing {name}: {e}")

    dom_gold_price = GLOBAL_PRICE_LOCAL.get("国内黄金 (Sat)", 0.0)
    threading.Thread(target=fetch_macro_and_radar, args=(dom_gold_price,), daemon=True).start()

    sat_df = pd.DataFrame(satellite_returns).fillna(0)
    optimized_sat_w = optimize_erc_weights(sat_df, penalty_dict)

    global GLOBAL_SAT_WEIGHTS
    GLOBAL_SAT_WEIGHTS = optimized_sat_w

    for name, info in ASSET_POOL.items():
        p_local = GLOBAL_PRICE_LOCAL.get(name, 0.0)
        bias, t_stat, v_stat, _ = GLOBAL_SIGNALS.get(name, (0.0, "-", "-", 1.0))
        
        role_label = "🌟 绝对核心" if info["role"] == "core" else "🛰️ 战术卫星"
        weight_str = "宏观 ERP 动态分配" if info["role"] == "core" else f"{optimized_sat_w.get(name, 0.0):.1%} (卫星内)"
        
        tree.insert("", tk.END, values=(name, role_label, f"¥{p_local:.3f}", f"{t_stat} / {v_stat}", weight_str, "-", "-"))

    update_status("✅ 全息数据与 ERC 动能降权矩阵就绪！请执行资金分配。")

def calculate_basket_trade():
    try:
        total_equity = float(equity_entry.get())
        dxy_val = float(dxy_entry.get())
        us10y_val = float(us10y_entry.get())
        vix_val = float(vix_entry.get()) 
        
        div_yield = float(div_yield_entry.get())
        bond_yield = float(bond_yield_entry.get())
        gld_shift_val = float(gld_shift_entry.get())
    except ValueError:
        update_status("⚠️ 输入格式错误，请确保框内均为纯数字。")
        return

    # ==========================================
    # 【核心注入】：国内账户顶层 SQLite HWM 风控
    # ==========================================
    hwm = get_hwm_and_record(total_equity)
    drawdown = (hwm - total_equity) / hwm if hwm > 0 else 0.0
    
    if drawdown <= 0.05:
        dd_mult = 1.0
        hwm_msg = f"🛡️ [顶层风控] 历史最高净值: ¥{hwm:.2f} | 账户回撤: {drawdown:.2%} | 状态: 🟢 绝对安全 (暴露乘数 1.0)"
        hwm_color = "#2E7D32"
    elif drawdown >= 0.15:
        dd_mult = 0.0
        hwm_msg = f"🛡️ [顶层风控] 历史最高净值: ¥{hwm:.2f} | 账户回撤: {drawdown:.2%} | 状态: 🚨 触及死线！强制砍断卫星敞口 (乘数 0.0)"
        hwm_color = "#C62828"
    else:
        dd_mult = (0.15 - drawdown) / (0.15 - 0.05)
        hwm_msg = f"🛡️ [顶层风控] 历史最高净值: ¥{hwm:.2f} | 账户回撤: {drawdown:.2%} | 状态: ⚠️ 回撤失血中 (卫星降仓乘数 {dd_mult:.2f})"
        hwm_color = "#E65100"
        
    safe_update_hwm(hwm_msg, hwm_color)
    # ==========================================

    circuit_breaker = False
    if vix_val > 35.0:
        circuit_breaker = True
        update_status("🚨 全球流动性危机熔断 (VIX > 35)！卫星风险预算强制清零！")

    spread = div_yield - bond_yield
    core_weight = 0.40 + (spread - 2.0) * 0.15 
    core_weight = max(0.30, min(core_weight, 0.70)) 
    
    core_capital = total_equity * core_weight
    sat_capital = total_equity - core_capital
    
    if circuit_breaker:
        sat_capital = 0.0 
    else:
        sat_capital *= dd_mult # HWM 严格执行

    for item in tree.get_children():
        values = tree.item(item, "values")
        name = values[0]
        role = ASSET_POOL[name]["role"]
        lot_size = ASSET_POOL[name]["lot_size"]
        price_local = GLOBAL_PRICE_LOCAL.get(name, 1e9)
        
        try:
            current_holding = float(holding_entries[name].get())
        except ValueError:
            current_holding = 0.0
            
        target_exposure = 0.0
        
        if role == "core":
            target_exposure = core_capital
            
        elif role == "satellite":
            sat_w = GLOBAL_SAT_WEIGHTS.get(name, 0.0)
            target_exposure = sat_capital * sat_w
            
            if "黄金" in name:
                f_dxy = 1.0 + (104 - dxy_val) / 40
                f_rates = 1.0 + (4.2 - us10y_val) / 5.0
                macro_multiplier = max(0.2, min(f_dxy * f_rates, 3.0))
                
                if GLOBAL_GOLD_PREMIUM > 0.035: 
                    target_exposure = 0.0  
                else:
                    if GLOBAL_GOLD_PREMIUM < -0.015: macro_multiplier *= 1.4  
                    target_exposure = target_exposure * macro_multiplier
                    target_exposure = min(target_exposure, total_equity * 0.30) 
                    target_exposure += gld_shift_val 
                    
        trade_delta = target_exposure - current_holding
        
        friction_band = total_equity * 0.03 if role == "core" else total_equity * 0.015
        
        # 修正：当目标仓位因风控被强制压到 0 时，无视摩擦缓冲带，绝对清仓。
        if abs(trade_delta) < friction_band and ("黄金" not in name or gld_shift_val == 0) and target_exposure > 0: 
            action, display_shares = "⚪ 敞口匹配", "0 股"
        else:
            target_lots = int(trade_delta / (price_local * lot_size))
            actual_shares = target_lots * lot_size
            
            if target_exposure == 0.0 and current_holding > 0:
                action = "🚨 风控指令清仓"
            elif target_lots > 0: action = "🟢 买入"
            elif target_lots < 0: action = "🔴 卖出"
            else: action = "⚪ 不足一手"
                
            display_shares = f"{actual_shares:+} 股 ({abs(target_lots)}手)"

        tree.item(item, values=(name, values[1], values[2], values[3], values[4], action, display_shares))
        
    if not circuit_breaker:
        status_msg = f"✅ 分配完毕！系统已执行防线测算，红利底仓配置 {core_weight:.1%}。"
        update_status(status_msg)

def update_status(msg):
    status_var.set(msg)
    root.update_idletasks()

def run_in_thread(): threading.Thread(target=fetch_and_calculate, daemon=True).start()

# === GUI 界面布局 ===
init_db() # 启动时初始化国内数据库
root = tk.Tk()
root.title("LTR发财树")
root.geometry("1150x790") 
root.attributes('-topmost', True) 

radar_var = tk.StringVar(value="⏳ [黄金跨境雷达] 等待执行全景扫描获取溢价率...")
radar_label = tk.Label(root, textvariable=radar_var, fg="#C62828", font=("Arial", 11, "bold"))
radar_label.pack(pady=2)

# 新增的国内 HWM 风控状态栏
hwm_var = tk.StringVar(value="🛡️ [顶层风控] 数据库已连接。等待净值录入...")
hwm_label = tk.Label(root, textvariable=hwm_var, fg="#4E342E", font=("Arial", 10, "bold"))
hwm_label.pack(pady=2)

frame_top = tk.LabelFrame(root, text=" [ 投资组合矩阵 ] ", padx=10, pady=10, fg="#1565C0", font=("Arial", 10, "bold"))
frame_top.pack(padx=20, pady=5, fill="x")

columns = ("标的", "资产定位", "最新现价", "微观量价状态 (均线/量能)", "组合内部目标权重", "执行指令", "执行股数")
tree = ttk.Treeview(frame_top, columns=columns, show="headings", height=6) 
for col in columns:
    tree.heading(col, text=col)
    tree.column(col, anchor="center", width=120 if col not in ("微观量价状态 (均线/量能)", "执行股数") else 160)
tree.pack(fill="x")

ctrl_top = tk.Frame(frame_top)
ctrl_top.pack(pady=10, fill="x")

tk.Button(ctrl_top, text="1. API 底层扫描", command=run_in_thread, bg="#1565C0", fg="white", font=("Arial", 12, "bold"), height=2).grid(row=0, column=0, padx=15, rowspan=4)

macro_f = tk.LabelFrame(ctrl_top, text=" 宏观因子 (ERP & VIX) ", fg="#2E7D32")
macro_f.grid(row=0, column=1, rowspan=4, padx=10, sticky="n")

tk.Label(macro_f, text="VIX恐慌指数:").grid(row=0, column=0, sticky="e", pady=2)
vix_entry = tk.Entry(macro_f, width=8); vix_entry.insert(0, "18.0"); vix_entry.grid(row=0, column=1)

# 【已修复：将 macro_frame 改为 macro_f】
tk.Label(macro_f, text="账户总权益(¥):").grid(row=1, column=0, sticky="e", pady=2)
equity_entry = tk.Entry(macro_f, width=8); equity_entry.insert(0, "100000"); equity_entry.grid(row=1, column=1)

tk.Label(macro_f, text="红利股息(%):").grid(row=2, column=0, sticky="e", pady=2)
div_yield_entry = tk.Entry(macro_f, width=8); div_yield_entry.insert(0, "5.5"); div_yield_entry.grid(row=2, column=1)

tk.Label(macro_f, text="CN10Y债收(%):").grid(row=3, column=0, sticky="e", pady=2)
bond_yield_entry = tk.Entry(macro_f, width=8); bond_yield_entry.insert(0, "2.3"); bond_yield_entry.grid(row=3, column=1)

gold_f = tk.LabelFrame(ctrl_top, text=" 黄金跨境参数 ", fg="#E53935")
gold_f.grid(row=0, column=2, rowspan=4, padx=10, sticky="n")

tk.Label(gold_f, text="DXY美元指数:").grid(row=0, column=0, sticky="e", pady=5)
dxy_entry = tk.Entry(gold_f, width=8); dxy_entry.insert(0, "104.0"); dxy_entry.grid(row=0, column=1)

tk.Label(gold_f, text="US10Y美债(%):").grid(row=1, column=0, sticky="e", pady=5)
us10y_entry = tk.Entry(gold_f, width=8); us10y_entry.insert(0, "4.2"); us10y_entry.grid(row=1, column=1)

tk.Label(gold_f, text="折价转入金(¥):", fg="#D32F2F").grid(row=2, column=0, sticky="e", pady=5)
gld_shift_entry = tk.Entry(gold_f, width=8); gld_shift_entry.insert(0, "0.0"); gld_shift_entry.grid(row=2, column=1)

holdings_f = tk.LabelFrame(ctrl_top, text=" 当前持仓市值 (¥) ", fg="#FF9800")
holdings_f.grid(row=0, column=3, rowspan=4, padx=10)
holding_entries = {}
for idx, name in enumerate(ASSET_POOL.keys()):
    tk.Label(holdings_f, text=f"{name.split(' ')[0]}:").grid(row=idx, column=0, sticky="e", padx=2, pady=2)
    ent = tk.Entry(holdings_f, width=8)
    ent.insert(0, "0")
    ent.grid(row=idx, column=1, padx=5, pady=2)
    holding_entries[name] = ent

tk.Button(ctrl_top, text="2. 生成配置指令", command=calculate_basket_trade, bg="#2E7D32", fg="white", font=("Arial", 12, "bold"), height=2).grid(row=0, column=4, padx=15, rowspan=4)

status_var = tk.StringVar(value="系统就绪。")
tk.Label(root, textvariable=status_var, fg="gray", font=("Arial", 10)).pack(side=tk.BOTTOM, pady=10)

root.mainloop()