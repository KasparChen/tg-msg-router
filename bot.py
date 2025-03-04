import os
import json
from datetime import datetime, timedelta
import pytz
import telebot
import boto3
import threading
import time
from dotenv import load_dotenv  # 用于加载 .env 文件

# 设置中国时区（UTC+8）
TZ = pytz.timezone('Asia/Shanghai')

# 加载 .env 文件
load_dotenv()

# 从环境变量读取配置
BOT_TOKEN = os.getenv('BOT_TOKEN')  # Telegram Bot 的 Token
S3_BUCKET = os.getenv('S3_BUCKET')  # S3 Bucket 名称
SUPER_ADMINS_RAW = os.getenv('SUPER_ADMINS', '[]')  # 超级管理员列表，默认空列表
print(f"原始 SUPER_ADMINS 值: {SUPER_ADMINS_RAW}")  # 调试：显示从 .env 读取的原始值
try:
    SUPER_ADMINS = json.loads(SUPER_ADMINS_RAW)  # 解析为 Python 列表
    print(f"解析后的 SUPER_ADMINS: {SUPER_ADMINS}")  # 调试：显示解析结果
except json.JSONDecodeError as e:
    print(f"错误：SUPER_ADMINS 解析失败，格式错误: {e}")
    SUPER_ADMINS = []

# 检查环境变量是否正确设置
if not BOT_TOKEN:
    print("错误：BOT_TOKEN 未设置，请检查 .env 文件或环境变量！")
    exit(1)
if not S3_BUCKET:
    print("错误：S3_BUCKET 未设置，请检查 .env 文件或环境变量！")
    exit(1)

# 初始化 S3 客户端，用于操作 S3 存储
s3 = boto3.client('s3')

# 配置和日志文件在 S3 中的路径
CONFIG_KEY = 'config.json'  # 配置文件名
LOG_PREFIX = 'logs/'  # 日志文件前缀

# 初始化 Telegram Bot
print("Bot正在连接...")  # 提示 Bot 开始连接
bot = telebot.TeleBot(BOT_TOKEN)
print("Bot已连接，正在运行...")  # 连接成功

# 从 S3 加载配置
def load_config():
    """从 S3 读取配置文件，如果不存在返回默认配置"""
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=CONFIG_KEY)
        return json.loads(obj['Body'].read().decode('utf-8'))
    except s3.exceptions.NoSuchKey:
        # 默认配置：无监控频道、无关键词、空的转发目标、管理员为超级管理员
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

# 清理超过三天的日志
def clean_old_logs():
    """清理三天前的日志文件"""
    now = datetime.now(TZ)
    cutoff_date = now - timedelta(days=3)
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=LOG_PREFIX):
        for obj in page.get('Contents', []):
            log_date_str = obj['Key'].split('/')[-1].replace('.log', '')
            try:
                log_date = datetime.strptime(log_date_str, '%Y-%m-%d').replace(tzinfo=TZ)
                if log_date < cutoff_date:
                    s3.delete_object(Bucket=S3_BUCKET, Key=obj['Key'])
                    log_event(f"清理日志文件: {obj['Key']}")
            except ValueError:
                continue

# 日志清理线程
def log_cleanup_thread():
    """每分钟检查一次，如果是 0 点则清理旧日志"""
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
    """检查用户是否为管理员或超级管理员，确保匹配 @karl_che"""
    config = load_config()
    if not username:  # 如果用户名为空（未设置 Telegram 用户名）
        print("权限检查失败: 用户名为空")
        return False
    # 规范化用户名：添加 @ 前缀并转为小写
    username = f"@{username}" if not username.startswith('@') else username
    username = username.lower()
    admins = [x.lower() for x in config['admins']]  # 普通管理员列表
    super_admins = [x.lower() for x in SUPER_ADMINS]  # 超级管理员列表
    is_admin_flag = username in admins or username in super_admins
    # 调试：打印权限检查详情
    print(f"权限检查: 输入用户={username}, 配置admins={admins}, 配置super_admins={super_admins}, 是否管理员={is_admin_flag}")
    return is_admin_flag

