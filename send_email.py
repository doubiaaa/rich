import os
import smtplib
import json
from email.mime.text import MIMEText
from email.header import Header
from datetime import datetime
from env_loader import load_env_file

load_env_file(".env")

def send_email(subject, content):
    """发送邮件，支持多个收件人（用逗号分隔）"""
    smtp_server = os.environ.get('SMTP_SERVER')
    smtp_port_str = os.environ.get('SMTP_PORT', '587')
    # 修复：如果端口值为空，使用默认587
    smtp_port = int(smtp_port_str) if smtp_port_str.strip() else 587
    sender = os.environ.get('EMAIL_SENDER')
    password = os.environ.get('EMAIL_PASSWORD')
    receiver_str = os.environ.get('EMAIL_RECEIVER')
    
    if not all([smtp_server, sender, password, receiver_str]):
        print("邮件配置缺失，请检查环境变量")
        return False
    
    # 支持多个收件人，用逗号分隔（支持空格）
    receivers = [addr.strip() for addr in receiver_str.split(',') if addr.strip()]
    
    msg = MIMEText(content, 'plain', 'utf-8')
    msg['Subject'] = Header(subject, 'utf-8')
    msg['From'] = sender
    msg['To'] = ', '.join(receivers)  # 显示在邮件头部
    
    try:
        # 465：QQ 等邮箱常用 SSL；587：STARTTLS
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
                server.login(sender, password)
                server.sendmail(sender, receivers, msg.as_string())
        else:
            with smtplib.SMTP(smtp_server, smtp_port) as server:
                server.starttls()
                server.login(sender, password)
                server.sendmail(sender, receivers, msg.as_string())
        print(f"邮件发送成功，收件人: {receivers}")
        return True
    except Exception as e:
        print(f"邮件发送失败: {e}")
        return False

def format_candidate(c, index=None):
    """格式化单个候选票信息"""
    prefix = f"{index}. " if index else ""
    return (f"{prefix}{c['名称']}({c['代码']})  "
            f"涨幅:{c['涨跌幅']:.2f}% 成交额:{c['成交额']/1e8:.1f}亿 换手:{c['换手率']:.1f}% 得分:{c.get('得分',0):.1f} 板块:{c['板块']}")

def get_trade_suggestion(mode):
    """根据模式返回交易建议"""
    if mode == 'large':
        return "【大市值模式】建议仓位：3成；次日冲高3%-5%卖出，-2%止损"
    elif mode == 'balanced':
        return "【均衡模式】建议仓位：2-3成；次日冲高3%-5%卖出，-2%止损"
    elif mode == 'small':
        return "【小市值模式】建议仓位：1-2成；次日冲高5%卖出，-3%止损"
    else:
        return "【未知模式】请参考交易纪律"

if __name__ == "__main__":
    result_file = 'result.json'
    if not os.path.exists(result_file):
        print("未找到 result.json，无法发送邮件")
        exit(0)

    with open(result_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    date = data.get('date', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    market_volume = data.get('market_volume')
    mode = data.get('mode')
    has_candidates = data.get('has_candidates', False)
    candidates = data.get('candidates', [])
    top_recommend = data.get('top_recommend')

    # 构建邮件内容
    content_lines = []
    content_lines.append(f"选股时间: {date}")
    if market_volume:
        content_lines.append(f"市场成交额: {market_volume:.0f}亿")
    if mode:
        content_lines.append(f"当前模式: {mode}")
        content_lines.append(get_trade_suggestion(mode))
    content_lines.append("")

    if has_candidates and candidates:
        content_lines.append("【候选票列表】（按得分排序）")
        for idx, c in enumerate(candidates, 1):
            content_lines.append(format_candidate(c, idx))
        content_lines.append("")
        if top_recommend:
            content_lines.append("【优先关注】")
            content_lines.append(format_candidate(top_recommend))
            content_lines.append("")
        content_lines.append("操作提醒：")
        content_lines.append("1. 人工复核分时图（尾盘15分钟白线在黄线上方、无跳水）")
        content_lines.append("2. 符合条件则在14:55以现价买入")
        content_lines.append("3. 严格遵守止盈止损纪律")
    else:
        content_lines.append("今日无符合条件的候选票，建议空仓")

    content = "\n".join(content_lines)
    subject = f"尾盘选股结果 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    send_email(subject, content)
