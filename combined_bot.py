import asyncio
import io
import logging
import os
import random
import signal
import warnings
from datetime import datetime
from time import time

import discord
from discord import Intents, File
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

import sys

# ============================================================
# 0. 必要的 Railway 改動
# ============================================================

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]

# Discord + Telegram 只共用「儲存連線」。
# 兩邊原本自己的指令、文案、壓縮參數、log 檔名都各自保留。
storage = MemeStorage()


# ============================================================
# 1. Logging
# ============================================================

warnings.filterwarnings("ignore", category=UserWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    stream=sys.stdout,
    force=True,
)

logger = logging.getLogger(__name__)

logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("discord.client").setLevel(logging.ERROR)
logging.getLogger("discord.gateway").setLevel(logging.ERROR)


async def append_tg_log(message, level="info"):
    """
    保留原本 Telegram append_log：
      1. console
      2. searchTG_output.log

    差別只有 .log 改存在 Bucket，避免 Railway redeploy 消失。
    """
    message = str(message)

    if level == "error":
        logger.error(message)
    else:
        logger.info(message)

    try:
        await asyncio.to_thread(
            storage.append_text_log,
            "searchTG_output.log",
            message,
        )
    except Exception as e:
        # log 上傳失敗不能害 Bot 主功能跟著死
        logger.error(f"Bucket TG log error: {e}")


async def append_dc_log(message, level="info"):
    """
    保留原本 Discord append_log：
      1. console
      2. searchDC_output.log

    差別只有 .log 改存在 Bucket。
    """
    message = str(message)

    if level == "error":
        logger.error(message)
    else:
        logger.info(message)

    try:
        await asyncio.to_thread(
            storage.append_text_log,
            "searchDC_output.log",
            message,
        )
    except Exception as e:
        logger.error(f"Bucket DC log error: {e}")


def get_clean_time():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# 2. 圖片壓縮：分開保留兩個版本原本參數
# ============================================================

def compress_discord_bytes(
    image_data: bytes,
    quality=70,
    max_length=1500,
) -> bytes:
    """
    原 Discord：
      quality=70
      max_length=1500
      RGBA/P -> RGB
      JPEG
    """
    output = io.BytesIO()

    with Image.open(io.BytesIO(image_data)) as img:
        exif = img._getexif()

        if exif:
            orientation = exif.get(0x0112, 1)

            if orientation in [3, 6, 8]:
                rotation = {3: 180, 6: 270, 8: 90}[orientation]
                img = img.rotate(rotation, expand=True)

        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        img.thumbnail(
            (max_length, max_length),
            Image.Resampling.LANCZOS,
        )

        img.save(
            output,
            "JPEG",
            quality=quality,
        )

    return output.getvalue()


def compress_telegram_bytes(
    image_data: bytes,
    quality=70,
    max_length=1280,
) -> bytes:
    """
    原 Telegram：
      quality=50
      max_length=1280
      EXIF rotation
    """
    output = io.BytesIO()

    with Image.open(io.BytesIO(image_data)) as img:
        exif = img._getexif()

        if exif:
            orientation = exif.get(0x0112, 1)

            if orientation in [3, 6, 8]:
                rotation = {3: 180, 6: 270, 8: 90}[orientation]
                img = img.rotate(rotation, expand=True)

        img.thumbnail(
            (max_length, max_length),
            Image.Resampling.LANCZOS,
        )

        # Bucket 主圖已是 JPEG，因此正常會是 RGB。
        # 若意外收到有 alpha 的格式，只做 Railway 必要相容處理，
        # 不改 quality / 尺寸邏輯。
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")

        img.save(
            output,
            "JPEG",
            quality=quality,
        )

    return output.getvalue()


def tg_photo_file(data: bytes, filename="photo.jpg"):
    bio = io.BytesIO(data)
    bio.name = filename
    bio.seek(0)
    return bio


# ============================================================
# 3. Discord：最大化保留使用者原始版本
# ============================================================

MAX_MESSAGE_LENGTH = 1500

intents = Intents.default()
intents.messages = True
intents.message_content = True

discord_bot = commands.Bot(
    command_prefix="/",
    intents=intents,
)