# 命令：/help - 显示使用指南
@bot.message_handler(commands=['help'])
def help_command(message):
    """发送 Bot 的使用指南"""
    help_text = """
    使用指南：
    /help - 显示这个指南
    /status - 查看当前Bot配置
    /get_group_id - 获取当前群组的ID（无需权限）
    /set_monitor_channel <channel_id> - 设置要监控的频道
    /set_keyword_initial <keyword> - 设置开头关键词
    /set_keyword_contain <keyword> - 设置包含关键词
    /set_sending_channel <channel_id> - 设置转发目标
    /add_admin <handle_name> - 添加管理员
    /rm_admin - 移除管理员
    """
    bot.reply_to(message, help_text)
    log_event(f"用户 @{message.from_user.username} 执行 /help")

# 命令：/status - 显示当前配置
@bot.message_handler(commands=['status'])
def status_command(message):
    """显示 Bot 的当前配置"""
    username = message.from_user.username
    print(f"收到命令 /status，用户名: {username}")
    if not is_admin(username):
        bot.reply_to(message, f"抱歉，你没有权限执行这个操作！你的用户名: @{username}")
        return
    config = load_config()
    monitor_channel_text = "未设置"
    if config['monitor_channel']:
        try:
            chat = bot.get_chat(config['monitor_channel'])
            monitor_channel_text = f"{chat.title} ({config['monitor_channel']})"
        except:
            monitor_channel_text = f"未知频道 ({config['monitor_channel']})"
    keyword_initial_text = ", ".join(config['keyword_initial']) if config['keyword_initial'] else "未设置"
    keyword_contain_text = ", ".join(config['keyword_contain']) if config['keyword_contain'] else "未设置"
    sending_channels_text = []
    for i, channel_id in enumerate(config['sending_channels'], 1):
        try:
            chat = bot.get_chat(channel_id)
            sending_channels_text.append(f"[{i}] {chat.title} ({channel_id})")
        except:
            sending_channels_text.append(f"[{i}] 未知频道 ({channel_id})")
    sending_channels_text = "\n".join(sending_channels_text) if sending_channels_text else "未设置"
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
    log_event(f"用户 @{username} 查看状态: 监控频道={monitor_channel_text}, 句首关键词={keyword_initial_text}, 句中关键词={keyword_contain_text}, 转发频道={sending_channels_text}")

# 命令：/get_group_id - 获取当前群组 ID
@bot.message_handler(commands=['get_group_id'])
def get_group_id_command(message):
    """返回当前聊天窗口的群组或频道 ID，并显示用户名"""
    group_id = message.chat.id
    username = message.from_user.username
    bot.reply_to(message, f"当前群组的ID是: {group_id}, 你的用户名是: @{username}")
    log_event(f"用户 @{username} 获取群组ID: {group_id}")

# 命令：/set_monitor_channel - 设置监控频道
@bot.message_handler(commands=['set_monitor_channel'])
def set_monitor_channel_command(message):
    """设置要监控的频道，只能设置一个"""
    username = message.from_user.username
    print(f"收到命令 /set_monitor_channel，用户名: {username}")
    if not is_admin(username):
        bot.reply_to(message, f"抱歉，你没有权限执行这个操作！你的用户名: @{username}")
        return
    try:
        channel_id = message.text.split()[1]
        config = load_config()
        old_channel = config['monitor_channel']
        config['monitor_channel'] = channel_id
        save_config(config)
        bot.reply_to(message, f"监控频道已设置为: {channel_id}")
        print(f"配置更新 - 用户 @{username} 将监控频道从 {old_channel} 变更为 {channel_id}")
        log_event(f"用户 @{username} 设置监控频道: 从 {old_channel} 变更为 {channel_id}")
    except IndexError:
        bot.reply_to(message, "请提供频道ID，例如: /set_monitor_channel -100123456789")

