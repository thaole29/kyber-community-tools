import discord
import aiohttp
import asyncio

import config
TOKEN = config.DISCORD_TOKEN
CH_NAME = 'archieved'

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    ch = discord.utils.get(client.get_guild(608934314960224276).text_channels, name=CH_NAME)
    async with aiohttp.ClientSession() as session:
        async for msg in ch.history(limit=20):
            if msg.attachments:
                att = msg.attachments[0]
                if att.filename.endswith('.html'):
                    print(f'Downloading {att.filename}...')
                    async with session.get(att.url) as r:
                        content = await r.text(encoding='utf-8', errors='replace')
                        print('--- HTML START ---')
                        print(content[:2000])
                        print('--- HTML END ---')
                        break
    await client.close()

client.run(TOKEN)
