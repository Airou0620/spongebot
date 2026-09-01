import asyncio
import io
import logging
import os
import random
import re
import signal
import uuid
import warnings
from datetime import datetime
from time import time

import discord
from discord import File, Intents
from discord.ext import commands
from PIL import Image
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from storage import MemeStorage


# ============================================================
# 共用設定
# ============================================================

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]

SAVE_RECEIVED = os.getenv("SAVE_RECEIVED", "false").lower() in {
    "1", "true", "yes", "on"
}

DISCORD_MAX_MESSAGE_LENGTH = 1500

# 重要：Discord + Telegram 共用同一份 Storage / S3 client / cache
storage = MemeStorage()

warnings.filterwarnings("ignore", category=UserWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)

logger = logging.getLogger("spongebob")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("discord.client").setLevel(logging.WARNING)
logging.getLogger("discord.gateway").setLevel(logging.WARNING)


def log(message, level="info"):
    if level == "error":
        logger.error(str(message))
    else:
        logger.info(str(message))


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def bytes_file(data: bytes, filename: str) -> io.BytesIO:
    bio = io.BytesIO(data)
    bio.name = filename
    bio.seek(0)
    return bio


def discord_file(data: bytes, filename: str) -> File:
    return File(io.BytesIO(data), filename=filename)


def recompress_jpeg(data: bytes, quality=20) -> bytes:
    """保留 Telegram 舊版 blacklist 使用者的低畫質彩蛋。"""
    src = io.BytesIO(data)
    out = io.BytesIO()

    with Image.open(src) as img:
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.save(out, "JPEG", quality=quality, optimize=True)

    return out.getvalue()


# ============================================================
# Discord Bot
# ============================================================

discord_intents = Intents.default()
discord_intents.messages = True
discord_intents.message_content = True

discord_bot = commands.Bot(
    command_prefix="/",
    intents=discord_intents,
)


@discord_bot.tree.command(name="start", description="開始使用")
async def dc_start(interaction: discord.Interaction):
    await interaction.response.send_message(
        "歡迎使用海草先生怪物機！請輸入您想搜尋的海綿寶寶圖片關鍵字。\n"
        "你可以試試搜尋圖片關鍵字，或直接使用 S+編號來獲得你想要的圖片\n"
        "(例如 S2731)\n"
        "單獨打一個 K 可以查詢修改過的圖片\n"
        "其他指令如 /random（隨機抽圖）、/description（機器人簡介）",
        ephemeral=True,
    )
    log(f"[Discord] {interaction.user.display_name} 使用了 /start")


@discord_bot.tree.command(name="description", description="簡介")
async def dc_description(interaction: discord.Interaction):
    log(f"[Discord] {interaction.user.display_name} 使用了 /description")

    await interaction.response.send_message(
        "海草先生怪物機，由**艾璐(Airou)**撰寫\n"
        "靈感來自於 Line bot 派星機（由 Dcard 尖頭亮光光製作），"
        "歡迎分享給喜愛海綿寶寶小怪圖的朋友\n\n"
        "圖片來自於巴哈姆特，有點高清的海綿寶寶梗圖"
        "（版主 只是個普通の火神天）",
        ephemeral=True,
    )


@discord_bot.tree.command(name="random", description="隨機抽圖")
async def dc_random(interaction: discord.Interaction):
    await interaction.response.defer()

    files = await asyncio.to_thread(storage.list_memes)
    if not files:
        await interaction.followup.send(
            "目前 Bucket 裡沒有梗圖。",
            ephemeral=True,
        )
        return

    filename = random.choice(files)
    data = await asyncio.to_thread(storage.get_meme, filename)

    log(
        f"[Discord] {interaction.user.display_name} 使用隨機抽圖，"
        f"回覆 {filename}"
    )

    await interaction.followup.send(
        f"**{interaction.user.display_name}** 使用隨機抽圖抽到了__{filename}__",
        file=discord_file(data, filename),
    )


