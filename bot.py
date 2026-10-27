# -*- coding: utf-8 -*-
"""
Telegram 聊天机器人（DeepSeek 驱动）
功能：
- 私聊机器人：测试同一套陪聊逻辑、收藏贴纸
- 模式二：Telegram「聊天自动化」连接后，以你的账号身份自动回复指定私聊
- 每个聊天独立记忆，自动截断旧消息省钱
- /start 打招呼  /reset 清空记忆重新开始  /help 查看帮助
"""

import asyncio
import json
import logging
import os
import random
import re
import socket
from collections import OrderedDict, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from openai import AsyncOpenAI
from telegram import Message, Update
from telegram.constants import ChatAction, ChatType
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    BusinessConnectionHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from runtime_support import save_json, setup_logging

logger = logging.getLogger("tg-bot")

client: AsyncOpenAI | None = None

# 每个聊天的对话历史：{聊天标识: [{"role":..., "content":...}, ...]}
# 保留原有 key 以兼容本地记忆：私聊测试是 "用户id"，正式代聊是 "biz:对方id"。
chat_history: dict[str, list[dict]] = defaultdict(list)

# 缓存完整连接，包含启用状态和回复权限。
biz_connections: dict = {}
chat_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
chat_revisions: dict[str, int] = defaultdict(int)
pending_replies: dict[str, "PendingReply"] = {}


@dataclass
class PendingReply:
    message: Message
    revision: int
    connection: object
    texts: list[str] = field(default_factory=list)
    has_emotion: bool = False
    task: asyncio.Task | None = None

# 记忆本地保存文件（和本脚本同目录）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(BASE_DIR, "chat_history.json")
STICKER_FILE = os.path.join(BASE_DIR, "stickers.json")

# 收藏的贴纸库：[{"file_id":..., "emoji":..., "type":...}, ...]
stickers: list[dict] = []

# 每个聊天的贴纸冷却计数：{chat_key: 剩余多少条消息后才能再发贴纸}
sticker_cooldown: dict[str, int] = defaultdict(int)

# 已处理过的 business 消息 id，防止重复投递/循环刷屏
_processed_biz_ids: OrderedDict[tuple[str, int, int], None] = OrderedDict()


def acquire_single_instance_lock():
    """占用本地端口作为单实例锁，防止重复启动多个机器人互相抢消息。"""
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", 17890))
        lock.listen(1)
        return lock  # 保持引用，进程退出时系统自动释放
    except OSError:
        lock.close()
        return None

# AI 回复里的贴纸标记，形如 [sticker:1] 或 [sticker:random]
STICKER_TAG_RE = re.compile(r"\[sticker:\s*(random|\d+)\s*\]", re.IGNORECASE)
LEGACY_STICKER_STATUS_RE = re.compile(r"[\[【]\s*已发送\s*(\d+)\s*张贴纸\s*[\]】]")


def clean_reply_text(text: str) -> str:
    """发送出口兜底：模型标记和旧版内部状态都不能进入聊天正文。"""
    return LEGACY_STICKER_STATUS_RE.sub("", STICKER_TAG_RE.sub("", text)).strip()


def normalize_history_message(message: dict) -> dict:
    """兼容旧记忆，把 AI 回复里的发送状态搬到本地元数据。"""
    result = dict(message)
    if result.get("role") == "assistant" and result.get("sender") != "owner":
        statuses = LEGACY_STICKER_STATUS_RE.findall(result.get("content", ""))
        if statuses:
            result.setdefault("sent_stickers", int(statuses[-1]))
        result["content"] = clean_reply_text(result.get("content", ""))
    return result


@dataclass(frozen=True)
class DeliveryResult:
    text: str
    sent_stickers: int

# 用户消息里出现这些词 = 明确要求发贴纸，可无视冷却立即发
STICKER_REQUEST_RE = re.compile(
    r"(?:发|来|给|要|求|整).{0,16}(?:贴纸|表情包|表情)"
    r"|(?:贴纸|表情包).{0,12}(?:发|来|给|要)"
    r"|(?:再发|连发|多发|来|发|给我)\s*[零一二两三四五六七八九十百千万几多\d]*\s*[张个枚](?!\s*(?:照片|图片|截图|票|纸|文件))"
    r"|^(?:再发|连发|多发)[呀吧嘛呗～~!！。\s]*$"
)
NO_STICKER_RE = re.compile(r"(?:别|不要|不用|不想|停止|不准|禁止).{0,8}(?:发|贴纸|表情)|只(?:回|要|发)文字")

