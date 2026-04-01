# -*- coding: utf-8 -*-
"""
尾盘先手选股策略
版本：v2.5 (1分钟K线分时 + 量比评分 + 大市值无上限)
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
from datetime import datetime, time as datetime_time
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
}

CONFIG["ENABLE_MINUTE"] = str(os.environ.get("ENABLE_MINUTE", str(CONFIG["ENABLE_MINUTE"]))).lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# 全局变量
all_candidates = []


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
            "MAX_MARKET_CAP": float('inf'),
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
    """获取沪深A股实时行情，过滤科创板/北交所，含量比"""
    df = safe_request(ak.stock_zh_a_spot_em)
    if df is None:
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
                    if largest_vol_stock['涨跌幅'] >= 2:
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
    """综合评分（约 0-12，含量比）"""
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
    vol_ratio = float(stock.get('量比', 1.0) or 1.0)
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
    condition = (
        (stocks['流通市值'] >= CONFIG["MIN_MARKET_CAP"] * 1e8) &
        (stocks['流通市值'] <= CONFIG["MAX_MARKET_CAP"] * 1e8) &
        (stocks['涨跌幅'] >= CONFIG["MIN_CHANGE_PCT"]) &
        (stocks['涨跌幅'] <= CONFIG["MAX_CHANGE_PCT"]) &
        (stocks['成交额'] >= CONFIG["MIN_VOLUME"] * 1e8) &
        (stocks['换手率'] >= CONFIG["MIN_TURNOVER"]) &
        (stocks['换手率'] <= CONFIG["MAX_TURNOVER"])
    )
    filtered = stocks[condition].copy()
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
    dynamic_cfg = get_dynamic_config(market_vol)
    if dynamic_cfg is None:
        log("根据成交量判断，今日不适合交易，请空仓。")
        # 仍然输出一个空结果
        result = {
            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "market_volume": market_vol,
            "mode": "no_trade",
            "has_candidates": False,
            "candidates": []
        }
        with open("result.json", "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
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

    # 8. 输出候选列表
    log("\n=== 今日尾盘候选票（按推荐优先级排序） ===")
    for i, c in enumerate(all_candidates):
        log(f"{i+1}. {c['名称']}({c['代码']}) 板块:{c['板块']} "
            f"涨幅:{c['涨跌幅']:.2f}% 成交额:{c['成交额']/1e8:.1f}亿 "
            f"换手:{c['换手率']:.1f}% 得分:{c['得分']:.1f}")

    # 9. 获取前3只的分时形态（可选）
    if CONFIG["ENABLE_MINUTE"]:
        log("\n正在获取分时数据（仅前3名），请稍候...")
        for c in all_candidates[:3]:
            minute = get_minute_line(c['代码'])
            if minute:
                log(f"  {c['名称']} 尾盘10根1分钟在累计均线上方占比:{minute['above_ratio']:.1f}% "
                    f"近2根1分钟涨跌:{minute['last5_drop']:.2f}%")
                if minute['above_ratio'] >= 80 and minute['last5_drop'] > -1:
                    log(f"    ✓ 分时形态较好，可重点关注")
                else:
                    log(f"    ✗ 分时形态一般，建议放弃")
            else:
                log(f"  {c['名称']} 分时数据获取失败，请人工查看")

    # 10. 最终建议（唯一推荐 + 可选分时）
    log("\n【操作建议】")
    if all_candidates:
        best = all_candidates[0]
        log(f"⭐ 唯一推荐：{best['名称']}({best['代码']}) 板块:{best['板块']} 得分:{best['得分']:.1f}")
        log(f"   今日涨幅:{best['涨跌幅']:.2f}% 成交额:{best['成交额']/1e8:.1f}亿 换手:{best['换手率']:.1f}%")
        if CONFIG["ENABLE_MINUTE"]:
            minute = get_minute_line(best['代码'])
            if minute:
                log(f"   分时(1分钟): 尾盘10根在累计均线上方占比:{minute['above_ratio']:.1f}% "
                    f"近2根1分钟涨跌:{minute['last5_drop']:.2f}%")
                if minute['above_ratio'] >= 80 and minute['last5_drop'] > -1:
                    log("   ✓ 分时形态较好，可重点关注")
                else:
                    log("   ✗ 分时形态一般，建议谨慎或放弃")
        log("\n买入条件：")
        log("   - 尾盘14:55分时图白线在黄线上方且无跳水，以现价买入")
        if mode == "large":
            log("   - 仓位：3成（大市值模式）")
            log("   - 次日计划：冲高3%-5%卖出，-2%止损")
        elif mode == "balanced":
            log("   - 仓位：2-3成（均衡模式）")
            log("   - 次日计划：冲高3%-5%卖出，-2%止损")
        else:
            log("   - 仓位：1-2成（小市值模式）")
            log("   - 次日计划：冲高5%卖出，-3%止损")
    else:
        log("今日无符合条件的候选票，建议空仓。")
    log("========== 选股完成 ==========\n")

    # 保存结果
    result = {
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "market_volume": market_vol,
        "mode": mode,
        "has_candidates": True,
        "candidates": all_candidates,
        "top_recommend": all_candidates[0] if all_candidates else None
    }
    with open("result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    log("结果已保存至 result.json")


if __name__ == "__main__":
    main()
