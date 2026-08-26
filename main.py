import os
import json
import re
import sys
import pandas as pd
import io
from datetime import datetime
import discord
from discord.ext import tasks
from dotenv import load_dotenv
from supabase import create_client, Client
from google import genai
from google.genai import types
from flask import Flask
from threading import Thread
from datetime import datetime, timedelta

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

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

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

# Global In-Memory Cache สำหรับเก็บความจำคำศัพท์ (ประหยัด Token)
ITEM_CACHE = {}

# รายการคำศัพท์ตรวจจับคำถาม (Global Query Keywords)
QUERY_KEYWORDS = [
        'เท่าไร', 'เท่าไหร่', 'กี่บาท', 'ยอดรวม', 'ยอดทั้งหมด', 'ยอดใช้จ่าย',
        'หมดไป', 'จ่ายไป', 'ใช้ไป', 'โดนไป', 'เสียไป',
        'อะไรบ้าง', 'ค่าไร', 'ค่าอะไร', 'ซื้อไร', 'ซื้ออะไร', 'จ่ายไร', 'จ่ายอะไร',
        'มีไรบ้าง', 'ทำไรไป',
        'เหลือเงิน', 'เงินเหลือ', 'งบเหลือ', 'เหลือเท่า',
        'สรุป', 'แพงสุด', 'มากสุด', 'เยอะสุด', 'บ่อยสุด', 'หมวดไหน', 'อันไหน',
        'ไฟล์', 'excel', 'รายงาน', 'export', 'ชีต',
        'ไหม', 'มั้ย', 'หรอ', 'เหรอ', 'ป่าว', 'เปล่า', 'รึเปล่า', 'หรือเปล่า', 
        'รึยัง', 'หรือยัง', 'บ้าง', 'มั่ง', 'ยังไง', '?',
        'วัน', 'เดือน', 'ปี', 'เมื่อวาน', 'ย้อนหลัง', 'ที่ผ่านมา', 'ก่อน'
    ]

# ==========================================
# 2. DATABASE LAYER & CACHE MANAGER
# ==========================================
def insert_transaction(msg_id: str, raw_text: str, data: dict):
    # กำหนดเวลาปัจจุบันแบบ วัน-เดือน-ปี ชัวโมง:นาที:วินาที (ไม่เอาเศษวินาทีและ Timezone)
    current_time_clean = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    record = {
        "discord_msg_id": msg_id,
        "raw_text": raw_text,
        "item_name": data.get("item_name", "ไม่ทราบชื่อ"),
        "quantity": data.get("quantity", 1),
        "total_price": data.get("total_price", 0),
        "transaction_type": data.get("transaction_type", "รายจ่าย"),
        "category": data.get("category", "ค่าสินค้า"),
        "created_at": current_time_clean  # บันทึกลง Database แบบสะอาด
    }
    supabase.table("transactions").insert(record).execute()
    
    item_lower = data.get("item_name", "").strip().lower()
    if item_lower:
        ITEM_CACHE[item_lower] = {
            "category": data.get("category", "ค่าสินค้า"),
            "transaction_type": data.get("transaction_type", "รายจ่าย")
        }

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

def load_item_cache_to_memory():
    global ITEM_CACHE
    try:
        response = supabase.table("transactions").select("item_name, category, transaction_type").limit(10000).execute()
        ITEM_CACHE.clear()

        if response.data:
            for row in response.data:
                name = row.get("item_name")
                if name and isinstance(name, str) and name.strip() and name.strip() != "ไม่ทราบชื่อ":
                    clean_key = name.strip().lower()
                    ITEM_CACHE[clean_key] = {
                        "category": row.get("category", "ค่าสินค้า"),
                        "transaction_type": row.get("transaction_type", "รายจ่าย")
                    }

        print(f"[CACHE] Successfully loaded {len(ITEM_CACHE)} items.")
    except Exception as e:
        print(f"[ERR] Cache load failed: {e}")

