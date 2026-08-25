import os
import json
from datetime import datetime
import discord
from discord.ext import tasks
from dotenv import load_dotenv
from supabase import create_client, Client
from google import genai
from google.genai import types
from flask import Flask
from threading import Thread

app = Flask(__name__)

@app.route('/')
def home():
    return "✅ บอททำงานปกติ 24 ชม. แล้วจ้า!"

def run_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_server)
    t.start()

# ==========================================
# 1. SETUP & CONFIGURATION
# ==========================================
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ALLOWED_USER_ID = int(os.getenv("DISCORD_ALLOWED_USER_ID", 0))

SALARY_AMOUNT = float(os.getenv("SALARY_AMOUNT", 30000))
SALARY_PAY_DAY = int(os.getenv("SALARY_PAY_DAY", 25))

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
ai_client = genai.Client(api_key=GEMINI_API_KEY)


# ==========================================
# 2. DATABASE LAYER
# ==========================================
def insert_transaction(msg_id: str, raw_text: str, data: dict):
    record = {
        "discord_msg_id": msg_id,
        "raw_text": raw_text,
        "item_name": data.get("item_name", "ไม่ทราบชื่อ"),
        "quantity": data.get("quantity", 1),
        "total_price": data.get("total_price", 0),
        "transaction_type": data.get("transaction_type", "รายจ่าย"),
        "category": data.get("category", "ค่าสินค้า")
    }
    supabase.table("transactions").insert(record).execute()

def update_transaction(msg_id: str, raw_text: str, data: dict):
    record = {
        "raw_text": raw_text,
        "item_name": data.get("item_name"),
        "quantity": data.get("quantity"),
        "total_price": data.get("total_price"),
        "transaction_type": data.get("transaction_type"),
        "category": data.get("category"),
        "updated_at": "now()" 
    }
    supabase.table("transactions").update(record).eq("discord_msg_id", msg_id).execute()

def delete_transaction(msg_id: str):
    supabase.table("transactions").delete().eq("discord_msg_id", msg_id).execute()

def get_current_month_data():
    today = datetime.now()
    start_date = f"{today.year}-{today.month:02d}-01"
    response = supabase.table("transactions").select("*").gte("created_at", start_date).execute()
    return response.data


# ==========================================
# 3. AI ENGINE
# ==========================================

# Action: วิเคราะห์ข้อความการใช้จ่าย -> แปลงเป็น JSON โครงสร้างรายการเงิน
async def parse_financial_text(text: str) -> dict:
    prompt = f"""วิเคราะห์: "{text}"
    ตอบ JSON เท่านั้น:
    {{
        "transactions": [
            {{
                "item_name": "ชื่อรายการสั้นๆ",
                "quantity": 1,
                "total_price": 0,
                "transaction_type": "รายรับ" หรือ "รายจ่าย",
                "category": "ค่าอาหาร" | "ค่าบริการ" | "ค่าสินค้า" | "รายได้" | "เงินเดือน"
            }}
        ]
    }}"""
    
    response = await ai_client.aio.models.generate_content(
        model='gemini-3.6-flash',
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0)
    )
    
    raw_text = response.text.strip()
    start_idx, end_idx = raw_text.find('{'), raw_text.rfind('}') + 1
    if start_idx != -1 and end_idx != 0:
        return json.loads(raw_text[start_idx:end_idx])
    raise ValueError("Invalid JSON")

# Action: วิเคราะห์คำถามเกี่ยวกับข้อมูลการเงินเดือนนี้ -> ตอบสรุปสั้นๆ พร้อม Emoji หมวดหมู่
async def answer_smart_query(question: str, db_data: list) -> str:
    prompt = f"""
    คำถาม: "{question}"
    ข้อมูล: {json.dumps(db_data, ensure_ascii=False)}
    ตอบให้สั้นที่สุด ตรงประเด็น ห้ามเวิ่นเว้อ
    ใช้ Emoji 1 ตัว เฉพาะคู่กับชื่อ "หมวดหมู่" เท่านั้น (เช่น 🍽️ ค่าอาหาร, 🚌 ค่าบริการ) ห้ามใส่ Emoji ที่อื่นเด็ดขาด
    """
    response = await ai_client.aio.models.generate_content(
        model='gemini-3.6-flash',
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0.0)
    )
    return response.text