# 命令：/set_keyword_initial - 设置开头关键词
@bot.message_handler(commands=['set_keyword_initial'])
def set_keyword_initial_command(message):
    """设置消息开头包含的关键词，最多 5 个"""
    username = message.from_user.username
    print(f"收到命令 /set_keyword_initial，用户名: {username}")
    if not is_admin(username):
        bot.reply_to(message, f"抱歉，你没有权限执行这个操作！你的用户名: @{username}")
        return
    try:
        keyword = message.text.split()[1]
        config = load_config()
        if len(config['keyword_initial']) < 5:
            config['keyword_initial'].append(keyword)
            save_config(config)
            bot.reply_to(message, f"已添加开头关键词: {keyword}")
            print(f"配置更新 - 用户 @{username} 添加开头关键词: {keyword}")
            log_event(f"用户 @{username} 添加开头关键词: {keyword}, 当前句首关键词列表: {config['keyword_initial']}")
        else:
            bot.reply_to(message, "开头关键词数量已达上限（5个）！")
    except IndexError:
        bot.reply_to(message, "请提供关键词，例如: /set_keyword_initial [Alpha]")

# 命令：/set_keyword_contain - 设置包含关键词
@bot.message_handler(commands=['set_keyword_contain'])
def set_keyword_contain_command(message):
    """设置消息中包含的关键词，最多 5 个"""
    username = message.from_user.username
    print(f"收到命令 /set_keyword_contain，用户名: {username}")
    if not is_admin(username):
        bot.reply_to(message, f"抱歉，你没有权限执行这个操作！你的用户名: @{username}")
        return
    try:
        keyword = message.text.split()[1]
        config = load_config()
        if len(config['keyword_contain']) < 5:
            config['keyword_contain'].append(keyword)
            save_config(config)
            bot.reply_to(message, f"已添加包含关键词: {keyword}")
            print(f"配置更新 - 用户 @{username} 添加包含关键词: {keyword}")
            log_event(f"用户 @{username} 添加包含关键词: {keyword}, 当前句中关键词列表: {config['keyword_contain']}")
        else:
            bot.reply_to(message, "包含关键词数量已达上限（5个）！")
    except IndexError:
        bot.reply_to(message, "请提供关键词，例如: /set_keyword_contain CA")

# 命令：/set_sending_channel - 设置转发目标
@bot.message_handler(commands=['set_sending_channel'])
def set_sending_channel_command(message):
    """设置消息转发的目标，最多 3 个"""
    username = message.from_user.username
    print(f"收到命令 /set_sending_channel，用户名: {username}")
    if not is_admin(username):
        bot.reply_to(message, f"抱歉，你没有权限执行这个操作！你的用户名: @{username}")
        return
    try:
        channel_id = message.text.split()[1]
        config = load_config()
        if len(config['sending_channels']) < 3:
            config['sending_channels'].append(channel_id)
            save_config(config)
            bot.reply_to(message, f"已添加转发目标: {channel_id}")
            print(f"配置更新 - 用户 @{username} 添加转发目标: {channel_id}")
            log_event(f"用户 @{username} 添加转发目标: {channel_id}, 当前转发频道列表: {config['sending_channels']}")
        else:
            bot.reply_to(message, "转发目标数量已达上限（3个）！")
    except IndexError:
        bot.reply_to(message, "请提供频道ID，例如: /set_sending_channel -100123456789")

