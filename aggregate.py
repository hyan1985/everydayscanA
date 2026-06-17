#!/usr/bin/env python3
"""
统一选股输出聚合器
从五个独立选股项目读取已有输出文件（零侵入），归一化后生成统一 HTML 看板 + 微信转发文本。
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.yaml"
OUTPUT_DIR = SCRIPT_DIR / "output"
DATA_DIR = SCRIPT_DIR / "data"


# ── 0. 市场环境检测 ─────────────────────────────────────────

class MarketCondition:
    """封装当日市场环境状态。"""
    def __init__(self, data_dir: Path, trade_date: str):
        self.trade_date = trade_date
        self.moneyflow_date = trade_date
        self.data_dir = Path(data_dir)
        self.advance_count = 0    # 上涨家数（pct_chg > 0）
        self.decline_count = 0    # 下跌家数（pct_chg < 0）
        self.flat_count = 0       # 平盘家数
        self.adv_decline_ratio = 0.0  # 涨跌比 = 上涨/下跌
        self.net_mf_amount = 0.0      # 全市场主力净流入合计（万元，负=净流出）
        self.net_mf_yi = 0.0          # 亿元展示值（保留两位）
        self.is_weak = False          # 是否为弱势/调整行情
        self.weak_reason = ""         # 触发弱势的原因描述
        self.required_resonance = 2   # 共振门槛固定为2
        self._load()

    def _load(self):
        """从本地 Parquet 读取 daily 和 moneyflow 数据，计算市场指标。"""
        # ── 涨跌家数：仅统计涨/跌，平盘单独列出（与行情软件口径一致）──
        daily_path = self.data_dir / "daily" / f"{self.trade_date}.parquet"
        if daily_path.exists():
            try:
                daily = pd.read_parquet(daily_path)
                if "pct_chg" in daily.columns:
                    pct = pd.to_numeric(daily["pct_chg"], errors="coerce")
                    self.advance_count = int((pct > 0).sum())
                    self.decline_count = int((pct < 0).sum())
                    self.flat_count = int((pct == 0).sum())
                    if self.decline_count > 0:
                        self.adv_decline_ratio = round(
                            self.advance_count / self.decline_count, 2
                        )
                    else:
                        self.adv_decline_ratio = 99.0
            except Exception:
                pass

        # ── 全市场主力资金：个股 net_mf_amount 加总（万元），与东财等软件可能差几个点 ──
        mf_dir = self.data_dir / "moneyflow"
        mf_dates = sorted(d.stem for d in mf_dir.glob("*.parquet") if d.stem <= self.trade_date)
        if self.trade_date in mf_dates:
            mf_pick = self.trade_date
        elif mf_dates:
            mf_pick = mf_dates[-1]
        else:
            mf_pick = ""
        self.moneyflow_date = mf_pick
        if mf_pick:
            mf_path = mf_dir / f"{mf_pick}.parquet"
            try:
                mf = pd.read_parquet(mf_path)
                if "net_mf_amount" in mf.columns:
                    self.net_mf_amount = round(
                        pd.to_numeric(mf["net_mf_amount"], errors="coerce").fillna(0).sum(), 0
                    )
                    self.net_mf_yi = round(self.net_mf_amount / 10000, 2)
            except Exception:
                pass

        # ── 综合判断弱势行情 ──
        reasons = []
        if self.advance_count or self.decline_count:
            reasons.append(
                f"涨{self.advance_count}跌{self.decline_count}"
                f"（涨跌比{self.adv_decline_ratio}）"
            )
        elif 0 < self.adv_decline_ratio < 0.7:
            reasons.append(f"涨跌比 {self.adv_decline_ratio}（<0.7）")
        if self.net_mf_yi < -80:
            reasons.append(f"资金净流出 {abs(self.net_mf_yi):.2f}亿")
        elif self.net_mf_amount < -800000:
            reasons.append(f"资金净流出 {abs(self.net_mf_amount/10000):.0f}亿（>80亿）")

        td = self.trade_date
        td_label = f"{td[:4]}-{td[4:6]}-{td[6:8]}" if len(td) == 8 else td
        if reasons:
            self.is_weak = True
            self.weak_reason = f"统计日 {td_label}：" + "；".join(reasons)
        else:
            self.is_weak = False
            self.weak_reason = f"统计日 {td_label}：市场偏强"


def _open_trade_dates_upto(end_date: str) -> list[str]:
    """从 trade_cal 读取不晚于 end_date 的开市日列表。"""
    cal_path = DATA_DIR / "static" / "trade_cal.parquet"
    if not cal_path.exists():
        return []
    try:
        cal = pd.read_parquet(cal_path)
        if cal.empty or "is_open" not in cal.columns:
            return []
        mask = (cal["cal_date"].astype(str) <= end_date) & (
            cal["is_open"].astype(int) == 1
        )
        return sorted(cal.loc[mask, "cal_date"].astype(str).tolist())
    except Exception:
        return []


def _refresh_trade_cal_if_stale() -> None:
    """仅单独运行 aggregate.py 时的兜底；正常应已由 run_all 步骤 0/聚合前刷新。"""
    try:
        from quant_data.fetcher import _get_pro, ensure_trade_cal_current

        ensure_trade_cal_current(_get_pro(), quiet=False)
    except Exception as exc:
        print(f"  [日历] trade_cal 刷新失败: {exc}")


def _resolve_latest_parquet_date(subdir: str, hint: str = "", open_days_only: bool = True) -> str:
    """取 Parquet 分区目录中的最新交易日；默认仅限开市日，跳过误拉的日历日文件。"""
    base = DATA_DIR / subdir
    if not base.exists():
        return str(hint or "").strip()
    dates = sorted(
        d.stem
        for d in base.glob("*.parquet")
        if len(d.stem) == 8 and d.stem.isdigit()
    )
    if not dates:
        return str(hint or "").strip()
    if open_days_only:
        open_set = set(_open_trade_dates_upto(datetime.now().strftime("%Y%m%d")))
        if open_set:
            dates = [d for d in dates if d in open_set]
    return dates[-1] if dates else ""


def _resolve_market_trade_date(hint: str = "") -> str:
    """市场环境：最近开市日，且本地必须有 daily 分区。"""
    _refresh_trade_cal_if_stale()
    today = datetime.now().strftime("%Y%m%d")
    open_dates = _open_trade_dates_upto(today)
    daily_dates = sorted(
        d.stem
        for d in (DATA_DIR / "daily").glob("*.parquet")
        if len(d.stem) == 8 and d.stem.isdigit()
    )
    candidates = []
    if open_dates:
        for d in reversed(open_dates):
            if (DATA_DIR / "daily" / f"{d}.parquet").exists():
                candidates.append(d)
                break
    if daily_dates:
        dmax = daily_dates[-1]
        if dmax in (open_dates or [dmax]) and (DATA_DIR / "daily" / f"{dmax}.parquet").exists():
            if not candidates or dmax > candidates[0]:
                candidates = [dmax]
    return candidates[0] if candidates else (open_dates[-1] if open_dates else "")


def check_market_condition(trade_date: str) -> MarketCondition:
    """便捷入口：从已缓存的 Parquet 数据检测市场环境。"""
    resolved = _resolve_market_trade_date(trade_date)
    return MarketCondition(DATA_DIR, resolved)


def _market_net_outflow_yi(mc: MarketCondition) -> str:
    if mc.net_mf_yi:
        return f"{abs(mc.net_mf_yi):.2f}"
    return f"{abs(mc.net_mf_amount / 10000):.0f}"


def _market_breadth_text(mc: MarketCondition) -> str:
    if mc.advance_count or mc.decline_count:
        return f"涨{mc.advance_count}跌{mc.decline_count}（涨跌比{mc.adv_decline_ratio}）"
    return f"涨跌比 {mc.adv_decline_ratio}"


# ── 0.5 弱势行情辅助分析（超跌反弹 + 防守板块）───────────

# 防守板块关键词（公用事业、高股息等）
_DEFENSIVE_SECTORS = {
    "电力": "电力",
    "水务": "水务",
    "燃气": "燃气",
    "高速公路": "高速公路",
    "银行": "银行",
    "保险": "保险",
    "煤炭": "煤炭",
    "石油": "石油石化",
    "公用事业": "公用事业",
    "交通运输": "交通运输",
    "电信运营": "电信",
    "高股息股": "高股息",
    "红利": "红利",
}


def compute_oversold_rebound_candidates(trade_date: str, top_n: int = 10) -> list[dict]:
    """计算超跌反弹候选股：高乖离率（偏离均线远）。

    乖离率 (BIAS) = (close - ma20) / ma20 * 100, 负值越大=超跌越严重。
    """
    daily_path = DATA_DIR / "daily" / f"{trade_date}.parquet"
    if not daily_path.exists():
        return []

    try:
        daily_today = pd.read_parquet(daily_path)
    except Exception:
        return []

    if daily_today.empty:
        return []

    # 读取最近 20 个交易日的 daily 数据计算 MA20
    from quant_data import storage as s
    cal = s.read_static("trade_cal")
    if cal is None or cal.empty:
        return []
    open_dates = sorted(
        cal[cal["cal_date"].astype(str) <= trade_date]["cal_date"].astype(str).tolist()
    )[-22:]
    open_dates = [d for d in open_dates if d <= trade_date]

    # 读取多天 daily 数据
    daily_frames = []
    for d in open_dates:
        dp = DATA_DIR / "daily" / f"{d}.parquet"
        if dp.exists():
            try:
                dfd = pd.read_parquet(dp)
                daily_frames.append(dfd)
            except Exception:
                continue
    if len(daily_frames) < 5:
        return []

    all_daily = pd.concat(daily_frames, ignore_index=True)
    all_daily = all_daily.sort_values(["ts_code", "trade_date"])

    # 对每只股票计算滚动 MA20
    grouped = all_daily.groupby("ts_code")["close"]
    ma20_series = grouped.transform(lambda x: x.rolling(window=20, min_periods=5).mean())

    all_daily["ma20"] = ma20_series

    # 筛选当日数据
    td_normalized = trade_date.replace("-", "")
    today_mask = all_daily["trade_date"].astype(str).str.replace("-", "").str[:8] == td_normalized
    today_df = all_daily[today_mask].copy()

    # 合并 stock_basic 获取 name
    static_path = DATA_DIR / "static" / "stock_basic.parquet"
    if static_path.exists():
        try:
            basic = pd.read_parquet(static_path)
            today_df = today_df.merge(basic[["ts_code", "name"]], on="ts_code", how="left")
        except Exception:
            today_df["name"] = ""

    candidates = []
    for _, row in today_df.iterrows():
        close = row.get("close", 0)
        ma20_val = row.get("ma20", None)
        if not ma20_val or pd.isna(ma20_val) or close <= 0 or ma20_val <= 0:
            continue

        bias = (close - ma20_val) / ma20_val * 100

        if bias <= -8:
            candidates.append({
                "ts_code": row["ts_code"],
                "name": row.get("name", ""),
                "close": round(close, 2),
                "bias": round(bias, 2),
                "pct_chg": round(row.get("pct_chg", 0), 2),
                "turnover_rate": round(row.get("turnover_rate", 0), 2) if pd.notna(row.get("turnover_rate")) else 0,
                "reason": "超跌偏离",
            })

    candidates.sort(key=lambda x: x["bias"])
    return candidates[:top_n]


def compute_defensive_sector_stocks(trade_date: str, top_per_sector: int = 3) -> list[dict]:
    """找出防守板块（高股息、公用事业、电力等）中今日表现较强的个股。

    筛选条件：防守板块中 pct_chg > 0 且量价正常的标的。
    """
    daily_path = DATA_DIR / "daily" / f"{trade_date}.parquet"
    if not daily_path.exists():
        return []

    # 获取 stock_basic 的行业分类
    static = pd.read_parquet(DATA_DIR / "static" / "stock_basic.parquet") if (DATA_DIR / "static" / "stock_basic.parquet").exists() else pd.DataFrame()

    try:
        daily = pd.read_parquet(daily_path)
    except Exception:
        return []

    if daily.empty:
        return []

    # 合并行业信息
    if not static.empty and "industry" in static.columns:
        daily = daily.merge(static[["ts_code", "name", "industry"]], on="ts_code", how="left")
    else:
        daily["name"] = ""
        daily["industry"] = ""

    daily["pct_chg"] = pd.to_numeric(daily.get("pct_chg", 0), errors="coerce").fillna(0)
    daily["amount"] = pd.to_numeric(daily.get("amount", 0), errors="coerce").fillna(0)

    # 匹配防守板块关键词
    def _match_defensive(ind: str) -> str:
        if pd.isna(ind):
            return ""
        for kw, label in _DEFENSIVE_SECTORS.items():
            if kw in ind:
                return label
        return ""

    daily["_defensive"] = daily["industry"].apply(_match_defensive)
    defensive = daily[daily["_defensive"] != ""].copy()

    if defensive.empty:
        return []

    results = []
    for sector_label in sorted(set(defensive["_defensive"])):
        sector_df = defensive[defensive["_defensive"] == sector_label]
        # 筛选涨幅为正且成交活跃的
        sector_df = sector_df[sector_df["pct_chg"] > 0].sort_values("pct_chg", ascending=False)
        for _, row in sector_df.head(top_per_sector).iterrows():
            results.append({
                "ts_code": row["ts_code"],
                "name": row.get("name", ""),
                "sector": sector_label,
                "pct_chg": round(row["pct_chg"], 2),
                "amount": round(row["amount"] / 10000, 1),  # 亿元
            })

    return results


# ── 1. 配置加载 ──────────────────────────────────────────────

def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    # 支持 %SCRIPT_DIR% 变量替换
    script_dir_str = str(SCRIPT_DIR)
    for s in raw.get("strategies", []):
        ws = s.get("workspace", "")
        if "%SCRIPT_DIR%" in ws:
            s["workspace"] = ws.replace("%SCRIPT_DIR%", script_dir_str)
    return raw


# ── 2. 解析各策略输出 ────────────────────────────────────────

# 量化项目：YYYYMMDD=交易日结果；YYYY-MM-DD=脚本运行日导出（勿用于聚合）
_TRADE_DATE_FILE_RE = re.compile(r"^daily_selection_(\d{4}-?\d{2}-?\d{2})_cn\.csv$")
_STORAGE_IPO_FILE_RE = re.compile(r"^storage_ipo_action_(\d{4}-?\d{2}-?\d{2})\.csv$")


def _parse_trade_date_from_filename(path: Path, file_date_pattern: Optional[str] = None) -> Optional[str]:
    """从结果文件名解析交易日（统一为 8 位 YYYYMMDD）。"""
    if file_date_pattern:
        m = re.match(file_date_pattern, path.name)
        if m:
            return _normalize_trade_date(m.group(1))
    for pat in (_TRADE_DATE_FILE_RE, _STORAGE_IPO_FILE_RE):
        m = pat.match(path.name)
        if m:
            return _normalize_trade_date(m.group(1))
    return None


def _normalize_trade_date(value: object) -> Optional[str]:
    """统一为 YYYYMMDD 字符串。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    raw = str(value).strip().replace("-", "")[:8]
    if len(raw) == 8 and raw.isdigit():
        return raw
    return None


