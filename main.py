# -*- coding: utf-8 -*-
"""
尾盘先手选股策略
版本：v2.8 (中军协同 + 均线多头可选 + 动态止盈止损 + 大盘20日线过滤)
运行时间：每个交易日 14:45 左右
输出：控制台打印 + result.json
"""

import os
import time
import json
import warnings
import pandas as pd
import numpy as np
import akshare as ak
from datetime import datetime, timedelta, time as datetime_time
from collections import defaultdict
from env_loader import load_env_file

load_env_file(".env")

# ========== 时区设置 ==========
os.environ['TZ'] = 'Asia/Shanghai'
if hasattr(time, "tzset"):
    time.tzset()  # Unix：使 datetime.now() 对齐 TZ；Windows 无 tzset，依赖本机时区

warnings.filterwarnings("ignore")

# ========== 基础配置 ==========
CONFIG = {
    # 基础阈值（会被动态覆盖）
    "MIN_BOARD_STOCKS": 5,          # 板块爆发最小涨停家数
    "MIN_BOARD_PCT": 2.5,           # 板块平均涨幅下限(%)
    "MAX_CHANGE_PCT": 8,            # 个股最大涨幅(%)
    "MIN_CHANGE_PCT": 0,            # 个股最小涨幅(%)
    "MIN_VOLUME": 3,                # 个股最小成交额(亿)
    "MIN_TURNOVER": 5,              # 个股最小换手率(%)
    "MAX_TURNOVER": 25,             # 个股最大换手率(%)
    "MIN_MARKET_CAP": 20,           # 个股最小流通市值(亿)
    "MAX_MARKET_CAP": 80,           # 个股最大流通市值(亿)
    "TOP_N": 3,                     # 每个板块最多选几个
    "EXCLUDE_BOARDS": ['688', '8'], # 排除科创板、北交所
    "MAX_CONCEPTS": 50,             # 最多分析的概念板块数量
    "LOG_DIR": "trade_logs",        # 日志目录
    "ENABLE_MINUTE": True,          # 是否获取分时数据
    "ENABLE_MA_FILTER": True,       # 是否做日线 MA5/10/20 多头过滤（候选票上会多请求日线）
}

CONFIG["ENABLE_MINUTE"] = str(os.environ.get("ENABLE_MINUTE", str(CONFIG["ENABLE_MINUTE"]))).lower() in (
    "1",
    "true",
    "yes",
    "on",
)
CONFIG["ENABLE_MA_FILTER"] = str(os.environ.get("ENABLE_MA_FILTER", str(CONFIG["ENABLE_MA_FILTER"]))).lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# 全局变量
all_candidates = []

# 个股日线 MA 缓存（代码 -> 是否多头排列）
_ma_cache = {}


def is_trading_time():
    """判断是否在交易时段（9:30-11:30, 13:00-15:00）"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    current = now.time()
    if (datetime_time(9, 30) <= current <= datetime_time(11, 30) or
        datetime_time(13, 0) <= current <= datetime_time(15, 0)):
        return True
    return False


def log(msg, print_console=True):
    """写入日志文件并打印"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_msg = f"[{timestamp}] {msg}"
    if print_console:
        print(log_msg)
    os.makedirs(CONFIG["LOG_DIR"], exist_ok=True)
    log_file = os.path.join(CONFIG["LOG_DIR"], f"{datetime.now().strftime('%Y%m%d')}.log")
    with open(log_file, 'a', encoding='utf-8') as f:
        f.write(log_msg + '\n')


def safe_request(func, *args, **kwargs):
    """带重试的请求包装器"""
    for i in range(3):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            log(f"请求失败({i+1}/3): {e}")
            time.sleep(2)
    return None


