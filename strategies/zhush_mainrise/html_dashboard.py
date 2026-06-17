import pandas as pd
import datetime
import os

def generate_html_dashboard(csv_path="dragons_candidates.csv", output_html="dashboard.html"):
    if not os.path.exists(csv_path):
        print(f"Error: {csv_path} not found. Please run main.py first.")
        return

    try:
        df = pd.read_csv(csv_path)
    except Exception as e:
        print(f"Error reading CSV: {e}")
        return

    # 为了方便截图，只截取排名前 20 的股票
    df = df.head(20)

    # 获取当前时间
    generate_time = datetime.datetime.now().strftime("%Y-%m-%d")

    # 构建表格行
    rows_html = ""
    for _, row in df.iterrows():
        # 根据总分设置不同的高亮样式
        score = row.get("total_score", 0)
        if score >= 85:
            score_class = "score-high"
        elif score >= 75:
            score_class = "score-medium"
        else:
            score_class = "score-normal"

        # 布尔值图标化
        def format_bool(val):
            if val == "是" or val == True:
                return "<span class='badge badge-yes'>✔</span>"
            return "<span class='badge badge-no'>✘</span>"

        gap_html = format_bool(row.get('gap_unfilled'))
        vol_html = format_bool(row.get('vol_expansion'))
        ma_html = format_bool(row.get('ma_bullish'))

        # 概念处理：方便截图，只显示前两个概念
        concepts = str(row.get('concept', '')).split(',')
        display_concepts = concepts[:2]
        concept_html = "".join([f"<span class='concept-tag'>{c.strip()}</span>" for c in display_concepts if c.strip()])
        if len(concepts) > 2:
            concept_html += "<span class='concept-tag'>...</span>"

        # 惩罚分数处理
        penalty = row.get('penalty', 0)
        penalty_html = f"<span class='penalty-red'>{penalty}</span>" if penalty < 0 else "<span class='penalty-none'>0</span>"

        rows_html += f"""
        <tr>
            <td class="font-mono">{row.get('ts_code')}</td>
            <td class="font-bold">{row.get('name')}</td>
            <td><div class="score-box {score_class}">{score}</div></td>
            <td class="text-center">{penalty_html}</td>
            <td><div class="concepts-wrapper">{concept_html}</div></td>
            <td class="text-center">{gap_html}</td>
            <td class="text-center font-mono">{row.get('limit_up_20d', 0)}</td>
            <td class="text-center font-mono">{row.get('consecutive_yang', 0)}</td>
            <td class="text-center">{vol_html}</td>
            <td class="text-center">{ma_html}</td>
            <td class="text-right font-mono">{row.get('turnover_rate(%)', 0):.2f}%</td>
            <td class="text-right font-mono">{row.get('circ_mv(亿)', 0):.2f}</td>
        </tr>
        """

    # HTML 模板 - 专为截图优化：高密度、高对比度、无多余留白
    html_template = f"""
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <title>主升浪选股战报</title>
        <style>
            :root {{
                --bg: #0f172a;
                --card-bg: #1e293b;
                --text: #f8fafc;
                --text-muted: #94a3b8;
                --border: #334155;
                --accent: #38bdf8;
            }}
            body {{
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
                background-color: var(--bg);
                color: var(--text);
                margin: 0;
                padding: 20px;
                display: flex;
                justify-content: center;
            }}
            .screenshot-container {{
                background-color: var(--card-bg);
                border-radius: 12px;
                border: 1px solid var(--border);
                box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.5);
                overflow: hidden;
                width: 1050px;
            }}
            .header {{
                padding: 16px 24px;
                border-bottom: 1px solid var(--border);
                display: flex;
                justify-content: space-between;
                align-items: center;
                background: linear-gradient(90deg, #1e293b, #0f172a);
            }}
            .header h1 {{
                margin: 0;
                font-size: 20px;
                color: #e2e8f0;
                display: flex;
                align-items: center;
                gap: 8px;
            }}
            .header .meta {{
                font-size: 13px;
                color: var(--text-muted);
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                font-size: 13px;
            }}
            th {{
                background-color: rgba(15, 23, 42, 0.4);
                color: var(--text-muted);
                font-weight: 500;
                text-align: left;
                padding: 10px 12px;
                border-bottom: 1px solid var(--border);
                white-space: nowrap;
            }}
            td {{
                padding: 8px 12px;
                border-bottom: 1px solid rgba(255, 255, 255, 0.03);
                vertical-align: middle;
            }}
            tr:nth-child(even) {{
                background-color: rgba(255, 255, 255, 0.01);
            }}
            .text-center {{ text-align: center; }}
            .text-right {{ text-align: right; }}
            .font-mono {{ font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace; }}
            .font-bold {{ font-weight: 600; color: #e2e8f0; }}
            
            .score-box {{
                display: inline-block;
                padding: 2px 8px;
                border-radius: 4px;
                font-weight: bold;
                font-family: monospace;
                font-size: 14px;
            }}
            .score-high {{ background: rgba(239, 68, 68, 0.15); color: #f87171; }}
            .score-medium {{ background: rgba(245, 158, 11, 0.15); color: #fbbf24; }}
            .score-normal {{ background: rgba(56, 189, 248, 0.15); color: #38bdf8; }}
            
            .penalty-red {{ color: #ef4444; font-weight: bold; }}
            .penalty-none {{ color: #475569; }}
            
            .badge {{
                display: inline-flex;
                align-items: center;
                justify-content: center;
                border-radius: 4px;
                font-size: 11px;
            }}
            .badge-yes {{ color: #10b981; font-weight: bold; }}
            .badge-no {{ color: #64748b; }}
            
            .concepts-wrapper {{
                display: flex;
                gap: 4px;
                flex-wrap: wrap;
            }}
            .concept-tag {{
                background: rgba(56, 189, 248, 0.1);
                color: #7dd3fc;
                padding: 2px 6px;
                border-radius: 4px;
                font-size: 11px;
                white-space: nowrap;
            }}
            .footer {{
                padding: 12px 24px;
                font-size: 12px;
                color: var(--text-muted);
                background-color: rgba(15, 23, 42, 0.6);
                display: flex;
                justify-content: space-between;
            }}
            .highlight-text {{ color: #38bdf8; }}
        </style>
    </head>
    <body>
        <div class="screenshot-container" id="capture-area">
            <div class="header">
                <h1>🔥 十五五主升浪战报 (Top 20)</h1>
                <div class="meta">生成日期：{generate_time} | 附带防追高过滤</div>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>代码</th>
                        <th>名称</th>
                        <th>总得分</th>
                        <th class="text-center">超涨扣分</th>
                        <th>核心概念</th>
                        <th class="text-center">缺口不补</th>
                        <th class="text-center">20日内涨停</th>
                        <th class="text-center">连阳天数</th>
                        <th class="text-center">连续放量</th>
                        <th class="text-center">均线多头</th>
                        <th class="text-right">换手率</th>
                        <th class="text-right">流通市值</th>
                    </tr>
                </thead>
                <tbody>
                    {rows_html}
                </tbody>
            </table>
            <div class="footer">
                <span>策略核心：抓取缺口未补、刚刚放量脱离底部的标的。</span>
                <span><span class="highlight-text">Antigravity Quant System</span> Auto-Generated</span>
            </div>
        </div>
    </body>
    </html>
    """

    with open(output_html, "w", encoding="utf-8") as f:
        f.write(html_template)
    print(f"Screenshot-friendly dashboard generated successfully at: {os.path.abspath(output_html)}")

if __name__ == '__main__':
    generate_html_dashboard()
