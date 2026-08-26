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
    return "OK", 200, {'Content-Type': 'text/plain'}

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
    # ดึงย้อนหลัง 180 วัน (6 เดือน) ครอบคลุมการถามย้อนหลังยาวๆ
    start_date = (today - timedelta(days=180)).strftime("%Y-%m-%d")
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
    if not text:
        return None

    # ปรับ Regex ให้รองรับชื่อที่มีตัวเลขผสม (เช่น M150 15, น้ำแพ็ค6 50)
    pattern = r'([ก-๙a-zA-Z]+(?:[\s-]*[ก-๙a-zA-Z0-9]+)*?)\s*(\d+(?:\.\d+)?)(?:\s*บาท)?'
    matches = re.findall(pattern, text)
    
    if not matches:
        return None

    transactions = []
    for item_name, price_str in matches:
        clean_name = item_name.strip()
        cache_key = clean_name.lower()

        if cache_dict and cache_key in cache_dict:
            cached_info = cache_dict[cache_key]
            transactions.append({
                "item_name": clean_name,
                "quantity": 1,
                "total_price": float(price_str),
                "transaction_type": cached_info.get("transaction_type", "รายจ่าย"),
                "category": cached_info.get("category", "ค่าอาหาร")
            })
        else:
            # สำคัญมาก! ถ้าเจอคำแปลกใหม่แม้แต่คำเดียว ต้อง Return None 
            # เพื่อให้ Router โยนข้อความนี้ไปให้ Gemini คิดวิเคราะห์หมวดหมู่ที่ถูกต้อง
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
            
            # ใช้ Record รวมศูนย์ เพื่อ Upsert ทีเดียว
            record = {
                "discord_msg_id": sub_id,
                "raw_text": self.raw_text,
                "item_name": item.get("item_name", "ไม่ทราบชื่อ"),
                "quantity": item.get("quantity", 1),
                "total_price": item.get("total_price", 0),
                "transaction_type": item.get("transaction_type", "รายจ่าย"),
                "category": item.get("category", "ค่าสินค้า")
            }
            # ใช้ upsert: ถ้ามีอยู่แล้วจะอัปเดตข้อมูล (คงเวลา created_at เดิมไว้) ถ้าไม่มีจะแทรกแถวใหม่
            supabase.table("transactions").upsert(record, on_conflict="discord_msg_id").execute()

        await interaction.response.edit_message(content="✅ **บันทึกข้อมูลเรียบร้อย**", view=None)

    @discord.ui.button(label="❌", style=discord.ButtonStyle.danger)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != ALLOWED_USER_ID: return
        await interaction.response.edit_message(content="❌ **ยกเลิก**", view=None)

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
# 5. DISCORD EVENTS & SMART ROUTER
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

# UI สำหรับการบันทึกรายการ (มีปุ่ม ✅ / ❌)
async def process_transaction_ui(channel, message, parsed_data, mode, used_ai):
    summary = ""
    for item in parsed_data["transactions"]:
        summary += f"• `{item['item_name']}` | {item['quantity']}x | **{item['total_price']}฿**\n"
    
    source_tag = "🤖 `[AI]`" if used_ai else "⚡ `[RAM]`"
    final_msg = f"{summary.strip()}\n{source_tag}"
    
    view = ApproveView(message.id, message.content, parsed_data, mode=mode)
    await message.reply(final_msg, view=view)

# ฟังก์ชันดึงข้อมูลจาก Supabase แบบยืดหยุ่น (On-Demand Fetch)
def fetch_transactions_by_range(start_date: str = None, end_date: str = None):
    query = supabase.table("transactions").select("*").order("created_at", desc=False)
    
    if start_date:
        query = query.gte("created_at", f"{start_date} 00:00:00")
    else:
        default_start = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
        query = query.gte("created_at", f"{default_start} 00:00:00")
        
    if end_date:
        query = query.lte("created_at", f"{end_date} 23:59:59")
        
    response = query.execute()
    return response.data or []