@discord_bot.tree.command(name="start", description="開始使用")
async def dc_start(interaction: discord.Interaction):
    await interaction.response.send_message(
        '歡迎使用海草先生怪物機！請輸入您想搜尋的海綿寶寶圖片關鍵字。\n'
        '你可以試試搜尋圖片關鍵字，或直接使用S+編號來獲得你想要的圖片\n(例如 S2731)\n'
        '單獨打一個K可以查詢修改過的圖片\n'
        '其他指令如 /random(隨機抽圖)、以及 /description(機器人簡介)',
        ephemeral=True
    )

    await append_dc_log(
        f"{get_clean_time()} "
        f"{interaction.user.display_name} 使用了 /start"
    )


@discord_bot.tree.command(name="description", description="簡介")
async def dc_description(interaction: discord.Interaction):
    await append_dc_log(
        f"{get_clean_time()} "
        f"{interaction.user.display_name} 使用了 /description"
    )

    await interaction.response.send_message(
        '海草先生怪物機，由**艾璐(Airou)**撰寫\n'
        '靈感來自於Line bot派星機(由Dcard 尖頭亮光光製作)，歡迎分享給喜愛海綿寶寶小怪圖的朋友\n\n'
        '圖片來自於巴哈姆特，有點高清的海綿寶寶梗圖(版主 只是個普通の火神天)',
        ephemeral=True
    )


@discord_bot.tree.command(name="random", description="隨機抽圖")
async def dc_random(interaction: discord.Interaction):
    await interaction.response.defer()

    files = await asyncio.to_thread(storage.list_memes)
    random_file = random.choice(files)

    original = await asyncio.to_thread(
        storage.get_meme,
        random_file,
    )

    compressed = await asyncio.to_thread(
        compress_discord_bytes,
        original,
    )

    await append_dc_log(
        f"{get_clean_time()} "
        f"{interaction.user.display_name} 使用了抽，回覆了 {random_file}"
    )

    await interaction.followup.send(
        f"**{interaction.user.display_name}** 使用隨機抽圖抽到了__{random_file}__",
        file=File(
            io.BytesIO(compressed),
            filename="compressed_rnd.jpg",
        ),
    )


@discord_bot.tree.command(name="can_i", description="我可不可以..?")
async def dc_can_i(interaction: discord.Interaction):
    await interaction.response.defer()

    yn_files = await asyncio.to_thread(storage.list_yn)

    rand = random.random()
    target_file = "【SS1456】可以.jpg"

    if rand < 0.5 and target_file in yn_files:
        file_name = target_file
    else:
        temp_files = list(yn_files)

        if target_file in temp_files:
            temp_files.remove(target_file)

        file_name = random.choice(temp_files)

    original = await asyncio.to_thread(
        storage.get_yn,
        file_name,
    )

    compressed = await asyncio.to_thread(
        compress_discord_bytes,
        original,
    )

    await append_dc_log(
        f"{get_clean_time()} "
        f"{interaction.user.display_name} 使用了可以不可以，回覆了 {file_name}"
    )

    await interaction.followup.send(
        f"**{interaction.user.display_name}** 使用我可不可以..抽到了__{file_name}__",
        file=File(
            io.BytesIO(compressed),
            filename="compressed_yn.jpg",
        ),
    )


# 原 Discord 程式中這兩個函數「沒有 @bot.tree.command decorator」。
# 所以這裡刻意不擅自把它們註冊成新的 slash command。
async def dc_airou(interaction: discord.Interaction):
    await interaction.response.send_message("咪咪，恭喜你找到彩蛋")
    await append_dc_log(
        f"{get_clean_time()} "
        f"{interaction.user.display_name} 使用了 /airou"
    )


async def dc_jack(interaction: discord.Interaction):
    await interaction.response.send_message("Say cheese")
    await append_dc_log(
        f"{get_clean_time()} "
        f"{interaction.user.display_name} 使用了 /jack"
    )


