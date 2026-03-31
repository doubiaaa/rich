# -*- coding: utf-8 -*-
import akshare as ak
import pandas as pd
import numpy as np
from datetime import datetime, time as datetime_time
import time as time_module
import os
import json
import warnings

warnings.filterwarnings("ignore", category=UserWarning, module="pkg_resources")

# ========== 参数配置 ==========
CONFIG = {
    "MIN_BOARD_STOCKS": 5,          # 板块爆发最小涨停家数
    "MIN_BOARD_PCT": 2.5,           # 板块爆发最小涨幅(%)
    "MAX_MARKET_CAP": 80,           # 最大流通市值(亿)
    "MIN_MARKET_CAP": 20,           # 最小流通市值(亿)
    "MAX_CHANGE_PCT": 8,            # 当天最大涨幅(%)
    "MIN_CHANGE_PCT": 0,            # 当天最小涨幅(%)
    "MIN_VOLUME": 3,                # 最小成交额(亿)
    "MIN_TURNOVER": 5,              # 最小换手率(%)
    "MAX_TURNOVER": 25,             # 最大换手率(%)
    "EXCLUDE_BOARDS": ['688', '8'], # 排除科创板、北交所
    "TOP_N": 3,
    "LOG_DIR": "trade_logs",
    "MAX_CONCEPTS": 50,
}

all_candidates = []  # 全局变量，用于保存结果

def is_trading_time():
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    current_time = now.time()
    morning_start = datetime_time(9, 30)
    morning_end = datetime_time(11, 30)
    afternoon_start = datetime_time(13, 0)
    afternoon_end = datetime_time(15, 0)
    if (morning_start <= current_time <= morning_end) or (afternoon_start <= current_time <= afternoon_end):
        return True
    return False

def log(msg, print_console=True):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_msg = f"[{timestamp}] {msg}"
    if print_console:
        print(log_msg)
    log_file = os.path.join(CONFIG["LOG_DIR"], f"{datetime.now().strftime('%Y%m%d')}.log")
    os.makedirs(CONFIG["LOG_DIR"], exist_ok=True)
    with open(log_file, 'a', encoding='utf-8') as f:
        f.write(log_msg + '\n')

def safe_request(func, *args, **kwargs):
    for i in range(3):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            log(f"请求失败({i+1}/3): {e}")
            time_module.sleep(2)
    return None

def get_all_stocks():
    if not is_trading_time():
        log("当前非交易时段，无法获取实时行情，请于交易时段（9:30-15:00）运行")
        return None
    df = safe_request(ak.stock_zh_a_spot_em)
    if df is None:
        return None
    df = df[~df['代码'].str.startswith(tuple(CONFIG["EXCLUDE_BOARDS"]))]
    df = df[['代码', '名称', '最新价', '涨跌幅', '成交额', '换手率', '流通市值']]
    for col in ['涨跌幅', '成交额', '换手率', '流通市值']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
    df = df.dropna()
    return df

def get_board_heat(stock_df):
    concept_list = safe_request(ak.stock_board_concept_name_em)
    if concept_list is None:
        return []
    concepts = concept_list.head(CONFIG["MAX_CONCEPTS"])['板块名称'].tolist()
    hot_boards = []
    for concept in concepts:
        try:
            cons = safe_request(ak.stock_board_concept_cons_em, symbol=concept)
            if cons is None:
                continue
            merged = pd.merge(cons, stock_df, left_on='代码', right_on='代码')
            if len(merged) == 0:
                continue
            limit_stocks = merged[merged['涨跌幅'] >= 9.8]
            cnt = len(limit_stocks)
            if cnt >= CONFIG["MIN_BOARD_STOCKS"]:
                avg_pct = merged['涨跌幅'].mean()
                if avg_pct >= CONFIG["MIN_BOARD_PCT"]:
                    hot_boards.append({
                        'name': concept,
                        'limit_count': cnt,
                        'avg_pct': avg_pct,
                        'stocks': merged
                    })
        except Exception as e:
            log(f"处理板块 {concept} 失败: {e}")
            continue
    hot_boards.sort(key=lambda x: x['avg_pct'], reverse=True)
    return hot_boards