# 中文数字/数量词 -> 张数
STICKER_NUM_WORDS = {
    "零": 0, "一": 1, "两": 2, "二": 2, "三": 3,
    "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    "十": 10, "百": 100, "千": 1000, "万": 10000, "几": 2, "多": 2,
}

# 一次回复最多连发几张（防止刷屏）
MAX_STICKERS_PER_REPLY = 3


def wanted_sticker_count(user_text: str) -> int:
    """从用户消息里解析想要的贴纸张数：0=没要求，1~3=明确要的张数。"""
    if not user_text:
        return 0
    t = user_text.strip()
    if NO_STICKER_RE.search(t) or not STICKER_REQUEST_RE.search(t):
        return 0
    # 支持“3个贴纸”“两张”“三枚”等量词，不能把未识别的“个”默认为一张。
    m = re.search(r'([零一二两三四五六七八九十百千万几多\d]+)\s*[张个枚]', t)
    if m:
        token = m.group(1)
        if token.isdigit():
            n = int(token)
        else:
            # 发送上限为 3，带“十/百”等单位的中文数直接截到上限即可。
            n = max((STICKER_NUM_WORDS.get(ch, 0) for ch in token), default=0)
        return min(n, MAX_STICKERS_PER_REPLY)
    # 有“几张/多张/连发”等模糊说法 -> 默认 2 张
    if any(w in t for w in ("几张", "多张", "连发", "多发")):
        return 2
    return 1


def load_stickers() -> None:
    """启动时读取收藏的贴纸 file_id 列表。"""
    global stickers
    if not os.path.exists(STICKER_FILE):
        return
    try:
        with open(STICKER_FILE, "r", encoding="utf-8") as f:
            stickers = json.load(f)
        logger.info("已加载收藏贴纸：%s 张", len(stickers))
    except Exception as e:
        logger.warning("读取贴纸文件失败: %s", e)
        stickers = []


def save_stickers() -> bool:
    """保存贴纸库到本地文件。"""
    try:
        save_json(STICKER_FILE, stickers)
        return True
    except Exception as e:
        logger.warning("保存贴纸失败: %s", e)
        return False


def load_history() -> None:
    """启动时从本地文件读取历史记忆。"""
    if not os.path.exists(HISTORY_FILE):
        return
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for key, messages in data.items():
            chat_history[str(key)] = [normalize_history_message(m)
                                      for m in messages[-config.MAX_HISTORY:]]
        logger.info("已加载本地记忆：%s 个聊天", len(data))
    except Exception as e:
        logger.warning("读取记忆文件失败（将重新开始）: %s", e)


def save_history() -> None:
    """把记忆写回本地文件（先写临时文件再替换，防止写一半损坏）。"""
    try:
        save_json(HISTORY_FILE, dict(chat_history))
    except Exception as e:
        logger.warning("保存记忆失败: %s", e)


def build_system_prompt() -> str:
    """组装人设：基础人设 + 动态贴纸清单。"""
    prompt = config.SYSTEM_PROMPT
    if getattr(config, "ENABLE_STICKERS", True) and stickers:
        lines = [
            "",
            "【贴纸使用规则】",
            "你收藏了下面这些贴纸，可以在合适时发给对方：",
        ]
        for i, s in enumerate(stickers, 1):
            lines.append(f"{i}. 对应表情 {s.get('emoji') or '贴纸'}")
        lines += [
            "想发贴纸时，在回复的最后另起一行写 [sticker:序号]；想让程序随机挑一张写 [sticker:random]",
            "平时一条回复最多写一个 [sticker]；只有当用户明确要求连续多发时（如“发两张”“连发几张”），才在回复末尾连续写多行标记，每行一个、最多 3 行，且不要重复同一序号；想要随机就每行都写 [sticker:random]，程序会自动挑不同的",
            "主动用贴纸表达情绪，不必等用户索要。轻松闲聊、打招呼、撒娇、卖萌、亲亲、开心、安慰或委屈时，优先搭配一张契合的贴纸；轻松对话中以每 2～3 轮主动配一张为参考，程序会控制冷却，无需你额外克制。严肃讨论、处理实际问题或没有合适贴纸时只回文字；用户没要求时一次只发一张",
            "严禁使用列表以外的序号，正常聊天文字里也不要出现 [sticker:...] 之外的英文标记",
        ]
        prompt += "\n".join(lines)
    return prompt


