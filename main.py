import os
import io
import re
import sys
import json
import pandas as pd
import traceback
import discord
from discord.ext import tasks
from datetime import datetime, timedelta
from dotenv import load_dotenv
from supabase import create_client, Client
from google import genai
from google.genai import types
from flask import Flask
from threading import Thread

# ==========================================
# 0. WEB SERVER (Keep Alive สำหรับ Render)
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return '', 204

@app.route('/healthz')
def healthz():
    return 'ok', 200, {'Content-Type': 'text/plain'}

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

# ล็อกโมเดลเป็น gemini-3.5-flash-lite ตลอดทั้งระบบ
MODEL_NAME = 'gemini-3.5-flash-lite'

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# Global In-Memory Cache สำหรับเก็บคำศัพท์
ITEM_CACHE = {}


# ==========================================
# 2. DATABASE LAYER & CACHE MANAGER
# ==========================================
def insert_transaction(msg_id: str, raw_text: str, data: dict):
    """บันทึกหรืออัปเดตข้อมูลลง Supabase พร้อมล็อกเวลาแบบไม่มีเศษวินาที"""
    current_time_clean = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    record = {
        "discord_msg_id": msg_id,
        "raw_text": raw_text,
        "item_name": data.get("item_name", "ไม่ทราบชื่อ"),
        "quantity": data.get("quantity", 1),
        "total_price": data.get("total_price", 0),
        "transaction_type": data.get("transaction_type", "รายจ่าย"),
        "category": data.get("category", "ค่าสินค้า"),
        "created_at": current_time_clean
    }
    supabase.table("transactions").upsert(record, on_conflict="discord_msg_id").execute()

def delete_transaction(msg_id: str):
    """ลบรายการออกจาก Supabase"""
    supabase.table("transactions").delete().eq("discord_msg_id", msg_id).execute()

def get_buffer_data():
    """ดึงข้อมูล 90 วันล่าสุดเป็น RAM Buffer เพื่อความรวดเร็วในการตอบคำถามทั่วไป"""
    today = datetime.now()
    start_date = (today - timedelta(days=90)).strftime("%Y-%m-%d")
    response = supabase.table("transactions").select("*").gte("created_at", f"{start_date} 00:00:00").execute()
    return response.data or []

def fetch_transactions_by_range(start_date: str = None, end_date: str = None):
    query = supabase.table("transactions").select("*").order("created_at", desc=False)
    if start_date:
        query = query.gte("created_at", f"{start_date}T00:00:00")
    if end_date:
        query = query.lte("created_at", f"{end_date}T23:59:59")
    response = query.execute()
    return response.data or []

def load_item_cache_to_memory():
    """โหลดประวัติรายการสินค้าขึ้น RAM ตอนเริ่มต้นระบบ"""
    global ITEM_CACHE
    try:
        response = supabase.table("transactions").select("item_name, category, transaction_type").limit(5000).execute()
        ITEM_CACHE.clear()
        if response.data:
            for row in response.data:
                name = row.get("item_name")
                if name and name.strip() and name.strip() != "ไม่ทราบชื่อ":
                    ITEM_CACHE[name.strip().lower()] = {
                        "category": row.get("category", "ค่าสินค้า"),
                        "transaction_type": row.get("transaction_type", "รายจ่าย")
                    }
        print(f"[CACHE] Loaded {len(ITEM_CACHE)} items to memory.")
    except Exception as e:
        print(f"[ERR] Cache load failed: {e}")

def get_user_budget(user_id: int) -> float:
    """ดึงงบประมาณรายเดือนของผู้ใช้ ถ้ายังไม่เคยตั้งจะใช้ SALARY_AMOUNT เป็นค่าเริ่มต้น"""
    try:
        res = supabase.table("user_settings").select("monthly_budget").eq("discord_user_id", user_id).execute()
        if res.data and len(res.data) > 0:
            return float(res.data[0].get("monthly_budget", SALARY_AMOUNT))
    except Exception:
        pass
    return SALARY_AMOUNT

