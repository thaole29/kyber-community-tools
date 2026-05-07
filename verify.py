import discord
import asyncio

TOKEN = '<REDACTED_DISCORD_TOKEN>' 

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f'Logged in as {client.user} - Verifying server access...')
    
    if len(client.guilds) == 0:
        print("ERROR: Bot is not invited to any servers yet!")
        await client.close()
        return
        
    for guild in client.guilds:
        print(f"\n--- Server: {guild.name} ({guild.id}) ---")
        
        # Check for roles
        roles = [r.name for r in guild.roles]
        if 'Community Admin' in roles:
            print("SUCCESS: Found 'Community Admin' role!")
        else:
            print(f"WARNING: No 'Community Admin' role found! Roles found: {', '.join(roles[:10])}...")
            
        # Check for channels
        ticket_channels = [c.name for c in guild.text_channels if c.name.startswith('ticket-')]
        if len(ticket_channels) > 0:
            print(f"SUCCESS: Found {len(ticket_channels)} existing ticket channel(s):")
            for tc in ticket_channels[:5]:
                print(f"  - #{tc}")
        else:
            print(f"NOTICE: Found 0 channels starting with 'ticket-'. Existing channels: {', '.join([c.name for c in guild.text_channels][:10])}...")
            
    print("\nVerification complete. Shutting down verifier.")
    await client.close()

client.run(TOKEN)