# AI สกัด Parameter + ตรวจสอบ Buffer
async def analyze_and_summarize_query(question: str, preloaded_data: list) -> dict:
    today_str = datetime.now().strftime('%Y-%m-%d')
    prompt = f"""
    คำถาม: "{question}"
    วันที่ปัจจุบัน: {today_str}
    ข้อมูลสำรองที่มี: {json.dumps(preloaded_data[:100], ensure_ascii=False)}

    หน้าที่ของคุณ:
    1. วิเคราะห์ช่วงวันที่ที่ผู้ใช้ต้องการ (start_date, end_date ในรูปแบบ YYYY-MM-DD) 
       - เช่น 'เมื่อวาน' -> start_date และ end_date คือวันเดียวกัน
       - เช่น '7 เดือนที่แล้ว' -> คำนวณเดือนและปีให้ถูกต้อง
       - ถ้าไม่ระบุวัน ให้ใส่ null
    2. ระบุว่าข้อมูลสำรองที่มี ครอบคลุมคำถามนี้หรือไม่ (is_covered: true/false)
    3. ตอบเป็น JSON โครงสร้างนี้เท่านั้น:
    {{
        "start_date": "YYYY-MM-DD" หรือ null,
        "end_date": "YYYY-MM-DD" หรือ null,
        "is_covered": true หรือ false,
        "target_category": "ค่าอาหาร" หรือ null
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
    return {"start_date": None, "end_date": None, "is_covered": False}

# UI สำหรับตอบคำถาม & ออกรายงาน Excel (ไม่มีปุ่ม)
async def process_query_ui(channel, message, db_buffer: list):
    analysis = await analyze_and_summarize_query(message.content, db_buffer)
    
    start_date = analysis.get("start_date")
    end_date = analysis.get("end_date")
    is_covered = analysis.get("is_covered", False)
    
    # เลือกระหว่าง RAM Buffer หรือยิง Supabase On-Demand
    if is_covered and db_buffer:
        final_data = db_buffer
        if start_date:
            final_data = [x for x in final_data if str(x.get("created_at", ""))[:10] >= start_date]
        if end_date:
            final_data = [x for x in final_data if str(x.get("created_at", ""))[:10] <= end_date]
    else:
        final_data = fetch_transactions_by_range(start_date, end_date)

    if not final_data:
        await message.reply("ไม่พบข้อมูลรายการในช่วงเวลาดังกล่าวครับ")
        return

    # สรุปข้อความผ่าน AI
    summary_prompt = f"""
    คำถาม: "{message.content}"
    ข้อมูลจริง: {json.dumps(final_data, ensure_ascii=False)}
    กฎ:
    1. ห้ามสร้างตาราง (Markdown Table) เด็ดขาด ให้ตอบเป็นบรรทัดๆ
    2. ใช้ Emoji 1 ตัว เฉพาะหน้าชื่อหมวดหมู่
    3. รูปแบบ:
    [Emoji] [หมวดหมู่]:
    - [ชื่อรายการ]: [ราคา] บาท

    รวมทั้งหมด: [ยอดรวม] บาท
    """
    summary_res = await ai_client.aio.models.generate_content(
        model='gemini-3.5-flash',
        contents=summary_prompt,
        config=types.GenerateContentConfig(temperature=0.0)
    )
    summary_text = summary_res.text.strip()

    # ตรวจสอบการขอไฟล์ Excel (ใช้ข้อมูลชุดเดียวกัน 100%)
    file_keywords = ['ไฟล์', 'excel', 'รายงาน', 'export', 'ชีต']
    wants_file = any(kw in message.content.lower() for kw in file_keywords)

    if wants_file:
        df = pd.DataFrame(final_data)
        if not df.empty and 'created_at' in df.columns:
            df['created_at'] = pd.to_datetime(df['created_at']).dt.strftime('%Y-%m-%d %H:%M:%S')
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

        if start_date and end_date and start_date != end_date:
            file_name = f"Report_{start_date.replace('-', '_')}_to_{end_date.replace('-', '_')}.xlsx"
        elif start_date:
            file_name = f"Report_{start_date.replace('-', '_')}.xlsx"
        else:
            file_name = f"Report_Summary_{datetime.now().strftime('%Y_%m_%d')}.xlsx"

        await message.reply(summary_text, file=discord.File(fp=buffer, filename=file_name))
    else:
        await message.reply(summary_text)

# Central Router: ตัวแยกงานและตัดสินใจหลัก
async def handle_incoming_message(channel, message, mode="insert"):
    async with channel.typing():
        try:
            # 1. เช็ก RAM Cache ก่อน (0 Token)
            parsed_data = extract_multiple_items(message.content, ITEM_CACHE)
            
            if parsed_data:
                await process_transaction_ui(channel, message, parsed_data, mode, used_ai=False)
                return

            # 2. ถ้าหลุด Cache ให้ AI วิเคราะห์ Intent (Transaction หรือ Query)
            intent_data = await parse_intent(message.content)

            if intent_data.get("action") == "query":
                # ตอบคำถาม / สรุปยอด / ขอไฟล์ Excel (ไม่มีปุ่มเด้ง)
                db_buffer = get_current_month_data()
                await process_query_ui(channel, message, db_buffer)
            else:
                # บันทึกรายการใหม่ -> บันทึก Cache และแสดงปุ่มยืนยัน
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

    await handle_incoming_message(message.channel, message, mode="insert")

@client.event
async def on_message_edit(before, after):
    if after.type not in (discord.MessageType.default, discord.MessageType.reply): return
    if after.author.id != ALLOWED_USER_ID or after.author == client.user: return
    if before.content == after.content: return
    
    # ลบข้อความ UI เก่าที่บอทเคยส่งไว้ทิ้ง เพื่อเคลียร์ปุ่มขยะ
    async for msg in after.channel.history(limit=20):
        if msg.author == client.user and msg.reference and msg.reference.message_id == after.id:
            try:
                await msg.delete()
            except Exception:
                pass
    
    await handle_incoming_message(after.channel, after, mode="update")

@client.event
async def on_raw_message_delete(payload):
    try:
        delete_transaction(str(payload.message_id))
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