# ==========================================
# 3. AI ENGINE & SMART PARSER
# ==========================================
def extract_multiple_items(text: str, cache_dict: dict):
    if not text or not cache_dict:
        return None

    transactions = []
    remaining_text = text.strip()
    cache_items = sorted(cache_dict.keys(), key=len, reverse=True)

    while remaining_text:
        found_item = False
        for cache_key in cache_items:
            pattern = rf'(?<!\S){re.escape(cache_key)}\s+(\d+(?:\.\d+)?)(?:\s*บาท)?(?=\s|$)'
            match = re.search(pattern, remaining_text, flags=re.IGNORECASE)

            if not match:
                continue

            cached_info = cache_dict[cache_key]
            price = float(match.group(1))

            clean_name = remaining_text[match.start():match.start() + len(match.group(0))]
            clean_name = re.sub(r'\s*\d+(?:\.\d+)?\s*(?:บาท)?$', '', clean_name).strip()

            transactions.append({
                "item_name": clean_name,
                "quantity": 1,
                "total_price": price,
                "transaction_type": cached_info["transaction_type"],
                "category": cached_info["category"]
            })

            remaining_text = (remaining_text[:match.start()] + remaining_text[match.end():]).strip()
            found_item = True
            break

        if not found_item:
            return None

    return {"transactions": transactions} if transactions else None

async def parse_intent(text: str) -> dict:
    prompt = f"""วิเคราะห์ข้อความ: "{text}"
    แยกแยะว่าผู้ใช้ต้องการ "บันทึกรายจ่าย/รายรับ" (transaction) หรือ "ถามข้อมูล/ขอสรุป" (query)
    ตอบเป็น JSON เท่านั้น:
    {{
        "action": "transaction" หรือ "query",
        "transactions": [
            {{
                "item_name": "ชื่อรายการสั้นๆ",
                "quantity": 1,
                "total_price": 0,
                "transaction_type": "รายรับ" หรือ "รายจ่าย",
                "category": "ค่าอาหาร" | "ค่าบริการ" | "ค่าสินค้า" | "รายได้" | "เงินเดือน"
            }}
        ] 
    }}
    *หมายเหตุ: ถ้า action เป็น query ให้ปล่อย transactions เป็น [] ได้เลย
    """
    
    response = await ai_client.aio.models.generate_content(
        model='gemini-3.5-flash', # ใช้รุ่นที่ฟรี 1,500 ครั้ง/วัน และเสถียรที่สุด
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0)
    )
    
    raw_text = response.text.strip()
    start_idx, end_idx = raw_text.find('{'), raw_text.rfind('}') + 1
    if start_idx != -1 and end_idx != 0:
        return json.loads(raw_text[start_idx:end_idx])
    raise ValueError("Invalid JSON")

# Action: วิเคราะห์คำถามเกี่ยวกับข้อมูล -> ตอบแบบลิสต์สวยงาม ห้ามสร้างตาราง
async def answer_smart_query(question: str, db_data: list) -> dict:
    prompt = f"""
    คำถาม: "{question}"
    วันที่ปัจจุบัน: {datetime.now().strftime('%Y-%m-%d')}
    ข้อมูล Database: {json.dumps(db_data, ensure_ascii=False)}

    หน้าที่:
    1. วิเคราะห์ช่วงวันที่ที่ผู้ใช้ถาม (เช่น 'วันที่ 17-20', 'เมื่อวาน') เพื่อคัดกรองข้อมูล
    2. สรุปข้อมูล (ห้ามสร้างตารางเด็ดขาด ให้ตอบเป็นบรรทัดๆ)
    3. ส่งกลับมาเป็น JSON ตามโครงสร้างนี้:
    {{
        "start_date": "YYYY-MM-DD" (หรือ null ถ้าไม่ระบุวัน),
        "end_date": "YYYY-MM-DD" (หรือ null ถ้าไม่ระบุวัน),
        "summary_text": "[Emoji] [หมวดหมู่]:\\n- [ชื่อ]: [ราคา] บาท\\n\\nรวมทั้งหมด: [ยอดรวม] บาท"
    }}
    """
    response = await ai_client.aio.models.generate_content(
        model='gemini-3.5-flash',
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0)
    )
    raw_text = response.text.strip()
    start_idx, end_idx = raw_text.find('{'), raw_text.rfind('}') + 1
    if start_idx != -1 and end_idx != 0:
        return json.loads(raw_text[start_idx:end_idx])
    return {"start_date": None, "end_date": None, "summary_text": "❌ วิเคราะห์คำถามไม่สำเร็จ"}

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

    @discord.ui.button(label="❌", style=discord.ButtonStyle.danger)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != ALLOWED_USER_ID: return
        await interaction.response.edit_message(content="❌ ยกเลิก", view=None)

class QuickActionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="☕ กาแฟ 50฿", style=discord.ButtonStyle.primary)
    async def coffee_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != ALLOWED_USER_ID: return
        msg_id = f"qa_{interaction.message.id}_{datetime.now().timestamp()}"
        data = {"item_name": "กาแฟ", "quantity": 1, "total_price": 50, "transaction_type": "รายจ่าย", "category": "ค่าอาหาร"}
        insert_transaction(msg_id, "Quick-Coffee", data)
        await interaction.response.send_message("✅ บันทึกกาแฟ 50฿", ephemeral=True)

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

async def send_clean_error(channel, error_obj):
    err = str(error_obj).lower()
    try:
        print(f"[SYS_ERR] {str(error_obj)}")
    except Exception:
        pass

    if "429" in err or "resource_exhausted" in err:
        await channel.send("⏳ โควตา AI รายวันเต็มแล้ว")
    elif "invalid json" in err or "json" in err:
        await channel.send("❌ รูปแบบข้อมูลไม่ถูกต้อง")
    elif "charmap" in err or "codec" in err:
        pass 
    else:
        await channel.send("❌ ระบบขัดข้อง ไม่สามารถประมวลผลได้")

# UI ชุดที่ 1: สำหรับการบันทึกรายการ (มีปุ่ม ✅ / ❌)
async def process_transaction_ui(channel, message, parsed_data, mode, used_ai):
    summary = ""
    for item in parsed_data["transactions"]:
        summary += f"• `{item['item_name']}` | {item['quantity']}x | **{item['total_price']}฿**\n"
    
    source_tag = "🧠 `[AI วิเคราะห์]`" if used_ai else "⚡ `[RAM Cache - 0 Token]`"
    final_msg = f"{summary.strip()}\n{source_tag}"
    
    view = ApproveView(message.id, message.content, parsed_data, mode=mode)
    await message.reply(final_msg, view=view)

# UI ชุดที่ 2: สำหรับตอบคำถาม & ออกรายงาน Excel (ไม่มีปุ่ม)
async def process_query_ui(channel, message, query_result, db_data):
    summary_text = query_result.get("summary_text", "ไม่พบข้อมูล")
    start_date = query_result.get("start_date")
    end_date = query_result.get("end_date")
    
    file_keywords = ['ไฟล์', 'excel', 'รายงาน', 'export', 'ชีต']
    wants_file = any(kw in message.content.lower() for kw in file_keywords)
    
    if wants_file and db_data:
        df = pd.DataFrame(db_data)
        if not df.empty and 'created_at' in df.columns:
            # แปลงเวลาให้เป็น YYYY-MM-DD HH:MM:SS สะอาดๆ
            df['created_at'] = pd.to_datetime(df['created_at']).dt.strftime('%Y-%m-%d %H:%M:%S')
            
            # ใช้ 10 ตัวแรก (YYYY-MM-DD) สำหรับเทียบวันที่
            df['date_only'] = df['created_at'].str[:10]
            
            # กรองข้อมูลตามวันที่
            if start_date and end_date:
                df = df[(df['date_only'] >= start_date) & (df['date_only'] <= end_date)]
            elif start_date:
                df = df[df['date_only'] == start_date]

            cols = [c for c in ['created_at', 'item_name', 'category', 'transaction_type', 'quantity', 'total_price'] if c in df.columns]
            df = df[cols]
            df.rename(columns={
                'created_at': 'วัน/เวลา',
                'item_name': 'ชื่อรายการ',
                'category': 'หมวดหมู่',
                'transaction_type': 'ประเภท',
                'quantity': 'จำนวน',
                'total_price': 'ราคา (บาท)'
            }, inplace=True)
        
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Summary')
        buffer.seek(0)
        
        # 🟢 ปรับ Logic การตั้งชื่อไฟล์ให้ตรงกับคำขอ
        if start_date and end_date and start_date != end_date:
            # กรณีระบุช่วงหลายวัน
            file_name = f"Report_{start_date.replace('-', '_')}_to_{end_date.replace('-', '_')}.xlsx"
        elif start_date:
            # กรณีระบุวันเดียว (หรือ start_date ตรงกับ end_date)
            file_name = f"Report_{start_date.replace('-', '_')}.xlsx"
        else:
            # กรณีดูทั้งเดือน
            file_name = f"Report_Month_{datetime.now().strftime('%Y_%m')}.xlsx"
            
        await message.reply(summary_text, file=discord.File(fp=buffer, filename=file_name))
    else:
        await message.reply(summary_text)