def _read_csv_sidecar_trade_date(csv_path: Path) -> Optional[str]:
    """读取策略输出的 sidecar meta（如 dragons_candidates.meta.json）中的交易日。"""
    meta_path = csv_path.with_suffix(".meta.json")
    if not meta_path.exists():
        meta_path = csv_path.parent / (csv_path.stem + ".meta.json")
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return _normalize_trade_date(meta.get("trade_date"))
    except (json.JSONDecodeError, OSError):
        return None


def _peek_trade_date_from_strategy(cfg: dict) -> Optional[str]:
    """从各策略已有输出中读取交易日（不写死，每次运行取最新）。"""
    ws = cfg["workspace"]
    dates: list[str] = []

    csv_path_key = cfg.get("csv_path")
    csv_glob_key = cfg.get("csv_glob")
    if csv_glob_key:
        latest = _find_latest_glob(
            ws,
            csv_glob_key,
            file_date_pattern=cfg.get("file_date_pattern"),
        )
        if latest:
            td = _parse_trade_date_from_filename(latest, cfg.get("file_date_pattern"))
            if td:
                dates.append(td)
    if csv_path_key:
        p = Path(ws) / csv_path_key
        if p.exists():
            td_meta = _read_csv_sidecar_trade_date(p)
            if td_meta:
                dates.append(td_meta)
            df = _read_csv_safe(p)
            for col in ("trade_date", "交易日期"):
                if col in df.columns:
                    for v in df[col].dropna().astype(str):
                        td = _normalize_trade_date(v)
                        if td:
                            dates.append(td)

    if cfg.get("md_report"):
        auto_json = Path(ws) / "data/input.auto.json"
        if auto_json.exists():
            try:
                meta = json.loads(auto_json.read_text(encoding="utf-8")).get("meta", {})
                td = _normalize_trade_date(meta.get("trade_date"))
                if td:
                    dates.append(td)
            except (json.JSONDecodeError, OSError):
                pass

    return max(dates) if dates else None


def _load_run_status() -> dict:
    """读取 run_all.sh 写入的本次运行状态（失败/超时策略），仅当为当天记录时有效。"""
    p = OUTPUT_DIR / ".run_status.json"
    if not p.exists():
        return {}
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    # 仅采信当天的运行状态，避免显示历史失败信息
    if str(st.get("run_date", "")) != datetime.now().strftime("%Y%m%d"):
        return {}
    return st


def _detect_stale_strategies(
    strategies: list, parsed_names: set, latest_td: str
) -> list:
    """对比各策略真实交易日与全局最新开市日，返回非最新（含无法判定）的策略列表。

    用于在看板/日志上显式告警，避免脚本失败或超时后静默沿用昨天的旧结果。
    """
    if not latest_td or len(latest_td) != 8:
        return []
    stale = []
    for s in strategies:
        if s["name"] not in parsed_names:
            continue
        std = s.get("_trade_date") or _peek_trade_date_from_strategy(s)
        if not std or std != latest_td:
            stale.append(
                {
                    "name": s["name"],
                    "short": s.get("short", s["name"]),
                    "date": std or "",
                }
            )
    return stale


def _find_latest_glob(
    workspace: str,
    pattern: str,
    prefer_trade_date: Optional[str] = None,
    file_date_pattern: Optional[str] = None,
) -> Path | None:
    """按文件名中的最近交易日选取主结果文件（仅 YYYYMMDD，排除 universe/空文件）。"""
    full = os.path.join(workspace, pattern)
    paths = [Path(p) for p in glob.glob(full)]
    if not paths:
        return None

    prefer = _normalize_trade_date(prefer_trade_date)
    by_date: dict[str, tuple[float, Path]] = {}

    for path in paths:
        if "_universe" in path.name:
            continue
        trade_date = _parse_trade_date_from_filename(path, file_date_pattern)
        if trade_date is None:
            continue
        df = _read_csv_safe(path)
        if df.empty:
            continue
        mtime = path.stat().st_mtime
        prev = by_date.get(trade_date)
        if prev is None or mtime > prev[0]:
            by_date[trade_date] = (mtime, path)

    if not by_date:
        return None

    if prefer and prefer in by_date:
        chosen = by_date[prefer][1]
        print(f"  [数据] {chosen.name} (对齐交易日 {prefer})")
        return chosen

    latest = max(by_date.keys())
    chosen = by_date[latest][1]
    print(f"  [数据] {chosen.name} (最近交易日 {latest})")
    return chosen


def _read_csv_safe(path: Path) -> pd.DataFrame:
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return pd.read_csv(path, encoding=enc)
        except (UnicodeDecodeError, UnicodeError, pd.errors.EmptyDataError):
            continue
    return pd.DataFrame()


def _extract_afterclose_sector_table(html_text: str) -> str:
    """从策略 auto_dashboard.html 提取「板块队形（龙头→追随）」表。"""
    m = re.search(
        r"<h2>板块队形[^<]*</h2>\s*(<table>.*?</table>)",
        html_text,
        re.DOTALL | re.IGNORECASE,
    )
    return m.group(1) if m else ""