# ==========================================
# 4. DISCORD UI
# ==========================================
class ApproveView(discord.ui.View):
    def __init__(self, msg_id, raw_text, parsed_data, mode="insert"):
        super().__init__(timeout=120)
        self.msg_id = str(msg_id)
        self.raw_text = raw_text
        self.parsed_data = parsed_data
        self.mode = mode

    # Action: ผู้ใช้กดปุ่ม ✅ -> บันทึกหรืออัปเดตข้อมูลลง Supabase แล้วแก้ไขข้อความปุ่มออก
    @discord.ui.button(label="✅", style=discord.ButtonStyle.success)
    async def approve_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != ALLOWED_USER_ID: return
        
        for idx, item in enumerate(self.parsed_data["transactions"]):
            sub_id = f"{self.msg_id}_{idx}" if len(self.parsed_data["transactions"]) > 1 else self.msg_id
            if self.mode == "insert":
                insert_transaction(sub_id, self.raw_text, item)
            elif self.mode == "update":
                update_transaction(sub_id, self.raw_text, item)

        await interaction.response.edit_message(content="✅ บันทึกแล้ว", view=None)

    # Action: ผู้ใช้กดปุ่ม ❌ -> ยกเลิกรายการและลบปุ่มยืนยันออก
    @discord.ui.button(label="❌", style=discord.ButtonStyle.danger)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != ALLOWED_USER_ID: return
        await interaction.response.edit_message(content="❌ ยกเลิก", view=None)

class QuickActionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    # Action: ผู้ใช้กดปุ่มด่วนกาแฟ -> บันทึก 50 บาทลง Supabase ทันที ไม่ผ่าน AI
    @discord.ui.button(label="☕ กาแฟ 50฿", style=discord.ButtonStyle.primary)
    async def coffee_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != ALLOWED_USER_ID: return
        msg_id = f"qa_{interaction.message.id}_{datetime.now().timestamp()}"
        data = {"item_name": "กาแฟ", "quantity": 1, "total_price": 50, "transaction_type": "รายจ่าย", "category": "ค่าอาหาร"}
        insert_transaction(msg_id, "Quick-Coffee", data)
        await interaction.response.send_message("✅ บันทึกกาแฟ 50฿", ephemeral=True)

    # Action: ผู้ใช้กดปุ่มด่วนข้าว -> บันทึก 60 บาทลง Supabase ทันที ไม่ผ่าน AI
    @discord.ui.button(label="🍜 ข้าว 60฿", style=discord.ButtonStyle.primary)
    async def food_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != ALLOWED_USER_ID: return
        msg_id = f"qa_{interaction.message.id}_{datetime.now().timestamp()}"
        data = {"item_name": "ข้าว", "quantity": 1, "total_price": 60, "transaction_type": "รายจ่าย", "category": "ค่าอาหาร"}
        insert_transaction(msg_id, "Quick-Food", data)
        await interaction.response.send_message("✅ บันทึกข้าว 60฿", ephemeral=True)


# ==========================================
# 5. DISCORD EVENTS & CLEAN LOGS
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# Action: ตรวจจับประเภท Error -> พิมพ์ Log สั้นใน Terminal และส่งข้อความแจ้งเตือนกระชับใน Discord
async def send_clean_error(channel, error_obj):
    err = str(error_obj).lower()
    
    if "429" in err or "resource_exhausted" in err:
        print("[ERR] Quota exceeded")
        await channel.send("⏳ โควตา AI เต็ม")
    elif "invalid json" in err or "json" in err:
        print("[ERR] JSON parse error")
        await channel.send("❌ รูปแบบข้อมูลไม่ถูกต้อง")
    else:
        print("[ERR] System error")
        await channel.send("❌ ระบบขัดข้อง")