def set_user_budget(user_id: int, budget: float):
    """บันทึกหรืออัปเดตงบประมาณรายเดือนลง Supabase"""
    record = {
        "discord_user_id": user_id,
        "monthly_budget": budget
    }
    supabase.table("user_settings").upsert(record, on_conflict="discord_user_id").execute()

# ==========================================
# 3. AI ENGINE & SMART PARSER
# ==========================================
def extract_multiple_items(text: str, cache_dict: dict):
    if not text:
        return None

    # [แก้บั๊ก ReDoS] ปรับ Regex ให้ทำงานแบบเส้นตรง
    # ป้องกันอาการ CPU ค้าง 100% เวลาพิมพ์ประโยคคำถามที่ไม่มีตัวเลข
    pattern = r'([ก-๙a-zA-Z][ก-๙a-zA-Z0-9\s-]*?)\s+(\d+(?:\.\d+)?)(?:\s*บาท)?'
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
            return None 

    return {"transactions": transactions} if transactions else None

async def parse_intent(text: str) -> dict:
    """[Unified Intent Parser] แยกแยะว่าเป็นการบันทึก, สอบถาม หรือตั้งค่างบประมาณ"""
    prompt = f"""วิเคราะห์ข้อความ: "{text}"
    แยกแยะเจตนาของผู้ใช้เป็น 1 ใน 3 รูปแบบ:
    1. "set_budget" (ต้องการตั้งเป้า/กำหนดงบ/เพดานเงิน เช่น 'ตั้งงบเดือนนี้ 4000', 'ห้ามใช้เงินเกิน 5000')
    2. "query" (ถามยอดเงิน, สรุปรายรับรายจ่าย, ถามงบที่เหลือ, ถามวันที่เหลือ)
    3. "transaction" (บันทึกรายรับ/รายจ่ายทั่วไป)

    ตอบเป็น JSON เท่านั้น:
    {{
        "action": "transaction" | "query" | "set_budget",
        "budget_amount": ตัวเลขงบประมาณ (ใส่เฉพาะเมื่อ action เป็น set_budget นอกนั้นใส่ null),
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
    """
    response = await ai_client.aio.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0)
    )
    raw_text = response.text.strip()
    start_idx, end_idx = raw_text.find('{'), raw_text.rfind('}') + 1
    if start_idx != -1 and end_idx != 0:
        return json.loads(raw_text[start_idx:end_idx])
    raise ValueError("Invalid JSON from parse_intent")

async def analyze_query_parameters(question: str) -> dict:
    today = datetime.now()
    today_str = today.strftime('%Y-%m-%d')
    first_day_this_month = today.strftime('%Y-%m-01')

    prompt = f"""
    คำถาม: "{question}"
    วันที่ปัจจุบันของระบบ: {today_str}
    วันแรกของเดือนปัจจุบัน: {first_day_this_month}

    หน้าที่:
    แปลงช่วงเวลาที่ผู้ใช้ถามเป็น start_date และ end_date (รูปแบบ YYYY-MM-DD):
    - 'ตอนนี้', 'เดือนนี้', 'ปัจจุบัน', 'เหลืองบเท่าไร', 'เหลืออีกกี่วัน': start_date คือ {first_day_this_month} และ end_date คือ {today_str}
    - 'เดือนที่แล้ว': วันแรกและวันสุดท้ายของเดือนก่อนหน้า (เช่น ปัจจุบันเดือน 8 ให้เป็น 2026-07-01 ถึง 2026-07-31)
    - 'เมื่อวาน': วันเดียวกันทั้ง start และ end
    - 'วันนี้': {today_str} ทั้ง start และ end
    - ถ้าถามภาพรวมโดยไม่ระบุเวลา: ให้ start_date เป็น {first_day_this_month} และ end_date เป็น {today_str}

    ตอบเป็น JSON เท่านั้น:
    {{
        "start_date": "YYYY-MM-DD",
        "end_date": "YYYY-MM-DD"
    }}
    """
    response = await ai_client.aio.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0)
    )
    raw_text = response.text.strip()
    start_idx, end_idx = raw_text.find('{'), raw_text.rfind('}') + 1
    if start_idx != -1 and end_idx != 0:
        return json.loads(raw_text[start_idx:end_idx])
    return {"start_date": first_day_this_month, "end_date": today_str}