def parse_afterclose_html(html_path: str, market_condition: Optional[MarketCondition] = None,
                          trade_date: str = "") -> str:
    """从盘后扫描追随的 auto_dashboard.html 提取 KPI、板块队形、胜率等辅助区块。

    当日推荐标的列表由 render_html 用 parse_afterclose_md 数据单独渲染，不在此重复。
    """
    path = Path(html_path)
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")

    # 提取 KPI 卡片
    kpi_cards = re.findall(r'<div class="card"><b>(.+?)</b><div>(.+?)</div></div>', text)

    # 用涨跌比推导的真实情绪覆盖原 KPI 中的情绪卡片
    if market_condition:
        ratio = market_condition.adv_decline_ratio
        if ratio >= 1.5:
            real_sentiment = "亢奋"
        elif ratio >= 0.9:
            real_sentiment = "回暖"
        elif ratio >= 0.5:
            real_sentiment = "修复"
        else:
            real_sentiment = "冰点"
        kpi_cards = [
            (label, real_sentiment) if label.strip() == "情绪" else (label, val)
            for label, val in kpi_cards
        ]

    # 提取胜率 KPI + 表格
    winrate_block = re.search(
        r'<h2>昨日选股次日胜率[^<]*</h2>\s*(.*)',
        text, re.DOTALL,
    )
    winrate_text = winrate_block.group(1) if winrate_block else ""
    winrate_cards = re.findall(r'<div class="card"><b>(.+?)</b><div>(.+?)</div></div>', winrate_text)
    winrate_table = re.search(r'(<table>.*?</table>)', winrate_text, re.DOTALL)

    sector_table_html = _extract_afterclose_sector_table(text)
    ths_sectors_html = _build_real_sectors_html(trade_date=trade_date)

    kpi_html = ""
    for label, val in kpi_cards:
        kpi_html += (
            f'<div style="background:#f1f5f9;border:1px solid #e2e8f0;border-radius:8px;padding:8px 14px;min-width:100px">'
            f'<div style="font-size:11px;color:#64748b">{label}</div>'
            f'<div style="font-size:18px;font-weight:700;margin-top:2px;color:#1e293b">{val}</div></div>'
        )

    section_styles = """
    <style>
    .af-section table { width:100%; border-collapse:collapse; font-size:13px; }
    .af-section th { background:#f1f5f9; color:#475569; padding:8px 10px; text-align:left; border:1px solid #e2e8f0; font-weight:600; }
    .af-section td { padding:7px 10px; border:1px solid #e2e8f0; color:#334155; }
    .af-section tr:hover { background:#f8fafc; }
    .af-section .stock { display:inline-flex; align-items:center; gap:6px; padding:2px 6px; border-radius:6px; }
    .af-section .stock.doable { background:rgba(16,185,129,0.1); border:1px solid #6ee7b7; }
    .af-section .badge { font-size:11px; padding:1px 6px; border-radius:999px; }
    .af-section .badge.doable { background:#ecfdf5; border:1px solid #6ee7b7; color:#047857; }
    .af-section .badge.caution { background:#fffbeb; border:1px solid #fbbf24; color:#92400e; }
    .af-section .badge.avoid { background:#fef2f2; border:1px solid #fca5a5; color:#b91c1c; }
    .af-section .table-scroll { max-height:300px; overflow-y:auto; border:1px solid #e2e8f0; border-radius:8px; margin-top:10px; }
    .af-section .table-scroll table { margin:0; border:0; }
    .af-section .table-scroll th { position:sticky; top:0; z-index:1; }
    </style>
    """

    winrate_section = ""
    if winrate_cards or winrate_table:
        wr_kpi = ""
        for label, val in winrate_cards:
            wr_kpi += (
                f'<div style="background:#f1f5f9;border:1px solid #e2e8f0;border-radius:8px;padding:6px 12px;min-width:90px">'
                f'<div style="font-size:11px;color:#64748b">{label}</div>'
                f'<div style="font-size:15px;font-weight:700;margin-top:2px;color:#1e293b">{val}</div></div>'
            )
        wr_table = winrate_table.group(1) if winrate_table else ""
        winrate_section = f"""
        <div style="margin-top:18px">
          <div style="font-size:14px;font-weight:700;margin-bottom:8px;color:#475569">昨日选股次日胜率</div>
          <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px">{wr_kpi}</div>
          <div class="table-scroll">{wr_table}</div>
        </div>"""

    sector_block = ""
    if sector_table_html:
        sector_block = f"""
      <div style="font-size:14px;font-weight:700;margin:8px 0">板块队形（龙头 → 追随）</div>
      <div class="table-scroll">{sector_table_html}</div>"""
    if ths_sectors_html and "尚未缓存" not in ths_sectors_html:
        sector_block += f"""
      <div style="font-size:13px;font-weight:600;margin:16px 0 8px;color:#64748b">同花顺概念板块涨跌幅 TOP10（对照）</div>
      {ths_sectors_html}"""

    result = f"""
    {section_styles}
    <div class="af-section">
      <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px">{kpi_html}</div>
      {sector_block}
      {winrate_section}
    </div>
    """
    return result


def _build_real_sectors_html(trade_date: str = "") -> str:
    """从 THS 板块日线数据读取真实概念板块涨跌幅 TOP 10，生成 HTML。"""
    ths_daily_dir = DATA_DIR / "ths_daily"
    if not ths_daily_dir.exists():
        return '<div style="color:#94a3b8;font-size:13px">板块日线数据尚未缓存</div>'

    td = _resolve_latest_parquet_date("ths_daily", trade_date)
    if not td:
        return '<div style="color:#94a3b8;font-size:13px">板块日线数据尚未缓存</div>'
    p = ths_daily_dir / f"{td}.parquet"
    if not p.exists():
        return '<div style="color:#94a3b8;font-size:13px">板块日线数据尚未缓存</div>'

    try:
        df = pd.read_parquet(p)
    except Exception:
        return '<div style="color:#94a3b8;font-size:13px">板块日线数据读取失败</div>'

    if df.empty or "pct_change" not in df.columns:
        return '<div style="color:#94a3b8;font-size:13px">板块日线数据为空</div>'

    # 读取 THS 板块名称和类型映射
    ths_index_path = DATA_DIR / "static" / "ths_index.parquet"
    name_map = {}
    type_map = {}  # ts_code -> exchange (概念/行业)
    if ths_index_path.exists():
        try:
            idx = pd.read_parquet(ths_index_path)
            for _, r in idx.iterrows():
                name_map[r["ts_code"]] = r.get("name", "")
                type_map[r["ts_code"]] = r.get("exchange", "")
        except Exception:
            pass

    # 合并名称
    df["_name"] = df["ts_code"].map(name_map).fillna(df["ts_code"])
    df["_type"] = df["ts_code"].map(type_map).fillna("")

    # 过滤出概念板块（exchange 包含"概念"），如果没有概念板块则不限
    concept_df = df[df["_type"].str.contains("概念", na=False)]
    if concept_df.empty:
        concept_df = df  # 回退到全部

    # 过滤掉策略指数/风格指数等非纯概念板块的关键词
    _exclude_keywords = [
        "昨日", "非ST", "涨停", "跌停", "打板", "连板", "首板",
        "三板", "换手", "高换手", "强势股", "弱势股",
        "高贝塔", "低贝塔", "高分红", "绩优", "ST",
    ]
    _mask = concept_df["_name"].str.contains("|".join(_exclude_keywords), na=False, regex=True)
    concept_df = concept_df[~_mask]

    # 过滤涨跌幅异常值（>±9% 的数据通常是指数计算问题）
    concept_df = concept_df[(concept_df["pct_change"] >= -9) & (concept_df["pct_change"] <= 9)]

    # 创建排序辅助列
    concept_df = concept_df.copy()
    concept_df["_abs"] = concept_df["pct_change"].abs()

    # 过滤掉同名板块（去重，同名取涨跌幅绝对值更大的）
    concept_df = concept_df.sort_values("_abs", ascending=False)
    concept_df = concept_df.drop_duplicates(subset=["_name"], keep="first")
    concept_df = concept_df.head(10)

    rows = ""
    for i, (_, r) in enumerate(concept_df.iterrows()):
        name = r.get("_name", "")
        chg = r.get("pct_change", 0)
        is_up = chg > 0
        chg_str = f'<span style="color:{"#ef4444" if is_up else "#22c55e"};font-weight:700">{chg:+.2f}%</span>'
        direction = "📈" if is_up else "📉"
        rows += f"""<tr>
            <td style="text-align:center;font-weight:600;color:#64748b">{i+1}</td>
            <td>{name}</td>
            <td style="text-align:right">{chg_str} {direction}</td>
        </tr>"""

    return f"""<table>
        <thead><tr>
            <th style="width:36px;text-align:center">#</th>
            <th>概念板块</th>
            <th style="width:110px;text-align:right">涨跌幅</th>
        </tr></thead>
        <tbody>{rows}</tbody>
    </table>
    <div style="font-size:11px;color:#94a3b8;margin-top:6px">数据来源：同花顺概念板块日线（{td}）</div>"""


def parse_afterclose_md(md_path: str) -> tuple:
    """从盘后扫描追随的 auto.md 提取完整结构化数据：个股六维 + 市场情绪 + 主线板块。"""
    path = Path(md_path)
    if not path.exists():
        return [], {}
    text = path.read_text(encoding="utf-8")

    meta = {}
    # 市场情绪
    m_mood = re.search(r"情绪判定：\*\*(.+?)\*\*", text)
    if m_mood:
        meta["sentiment"] = m_mood.group(1)
    m_pos = re.search(r"总仓位建议：\*\*(.+?)\*\*", text)
    if m_pos:
        meta["position"] = m_pos.group(1)

    # 主线板块
    sectors = re.findall(
        r"TOP\d\s+`([^`]+)`\s*\|\s*板块涨幅\s*([\d.]+)%\s*\|\s*持续性\s*([\d.]+)/10\s*\|\s*阶段\s*(\S+)",
        text,
    )
    if sectors:
        meta["top_sectors"] = [
            {"name": s[0], "change": float(s[1]), "persistence": float(s[2]), "phase": s[3]}
            for s in sectors
        ]

    # 个股明细：从"第三步&第四步"段落提取，包含六维评分
    stock_pattern = re.compile(
        r"`([^`(]+)\((\d{6}\.\w{2})\)`\s*总分\s*\*\*([\d.]+)/40\*\*"
        r"(?:\s*（([^）]*)）)?"
        r"\s*\|\s*量能([\d.]+)\s+位置([\d.]+)\s+题材([\d.]+)\s+人气([\d.]+)\s+追随([\d.]+)\s+安全([\d.]+)"
        r"\s+建议(可做|谨慎|放弃)"
    )
    results = []
    for m in stock_pattern.finditer(text):
        results.append({
            "code": m.group(2),
            "name": m.group(1),
            "total_score": float(m.group(3)),
            "tag": m.group(4) or "",
            "量能": float(m.group(5)),
            "位置": float(m.group(6)),
            "题材": float(m.group(7)),
            "人气": float(m.group(8)),
            "追随": float(m.group(9)),
            "安全": float(m.group(10)),
            "advice": m.group(11),
        })

    return results, meta


