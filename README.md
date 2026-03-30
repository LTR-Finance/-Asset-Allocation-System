
# 🚀 Asset-Allocation-system-for-noob
### **小白专属量化资产配置器**

---

## 📌 项目概述 | Project Overview
本项目是一套基于 Python 开发的闭环量化投资决策系统，旨在消除个体投资中的情绪化干扰，通过**宏观因子驱动、风险平价分配、顶层回撤控制**三位一体的逻辑，实现跨市场（A股、港股、美股）的科学资产配置。

This is a closed-loop quantitative investment decision system developed in Python. It aims to eliminate emotional biases in individual trading through a tripartite logic: **macro-factor driving, risk-parity allocation, and top-level drawdown control**, enabling systematic asset allocation across A-shares, HK, and US markets.

---

## 💡 投资哲学 | Investment Philosophy
* **核心-卫星策略 (Core-Satellite):** 以高股息、低波动红利资产为防御核心，以科技、有色、黄金为进攻卫星。
* **风险预算制 (Risk Budgeting):** 放弃预测价格涨跌，转向管理“风险暴露”，确保单一资产波动不影响整体组合稳定性。
* **知行合一 (Systematic Execution):** 所有的买入、卖出动作均由算法根据均线过滤和动能惩罚严格计算得出。

---

## 🛠️ 核心功能模块 | Key Modules

### 1. 风险平价优化器 | Risk Parity Optimizer (ERC)
* **逻辑：** 调用 `scipy.optimize` 求解二次规划问题，通过 EWMA 协方差矩阵计算各资产间的相关性。
* **目标：** 实现各卫星资产对组合风险贡献（Risk Contribution）的绝对均衡，规避行业拥挤度风险。
* **Math:** $\min f(w) = \sum_{i=1}^{n} \sum_{j=1}^{n} (RC_i - RC_j)^2$

### 2. 动态 ERP 与宏观因子集成 | Macro Factor & ERP Integration
* **实时监控：** 系统集成 VIX（恐慌指数）、US10Y（美债收益率）、DXY（美元指数）等宏观锚点。
* **动态调整：** 根据权益风险溢价（ERP）模型，动态调整核心底仓与卫星资产的资金分配权重。

### 3. 高水位线顶层风控 (HWM) | High Water Mark Risk Control
* **数据持久化：** 使用 **SQLite** 本地数据库实时录入账户净值，自动绘制净值曲线并追踪 HWM。
* **阶梯降仓：** 严格执行回撤风控：
    * *回撤 < 5%:* 核心策略正常运行。
    * *5% < 回撤 < 15%:* 线性收缩卫星资产风险预算。
    * *回撤 > 15%:* 触及“死线”，强制切断风险敞口。

### 4. 跨市套利雷达 | Cross-Market Arbitrage Radar
* **黄金溢价监测：** 实时对比境内（A股黄金ETF）与境外（美股GLD/离岸现货金）的定价偏差。
* **执行指令：** 当溢价/折价覆盖摩擦成本时，系统自动发出跨市划转指令。

---

## 💻 技术栈 | Technical Stack
* **Engine:** Python 3.10+
* **Data API:** Eastmoney (Direct Fetch), Yahoo Finance (YFinance)
* **Math/Stats:** Pandas, NumPy, Scipy (Optimization), Pandas-TA (Technical Analysis)
* **Database:** SQLite3
* **GUI:** Tkinter (Custom-built for real-time portfolio management)

---

## 📈 项目的一些小价值 | Professional Value
该项目不仅是我个人的实盘交易中枢，更是我对**vibecoding、组合管理、分布式风控**的一次深度实践。
我希望在AI的协助下，量化教育与因子提取会成为每一位投资者都能接触与学习的知识！
一周运行一次系统，即可建立自己的资产配置！
---
