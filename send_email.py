import os
import smtplib
import json
from email.mime.text import MIMEText
from email.header import Header
from datetime import datetime

def send_email(subject, content):
    """发送邮件，支持多个收件人（用逗号分隔）"""
    smtp_server = os.environ.get('SMTP_SERVER')
    smtp_port = int(os.environ.get('SMTP_PORT', 587))
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
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, receivers, msg.as_string())
        print(f"邮件发送成功，收件人: {receivers}")
        return True
    except Exception as e:
        print(f"邮件发送失败: {e}")
        return False

if __name__ == "__main__":
    result_file = 'result.json'
    if os.path.exists(result_file):
        with open(result_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        candidates = data.get('candidates', [])
        date = data.get('date', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        if candidates:
            content = f"选股时间: {date}\n\n候选票:\n"
            for c in candidates:
                content += f"{c['名称']}({c['代码']}) 涨幅:{c['涨跌幅']}% 成交额:{c['成交额']/1e8:.1f}亿 板块:{c['板块']}\n"
        else:
            content = f"选股时间: {date}\n\n今日无符合条件的候选票"
        subject = f"尾盘选股结果 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        send_email(subject, content)
    else:
        print("未找到 result.json，无法发送邮件")
