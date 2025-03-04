import os
import json
from datetime import datetime, timedelta
import pytz
import telebot
import boto3
import threading
import time

# 设置中国时区（UTC+8）
TZ = pytz.timezone('Asia/Shanghai')

# 从环境变量读取配置
BOT_TOKEN = os.getenv('BOT_TOKEN')  # Telegram Bot的Token
S3_BUCKET = os.getenv('S3_BUCKET')  # S3 Bucket名称
SUPER_ADMINS = json.loads(os.getenv('SUPER_ADMINS', '[]'))  # 超级管理员列表，例如["@admin1", "@admin2"]

# 初始化S3客户端
s3 = boto3.client('s3')

# 配置和日志文件在S3中的路径
CONFIG_KEY = 'config.json'  # 配置文件名
LOG_PREFIX = 'logs/'  # 日志文件前缀

# 初始化Telegram Bot
bot = telebot.TeleBot(BOT_TOKEN)

# 从S3加载配置
def load_config():
    """从S3读取配置文件，如果不存在就返回默认配置"""
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=CONFIG_KEY)
        return json.loads(obj['Body'].read().decode('utf-8'))
    except s3.exceptions.NoSuchKey:
        # 默认配置：没有监控频道，没有关键词，空的转发目标，管理员只有超级管理员
        return {
            'monitor_channel': None,
            'keyword_initial': [],
            'keyword_contain': [],
            'sending_channels': [],
            'admins': SUPER_ADMINS
        }

# 保存配置到S3
def save_config(config):
    """将配置保存到S3"""
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=CONFIG_KEY,
        Body=json.dumps(config, ensure_ascii=False).encode('utf-8')
    )

# 记录日志到S3
def log_event(event):
    """记录事件到当天的日志文件中，使用中国时区"""
    now = datetime.now(TZ)
    log_key = f"{LOG_PREFIX}{now.strftime('%Y-%m-%d')}.log"
    log_entry = f"{now.strftime('%Y-%m-%d %H:%M:%S')} - {event}\n"
    try:
        # 尝试追加到已有日志文件
        obj = s3.get_object(Bucket=S3_BUCKET, Key=log_key)
        content = obj['Body'].read().decode('utf-8') + log_entry
    except s3.exceptions.NoSuchKey:
        # 如果文件不存在，创建新文件
        content = log_entry
    s3.put_object(Bucket=S3_BUCKET, Key=log_key, Body=content.encode('utf-8'))

# 清理超过三天的日志
def clean_old_logs():
    """清理三天前的日志文件"""
    now = datetime.now(TZ)
    cutoff_date = now - timedelta(days=3)  # 三天前的日期
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=LOG_PREFIX):
        for obj in page.get('Contents', []):
            log_date_str = obj['Key'].split('/')[-1].replace('.log', '')
            try:
                log_date = datetime.strptime(log_date_str, '%Y-%m-%d').replace(tzinfo=TZ)
                if log_date < cutoff_date:
                    s3.delete_object(Bucket=S3_BUCKET, Key=obj['Key'])
                    log_event(f"Deleted old log file: {obj['Key']}")
            except ValueError:
                continue

# 日志清理的后台线程
def log_cleanup_thread():
    """每分钟检查一次时间，如果是0点则清理旧日志"""
    last_cleanup_date = None
    while True:
        now = datetime.now(TZ)
        current_date = now.strftime('%Y-%m-%d')
        # 如果是新的一天且刚过0点，执行清理
        if last_cleanup_date != current_date and now.hour == 0 and now.minute == 0:
            clean_old_logs()
            last_cleanup_date = current_date
        time.sleep(60)  # 每分钟检查一次

# 检查用户是否为管理员
def is_admin(username):
    """检查用户是否为管理员或超级管理员"""
    config = load_config()
    return username in config['admins'] or username in SUPER_ADMINS

# 命令：/help - 显示使用指南
@bot.message_handler(commands=['help'])
def help_command(message):
    """发送Bot的使用指南"""
    help_text = """
    使用指南：
    /help - 显示这个指南
    /status - 查看当前Bot配置
    /get_group_id - 获取当前群组的ID（无需权限）
    /set_monitor_channel <channel_id> - 设置要监控的频道（覆盖之前的设置）
    /set_keyword_initial <keyword> - 设置开头关键词（最多5个）
    /set_keyword_contain <keyword> - 设置包含关键词（最多5个）
    /set_sending_channel <channel_id> - 设置转发目标（最多3个）
    /add_admin <handle_name> - 添加管理员
    /rm_admin - 移除管理员（先显示列表）
    注意：除了/get_group_id外，所有命令需要管理员权限！
    """
    bot.reply_to(message, help_text)