async def generate_query_summary(question: str, final_data: list, user_id: int = ALLOWED_USER_ID) -> str:
    """[Summary Generator] ตอบตรงคำถาม สั้นกระชับ ไม่พ่นข้อมูลส่วนเกินที่ไม่ได้ถาม"""
    if not final_data:
        return "ไม่พบข้อมูลรายการในช่วงเวลาดังกล่าวครับ"

    today = datetime.now()
    # คำนวณวันสุดท้ายของเดือนปัจจุบัน และวันที่เหลือ
    next_month = today.replace(day=28) + timedelta(days=4)
    last_day_of_month = (next_month - timedelta(days=next_month.day)).day
    days_left = max(0, last_day_of_month - today.day)

    budget = get_user_budget(user_id)
    
    cat_summary = {}
    total_expense = 0.0
    total_income = 0.0

    for row in final_data:
        cat = row.get("หมวดหมู่") or "ค่าสินค้า"
        price = float(row.get("ราคา") or row.get("total_price") or 0)
        t_type = row.get("ประเภท") or row.get("transaction_type") or "รายจ่าย"

        cat_summary[cat] = cat_summary.get(cat, 0.0) + price
        if t_type == "รายรับ" or cat in ["เงินเดือน", "รายได้"]:
            total_income += price
        else:
            total_expense += price

    remaining_budget = budget - total_expense
    daily_budget_left = (remaining_budget / days_left) if days_left > 0 and remaining_budget > 0 else 0

    summary_payload = {
        "วันที่ปัจจุบัน": today.strftime('%Y-%m-%d'),
        "จำนวนวันที่เหลือในเดือนนี้": f"{days_left} วัน",
        "เพดานงบประมาณ": f"{budget:,.2f} บาท",
        "ใช้ไปแล้ว": f"{total_expense:,.2f} บาท",
        "งบที่เหลือ": f"{remaining_budget:,.2f} บาท",
        "เฉลี่ยใช้วันละ": f"{daily_budget_left:,.2f} บาท",
        "รายรับทั้งหมด": f"{total_income:,.2f} บาท",
        "ยอดแยกตามหมวดหมู่": cat_summary
    }

    summary_prompt = f"""
    คำถามของผู้ใช้: "{question}"
    ข้อมูลการเงิน: {json.dumps(summary_payload, ensure_ascii=False)}

    กฎการตอบ:
    1. ตอบเฉพาะสิ่งที่คำถามต้องการรู้โดยตรงเท่านั้น สั้นกระชับ (1-3 บรรทัด)
    2. ห้ามแถมข้อมูลที่ผู้ใช้ไม่ได้ถาม (เช่น ถ้าไม่ได้ถามหารายรับ หรือไม่ได้ขอแยกหมวดหมู่ ห้ามใส่มาเด็ดขาด)
    3. ตัวอย่างการตอบ:
       - ถ้าถาม 'ใช้ไปเท่าไร เหลืออีกกี่วัน':
         "💸 **ใช้ไปแล้ว:** 10,397.00 / 8,000.00 บาท (เกินงบ 2,397.00 บาท)
         📅 **เหลือเวลาอีก:** 5 วัน (งบเฉลี่ยคงเหลือ 0.00 บาท/วัน)"
    4. ห้ามสร้างตาราง (Markdown Table) เด็ดขาด
    """

    summary_res = await ai_client.aio.models.generate_content(
        model=MODEL_NAME,
        contents=summary_prompt,
        config=types.GenerateContentConfig(temperature=0.0)
    )
    return summary_res.text.strip()

