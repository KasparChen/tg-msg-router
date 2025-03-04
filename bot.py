import os
import json
from datetime import datetime, timedelta
import pytz
import telebot
import boto3
import threading
import time
from dotenv import load_dotenv
import logging

# 设置日志格式，便于调试
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# 设置中国时区（UTC+8）
TZ = pytz.timezone('Asia/Shanghai')

# 加载 .env 文件中的环境变量
load_dotenv()

# 从环境变量读取配置
BOT_TOKEN = os.getenv('BOT_TOKEN')
S3_BUCKET = os.getenv('S3_BUCKET')
SUPER_ADMINS_RAW = os.getenv('SUPER_ADMINS', '[]')

# 解析 SUPER_ADMINS，处理可能的JSON格式错误
try:
    SUPER_ADMINS = json.loads(SUPER_ADMINS_RAW)
except json.JSONDecodeError as e:
    logging.error(f"错误：SUPER_ADMINS 解析失败，格式错误: {e}")
    SUPER_ADMINS = []

# 检查必要环境变量是否配置
if not BOT_TOKEN:
    logging.error("错误：BOT_TOKEN 未设置，请检查 .env 文件或环境变量！")
    exit(1)
if not S3_BUCKET:
    logging.error("错误：S3_BUCKET 未设置，请检查 .env 文件或环境变量！")
    exit(1)

# 初始化 S3 客户端，用于存储配置和日志
s3 = boto3.client('s3')

# 定义配置文件和日志的前缀
CONFIG_KEY = 'config.json'
LOG_PREFIX = 'logs/'

# 初始化 Telegram Bot
logging.info("Bot正在连接...")
bot = telebot.TeleBot(BOT_TOKEN)
logging.info("Bot已连接，正在运行...")

# 从 S3 加载配置
def load_config():
    """从 S3 读取配置文件，若不存在返回默认配置"""
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=CONFIG_KEY)
        return json.loads(obj['Body'].read().decode('utf-8'))
    except s3.exceptions.NoSuchKey:
        return {
            'monitor_channel': None,
            'keyword_initial': [],
            'keyword_contain': [],
            'sending_channels': [],
            'admins': SUPER_ADMINS
        }

# 保存配置到 S3
def save_config(config):
    """将配置保存到 S3"""
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=CONFIG_KEY,
        Body=json.dumps(config, ensure_ascii=False).encode('utf-8')
    )

# 记录日志到 S3
def log_event(event):
    """记录事件到当天的日志文件，使用中国时区，不记录 getUpdates"""
    now = datetime.now(TZ)
    log_key = f"{LOG_PREFIX}{now.strftime('%Y-%m-%d')}.log"
    log_entry = f"{now.strftime('%Y-%m-%d %H:%M:%S')} - {event}\n"
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=log_key)
        content = obj['Body'].read().decode('utf-8') + log_entry
    except s3.exceptions.NoSuchKey:
        content = log_entry
    s3.put_object(Bucket=S3_BUCKET, Key=log_key, Body=content.encode('utf-8'))

# 清理旧日志
def clean_old_logs():
    """每天检查日志条数，若超过 500 条，只保留最近 500 条"""
    now = datetime.now(TZ)
    log_key = f"{LOG_PREFIX}{now.strftime('%Y-%m-%d')}.log"
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=log_key)
        content = obj['Body'].read().decode('utf-8')
        log_lines = content.splitlines()
        if len(log_lines) > 500:
            new_content = '\n'.join(log_lines[-500:]) + '\n'
            s3.put_object(Bucket=S3_BUCKET, Key=log_key, Body=new_content.encode('utf-8'))
            log_event(f"清理日志: {log_key} 从 {len(log_lines)} 条缩减至 500 条")
    except s3.exceptions.NoSuchKey:
        pass

# 日志清理线程
def log_cleanup_thread():
    """每分钟检查一次，如果是 0 点则清理日志条数"""
    last_cleanup_date = None
    while True:
        now = datetime.now(TZ)
        current_date = now.strftime('%Y-%m-%d')
        if last_cleanup_date != current_date and now.hour == 0 and now.minute == 0:
            clean_old_logs()
            last_cleanup_date = current_date
        time.sleep(60)

# 检查用户是否为管理员
def is_admin(username):
    """检查用户是否为管理员或超级管理员"""
    config = load_config()
    if not username:
        return False
    username = f"@{username}" if not username.startswith('@') else username
    username = username.lower()
    admins = [x.lower() for x in config['admins']]
    super_admins = [x.lower() for x in SUPER_ADMINS]
    return username in admins or username in super_admins

