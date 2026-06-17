import pandas as pd
import time
import datetime
import json
import os

from quant_data import get_provider, DAILY_BASIC_FIELDS

class DataFetcher:
    def __init__(self, token):
        self.pro = get_provider(token=token)
        self._raw_pro = None

    def _raw_tushare(self):
        """少量需要直通 tushare 的场景（无缓存代理）。"""
        if self._raw_pro is None:
            import tushare as ts
            self._raw_pro = ts.pro_api()
        return self._raw_pro

    def get_trade_dates(self, days=60):
        """获取最近 N 个交易日"""
        today = datetime.datetime.now().strftime('%Y%m%d')
        start_date = (datetime.datetime.now() - datetime.timedelta(days=days*2)).strftime('%Y%m%d')
        
        df = self.pro.trade_cal(exchange='SSE', is_open='1', start_date=start_date, end_date=today)
        dates = df['cal_date'].values.tolist()
        dates.sort()
        return dates[-days:]

    def get_theme_pool(self, config_path="config/themes.json"):
        """使用统一概念配置构建主题池"""
        from quant_data.concepts import load_concepts
        c = load_concepts(fallback_json=config_path if os.path.exists(config_path) else None,
                          inject_hot_boards=True)
        all_kws = set(c.get_all_keywords())
            
        print("正在获取同花顺概念板块列表...")
        try:
            ths_index = self.pro.ths_index()
        except Exception as e:
            print(f"获取同花顺板块失败: {e}")
            return pd.DataFrame(columns=["ts_code", "ths_concept_name"])
            
        if ths_index is None or ths_index.empty:
            return pd.DataFrame(columns=["ts_code", "ths_concept_name"])
            
        # 匹配板块
        matched_indices = {}
        for _, row in ths_index.iterrows():
            name = str(row['name'])
            for kw in all_kws:
                if kw in name:
                    matched_indices[row['ts_code']] = name
                    break
                    
        print(f"命中 {len(matched_indices)} 个相关的同花顺概念板块。正在拉取成分股...")
        
        members = []
        from tqdm import tqdm
        for ts_code, name in tqdm(matched_indices.items(), desc="拉取板块成分股"):
            try:
                m = self.pro.ths_member(ts_code=ts_code)
                if m is not None and not m.empty:
                    m = m[['con_code']].rename(columns={'con_code': 'ts_code'})
                    m['ths_concept_name'] = name
                    members.append(m)
            except Exception:
                continue
            time.sleep(0.13) # 防止频控限制
            
        if not members:
            return pd.DataFrame(columns=["ts_code", "ths_concept_name"])
            
        # 合并所有成分股
        theme_df = pd.concat(members, ignore_index=True)
        # 同一个股票可能属于多个相关概念，将其概念名称合并
        theme_df = theme_df.groupby('ts_code')['ths_concept_name'].apply(lambda x: ', '.join(x.unique())).reset_index()
        
        return theme_df

    def get_stock_basic(self, theme_df=None):
        """获取基础股票列表，进行风控过滤，并可选与主题池求交集"""
        print("正在获取全市场基础股票列表...")
        df = self.pro.stock_basic(exchange='', list_status='L', fields='ts_code,symbol,name,industry,list_date,market')
        
        # 1. 过滤 ST 股
        df = df[~df['name'].str.contains('ST')]
        
        # 2. 过滤北交所股票 (BJ)
        df = df[df['market'] != '北交所']
        
        # 3. 过滤次新股 (上市不满半年)
        six_months_ago = (datetime.datetime.now() - datetime.timedelta(days=180)).strftime('%Y%m%d')
        df = df[df['list_date'] < six_months_ago]
        
        # 4. 如果提供了主题池，则进行过滤和映射
        if theme_df is not None and not theme_df.empty:
            df = pd.merge(df, theme_df, on='ts_code', how='inner')
            print(f"经过十五五主题过滤，剩余 {len(df)} 只候选股票。")
        else:
            df['ths_concept_name'] = df['industry'] # Fallback
            
        return df

    def get_market_data(self, dates, valid_ts_codes):
        """按日期拉取市场数据并合并，过滤出有效的股票"""
        all_daily = []
        all_basic = []
        
        print(f"正在按日期批量拉取 {len(dates)} 天的行情数据...")
        for date in dates:
            try:
                daily_df = self.pro.daily(trade_date=date)
                basic_df = self.pro.daily_basic(trade_date=date, fields=DAILY_BASIC_FIELDS)
                
                # 在单日级别就过滤掉不需要的股票，减少后续内存占用
                daily_df = daily_df[daily_df['ts_code'].isin(valid_ts_codes)]
                basic_df = basic_df[basic_df['ts_code'].isin(valid_ts_codes)]
                
                all_daily.append(daily_df)
                all_basic.append(basic_df)
                
            except Exception as e:
                print(f"警告: 拉取 {date} 数据时出错 - {e}")
                
        df_daily = pd.concat(all_daily, ignore_index=True)
        df_basic = pd.concat(all_basic, ignore_index=True)
        
        print("正在合并行情与指标数据...")
        # daily_basic 也有 close 列，merge 时产生 close_x/close_y，需去掉 basic 的 close 避免冲突
        if "close" in df_basic.columns:
            df_basic = df_basic.drop(columns=["close"])
        merged = pd.merge(df_daily, df_basic, on=['ts_code', 'trade_date'], how='inner')
        merged = merged.sort_values(['ts_code', 'trade_date'], ascending=[True, True])
        
        return merged

    def get_latest_moneyflow(self, date):
        """获取全市场指定日期的资金流向数据"""
        print(f"正在拉取 {date} 的全市场资金流向数据...")
        try:
            df = self.pro.moneyflow(trade_date=date)
            return df
        except Exception as e:
            print(f"获取资金流向失败: {e}")
            return pd.DataFrame()
