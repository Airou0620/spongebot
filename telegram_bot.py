import asyncio
import io
import logging
import os
import random
import re
import uuid
from datetime import datetime
from time import time

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


TOKEN = os.environ["TELEGRAM_TOKEN"]
SAVE_RECEIVED = os.getenv("SAVE_RECEIVED", "false").lower() in {"1", "true", "yes", "on"}
storage = MemeStorage()

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)


def append_log(message, level="info"):
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


def recompress_jpeg(data: bytes, quality=20) -> bytes:
    """保留舊版對 blacklist 使用者傳低畫質圖的彩蛋。"""
    src = io.BytesIO(data)
    out = io.BytesIO()

    with Image.open(src) as img:
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.save(out, "JPEG", quality=quality, optimize=True)

    return out.getvalue()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text("歡迎使用海草先生怪物機！請輸入您想搜尋的海綿寶寶圖片關鍵字。")
    await update.message.reply_text(
        "你可以試試搜尋圖片關鍵字，或直接使用 S+編號來獲得你想要的圖片\n"
        "(例如 S2731)"
    )
    await update.message.reply_text("單獨打一個 K 可以查詢修改過的圖片")
    await update.message.reply_text("其他指令如 /random（隨機抽圖）、/description（機器人簡介）")


async def description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "海草先生怪物機，由艾璐(Airou)撰寫\n"
        "靈感來自於 Line bot 派星機（由 Dcard 尖頭亮光光製作），"
        "歡迎分享給喜愛海綿寶寶小怪圖的朋友\n"
    )
    await update.message.reply_sticker(
        sticker="CAACAgUAAxkBAAEqsrVmECin_-lQdzlDsoxuaN_PW4978wACdw0AAgZPAAFVESQ4XyOgVeI0BA"
    )
    await update.message.reply_text(
        "圖片來自於巴哈姆特，有點高清的海綿寶寶梗圖"
        "（版主 只是個普通の火神天）"
    )


async def reply_to_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    responses = [
        "好希望我有眼睛，好讓我看清楚你傳了什麼",
        "是什麼樣的刺激讓你想傳貼圖給一個機器人",
        "嗶嗶，機油好好喝",
        "吃著火鍋唱著歌，突然傳貼圖是什麼意思",
        "Service provided by Railway, beep beep",
        "艾璐教授加博士先生並沒有讓我學會看貼圖",
        "作者是隻大貓咪，叫他小貓咪他會生氣\nhttps://x.com/AirouCat620/status/1769713690570551797",
        "我不接受吃咖哩會拌的人用我做的Bot -Airou",
        "這貼圖沒什麼好看的，你可以停了",
        "我寫這句話的時候是2024/04/06 03:39AM，Just let you know",
        "Meeeeeeeow",
        "世界上已經有海草先生怪物人了",
        "有了，閣樓",
        "給你看我家的貓，他叫呆咩(玳瑁的諧音)，aka 臭臭、沙茶醬、美眉\nhttps://x.com/AirouCat620/status/1447790010996965376",
        "作者現職是隻貓\nhttps://x.com/AirouCat620/status/1758871519328166037",
        "你應該去用用看超優質的機器人，派星機！\nhttps://www.dcard.tw/f/spongebob/p/253298007",
        "瑞瑞愛吃刺刺球，而我喜歡喝機油",
        "SJF scheduling may lead to starvation",
        "40 6b 6b 1f 68 6d 72 73 60 6d 62 64 72 1f 71 64 70 74 68 71 64 72 1f 6e 74 73 72 73 60 6d 63 68 6d 66 1f 74 6d 63 64 71 72 73 60 6d 63 68 6d 66 2d",
        "香菜很好吃，推薦你也試試看",
        "小農牛奶與果汁牛乳的全家霜淇淋是最頂的",
        "Rush B!",
        "熱狗熱狗，好吃好吃好吃",
        "You're goofy goober, rock!~",
        "你這個吹泡泡的臭小鬼",
        "吹一次兩毛五，先生",
        "好棒，E小調",
        "艾璐 @SmawaKemono ，歡迎跟我主人反應機器人的問題或給出建議！",
    ]

    reply = random.choice(responses)
    await update.message.reply_text(reply)
    append_log(f"{now_text()} {update.effective_user.full_name}：(貼圖) 回應了「{reply}」")