def get_market_volume():
    """
    获取沪深两市总成交额（亿元）
    优先使用实时数据，失败则取上一交易日收盘数据
    """
    # 方法1：实时行情（必须用「沪深重要指数」，默认「上证系列指数」只有沪市，无法配到深证）
    try:
        df = safe_request(ak.stock_zh_index_spot_em, symbol="沪深重要指数")
        if df is not None and not df.empty:
            if not hasattr(get_market_volume, "_debug_logged"):
                log(f"指数数据样例: {df.head(2).to_dict()}")
                get_market_volume._debug_logged = True
            name_col = '名称' if '名称' in df.columns else 'name'
            code_col = '代码' if '代码' in df.columns else 'code'
            vol_col = '成交额' if '成交额' in df.columns else 'amount'
            # 优先用固定代码：上证指数 000001、深证成指 399001（名称可能变更）
            sh_rows = df[df[code_col].astype(str).str.replace(r'\.0$', '', regex=True) == '000001']
            sz_rows = df[df[code_col].astype(str).str.replace(r'\.0$', '', regex=True) == '399001']
            if sh_rows.empty:
                sh_rows = df[df[name_col].str.contains('上证指数|上证综指', na=False, regex=True)]
            if sz_rows.empty:
                sz_rows = df[
                    df[name_col].str.contains('深证成指|深证指数|深成指', na=False, regex=True)
                ]
            if not sh_rows.empty and not sz_rows.empty:
                vol_sh = float(sh_rows.iloc[0][vol_col]) / 1e8
                vol_sz = float(sz_rows.iloc[0][vol_col]) / 1e8
                return vol_sh + vol_sz
    except Exception as e:
        log(f"获取实时成交额失败: {e}")

    # 方法2：历史日线（东财接口须 sh000001 / sz399001，列为 amount 非「成交额」）
    try:
        sh_hist = safe_request(ak.stock_zh_index_daily_em, symbol="sh000001")
        sz_hist = safe_request(ak.stock_zh_index_daily_em, symbol="sz399001")
        if sh_hist is not None and sz_hist is not None and len(sh_hist) > 0 and len(sz_hist) > 0:
            amt_col = 'amount' if 'amount' in sh_hist.columns else '成交额'
            vol_sh = float(sh_hist.iloc[-1][amt_col]) / 1e8
            vol_sz = float(sz_hist.iloc[-1][amt_col]) / 1e8
            return vol_sh + vol_sz
    except Exception as e:
        log(f"获取历史成交额失败: {e}")

    return 0.0


def get_market_trend():
    """上证指数收盘是否在 20 日均线上方；数据不足或失败时默认 True，不挡交易。"""
    try:
        df = safe_request(ak.stock_zh_index_daily_em, symbol="sh000001")
        if df is None or len(df) < 20:
            return True
        close = pd.to_numeric(df["close"], errors="coerce")
        ma20 = close.rolling(20).mean()
        last_close = close.iloc[-1]
        last_ma = ma20.iloc[-1]
        if pd.isna(last_close) or pd.isna(last_ma):
            return True
        return last_close > last_ma
    except Exception:
        return True


def get_ma_status(code):
    """MA5>MA10>MA20 且 收盘>MA5（前复权日线）。"""
    sym = str(code).strip().zfill(6)
    if sym in _ma_cache:
        return _ma_cache[sym]
    if not hasattr(ak, "stock_zh_a_hist"):
        _ma_cache[sym] = False
        return False
    try:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=150)).strftime("%Y%m%d")
        df = safe_request(
            ak.stock_zh_a_hist,
            symbol=sym,
            period="daily",
            start_date=start,
            end_date=end,
            adjust="qfq",
        )
        if df is None or len(df) < 20:
            _ma_cache[sym] = False
            return False
        c = pd.to_numeric(df["收盘"], errors="coerce")
        ma5 = c.rolling(5).mean()
        ma10 = c.rolling(10).mean()
        ma20 = c.rolling(20).mean()
        l5, l10, l20 = ma5.iloc[-1], ma10.iloc[-1], ma20.iloc[-1]
        lc = c.iloc[-1]
        if pd.isna(l5) or pd.isna(l10) or pd.isna(l20) or pd.isna(lc):
            _ma_cache[sym] = False
            return False
        ok = (l5 > l10 > l20) and (lc > l5)
        _ma_cache[sym] = bool(ok)
        return bool(ok)
    except Exception:
        _ma_cache[sym] = False
        return False


