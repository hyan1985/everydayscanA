"""新双池并行版本的 run_daily_selection（临时存放，用于替换）"""

def run_daily_selection(token: str, top_n: int = 8) -> pd.DataFrame:
    # ── 双池并行：版本A(超卖反弹) + 版本B(科技成长) ──
    today = datetime.now().date()
    raw_end_date = today.strftime("%Y-%m-%d")
    fetcher = DataFetcher(token=token)
    end_date = fetcher.get_latest_trade_date(raw_end_date)
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    end_ymd = end_dt.strftime("%Y%m%d")
    start_260 = (end_dt.date() - timedelta(days=420)).strftime("%Y-%m-%d")

    basic = fetcher.get_stock_basic_mainboard()
    universe = basic[["ts_code", "name", "industry"]].drop_duplicates(subset=["ts_code"]).copy()
    codes_all = universe["ts_code"].astype(str).tolist()
    print(f"选股日期: {end_date} | 全市场候选(剔ST/北交等): {len(codes_all)}")

    daily_basic_today = fetcher.get_daily_basic(codes_all, end_date)
    daily_basic_today["pe_ttm"] = pd.to_numeric(daily_basic_today.get("pe_ttm", np.nan), errors="coerce")

    # ── 同花顺概念1映射 ──
    ths_map = _build_ths_map(fetcher)

    # ── 布林带宽度 ──
    def _bb_width_metrics(code):
        df = fetcher.get_stock_daily(code, start_260, end_date)
        if df is None or df.empty or "close" not in df.columns:
            return (np.nan, np.nan)
        c = pd.to_numeric(df["close"], errors="coerce").dropna()
        if len(c) < 260:
            return (np.nan, np.nan)
        ma20 = c.rolling(20).mean()
        std20 = c.rolling(20).std()
        width = ((ma20 + 2*std20) - (ma20 - 2*std20)) / ma20.replace(0, np.nan)
        width = width.dropna()
        if len(width) < 250:
            return (np.nan, np.nan)
        return (float(width.iloc[-1]), float(width.tail(250).mean()))

    def _bb_filter(df_in, quantile=0.30):
        codes = df_in["ts_code"].astype(str).tolist()
        bb_cur, bb_mean = [], []
        for c in codes:
            cur, m250 = _bb_width_metrics(c)
            bb_cur.append(cur)
            bb_mean.append(m250)
        df_in["bb_width_cur"] = bb_cur
        df_in["bb_width_mean250"] = bb_mean
        df_in["bb_width_ratio"] = df_in["bb_width_cur"] / df_in["bb_width_mean250"].replace(0, np.nan)
        th = float(pd.to_numeric(df_in["bb_width_ratio"], errors="coerce").quantile(quantile))
        return df_in[df_in["bb_width_ratio"].notna() & (df_in["bb_width_ratio"] <= (th if np.isfinite(th) else 0.9))].copy()

    def _turnover_60d_avg(code):
        hist = fetcher.pro.daily_basic(
            ts_code=code,
            start_date=fetcher._to_ts_date((end_dt.date() - timedelta(days=120)).strftime("%Y-%m-%d")),
            end_date=fetcher._to_ts_date(end_date),
            fields="trade_date,ts_code,turnover_rate",
        )
        if hist is None or hist.empty or "turnover_rate" not in hist.columns:
            return np.nan
        x = pd.to_numeric(hist["turnover_rate"], errors="coerce").dropna().tail(60)
        if len(x) < 30:
            return np.nan
        return float(x.mean())

    def _calc_turnover(df_in):
        codes = df_in["ts_code"].astype(str).tolist()
        df_in["turnover_60d_avg"] = [_turnover_60d_avg(c) for c in codes]
        return df_in

    def _roe_latest(code):
        fi = fetcher.get_fina_indicator_latest(code)
        if fi is None or fi.empty:
            return np.nan
        for col in ("roe_dt", "roe"):
            if col in fi.columns:
                v = pd.to_numeric(fi[col].iloc[0], errors="coerce")
                if pd.notna(v):
                    return float(v)
        return np.nan

    def _q_rev_yu(code):
        fi = fetcher.get_fina_indicator_latest(code)
        if fi is None or fi.empty:
            return np.nan
        for col in ("q_rev_yu", "q_gr_yoy", "revenue_yoy"):
            if col in fi.columns:
                v = pd.to_numeric(fi[col].iloc[0], errors="coerce")
                if pd.notna(v):
                    return float(v)
        return np.nan

    # ════════════════════════════════════
    # 版本A：超卖反弹
    # ════════════════════════════════════
    print("\n===== 版本A：超卖反弹 =====")
    pe_ok = daily_basic_today[
        daily_basic_today["pe_ttm"].notna()
        & (daily_basic_today["pe_ttm"] >= 0)
        & (daily_basic_today["pe_ttm"] <= 80)
    ][["ts_code", "pe_ttm"]].copy()
    a1 = universe.merge(pe_ok, on="ts_code", how="inner")
    print(f"A-1 PE 0-80：{len(a1)}")
    if a1.empty:
        pool_a = pd.DataFrame()
    else:
        a1 = _calc_turnover(a1)
        a2 = a1[(a1["turnover_60d_avg"].notna()) & (a1["turnover_60d_avg"] >= 0.8) & (a1["turnover_60d_avg"] <= 5.0)].copy()
        print(f"A-2 换手 0.8%-5%：{len(a2)}")
        if a2.empty:
            pool_a = pd.DataFrame()
        else:
            a3 = _bb_filter(a2, 0.30)
            print(f"A-3 布林带收敛(30%)：{len(a3)}")
            if a3.empty:
                pool_a = pd.DataFrame()
            else:
                sig_ok, sig_day, wr_now = [], [], []
                for c in a3["ts_code"].astype(str).tolist():
                    df = fetcher.get_stock_daily(c, (end_dt.date()-timedelta(days=80)).strftime("%Y-%m-%d"), end_date)
                    ok, day, wrv = False, "", np.nan
                    if df is not None and not df.empty and len(df) >= 25:
                        df_s = df.sort_index()
                        hh = pd.to_numeric(df_s["high"], errors="coerce").rolling(14).max()
                        ll = pd.to_numeric(df_s["low"], errors="coerce").rolling(14).min()
                        wr14 = (hh - pd.to_numeric(df_s["close"], errors="coerce")) / (hh - ll).replace(0, np.nan) * 100
                        ma5 = pd.to_numeric(df_s["close"], errors="coerce").rolling(5).mean()
                        close = pd.to_numeric(df_s["close"], errors="coerce")
                        if len(wr14.dropna()) >= 3:
                            if (pd.notna(wr14.iloc[-2]) and pd.notna(wr14.iloc[-1]) and pd.notna(close.iloc[-1]) and pd.notna(ma5.iloc[-1])
                                    and wr14.iloc[-2] > 80 and wr14.iloc[-1] < 80 and close.iloc[-1] >= ma5.iloc[-1]):
                                ok, day, wrv = True, "today", float(wr14.iloc[-1])
                            elif (pd.notna(wr14.iloc[-3]) and pd.notna(wr14.iloc[-2]) and pd.notna(close.iloc[-2]) and pd.notna(ma5.iloc[-2])
                                    and wr14.iloc[-3] > 80 and wr14.iloc[-2] < 80 and close.iloc[-2] >= ma5.iloc[-2]):
                                ok, day, wrv = True, "yesterday", float(wr14.iloc[-2])
                    sig_ok.append(ok)
                    sig_day.append(day)
                    wr_now.append(wrv)
                a3["wr14_now"] = wr_now
                a3["signal_day"] = sig_day
                a4 = a3[pd.Series(sig_ok, index=a3.index)].copy()
                print(f"A-4 WR14信号：{len(a4)}")
                if a4.empty:
                    pool_a = pd.DataFrame()
                else:
                    a4["roe"] = [_roe_latest(c) for c in a4["ts_code"].astype(str).tolist()]
                    a4["roe_pass"] = a4["roe"].notna() & (a4["roe"] > 5.0)
                    a4["pool_type"] = "A-超卖反弹"
                    a4["ths_concept1"] = a4["ts_code"].map(lambda x: ths_map.get(str(x), ""))
                    a4["concept"] = a4["ths_concept1"].fillna("")
                    pool_a = a4

    # ════════════════════════════════════
    # 版本B：科技成长
    # ════════════════════════════════════
    print("\n===== 版本B：科技成长 =====")
    b1 = _calc_turnover(universe)
    b1 = b1[b1["turnover_60d_avg"].notna() & (b1["turnover_60d_avg"] >= 2.0) & (b1["turnover_60d_avg"] <= 6.0)].copy()
    print(f"B-1 换手 2%-6%：{len(b1)}")
    if b1.empty:
        pool_b = pd.DataFrame()
    else:
        rev_codes = b1["ts_code"].astype(str).tolist()
        b1["q_rev_yu"] = [_q_rev_yu(c) for c in rev_codes]
        b2 = b1[b1["q_rev_yu"].notna() & (b1["q_rev_yu"] > 20.0)].copy()
        print(f"B-2 营收增速>20%：{len(b2)}")
        if b2.empty:
            pool_b = pd.DataFrame()
        else:
            b3 = _bb_filter(b2, 0.30)
            print(f"B-3 布林带收敛(30%)：{len(b3)}")
            if b3.empty:
                pool_b = pd.DataFrame()
            else:
                codes_b4 = b3["ts_code"].astype(str).tolist()
                vol_ratio = []
                for c in codes_b4:
                    df = fetcher.get_stock_daily(c, (end_dt.date()-timedelta(days=45)).strftime("%Y-%m-%d"), end_date)
                    if df is None or df.empty or "volume" not in df.columns or len(df) < 4:
                        vol_ratio.append(np.nan)
                    else:
                        v = pd.to_numeric(df["volume"], errors="coerce").dropna()
                        if len(v) < 4:
                            vol_ratio.append(np.nan)
                        else:
                            tail_v = v.tail(20)
                            denom = tail_v.mean()
                            if pd.isna(denom) or denom <= 1e-9:
                                vol_ratio.append(np.nan)
                            else:
                                vol_ratio.append(float(v.tail(3).mean() / denom))
                b3["vol_ratio_3d"] = vol_ratio
                b4 = b3[b3["vol_ratio_3d"].notna() & (b3["vol_ratio_3d"] >= 1.3)].copy()
                print(f"B-4 量比>1.3：{len(b4)}")
                if b4.empty:
                    pool_b = pd.DataFrame()
                else:
                    b4["roe"] = [_roe_latest(c) for c in b4["ts_code"].astype(str).tolist()]
                    b4["roe_pass"] = b4["roe"].notna() & (b4["roe"] > 5.0)
                    b4["pool_type"] = "B-科技成长"
                    b4["wr14_now"] = np.nan
                    b4["signal_day"] = ""
                    b4["ths_concept1"] = b4["ts_code"].map(lambda x: ths_map.get(str(x), ""))
                    b4["concept"] = b4["ths_concept1"].fillna("")
                    b4["bb_width_cur"] = np.nan
                    b4["bb_width_mean250"] = np.nan
                    b4["bb_width_ratio"] = np.nan
                    pool_b = b4

    # ════════════════════════════════════
    # 合并 + 输出
    # ════════════════════════════════════
    parts = []
    if not pool_a.empty:
        parts.append(pool_a)
    if not pool_b.empty:
        parts.append(pool_b)
    if not parts:
        print("\n⚠ 双池均为空")
        return pd.DataFrame()

    final_df = pd.concat(parts, ignore_index=True)
    final_df["score_raw"] = 0.0
    final_df["score_norm"] = 0.0

    final_df["strategy"] = "量化蓄势突破"
    final_df["strategy_short"] = "蓄势"
    final_df["latest_trade_date"] = end_ymd
    final_df["pe_ttm"] = final_df["ts_code"].map(
        lambda x: float(daily_basic_today[daily_basic_today["ts_code"]==x]["pe_ttm"].iloc[0])
        if x in daily_basic_today["ts_code"].values else np.nan
    )
    final_df["turnover_rate"] = final_df["ts_code"].map(
        lambda x: float(daily_basic_today[daily_basic_today["ts_code"]==x]["turnover_rate"].iloc[0])
        if x in daily_basic_today["ts_code"].values else np.nan
    )

    out = final_df.sort_values(["pool_type", "score_norm"], ascending=[True, False]).reset_index(drop=True)
    out = out.head(max(int(top_n), 1))

    out_dir = Path("output/daily")
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = str(end_date).replace("-", "")
    export_suffix = os.getenv("QUANT_EXPORT_SUFFIX", "").strip()
    file_mid = f"daily_selection_{tag}{export_suffix}"
    csv_en = out_dir / f"{file_mid}.csv"
    csv_cn = out_dir / f"{file_mid}_cn.csv"
    out.to_csv(csv_en, index=False, encoding="utf-8-sig")
    to_chinese_columns(out.copy()).to_csv(csv_cn, index=False, encoding="utf-8-sig")
    print(f"\n已导出: {csv_en.resolve()}")
    print(f"        {csv_cn.resolve()}")
    return out


def _build_ths_map(fetcher) -> dict[str, str]:
    """从 ths_index / ths_member 反向映射同花顺概念1"""
    m: dict[str, str] = {}
    try:
        idx = fetcher.pro.ths_index()
        if idx is None or idx.empty or "ts_code" not in idx.columns or "name" not in idx.columns:
            return m
        pairs: list[tuple[str, str]] = []
        for _, r in idx.head(200).iterrows():
            c, nm = str(r["ts_code"]), str(r["name"])
            try:
                mem = fetcher.pro.ths_member(ts_code=c)
            except Exception:
                continue
            if mem is None or mem.empty or "con_code" not in mem.columns:
                continue
            for code in mem["con_code"].astype(str).tolist():
                pairs.append((code, nm))
        if pairs:
            inv = pd.DataFrame(pairs, columns=["ts_code", "concept1"])
            inv = inv.sort_values(["ts_code", "concept1"]).drop_duplicates(subset=["ts_code"], keep="first")
            m = dict(zip(inv["ts_code"], inv["concept1"]))
    except Exception:
        pass
    return m