def parse_strategy(cfg: dict, market_condition: Optional[MarketCondition] = None) -> pd.DataFrame:
    """按配置解析单个策略，返回统一 schema 的 DataFrame。"""
    ws = cfg["workspace"]
    fields = cfg["fields"]

    # 读取 CSV
    csv_path_key = cfg.get("csv_path")
    csv_glob_key = cfg.get("csv_glob")
    if csv_path_key:
        fpath = Path(ws) / csv_path_key
        if not fpath.exists():
            print(f"  [跳过] 文件不存在: {fpath}")
            return pd.DataFrame()
        df = _read_csv_safe(fpath)
    elif csv_glob_key:
        fpath = _find_latest_glob(
            ws,
            csv_glob_key,
            prefer_trade_date=cfg.get("_prefer_trade_date"),
            file_date_pattern=cfg.get("file_date_pattern"),
        )
        if not fpath:
            print(f"  [跳过] 未匹配到文件: {ws}/{csv_glob_key}")
            return pd.DataFrame()
        df = _read_csv_safe(fpath)
    else:
        df = pd.DataFrame()

    # 盘后扫描追随：以 auto.md 为主数据源（当天完整六维评分）
    md_report = cfg.get("md_report")
    if md_report:
        md_rows, md_meta = parse_afterclose_md(os.path.join(ws, md_report))
        if md_rows:
            df = pd.DataFrame(md_rows)
            cfg["_md_meta"] = md_meta
        elif df.empty:
            return pd.DataFrame()
        # 提取原始 HTML 板块队形表格
        html_dashboard = cfg.get("html_dashboard")
        if html_dashboard:
            original_html = parse_afterclose_html(os.path.join(ws, html_dashboard), market_condition,
                                                         trade_date=cfg.get("_trade_date", ""))
            if original_html:
                cfg["_original_html"] = original_html
    elif df.empty:
        if csv_path_key:
            td_meta = _read_csv_sidecar_trade_date(Path(ws) / csv_path_key)
            if td_meta:
                cfg["_trade_date"] = td_meta
        return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    for td_col in ("trade_date", "交易日期"):
        if td_col in df.columns:
            series = df[td_col].dropna().astype(str)
            if not series.empty:
                td = series.iloc[0].replace("-", "").strip()[:8]
                if len(td) == 8 and td.isdigit():
                    cfg["_trade_date"] = td
            break
    # 内容无交易日列时，glob 策略用所选文件名日期兜底，保证可做新鲜度校验
    if not cfg.get("_trade_date") and csv_glob_key:
        td_fn = _parse_trade_date_from_filename(fpath, cfg.get("file_date_pattern"))
        if td_fn:
            cfg["_trade_date"] = td_fn

    # 字段映射
    rename = {}
    for unified, src in fields.items():
        if src and src in df.columns:
            rename[src] = unified
    df = df.rename(columns=rename)

    # 确保必需列存在
    tier_col = cfg.get("tier_column")
    tier_map = cfg.get("tier_score_map") or {}
    if tier_col and tier_col in df.columns and tier_map:
        df["score"] = df[tier_col].map(tier_map).fillna(0)
    elif "score" in df.columns:
        df["score"] = pd.to_numeric(df["score"], errors="coerce").fillna(0)
    else:
        df["score"] = 0

    for col in ("ts_code", "name"):
        if col not in df.columns:
            return pd.DataFrame()

    # 归一化到 0-100
    lo, hi = cfg["score_range"]
    if hi != lo and hi != 100:
        df["score_norm"] = ((df["score"] - lo) / (hi - lo) * 100).clip(0, 100).round(1)
    else:
        df["score_norm"] = df["score"].clip(0, 100).round(1)

    df["score_raw"] = df["score"].round(2)
    df["strategy"] = cfg["name"]
    df["strategy_short"] = cfg["short"]

    for col in ("industry", "concept"):
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].fillna("").astype(str)

    # ── 行业补全：如果 industry 为空（config 中为 ~ 或 CSV 缺失），
    #    从本地 stock_basic.parquet 统一补齐，不再依赖各策略各自的行业列 ──
    if df["industry"].str.strip().eq("").all():
        static_path = DATA_DIR / "static" / "stock_basic.parquet"
        if static_path.exists():
            try:
                basic = pd.read_parquet(static_path)[["ts_code", "industry"]]
                ind_map = basic.set_index("ts_code")["industry"].to_dict()
                df["industry"] = df["ts_code"].map(ind_map).fillna("").astype(str)
            except Exception:
                pass

    # 提取 extra_cols
    extras = {}
    for ec in cfg.get("extra_cols", []):
        src, label = ec["src"], ec["label"]
        if src in df.columns:
            extras[label] = df[src].values
    df_extras = pd.DataFrame(extras, index=df.index) if extras else pd.DataFrame(index=df.index)

    result = df[["ts_code", "name", "score_raw", "score_norm", "industry", "concept", "strategy", "strategy_short"]].copy()
    result = pd.concat([result, df_extras], axis=1)

    if "advice" in df.columns:
        result["advice"] = df["advice"]
    elif tier_col and tier_col in df.columns:
        result["advice"] = df[tier_col]

    return result.reset_index(drop=True)


# ── 3. 交叉共振检测 ─────────────────────────────────────────

def detect_resonance(all_df: pd.DataFrame, market_condition: Optional[MarketCondition] = None) -> pd.DataFrame:
    """找出被 >= 2 个策略同时选中的股票。

    market_condition 仅用于在结果中附加市场环境标记，不影响共振候选门槛。
    """
    if all_df.empty:
        return pd.DataFrame()
    def _first_nonempty(series: pd.Series) -> str:
        for v in series:
            s = str(v or "").strip()
            if s and s.lower() != "nan":
                return s
        return ""

    def _richest_concept(series: pd.Series) -> str:
        texts = [str(v or "").strip() for v in series if str(v or "").strip()]
        return max(texts, key=len, default="")

    grouped = all_df.groupby("ts_code").agg(
        name=("name", "first"),
        industry=("industry", _first_nonempty),
        concept=("concept", _richest_concept),
        strategies=("strategy_short", lambda x: "、".join(sorted(set(x)))),
        strategy_count=("strategy", "nunique"),
        avg_score=("score_norm", "mean"),
        max_score=("score_norm", "max"),
    ).reset_index()
    resonance = grouped[grouped["strategy_count"] >= 2].sort_values(
        ["strategy_count", "avg_score"], ascending=[False, False]
    )
    return resonance.reset_index(drop=True)


# ── 3.5 共振标的资金趋势分析 ─────────────────────────────

_GENERIC_THEMES = frozenset({
    "半导体", "元器件", "化工原料", "互联网", "软件服务", "专用机械",
    "电气设备", "汽车配件", "建筑工程", "制造封测",
})


def _fmt_flow_yi(net_wan: float) -> str:
    """万元 -> 亿元字符串（个股/板块序列展示）。"""
    yi = net_wan / 10000
    if abs(yi) >= 100:
        return f"{yi:.0f}亿"
    if abs(yi) >= 10:
        return f"{yi:.1f}亿"
    return f"{yi:.2f}亿"


def _moneyflow_series_suspect(pct_chg: Optional[float], net_wan: float) -> bool:
    """涨跌幅与主力净流入严重背离时，Tushare 个股数据常不可信。"""
    if pct_chg is None:
        return False
    net_yi = net_wan / 10000
    if pct_chg > 5 and net_yi < -8:
        return True
    if pct_chg < -5 and net_yi > 8:
        return True
    return False


def _mf_dates_short_label(dates: list[str]) -> str:
    if len(dates) < 2:
        return ""
    a, b = dates[0], dates[-1]
    return f"{a[4:6]}-{a[6:8]}→{b[4:6]}-{b[6:8]}"


def _resolve_moneyflow_dates(trade_date: str) -> list[str]:
    """取 trade_date 及之前最近 3 个开市日的 moneyflow 分区。"""
    mf_dir = DATA_DIR / "moneyflow"
    open_set = set(_open_trade_dates_upto(trade_date or datetime.now().strftime("%Y%m%d")))
    all_dates = sorted(
        d.stem
        for d in mf_dir.glob("*.parquet")
        if len(d.stem) == 8 and d.stem.isdigit() and (not open_set or d.stem in open_set)
    )
    if not all_dates:
        return []
    pool = [d for d in all_dates if d <= trade_date] if trade_date else all_dates
    if not pool:
        pool = all_dates
    return pool[-3:]


def _primary_theme(concept: str, industry: str) -> str:
    """从概念标签中提取主题材（跳过宽泛行业名）。"""
    text = str(concept or "").strip()
    if not text:
        return str(industry or "").strip() or "未知"
    parts: list[str] = []
    for chunk in re.split(r"[|,，]", text):
        name = re.sub(r"\([^)]*\)", "", chunk).strip()
        name = re.sub(r"\d+\.TI$", "", name).strip()
        if not name or "A股" in name or "产品与设备" in name:
            continue
        if len(name) > 24:
            continue
        parts.append(name)
    for name in parts:
        if name not in _GENERIC_THEMES:
            return name
    return parts[0] if parts else (str(industry or "").strip() or "未知")


def _peer_codes_for_theme(
    all_df: pd.DataFrame, theme: str, industry: str, self_code: str
) -> set[str]:
    """板块资金：仅在当日选股池内找同题材/同行业 peers，避免全行业加总导致数值雷同。"""
    if all_df is None or all_df.empty:
        return {self_code}
    codes: set[str] = set()
    theme = str(theme or "").strip()
    industry = str(industry or "").strip()
    if theme and theme != "未知":
        mask = all_df["concept"].fillna("").astype(str).str.contains(
            re.escape(theme), regex=True, na=False
        )
        codes = set(all_df.loc[mask, "ts_code"].astype(str))
    if len(codes) < 2 and industry:
        codes = set(
            all_df[all_df["industry"].astype(str) == industry]["ts_code"].astype(str)
        )
    if not codes:
        codes = {self_code}
    return codes


