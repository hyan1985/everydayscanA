import pandas as pd

def check_limit_up(df, window=20):
    """
    检查指定窗口内是否有涨停，并返回涨停次数。
    涨停标准粗略计算为涨跌幅 >= 9.8% (兼容主板和双创板块，双创20%同样满足>=9.8)。
    """
    if len(df) == 0:
        return False, 0
    recent = df.tail(window)
    limit_ups = recent[recent['pct_chg'] >= 9.8]
    count = len(limit_ups)
    return count > 0, count

def count_consecutive_yang(df):
    """
    计算最近连续收阳线 (收盘价 > 开盘价) 的天数。
    使用 iloc 逆序遍历；如果缺少列则安全返回 0。
    """
    if "close" not in df.columns or "open" not in df.columns:
        return False, 0
    count = 0
    for i in range(len(df) - 1, -1, -1):
        if df.iloc[i]["close"] > df.iloc[i]["open"]:
            count += 1
        else:
            break
    return count >= 4, count

def check_volume_expansion(df):
    """
    检查成交量是否连续放量。
    条件：5日均量 > 10日均量，或者最近3天连续放量 (V_t > V_{t-1} > V_{t-2})
    """
    if len(df) < 10:
        return False
        
    vol_ma5 = df['vol'].rolling(window=5).mean().iloc[-1]
    vol_ma10 = df['vol'].rolling(window=10).mean().iloc[-1]
    
    last_3_vols = df['vol'].tail(3).values
    continuous_increase = (last_3_vols[2] > last_3_vols[1]) and (last_3_vols[1] > last_3_vols[0])
    
    return (vol_ma5 > vol_ma10) or continuous_increase

def check_gap_and_support(df, lookback=10):
    """
    检查近期是否存在向上跳空缺口，并且后续日期的股价（最低价）一直维持在缺口上限之上。
    """
    if len(df) < lookback + 1:
        return False
        
    recent = df.tail(lookback).copy()
    
    # 倒序寻找最近的一个有效向上缺口
    for i in range(len(recent) - 1, 0, -1):
        curr = recent.iloc[i]
        prev = recent.iloc[i-1]
        
        # 向上跳空缺口：今天的最低价 > 昨天最高价的 1.01倍 (要求有一定幅度的真缺口)
        if curr['low'] > prev['high'] * 1.01:
            gap_top = prev['high'] # 缺口的下沿防线 (即昨天的最高价)
            
            # 如果缺口就发生在最后一天，则缺口未破
            if i == len(recent) - 1:
                return True
            else:
                # 检查缺口之后的所有日子，最低价是否都 > gap_top
                subsequent_days = recent.iloc[i+1:]
                if subsequent_days['low'].min() > gap_top:
                    return True
            # 只看最近的一个符合跳空的形态即可，找到了就跳出循环判断结束
            break
            
    return False

def check_ma_alignment(df):
    """
    检查均线多头排列 (MA5 > MA10 > MA20) 且 收盘价 > MA5
    """
    if len(df) < 20:
        return False
        
    ma5 = df['close'].rolling(window=5).mean().iloc[-1]
    ma10 = df['close'].rolling(window=10).mean().iloc[-1]
    ma20 = df['close'].rolling(window=20).mean().iloc[-1]
    
    close_price = df['close'].iloc[-1]
    
    return (ma5 > ma10) and (ma10 > ma20) and (close_price > ma5)

def calculate_overextended_penalty(df, window=20):
    """
    计算追高扣分（防追高机制）。
    避免买入已经处于主升浪中后期的股票，重点挖掘“即将启动”或“刚刚突破”的股票。
    """
    if len(df) < window:
        return 0
        
    recent_low = df['low'].tail(window).min()
    close_price = df['close'].iloc[-1]
    ma20 = df['close'].rolling(window=window).mean().iloc[-1]
    
    # 1. 底部累计涨幅惩罚
    runup = (close_price / recent_low) - 1
    runup_penalty = 0
    if runup > 0.5:     # 20日内涨幅超过50%，风险极高
        runup_penalty = -50
    elif runup > 0.35:  # 涨幅超过35%
        runup_penalty = -25
    elif runup > 0.25:  # 涨幅超过25%
        runup_penalty = -10
        
    # 2. 均线乖离率惩罚 (偏离20日线太远，随时可能回调)
    deviation = (close_price / ma20) - 1
    dev_penalty = 0
    if deviation > 0.3:
        dev_penalty = -40
    elif deviation > 0.2:
        dev_penalty = -20
    elif deviation > 0.12:
        dev_penalty = -5
        
    # 取两项中扣分最狠的一项
    return min(runup_penalty, dev_penalty)

def get_circ_mv(df):
    """
    获取最新流通市值 (单位：亿元)
    Tushare circ_mv 单位为 万元
    """
    if len(df) == 0:
        return 0
    return df['circ_mv'].iloc[-1] / 10000

def get_turnover_rate(df):
    """
    获取最新换手率 (%) - 对齐同花顺标准列表口径
    """
    if len(df) == 0:
        return 0
    return df['turnover_rate'].iloc[-1]