def build_messages(chat_key: str, user_text: str) -> list[dict]:
    """只构造请求；发送成功后才提交记忆，避免记录未发出的回复。"""
    history = chat_history[chat_key] + [{"role": "user", "content": user_text}]
    messages = [{"role": "system", "content": build_system_prompt()}]
    for item in history[-config.MAX_HISTORY:]:
        # 旧文件兼容与输出脱敏独立防护；纯贴纸记录不伪装成 assistant 台词。
        item = normalize_history_message(item)
        if item.get("content"):
            messages.append({"role": item["role"], "content": item["content"]})
    return messages


def remember(chat_key: str, *messages: dict) -> None:
    history = chat_history[chat_key]
    history.extend(messages)
    del history[:-config.MAX_HISTORY]
    save_history()


def record_owner_says(chat_key: str, text: str) -> None:
    """本人插话时取消正在生成的旧回复，记忆保留真实发言。"""
    chat_revisions[chat_key] += 1
    if text:
        remember(chat_key, {"role": "assistant", "content": text, "sender": "owner"})


async def ask_deepseek(chat_key: str, user_text: str) -> str | None:
    """调用 DeepSeek，返回回复文本。"""
    global client
    messages = build_messages(chat_key, user_text)
    extra_body = {"thinking": {"type": "enabled" if config.THINKING else "disabled"}}
    try:
        if client is None:
            client = AsyncOpenAI(api_key=config.DEEPSEEK_API_KEY,
                                 base_url=config.BASE_URL, timeout=config.TIMEOUT,
                                 max_retries=1)
        resp = await client.chat.completions.create(
            model=config.MODEL,
            messages=messages,
            temperature=1.1,      # 高一点更活泼、更像撒娇
            max_tokens=300,
            extra_body=extra_body,
        )
        answer = (resp.choices[0].message.content or "").strip()
        if not answer:
            raise ValueError("模型返回空回复")
    except Exception as e:
        logger.error("API 调用失败: %s", e)
        return None

    usage = resp.usage
    if usage:
        logger.info(
            "聊天%s | 本轮 tokens: 输入%s 输出%s",
            chat_key, usage.prompt_tokens, usage.completion_tokens,
        )
    return answer