# Action: ส่งข้อความไปวิเคราะห์ผ่าน AI -> สร้างข้อความสรุปพร้อมปุ่มกด ✅ / ❌ ส่งกลับไปใน Discord
async def handle_transaction(channel, message, mode="insert"):
    async with channel.typing():
        try:
            data = await parse_financial_text(message.content)
            
            summary = ""
            for item in data["transactions"]:
                summary += f"• `{item['item_name']}` | {item['quantity']}x | **{item['total_price']}฿**\n"
            
            view = ApproveView(message.id, message.content, data, mode=mode)
            await message.reply(summary.strip(), view=view)
        except Exception as e:
            await send_clean_error(channel, e)

# Action: ดึงข้อมูลเดือนนี้จาก Supabase -> ให้ AI ประมวลผลคำตอบแล้วส่งข้อความตอบกลับผู้ใช้
async def handle_smart_query(channel, message):
    async with channel.typing():
        try:
            db_data = get_current_month_data()
            answer = await answer_smart_query(message.content, db_data)
            await message.reply(answer)
        except Exception as e:
            await send_clean_error(channel, e)

# Action: ดักจับข้อความใหม่ -> คัดกรองว่าเป็นคำสั่ง !menu, คำถาม (Query) หรือ รายการบันทึก (Transaction)
@client.event
async def on_message(message):
    if message.type not in (discord.MessageType.default, discord.MessageType.reply):
        return
    if message.author.id != ALLOWED_USER_ID or message.author == client.user: 
        return

    text = message.content.strip()
    if text.lower() == "!menu":
        await message.channel.send("⚡ เมนูด่วน", view=QuickActionView())
        return

    query_keywords = ['เท่าไร', 'เท่าไหร่', 'อะไรบ้าง', 'สรุป', 'ไหม', '?']
    is_query = any(keyword in text for keyword in query_keywords)

    if is_query:
        await handle_smart_query(message.channel, message)
    else:
        await handle_transaction(message.channel, message, mode="insert")

# Action: ดักจับการกด Edit ข้อความเดิมใน Discord -> ส่งข้อความแก้ไขไปประมวลผลใหม่เพื่ออัปเดตแถวเดิม
@client.event
async def on_message_edit(before, after):
    if after.type not in (discord.MessageType.default, discord.MessageType.reply): return
    if after.author.id != ALLOWED_USER_ID or after.author == client.user: return
    if before.content == after.content: return
    
    query_keywords = ['เท่าไร', 'เท่าไหร่', 'อะไรบ้าง', 'สรุป', 'ไหม', '?']
    is_query = any(keyword in after.content for keyword in query_keywords)
    
    if not is_query:
        await handle_transaction(after.channel, after, mode="update")

# Action: ดักจับการลบข้อความใน Discord -> ทำการ DELETE ข้อมูลที่มี msg_id ตรงกันออกจาก Supabase ทันที
@client.event
async def on_raw_message_delete(payload):
    try:
        delete_transaction(str(payload.message_id))
        channel = client.get_channel(payload.channel_id)
        if channel:
            await channel.send("🗑️ ลบรายการแล้ว")
    except Exception:
        pass


# ==========================================
# 6. AUTOMATION
# ==========================================

# Action: ทำงานเบื้องหลังทุก 24 ชั่วโมง -> ตรวจสอบว่าตรงกับวันเงินเดือนออกหรือไม่ ถ้าตรงให้บันทึก Auto-Salary
@tasks.loop(hours=24)
async def daily_jobs():
    today = datetime.now()
    if today.day == SALARY_PAY_DAY:
        data = {"item_name": "เงินเดือน", "quantity": 1, "total_price": SALARY_AMOUNT, "transaction_type": "รายรับ", "category": "เงินเดือน"}
        msg_id = f"salary_{today.strftime('%Y%m')}"
        try:
            insert_transaction(msg_id, "Auto-Salary", data)
            print(f"[OK] Salary auto-inserted: {msg_id}")
        except Exception:
            pass

# Action: เมื่อบอทล็อกอินและเชื่อมต่อ Discord สำเร็จ -> เริ่มรัน Background Task ทันที
@client.event
async def on_ready():
    print(f"[OK] Online as {client.user}")
    if not daily_jobs.is_running(): 
        daily_jobs.start()

if __name__ == "__main__":
    keep_alive()
    client.run(DISCORD_TOKEN)