def get_dynamic_config(market_vol):
    """
    根据市场总成交额返回当前应使用的参数配置
    返回：config_dict 或 None（空仓）
    """
    if market_vol >= 20000:
        log(f"当前成交额 {market_vol:.0f}亿 >= 2万亿，使用【大市值模式】")
        return {
            "mode": "large",
            "MIN_BOARD_STOCKS": 8,
            "MIN_BOARD_PCT": 3.0,
            "MIN_MARKET_CAP": 100,       # 亿
            "MAX_MARKET_CAP": 1000,      # 亿，避免超大盘弹性过低
            "MAX_CHANGE_PCT": 8,
            "MIN_TURNOVER": 2,
            "MAX_TURNOVER": 15,
            "MIN_VOLUME": 10,            # 亿
            "TOP_N": 3,
            "MIN_CHANGE_PCT": 0,
        }
    elif market_vol >= 12000:
        log(f"当前成交额 {market_vol:.0f}亿 (1.2万亿~2万亿)，使用【均衡模式】")
        return {
            "mode": "balanced",
            "MIN_BOARD_STOCKS": 6,
            "MIN_BOARD_PCT": 2.5,
            "MIN_MARKET_CAP": 50,
            "MAX_MARKET_CAP": 200,
            "MAX_CHANGE_PCT": 6,
            "MIN_TURNOVER": 3,
            "MAX_TURNOVER": 20,
            "MIN_VOLUME": 5,
            "TOP_N": 3,
            "MIN_CHANGE_PCT": 0,
        }
    elif market_vol >= 8000:
        log(f"当前成交额 {market_vol:.0f}亿 (8000亿~1.2万亿)，使用【小市值模式】")
        return {
            "mode": "small",
            "MIN_BOARD_STOCKS": 5,
            "MIN_BOARD_PCT": 2.5,
            "MIN_MARKET_CAP": 20,
            "MAX_MARKET_CAP": 80,
            "MAX_CHANGE_PCT": 6,
            "MIN_TURNOVER": 5,
            "MAX_TURNOVER": 25,
            "MIN_VOLUME": 3,
            "TOP_N": 3,
            "MIN_CHANGE_PCT": 0,
        }
    else:
        log(f"当前成交额 {market_vol:.0f}亿 < 8000亿，【空仓模式】，不交易")
        return None


def get_all_stocks():
    """获取沪深A股实时行情，过滤科创板/北交所，含量比。
    说明：在部分网络环境下 `stock_zh_a_spot_em` 可能断连，
    此时自动降级为 `stock_sh_a_spot_em` + `stock_sz_a_spot_em` 合并。"""
    df = safe_request(ak.stock_zh_a_spot_em)
    if df is None:
        # 降级：分开拉沪市/深市，减少单次请求体量与潜在拦截
        sh_df = safe_request(ak.stock_sh_a_spot_em)
        sz_df = safe_request(ak.stock_sz_a_spot_em)
        if sh_df is not None and sz_df is not None and not sh_df.empty and not sz_df.empty:
            df = pd.concat([sh_df, sz_df], ignore_index=True)
        elif sh_df is not None and not sh_df.empty:
            df = sh_df
        elif sz_df is not None and not sz_df.empty:
            df = sz_df
        else:
            return None
    # 过滤科创板、北交所
    df = df[~df['代码'].str.startswith(tuple(CONFIG["EXCLUDE_BOARDS"]))]
    cols = ['代码', '名称', '最新价', '涨跌幅', '成交额', '换手率', '流通市值', '量比']
    for col in cols:
        if col not in df.columns:
            df[col] = 1.0 if col == '量比' else np.nan
    df = df[cols]
    for col in ['涨跌幅', '成交额', '换手率', '流通市值', '量比']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df['量比'] = df['量比'].fillna(1.0)
    df = df.dropna(subset=['涨跌幅', '成交额', '换手率', '流通市值'])
    return df


def get_board_heat(stock_df):
    """找出当日强势概念板块"""
    # 获取概念板块列表
    concept_list = safe_request(ak.stock_board_concept_name_em)
    if concept_list is None:
        return []
    concepts = concept_list.head(CONFIG["MAX_CONCEPTS"])['板块名称'].tolist()
    hot_boards = []
    for concept in concepts:
        try:
            cons = safe_request(ak.stock_board_concept_cons_em, symbol=concept)
            if cons is None or cons.empty or '代码' not in cons.columns:
                continue
            # 只取成份代码再并行情，避免与 stock_df 同名列产生 涨跌幅_x / 涨跌幅_y
            codes = cons[['代码']].drop_duplicates()
            merged = pd.merge(codes, stock_df, on='代码', how='inner')
            if len(merged) == 0:
                continue
            # 涨停家数（涨幅>=9.8%）
            limit_stocks = merged[merged['涨跌幅'] >= 9.8]
            cnt = len(limit_stocks)
            if cnt >= CONFIG["MIN_BOARD_STOCKS"]:
                avg_pct = merged['涨跌幅'].mean()
                if avg_pct >= CONFIG["MIN_BOARD_PCT"]:
                    # 额外条件：板块内成交额最大的个股涨幅>2%（避免小票自嗨）
                    largest_vol_stock = merged.sort_values('成交额', ascending=False).iloc[0]
                    if largest_vol_stock['涨跌幅'] >= avg_pct * 0.8:
                        hot_boards.append({
                            'name': concept,
                            'limit_count': cnt,
                            'avg_pct': avg_pct,
                            'stocks': merged
                        })
        except Exception as e:
            log(f"处理板块 {concept} 失败: {e}")
            continue
    # 按平均涨幅排序
    hot_boards.sort(key=lambda x: x['avg_pct'], reverse=True)
    return hot_boards


