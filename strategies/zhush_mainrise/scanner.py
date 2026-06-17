import pandas as pd
from metrics import *

class Scanner:
    def __init__(self, df_merged, df_basic):
        self.df_merged = df_merged
        self.df_basic = df_basic
        
    def scan(self, min_score=60):
        results = []
        grouped = self.df_merged.groupby('ts_code')
        
        from tqdm import tqdm
        for ts_code, df in tqdm(grouped, desc="计算打分"):
            if len(df) < 20:
                continue
                
            df = df.sort_values('trade_date')
            
            # --- 指标计算 ---
            has_limit_up, limit_up_count = check_limit_up(df, 20)
            has_4_yang, yang_count = count_consecutive_yang(df)
            has_vol_exp = check_volume_expansion(df)
            has_gap = check_gap_and_support(df, 10) # 缺口追溯时间为 10 天
            has_ma_bull = check_ma_alignment(df)
            
            circ_mv_yi = get_circ_mv(df)
            turnover = get_turnover_rate(df)
            
            # 基础风控：市值和换手率严重偏离的不参与打分（市值>1000亿，或换手率过低，或数据缺失）
            if pd.isna(circ_mv_yi) or pd.isna(turnover) or circ_mv_yi > 1000 or turnover < 1.0:
                continue
                
            # --- 加权打分系统 (满分 100 分) ---
            score = 0
            
            # 1. 缺口 (30分)
            if has_gap:
                score += 30
                
            # 2. 涨停 (25分)
            if limit_up_count >= 3:
                score += 25
            elif limit_up_count == 2:
                score += 20
            elif limit_up_count == 1:
                score += 15
                
            # 3. 连阳 (15分)
            if yang_count >= 4:
                score += 15
            elif yang_count == 3:
                score += 10
            elif yang_count == 2:
                score += 5
                
            # 4. 均线多头 (15分)
            if has_ma_bull:
                score += 15
                
            # 5. 放量 (15分)
            if has_vol_exp:
                score += 15
                
            # 6. 防追高机制 (动态扣分)
            penalty = calculate_overextended_penalty(df, 20)
            score += penalty
            
            # 只有达到门槛分数才输出
            if score >= min_score:
                stock_info = self.df_basic[self.df_basic['ts_code'] == ts_code]
                if len(stock_info) == 0:
                    continue
                    
                name = stock_info.iloc[0]['name']
                # 使用同花顺概念名
                concept = stock_info.iloc[0].get('ths_concept_name', stock_info.iloc[0]['industry'])
                
                results.append({
                    'ts_code': ts_code,
                    'name': name,
                    'concept': concept,
                    'total_score': score,
                    'penalty': penalty,
                    'gap_unfilled': '是' if has_gap else '否',
                    'limit_up_20d': limit_up_count,
                    'consecutive_yang': yang_count,
                    'vol_expansion': '是' if has_vol_exp else '否',
                    'ma_bullish': '是' if has_ma_bull else '否',
                    'turnover_rate(%)': round(turnover, 2),
                    'circ_mv(亿)': round(circ_mv_yi, 2)
                })
            
        results_df = pd.DataFrame(results)
        if len(results_df) > 0:
            results_df = results_df.sort_values(['total_score', 'turnover_rate(%)'], ascending=[False, False])
            
        return results_df