# ==========================================
# 4. DISCORD UI COMPONENTS
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
            insert_transaction(sub_id, self.raw_text, item)

        # คำนวณยอดรวมรายจ่ายเดือนนี้เทียบกับงบประมาณ
        budget = get_user_budget(interaction.user.id)
        today = datetime.now()
        start_month = f"{today.year}-{today.month:02d}-01T00:00:00"
        res = supabase.table("transactions").select("total_price, transaction_type").gte("created_at", start_month).execute()
        
        total_expense = sum(float(x.get("total_price", 0)) for x in (res.data or []) if x.get("transaction_type") != "รายรับ")
        percent_used = (total_expense / budget) * 100 if budget > 0 else 0
        remaining = budget - total_expense

        warning_tag = ""
        if percent_used >= 100:
            warning_tag = f"\n🚨 **เตือนสติ:** ใช้เงินเกินงบแล้ว! ({total_expense:,.2f}/{budget:,.2f} ฿)"
        elif percent_used >= 90:
            warning_tag = f"\n⚠️ **เตือนระดับวิกฤต:** ใช้ไปแล้ว {percent_used:.1f}% เหลือเงินอีกแค่ {remaining:,.2f} ฿"
        elif percent_used >= 80:
            warning_tag = f"\n⚠️ **เตือน:** ใช้ไปแล้ว {percent_used:.1f}% ใกล้ถึงเพดานงบแล้ว"
        elif percent_used >= 50:
            warning_tag = f"\n⚡ **แจ้งเตือน:** ใช้เงินแตะครึ่งทาง (50%) ของงบแล้ว ({total_expense:,.2f}/{budget:,.2f} ฿)"

        status_text = "✅ **บันทึกแล้ว**" if self.mode == "insert" else "🔄 **อัปเดตเรียบร้อย**"
        await interaction.response.edit_message(content=f"{status_text}{warning_tag}", view=None)

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


# ==========================================
# 5. DISCORD EVENTS & SMART ROUTER
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

async def send_clean_error(channel, error_obj):
    print("\n" + "="*50, flush=True)
    print("[SYS_ERR] เกิดข้อผิดพลาด:", flush=True)
    traceback.print_exc()
    print("="*50 + "\n", flush=True)

    err = str(error_obj).lower()
    if "429" in err or "resource_exhausted" in err:
        await channel.send("⏳ โควตา AI รายวันเต็มแล้ว")
    elif "invalid json" in err or "json" in err:
        await channel.send("❌ รูปแบบข้อมูลไม่ถูกต้อง")
    elif "not found" in err or "404" in err:
        await channel.send("❌ ไม่พบโมเดล AI ที่ระบุ")
    else:
        await channel.send(f"❌ ระบบขัดข้อง: {str(error_obj)[:60]}")

async def process_transaction_ui(channel, message, parsed_data, mode, used_ai):
    """ส่งสรุปรายการให้ผู้ใช้กด Approve / Cancel"""
    summary = ""
    for item in parsed_data["transactions"]:
        summary += f"• `{item['item_name']}` | {item['quantity']}x | **{item['total_price']}฿**\n"
    source_tag = "🤖 `[AI]`" if used_ai else "⚡ `[RAM]`"
    final_msg = f"{summary.strip()}\n{source_tag}"
    view = ApproveView(message.id, message.content, parsed_data, mode=mode)
    await message.reply(final_msg, view=view)