def filter_stocks_in_board(board, stock_df):
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
    filtered = filtered.sort_values('成交额', ascending=False).head(CONFIG["TOP_N"])
    filtered['板块'] = board['name']
    filtered['板块平均涨幅'] = board['avg_pct']
    filtered['板块涨停家数'] = board['limit_count']
    return filtered.to_dict('records')

def get_minute_line(code):
    try:
        trade_date = datetime.now().strftime('%Y%m%d')
        df = safe_request(ak.stock_zh_a_tick_tx, code=code, trade_date=trade_date)
        if df is None or len(df) == 0:
            return None
        df['amount'] = df['成交额']
        df['volume'] = df['成交量']
        df['cum_amount'] = df['amount'].cumsum()
        df['cum_volume'] = df['volume'].cumsum()
        df['avg_price'] = df['cum_amount'] / df['cum_volume']
        last_10 = df.tail(10)
        above_avg = (last_10['价格'] > last_10['avg_price']).mean() * 100
        last_5 = df.tail(5)
        drop = (last_5['价格'].iloc[-1] - last_5['价格'].iloc[0]) / last_5['价格'].iloc[0] * 100
        return {'above_ratio': above_avg, 'last5_drop': drop}
    except Exception as e:
        log(f"获取分时数据失败 {code}: {e}")
        return None

def main():
    global all_candidates
    log("========== 开始尾盘选股 ==========")
    stock_df = get_all_stocks()
    if stock_df is None:
        log("获取股票数据失败，退出")
        return
    hot_boards = get_board_heat(stock_df)
    if not hot_boards:
        log("未发现符合条件的爆发板块，今日无交易")
        return
    log(f"发现{len(hot_boards)}个热点板块: {[b['name'] for b in hot_boards]}")
    all_candidates = []
    for board in hot_boards:
        candidates = filter_stocks_in_board(board, stock_df)
        if candidates:
            all_candidates.extend(candidates)
            log(f"板块【{board['name']}】发现{len(candidates)}只候选")
    if not all_candidates:
        log("所有板块内均无符合条件的个股，今日无交易")
        return
    all_candidates.sort(key=lambda x: x['成交额'], reverse=True)
    log("\n=== 今日尾盘候选票（需人工复核分时） ===")
    for c in all_candidates:
        log(f"{c['名称']}({c['代码']}) 板块:{c['板块']} "
            f"涨幅:{c['涨跌幅']:.2f}% 成交额:{c['成交额']/1e8:.1f}亿 "
            f"换手:{c['换手率']:.1f}% 市值:{c['流通市值']/1e8:.1f}亿")
    # 可选：获取前3只的分时数据
    log("\n正在获取分时数据（仅前3名），请稍候...")
    for c in all_candidates[:3]:
        minute = get_minute_line(c['代码'])
        if minute:
            log(f"  {c['名称']} 尾盘10分钟在均线上方占比:{minute['above_ratio']:.1f}% 最后5分钟跌幅:{minute['last5_drop']:.2f}%")
            if minute['above_ratio'] >= 80 and minute['last5_drop'] > -1:
                log(f"    ✓ 分时形态较好，可重点关注")
            else:
                log(f"    ✗ 分时形态一般，建议放弃")
        else:
            log(f"  {c['名称']} 分时数据获取失败，请人工查看")
    log("\n请人工复核分时图（白线是否在黄线上方，尾盘无跳水），符合条件的14:55买入。")
    log("========== 选股完成 ==========\n")

    # 输出结果到JSON文件（供Server酱推送）
    result = {
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "has_candidates": len(all_candidates) > 0,
        "candidates": all_candidates
    }
    with open("result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    log("结果已保存至 result.json")

if __name__ == "__main__":
    main()