def extract_stickers(chat_key: str, reply: str, user_text: str = "",
                     requested_count: int | None = None) -> tuple[list[str], str]:
    """解析回复中的贴纸标记，返回 (file_id 列表, 纯文字)。

    规则：
    - 无贴纸/贴纸功能关闭 -> 返回空列表
    - 用户明确要求时，不依赖 AI 标记；标记缺失、无效或数量不足时随机补足
    - 用户没明确要求时，AI 自发最多发 1 张，且受冷却限制
    - 用户明确要求时（如“发两张”“连发几张”），无视冷却，按要求张数连发（最多 3 张）
    - 随机贴纸自动去重，不会连发同一张；库存不足时能发几张发几张
    """
    clean_text = clean_reply_text(reply)
    # 每轮回复冷却计数减 1
    sticker_cooldown[chat_key] = max(0, sticker_cooldown.get(chat_key, 0) - 1)

    if (not getattr(config, "ENABLE_STICKERS", True) or not stickers
            or NO_STICKER_RE.search(user_text)):
        return [], clean_text
    tags = STICKER_TAG_RE.findall(reply)
    wanted = wanted_sticker_count(user_text) if requested_count is None else requested_count
    if not tags and wanted == 0:
        return [], clean_text

    if wanted == 0 and sticker_cooldown[chat_key] > 0:
        logger.info("贴纸冷却中（剩 %s 条），本轮不发", sticker_cooldown[chat_key])
        return [], clean_text

    # 用户没要求 -> AI 自发限 1 张；要求了 -> 按要求张数
    limit = wanted if wanted >= 1 else 1
    limit = min(limit, MAX_STICKERS_PER_REPLY)

    file_ids: list[str] = []
    for tag in tags:
        if len(file_ids) >= limit:
            break
        tag = tag.lower()
        if tag == "random":
            pool = [s["file_id"] for s in stickers if s["file_id"] not in file_ids]
            if pool:
                file_ids.append(random.choice(pool))
        else:
            idx = int(tag)
            if 1 <= idx <= len(stickers):
                fid = stickers[idx - 1]["file_id"]
                if fid not in file_ids:
                    file_ids.append(fid)

    # 明确索要贴纸是用户指令，不能因 AI 漏写标记而只回复文字。
    # 先保留 AI 选中的有效贴纸，再从剩余库存中不重复地补足张数。
    if wanted > len(file_ids):
        pool = list(dict.fromkeys(
            s["file_id"] for s in stickers if s["file_id"] not in file_ids
        ))
        file_ids.extend(random.sample(pool, min(wanted - len(file_ids), len(pool))))

    if not file_ids:
        return [], clean_text

    if wanted >= 1:
        logger.info("对方明确要求贴纸，本轮计划发送 %s 张", len(file_ids))
    if len(file_ids) < wanted:
        logger.info("贴纸库存不足，想要 %s 张可发送 %s 张", wanted, len(file_ids))
    return file_ids, clean_text


async def deliver_reply(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    chat_key: str,
    reply: str,
    bc_id: str | None = None,
    target: Message | None = None,
    user_text: str = "",
    can_send: Callable[[], bool] | None = None,
    requested_stickers: int | None = None,
) -> DeliveryResult | None:
    """发送回复：先文字，再连续发贴纸（0~3 张）。
    - bc_id 不为空：聊天自动化模式，以账号主人身份发
    - target 不为空：普通模式，用收到的消息直接回复
    - user_text：触发本轮回复的用户消息，用于识别“明确要求贴纸”及张数
    """
    file_ids, text = extract_stickers(chat_key, reply, user_text, requested_stickers)

    sent_text = ""
    if text and (can_send is None or can_send()):
        try:
            if bc_id:
                await context.bot.send_message(
                    chat_id=chat_id, text=text, business_connection_id=bc_id,
                )
            elif target is not None:
                await target.reply_text(text)
            else:
                await context.bot.send_message(chat_id=chat_id, text=text)
            sent_text = text
        except TelegramError as e:
            logger.warning("文字发送失败（不自动重发，避免重复）: %s", e)

    sent_count = 0
    for file_id in file_ids:
        if can_send is not None and not can_send():
            break
        try:
            if bc_id:
                await context.bot.send_sticker(
                    chat_id=chat_id, sticker=file_id, business_connection_id=bc_id,
                )
            elif target is not None:
                await target.reply_sticker(file_id)
            else:
                await context.bot.send_sticker(chat_id=chat_id, sticker=file_id)
            sent_count += 1
        except Exception as e:
            logger.warning("贴纸发送失败: %s", e)
    if sent_count:
        # 只对真正发出的贴纸设置冷却，发送失败不占用额度。
        # 连发多张也使用同样的冷却，不再额外延长主动发送的等待轮数。
        sticker_cooldown[chat_key] = getattr(config, "STICKER_COOLDOWN", 2)
    if file_ids:
        logger.info("贴纸发送结果：成功 %s/%s 张 -> 聊天 %s", sent_count, len(file_ids), chat_key)
    return DeliveryResult(sent_text, sent_count) if sent_text or sent_count else None