def enrich_resonance_with_moneyflow(
    resonance: pd.DataFrame,
    trade_date: str,
    pick_universe: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """对共振标的补充近 3 天资金趋势分析和策略提示。

    对每只共振股票，从 moneyflow 表计算：
    - stock_trend: 个股资金趋势 (流入递增/递减/震荡/净流入/净流出)
    - sector_trend: 所属行业板块资金趋势
    - label: 综合策略标签
    - advice: 操作提示文字

    返回追加了 moneyflow_* 列的共振 DataFrame。
    """
    if resonance.empty:
        return resonance

    mf_dir = DATA_DIR / "moneyflow"
    mf_dates = _resolve_moneyflow_dates(trade_date)

    if len(mf_dates) < 2:
        return resonance

    mf_frames = {}
    for d in mf_dates:
        try:
            mf_frames[d] = pd.read_parquet(mf_dir / f"{d}.parquet")
        except Exception:
            continue

    if not mf_frames:
        return resonance

    daily_by_date: dict[str, pd.DataFrame] = {}
    for d in mf_dates:
        dp = DATA_DIR / "daily" / f"{d}.parquet"
        if dp.exists():
            try:
                daily_by_date[d] = pd.read_parquet(dp)[["ts_code", "pct_chg"]]
            except Exception:
                pass

    def _pct_chg_on(code: str, d: str) -> Optional[float]:
        df = daily_by_date.get(d)
        if df is None:
            return None
        hit = df[df["ts_code"] == code]
        if hit.empty:
            return None
        return float(pd.to_numeric(hit["pct_chg"].iloc[0], errors="coerce"))

    date_hint = _mf_dates_short_label(mf_dates)

    def _trend_label(values: list) -> str:
        """判断趋势。"""
        if len(values) < 2:
            return "数据不足"
        # 全部正值且递增
        if all(v > 0 for v in values) and values == sorted(values):
            return "流入递增↑"
        # 全部正值且递减
        if all(v > 0 for v in values) and values == sorted(values, reverse=True):
            return "流入递减↓"
        # 全部负值且递减（流出增大）
        if all(v < 0 for v in values) and values == sorted(values):
            return "流出扩大↓"
        # 全部负值且递增（流出缩小）
        if all(v < 0 for v in values) and values == sorted(values, reverse=True):
            return "流出收窄↑"
        # 全部正值
        if all(v > 0 for v in values):
            return "持续流入"
        # 全部负值
        if all(v < 0 for v in values):
            return "持续流出"
        # 从负转正
        if values[0] < 0 and values[-1] > 0:
            return "资金反转↑"
        # 从正转负
        if values[0] > 0 and values[-1] < 0:
            return "资金反转↓"
        return "震荡"

    def _flow_span_label(values: list, prefix: str, n_peers: int = 0) -> str:
        """资金序列标签（万元输入，展示为亿元）。"""
        if len(values) < 2:
            return "数据不足"
        v0, v1 = _fmt_flow_yi(values[0]), _fmt_flow_yi(values[-1])
        suffix = f"，{n_peers}只" if n_peers > 1 else ""
        if values[0] < 0 and values[-1] > 0:
            return f"{prefix}反转（{v0}→{v1}{suffix}）"
        if values[0] > 0 and values[-1] < 0:
            return f"{prefix}转弱（{v0}→{v1}{suffix}）"
        if values == sorted(values):
            return f"{prefix}走强（{v0}→{v1}{suffix}）"
        if values == sorted(values, reverse=True):
            return f"{prefix}走弱（{v0}→{v1}{suffix}）"
        return f"{prefix}震荡（{v0}→{v1}{suffix}）"

    labels = []
    stock_trends = []
    sector_trends = []
    advices = []

    for _, row in resonance.iterrows():
        code = str(row["ts_code"])
        industry = str(row.get("industry") or "").strip()
        concept = str(row.get("concept") or "").strip()
        theme = _primary_theme(concept, industry)
        peer_codes = _peer_codes_for_theme(pick_universe, theme, industry, code)

        # ── 个股资金趋势 ──
        stock_nets = []
        for d in mf_dates:
            mf = mf_frames.get(d)
            if mf is not None:
                found = mf[mf["ts_code"] == code]
                if not found.empty:
                    stock_nets.append(float(found["net_mf_amount"].values[0]))
        last_pct = _pct_chg_on(code, mf_dates[-1]) if mf_dates else None
        stock_suspect = bool(
            stock_nets
            and mf_dates
            and _moneyflow_series_suspect(last_pct, stock_nets[-1])
        )
        stock_trend = (
            "数据存疑"
            if stock_suspect
            else (_trend_label(stock_nets) if stock_nets else "无数据")
        )
        stock_span = ""
        if len(stock_nets) >= 2:
            if stock_suspect:
                stock_span = (
                    f"个股主力数据存疑（涨跌幅{last_pct:+.1f}%与净流入矛盾，"
                    f"请以行情软件为准）"
                )
            else:
                stock_span = _flow_span_label(stock_nets, "个股", 1)

        # ── 板块资金：当日选股池内同题材/同行业均流（非全市场加总）──
        sector_nets = []
        for d in mf_dates:
            mf = mf_frames.get(d)
            if mf is not None:
                ind_mf = mf[mf["ts_code"].isin(peer_codes)]
                if not ind_mf.empty:
                    sector_nets.append(float(ind_mf["net_mf_amount"].mean()))
        n_peers = len(peer_codes)
        sector_trend = (
            _flow_span_label(sector_nets, f"{theme}均流", n_peers)
            if sector_nets
            else ""
        )
        if date_hint:
            if stock_span:
                stock_span = f"{stock_span} [{date_hint}]"
            if sector_trend:
                sector_trend = f"{sector_trend} [{date_hint}]"
        if stock_span and sector_trend:
            sector_trend = f"{stock_span} | {sector_trend}"
        elif stock_span:
            sector_trend = stock_span

        stock_trends.append(stock_trend)
        sector_trends.append(sector_trend)

        # ── 综合策略标签 ──
        if stock_nets and len(stock_nets) >= 2:
            last = stock_nets[-1]
            prev = stock_nets[-2]
            if last > 0 and prev > 0 and last > prev:
                if "反转" in sector_trend or "走强" in sector_trend or "均流走强" in sector_trend:
                    label = "🌟 资金共振"
                    advice = "个股资金递增 + 板块同步走强，高确定性信号"
                else:
                    label = "✅ 资金看好"
                    advice = "个股连续净流入，关注持续性"
            elif last > 0 and prev < 0:
                label = "⚡ 资金回流"
                advice = "个股从净流出转为净流入，止跌信号"
            elif last < 0 and prev > 0:
                label = "⚠️ 资金恶化"
                advice = "个股从净流入转为净流出，注意风险"
            elif last < 0 and prev < 0 and last < prev:
                label = "🔻 资金逃离"
                advice = "个股连续净流出且扩大，回避"
            elif last < 0 and prev < 0 and last > prev:
                label = "🔄 流出收窄"
                advice = "流出在缩小，关注是否止跌"
            else:
                label = "➖ 资金震荡"
                advice = "资金无明显方向，结合其他指标判断"
        else:
            label = "📊 数据不足"
            advice = "近3日资金数据不足，参考其他指标"

        labels.append(label)
        advices.append(advice)

    result = resonance.copy()
    result["moneyflow_stock_trend"] = stock_trends
    result["moneyflow_sector_trend"] = sector_trends
    result["moneyflow_label"] = labels
    result["moneyflow_advice"] = advices

    return result


# ── 4. HTML 渲染 ─────────────────────────────────────────────

STRATEGY_COLORS = {
    "擒龙猎手": "#f59e0b",
    "主升行情启动": "#ef4444",
    "盘后扫描追随": "#3b82f6",
    "量化蓄势突破": "#10b981",
    "存储IPO供应链": "#8b5cf6",
}

DISPLAY_LIMITS = {
    "存储IPO供应链": 40,
}

TIER_BADGE_STYLE = {
    "可做": ("#ecfdf5", "#047857", "#6ee7b7"),
    "观察": ("#fffbeb", "#92400e", "#fbbf24"),
    "放弃": ("#fef2f2", "#b91c1c", "#fca5a5"),
}


def _score_color(val: float) -> str:
    if val >= 85:
        return "#dc2626"
    if val >= 70:
        return "#d97706"
    if val >= 55:
        return "#2563eb"
    return "#64748b"


def _badge(text: str, color: str) -> str:
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:4px;'
        f'font-size:12px;color:#fff;background:{color};margin:1px 2px">{text}</span>'
    )


def _tier_badge(text: str) -> str:
    bg, fg, border = TIER_BADGE_STYLE.get(str(text), ("#f1f5f9", "#475569", "#e2e8f0"))
    return (
        f'<span style="display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;'
        f'font-weight:600;background:{bg};color:{fg};border:1px solid {border}">{text}</span>'
    )


def _storage_tab_summary(sdf: pd.DataFrame) -> str:
    if sdf.empty or "档位" not in sdf.columns:
        return ""
    ok = int((sdf["档位"] == "可做").sum())
    watch = int((sdf["档位"] == "观察").sum())
    return (
        f'<p style="color:#64748b;font-size:13px;margin-bottom:12px">'
        f'长鑫存储(DRAM) / 长江存储(NAND) IPO 供应链映射 · '
        f'<span style="color:#047857;font-weight:600">可做 {ok}</span> · '
        f'<span style="color:#92400e;font-weight:600">观察 {watch}</span>'
        f'</p>'
    )


def _render_weak_strategies_html(oversold_candidates: list = None, defensive_stocks: list = None) -> str:
    """渲染弱势行情辅助策略板块（超跌反弹 + 防守板块轮动）。"""
    oversold_candidates = oversold_candidates or []
    defensive_stocks = defensive_stocks or []

    if not oversold_candidates and not defensive_stocks:
        return ""

    sections = ""

    # 超跌反弹
    if oversold_candidates:
        oversold_rows = ""
        for item in oversold_candidates[:8]:
            oversold_rows += f"""<tr>
                <td style="color:#b45309;font-weight:600">{item['ts_code']}</td>
                <td style="font-weight:600">{item['name']}</td>
                <td style="text-align:center;color:#ef4444;font-weight:700">{item['bias']:.1f}%</td>
                <td style="text-align:center">{item['pct_chg']:+.2f}%</td>
                <td style="text-align:center">{item['turnover_rate']:.1f}%</td>
            </tr>"""
        sections += f"""
  <div class="section" style="border-left:3px solid #8b5cf6">
    <div class="section-title" style="color:#7c3aed">📉 超跌反弹候选（BIAS ≤ -8%）</div>
    <div style="font-size:13px;color:#64748b;margin-bottom:12px">高乖离率超跌标的 — 乖离率负值越大表示偏离均线越远，反弹概率提升</div>
    <table><thead><tr>
        <th>代码</th><th>名称</th><th>乖离率(BIAS)</th><th>今日涨跌</th><th>换手率</th>
    </tr></thead><tbody>{oversold_rows}</tbody></table>
  </div>"""

    # 防守板块
    if defensive_stocks:
        def_rows = ""
        for item in defensive_stocks[:12]:
            def_rows += f"""<tr>
                <td style="color:#b45309;font-weight:600">{item['ts_code']}</td>
                <td style="font-weight:600">{item['name']}</td>
                <td><span style="display:inline-block;padding:2px 8px;border-radius:4px;font-size:12px;background:#eef2ff;color:#4338ca">{item['sector']}</span></td>
                <td style="text-align:center;color:#059669;font-weight:600">+{item['pct_chg']:.2f}%</td>
                <td style="text-align:center">{item['amount']:.1f}亿</td>
            </tr>"""
        sections += f"""
  <div class="section" style="border-left:3px solid #059669">
    <div class="section-title" style="color:#047857">🛡️ 防守板块轮动关注</div>
    <div style="font-size:13px;color:#64748b;margin-bottom:12px">弱势行情下高股息、公用事业、银行等防守板块中表现较强的个股</div>
    <table><thead><tr>
        <th>代码</th><th>名称</th><th>防守板块</th><th>今日涨幅</th><th>成交额</th>
    </tr></thead><tbody>{def_rows}</tbody></table>
  </div>"""

    return sections