def calculate_score(stock, board, rank_in_board):
    """综合评分（0-12）"""
    score = 0.0
    # 1. 板块涨停家数 (0-2)
    limit_cnt = board['limit_count']
    if limit_cnt >= 10:
        score += 2
    elif limit_cnt >= 7:
        score += 1.5
    elif limit_cnt >= 5:
        score += 1

    # 2. 板块平均涨幅 (0-2)
    avg_pct = board['avg_pct']
    if avg_pct >= 5:
        score += 2
    elif avg_pct >= 3.5:
        score += 1.5
    elif avg_pct >= 2.5:
        score += 1

    # 3. 个股成交额在板块内的排名 (0-2)
    if rank_in_board == 1:
        score += 2
    elif rank_in_board == 2:
        score += 1.5
    else:
        score += 1

    # 4. 个股涨幅 (0-2)
    pct = stock['涨跌幅']
    if 3 <= pct <= 6:
        score += 2
    elif 0 <= pct < 3 or 6 < pct <= 8:
        score += 1

    # 5. 个股换手率 (0-2)
    turnover = stock['换手率']
    if 10 <= turnover <= 20:
        score += 2
    elif 5 <= turnover < 10 or 20 < turnover <= 25:
        score += 1

    # 6. 量比 (0-2)
    vol_ratio = stock.get('量比', 1.0)
    try:
        vol_ratio = float(vol_ratio if vol_ratio is not None else 1.0)
    except (TypeError, ValueError):
        vol_ratio = 1.0
    if vol_ratio >= 2.0:
        score += 2
    elif vol_ratio >= 1.5:
        score += 1.5
    elif vol_ratio >= 1.2:
        score += 1

    return score


def filter_stocks_in_board(board, stock_df):
    """在板块内筛选符合条件的个股，返回带评分的列表"""
    stocks = board['stocks'].copy()
    vol_ratio = pd.to_numeric(stocks['量比'], errors='coerce').fillna(0.0)
    condition = (
        (stocks['流通市值'] >= CONFIG["MIN_MARKET_CAP"] * 1e8) &
        (stocks['流通市值'] <= CONFIG["MAX_MARKET_CAP"] * 1e8) &
        (stocks['涨跌幅'] >= CONFIG["MIN_CHANGE_PCT"]) &
        (stocks['涨跌幅'] <= CONFIG["MAX_CHANGE_PCT"]) &
        (stocks['成交额'] >= CONFIG["MIN_VOLUME"] * 1e8) &
        (stocks['换手率'] >= CONFIG["MIN_TURNOVER"]) &
        (stocks['换手率'] <= CONFIG["MAX_TURNOVER"]) &
        (vol_ratio >= 0.8)
    )
    filtered = stocks[condition].copy()
    if len(filtered) == 0:
        return []

    if CONFIG.get("ENABLE_MA_FILTER", True):
        keep = filtered['代码'].map(lambda c: get_ma_status(c))
        filtered = filtered[keep].copy()
        if len(filtered) == 0:
            return []

    # 按成交额排序，取前 TOP_N
    filtered = filtered.sort_values('成交额', ascending=False).head(CONFIG["TOP_N"])
    results = []
    for idx, (_, row) in enumerate(filtered.iterrows(), start=1):
        row_dict = row.to_dict()
        row_dict['板块'] = board['name']
        row_dict['板块平均涨幅'] = board['avg_pct']
        row_dict['板块涨停家数'] = board['limit_count']
        row_dict['板块内排名'] = idx
        row_dict['得分'] = calculate_score(row_dict, board, idx)
        results.append(row_dict)
    return results