# 命令：/add_admin - 添加管理员
@bot.message_handler(commands=['add_admin'])
def add_admin_command(message):
    """添加新的管理员"""
    username = message.from_user.username
    print(f"收到命令 /add_admin，用户名: {username}")
    if not is_admin(username):
        bot.reply_to(message, f"抱歉，你没有权限执行这个操作！你的用户名: @{username}")
        return
    try:
        handle_name = message.text.split()[1]
        config = load_config()
        if handle_name not in config['admins']:
            config['admins'].append(handle_name)
            save_config(config)
            bot.reply_to(message, f"已添加管理员: {handle_name}")
            print(f"配置更新 - 用户 @{username} 添加管理员: {handle_name}")
            log_event(f"用户 @{username} 添加管理员: {handle_name}, 当前管理员列表: {config['admins']}")
        else:
            bot.reply_to(message, f"{handle_name} 已经是管理员！")
    except IndexError:
        bot.reply_to(message, "请提供Handle Name，例如: /add_admin @username")

# 命令：/rm_admin - 移除管理员
@bot.message_handler(commands=['rm_admin'])
def rm_admin_command(message):
    """显示管理员列表并等待用户选择移除"""
    username = message.from_user.username
    print(f"收到命令 /rm_admin，用户名: {username}")
    if not is_admin(username):
        bot.reply_to(message, f"抱歉，你没有权限执行这个操作！你的用户名: @{username}")
        return
    config = load_config()
    admins = config['admins']
    if not admins:
        bot.reply_to(message, "当前没有管理员可以移除！")
        return
    admin_list = "\n".join([f"{i+1}. {admin}" for i, admin in enumerate(admins)])
    bot.reply_to(message, f"当前管理员列表：\n{admin_list}\n请回复要移除的管理员编号。")
    log_event(f"用户 @{username} 请求移除管理员，当前管理员列表: {admins}")
    bot.register_next_step_handler(message, process_rm_admin)

def process_rm_admin(message):
    """处理移除管理员的逻辑"""
    username = message.from_user.username
    try:
        index = int(message.text) - 1
        config = load_config()
        admins = config['admins']
        if 0 <= index < len(admins):
            removed_admin = admins.pop(index)
            save_config(config)
            bot.reply_to(message, f"已移除管理员: {removed_admin}")
            print(f"配置更新 - 用户 @{username} 移除管理员: {removed_admin}")
            log_event(f"用户 @{username} 移除管理员: {removed_admin}, 当前管理员列表: {admins}")
        else:
            bot.reply_to(message, "无效的编号，请检查！")
    except ValueError:
        bot.reply_to(message, "请提供有效的编号，例如: 1")

# 监听频道消息并转发
@bot.channel_post_handler(func=lambda message: True)
def handle_channel_post(message):
    """监听频道消息，根据关键词转发"""
    config = load_config()
    if str(message.chat.id) != config['monitor_channel']:
        return
    text = message.text or ""
    keyword_initial = config['keyword_initial']
    keyword_contain = config['keyword_contain']
    sending_channels = config['sending_channels']

    if not keyword_initial and not keyword_contain:
        for channel in sending_channels:
            bot.forward_message(channel, message.chat.id, message.message_id)
        log_event(f"转发消息 {message.message_id} 从 {message.chat.id} 到 {sending_channels}（无关键词，默认转发）")
        return

    should_forward = False
    matched_keyword = None
    for keyword in keyword_initial:
        if text.startswith(keyword):
            should_forward = True
            matched_keyword = keyword
            break
    if not should_forward:
        for keyword in keyword_contain:
            if keyword in text:
                should_forward = True
                matched_keyword = keyword
                break

    if should_forward:
        for channel in sending_channels:
            bot.forward_message(channel, message.chat.id, message.message_id)
        log_event(f"转发消息 {message.message_id} 从 {message.chat.id} 到 {sending_channels}（匹配关键词: {matched_keyword}）")

# 主程序入口
if __name__ == '__main__':
    # 启动日志清理线程
    cleanup_thread = threading.Thread(target=log_cleanup_thread, daemon=True)
    cleanup_thread.start()
    clean_old_logs()  # 启动时清理一次旧日志
    print("Bot初始化完成，开始监听消息...")
    bot.polling(none_stop=True)  # 持续监听消息