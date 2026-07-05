#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""笔记哥选股器 — 按6种量价模式筛选A股

流程（严格按笔记哥原话）：
  1. 从全市场搜"放量长阳"前50名
  2. 在50只中看是否有15天内有涨停、且未跌破涨停日最低价、量不能太大 → 模式2
  3. 对同时满足1+2的，再检查是否有模式3/4/5/6任一一条

数据源：
  - 默认: 本地 Parquet 缓存（data/daily/*.parquet + data/static/stock_basic.parquet）
  - 可选: mootdx 通达信实时数据（需安装 mootdx，本地调试用）
"""

import sys, argparse, time, warnings, os, pickle, random, json, csv as _csv
import numpy as np
import pandas as pd

# ── 可选 mootdx 导入（仅在非 Parquet 模式下使用）──
try:
    from mootdx.quotes import Quotes
    _HAS_MOOTDX = True
except ImportError:
    _HAS_MOOTDX = False
    Quotes = None  # type: ignore

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
warnings.filterwarnings("ignore")

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(SKILL_DIR, ".stock_cache.pkl")


# ═══════════════════════════════════════════════════════
#  STAGE 0 — 辅助函数
# ═══════════════════════════════════════════════════════

def _pct(v):
    return f"{v*100:+.1f}%"


def _add_exchange_suffix(code: str) -> str:
    """给6位代码加上交易所后缀 .SH / .SZ"""
    if code.startswith(("60", "68")):
        return code + ".SH"
    return code + ".SZ"


# ── Parquet 数据源（默认，无需网络）─────────────────────────

def _get_stock_list_parquet(data_dir: str) -> list[tuple[str, str]]:
    """从 stock_basic.parquet 读取全市场股票列表。"""
    p = os.path.join(data_dir, "static", "stock_basic.parquet")
    if not os.path.exists(p):
        raise RuntimeError(f"stock_basic.parquet 不存在: {p}，请先运行预拉取")
    df = pd.read_parquet(p)
    # 过滤掉北交所
    df = df[~df["ts_code"].str.endswith(".BJ")]
    # 返回 (6位代码, 名称) 列表
    result = []
    for _, row in df.iterrows():
        code = str(row["ts_code"]).replace(".SH", "").replace(".SZ", "")
        if len(code) == 6 and code.isdigit():
            result.append((code, str(row.get("name", "?"))))
    return result


def _build_parquet_daily_cache(data_dir: str, days: int = 90) -> dict[str, pd.DataFrame]:
    """预加载最近 N 个交易日的 daily 分区，按 ts_code 索引。

    返回: {ts_code: DataFrame(90-day K-line)}
    """
    daily_dir = os.path.join(data_dir, "daily")
    if not os.path.isdir(daily_dir):
        return {}

    files = sorted(
        f for f in os.listdir(daily_dir)
        if f.endswith(".parquet") and len(f.replace(".parquet", "")) == 8
    )[-days:]

    if not files:
        return {}

    # 读取并拼接所有分区
    frames = []
    for fn in files:
        try:
            df = pd.read_parquet(os.path.join(daily_dir, fn))
            if len(df) >= 500:  # 全市场分区，至少 500 行
                frames.append(df)
        except Exception:
            continue

    if not frames:
        return {}

    all_data = pd.concat(frames, ignore_index=True)
    all_data = all_data.sort_values(["ts_code", "trade_date"])

    # 按 ts_code 分组
    cache: dict[str, pd.DataFrame] = {}
    for code, group in all_data.groupby("ts_code"):
        # 只用最近 days 个交易日
        cache[str(code)] = group.tail(days).reset_index(drop=True)

    print(f"  Parquet 日线缓存就绪: {len(cache)} 只股票, {len(files)} 个交易日分区")
    return cache


def _get_kline_parquet(code: str, daily_cache: dict) -> pd.DataFrame:
    """从本地 Parquet 缓存获取单只股票 K 线（带交易所后缀匹配）。"""
    for suffix in (".SH", ".SZ", ""):
        key = code + suffix
        df = daily_cache.get(key)
        if df is not None and len(df) >= 20:
            return df
    # 纯6位代码匹配
    df = daily_cache.get(code)
    if df is not None and len(df) >= 20:
        return df
    return None


def _get_index_kline_parquet(data_dir: str) -> pd.DataFrame:
    """从 index_daily 分区读取上证指数日线（999999.SH / 000001.SH）。"""
    idx_dir = os.path.join(data_dir, "index_daily")
    if not os.path.isdir(idx_dir):
        return None
    files = sorted(f for f in os.listdir(idx_dir) if f.endswith(".parquet"))[-90:]
    if not files:
        return None
    frames = []
    for fn in files:
        try:
            df = pd.read_parquet(os.path.join(idx_dir, fn))
            frames.append(df)
        except Exception:
            continue
    if not frames:
        return None
    all_idx = pd.concat(frames, ignore_index=True)
    # 匹配 000001.SH（上证指数）
    idx_df = all_idx[all_idx["ts_code"] == "000001.SH"].sort_values("trade_date").reset_index(drop=True)
    if idx_df.empty:
        idx_df = all_idx[all_idx["ts_code"] == "999999.SH"].sort_values("trade_date").reset_index(drop=True)
    if idx_df.empty or len(idx_df) < 5:
        return None
    return idx_df.tail(90).reset_index(drop=True)


# ── mootdx 数据源（可选，本地调试用）─────────────────────────

def _get_stock_list_mootdx() -> list[tuple[str, str]]:
    """通过 mootdx 获取全市场股票(代码,名称)列表，优先用缓存。"""
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "rb") as f:
            return pickle.load(f)

    if not _HAS_MOOTDX:
        raise RuntimeError("mootdx 未安装，请使用 --data-dir 指定 Parquet 数据目录")

    print("首次运行，通过 mootdx 获取股票列表（~20s）...")
    q = Quotes.factory(market="std", timeout=30)
    blocks = q.block()
    if blocks is not None and len(blocks) > 0:
        stock_rows = blocks[blocks['code'].str.match(r'^\d{6}$', na=False)]
        seen = set()
        uniq = []
        for _, row in stock_rows.iterrows():
            code = row['code']
            if code not in seen:
                seen.add(code)
                uniq.append((code, "?"))
        print(f"获取到 {len(uniq)} 只股票")
        with open(CACHE_FILE, "wb") as f:
            pickle.dump(uniq, f)
        return uniq

    raise RuntimeError("无法获取股票列表")


def _get_kline_mootdx(q, code, days=90):
    try:
        df = q.bars(symbol=code, frequency=9, start=0, offset=days)
        if df is None or len(df) < 20:
            return None
        if isinstance(df.index, pd.DatetimeIndex):
            df = df.sort_index()
        for col in ["open", "close", "high", "low", "volume"]:
            df[col] = df[col].astype(float)
        return df
    except Exception:
        return None


# ═══════════════════════════════════════════════════════
#  STAGE 1 — "放量长阳" 前 N 名
# ═══════════════════════════════════════════════════════

def scan_fangliang_changyang(
    stock_list: list[tuple[str, str]],
    top_n: int = 50,
    gain_min: float = 0.03,
    vol_ratio_min: float = 1.5,
    body_ratio_min: float = 0.6,
    max_lower_shadow: float = 0.3,
    daily_cache: dict = None,
) -> list[dict]:
    """
    在全市场中找到"放量长阳"前 top_n 名。
    排序依据：涨幅 × 量比 的综合得分。

    Args:
        daily_cache: Parquet 预加载的日线缓存 {ts_code: DataFrame}。
                     为 None 时使用 mootdx 逐只拉取（慢）。
    """
    if daily_cache is None and _HAS_MOOTDX:
        q = Quotes.factory(market="std", multithread=True, heartbeat=True, timeout=20)
    candidates = []
    t0 = time.time()
    total = len(stock_list)

    for i, (code, name) in enumerate(stock_list):
        if (i + 1) % 500 == 0:
            elapsed = time.time() - t0
            print(f"  扫描放量长阳: {i+1}/{total}  ({elapsed:.0f}s)  候选: {len(candidates)}", end="\r")
            sys.stdout.flush()

        if daily_cache is not None:
            df = _get_kline_parquet(code, daily_cache)
        else:
            df = _get_kline_mootdx(q, code, days=90)

        if df is None or len(df) < 60:
            continue

        tdy = df.iloc[-1]
        prev = df.iloc[-2]
        chg = (tdy["close"] - prev["close"]) / prev["close"]
        if chg <= gain_min:
            continue

        vol_ma20 = df["volume"].rolling(20).mean().iloc[-2]
        if tdy["volume"] <= vol_ma20 * vol_ratio_min:
            continue

        body = abs(tdy["close"] - tdy["open"])
        total_range = tdy["high"] - tdy["low"]
        if total_range < 0.001:
            continue
        if body / total_range < body_ratio_min:
            continue

        if tdy["close"] <= tdy["open"]:
            continue
        lower_shadow = tdy["open"] - tdy["low"]
        if body > 0 and lower_shadow / body > max_lower_shadow:
            continue

        ma60 = df["close"].rolling(60).mean().iloc[-1]
        if tdy["close"] < ma60 * 0.95:
            continue

        score = chg * (tdy["volume"] / vol_ma20)
        candidates.append({
            "code": code,
            "name": name,
            "gain": chg,
            "vol_ratio": tdy["volume"] / vol_ma20,
            "score": score,
            "df": df,
        })

    elapsed = time.time() - t0
    print(f"\n  扫描完成: {total}只 | {elapsed:.0f}s | 放量长阳候选: {len(candidates)}")

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:top_n]


# ═══════════════════════════════════════════════════════
#  STAGE 2 — 检查模式2（一进二）
# ═══════════════════════════════════════════════════════

def check_mode2(df: pd.DataFrame) -> bool:
    """
    15天内有过涨停(>=9.8%)
    当前价未跌破最近涨停日的最低价
    涨停至今日均量不超过涨停日量的 3 倍
    """
    if len(df) < 15:
        return False
    recent = df.tail(15)
    n = len(recent)
    for i in range(n - 1, 0, -1):
        chg = (recent.iloc[i]["close"] - recent.iloc[i - 1]["close"]) / recent.iloc[i - 1]["close"]
        if chg >= 0.098:
            # iloc position of limit-up day in original df
            limit_up_iloc = len(df) - n + i
            limit_up_row = df.iloc[limit_up_iloc]
            # 当前价 vs 涨停日最低价
            if df.iloc[-1]["close"] < limit_up_row["low"]:
                return False
            # 量能检查：涨停日后的日均量
            after = df.iloc[limit_up_iloc + 1:]
            if len(after) > 0 and after["volume"].mean() > limit_up_row["volume"] * 3:
                return False
            return True
    return False


# ═══════════════════════════════════════════════════════
#  STAGE 3 — 检查模式 3/4/5/6
# ═══════════════════════════════════════════════════════

def check_mode3(df: pd.DataFrame) -> bool:
    """横盘突破涨停：涨停前5天横盘(日涨跌<=2%)"""
    if len(df) < 8:
        return False
    tdy, prev = df.iloc[-1], df.iloc[-2]
    if (tdy["close"] - prev["close"]) / prev["close"] < 0.098:
        return False
    # 涨停前 5 天（从 -7 到 -3，-2 是昨天，-1 是今天涨停）
    cons_iloc_start = len(df) - 7
    cons_iloc_end = len(df) - 3
    for j in range(cons_iloc_start, cons_iloc_end):
        prev_close = df.iloc[j - 1]["close"] if j > 0 else df.iloc[0]["close"]
        dchg = abs((df.iloc[j]["close"] - prev_close) / prev_close)
        if dchg > 0.02:
            return False
    return True


def check_mode4(df: pd.DataFrame) -> bool:
    """涨停突破前高：涨停 + 突破60日最高价"""
    if len(df) < 60:
        return False
    tdy, prev = df.iloc[-1], df.iloc[-2]
    if (tdy["close"] - prev["close"]) / prev["close"] < 0.098:
        return False
    return float(tdy["high"]) > df.iloc[-61:-1]["high"].max()


def check_mode5_simple(df: pd.DataFrame) -> bool:
    """
    强势股回调确认底部（简化版）
    - 近期（5天内）有过明显回调（至少一天跌超1.5%）
    - 最近2天企稳（不再大跌）
    - 今天收阳（止跌信号）
    """
    if len(df) < 8:
        return False
    
    five = df.iloc[-7:-2]  # 第 -7 到 -3 天（不含近2天）
    # 必须有过明显回调
    had_significant_pullback = False
    for j in range(1, len(five)):
        chg = (five.iloc[j]["close"] - five.iloc[j - 1]["close"]) / five.iloc[j - 1]["close"]
        if chg < -0.015:
            had_significant_pullback = True
            break
    if not had_significant_pullback:
        return False
    
    # 近2天企稳（不能跌超2%）
    last2 = df.iloc[-4:-1]  # 昨天和前天
    for j in range(1, len(last2)):
        if (last2.iloc[j]["close"] - last2.iloc[j - 1]["close"]) / last2.iloc[j - 1]["close"] < -0.02:
            return False
    
    # 今天必须是阳线
    tdy = df.iloc[-1]
    if tdy["close"] <= tdy["open"]:
        return False
    
    return True


def check_mode5_with_index(df, idx_df):
    """强势股回调确认底部（含大盘对比）"""
    if idx_df is None or len(idx_df) < 5:
        return check_mode5_simple(df)

    # 大盘对比：近5天内至少1天大盘跌超-0.5%但个股逆势
    strong_vs_market = False
    for i in range(1, min(6, min(len(df), len(idx_df)))):
        sc = (df.iloc[-i]["close"] - df.iloc[-i - 1]["close"]) / df.iloc[-i - 1]["close"]
        ic = (idx_df.iloc[-i]["close"] - idx_df.iloc[-i - 1]["close"]) / idx_df.iloc[-i - 1]["close"]
        if ic < -0.005 and sc >= -0.003:
            strong_vs_market = True
            break
    if not strong_vs_market:
        return False

    return check_mode5_simple(df)


def check_mode6(df: pd.DataFrame) -> bool:
    """背离抄底：近3天连跌，第3天缩量但跌幅更大"""
    if len(df) < 4:
        return False
    r = df.iloc[-4:]
    chgs = [(r.iloc[j]["close"] - r.iloc[j - 1]["close"]) / r.iloc[j - 1]["close"] for j in range(1, 4)]
    if not all(c < 0 for c in chgs):
        return False
    if float(r.iloc[-1]["volume"]) < float(r.iloc[-2]["volume"]) and chgs[2] < chgs[1]:
        return True
    return False


# ═══════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════

def _add_exchange_suffix(code: str) -> str:
    """给6位代码加上交易所后缀 .SH / .SZ"""
    if code.startswith(("60", "68")):
        return code + ".SH"
    return code + ".SZ"


def _write_empty_csv(path: str):
    """写入空结果 CSV（仅表头），让聚合器知道'今天跑了但没选到'"""
    if not path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as _f:
        _w = _csv.writer(_f)
        _w.writerow(["ts_code", "name", "gain", "vol_ratio", "tags"])
    print(f"[CSV] 空结果已保存 → {path}")


def main():
    parser = argparse.ArgumentParser(description="笔记哥选股器")
    parser.add_argument("--top", type=int, default=50, help="放量长阳取前N名（默认50）")
    parser.add_argument("--full", action="store_true", help="扫描全市场（慢）")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--output-csv", type=str, default="", help="输出结果到 CSV 文件路径")
    parser.add_argument("--data-dir", type=str, default="",
                        help="Parquet 数据目录（默认自动检测: ../data）")
    args = parser.parse_args()

    # ── 确定数据目录 ──
    if not args.data_dir:
        # 自动检测: 优先用 ../data（run_all.sh 从 strategies/bijige/ 运行）
        auto_dirs = [
            os.path.join(SKILL_DIR, "..", "..", "data"),          # strategies/bijige/ → ../../data/
            os.path.join(SKILL_DIR, "data"),                       # Fallback
        ]
        for d in auto_dirs:
            if os.path.isdir(os.path.join(d, "daily")):
                args.data_dir = os.path.abspath(d)
                break

    if args.data_dir and os.path.isdir(os.path.join(args.data_dir, "daily")):
        print(f"数据源: Parquet ({args.data_dir})")
        # ── 股票列表 ──
        stock_list = _get_stock_list_parquet(args.data_dir)
        # ── 预加载日线缓存 ──
        daily_cache = _build_parquet_daily_cache(args.data_dir, days=90)
        # ── 大盘指数 ──
        idx_df = _get_index_kline_parquet(args.data_dir)
    elif _HAS_MOOTDX:
        print("数据源: mootdx (通达信)")
        stock_list = _get_stock_list_mootdx()
        daily_cache = None
        q = Quotes.factory(market="std", timeout=20)
        idx_df = _get_kline_mootdx(q, "999999", days=90)
    else:
        print("错误: 未找到 Parquet 数据且 mootdx 未安装")
        print("  用法: python pick.py --data-dir /path/to/统一输出/data")
        print("  或安装 mootdx: pip install mootdx")
        return

    if not stock_list:
        print("错误: 股票列表为空")
        return

    if not daily_cache:
        print("错误: 日线缓存为空")
        return

    # 非全量模式：随机采样
    if not args.full and len(stock_list) > 1500:
        random.seed(int(time.time()))
        stock_list = random.sample(stock_list, 1500)
        print(f"快速模式：随机采样 {len(stock_list)} 只（用 --full 扫描全市场）")

    # ── Stage 1：放量长阳前 N ──
    print("\n========== Stage 1: 放量长阳前{}名 ==========".format(args.top))
    candidates = scan_fangliang_changyang(stock_list, top_n=args.top, daily_cache=daily_cache)
    if not candidates:
        print("[!] 未找到放量长阳股票")
        _write_empty_csv(args.output_csv)
        return
    print(f"放量长阳 Top {len(candidates)}:\n")
    for i, c in enumerate(candidates):
        print(f"  {i+1:2d}. {c['code']} {c['name']:6s}  gain={_pct(c['gain'])}  vol_ratio={c['vol_ratio']:.1f}x")

    # ── Stage 2：模式2（一进二）─
    print(f"\n========== Stage 2: 检查模式2（一进二）==========")
    m12 = []
    for c in candidates:
        if check_mode2(c["df"]):
            m12.append(c)
            print(f"  [M2 PASS] {c['code']} {c['name']}")
    if not m12:
        print("[!] 没有股票同时满足 放量长阳 + 一进二")
        _write_empty_csv(args.output_csv)
        return

    # ── Stage 3：模式 3/4/5/6 ─
    print(f"\n========== Stage 3: 检查模式 3/4/5/6 ==========")
    final = []
    for c in m12:
        tags = ["M1", "M2"]
        df = c["df"]
        m3, m4, m5, m6 = False, False, False, False
        m3 = check_mode3(df)
        m4 = check_mode4(df)
        m5 = check_mode5_with_index(df, idx_df)
        m6 = check_mode6(df)
        if m3: tags.append("M3(横盘突破)")
        if m4: tags.append("M4(突破前高)")
        if m5: tags.append("M5(强势回调)")
        if m6: tags.append("M6(背离抄底)")
        if m3 or m4 or m5 or m6:
            c["tags"] = tags
            final.append(c)
            tag_str = " | ".join(tags)
            print(f"  [HIT] {c['code']} {c['name']:6s}  gain={_pct(c['gain'])}  vol={c['vol_ratio']:.1f}x  -> {tag_str}")

    # ── 输出 ─
    if not final:
        print("\n[!] 同时满足 放量长阳 + 一进二 但没有额外匹配模式3/4/5/6")
        _write_empty_csv(args.output_csv)
        return

    print(f"\n{'='*60}")
    print(f"  最终结果：{len(final)} 只")
    print(f"{'='*60}")
    print(f"{'代码':<8} {'名称':<8} {'涨幅':>8} {'量比':>6}  {'满足模式'}")
    print("-" * 60)
    for c in final:
        print(f"{c['code']:<8} {c['name']:<8} {_pct(c['gain']):>8} {c['vol_ratio']:>5.1f}x  {' | '.join(c['tags'])}")

    # ── CSV 输出 ──
    if args.output_csv and final:
        os.makedirs(os.path.dirname(os.path.abspath(args.output_csv)) or ".", exist_ok=True)
        with open(args.output_csv, "w", newline="", encoding="utf-8-sig") as _f:
            _w = _csv.writer(_f)
            _w.writerow(["ts_code", "name", "gain", "vol_ratio", "tags"])
            for c in final:
                _w.writerow([
                    _add_exchange_suffix(c["code"]),
                    c["name"],
                    round(c["gain"] * 100, 2),
                    round(c["vol_ratio"], 2),
                    " | ".join(c["tags"]),
                ])
        print(f"\n[CSV] 已保存 → {args.output_csv} ({len(final)} 只)")


if __name__ == "__main__":
    main()
