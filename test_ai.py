import os
import json
from datetime import datetime
import discord
from discord.ext import tasks
from dotenv import load_dotenv
from supabase import create_client, Client
from google import genai
from google.genai import types

# ==========================================
# 1. SETUP & CONFIGURATION (ตั้งค่า)
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
# 2. DATABASE LAYER (จัดการฐานข้อมูล)
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
        "updated_at": "now()" # อัปเดตแค่เวลาแก้ไข แต่ created_at คงเดิม
    }
    supabase.table("transactions").update(record).eq("discord_msg_id", msg_id).execute()

def delete_transaction(msg_id: str):
    supabase.table("transactions").delete().eq("discord_msg_id", msg_id).execute()


# ==========================================
# 3. AI ENGINE (วิเคราะห์เร็วพิเศษ)
# ==========================================
async def parse_financial_text(text: str) -> dict:
    prompt = f"""วิเคราะห์ข้อความ: "{text}"
    ตอบเป็น JSON ตามโครงสร้างนี้เท่านั้น:
    {{
        "item_name": "ชื่อรายการ",
        "quantity": 1,
        "total_price": 0,
        "transaction_type": "รายรับ" หรือ "รายจ่าย",
        "category": "ค่าอาหาร" | "ค่าบริการ" | "ค่าสินค้า" | "รายได้" | "เงินเดือน"
    }}"""
    
    # ปลด max_output_tokens ออกเพื่อให้ AI พิมพ์ JSON จนจบ (temperature=0.0 ยังช่วยให้เร็วอยู่)
    response = await ai_client.aio.models.generate_content(
        model='gemini-3.6-flash',
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.0
        )
    )
    
    # ดึงมาเฉพาะเนื้อหาที่อยู่ในปีกกา {} ป้องกัน AI พิมพ์ข้อความแถม
    raw_text = response.text.strip()
    start_idx = raw_text.find('{')
    end_idx = raw_text.rfind('}') + 1
    
    if start_idx != -1 and end_idx != 0:
        clean_json = raw_text[start_idx:end_idx]
        return json.loads(clean_json)
    else:
        raise ValueError(f"รูปแบบที่ AI ตอบกลับมาไม่ถูกต้อง: {raw_text}")


# ==========================================
# 4. DISCORD UI (ปุ่ม Approve)
# ==========================================
class ApproveView(discord.ui.View):
    def __init__(self, msg_id, raw_text, parsed_data, mode="insert"):
        super().__init__(timeout=120)
        self.msg_id = str(msg_id)
        self.raw_text = raw_text
        self.parsed_data = parsed_data
        self.mode = mode # กำหนดว่าเป็นสร้างใหม่ (insert) หรือแก้ไข (update)

    @discord.ui.button(label="✅ ยืนยัน", style=discord.ButtonStyle.success)
    async def approve_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != ALLOWED_USER_ID: return
        
        if self.mode == "insert":
            insert_transaction(self.msg_id, self.raw_text, self.parsed_data)
            await interaction.response.edit_message(content="✅ **บันทึกรายการสำเร็จ!**", view=None)
        elif self.mode == "update":
            update_transaction(self.msg_id, self.raw_text, self.parsed_data)
            await interaction.response.edit_message(content="🔄 **อัปเดตรายการสำเร็จ!** (เวลาเดิม)", view=None)

    @discord.ui.button(label="❌ ยกเลิก", style=discord.ButtonStyle.danger)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != ALLOWED_USER_ID: return
        await interaction.response.edit_message(content="❌ **ยกเลิกแล้ว**", view=None)


# ==========================================
# 5. DISCORD EVENTS (ดักจับแชท/ลบ/แก้ไข)
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

async def process_and_send(channel, message, mode="insert"):
    async with channel.typing():
        try:
            data = await parse_financial_text(message.content)
            header = "📝 **สรุปรายการใหม่:**" if mode == "insert" else "✏️ **สรุปรายการ (แก้ไข):**"
            summary = (f"{header}\n"
                       f"• `{data['item_name']}` ({data['category']}) | {data['quantity']} ชิ้น\n"
                       f"• {data['transaction_type']}: `{data['total_price']}` บาท")
            
            view = ApproveView(message.id, message.content, data, mode=mode)
            await message.reply(summary, view=view)
        except Exception as e:
            print(f"❌ Error Detail: {e}")  # พิมพ์สาเหตุจริงลง Terminal
            await channel.send("❌ AI วิเคราะห์ไม่สำเร็จ กรุณาพิมพ์ใหม่ครับ")

@client.event
async def on_message(message):
    if message.author.id != ALLOWED_USER_ID or message.author == client.user: return
    await process_and_send(message.channel, message, mode="insert")

@client.event
async def on_message_edit(before, after):
    if after.author.id != ALLOWED_USER_ID or after.author == client.user: return
    if before.content == after.content: return # กันบอทเตือนซ้ำตอน Discord โหลด Embed
    await process_and_send(after.channel, after, mode="update")

@client.event
async def on_raw_message_delete(payload):
    # ตรวจสอบการลบข้อความ หากมีใน DB จะทำการลบทิ้ง
    try:
        delete_transaction(str(payload.message_id))
        channel = client.get_channel(payload.channel_id)
        if channel:
            await channel.send("🗑️ **ตรวจพบการลบข้อความ: นำรายการออกจาก Database แล้ว!**")
    except Exception as e:
        pass


# ==========================================
# 6. AUTOMATION & STARTUP
# ==========================================
@tasks.loop(hours=24)
async def auto_salary_job():
    if datetime.now().day == SALARY_PAY_DAY:
        data = {"item_name": "เงินเดือน", "quantity": 1, "total_price": SALARY_AMOUNT, "transaction_type": "รายรับ", "category": "เงินเดือน"}
        msg_id = f"salary_{datetime.now().strftime('%Y%m')}"
        try:
            insert_transaction(msg_id, "Auto-Salary", data)
            print(f"💰 บันทึกเงินเดือนอัตโนมัติสำเร็จ! (ID: {msg_id})")
        except Exception as e:
            # ดัก Error ข้อมูลซ้ำ เพื่อไม่ให้บอทพัง
            if "duplicate key" in str(e).lower():
                print(f"⏩ ข้ามเงินเดือนอัตโนมัติ: เดือนนี้ ({msg_id}) ถูกบันทึกไปแล้ว")
            else:
                print(f"❌ เกิดข้อผิดพลาดในการบันทึกเงินเดือน: {e}")

@client.event
async def on_ready():
    print(f'✅ บอทออนไลน์: {client.user}')
    if not auto_salary_job.is_running(): 
        auto_salary_job.start()

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)