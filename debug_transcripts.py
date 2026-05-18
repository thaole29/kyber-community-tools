import discord
from datetime import datetime, timezone, timedelta

import config
TOKEN = config.DISCORD_TOKEN
TRANSCRIPT_CHANNEL_NAME = 'transcripts'

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f'Connected as: {client.user}')
    cutoff_time = datetime.now(timezone.utc) - timedelta(days=7) # Look back 7 days for more data
    
    for guild in client.guilds:
        transcript_ch = discord.utils.get(guild.text_channels, name=TRANSCRIPT_CHANNEL_NAME)
        if not transcript_ch:
            print(f'ERROR: Could not find channel #{TRANSCRIPT_CHANNEL_NAME} in {guild.name}')
            continue
            
        print(f'Scanning #{transcript_ch.name} in {guild.name}...')
        count = 0
        async for message in transcript_ch.history(limit=50, after=cutoff_time):
            count += 1
            print(f'[{message.created_at}] Message by {message.author}: {message.content[:50]}...')
            if message.attachments:
                for att in message.attachments:
                    print(f'  Attachment: {att.filename} ({att.url[:50]}...)')
            else:
                print('  No attachments.')
        
        if count == 0:
            print('No messages found in the last 7 days.')

    await client.close()

client.run(TOKEN)