@discord_bot.tree.command(name="s", description="搜尋海寶圖")
async def dc_search(interaction: discord.Interaction, keyword: str):
    try:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        await append_dc_log(
            f"{now} {interaction.user.display_name}：{keyword}"
        )

        valid_files = await asyncio.to_thread(storage.list_memes)

        matching_files = [
            f for f in valid_files
            if keyword.lower() in f.lower()
        ]

        if not matching_files:
            await interaction.response.send_message(
                f"海草先生怪物機找不到符合'{keyword}'的檔案。",
                ephemeral=True
            )

        elif len(matching_files) == 1:
            await interaction.response.defer()

            original = await asyncio.to_thread(
                storage.get_meme,
                matching_files[0],
            )

            compressed = await asyncio.to_thread(
                compress_discord_bytes,
                original,
            )

            await interaction.followup.send(
                f"**{interaction.user.display_name}** 傳送了：__{matching_files[0]}__",
                file=File(
                    io.BytesIO(compressed),
                    filename="compressed.jpg",
                )
            )

        else:
            response = ""
            returned = 0

            for file in matching_files:
                response += "{}\n".format(file)

                if len(response) > MAX_MESSAGE_LENGTH:
                    if returned == 0:
                        await interaction.response.send_message(
                            response,
                            ephemeral=True
                        )
                        returned = 1
                    else:
                        await interaction.followup.send(
                            response,
                            ephemeral=True
                        )

                    response = ""

            if response:
                if returned == 0:
                    await interaction.response.send_message(
                        response,
                        ephemeral=True
                    )
                else:
                    await interaction.followup.send(
                        response,
                        ephemeral=True
                    )

    except Exception as e:
        await append_dc_log(
            f"Exception: {e}",
            level="error",
        )

        if not interaction.response.is_done():
            await interaction.response.send_message(
                '海草先生怪物機好像出了點問題，請聯繫**艾璐**貓咪進行修理',
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                '海草先生怪物機好像出了點問題，請聯繫**艾璐**貓咪進行修理',
                ephemeral=True
            )


@discord_bot.event
async def on_ready():
    try:
        slash = await discord_bot.tree.sync()

        print(f"目前登入身份 --> {discord_bot.user}")
        print(f"載入 {len(slash)} 個斜線指令")

    except Exception as e:
        print(f"同步指令失敗: {e}")


# ============================================================
# 4. Telegram：最大化保留使用者舊版本
#    只把舊 Updater API 換成新版 async API，及本機檔案換 Bucket
# ============================================================

async def tg_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await update.message.reply_text(
        '歡迎使用海草先生怪物機！請輸入您想搜尋的海綿寶寶圖片關鍵字。'
    )
    await update.message.reply_text(
        '你可以試試搜尋圖片關鍵字，或直接使用S+編號來獲得你想要的圖片\n(例如S2731)'
    )
    await update.message.reply_text(
        '單獨打一個K可以查詢修改過的圖片'
    )
    await update.message.reply_text(
        '其他指令如/random(隨機抽圖)、以及/descrption(機器人簡介)'
    )