def _render_strategy_picks_table(sdf: pd.DataFrame, sname: str) -> str:
    """渲染策略推荐标的表格（与统一看板各策略 Tab 列规则一致）。"""
    if sdf is None or sdf.empty:
        return '<p style="color:#94a3b8">暂无推荐标的</p>'

    is_storage = sname == "存储IPO供应链"
    is_lh = sname == "量化蓄势突破"
    base_cols = ["ts_code", "name", "score_raw", "score_norm"]
    if is_storage:
        base_cols = ["ts_code", "name"]
    show_cols = base_cols.copy()
    col_labels = {"ts_code": "代码", "name": "名称", "score_raw": "原始分", "score_norm": "归一化分"}

    for c in sdf.columns:
        if c not in ("ts_code", "name", "score_raw", "score_norm", "industry", "concept",
                     "strategy", "strategy_short", "score", "advice"):
            show_cols.append(c)
            col_labels[c] = c

    if not is_storage:
        if "concept" in sdf.columns and sdf["concept"].astype(str).str.strip().any():
            show_cols.append("concept")
            col_labels["concept"] = "概念/题材"
        if "industry" in sdf.columns and sdf["industry"].astype(str).str.strip().any():
            show_cols.insert(3, "industry")
            col_labels["industry"] = "行业"
    if "advice" in sdf.columns and "advice" not in show_cols:
        show_cols.append("advice")
        col_labels["advice"] = "建议"

    thead = "".join(f"<th>{col_labels.get(c, c)}</th>" for c in show_cols)
    tbody = ""
    row_limit = DISPLAY_LIMITS.get(sname, 20)
    display_sdf = sdf

    # 池名缩写
    _POOL_ABBR = {"A-超卖反弹": "A-超卖", "B-科技成长": "B-科技"}

    # 量化蓄势突破：字号更小，列更拥挤
    cell_style = ' style="font-size:12px;padding:4px 5px"' if is_lh else ""

    if is_storage and "档位" in sdf.columns:
        tier_order = {"可做": 0, "观察": 1}
        display_sdf = sdf.copy()
        display_sdf["_sort_tier"] = display_sdf["档位"].map(tier_order).fillna(9)
        display_sdf = display_sdf.sort_values(
            ["_sort_tier", "score_norm"], ascending=[True, False]
        )
    for _, row in display_sdf.head(row_limit).iterrows():
        cells = ""
        for c in show_cols:
            val = row.get(c, "")
            if c in ("advice", "档位"):
                cells += f"<td>{_tier_badge(str(val))}</td>"
            elif c == "score_norm":
                sc = _score_color(float(val) if pd.notna(val) else 0)
                cells += f'<td style="text-align:center;color:{sc};font-weight:700"{cell_style}>{val}</td>'
            elif c == "ts_code":
                cells += f'<td style="color:#b45309;font-weight:600"{cell_style}>{val}</td>'
            elif c == "concept":
                raw = str(val) if pd.notna(val) else ""
                short = raw[:20] + "…" if len(raw) > 20 else raw
                cells += f'<td title="{raw}" style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap{";font-size:12px;padding:4px 5px" if is_lh else ""}">{short}</td>'
            else:
                display = val if pd.notna(val) else ""
                # 量化蓄势突破池名缩写
                if is_lh and c == "池":
                    display = _POOL_ABBR.get(str(display), str(display))
                if isinstance(display, float):
                    display = f"{display:.2f}" if abs(display) < 1000 else f"{display:.0f}"
                cells += f"<td{cell_style}>{display}</td>"
        tbody += f"<tr>{cells}</tr>"

    table_html = f"<table><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table>"
    if is_lh:
        table_html = f'<div style="font-size:12px;overflow-x:auto">{table_html}</div>'
    return table_html


def _data_date_subtitle(market_meta: dict) -> str:
    """看板副标题：标明大盘/资金统计日，避免与策略选股日混淆。"""
    data_td = str(market_meta.get("data_trade_date", "") or "").strip()
    if len(data_td) != 8 or not data_td.isdigit():
        return ""
    label = f"{data_td[:4]}-{data_td[4:6]}-{data_td[6:8]}"
    strat_td = str(market_meta.get("strategy_trade_date", "") or market_meta.get("trade_date", "")).strip()
    if strat_td and strat_td != data_td and len(strat_td) == 8:
        sl = f"{strat_td[:4]}-{strat_td[4:6]}-{strat_td[6:8]}"
        return f" &nbsp;|&nbsp; 大盘统计日 {label} &middot; 选股结果日 {sl}"
    return f" &nbsp;|&nbsp; 大盘统计日 {label}"