async def reply_to_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    user = update.effective_user
    num = context.user_data.get("num", 0) + 1
    context.user_data["num"] = num

    if SAVE_RECEIVED:
        photo = await update.message.photo[-1].get_file()
        data = bytes(await photo.download_as_bytearray())
        safe_name = re.sub(r'[\\/:*?"<>|]+', "_", user.full_name)
        filename = f"{datetime.now():%Y%m%d_%H%M%S}_{safe_name}_{num}_{uuid.uuid4().hex[:8]}.jpg"
        await asyncio.to_thread(storage.save_received, filename, data, "image/jpeg")

    airou_files = await asyncio.to_thread(storage.list_airou)
    if not airou_files:
        await update.message.reply_text("我收到你的圖片了，但作者照片圖庫目前還沒上傳到 Bucket。")
        return

    random_file = random.choice(airou_files)
    data = await asyncio.to_thread(storage.get_airou, random_file)

    await update.message.reply_text(
        f"我拿這張圖跟 {user.full_name} 交換！這是一張作者的照片"
    )
    await update.message.reply_photo(
        photo=bytes_file(data, random_file),
        filename=random_file,
    )

    append_log(
        f"{now_text()} {user.full_name} 傳送了第 {num} 張圖片，回覆了 {random_file}"
    )


async def random_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    files = await asyncio.to_thread(storage.list_memes)
    if not files:
        await update.message.reply_text("目前 Bucket 裡沒有梗圖。")
        return

    random_file = random.choice(files)
    data = await asyncio.to_thread(storage.get_meme, random_file)

    await update.message.reply_photo(
        photo=bytes_file(data, random_file),
        filename=random_file,
    )
    append_log(f"{now_text()} {update.effective_user.full_name} 使用了抽，回覆了 {random_file}")


async def can_i(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    files = await asyncio.to_thread(storage.list_yn)
    if not files:
        await update.message.reply_text("目前 Bucket 裡沒有 YN 圖庫。")
        return

    target = "【SS1456】可以.jpg"
    if random.random() < 0.5 and target in files:
        file_name = target
    else:
        choices = [f for f in files if f != target]
        file_name = random.choice(choices or files)

    data = await asyncio.to_thread(storage.get_yn, file_name)
    await update.message.reply_photo(
        photo=bytes_file(data, file_name),
        filename=file_name,
    )
    append_log(
        f"{now_text()} {update.effective_user.full_name} 使用了可以不可以，回覆了 {file_name}"
    )


async def airou(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.effective_user:
        await update.message.reply_text("咪咪，恭喜你找到彩蛋")
        append_log(f"{now_text()} {update.effective_user.full_name} 使用了 /airou")


async def jack(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.effective_user:
        await update.message.reply_text("Say cheese")
        append_log(f"{now_text()} {update.effective_user.full_name} 使用了 /jack")


async def search_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    blackL = ["pipikapi"]

    try:
        if not update.message or not update.effective_user or update.message.text is None:
            return

        start_time = time()
        user = update.effective_user
        user_name = user.username or "no_username"
        keyword = update.message.text

        append_log(f"{now_text()} {user.full_name}({user_name})：{keyword}")

        matching_files = await asyncio.to_thread(storage.search_memes, keyword)

        if len(matching_files) > 300:
            await update.message.reply_text(
                f"搜尋結果超過300個，請調整關鍵字後重新搜尋({len(matching_files)})"
            )
            return

        if not matching_files:
            await update.message.reply_text(
                f'海草先生怪物機找不到符合"{keyword}"的檔案。'
            )

        elif len(matching_files) == 1:
            file_name = matching_files[0]
            data = await asyncio.to_thread(storage.get_meme, file_name)

            if user_name in blackL:
                data = await asyncio.to_thread(recompress_jpeg, data, 20)

            await update.message.reply_photo(
                photo=bytes_file(data, file_name),
                filename=file_name,
            )

            if user_name in blackL:
                await update.message.reply_text("橘色小怪物")
                append_log("回覆糊糊的圖")

        else:
            response = ""
            for file_name in matching_files:
                line = f"{file_name}\n"
                if len(response) + len(line) > 3000 and response:
                    await update.message.reply_text(response)
                    response = ""
                response += line

            if response:
                await update.message.reply_text(response)

        elapsed = time() - start_time
        if len(matching_files) > 1:
            await update.message.reply_text(
                f"共 {len(matching_files)} 筆搜尋結果，用時 {elapsed:.2f} 秒"
            )

    except Exception as e:
        append_log(f"Exception: {e}", level="error")
        if update.message:
            await update.message.reply_text(
                "海草先生怪物機出了點問題(冒黑煙)，請聯絡 @SmawaKemono 修復"
            )


async def post_init(application: Application) -> None:
    # 啟動時只抓 Bucket 檔名，不下載 3GB 圖片。
    await asyncio.to_thread(storage.refresh)
    print(f"Telegram Bucket 梗圖索引：{len(storage.list_memes())} 張")


def main() -> None:
    application = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("description", description))
    application.add_handler(CommandHandler("random", random_image))
    application.add_handler(CommandHandler("can_i", can_i))
    application.add_handler(CommandHandler("airou", airou))
    application.add_handler(CommandHandler("jack", jack))
    application.add_handler(MessageHandler(filters.Sticker.ALL, reply_to_sticker))
    application.add_handler(MessageHandler(filters.PHOTO, reply_to_photo))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, search_files)
    )

    application.run_polling()


if __name__ == "__main__":
    main()