async def process_query_ui(channel, message, db_buffer: list):
    try:
        # 1. สกัดช่วงวันที่
        analysis = await analyze_query_parameters(message.content)
        start_date = analysis.get("start_date")
        end_date = analysis.get("end_date")

        buffer_start = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")

        # 2. เลือกว่าจะดึงจาก RAM Buffer หรือยิง Supabase
        if start_date and start_date >= buffer_start and db_buffer:
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

        # 3. ส่งเฉพาะฟิลด์ที่จำเป็นเข้า AI เพื่อลดขนาด Payload
        payload_for_ai = [
            {"รายการ": x.get("item_name"), "หมวดหมู่": x.get("category"), "ราคา": x.get("total_price"), "ประเภท": x.get("transaction_type")}
            for x in final_data
        ]

        summary_text = await generate_query_summary(message.content, payload_for_ai, user_id=message.author.id)

        # 4. ป้องกันข้อความยาวเกินขีดจำกัดของ Discord
        if len(summary_text) > 1900:
            summary_text = summary_text[:1900] + "\n...(ข้อมูลยาวเกินกำหนด พิมพ์ขอเป็นไฟล์ Excel เพื่อดูครบทุกรายการ)"

        # 5. จัดการไฟล์ Excel (กรณีผู้ใช้พิมพ์ขอไฟล์)
        file_keywords = ['ไฟล์', 'excel', 'รายงาน', 'export', 'ชีต']
        wants_file = any(kw in message.content.lower() for kw in file_keywords)

        if wants_file:
            df = pd.DataFrame(final_data)
            if not df.empty and 'created_at' in df.columns:
                df['created_at'] = pd.to_datetime(df['created_at']).dt.strftime('%Y-%m-%d %H:%M:%S')
                cols = [c for c in ['created_at', 'item_name', 'category', 'transaction_type', 'quantity', 'total_price'] if c in df.columns]
                df = df[cols]
                df.rename(columns={
                    'created_at': 'วัน/เวลา', 'item_name': 'ชื่อรายการ', 'category': 'หมวดหมู่',
                    'transaction_type': 'ประเภท', 'quantity': 'จำนวน', 'total_price': 'ราคา (บาท)'
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

    except Exception as e:
        await send_clean_error(channel, e)

async def handle_incoming_message(channel, message, mode="insert"):
    """ตัวจัดการ Router กลาง: แยกงานระหว่าง RAM Cache และ AI Engine"""
    async with channel.typing():
        try:
            # 1. เช็กความจำ RAM Cache ก่อน (0 Token)
            parsed_data = extract_multiple_items(message.content, ITEM_CACHE)
            if parsed_data:
                await process_transaction_ui(channel, message, parsed_data, mode, used_ai=False)
                return

            # 2. ส่งข้อความให้ AI ตีความเจตนา
            intent_data = await parse_intent(message.content)
            action = intent_data.get("action")

            if action == "set_budget":
                # จัดการการตั้งงบประมาณ
                new_budget = float(intent_data.get("budget_amount") or 0)
                if new_budget > 0:
                    set_user_budget(message.author.id, new_budget)
                    await message.reply(f"🎯 **ตั้งงบประมาณรายเดือนสำเร็จ:** `{new_budget:,.2f} บาท`\nระบบจะคอยแจ้งเตือนเมื่อใช้เงินแตะ 50%, 80%, 90% และ 100%")
                else:
                    await message.reply("❌ กรุณาระบุจำนวนเงินงบประมาณที่ถูกต้อง")

            elif action == "query":
                db_buffer = get_buffer_data()
                await process_query_ui(channel, message, db_buffer)

            else:
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
    
    # ลบ UI ปุ่มเก่าย้อนหลังอัตโนมัติเมื่อกด Edit ข้อความ
    async for msg in after.channel.history(limit=20):
        if msg.author == client.user and msg.reference and msg.reference.message_id == after.id:
            try: await msg.delete()
            except: pass
    
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
        try: insert_transaction(msg_id, "Auto-Salary", data)
        except Exception: pass

@client.event
async def on_ready():
    print(f"[OK] Online as {client.user} | Model: {MODEL_NAME}")
    load_item_cache_to_memory()
    if not daily_jobs.is_running(): 
        daily_jobs.start()

if __name__ == "__main__":
    keep_alive()
    client.run(DISCORD_TOKEN)