def render_html(all_df: pd.DataFrame, resonance: pd.DataFrame, strategy_dfs: dict, market_meta: dict = None, market_condition: Optional[MarketCondition] = None, oversold_candidates: list = None, defensive_stocks: list = None) -> str:
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    market_meta = market_meta or {}

    total_stocks = all_df["ts_code"].nunique() if not all_df.empty else 0
    resonance_count = len(resonance)

    # 数据新鲜度告警（脚本失败/超时导致沿用旧结果时显式标红）
    stale_warning = ""
    stale_list = market_meta.get("stale_strategies") or []
    failed_tasks = market_meta.get("failed_tasks") or []
    if stale_list or failed_tasks:
        data_td = str(market_meta.get("data_trade_date", "") or "")
        latest_lbl = (
            f"{data_td[:4]}-{data_td[4:6]}-{data_td[6:8]}"
            if len(data_td) == 8 else data_td
        )
        stale_block = ""
        if stale_list:
            items = ""
            for st in stale_list:
                d = str(st.get("date") or "")
                dl = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else "未知日期"
                items += f"<li><b>{st.get('short') or st.get('name')}</b>：数据日 {dl}</li>"
            stale_block = f"""
    <div style="font-size:14px;color:#991b1b;margin:6px 0">
      以下策略输出 <b>不是</b>最新交易日（{latest_lbl}），可能因脚本失败/超时而沿用了旧结果，请重跑对应策略后再参考：
    </div>
    <ul style="font-size:13px;color:#7f1d1d;margin:0;padding-left:20px">{items}</ul>"""
        failed_block = ""
        if failed_tasks:
            fitems = "".join(f"<li><b>{t}</b></li>" for t in failed_tasks)
            failed_block = f"""
    <div style="font-size:14px;color:#991b1b;margin:6px 0">
      本次运行有 <b>{len(failed_tasks)}</b> 个策略失败/超时，其展示数据可能不是本轮结果：
    </div>
    <ul style="font-size:13px;color:#7f1d1d;margin:0;padding-left:20px">{fitems}</ul>"""
        stale_warning = f"""
  <div class="section" style="border-left:3px solid #ef4444;background:#fef2f2">
    <div class="section-title" style="color:#dc2626">
      <span>🚨 数据新鲜度告警</span>
    </div>{failed_block}{stale_block}
  </div>"""

    # 市场环境过滤器警告
    market_warning = ""
    if market_condition and market_condition.is_weak:
        market_warning = f"""
  <div class="section" style="border-left:3px solid #f59e0b;background:#fffbeb">
    <div class="section-title" style="color:#d97706">
      <span>⚠️ 市场环境预警</span>
    </div>
    <div style="font-size:14px;color:#92400e;margin-bottom:8px">
      当前市场处于调整期，触发过滤条件：{market_condition.weak_reason}
    </div>
    <div style="font-size:13px;color:#78350f">
      建议降低仓位、谨慎操作，共振标的仅作参考 | 
      {_market_breadth_text(market_condition)} | 
      资金净流出 {_market_net_outflow_yi(market_condition)}亿（全市场主力加总）
    </div>
  </div>"""

    # 市场情绪区 —— 用 MarketCondition 客观数据覆盖策略输出的情绪
    sentiment = market_meta.get("sentiment", "")
    position = market_meta.get("position", "")
    top_sectors = market_meta.get("top_sectors", [])

    # 用涨跌比重新推导真实情绪，覆盖盘后扫描追随策略的输出
    if market_condition:
        ratio = market_condition.adv_decline_ratio
        if ratio >= 1.5:
            real_sentiment = "亢奋"
        elif ratio >= 0.9:
            real_sentiment = "回暖"
        elif ratio >= 0.5:
            real_sentiment = "修复"
        else:
            real_sentiment = "冰点"
        # 如果真实情绪和 auto.md 不一致，用真实情绪覆盖并加标注
        if real_sentiment != sentiment:
            sentiment = real_sentiment

    market_section = ""
    if sentiment or market_condition:
        sentiment_color = {"亢奋": "#ef4444", "回暖": "#34d399", "冰点": "#3b82f6", "修复": "#f59e0b"}.get(sentiment, "#94a3b8")
        market_section = f"""
  <div class="section" style="border-left:3px solid {sentiment_color}">
    <div class="section-title" style="gap:12px">
      <span>市场情绪</span>
      <span style="color:{sentiment_color};font-size:20px;font-weight:800">{sentiment}</span>
      {'<span style="color:#64748b;font-size:13px">| 仓位建议：' + position + '</span>' if position else ''}
      {'<span style="color:#94a3b8;font-size:12px">（' + _market_breadth_text(market_condition) + '，资金净流出' + _market_net_outflow_yi(market_condition) + '亿）</span>' if market_condition and market_condition.net_mf_amount < 0 else ''}
    </div>
  </div>"""

    # 共振区
    resonance_rows = ""
    has_moneyflow = "moneyflow_label" in resonance.columns if not resonance.empty else False
    for _, r in resonance.iterrows():
        badges = "".join(_badge(s.strip(), STRATEGY_COLORS.get(s.strip(), "#6366f1"))
                         for s in r["strategies"].split("、"))
        # 资金策略标签
        if has_moneyflow:
            mf_label = r.get("moneyflow_label", "")
            mf_trend = r.get("moneyflow_stock_trend", "")
            mf_sector = r.get("moneyflow_sector_trend", "")
            mf_advice = r.get("moneyflow_advice", "")
            # 根据标签类型着色
            label_style = {"🌟 资金共振": "#7c3aed", "✅ 资金看好": "#059669",
                           "⚡ 资金回流": "#2563eb", "⚠️ 资金恶化": "#d97706",
                           "🔻 资金逃离": "#dc2626", "🔄 流出收窄": "#d97706",
                           "➖ 资金震荡": "#64748b", "📊 数据不足": "#94a3b8"}.get(mf_label, "#6366f1")
            mf_cell = f'<td style="max-width:260px"><span style="display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:600;color:#fff;background:{label_style}">{mf_label}</span><br><span style="font-size:11px;color:#64748b" title="{mf_advice}">{mf_trend} | {mf_sector}</span></td>'
        else:
            mf_cell = '<td style="color:#94a3b8;font-size:11px">—</td>'

        resonance_rows += f"""<tr>
            <td style="color:#b45309;font-weight:700">{r['ts_code']}</td>
            <td style="font-weight:600">{r['name']}</td>
            <td>{badges}</td>
            <td style="text-align:center;font-weight:700">{r['strategy_count']}</td>
            <td style="text-align:center;color:{_score_color(r['avg_score'])};font-weight:700">{r['avg_score']:.1f}</td>
            {mf_cell}
        </tr>"""

    # 弱势行情提醒（仅提醒，不改变共振门槛）
    weak_resonance_note = ""
    if market_condition and market_condition.is_weak:
        weak_resonance_note = (
            '<div style="margin-top:10px;padding:8px 12px;background:#fffbeb;'
            'border:1px solid #fbbf24;border-radius:6px;font-size:13px;color:#92400e">'
            '⚠️ 当前市场处于调整期（涨跌比 {:.2f}），共振标的仅作参考，建议降低仓位谨慎操作'
            '</div>'.format(market_condition.adv_decline_ratio)
        )

    resonance_title = '<div class="section-title">多策略共振（被 2 个及以上策略同时选中）</div>'
    if not resonance_rows:
        resonance_section = resonance_title + '<p style="color:#94a3b8;text-align:center;padding:20px">今日暂无多策略共振标的</p>'
    else:
        resonance_section = resonance_title + f"""<table><thead><tr>
            <th>代码</th><th>名称</th><th>命中策略</th><th>命中数</th><th>均分</th><th>资金趋势 / 策略</th>
        </tr></thead><tbody>{resonance_rows}</tbody></table>"""
    resonance_section += weak_resonance_note

    # 策略 tabs -- 分离出 __html 键
    original_html_map = {}
    pure_strategy_dfs = {}
    for k, v in strategy_dfs.items():
        if k.endswith("__html"):
            original_html_map[k.replace("__html", "")] = v
        else:
            pure_strategy_dfs[k] = v

    tab_buttons = ""
    tab_panels = ""
    strategy_count = len(pure_strategy_dfs)
    for i, (sname, sdf) in enumerate(pure_strategy_dfs.items()):
        active = "active" if i == 0 else ""
        color = STRATEGY_COLORS.get(sname, "#6366f1")
        tab_buttons += f'<button class="tab-btn {active}" data-tab="tab{i}" style="--accent:{color}">{sname} ({len(sdf)})</button>'

        panel_display = "block" if i == 0 else "none"
        picks_table = _render_strategy_picks_table(sdf, sname)

        if sname in original_html_map:
            tab_panels += f"""<div class="tab-panel" id="tab{i}" style="display:{panel_display}">
                <div style="font-size:15px;font-weight:700;margin-bottom:10px;color:#1e40af">
                  今日推荐标的（{len(sdf)} 只，来源 reports/auto.md）
                </div>
                {picks_table}
                <hr style="margin:22px 0;border:none;border-top:1px solid #e2e8f0">
                {original_html_map[sname]}
            </div>"""
            continue

        storage_intro = _storage_tab_summary(sdf) if sname == "存储IPO供应链" else ""
        tab_panels += f"""<div class="tab-panel" id="tab{i}" style="display:{panel_display}">
            {storage_intro}
            {picks_table}
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>统一选股看板 {today}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ background:#f8fafc; color:#1e293b; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",sans-serif; padding:20px; }}
.container {{ max-width:1200px; margin:0 auto; }}
h1 {{ text-align:center; font-size:24px; color:#0f172a; margin-bottom:4px; }}
.subtitle {{ text-align:center; color:#64748b; font-size:13px; margin-bottom:24px; }}
.stats {{ display:flex; gap:16px; justify-content:center; margin-bottom:28px; flex-wrap:wrap; }}
.stat-card {{ background:#fff; border:1px solid #e2e8f0; border-radius:10px; padding:16px 28px; text-align:center; min-width:140px; box-shadow:0 1px 3px rgba(0,0,0,0.06); }}
.stat-card .num {{ font-size:28px; font-weight:800; }}
.stat-card .lbl {{ font-size:12px; color:#64748b; margin-top:4px; }}
.section {{ background:#fff; border:1px solid #e2e8f0; border-radius:10px; padding:20px; margin-bottom:20px; box-shadow:0 1px 3px rgba(0,0,0,0.06); }}
.section-title {{ font-size:16px; font-weight:700; color:#0f172a; margin-bottom:14px; display:flex; align-items:center; gap:8px; }}
.section-title::before {{ content:""; display:inline-block; width:4px; height:18px; border-radius:2px; }}
.resonance .section-title::before {{ background:#d97706; }}
.strategies .section-title::before {{ background:#6366f1; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th {{ background:#f1f5f9; color:#475569; padding:8px 10px; text-align:left; font-weight:600; position:sticky; top:0; white-space:nowrap; border-bottom:2px solid #e2e8f0; }}
td {{ padding:7px 10px; border-bottom:1px solid #f1f5f9; color:#334155; }}
tr:hover {{ background:#f8fafc; }}
.tabs {{ display:flex; gap:6px; margin-bottom:14px; flex-wrap:wrap; }}
.tab-btn {{ background:#f1f5f9; color:#475569; border:1px solid #e2e8f0; padding:8px 18px; border-radius:6px; cursor:pointer; font-size:13px; font-weight:600; transition:all .2s; }}
.tab-btn:hover {{ background:#e2e8f0; color:#1e293b; }}
.tab-btn.active {{ background:var(--accent,#6366f1); color:#fff; border-color:var(--accent,#6366f1); }}
.footer {{ text-align:center; color:#94a3b8; font-size:11px; margin-top:24px; padding:16px; }}
@media(max-width:768px) {{
  body {{ padding:10px; }}
  .stat-card {{ min-width:100px; padding:12px 16px; }}
  .stat-card .num {{ font-size:22px; }}
  table {{ font-size:12px; }}
  th, td {{ padding:5px 6px; }}
}}
</style>
</head>
<body>
<div class="container">
  <h1>统一选股看板</h1>
  <p class="subtitle">{today} &nbsp;|&nbsp; 五策略聚合 &middot; 自动生成{_data_date_subtitle(market_meta)}</p>

  <div class="stats">
    <div class="stat-card">
      <div class="num" style="color:#3b82f6">{total_stocks}</div>
      <div class="lbl">入选标的总数</div>
    </div>
    <div class="stat-card">
      <div class="num" style="color:#fbbf24">{resonance_count}</div>
      <div class="lbl">多策略共振</div>
    </div>
    <div class="stat-card">
      <div class="num" style="color:#10b981">{strategy_count}</div>
      <div class="lbl">策略数量</div>
    </div>
  </div>

  {stale_warning}

  {market_warning}

  {market_section}

  <div class="section resonance">
    {resonance_section}
  </div>

  <div class="section strategies">
    <div class="section-title">各策略选股详情</div>
    <div class="tabs">{tab_buttons}</div>
    {tab_panels}
  </div>

  {_render_weak_strategies_html(oversold_candidates, defensive_stocks)}

  <div class="footer">
    数据来源：擒龙猎手 / 主升行情启动 / 盘后扫描追随 / 量化蓄势突破 / 存储IPO供应链<br>
    仅为量化模型推演，不构成交易建议，入市有风险，投资需谨慎。
  </div>
</div>
<script>
document.querySelectorAll('.tab-btn').forEach(btn => {{
  btn.addEventListener('click', () => {{
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.style.display = 'none');
    btn.classList.add('active');
    document.getElementById(btn.dataset.tab).style.display = 'block';
  }});
}});
</script>
</body>
</html>"""
    return html


# ── 5. 微信转发文本渲染 ──────────────────────────────────────

