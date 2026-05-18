import discord

import config
TOKEN = config.DISCORD_TOKEN
intents = discord.Intents.default()
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f'Connected as: {client.user}')
    for guild in client.guilds:
        print(f'\nServer: {guild.name}')
        print('All text channels:')
        for ch in guild.text_channels:
            print(f'  - #{ch.name}')
    await client.close()

client.run(TOKEN)