async def reply_to_chat(context, message, chat_key, text, bc_id=None, *,
                        emotion_only=False, accompany_sticker=False) -> None:
    """测试和正式代聊共用：同一聊天顺序处理，不同聊天不互相阻塞。"""
    revision = chat_revisions[chat_key]
    connection = biz_connections.get(bc_id) if bc_id else None

    def can_send():
        if revision != chat_revisions[chat_key]:
            return False
        if bc_id:
            return (biz_connections.get(bc_id) is connection
                    and connection_can_reply(connection))
        return True

    async with chat_locks[chat_key]:
        if not can_send():
            return
        if emotion_only:
            # 单发表情不交给语言模型解读，也不生成任何文字回复。
            reply = "[sticker:random]"
        else:
            action_args = {"chat_id": message.chat.id, "action": ChatAction.TYPING}
            if bc_id:
                action_args["business_connection_id"] = bc_id
            try:
                await context.bot.send_chat_action(**action_args)
            except TelegramError as e:
                logger.info("无法显示正在输入，继续回复: %s", e)
            reply = await ask_deepseek(chat_key, text)
        if not can_send():
            logger.info("本人已插话或连接已停用，取消旧回复")
            return
        result = await deliver_reply(
            context, message.chat.id, chat_key,
            reply or "呜…刚才网络有点不顺，等一下再聊嘛🥺",
            bc_id=bc_id, target=None if bc_id else message,
            # 贴纸事件的内部描述不是用户“要求再发贴纸”的指令。
            user_text=text if reply and not emotion_only else "", can_send=can_send,
            requested_stickers=(1 if emotion_only else
                                max(1, wanted_sticker_count(text)) if accompany_sticker and reply else None),
        )
        if result and reply:
            remember(chat_key,
                     {"role": "user", "content": "" if emotion_only else text,
                      "sender": "peer", "emotion_only": emotion_only},
                     {"role": "assistant", "content": result.text, "sender": "ai",
                      "sent_stickers": result.sent_stickers})


def is_emotion_message(message: Message, text: str) -> bool:
    if message.sticker:
        return True
    # Unicode emoji、旗帜、肤色和组合表情；普通数字或带文字的句子不算。
    return bool(text.strip() and re.fullmatch(
        r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2300-\u23FF\u2B00-\u2BFF\uFE0E\uFE0F\u200D\s]+",
        text,
    ))


async def queue_chat_reply(context, message, chat_key, text, bc_id=None) -> None:
    """短时间连续消息合并成一轮，文字优先，表情仅作为附加回复信号。"""
    revision = chat_revisions[chat_key]
    connection = biz_connections.get(bc_id) if bc_id else None
    pending = pending_replies.get(chat_key)
    if pending is None or pending.revision != revision or pending.connection is not connection:
        pending = PendingReply(message, revision, connection)
        pending_replies[chat_key] = pending

    if is_emotion_message(message, text):
        pending.has_emotion = True
    elif text:
        if not pending.texts:
            pending.message = message
        pending.texts.append(text)

    async def flush():
        try:
            await asyncio.sleep(getattr(config, "MESSAGE_MERGE_SECONDS", 2.0))
            if (pending.revision != chat_revisions[chat_key]
                    or (bc_id and biz_connections.get(bc_id) is not pending.connection)):
                return
            # 开始生成后，新消息进入下一批；同一聊天仍由 chat_locks 顺序发送。
            if pending_replies.get(chat_key) is pending:
                pending_replies.pop(chat_key)
            await reply_to_chat(
                context, pending.message, chat_key, "\n".join(pending.texts), bc_id,
                emotion_only=not pending.texts and pending.has_emotion,
                accompany_sticker=bool(pending.texts) and pending.has_emotion,
            )
        finally:
            if pending_replies.get(chat_key) is pending:
                pending_replies.pop(chat_key)

    if pending.task is None:
        pending.task = asyncio.create_task(flush())
    await asyncio.shield(pending.task)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await help_cmd(update, context)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = str(update.effective_user.id)
    chat_revisions[uid] += 1
    chat_history.pop(uid, None)
    sticker_cooldown.pop(uid, None)
    save_history()
    await update.message.reply_text("好嘛，之前的都忘掉啦，宝宝重新跟我说话吧🥰")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "这里是陪聊测试窗口，直接聊天即可，不需要加【测试】。\n"
        "回复风格和正式代聊相同，测试记忆与正式聊天分开。\n"
        "发贴纸会自动收藏；单发表情只回贴纸，紧接文字的表情会合并到同一轮。\n"
        "本地启动程序后，机器人会在 Telegram 允许的私聊里代聊；停止程序即停止代聊。\n\n"
        "/stickers —— 查看已收藏的贴纸\n"
        "/delsticker —— 回复贴纸删除；也可用 /delsticker 编号\n"
        "/clearstickers —— 清空全部收藏贴纸\n"
        "/reset —— 只清空当前测试记忆\n"
        "/help —— 显示这条帮助"
    )


