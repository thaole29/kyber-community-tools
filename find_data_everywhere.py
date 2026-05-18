import discord
from datetime import datetime, timezone, timedelta

import config
TOKEN = config.DISCORD_TOKEN
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f'Connected as: {client.user}')
    cutoff_time = datetime.now(timezone.utc) - timedelta(days=2)
    
    for guild in client.guilds:
        print(f'\nScanning {guild.name}...')
        for ch in guild.text_channels:
            try:
                # Only check channels that might have logs or transcripts
                if any(kw in ch.name.lower() for kw in ['transcript', 'log', 'archieve', 'history', 'closed']):
                    print(f'Checking #{ch.name}...')
                    async for message in ch.history(limit=5, after=cutoff_time):
                        if message.attachments:
                            print(f'  [FOUND] Message with attachments in #{ch.name} by {message.author}')
                            for att in message.attachments:
                                print(f'    File: {att.filename}')
            except discord.Forbidden:
                pass
            except Exception as e:
                print(f'  Error checking #{ch.name}: {e}')

    print('\nScan complete.')
    await client.close()

client.run(TOKEN)