# 命令：/status - 显示当前配置
@bot.message_handler(commands=['status'])
def status_command(message):
    """显示Bot的当前配置"""
    if not is_admin(message.from_user.username):
        bot.reply_to(message, "抱歉，你没有权限执行这个操作！")
        return
    config = load_config()
    
    # 获取监控频道信息
    monitor_channel_text = "未设置"
    if config['monitor_channel']:
        try:
            chat = bot.get_chat(config['monitor_channel'])
            monitor_channel_text = f"{chat.title} ({config['monitor_channel']})"
        except Exception as e:
            monitor_channel_text = f"未知频道 ({config['monitor_channel']})"

    # 关键词配置
    keyword_initial_text = ", ".join(config['keyword_initial']) if config['keyword_initial'] else "未设置"
    keyword_contain_text = ", ".join(config['keyword_contain']) if config['keyword_contain'] else "未设置"

    # 转发频道列表
    sending_channels_text = []
    for i, channel_id in enumerate(config['sending_channels'], 1):
        try:
            chat = bot.get_chat(channel_id)
            sending_channels_text.append(f"[{i}] {chat.title} ({channel_id})")
        except Exception as e:
            sending_channels_text.append(f"[{i}] 未知频道 ({channel_id})")
    sending_channels_text = "\n".join(sending_channels_text) if sending_channels_text else "未设置"

    # 格式化输出
    status_text = f"""
    当前监控视野:
    {monitor_channel_text}

    关键词抓取配置:
    > 句首: {keyword_initial_text}
    > 句中: {keyword_contain_text}

    转发频道:
    {sending_channels_text}
    """
    bot.reply_to(message, status_text)
    log_event(f"用户 {message.from_user.username} 查看了Bot状态")

# 命令：/get_group_id - 获取当前群组ID
@bot.message_handler(commands=['get_group_id'])
def get_group_id_command(message):
    """返回当前聊天窗口的群组或频道ID"""
    group_id = message.chat.id
    bot.reply_to(message, f"当前群组的ID是: {group_id}")

# 命令：/set_monitor_channel - 设置监控频道
@bot.message_handler(commands=['set_monitor_channel'])
def set_monitor_channel_command(message):
    """设置要监控的频道，只能设置一个"""
    if not is_admin(message.from_user.username):
        bot.reply_to(message, "抱歉，你没有权限执行这个操作！")
        return
    try:
        channel_id = message.text.split()[1]  # 获取命令后的频道ID
        config = load_config()
        config['monitor_channel'] = channel_id  # 覆盖之前的设置
        save_config(config)
        bot.reply_to(message, f"监控频道已设置为: {channel_id}")
        log_event(f"用户 {message.from_user.username} 设置监控频道为 {channel_id}")
    except IndexError:
        bot.reply_to(message, "请提供频道ID，例如: /set_monitor_channel -100123456789")

# 命令：/set_keyword_initial - 设置开头关键词
@bot.message_handler(commands=['set_keyword_initial'])
def set_keyword_initial_command(message):
    """设置消息开头包含的关键词，最多5个"""
    if not is_admin(message.from_user.username):
        bot.reply_to(message, "抱歉，你没有权限执行这个操作！")
        return
    try:
        keyword = message.text.split()[1]  # 获取命令后的关键词
        config = load_config()
        if len(config['keyword_initial']) < 5:
            config['keyword_initial'].append(keyword)
            save_config(config)
            bot.reply_to(message, f"已添加开头关键词: {keyword}")
            log_event(f"用户 {message.from_user.username} 添加开头关键词 {keyword}")
        else:
            bot.reply_to(message, "开头关键词数量已达上限（5个）！")
    except IndexError:
        bot.reply_to(message, "请提供关键词，例如: /set_keyword_initial [Alpha]")

# 命令：/set_keyword_contain - 设置包含关键词
@bot.message_handler(commands=['set_keyword_contain'])
def set_keyword_contain_command(message):
    """设置消息中包含的关键词，最多5个"""
    if not is_admin(message.from_user.username):
        bot.reply_to(message, "抱歉，你没有权限执行这个操作！")
        return
    try:
        keyword = message.text.split()[1]  # 获取命令后的关键词
        config = load_config()
        if len(config['keyword_contain']) < 5:
            config['keyword_contain'].append(keyword)
            save_config(config)
            bot.reply_to(message, f"已添加包含关键词: {keyword}")
            log_event(f"用户 {message.from_user.username} 添加包含关键词 {keyword}")
        else:
            bot.reply_to(message, "包含关键词数量已达上限（5个）！")
    except IndexError:
        bot.reply_to(message, "请提供关键词，例如: /set_keyword_contain CA")