async def list_stickers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not stickers:
        await update.message.reply_text(
            "还没有收藏贴纸哦，你直接把贴纸发给我，我就会自动存起来😘"
        )
        return
    lines = [f"已经收藏 {len(stickers)} 张贴纸啦："]
    for i, s in enumerate(stickers, 1):
        lines.append(f"{i}. {s.get('emoji') or '贴纸'}")
    lines.append("删除：回复贴纸发送 /delsticker，或发送 /delsticker 编号。删除后编号会重新排列。")
    await update.message.reply_text("\n".join(lines))


async def clear_stickers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    stickers.clear()
    save_stickers()
    await update.message.reply_text("贴纸都清空啦，重新发给我就会重新收藏🥺")


async def delete_sticker_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    target = message.reply_to_message
    sticker = target.sticker if target else None
    if sticker is None:
        args = getattr(context, "args", None) or []
        if len(args) != 1 or not args[0].isascii() or not args[0].isdigit():
            await message.reply_text("请回复那张贴纸发送 /delsticker，或先用 /stickers 查看编号，再发送 /delsticker 17 这样的命令。")
            return
        index = int(args[0]) - 1
        if not 0 <= index < len(stickers):
            await message.reply_text("这个编号不在收藏库中，请用 /stickers 查看最新编号。")
            return
        remaining = stickers[:index] + stickers[index + 1:]
    else:
        unique_id = sticker.file_unique_id
        remaining = [s for s in stickers if not (
            s["file_id"] == sticker.file_id
            or (unique_id and s.get("file_unique_id") == unique_id)
        )]
    if len(remaining) == len(stickers):
        await message.reply_text("这张贴纸不在收藏库里，无需删除。")
        return
    previous = stickers[:]
    stickers[:] = remaining
    if not save_stickers():
        stickers[:] = previous
        await message.reply_text("保存失败，暂时没有删除，请稍后重试。")
        return
    await message.reply_text(f"已删除这张贴纸，剩余 {len(stickers)} 张收藏。以后重新发给我会再次收藏。")


async def handle_sticker_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """测试窗口收藏贴纸后照常聊天；正式代聊不修改贴纸库。"""
    message = update.message
    if not message or message.chat.type != ChatType.PRIVATE:
        return

    st = message.sticker
    if not st:
        return
    existing = next((s for s in stickers if s["file_id"] == st.file_id
                     or s.get("file_unique_id") == st.file_unique_id), None)
    item = {
        "file_id": st.file_id, "file_unique_id": st.file_unique_id,
        "emoji": st.emoji or "",
        "type": "video" if st.is_video else ("animated" if st.is_animated else "static"),
    }
    if existing is None:
        stickers.append(item)
    else:
        existing.update(item)
    save_stickers()
    await handle_message(update, context)


def connection_can_reply(conn) -> bool:
    return bool(conn and conn.is_enabled and conn.rights and conn.rights.can_reply)


def message_text(message: Message) -> str:
    # 不把贴纸转换成一段要求模型理解的描述。
    return message.text or message.caption or ""


async def on_business_connection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = update.business_connection
    biz_connections[conn.id] = conn
    logger.info("聊天自动化连接更新：启用=%s，可回复=%s", conn.is_enabled, connection_can_reply(conn))