async def tg_description(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await update.message.reply_text(
        '海草先生怪物機，由艾璐(Airou)撰寫\n'
        '靈感來自於Line bot派星機(由Dcard 尖頭亮光光製作)，歡迎分享給喜愛海綿寶寶小怪圖的朋友\n'
    )

    await update.message.reply_text(
        '由於目前架設在免費平台上，因此如果有斷線、不穩還請見諒\n'
    )

    await update.message.reply_sticker(
        sticker='CAACAgUAAxkBAAEqsrVmECin_-lQdzlDsoxuaN_PW4978wACdw0AAgZPAAFVESQ4XyOgVeI0BA'
    )

    await update.message.reply_text(
        '圖片來自於巴哈姆特，有點高清的海綿寶寶梗圖(版主 只是個普通の火神天)'
    )


async def tg_reply_to_sticker(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    responses = [
        '好希望我有眼睛，好讓我看清楚你傳了什麼',
        '是什麼樣的刺激讓你想傳貼圖給一個機器人',
        '嗶嗶，機油好好喝',
        '吃著火鍋唱著歌，突然傳貼圖是什麼意思',
        'Service provided by PythonAnyWhere, beep beep',
        '艾璐教授加博士先生並沒有讓我學會看貼圖',
        '作者是隻大貓咪，叫他小貓咪他會生氣\nhttps://x.com/AirouCat620/status/1769713690570551797',
        '我不接受吃咖哩會拌的人用我做的Bot -Airou',
        '這貼圖沒什麼好看的，你可以停了',
        '我寫這句話的時候是2024/04/06 03:39AM，Just let you know',
        'Meeeeeeeow',
        '世界上已經有海草先生怪物人了',
        '有了，閣樓',
        '給你看我家的貓，他叫呆咩(玳瑁的諧音)，aka 臭臭、沙茶醬、美眉\nhttps://x.com/AirouCat620/status/1447790010996965376',
        '作者現職是隻貓\nhttps://x.com/AirouCat620/status/1758871519328166037',
        '你應該去用用看超優質的機器人，派星機！\nhttps://www.dcard.tw/f/spongebob/p/253298007',
        '瑞瑞愛吃刺刺球，而我喜歡喝機油',
        'SJF scheduling may lead to starvation',
        '40 6b 6b 1f 68 6d 72 73 60 6d 62 64 72 1f 71 64 70 74 68 71 64 72 1f 6e 74 73 72 73 60 6d 63 68 6d 66 1f 74 6d 63 64 71 72 73 60 6d 63 68 6d 66 2d',
        '香菜很好吃，推薦你也試試看',
        '小農牛奶與果汁牛乳的全家霜淇淋是最頂的',
        'Rush B!',
        '熱狗熱狗，好吃好吃好吃',
        'You\'re goofy goober, rock!~',
        '你這個吹泡泡的臭小鬼',
        '吹一次兩毛五，先生',
        '好棒，E小調',
        '艾璐 @SmawaKemono ，歡迎跟我主人反應機器人的問題或給出建議！',
    ]

    reply = random.choice(responses)

    await update.message.reply_text(reply)

    user = update.message.from_user
    user_full_name = user.full_name
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S') + " "

    await append_tg_log(
        now
        + user_full_name
        + "："
        + "(貼圖)"
        + "回應了\""
        + reply
        + "\""
    )


async def tg_reply_to_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    # 原邏輯：收到照片 -> PhotoReceived -> 隨機 Airou -> 壓縮 -> 交換
    user = update.message.from_user
    user_full_name = user.full_name

    num = context.user_data.get('num', 0) + 1
    context.user_data['num'] = num

    # 下載 Telegram 收到的圖片
    photo = await update.message.photo[-1].get_file()
    received_data = bytes(await photo.download_as_bytearray())

    # 原本檔名就是 {user_full_name}{num}.jpg
    received_filename = f"{user_full_name}{num}.jpg"

    # 改成持久存 Railway Bucket / PhotoReceived/
    await asyncio.to_thread(
        storage.save_received,
        received_filename,
        received_data,
    )

    # 原本 while True 排除 compressed.jpg
    while True:
        airou_files = await asyncio.to_thread(storage.list_airou)
        random_file = random.choice(airou_files)

        if random_file != "compressed.jpg":
            break

    original = await asyncio.to_thread(
        storage.get_airou,
        random_file,
    )

    compressed = await asyncio.to_thread(
        compress_telegram_bytes,
        original,
    )

    # 完全保留原文，不自行新增「圖庫沒上傳」之類的訊息
    await update.message.reply_text(
        "我拿這張圖跟"
        + user_full_name
        + "交換！這是一張作者的照片"
    )

    await update.message.reply_photo(
        photo=tg_photo_file(
            compressed,
            "compressed.jpg",
        )
    )

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S') + " "

    # 原本 log 最後記 photo_path；
    # Railway 改記 Bucket 的 Airou key，語義相同。
    await append_tg_log(
        now
        + user_full_name
        + "傳送了第"
        + str(num)
        + "張圖片，回覆了"
        + f"{storage.airou_prefix}/{random_file}"
    )


async def tg_random_image(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    files = await asyncio.to_thread(storage.list_memes)
    random_file = random.choice(files)

    original = await asyncio.to_thread(
        storage.get_meme,
        random_file,
    )

    compressed = await asyncio.to_thread(
        compress_telegram_bytes,
        original,
    )

    await update.message.reply_photo(
        photo=tg_photo_file(
            compressed,
            "compressed.jpg",
        )
    )

    user = update.message.from_user
    user_full_name = user.full_name
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S') + " "

    await append_tg_log(
        now
        + user_full_name
        + "使用了抽，回覆了"
        + random_file
    )


async def tg_can_i(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.message.from_user
    user_full_name = user.full_name

    files = await asyncio.to_thread(storage.list_yn)

    rand = random.random()

    if rand < 0.5:
        file_name = "【SS1456】可以.jpg"
    else:
        files.remove("【SS1456】可以.jpg")
        file_name = random.choice(files)

    # 原碼這裡 photo_path 少接 /YN，屬於舊程式路徑 bug。
    # Railway storage 必須從實際 YN/ 取檔，否則功能無法成立。
    original = await asyncio.to_thread(
        storage.get_yn,
        file_name,
    )

    compressed = await asyncio.to_thread(
        compress_telegram_bytes,
        original,
    )

    await update.message.reply_photo(
        photo=tg_photo_file(
            compressed,
            "compressed.jpg",
        )
    )

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S') + " "

    await append_tg_log(
        now
        + user_full_name
        + "使用了可以不可以，回覆了"
        + file_name
    )


async def tg_airou(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.message.from_user
    user_full_name = user.full_name

    await update.message.reply_text(
        "咪咪，恭喜你找到彩蛋"
    )

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S') + " "

    await append_tg_log(
        now
        + user_full_name
        + "使用了/Airou"
    )


async def tg_jack(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.message.from_user
    user_full_name = user.full_name

    await update.message.reply_text(
        "Say cheese"
    )

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S') + " "

    await append_tg_log(
        now
        + user_full_name
        + "使用了/Jack"
    )


async def tg_search_files(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    blackL = ["pipikapi"]

    try:
        start_time = time()

        user = update.message.from_user
        user_name = user.username
        user_full_name = user.full_name
        keyword = update.message.text

        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S') + " "

        await append_tg_log(
            now
            + user_full_name
            + "("
            + str(user_name)
            + ")"
            + "："
            + keyword
        )

        notQualified = False

        files = await asyncio.to_thread(storage.list_memes)

        matching_files = [
            f for f in files
            if keyword.lower() in f.lower()
            and f.endswith('.jpg')
        ]

        if len(matching_files) > 300:
            await update.message.reply_text(
                f'搜尋結果超過300個，請調整關鍵字後重新搜尋({len(matching_files)})'
            )
            return

        if not matching_files:
            if not notQualified:
                await update.message.reply_text(
                    '海草先生怪物機找不到符合\"'
                    + keyword
                    + '\"的檔案。'
                )

        elif len(matching_files) == 1:
            original = await asyncio.to_thread(
                storage.get_meme,
                matching_files[0],
            )

            if user_name in blackL:
                compressed = await asyncio.to_thread(
                    compress_telegram_bytes,
                    original,
                    20,
                    1280,
                )
            else:
                compressed = await asyncio.to_thread(
                    compress_telegram_bytes,
                    original,
                )

            await update.message.reply_photo(
                photo=tg_photo_file(
                    compressed,
                    "compressed.jpg",
                )
            )

            if user_name in blackL:
                await update.message.reply_text(
                    '橘色小怪物'
                )
                await append_tg_log(
                    "回覆糊糊的圖"
                )

        else:
            response = ""

            for file in matching_files:
                response += "{}\n".format(file)

                if len(response) > 3000:
                    await update.message.reply_text(response)
                    response = ""

            if response:
                await update.message.reply_text(response)

        end_time = time()
        elapsed_time = end_time - start_time

        if len(matching_files) > 1:
            await update.message.reply_text(
                f"共 {len(matching_files)} 筆搜尋結果，用時 {elapsed_time:.2f} 秒"
            )

    except Exception as e:
        await append_tg_log(e)

        await update.message.reply_text(
            '海草先生怪物機出了點問題(冒黑煙)，請聯絡 @SmawaKemono修復'
        )


def build_telegram_app():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(
        CommandHandler("start", tg_start)
    )
    app.add_handler(
        CommandHandler("description", tg_description)
    )
    app.add_handler(
        CommandHandler("random", tg_random_image)
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
            tg_search_files,
        )
    )

    return app


# ============================================================
# 5. 合併執行：這是唯一真正需要新增的主要邏輯
# ============================================================

async def start_telegram(app):
    await app.initialize()

    if app.updater is None:
        raise RuntimeError("Telegram updater 不存在")

    await app.updater.start_polling()
    await app.start()


async def stop_telegram(app):
    if app.updater is not None and app.updater.running:
        await app.updater.stop()

    if app.running:
        await app.stop()

    await app.shutdown()


async def main():
    tg_app = build_telegram_app()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(
                sig,
                stop_event.set,
            )
        except NotImplementedError:
            pass

    await start_telegram(tg_app)

    discord_task = asyncio.create_task(
        discord_bot.start(DISCORD_TOKEN)
    )

    stop_task = asyncio.create_task(
        stop_event.wait()
    )

    try:
        done, _ = await asyncio.wait(
            {discord_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

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

        await stop_telegram(tg_app)


if __name__ == '__main__':
    asyncio.run(main())