def render_wechat(all_df: pd.DataFrame, resonance: pd.DataFrame, strategy_dfs: dict, market_meta: dict = None, market_condition: Optional[MarketCondition] = None, oversold_candidates: list = None, defensive_stocks: list = None) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    market_meta = market_meta or {}
    data_td = str(market_meta.get("data_trade_date", "") or market_meta.get("trade_date", "")).strip()
    strat_td = str(market_meta.get("strategy_trade_date", "")).strip()
    if len(data_td) == 8 and data_td.isdigit():
        date_line = f"📅 大盘统计日：{data_td[:4]}-{data_td[4:6]}-{data_td[6:8]}"
        if strat_td and strat_td != data_td and len(strat_td) == 8:
            date_line += f" | 选股结果日：{strat_td[:4]}-{strat_td[4:6]}-{strat_td[6:8]}"
    elif len(strat_td) == 8 and strat_td.isdigit():
        date_line = f"📅 选股结果日：{strat_td[:4]}-{strat_td[4:6]}-{strat_td[6:8]}"
    else:
        date_line = f"📅 日期：{today}"
    lines = [
        f"📊 【五策略统一选股报告】",
        date_line,
    ]

    sentiment = market_meta.get("sentiment", "")
    position = market_meta.get("position", "")

    # 用涨跌比覆盖情绪（和 HTML 看板保持一致）
    if market_condition:
        ratio = market_condition.adv_decline_ratio
        if ratio >= 1.5:
            real_sentiment = "亢奋"
        elif ratio >= 0.9:
            real_sentiment = "回暖"
        elif ratio >= 0.5:
            real_sentiment = "修复"
        else:
            real_sentiment = "冰点"
        sentiment = real_sentiment

    if sentiment:
        lines.append(f"🌡️ 市场情绪：{sentiment}" + (f"｜仓位建议：{position}" if position else "")
                     + (f"（涨跌比 {market_condition.adv_decline_ratio}）" if market_condition else ""))

    # 市场环境检测
    if market_condition and market_condition.is_weak:
        lines.append(f"⚠️ 市场预警：调整期（{market_condition.weak_reason}）")
        lines.append(
            f"   {_market_breadth_text(market_condition)} | "
            f"资金净流出 {_market_net_outflow_yi(market_condition)}亿"
        )
        lines.append(f"   共振标的仅作参考，建议谨慎操作")

    lines.append("━" * 20)

    if not resonance.empty:
        lines.append("")
        lines.append("🔥 多策略共振精选：")
        has_mf = "moneyflow_label" in resonance.columns
        for _, r in resonance.iterrows():
            mf_tag = ""
            if has_mf:
                mf_label = r.get("moneyflow_label", "")
                mf_advice = r.get("moneyflow_advice", "")
                if mf_label and "数据不足" not in mf_label:
                    mf_tag = f" | {mf_label}"
            lines.append(f"  ⭐ {r['name']}({r['ts_code']}) | 命中{r['strategy_count']}策略 [{r['strategies']}] | 均分{r['avg_score']:.0f}{mf_tag}")
        lines.append("━" * 20)

    # 弱势行情辅助策略
    if oversold_candidates:
        lines.append("")
        lines.append("📉 超跌反弹候选：")
        for item in oversold_candidates[:5]:
            lines.append(f"  ⚡ {item['name']}({item['ts_code']}) | 乖离率{item['bias']:.1f}% | 今日{item['pct_chg']:+.2f}%")
        lines.append("━" * 20)

    if defensive_stocks:
        lines.append("")
        lines.append("🛡️ 防守板块关注：")
        # 按板块分组
        seen_sectors = set()
        for item in defensive_stocks[:8]:
            if item['sector'] not in seen_sectors:
                lines.append(f"  【{item['sector']}】")
                seen_sectors.add(item['sector'])
            lines.append(f"    {item['name']}({item['ts_code']}) | +{item['pct_chg']:.2f}%")
        lines.append("━" * 20)

    medals = ["🥇", "🥈", "🥉"]
    for sname, sdf in strategy_dfs.items():
        if sname.endswith("__html") or not isinstance(sdf, pd.DataFrame):
            continue
        lines.append("")
        lines.append(f"📌 {sname}（Top 3）：")
        for i, (_, row) in enumerate(sdf.head(3).iterrows()):
            medal = medals[i] if i < 3 else "  "
            concept = str(row.get("concept", "")) if pd.notna(row.get("concept")) else ""
            concept_short = concept.split(",")[0].split("、")[0].strip() if concept else ""
            concept_str = f" | {concept_short}" if concept_short else ""
            lines.append(f"  {medal} {row['name']}({row['ts_code']}) | 分数{row['score_norm']:.0f}{concept_str}")

    lines.append("")
    lines.append("━" * 20)
    lines.append("⚠️ 仅为量化模型推演，不构成交易建议")
    return "\n".join(lines)


# ── 6. 主流程 ────────────────────────────────────────────────

def main():
    config = load_config()
    strategies = config["strategies"]

    all_dfs = []
    strategy_dfs = {}
    market_meta = {"sentiment": "", "position": "", "trade_date": ""}
    trade_dates: list[str] = []

    # 先从各策略输出推断最近交易日，供量化 CSV 对齐（动态，非写死）
    ref_dates = [_peek_trade_date_from_strategy(s) for s in strategies]
    ref_dates = [d for d in ref_dates if d]
    prefer_trade_date = max(ref_dates) if ref_dates else None
    if prefer_trade_date:
        print(f"参考交易日: {prefer_trade_date}（由各策略输出自动推断）")

    print("=" * 50)
    print("  统一选股输出聚合器")
    print("=" * 50)

    market_condition = None
    from datetime import datetime
    _today_str = datetime.now().strftime("%Y%m%d")

    for s in strategies:
        if prefer_trade_date:
            s["_prefer_trade_date"] = prefer_trade_date
        print(f"\n▸ 解析: {s['name']} ...")
        df = parse_strategy(s, market_condition)
        if df.empty:
            print(f"  ⚠ 无数据")
            continue
        df = df.sort_values("score_norm", ascending=False).reset_index(drop=True)
        print(f"  ✓ 读取到 {len(df)} 只标的，最高分 {df['score_norm'].max():.1f}")
        rdf = df
        resonance_tiers = s.get("resonance_tiers")
        if resonance_tiers and "档位" in df.columns:
            rdf = df[df["档位"].isin(resonance_tiers)]
            print(f"  ↳ 参与共振: {len(rdf)} 只（档位 {resonance_tiers}）")
        all_dfs.append(rdf)
        strategy_dfs[s["name"]] = df
        if "_md_meta" in s:
            market_meta = {**market_meta, **s["_md_meta"]}
        if "_trade_date" in s:
            trade_dates.append(s["_trade_date"])
        if "_original_html" in s:
            strategy_dfs[s["name"] + "__html"] = s["_original_html"]

    if not all_dfs:
        print("\n❌ 所有策略均无数据，请先运行各选股脚本。")
        sys.exit(1)

    if trade_dates:
        market_meta["trade_date"] = max(trade_dates)
    elif prefer_trade_date:
        market_meta["trade_date"] = prefer_trade_date

    # ── 市场环境：始终以本地最新 daily 为准（每次聚合重算，避免卡在旧策略日）───
    trade_date_for_check = _resolve_market_trade_date(
        market_meta.get("trade_date", "") or prefer_trade_date or _today_str
    )
    if trade_date_for_check:
        market_condition = check_market_condition(trade_date_for_check)
        market_meta["data_trade_date"] = trade_date_for_check
        strat_td = str(market_meta.get("trade_date", "") or "").strip()
        if strat_td and strat_td != trade_date_for_check:
            market_meta["strategy_trade_date"] = strat_td
        print(f"市场环境统计日: {trade_date_for_check}（本地 daily 最新可用日）")
        if strat_td and strat_td != trade_date_for_check:
            print(f"  策略输出日: {strat_td}（选股结果日，可与大盘统计日不同）")

        # ── 数据新鲜度校验：逐策略对比真实交易日 vs 最新开市日 ──
        stale_strategies = _detect_stale_strategies(
            strategies, set(strategy_dfs.keys()), trade_date_for_check
        )
        if stale_strategies:
            market_meta["stale_strategies"] = stale_strategies
            print(
                f"\n🚨 数据新鲜度告警：以下策略输出非最新交易日 "
                f"{trade_date_for_check}（可能脚本失败/超时，沿用了旧结果）："
            )
            for st in stale_strategies:
                print(f"   - {st['name']}: {st['date'] or '未知日期'}（请重跑该策略）")
        else:
            print("✅ 数据新鲜度校验通过：所有策略输出均为最新交易日")

        # 合并本次运行失败/超时的策略（即便其旧文件恰好看起来是最新的，也显式提示）
        run_status = _load_run_status()
        failed_tasks = run_status.get("failed") or []
        if failed_tasks:
            market_meta["failed_tasks"] = failed_tasks
            print(
                f"⚠️ 本次运行有 {len(failed_tasks)} 个策略失败/超时："
                f"{', '.join(failed_tasks)}（看板已标注）"
            )
    if market_condition and market_condition.is_weak:
        print(f"\n⚠️ 市场环境预警: {market_condition.weak_reason}")
        print(
            f"   {_market_breadth_text(market_condition)} | "
            f"资金净流出 {_market_net_outflow_yi(market_condition)}亿"
        )
        print(f"   共振标的仅作参考，建议谨慎操作")
    elif market_condition:
        print(f"\n✅ 市场环境正常（涨跌比 {market_condition.adv_decline_ratio}）")

    # ── 弱势行情辅助分析 ──
    oversold_candidates = []
    defensive_stocks = []
    if trade_date_for_check:
        print("\n📊 弱势行情辅助分析（超跌反弹 + 防守板块）...")
        oversold_candidates = compute_oversold_rebound_candidates(trade_date_for_check, top_n=10)
        defensive_stocks = compute_defensive_sector_stocks(trade_date_for_check, top_per_sector=3)
        if oversold_candidates:
            print(f"  ✓ 超跌反弹: {len(oversold_candidates)} 只候选")
        else:
            print(f"  - 超跌反弹: 无符合条件的标的")
        if defensive_stocks:
            sectors = set(d["sector"] for d in defensive_stocks)
            print(f"  ✓ 防守板块: {len(defensive_stocks)} 只（{', '.join(sorted(sectors))}）")
        else:
            print(f"  - 防守板块: 今日无数据")

    all_df = pd.concat(all_dfs, ignore_index=True)

    # ── 共振检测 + 资金趋势分析 ──
    resonance = detect_resonance(all_df, market_condition)
    if not resonance.empty:
        print(f"\n🔥 多策略共振: {len(resonance)} 只标的")
        # 资金趋势必须与大盘统计日为同一交易日（禁止单独取 moneyflow 最大分区，避免 60525 等非交易日脏文件）
        _moneyflow_trade_date = trade_date_for_check
        if (DATA_DIR / "moneyflow" / f"{_moneyflow_trade_date}.parquet").exists():
            resonance = enrich_resonance_with_moneyflow(
                resonance, _moneyflow_trade_date, pick_universe=all_df
            )
            if "moneyflow_label" in resonance.columns:
                for _, r in resonance.iterrows():
                    label = r.get("moneyflow_label", "")
                    trend = r.get("moneyflow_stock_trend", "")
                    sector = r.get("moneyflow_sector_trend", "")
                    print(f"   ⭐ {r['name']}({r['ts_code']}) - 命中 {r['strategy_count']} 策略 [{r['strategies']}] | {label} {trend} {sector}")
            else:
                for _, r in resonance.iterrows():
                    print(f"   ⭐ {r['name']}({r['ts_code']}) - 命中 {r['strategy_count']} 策略 [{r['strategies']}]")
    else:
        print(f"\n📊 今日无多策略共振标的（被 2 个及以上策略同时选中）")

    # 输出
    OUTPUT_DIR.mkdir(exist_ok=True)

    html = render_html(all_df, resonance, strategy_dfs, market_meta, market_condition, oversold_candidates, defensive_stocks)
    html_path = OUTPUT_DIR / "unified_dashboard.html"
    html_path.write_text(html, encoding="utf-8")
    print(f"\n✅ HTML 看板: {html_path}")

    wechat = render_wechat(all_df, resonance, strategy_dfs, market_meta, market_condition, oversold_candidates, defensive_stocks)
    wechat_path = OUTPUT_DIR / "unified_wechat.txt"
    wechat_path.write_text(wechat, encoding="utf-8")
    print(f"✅ 微信文本: {wechat_path}")

    # 汇总 CSV
    csv_path = OUTPUT_DIR / "unified_all_picks.csv"
    all_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"✅ 汇总 CSV: {csv_path}")

    print(f"\n{'=' * 50}")
    print(f"  完成！共 {all_df['ts_code'].nunique()} 只不重复标的")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    main()
