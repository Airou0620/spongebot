import asyncio
import io
import logging
import os
import random
import warnings
from datetime import datetime

import discord
from discord import File, Intents
from discord.ext import commands

from storage import MemeStorage


warnings.filterwarnings("ignore", category=UserWarning)
MAX_MESSAGE_LENGTH = 1500

TOKEN = os.environ["DISCORD_TOKEN"]
storage = MemeStorage()

logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
logger = logging.getLogger(__name__)
logging.getLogger("discord.client").setLevel(logging.ERROR)
logging.getLogger("discord.gateway").setLevel(logging.ERROR)


def append_log(message, level="info"):
    if level == "error":
        logger.error(str(message))
    else:
        logger.info(str(message))


def get_clean_time():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def discord_file(data: bytes, filename: str) -> File:
    bio = io.BytesIO(data)
    return File(bio, filename=filename)


intents = Intents.default()
intents.messages = True
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)


@bot.tree.command(name="start", description="開始使用")
async def start(interaction: discord.Interaction):
    await interaction.response.send_message(
        "歡迎使用海草先生怪物機！請輸入您想搜尋的海綿寶寶圖片關鍵字。\n"
        "你可以試試搜尋圖片關鍵字，或直接使用 S+編號來獲得你想要的圖片\n"
        "(例如 S2731)\n"
        "單獨打一個 K 可以查詢修改過的圖片\n"
        "其他指令如 /random（隨機抽圖）、/description（機器人簡介）",
        ephemeral=True,
    )
    append_log(f"{get_clean_time()} {interaction.user.display_name} 使用了 /start")


@bot.tree.command(name="description", description="簡介")
async def description(interaction: discord.Interaction):
    append_log(f"{get_clean_time()} {interaction.user.display_name} 使用了 /description")
    await interaction.response.send_message(
        "海草先生怪物機，由**艾璐(Airou)**撰寫\n"
        "靈感來自於 Line bot 派星機（由 Dcard 尖頭亮光光製作），"
        "歡迎分享給喜愛海綿寶寶小怪圖的朋友\n\n"
        "圖片來自於巴哈姆特，有點高清的海綿寶寶梗圖"
        "（版主 只是個普通の火神天）",
        ephemeral=True,
    )


@bot.tree.command(name="random", description="隨機抽圖")
async def random_command(interaction: discord.Interaction):
    await interaction.response.defer()

    files = await asyncio.to_thread(storage.list_memes)
    if not files:
        await interaction.followup.send("目前 Bucket 裡沒有梗圖。", ephemeral=True)
        return

    random_file = random.choice(files)
    data = await asyncio.to_thread(storage.get_meme, random_file)

    append_log(
        f"{get_clean_time()} {interaction.user.display_name} 使用了抽，回覆了 {random_file}"
    )
    await interaction.followup.send(
        f"**{interaction.user.display_name}** 使用隨機抽圖抽到了__{random_file}__",
        file=discord_file(data, random_file),
    )


@bot.tree.command(name="can_i", description="我可不可以..?")
async def can_i(interaction: discord.Interaction):
    await interaction.response.defer()

    yn_files = await asyncio.to_thread(storage.list_yn)
    if not yn_files:
        await interaction.followup.send("目前 Bucket 裡沒有 YN 圖庫。", ephemeral=True)
        return

    target_file = "【SS1456】可以.jpg"

    if random.random() < 0.5 and target_file in yn_files:
        file_name = target_file
    else:
        temp_files = [f for f in yn_files if f != target_file]
        file_name = random.choice(temp_files or yn_files)

    data = await asyncio.to_thread(storage.get_yn, file_name)

    append_log(
        f"{get_clean_time()} {interaction.user.display_name} 使用了可以不可以，回覆了 {file_name}"
    )
    await interaction.followup.send(
        f"**{interaction.user.display_name}** 使用我可不可以..抽到了__{file_name}__",
        file=discord_file(data, file_name),
    )


@bot.tree.command(name="airou", description="彩蛋")
async def airou(interaction: discord.Interaction):
    await interaction.response.send_message("咪咪，恭喜你找到彩蛋")
    append_log(f"{get_clean_time()} {interaction.user.display_name} 使用了 /airou")


@bot.tree.command(name="jack", description="Say cheese")
async def jack(interaction: discord.Interaction):
    await interaction.response.send_message("Say cheese")
    append_log(f"{get_clean_time()} {interaction.user.display_name} 使用了 /jack")


@bot.tree.command(name="s", description="搜尋海寶圖")
async def s(interaction: discord.Interaction, keyword: str):
    try:
        append_log(f"{get_clean_time()} {interaction.user.display_name}：{keyword}")

        matching_files = await asyncio.to_thread(storage.search_memes, keyword)

        if not matching_files:
            await interaction.response.send_message(
                f"海草先生怪物機找不到符合「{keyword}」的檔案。",
                ephemeral=True,
            )
            return

        if len(matching_files) == 1:
            await interaction.response.defer()

            file_name = matching_files[0]
            data = await asyncio.to_thread(storage.get_meme, file_name)

            await interaction.followup.send(
                f"**{interaction.user.display_name}** 傳送了：__{file_name}__",
                file=discord_file(data, file_name),
            )
            return

        response = ""
        returned = False

        for file_name in matching_files:
            line = f"{file_name}\n"

            if len(response) + len(line) > MAX_MESSAGE_LENGTH and response:
                if not returned:
                    await interaction.response.send_message(response, ephemeral=True)
                    returned = True
                else:
                    await interaction.followup.send(response, ephemeral=True)
                response = ""

            response += line

        if response:
            if not returned:
                await interaction.response.send_message(response, ephemeral=True)
            else:
                await interaction.followup.send(response, ephemeral=True)

    except Exception as e:
        append_log(f"Exception: {e}", level="error")
        msg = "海草先生怪物機好像出了點問題，請聯繫**艾璐**貓咪進行修理"
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)


@bot.event
async def on_ready():
    try:
        # 先抓一次 Bucket 檔名索引；不是下載圖片。
        await asyncio.to_thread(storage.refresh)
        slash = await bot.tree.sync()
        print(f"目前登入身份 --> {bot.user}")
        print(f"載入 {len(slash)} 個斜線指令")
        print(f"Bucket 梗圖索引：{len(storage.list_memes())} 張")
    except Exception as e:
        print(f"啟動或同步指令失敗: {e}")


if __name__ == "__main__":
    bot.run(TOKEN)