# Central Router: ทำหน้าที่จ่ายงาน
async def handle_incoming_message(channel, message, mode="insert"):
    async with channel.typing():
        try:
            # 1. เช็กสมองตัวเองก่อน (Cache) - ถ้าเจอถือว่าบันทึกชัวร์ 0 Token
            parsed_data = extract_multiple_items(message.content, ITEM_CACHE)
            
            if parsed_data:
                print("[CACHE HIT] Item matched in memory (0 Token)")
                await process_transaction_ui(channel, message, parsed_data, mode, used_ai=False)
                return

            # 2. ถ้าหลุด Cache ให้ AI วิเคราะห์เจตนา (Transaction หรือ Query)
            print("[AI API] Cache miss. Forwarding request to Gemini for Intent Classification...")
            intent_data = await parse_intent(message.content)

            if intent_data.get("action") == "query":
                # 🟢 3A: AI บอกว่าเป็นคำถาม -> ดึง DB, ให้ AI สรุปข้อมูล, ส่ง Excel แบบไม่มีปุ่ม
                db_data = get_current_month_data()
                query_result = await answer_smart_query(message.content, db_data)
                await process_query_ui(channel, message, query_result, db_data)
            else:
                # 🟢 3B: AI บอกว่าเป็นการบันทึก -> บันทึก Cache เผื่ออนาคต, ส่งปุ่ม Approve ให้ยืนยัน
                parsed_data = {"transactions": intent_data.get("transactions", [])}
                
                for item in parsed_data["transactions"]:
                    name = item.get("item_name", "").strip().lower()
                    if name:
                        ITEM_CACHE[name] = {
                            "category": item.get("category", "ค่าสินค้า"),
                            "transaction_type": item.get("transaction_type", "รายจ่าย")
                        }
                
                await process_transaction_ui(channel, message, parsed_data, mode, used_ai=True)

        except Exception as e:
            await send_clean_error(channel, e)

@client.event
async def on_message(message):
    if message.type not in (discord.MessageType.default, discord.MessageType.reply): return
    if message.author.id != ALLOWED_USER_ID or message.author == client.user: return

    text = message.content.strip()
    if text.lower() == "!menu":
        await message.channel.send("⚡ เมนูด่วน", view=QuickActionView())
        return

    # ลบ List คำศัพท์ดักคำถามทิ้งไปได้เลย และโยนทุกอย่างเข้าสู่ Router กลาง
    await handle_incoming_message(message.channel, message, mode="insert")

@client.event
async def on_message_edit(before, after):
    if after.type not in (discord.MessageType.default, discord.MessageType.reply): return
    if after.author.id != ALLOWED_USER_ID or after.author == client.user: return
    if before.content == after.content: return
    
    # ลบ List คำศัพท์ดักคำถามทิ้ง และโยนให้ Router กลางจัดการแบบอัปเดต
    await handle_incoming_message(after.channel, after, mode="update")

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

@client.event
async def on_ready():
    print(f"[OK] Online as {client.user}")
    load_item_cache_to_memory()
    if not daily_jobs.is_running(): 
        daily_jobs.start()

if __name__ == "__main__":
    keep_alive()
    client.run(DISCORD_TOKEN)