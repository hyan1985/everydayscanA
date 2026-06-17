import os
import datetime
import pandas as pd
from data_fetcher import DataFetcher
from scanner import Scanner
from tushare_token import get_tushare_token
from html_dashboard import generate_html_dashboard

def generate_wechat_report(df, top_concepts, filename="wechat_share.txt"):
    """生成方便微信转发的纯文本精简报告"""
    if len(df) == 0:
        return
        
    lines = [
        "🔥 【主升启动·资金共振】精选 5 只金股",
        f"📅 日期：{datetime.datetime.now().strftime('%Y-%m-%d')}",
        "⚠️ 提示：已加入【防追高机制】且叠加【主力资金最青睐板块】",
        "----------------------------------------",
        "💰 今日主力净流入 Top 3 热门板块："
    ]
    
    for i, (concept, amount) in enumerate(top_concepts.items()):
        medals = ["🥇", "🥈", "🥉"]
        rank = medals[i] if i < len(medals) else "🔹"
        lines.append(f"  {rank} {concept}: 净流入 {amount:.2f} 亿元")
        
    lines.append("----------------------------------------")
    
    medals = ["🥇", "🥈", "🥉", "🏅", "🏅"]
    
    for i, (_, row) in enumerate(df.iterrows()):
        rank = medals[i] if i < len(medals) else "🔹"
        name = row['name']
        ts_code = row['ts_code'].split('.')[0]
        score = row['total_score']
        concept = str(row['concept']).split(',')[0] if ',' in str(row['concept']) else str(row['concept'])
        limit_ups = row['limit_up_20d']
        penalty = row.get('penalty', 0)
        
        penalty_str = f" [超涨扣分: {penalty}]" if penalty < 0 else ""
        
        lines.append(f"{rank} {name} ({ts_code}) | 综合得分: {score}{penalty_str}")
        lines.append(f"   💡 核心概念: {concept}")
        lines.append(f"   📈 20日内涨停: {limit_ups}次 | 均线多头: {row['ma_bullish']} | 缺口: {row['gap_unfilled']}")
        lines.append(f"   💰 流通市值: {row['circ_mv(亿)']}亿 | 换手率: {row['turnover_rate(%)']}%")
        lines.append("")
        
    lines.append("----------------------------------------")
    lines.append("💡 策略说明: 优先选择缺口未补、刚刚放量脱离底部的标的，规避高位接盘。")
    
    report_text = "\n".join(lines)
    with open(filename, "w", encoding="utf-8") as f:
        f.write(report_text)
        
    print(f"\n✅ 微信转发精简版报告已生成: {os.path.abspath(filename)}")


_CANDIDATE_COLUMNS = [
    "ts_code", "name", "concept", "total_score", "penalty", "gap_unfilled",
    "limit_up_20d", "consecutive_yang", "vol_expansion", "ma_bullish",
    "turnover_rate(%)", "circ_mv(亿)", "trade_date",
]


def _save_candidates(results_df: pd.DataFrame, latest_date: str, output_csv: str = "dragons_candidates.csv"):
    """写入结果；零候选时也覆盖旧文件并标注 trade_date，避免聚合器静默沿用历史 CSV。"""
    out = results_df.copy() if not results_df.empty else pd.DataFrame(columns=_CANDIDATE_COLUMNS)
    out["trade_date"] = str(latest_date)
    out.to_csv(output_csv, index=False, encoding="utf-8-sig")
    meta_path = output_csv.replace(".csv", ".meta.json")
    import json
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({"trade_date": str(latest_date), "count": int(len(results_df))}, f)
    print(f"\n结果已保存至 {output_csv}（{len(results_df)} 只）")