# 转义 Markdown 特殊字符
def escape_markdown(text):
    """转义 Markdown 特殊字符，确保文本按原样展示"""
    chars_to_escape = ['_', '*', '[', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in chars_to_escape:
        text = text.replace(char, f'\\{char}')
    return text

# /help 命令 - 使用 Markdown
@bot.message_handler(commands=['help'])
def help_command(message):
    help_text = (
        "*MSG Router* 是一个消息处理 Bot，能够监听指定 Channel 中包含特定关键词的消息，并将其完整复制发送至指定的 Channel/Group。\n"
        "_*关键词匹配无视大小写*_。\n\n"
        "*使用指南*:  \n"
        "`/help` - 显示这个指南  \n"
        "`/status` - 查看当前 Bot 配置  \n"
        "`/get_group_id` - 获取当前群组的 ID（无需权限）  \n"
        "`/set_monitor_channel` - 设置要监控的频道 ID（目前只支持 1 个）  \n"
        "`/set_keyword_initial` - 设置抓取的句首关键词（用逗号分隔多个，最多 5 个）  \n"
        "`/set_keyword_contain` - 设置抓取的句中关键词（用逗号分隔多个，最多 5 个）  \n"
        "`/set_sending_channel` - 设置发送频道的 ID（最多 3 个）  \n"
        "`/add_admin` - 添加管理员  \n"
        "`/rm_admin` - 移除管理员"
    )
    try:
        bot.send_message(message.chat.id, help_text, parse_mode='Markdown')
    except Exception as e:
        bot.send_message(message.chat.id, "发送帮助信息时出现错误，请检查Bot权限或稍后再试。")

# /status 命令 - 使用 Markdown 并保护关键词
@bot.message_handler(commands=['status'])
def status_command(message):
    if not is_admin(message.from_user.username):
        bot.send_message(message.chat.id, f"抱歉，你没有权限执行这个操作！你的用户名: @{message.from_user.username}")
        return
    config = load_config()

    monitor_channel_text = "未设置" if not config['monitor_channel'] else f"{escape_markdown(bot.get_chat(config['monitor_channel']).title)} ({config['monitor_channel']})"
    keyword_initial_text = ", ".join(escape_markdown(kw) for kw in config['keyword_initial']) if config['keyword_initial'] else "未设置"
    keyword_contain_text = ", ".join(escape_markdown(kw) for kw in config['keyword_contain']) if config['keyword_contain'] else "未设置"
    sending_channels_text = "\n".join(f"[{i}] {escape_markdown(bot.get_chat(cid).title)} ({cid})" for i, cid in enumerate(config['sending_channels'], 1)) if config['sending_channels'] else "未设置"

    status_text = (
        f"*当前监控视野*:\n"
        f"{monitor_channel_text} \n\n"
        f"*关键词抓取配置*: \n"
        f"> *句首*:"
        f"{keyword_initial_text}\n"
        f"> *句中*:"
        f"{keyword_contain_text}_\n\n"
        f"*发送频道*:\n"
        f"{sending_channels_text}"
    )
    try:
        bot.send_message(message.chat.id, status_text, parse_mode='Markdown')
    except Exception as e:
        bot.send_message(message.chat.id, "发送状态信息时出现错误，请检查Bot权限或稍后再试。")
# 命令：/get_group_id - 获取当前群组 ID
@bot.message_handler(commands=['get_group_id'])
def get_group_id_command(message):
    """返回当前群组或频道 ID"""
    group_id = message.chat.id
    username = message.from_user.username
    bot.reply_to(message, f"当前群组的ID是: {group_id}")
    log_event(f"用户 @{username} 获取群组ID: {group_id}")

# 命令：/set_monitor_channel - 设置监控频道
@bot.message_handler(commands=['set_monitor_channel'])
def set_monitor_channel_command(message):
    """提示用户提供监控频道的 ID"""
    username = message.from_user.username
    if not is_admin(username):
        bot.reply_to(message, f"抱歉，你没有权限执行这个操作！你的用户名: @{username}")
        return
    bot.reply_to(message, "请提供要监控的频道 ID（例如 -100123456789）")
    bot.register_next_step_handler(message, process_set_monitor_channel)

def process_set_monitor_channel(message):
    """处理用户输入的监控频道 ID"""
    username = message.from_user.username
    channel_id = message.text.strip()
    try:
        chat = bot.get_chat(channel_id)
        config = load_config()
        old_channel = config['monitor_channel']
        config['monitor_channel'] = channel_id
        save_config(config)
        bot.reply_to(message, f"{chat.title} ({channel_id}) 已设置为监控频道")
        logging.info(f"配置更新 - 用户 @{username} 将监控频道从 {old_channel} 变更为 {channel_id}")
        log_event(f"用户 @{username} 设置监控频道: 从 {old_channel} 变更为 {channel_id}")
    except Exception as e:
        logging.error("Failed to set monitor channel: %s", str(e))
        bot.reply_to(message, "无效的频道 ID，请确保输入正确并确保 Bot 有权限访问该频道！")

# 命令：/set_keyword_initial - 设置句首关键词
@bot.message_handler(commands=['set_keyword_initial'])
def set_keyword_initial_command(message):
    """提示用户提供句首关键词"""
    username = message.from_user.username
    if not is_admin(username):
        bot.send_message(message.chat.id, f"抱歉，你没有权限执行这个操作！你的用户名: @{username}")
        return
    bot.send_message(message.chat.id, "请提供开头关键词（用逗号分隔多个，例如 alpha, breaking, just in）\n输入`.-.` 恢复默认")
    bot.register_next_step_handler(message, process_set_keyword_initial)

def process_set_keyword_initial(message):
    """处理用户输入的句首关键词，覆盖旧配置或清空列表"""
    username = message.from_user.username
    input_text = message.text.strip()
    if input_text == '.-.':
        config = load_config()
        config['keyword_initial'] = []
        save_config(config)
        bot.send_message(message.chat.id, "开头关键词已清空，恢复默认设置")
        logging.info(f"配置更新 - 用户 @{username} 清空开头关键词")
        log_event(f"用户 @{username} 清空开头关键词")
        return

    keywords = [kw.strip() for kw in input_text.split(',')]
    if len(keywords) > 5:
        bot.send_message(message.chat.id, "开头关键词数量不能超过 5 个！")
        return
    config = load_config()
    config['keyword_initial'] = keywords
    save_config(config)
    bot.send_message(message.chat.id, f"开头关键词已设置为: {', '.join(keywords)}")
    logging.info(f"配置更新 - 用户 @{username} 设置开头关键词: {keywords}")
    log_event(f"用户 @{username} 设置开头关键词: {keywords}")

# 命令：/set_keyword_contain - 设置句中关键词
@bot.message_handler(commands=['set_keyword_contain'])
def set_keyword_contain_command(message):
    """提示用户提供句中关键词"""
    username = message.from_user.username
    if not is_admin(username):
        bot.send_message(message.chat.id, f"抱歉，你没有权限执行这个操作！你的用户名: @{username}")
        return
    bot.send_message(message.chat.id, "请提供包含关键词（用逗号分隔多个，例如 CA, news, update）\n输入`.-.` 恢复默认")
    bot.register_next_step_handler(message, process_set_keyword_contain)

def process_set_keyword_contain(message):
    """处理用户输入的句中关键词，覆盖旧配置或清空列表"""
    username = message.from_user.username
    input_text = message.text.strip()
    if input_text == '.-.':
        config = load_config()
        config['keyword_contain'] = []
        save_config(config)
        bot.send_message(message.chat.id, "包含关键词已清空，恢复默认设置")
        logging.info(f"配置更新 - 用户 @{username} 清空包含关键词")
        log_event(f"用户 @{username} 清空包含关键词")
        return

    keywords = [kw.strip() for kw in input_text.split(',')]
    if len(keywords) > 5:
        bot.send_message(message.chat.id, "包含关键词数量不能超过 5 个！")
        return
    config = load_config()
    config['keyword_contain'] = keywords
    save_config(config)
    bot.send_message(message.chat.id, f"包含关键词已设置为: {', '.join(keywords)}")
    logging.info(f"配置更新 - 用户 @{username} 设置包含关键词: {keywords}")
    log_event(f"用户 @{username} 设置包含关键词: {keywords}")

# 命令：/set_sending_channel - 设置发送目标
@bot.message_handler(commands=['set_sending_channel'])
def set_sending_channel_command(message):
    """提示用户提供发送目标频道 ID，支持多个"""
    username = message.from_user.username
    if not is_admin(username):
        bot.reply_to(message, f"抱歉，你没有权限执行这个操作！你的用户名: @{username}")
        return
    bot.reply_to(message, "请提供发送目标频道 ID（用逗号分隔多个，例如 -100987654321, -100123456789，最多 3 个）")
    bot.register_next_step_handler(message, process_set_sending_channel)

def process_set_sending_channel(message):
    """处理用户输入的发送目标频道 ID，覆盖旧配置"""
    username = message.from_user.username
    channel_ids = [cid.strip() for cid in message.text.split(',')]
    if len(channel_ids) > 3:
        bot.reply_to(message, "发送目标数量不能超过 3 个！")
        return

    valid_channels = []
    for channel_id in channel_ids:
        try:
            chat = bot.get_chat(channel_id)
            valid_channels.append((channel_id, chat.title))
        except Exception as e:
            logging.error("Invalid channel ID %s: %s", channel_id, str(e))
            bot.reply_to(message, f"无效的频道 ID: {channel_id}，请确保输入正确并确保 Bot 有权限访问该频道！")
            return

    config = load_config()
    config['sending_channels'] = [cid for cid, _ in valid_channels]
    save_config(config)

    channel_list = "\n".join([f"{chat_title} ({chat_id})" for chat_id, chat_title in valid_channels])
    bot.reply_to(message, f"发送目标已设置为:\n{channel_list}")
    logging.info(f"配置更新 - 用户 @{username} 设置发送目标: {config['sending_channels']}")
    log_event(f"用户 @{username} 设置发送目标: {config['sending_channels']}")

# 命令：/add_admin - 添加管理员
@bot.message_handler(commands=['add_admin'])
def add_admin_command(message):
    """提示用户提供管理员 handle name"""
    username = message.from_user.username
    if not is_admin(username):
        bot.reply_to(message, f"抱歉，你没有权限执行这个操作！你的用户名: @{username}")
        return
    bot.reply_to(message, "请提供要添加的管理员 handle name（例如 @username）")
    bot.register_next_step_handler(message, process_add_admin)

def process_add_admin(message):
    """处理用户输入的管理员 handle name"""
    username = message.from_user.username
    handle_name = message.text.strip()
    config = load_config()
    if handle_name not in config['admins']:
        config['admins'].append(handle_name)
        save_config(config)
        bot.reply_to(message, f"管理员 {handle_name} 已添加")
        logging.info(f"配置更新 - 用户 @{username} 添加管理员: {handle_name}")
        log_event(f"用户 @{username} 添加管理员: {handle_name}, 当前管理员列表: {config['admins']}")
    else:
        bot.reply_to(message, f"{handle_name} 已经是管理员！")

# 命令：/rm_admin - 移除管理员
@bot.message_handler(commands=['rm_admin'])
def rm_admin_command(message):
    """显示管理员列表并等待用户选择移除"""
    username = message.from_user.username
    if not is_admin(username):
        bot.reply_to(message, f"抱歉，你没有权限执行这个操作！你的用户名: @{username}")
        return
    config = load_config()
    admins = config['admins']
    if not admins:
        bot.reply_to(message, "当前没有管理员可以移除！")
        return
    admin_list = "\n".join([f"{i+1}. {admin}" for i, admin in enumerate(admins)])
    bot.reply_to(message, f"当前管理员列表：\n{admin_list}\n请回复要移除的管理员编号")
    log_event(f"用户 @{username} 请求移除管理员，当前管理员列表: {admins}")
    bot.register_next_step_handler(message, process_rm_admin)

def process_rm_admin(message):
    """处理移除管理员的逻辑"""
    username = message.from_user.username
    try:
        index = int(message.text.strip()) - 1
        config = load_config()
        admins = config['admins']
        if 0 <= index < len(admins):
            removed_admin = admins.pop(index)
            save_config(config)
            bot.reply_to(message, f"管理员 {removed_admin} 已移除")
            logging.info(f"配置更新 - 用户 @{username} 移除管理员: {removed_admin}")
            log_event(f"用户 @{username} 移除管理员: {removed_admin}, 当前管理员列表: {admins}")
        else:
            bot.reply_to(message, "无效的编号，请检查！")
    except ValueError:
        bot.reply_to(message, "请提供有效的编号，例如: 1")

# 监听频道消息并复制发送
@bot.channel_post_handler(func=lambda message: True)
def handle_channel_post(message):
    """监听频道消息，复制带有关键词的内容并发送，不转发"""
    config = load_config()
    if str(message.chat.id) != config['monitor_channel']:
        return
    text = message.text or ""
    keyword_initial = config['keyword_initial']
    keyword_contain = config['keyword_contain']
    sending_channels = config['sending_channels']

    if not keyword_initial and not keyword_contain:
        for channel in sending_channels:
            try:
                bot.send_message(channel, text)
            except Exception as e:
                logging.error("Failed to send message to %s: %s", channel, str(e))
        log_event(f"复制消息 {message.message_id} 从 {message.chat.id} 到 {sending_channels}（无关键词，默认复制）")
        return

    text_lower = text.lower()
    should_send = False
    matched_keyword = None
    for keyword in keyword_initial:
        if text_lower.startswith(keyword.lower()):
            should_send = True
            matched_keyword = keyword
            break
    if not should_send:
        for keyword in keyword_contain:
            if keyword.lower() in text_lower:
                should_send = True
                matched_keyword = keyword
                break

    if should_send:
        for channel in sending_channels:
            try:
                bot.send_message(channel, text)
            except Exception as e:
                logging.error("Failed to send message to %s: %s", channel, str(e))
        log_event(f"复制消息 {message.message_id} 从 {message.chat.id} 到 {sending_channels}（匹配关键词: {matched_keyword}）")

# 主程序入口
if __name__ == '__main__':
    # 启动日志清理线程
    cleanup_thread = threading.Thread(target=log_cleanup_thread, daemon=True)
    cleanup_thread.start()
    clean_old_logs()
    logging.info("Bot初始化完成，开始监听消息...")
    bot.polling(none_stop=True)