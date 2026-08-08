import os
import threading
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
import discord
from discord import app_commands
from discord.ext import commands, tasks

# --- Render & UptimeRobot 用のダミーWebサーバー ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_web_server():
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=run_web_server, daemon=True).start()

# --- 設定項目 ---
# 運営陣のロールID（アドミン & 幹部）
STAFF_ROLE_IDS = [
    1169308538233958461,  # アドミン
    1535691287842390128,  # 幹部
]

# チケット作成先カテゴリ名
TICKET_CATEGORY_NAME = "🎫チケット対応"

# --- チケット管理ボタンView ---
class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🔒 チケットを閉じる（削除）", 
        style=discord.ButtonStyle.danger, 
        custom_id="close_ticket_btn",
        emoji="🔒"
    )
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("このチケットチャンネルは 5 秒後に削除されます...", ephemeral=False)
        import asyncio
        await asyncio.sleep(5)
        await interaction.channel.delete(reason="ユーザーまたはスタッフによるチケット終了")

# --- パネル設置ボタンView ---
class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🎫 チケットを発行する", 
        style=discord.ButtonStyle.primary, 
        custom_id="create_ticket_btn",
        emoji="📩"
    )
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        user = interaction.user

        # チャンネル名の設定（例: ticket-username）
        channel_name = f"ticket-{user.name}".lower().replace(" ", "-")

        # 既存のチケットチャンネルが無いかチェック
        existing_channel = discord.utils.get(guild.text_channels, name=channel_name)
        if existing_channel:
            await interaction.response.send_message(f"⚠️ すでに発行済みのチケットがあります: {existing_channel.mention}", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        # 権限オーバーライドの設定
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False), # 一般メンバーからは非表示
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True), # 発行者
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True) # Bot自身
        }

        # 運営ロール（アドミン・幹部）に閲覧・送信権限を付与
        staff_mentions = []
        for role_id in STAFF_ROLE_IDS:
            role = guild.get_role(role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True)
                staff_mentions.append(role.mention)

        # カテゴリの取得または作成
        category = discord.utils.get(guild.categories, name=TICKET_CATEGORY_NAME)
        if not category:
            category = await guild.create_category(TICKET_CATEGORY_NAME)

        # プライベートチャンネルの作成
        ticket_channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            reason=f"{user.display_name} によるお問い合わせチケット発行"
        )

        # 案内メッセージの送信
        mentions_str = " ".join(staff_mentions)
        embed = discord.Embed(
            title="🎫 お問い合わせチケット",
            description=f"{user.mention} 様、お問い合わせありがとうございます！\n運営陣からの返答をお待ちください。\n\n※**2日間（48時間）メッセージの送信がない場合、自動的にこのチャンネルは削除されます。**",
            color=discord.Color.green()
        )
        await ticket_channel.send(content=f"{user.mention} {mentions_str}", embed=embed, view=TicketControlView())
        await interaction.followup.send(f"✅ チケットを作成しました: {ticket_channel.mention}", ephemeral=True)

# --- Bot本体 ---
class TicketBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.add_view(TicketPanelView())
        self.add_view(TicketControlView())
        await self.tree.sync()
        self.check_inactive_tickets.start() # 放置チケットの自動削除タスクを起動

    # --- 48時間放置チャンネルの自動削除タスク ---
    @tasks.loop(minutes=10) # 10分ごとにチェック
    async def check_inactive_tickets(self):
        now = datetime.now(timezone.utc)
        limit_time = now - timedelta(days=2) # 2日前（48時間前）

        for guild in self.guilds:
            category = discord.utils.get(guild.categories, name=TICKET_CATEGORY_NAME)
            if not category:
                continue

            for channel in category.text_channels:
                # ticket- から始まるチャンネルのみを対象にする
                if channel.name.startswith("ticket-"):
                    try:
                        # 最新のメッセージを1件取得
                        last_message = None
                        async for msg in channel.history(limit=1):
                            last_message = msg

                        # メッセージが存在する場合、最終送信日時を判定
                        if last_message:
                            if last_message.created_at < limit_time:
                                print(f"放置チケット削除: {channel.name}")
                                await channel.delete(reason="48時間以上発言がないため自動削除")
                        else:
                            # メッセージが1件もない場合、チャンネル作成日時で判定
                            if channel.created_at < limit_time:
                                await channel.delete(reason="48時間以上発言がないため自動削除")
                    except Exception as e:
                        print(f"チケット自動削除チェックエラー ({channel.name}): {e}")

    @check_inactive_tickets.before_loop
    async def before_check(self):
        await self.wait_until_ready()

bot = TicketBot()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

@bot.tree.command(name="setup_ticket", description="お問い合わせチケットパネルを設置します（管理者専用）")
@app_commands.checks.has_permissions(administrator=True)
async def setup_ticket(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    embed = discord.Embed(
        title="📩 お問い合わせ・サポート",
        description="ご質問や運営へのお問い合わせがある場合は、下のボタンを押してください。\nあなたと運営陣だけが閲覧できる専用チャンネルが作成されます。",
        color=discord.Color.blue()
    )
    await interaction.channel.send(embed=embed, view=TicketPanelView())
    await interaction.followup.send("チケット設置パネルを作成したよ！", ephemeral=True)

TOKEN = os.getenv("DISCORD_TOKEN")
if TOKEN:
    bot.run(TOKEN)
else:
    print("エラー: DISCORD_TOKEN が設定されていません。")