@discord_bot.tree.command(name="can_i", description="我可不可以..?")
async def dc_can_i(interaction: discord.Interaction):
    await interaction.response.defer()

    yn_files = await asyncio.to_thread(storage.list_yn)
    if not yn_files:
        await interaction.followup.send(
            "目前 Bucket 裡沒有 YN 圖庫。",
            ephemeral=True,
        )
        return

    target = "【SS1456】可以.jpg"

    if random.random() < 0.5 and target in yn_files:
        filename = target
    else:
        choices = [f for f in yn_files if f != target]
        filename = random.choice(choices or yn_files)

    data = await asyncio.to_thread(storage.get_yn, filename)

    log(
        f"[Discord] {interaction.user.display_name} 使用我可不可以，"
        f"回覆 {filename}"
    )

    await interaction.followup.send(
        f"**{interaction.user.display_name}** 使用我可不可以..抽到了__{filename}__",
        file=discord_file(data, filename),
    )


@discord_bot.tree.command(name="airou", description="彩蛋")
async def dc_airou(interaction: discord.Interaction):
    await interaction.response.send_message("咪咪，恭喜你找到彩蛋")
    log(f"[Discord] {interaction.user.display_name} 使用了 /airou")


@discord_bot.tree.command(name="jack", description="Say cheese")
async def dc_jack(interaction: discord.Interaction):
    await interaction.response.send_message("Say cheese")
    log(f"[Discord] {interaction.user.display_name} 使用了 /jack")


@discord_bot.tree.command(name="s", description="搜尋海寶圖")
async def dc_search(interaction: discord.Interaction, keyword: str):
    try:
        log(f"[Discord] {interaction.user.display_name}：{keyword}")

        matching = await asyncio.to_thread(storage.search_memes, keyword)

        if not matching:
            await interaction.response.send_message(
                f"海草先生怪物機找不到符合「{keyword}」的檔案。",
                ephemeral=True,
            )
            return

        if len(matching) == 1:
            await interaction.response.defer()

            filename = matching[0]
            data = await asyncio.to_thread(storage.get_meme, filename)

            await interaction.followup.send(
                f"**{interaction.user.display_name}** 傳送了：__{filename}__",
                file=discord_file(data, filename),
            )
            return

        response = ""
        has_replied = False

        for filename in matching:
            line = f"{filename}\n"

            if (
                len(response) + len(line) > DISCORD_MAX_MESSAGE_LENGTH
                and response
            ):
                if not has_replied:
                    await interaction.response.send_message(
                        response,
                        ephemeral=True,
                    )
                    has_replied = True
                else:
                    await interaction.followup.send(
                        response,
                        ephemeral=True,
                    )
                response = ""

            response += line

        if response:
            if not has_replied:
                await interaction.response.send_message(
                    response,
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    response,
                    ephemeral=True,
                )

    except Exception as e:
        log(f"[Discord] Exception: {e}", level="error")

        msg = (
            "海草先生怪物機好像出了點問題，"
            "請聯繫**艾璐**貓咪進行修理"
        )

        if not interaction.response.is_done():
            await interaction.response.send_message(
                msg,
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                msg,
                ephemeral=True,
            )


@discord_bot.event
async def on_ready():
    try:
        synced = await discord_bot.tree.sync()

        print(
            f"[Discord] 登入身份：{discord_bot.user} | "
            f"Slash commands：{len(synced)}"
        )
    except Exception as e:
        log(f"[Discord] 同步 Slash Commands 失敗：{e}", level="error")


# ============================================================
# Telegram Bot
# ============================================================

async def tg_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    await update.message.reply_text(
        "歡迎使用海草先生怪物機！請輸入您想搜尋的海綿寶寶圖片關鍵字。"
    )
    await update.message.reply_text(
        "你可以試試搜尋圖片關鍵字，或直接使用 S+編號來獲得你想要的圖片\n"
        "(例如 S2731)"
    )
    await update.message.reply_text(
        "單獨打一個 K 可以查詢修改過的圖片"
    )
    await update.message.reply_text(
        "其他指令如 /random（隨機抽圖）、/description（機器人簡介）"
    )