async def handle_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.business_message
    if not message or message.chat.type != ChatType.PRIVATE:
        return
    bc_id = message.business_connection_id
    if not bc_id or message.sender_business_bot is not None or not message.from_user:
        return

    if bc_id not in biz_connections:
        try:
            conn = await context.bot.get_business_connection(bc_id)
            # 查询期间可能已收到更新的停用事件，不覆盖它。
            biz_connections.setdefault(bc_id, conn)
        except TelegramError as e:
            logger.warning("无法确认连接身份，跳过消息: %s", e)
            return
    conn = biz_connections[bc_id]
    if not connection_can_reply(conn):
        return

    # Telegram 的消息编号只在单个聊天内唯一。
    identity = (bc_id, message.chat.id, message.message_id)
    if identity in _processed_biz_ids:
        return
    _processed_biz_ids[identity] = None
    if len(_processed_biz_ids) > 1024:
        _processed_biz_ids.popitem(last=False)

    chat_key = f"biz:{message.chat.id}"
    text = message_text(message)
    if message.from_user.id == conn.user.id:
        record_owner_says(chat_key, text)
        return
    if message.from_user.id != message.chat.id or message.from_user.is_bot:
        return
    if not text and not message.sticker:
        return
    await queue_chat_reply(context, message, chat_key, text, bc_id)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or message.chat.type != ChatType.PRIVATE or not message.from_user:
        return
    if message.from_user.is_bot:
        return
    text = message_text(message)
    if not text and not message.sticker:
        await message.reply_text("目前可以聊文字和贴纸；图片、语音的内容还看不懂，发文字给我吧～")
        return
    # 兼容旧测试习惯，前缀不再影响身份或人设。
    text = re.sub(r"^\s*(?:\[测试\]|【测试】)\s*", "", text).strip()
    # 复制、等宽格式或前导空白可能让 Telegram 不提供 bot_command 实体。
    # 私聊管理指令必须在进入模型之前按实际文字再识别一次。
    if text.startswith("/"):
        command, *args = text.split()
        name, _, username = command[1:].partition("@")
        if username and username.casefold() != context.bot.username.casefold():
            return
        callback = private_commands().get(name.casefold())
        if callback is None:
            await message.reply_text("没有这个命令，请发送 /help 查看用法。")
        else:
            context.args = args
            await callback(update, context)
        return
    if text or message.sticker:
        await queue_chat_reply(context, message, str(message.from_user.id), text)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("处理更新失败: %s", context.error)


async def shutdown(application) -> None:
    global client
    if client is not None:
        await client.close()
        client = None


def private_commands():
    return {"start": start, "help": help_cmd, "reset": reset,
            "stickers": list_stickers_cmd, "clearstickers": clear_stickers_cmd,
            "delsticker": delete_sticker_cmd}


def build_application():
    app = (ApplicationBuilder().token(config.TG_BOT_TOKEN)
           .concurrent_updates(16).post_shutdown(shutdown).build())
    # 管理命令只作用于机器人私聊，绝不把正式聊天里的 /reset 当成管理命令。
    direct = filters.UpdateType.MESSAGE & filters.ChatType.PRIVATE
    app.add_handler(BusinessConnectionHandler(on_business_connection))
    app.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, handle_business_message))
    for command, callback in private_commands().items():
        app.add_handler(CommandHandler(command, callback, filters=direct))
    app.add_handler(MessageHandler(direct & filters.Sticker.ALL, handle_sticker_received))
    app.add_handler(MessageHandler(direct, handle_message))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    setup_logging(BASE_DIR, (config.TG_BOT_TOKEN, config.DEEPSEEK_API_KEY))
    if any(not key or "在这里填" in key
           for key in (config.TG_BOT_TOKEN, config.DEEPSEEK_API_KEY)):
        raise SystemExit("请先在 config.py 填入 TG_BOT_TOKEN 和 DEEPSEEK_API_KEY")
    # 单实例锁：防止重复启动多个机器人
    lock = acquire_single_instance_lock()
    if lock is None:
        raise SystemExit("检测到机器人已经在运行，不能重复启动！请先停止旧实例（双击停止机器人.bat）。")

    load_history()
    load_stickers()
    app = build_application()

    logger.info("机器人启动成功，按 Ctrl+C 停止")
    # drop_pending_updates：启动时丢弃离线期间积压的消息，防止上线后一次性补发刷屏
    try:
        app.run_polling(drop_pending_updates=True,
                        allowed_updates=["message", "business_connection", "business_message"])
    finally:
        lock.close()


if __name__ == "__main__":
    main()