def get_minute_line(code):
    """
    用东财 1 分钟走势（akshare 1 分钟周期）估算尾盘相对均价位置与近两根涨跌。
    失败返回 None。
    """
    try:
        if not hasattr(ak, "stock_zh_a_hist_min_em"):
            return None
        sym = str(code).strip().zfill(6)
        df = safe_request(ak.stock_zh_a_hist_min_em, symbol=sym, period="1", adjust="")
        if df is None or df.empty:
            return None
        df['amount'] = pd.to_numeric(df['成交额'], errors='coerce')
        df['volume'] = pd.to_numeric(df['成交量'], errors='coerce')
        df['收盘'] = pd.to_numeric(df['收盘'], errors='coerce')
        df = df.dropna(subset=['amount', 'volume', '收盘'])
        if len(df) == 0:
            return None
        df['cum_amount'] = df['amount'].cumsum()
        df['cum_volume'] = df['volume'].cumsum()
        df['avg_price'] = df['cum_amount'] / df['cum_volume']
        last_10 = df.tail(10)
        if len(last_10) == 0:
            return None
        above_avg = (last_10['收盘'] > last_10['avg_price']).mean() * 100
        if len(last_10) >= 2:
            last5_drop = (
                (last_10['收盘'].iloc[-1] - last_10['收盘'].iloc[-2])
                / last_10['收盘'].iloc[-2]
                * 100
            )
        else:
            last5_drop = 0.0
        return {'above_ratio': above_avg, 'last5_drop': last5_drop}
    except Exception as e:
        log(f"获取1分钟走势失败 {code}: {e}")
        return None