async def tg_description(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    await update.message.reply_text(
        "海草先生怪物機，由艾璐(Airou)撰寫\n"
        "靈感來自於 Line bot 派星機（由 Dcard 尖頭亮光光製作），"
        "歡迎分享給喜愛海綿寶寶小怪圖的朋友\n"
    )

    await update.message.reply_sticker(
        sticker=(
            "CAACAgUAAxkBAAEqsrVmECin_-lQdzlDsoxuaN_"
            "PW4978wACdw0AAgZPAAFVESQ4XyOgVeI0BA"
        )
    )

    await update.message.reply_text(
        "圖片來自於巴哈姆特，有點高清的海綿寶寶梗圖"
        "（版主 只是個普通の火神天）"
    )


async def tg_reply_to_sticker(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message or not update.effective_user:
        return

    responses = [
        "好希望我有眼睛，好讓我看清楚你傳了什麼",
        "是什麼樣的刺激讓你想傳貼圖給一個機器人",
        "嗶嗶，機油好好喝",
        "吃著火鍋唱著歌，突然傳貼圖是什麼意思",
        "Service provided by Railway, beep beep",
        "艾璐教授加博士先生並沒有讓我學會看貼圖",
        (
            "作者是隻大貓咪，叫他小貓咪他會生氣\n"
            "https://x.com/AirouCat620/status/1769713690570551797"
        ),
        "我不接受吃咖哩會拌的人用我做的Bot -Airou",
        "這貼圖沒什麼好看的，你可以停了",
        "我寫這句話的時候是2024/04/06 03:39AM，Just let you know",
        "Meeeeeeeow",
        "世界上已經有海草先生怪物人了",
        "有了，閣樓",
        (
            "給你看我家的貓，他叫呆咩(玳瑁的諧音)，"
            "aka 臭臭、沙茶醬、美眉\n"
            "https://x.com/AirouCat620/status/1447790010996965376"
        ),
        (
            "作者現職是隻貓\n"
            "https://x.com/AirouCat620/status/1758871519328166037"
        ),
        (
            "你應該去用用看超優質的機器人，派星機！\n"
            "https://www.dcard.tw/f/spongebob/p/253298007"
        ),
        "瑞瑞愛吃刺刺球，而我喜歡喝機油",
        "SJF scheduling may lead to starvation",
        (
            "40 6b 6b 1f 68 6d 72 73 60 6d 62 64 72 1f 71 64 "
            "70 74 68 71 64 72 1f 6e 74 73 72 73 60 6d 63 68 "
            "6d 66 1f 74 6d 63 64 71 72 73 60 6d 63 68 6d 66 2d"
        ),
        "香菜很好吃，推薦你也試試看",
        "小農牛奶與果汁牛乳的全家霜淇淋是最頂的",
        "Rush B!",
        "熱狗熱狗，好吃好吃好吃",
        "You're goofy goober, rock!~",
        "你這個吹泡泡的臭小鬼",
        "吹一次兩毛五，先生",
        "好棒，E小調",
        (
            "艾璐 @SmawaKemono ，"
            "歡迎跟我主人反應機器人的問題或給出建議！"
        ),
    ]

    reply = random.choice(responses)

    await update.message.reply_text(reply)

    log(
        f"[Telegram] {update.effective_user.full_name}："
        f"(貼圖) 回應「{reply}」"
    )


async def tg_reply_to_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message or not update.effective_user:
        return

    user = update.effective_user

    num = context.user_data.get("num", 0) + 1
    context.user_data["num"] = num

    if SAVE_RECEIVED:
        photo = await update.message.photo[-1].get_file()
        data = bytes(await photo.download_as_bytearray())

        safe_name = re.sub(
            r'[\\/:*?"<>|]+',
            "_",
            user.full_name,
        )

        filename = (
            f"{datetime.now():%Y%m%d_%H%M%S}_"
            f"{safe_name}_{num}_{uuid.uuid4().hex[:8]}.jpg"
        )

        await asyncio.to_thread(
            storage.save_received,
            filename,
            data,
            "image/jpeg",
        )

    airou_files = await asyncio.to_thread(storage.list_airou)

    if not airou_files:
        await update.message.reply_text(
            "我收到你的圖片了，但作者照片圖庫目前還沒上傳到 Bucket。"
        )
        return

    filename = random.choice(airou_files)
    data = await asyncio.to_thread(storage.get_airou, filename)

    await update.message.reply_text(
        f"我拿這張圖跟 {user.full_name} 交換！"
        "這是一張作者的照片"
    )

    await update.message.reply_photo(
        photo=bytes_file(data, filename),
        filename=filename,
    )

    log(
        f"[Telegram] {user.full_name} 傳送第 {num} 張圖片，"
        f"回覆 {filename}"
    )


async def tg_random(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message or not update.effective_user:
        return

    files = await asyncio.to_thread(storage.list_memes)

    if not files:
        await update.message.reply_text(
            "目前 Bucket 裡沒有梗圖。"
        )
        return

    filename = random.choice(files)
    data = await asyncio.to_thread(storage.get_meme, filename)

    await update.message.reply_photo(
        photo=bytes_file(data, filename),
        filename=filename,
    )

    log(
        f"[Telegram] {update.effective_user.full_name} "
        f"使用隨機抽圖，回覆 {filename}"
    )


async def tg_can_i(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message or not update.effective_user:
        return

    files = await asyncio.to_thread(storage.list_yn)

    if not files:
        await update.message.reply_text(
            "目前 Bucket 裡沒有 YN 圖庫。"
        )
        return

    target = "【SS1456】可以.jpg"

    if random.random() < 0.5 and target in files:
        filename = target
    else:
        choices = [f for f in files if f != target]
        filename = random.choice(choices or files)

    data = await asyncio.to_thread(storage.get_yn, filename)

    await update.message.reply_photo(
        photo=bytes_file(data, filename),
        filename=filename,
    )

    log(
        f"[Telegram] {update.effective_user.full_name} "
        f"使用我可不可以，回覆 {filename}"
    )


async def tg_airou(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if update.message and update.effective_user:
        await update.message.reply_text(
            "咪咪，恭喜你找到彩蛋"
        )
        log(
            f"[Telegram] {update.effective_user.full_name} "
            "使用 /airou"
        )


async def tg_jack(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if update.message and update.effective_user:
        await update.message.reply_text("Say cheese")
        log(
            f"[Telegram] {update.effective_user.full_name} "
            "使用 /jack"
        )


async def tg_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    blacklist = ["pipikapi"]

    try:
        if (
            not update.message
            or not update.effective_user
            or update.message.text is None
        ):
            return

        start_time = time()

        user = update.effective_user
        username = user.username or "no_username"
        keyword = update.message.text

        log(
            f"[Telegram] {user.full_name}"
            f"({username})：{keyword}"
        )

        matching = await asyncio.to_thread(
            storage.search_memes,
            keyword,
        )

        if len(matching) > 300:
            await update.message.reply_text(
                "搜尋結果超過300個，"
                f"請調整關鍵字後重新搜尋({len(matching)})"
            )
            return

        if not matching:
            await update.message.reply_text(
                f'海草先生怪物機找不到符合"{keyword}"的檔案。'
            )

        elif len(matching) == 1:
            filename = matching[0]
            data = await asyncio.to_thread(
                storage.get_meme,
                filename,
            )

            if username in blacklist:
                data = await asyncio.to_thread(
                    recompress_jpeg,
                    data,
                    20,
                )

            await update.message.reply_photo(
                photo=bytes_file(data, filename),
                filename=filename,
            )

            if username in blacklist:
                await update.message.reply_text(
                    "橘色小怪物"
                )
                log("[Telegram] 回覆糊糊的圖")

        else:
            response = ""

            for filename in matching:
                line = f"{filename}\n"

                if len(response) + len(line) > 3000 and response:
                    await update.message.reply_text(response)
                    response = ""

                response += line

            if response:
                await update.message.reply_text(response)

        elapsed = time() - start_time

        if len(matching) > 1:
            await update.message.reply_text(
                f"共 {len(matching)} 筆搜尋結果，"
                f"用時 {elapsed:.2f} 秒"
            )

    except Exception as e:
        log(
            f"[Telegram] Exception: {e}",
            level="error",
        )

        if update.message:
            await update.message.reply_text(
                "海草先生怪物機出了點問題(冒黑煙)，"
                "請聯絡 @SmawaKemono 修復"
            )


def build_telegram_application() -> Application:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(
        CommandHandler("start", tg_start)
    )
    app.add_handler(
        CommandHandler("description", tg_description)
    )
    app.add_handler(
        CommandHandler("random", tg_random)
    )
    app.add_handler(
        CommandHandler("can_i", tg_can_i)
    )
    app.add_handler(
        CommandHandler("airou", tg_airou)
    )
    app.add_handler(
        CommandHandler("jack", tg_jack)
    )
    app.add_handler(
        MessageHandler(
            filters.Sticker.ALL,
            tg_reply_to_sticker,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.PHOTO,
            tg_reply_to_photo,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            tg_search,
        )
    )

    return app


# ============================================================
# 同一個 asyncio event loop 同時執行兩隻 Bot
# ============================================================

async def start_telegram(app: Application):
    """
    python-telegram-bot 官方建議：
    與其他 asyncio framework 共存時不要用 run_polling()，
    而是手動管理 initialize/start_polling/start/shutdown。
    """
    await app.initialize()

    if app.updater is None:
        raise RuntimeError("Telegram Updater 不存在")

    # 依 PTB run_polling() 的官方 lifecycle 順序
    await app.updater.start_polling()
    await app.start()

    me = await app.bot.get_me()
    print(
        f"[Telegram] 登入身份：@{me.username} "
        f"(id={me.id})"
    )


async def stop_telegram(app: Application):
    try:
        if app.updater is not None and app.updater.running:
            await app.updater.stop()
    finally:
        if app.running:
            await app.stop()

        await app.shutdown()


async def main():
    # 啟動前只抓一次 Bucket 檔名清單，不下載整個圖庫。
    print("[Storage] 正在載入 Bucket 檔名索引...")
    await asyncio.to_thread(storage.refresh)

    print(
        "[Storage] 載入完成："
        f"memes={len(storage.list_memes())}, "
        f"YN={len(storage.list_yn())}, "
        f"Airou={len(storage.list_airou())}"
    )

    tg_app = build_telegram_application()

    # Railway / Linux 用 SIGTERM 停 service 時可以收尾。
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(
                sig,
                stop_event.set,
            )
        except NotImplementedError:
            # Windows 本機測試可能不支援 add_signal_handler
            pass

    await start_telegram(tg_app)

    discord_task = asyncio.create_task(
        discord_bot.start(DISCORD_TOKEN),
        name="discord-bot",
    )

    stop_task = asyncio.create_task(
        stop_event.wait(),
        name="stop-signal",
    )

    print(
        "[Combined] Telegram + Discord 已在同一個 Python process 執行"
    )

    try:
        done, _ = await asyncio.wait(
            {discord_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        # 如果不是因為 SIGTERM/SIGINT，而是 Discord task 自己退出，
        # 將 exception 往上拋，讓 Railway 視為 crash 並重啟。
        if discord_task in done:
            exc = discord_task.exception()
            if exc is not None:
                raise exc

    finally:
        stop_task.cancel()

        if not discord_bot.is_closed():
            await discord_bot.close()

        if not discord_task.done():
            discord_task.cancel()

        try:
            await discord_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log(
                f"[Discord] 關閉時發生錯誤：{e}",
                level="error",
            )

        await stop_telegram(tg_app)

        print("[Combined] Bot 已關閉")


if __name__ == "__main__":
    asyncio.run(main())