def main():
    token = get_tushare_token()
    if not token:
        print(
            "未找到 Tushare Token。请配置其一："
            "环境变量 TUSHARE_TOKEN；"
            "或在「钥匙串访问」中新增通用密码："
            "服务名（钥匙串条目名）cursor-quant-tushare、账号 default，密码填 token。"
        )
        return
    fetcher = DataFetcher(token)
    
    # 1. 获取十五五概念板块股票池
    theme_df = fetcher.get_theme_pool()
    if theme_df.empty:
        print("未能获取到十五五概念股票，请检查 themes.json 配置或 Tushare 权限。")
        return
        
    # 2. 获取基础信息，并与主题池合并
    df_basic = fetcher.get_stock_basic(theme_df)
    if df_basic.empty:
        print("过滤后没有满足条件的候选股票。")
        return
        
    # 3. 获取交易日历
    dates = fetcher.get_trade_dates(days=60)
    latest_date = dates[-1]
    
    # 4. 获取最新的主力资金流向并计算板块流入
    mf_df = fetcher.get_latest_moneyflow(latest_date)
    top_concepts = {}
    if not mf_df.empty:
        # 仅保留我们的概念股
        valid_ts_codes = df_basic['ts_code'].unique()
        mf_df = mf_df[mf_df['ts_code'].isin(valid_ts_codes)].copy()
        
        # 计算主力净流入 (大单 + 特大单) 单位为万元 -> 转为亿元
        mf_df['main_net_inflow'] = (mf_df['buy_lg_amount'] + mf_df['buy_elg_amount'] - 
                                    mf_df['sell_lg_amount'] - mf_df['sell_elg_amount']) / 10000
                                    
        # 将资金流向与概念映射
        # 一个股票可能有多个概念，为了简单处理，我们将其拆分为多行或平均分配，或者直接加给它包含的每一个概念
        concept_inflow = {}
        # 将基础信息中的 ts_code, ths_concept_name 合并过来
        mf_merged = pd.merge(mf_df, df_basic[['ts_code', 'ths_concept_name']], on='ts_code', how='inner')
        
        for _, row in mf_merged.iterrows():
            inflow = row['main_net_inflow']
            concepts = str(row['ths_concept_name']).split(',')
            for c in concepts:
                c = c.strip()
                if c:
                    concept_inflow[c] = concept_inflow.get(c, 0) + inflow
                    
        # 选取资金流入最多的前 3 个板块
        sorted_concepts = sorted(concept_inflow.items(), key=lambda x: x[1], reverse=True)
        top_concepts = {k: v for k, v in sorted_concepts[:3]}
        print(f"\n今日主力净流入 Top 3 板块：")
        for k, v in top_concepts.items():
            print(f"  - {k}: {v:.2f} 亿元")
    
    # 5. 获取日线数据
    valid_ts_codes = df_basic['ts_code'].unique()
    df_merged = fetcher.get_market_data(dates, valid_ts_codes)
    
    # 6. 扫描打分
    print("\n开始执行量化打分...")
    scanner = Scanner(df_merged, df_basic)
    results_df = scanner.scan(min_score=60)
    
    if len(results_df) == 0:
        print("今天没有符合主升浪启动条件的十五五概念股。")
        _save_candidates(results_df, latest_date)
        return
        
    # 7. 结合热门资金板块进行二次过滤
    if top_concepts:
        top_concept_names = list(top_concepts.keys())
        def has_top_concept(concept_str):
            concepts = [c.strip() for c in str(concept_str).split(',')]
            for c in concepts:
                if c in top_concept_names:
                    return True
            return False
            
        results_df = results_df[results_df['concept'].apply(has_top_concept)]
        print(f"经过热门资金板块过滤，剩余候选股: {len(results_df)} 只")
        
    # 8. 只保留前 5 只股票
    results_df = results_df.head(5)
    
    if len(results_df) == 0:
        print("热门资金板块中没有符合主升启动条件的股票。")
        _save_candidates(results_df, latest_date)
        return
        
    print("\n=== 主力爆买板块·主升浪启动候选股 (Top 5) ===")
    pd.set_option('display.max_rows', 100)
    pd.set_option('display.max_columns', 15)
    pd.set_option('display.width', 1000)
    pd.set_option('display.unicode.east_asian_width', True)
    
    print(results_df.to_string(index=False))

    output_csv = "dragons_candidates.csv"
    _save_candidates(results_df, latest_date, output_csv)
    
    # 9. 生成微信转发版报告
    generate_wechat_report(results_df, top_concepts)

    # 10. 生成 HTML 可视化仪表盘
    generate_html_dashboard(output_csv)

if __name__ == '__main__':
    main()