def main():
    global all_candidates
    log("========== 开始尾盘选股 ==========")

    # 1. 判断交易时间（非交易时段也允许运行，但会提示）
    if not is_trading_time():
        log("⚠️ 当前非交易时段，数据可能不是实时行情，请确认运行时间")

    # 2. 获取市场成交额，决定模式
    market_vol = get_market_volume()
    # 如果成交额为0，尝试使用历史日线再次获取（确保万无一失）
    if market_vol == 0:
        log("实时和历史成交额获取均失败，尝试再次获取历史日线...")
        try:
            sh_hist = ak.stock_zh_index_daily_em(symbol="sh000001")
            sz_hist = ak.stock_zh_index_daily_em(symbol="sz399001")
            if sh_hist is not None and sz_hist is not None and len(sh_hist) > 0 and len(sz_hist) > 0:
                amt_col = 'amount' if 'amount' in sh_hist.columns else '成交额'
                market_vol = (
                    float(sh_hist.iloc[-1][amt_col]) + float(sz_hist.iloc[-1][amt_col])
                ) / 1e8
                log(f"最终使用上一交易日成交额: {market_vol:.0f}亿")
        except Exception as e:
            log(f"再次获取历史成交额失败: {e}")

    log(f"全市场成交额: {market_vol:.0f}亿")
    if not get_market_trend():
        log("上证指数在20日均线下方，大盘趋势走弱，今日不交易")
        result = {
            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "market_volume": market_vol,
            "mode": "no_trade",
            "has_candidates": False,
            "unique_recommendation": None,
            "all_candidates": [],
            "candidates": [],
            "reason": "market_below_ma20",
        }
        with open("result.json", "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
        return

    dynamic_cfg = get_dynamic_config(market_vol)
    if dynamic_cfg is None:
        log("根据成交量判断，今日不适合交易，请空仓。")
        # 仍然输出一个空结果
        result = {
            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "market_volume": market_vol,
            "mode": "no_trade",
            "has_candidates": False,
            "unique_recommendation": None,
            "all_candidates": [],
            "candidates": [],
        }
        with open("result.json", "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
        return
    mode = dynamic_cfg["mode"]

    # 3. 更新全局配置
    for k, v in dynamic_cfg.items():
        if k in CONFIG:
            CONFIG[k] = v
    log(f"当前模式: {dynamic_cfg['mode']}，参数: { {k:v for k,v in dynamic_cfg.items() if k != 'mode'} }")

    # 4. 获取股票实时行情
    stock_df = get_all_stocks()
    if stock_df is None:
        log("获取股票数据失败，退出")
        return

    # 5. 获取热点板块
    hot_boards = get_board_heat(stock_df)
    if not hot_boards:
        log("未发现符合条件的爆发板块，今日无交易")
        return

    log(f"发现 {len(hot_boards)} 个热点板块: {[b['name'] for b in hot_boards]}")

    # 6. 筛选个股
    all_candidates = []
    for board in hot_boards:
        candidates = filter_stocks_in_board(board, stock_df)
        if candidates:
            all_candidates.extend(candidates)
            log(f"板块【{board['name']}】发现 {len(candidates)} 只候选")

    if not all_candidates:
        log("所有板块内均无符合条件的个股，今日无交易")
        return

    # 7. 综合评分排序
    all_candidates.sort(key=lambda x: (x['得分'], x['成交额']), reverse=True)

    log("\n=== 今日尾盘候选票（按推荐优先级排序，节选） ===")
    for i, c in enumerate(all_candidates[:15]):
        log(f"{i+1}. {c['名称']}({c['代码']}) 板块:{c['板块']} "
            f"涨幅:{c['涨跌幅']:.2f}% 成交额:{c['成交额']/1e8:.1f}亿 "
            f"换手:{c['换手率']:.1f}% 得分:{c['得分']:.1f}")

    # 8. 唯一推荐：最强板块（涨停家数*2+平均涨幅）+ 该板块内得分第一
    board_stats = defaultdict(lambda: {'count': 0, 'avg_pct': 0.0, 'stocks': []})
    for c in all_candidates:
        bname = c['板块']
        board_stats[bname]['count'] = c['板块涨停家数']
        board_stats[bname]['avg_pct'] = c['板块平均涨幅']
        board_stats[bname]['stocks'].append(c)

    best_board_name = None
    best_board_score = -1.0
    for bname, stat in board_stats.items():
        brd_score = stat['count'] * 2 + stat['avg_pct']
        if brd_score > best_board_score:
            best_board_score = brd_score
            best_board_name = bname

    unique_recommendation = None
    if best_board_name:
        stocks_in_board = board_stats[best_board_name]['stocks']
        stocks_in_board.sort(key=lambda x: (x['得分'], x['成交额']), reverse=True)
        unique_recommendation = stocks_in_board[0]
        bc = board_stats[best_board_name]['count']
        ba = board_stats[best_board_name]['avg_pct']
        log(f"\n【唯一推荐板块】{best_board_name} (涨停{bc}家, 平均涨幅{ba:.2f}%)")

    # 9. 分时确认 + 最终交易指令
    if unique_recommendation:
        code = unique_recommendation['代码']
        name = unique_recommendation['名称']
        log(f"\n【唯一推荐个股】{name}({code})")
        log(f"   板块:{unique_recommendation['板块']} 得分:{unique_recommendation['得分']:.1f}")
        log(f"   涨幅:{unique_recommendation['涨跌幅']:.2f}% "
            f"成交额:{unique_recommendation['成交额']/1e8:.1f}亿 "
            f"换手:{unique_recommendation['换手率']:.1f}%")

        minute_ok = False
        if CONFIG["ENABLE_MINUTE"]:
            minute = get_minute_line(code)
            if minute:
                above = minute['above_ratio']
                last5 = minute['last5_drop']
                log(f"   分时(1分钟): 尾盘10根在累计均线上方占比:{above:.1f}% "
                    f"近2根1分钟涨跌:{last5:.2f}%")
                if above >= 80 and last5 > -1:
                    minute_ok = True
                    log("   ✓ 分时形态良好，符合买入条件")
                else:
                    log("   ✗ 分时形态不佳，建议放弃")
            else:
                log("   ⚠️ 分时数据获取失败，请人工复核")
                minute_ok = False
        else:
            minute_ok = True

        if minute_ok:
            limit_cnt = int(unique_recommendation['板块涨停家数'])
            if limit_cnt >= 10:
                take_profit = "5%-7%"
                stop_loss = "-4%"
            elif limit_cnt >= 7:
                take_profit = "4%-6%"
                stop_loss = "-3.5%"
            else:
                take_profit = "3%-5%"
                stop_loss = "-3%"
            log("\n【最终交易指令】")
            log(f"   标的：{name}({code})")
            log("   仓位：全仓（小资金集中模式）")
            log("   买入：14:55 以现价买入，确认分时图白线在黄线上方")
            log(f"   次日：冲高{take_profit}卖出，{stop_loss}硬止损")
            log("   ⚠️ 全仓操作，务必严格执行止损！")
        else:
            log("\n【最终结论】分时形态不符合要求，今日不交易")
            unique_recommendation = None
    else:
        log("\n【最终结论】今日无符合条件的唯一推荐票，建议空仓")

    log("========== 选股完成 ==========\n")

    result = {
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "market_volume": market_vol,
        "mode": mode,
        "has_candidates": unique_recommendation is not None,
        "unique_recommendation": unique_recommendation,
        "all_candidates": all_candidates[:5],
        "candidates": all_candidates,
        "top_recommend": unique_recommendation,
    }
    with open("result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    log("结果已保存至 result.json")


if __name__ == "__main__":
    main()