# 命令：/set_sending_channel - 设置转发目标
@bot.message_handler(commands=['set_sending_channel'])
def set_sending_channel_command(message):
    """设置消息转发的目标，最多3个"""
    if not is_admin(message.from_user.username):
        bot.reply_to(message, "抱歉，你没有权限执行这个操作！")
        return
    try:
        channel_id = message.text.split()[1]  # 获取命令后的频道ID
        config = load_config()
        if len(config['sending_channels']) < 3:
            config['sending_channels'].append(channel_id)
            save_config(config)
            bot.reply_to(message, f"已添加转发目标: {channel_id}")
            log_event(f"用户 {message.from_user.username} 添加转发目标 {channel_id}")
        else:
            bot.reply_to(message, "转发目标数量已达上限（3个）！")
    except IndexError:
        bot.reply_to(message, "请提供频道ID，例如: /set_sending_channel -100123456789")

# 命令：/add_admin - 添加管理员
@bot.message_handler(commands=['add_admin'])
def add_admin_command(message):
    """添加新的管理员"""
    if not is_admin(message.from_user.username):
        bot.reply_to(message, "抱歉，你没有权限执行这个操作！")
        return
    try:
        handle_name = message.text.split()[1]  # 获取命令后的Handle Name
        config = load_config()
        if handle_name not in config['admins']:
            config['admins'].append(handle_name)
            save_config(config)
            bot.reply_to(message, f"已添加管理员: {handle_name}")
            log_event(f"用户 {message.from_user.username} 添加管理员 {handle_name}")
        else:
            bot.reply_to(message, f"{handle_name} 已经是管理员！")
    except IndexError:
        bot.reply_to(message, "请提供Handle Name，例如: /add_admin @username")

# 命令：/rm_admin - 移除管理员
@bot.message_handler(commands=['rm_admin'])
def rm_admin_command(message):
    """显示管理员列表并等待用户选择移除"""
    if not is_admin(message.from_user.username):
        bot.reply_to(message, "抱歉，你没有权限执行这个操作！")
        return
    config = load_config()
    admins = config['admins']
    if not admins:
        bot.reply_to(message, "当前没有管理员可以移除！")
        return
    # 生成管理员列表，带编号
    admin_list = "\n".join([f"{i+1}. {admin}" for i, admin in enumerate(admins)])
    bot.reply_to(message, f"当前管理员列表：\n{admin_list}\n请回复要移除的管理员编号。")
    # 注册下一步处理函数
    bot.register_next_step_handler(message, process_rm_admin)

def process_rm_admin(message):
    """处理移除管理员的逻辑"""
    try:
        index = int(message.text) - 1  # 用户输入的编号（从1开始）
        config = load_config()
        admins = config['admins']
        if 0 <= index < len(admins):
            removed_admin = admins.pop(index)  # 移除指定管理员
            save_config(config)
            bot.reply_to(message, f"已移除管理员: {removed_admin}")
            log_event(f"用户 {message.from_user.username} 移除管理员 {removed_admin}")
        else:
            bot.reply_to(message, "无效的编号，请检查！")
    except ValueError:
        bot.reply_to(message, "请提供有效的编号，例如: 1")

# 监听频道消息并转发
@bot.channel_post_handler(func=lambda message: True)
def handle_channel_post(message):
    """监听频道消息，根据关键词转发"""
    config = load_config()
    # 只处理指定的监控频道
    if str(message.chat.id) != config['monitor_channel']:
        return
    text = message.text or ""  # 获取消息文本，如果没有文本则为空
    keyword_initial = config['keyword_initial']
    keyword_contain = config['keyword_contain']
    sending_channels = config['sending_channels']

    # 如果没有设置关键词，默认转发所有消息
    if not keyword_initial and not keyword_contain:
        for channel in sending_channels:
            bot.forward_message(channel, message.chat.id, message.message_id)
        log_event(f"转发消息 {message.message_id} 到 {sending_channels}")
        return

    # 检查关键词是否匹配
    should_forward = False
    for keyword in keyword_initial:
        if text.startswith(keyword):  # 检查开头关键词
            should_forward = True
            break
    if not should_forward:
        for keyword in keyword_contain:
            if keyword in text:  # 检查包含关键词
                should_forward = True
                break

    # 如果匹配，转发消息
    if should_forward:
        for channel in sending_channels:
            bot.forward_message(channel, message.chat.id, message.message_id)
        log_event(f"转发消息 {message.message_id} 到 {sending_channels}（匹配关键词）")

# 主程序入口
if __name__ == '__main__':
    # 启动日志清理线程
    cleanup_thread = threading.Thread(target=log_cleanup_thread, daemon=True)
    cleanup_thread.start()
    # 启动时执行一次清理（确保启动时清理过期日志）
    clean_old_logs()
    print("Bot已启动，正在监听消息...")
    # 启动Bot，持续监听消息
    bot.polling(none_stop=True)