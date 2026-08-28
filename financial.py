import os
import io
import re
import sys
import json
import difflib
import asyncio
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

AI_MODELS = [
    # --- 1. กลุ่ม Lite (ประหยัดสุด/เร็วสุด) ---
    "gemini-flash-lite-latest",
    "gemini-2.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.1-flash-lite-preview",
    "gemini-3.5-flash-lite",
    
    # --- 2. กลุ่ม Flash (มาตรฐาน) ---
    "gemini-flash-latest",
    "gemini-2.5-flash",
    "gemini-3-flash-preview",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# Global In-Memory Cache สำหรับเก็บคำศัพท์ (0 Token)
ITEM_CACHE = {}

async def generate_with_fallback(contents, config):
    """วนลูปยิงโมเดลทีละตัวจากเล็กไปใหญ่ ถ้าพังจะสลับไปตัวถัดไปทันที"""
    last_error = None
    for model_name in AI_MODELS:
        try:
            response = await ai_client.aio.models.generate_content(
                model=model_name,
                contents=contents,
                config=config
            )
            return response, model_name
        except Exception as e:
            print(f"[Fallback] {model_name} failed: {e}. Switching...")
            last_error = e
            continue
    raise Exception(f"All AI Models failed! Last Error: {last_error}")

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

def fetch_transactions_by_range(start_date: str = None, end_date: str = None):
    """ดึงข้อมูลสดจาก Supabase ตามช่วงวันที่กำหนด"""
    query = supabase.table("transactions").select("*").order("created_at", desc=False)
    if start_date:
        query = query.gte("created_at", f"{start_date} 00:00:00")
    if end_date:
        query = query.lte("created_at", f"{end_date} 23:59:59")
    response = query.execute()
    return response.data or []

def load_item_cache_to_memory():
    """โหลดประวัติรายการสินค้าและข้อความเดิมขึ้น RAM เพื่อจับคู่ด่วน"""
    global ITEM_CACHE
    try:
        response = supabase.table("transactions").select("item_name, raw_text, category, transaction_type").limit(5000).execute()
        ITEM_CACHE.clear()
        if response.data:
            for row in response.data:
                name = row.get("item_name")
                raw = row.get("raw_text")
                info = {
                    "category": row.get("category", "ค่าสินค้า"),
                    "transaction_type": row.get("transaction_type", "รายจ่าย")
                }
                # เก็บทั้งชื่อสินค้ามาตรฐาน และ raw_text เดิมที่เคยผ่าน AI มาแล้ว
                if name and name.strip() and name.strip() != "ไม่ทราบชื่อ":
                    ITEM_CACHE[name.strip().lower()] = info
                if raw and raw.strip():
                    ITEM_CACHE[raw.strip().lower()] = info
                    
        print(f"[CACHE] Loaded {len(ITEM_CACHE)} patterns to memory.")
    except Exception as e:
        print(f"[ERR] Cache load failed: {e}")

def get_user_budget(user_id: int) -> float:
    """ดึงงบประมาณรายเดือนของผู้ใช้"""
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
def find_best_cached_match(name: str, cache_dict: dict) -> dict | None:
    """ค้นหาหมวดหมู่จาก RAM โดยรองรับคำพิมพ์ผิด/คำใกล้เคียง"""
    key = name.lower().strip()
    if key in cache_dict:
        return cache_dict[key]

    # ตรวจสอบว่าคำค้นมีส่วนย่อยตรงกับคีย์ใน RAM หรือไม่
    for cached_name, info in cache_dict.items():
        if cached_name in key or key in cached_name:
            return info

    # Fuzzy Matching คำใกล้เคียง (ความแม่นยำ 75% ขึ้นไป)
    matches = difflib.get_close_matches(key, cache_dict.keys(), n=1, cutoff=0.75)
    if matches:
        return cache_dict[matches[0]]

    return None

def extract_multiple_items(text: str, cache_dict: dict):
    """ดึงรายการสินค้าจาก RAM Cache รองรับหลายรายการและคำพิมพ์ผิด (0 Token)"""
    if not text:
        return None

    # สกัดคู่ ข้อความ + ตัวเลขราคา เช่น "ไก่ย่าง 60", "น้ำตก 60 บาท"
    pattern = r'([ก-๙a-zA-Z0-9\s-]+?)\s+(\d+(?:\.\d+)?)(?:\s*บาท|\s*฿)?'
    matches = re.findall(pattern, text)
    
    if not matches:
        return None

    transactions = []
    for item_name_raw, price_str in matches:
        clean_name = item_name_raw.strip()
        # ข้ามคำสั้นเกินไปหรือคำเชื่อม
        if not clean_name or len(clean_name) < 2:
            return None

        cached_info = find_best_cached_match(clean_name, cache_dict)
        if cached_info:
            transactions.append({
                "item_name": clean_name,
                "quantity": 1,
                "total_price": float(price_str),
                "transaction_type": cached_info.get("transaction_type", "รายจ่าย"),
                "category": cached_info.get("category", "ค่าอาหาร")
            })
        else:
            # หากมีแม้แต่รายการเดียวที่ไม่เคยบันทึก ให้ส่งต่อไปให้ AI เรียนรู้
            return None 

    return {"transactions": transactions} if transactions else None

async def parse_intent(text: str) -> tuple[dict, str]:
    """วิเคราะห์เจตนาข้อความ: transaction, query หรือ set_budget"""
    prompt = f"""วิเคราะห์ข้อความ: "{text}"
แยกแยะเจตนาของผู้ใช้เป็น 1 ใน 3 รูปแบบ:
1. "set_budget" (ต้องการตั้งเป้า/กำหนดงบ/เพดานเงิน เช่น 'ตั้งงบเดือนนี้ 4000', 'ห้ามใช้เงินเกิน 5000')
2. "query" (ถามยอดเงิน, สรุปรายรับรายจ่าย, ถามงบที่เหลือ, ถามวันที่เหลือ, ถามภาพรวม)
3. "transaction" (บันทึกรายรับ/รายจ่ายทั่วไป เช่น 'ข้าวมันไก่ 50', 'เงินเดือนเข้า 30000')

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
}}"""
    config = types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0)
    response, used_model = await generate_with_fallback(prompt, config)
    raw_text = response.text.strip()
    start_idx, end_idx = raw_text.find('{'), raw_text.rfind('}') + 1
    if start_idx != -1 and end_idx != 0:
        return json.loads(raw_text[start_idx:end_idx]), used_model
    raise ValueError("Invalid JSON from parse_intent")

async def parse_intent_with_retry(text: str, max_retries: int = 2) -> tuple[dict, str]:
    """ระบบ Auto-Retry เมื่อ AI เกิด Error 503 หรือ High Demand ชั่วคราว"""
    for attempt in range(max_retries + 1):
        try:
            return await parse_intent(text)
        except Exception as e:
            err_str = str(e).lower()
            if ("503" in err_str or "unavailable" in err_str or "high demand" in err_str) and attempt < max_retries:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            raise e

async def analyze_query_parameters(question: str) -> dict:
    """สกัดช่วงเวลา start_date และ end_date จากคำถามของผู้ใช้"""
    today = datetime.now()
    today_str = today.strftime('%Y-%m-%d')
    first_day_this_month = today.strftime('%Y-%m-01')
    first_day_2_months_ago = (today.replace(day=1) - timedelta(days=60)).strftime('%Y-%m-01')
    yesterday_str = (today - timedelta(days=1)).strftime('%Y-%m-%d')

    prompt = f"""คำถาม: "{question}"
วันที่ปัจจุบัน: {today_str}
วันแรกของเดือนนี้: {first_day_this_month}
วันแรกของ 2 เดือนที่แล้ว: {first_day_2_months_ago}

หน้าที่: สกัดช่วงเวลา start_date และ end_date (รูปแบบ YYYY-MM-DD):
- '2 เดือนที่ผ่านมา', '2 เดือนย้อนหลัง': start_date คือ {first_day_2_months_ago}, end_date คือ {today_str}
- 'เดือนที่แล้ว': วันแรกและวันสุดท้ายของเดือนก่อนหน้า (เช่น 2026-07-01 ถึง 2026-07-31)
- 'เดือนนี้', 'ตอนนี้', 'ปัจจุบัน', 'งบเหลือเท่าไร', 'เหลืออีกกี่วัน': start_date คือ {first_day_this_month}, end_date คือ {today_str}
- 'วันนี้': start_date คือ {today_str}, end_date คือ {today_str}
- 'เมื่อวาน': start_date คือ {yesterday_str}, end_date คือ {yesterday_str}
- ถ้าไม่ระบุเวลา: start_date คือ {first_day_this_month}, end_date คือ {today_str}

ตอบเป็น JSON เท่านั้น:
{{
    "start_date": "YYYY-MM-DD",
    "end_date": "YYYY-MM-DD"
}}"""
    config = types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0)
    response, _ = await generate_with_fallback(prompt, config)
    raw_text = response.text.strip()
    start_idx, end_idx = raw_text.find('{'), raw_text.rfind('}') + 1
    if start_idx != -1 and end_idx != 0:
        return json.loads(raw_text[start_idx:end_idx])
    return {"start_date": first_day_this_month, "end_date": today_str}

async def generate_query_summary(question: str, final_data: list, user_id: int = ALLOWED_USER_ID) -> str:
    """คำนวณตัวเลขทางคณิตศาสตร์ให้เสร็จสมบูรณ์ก่อนส่งให้ AI สรุปเป็นภาษาไทย"""
    if not final_data:
        return "ไม่พบข้อมูลรายการในช่วงเวลาดังกล่าวครับ"

    today = datetime.now()
    next_month = today.replace(day=28) + timedelta(days=4)
    last_day_of_month = (next_month - timedelta(days=next_month.day)).day
    days_left = max(0, last_day_of_month - today.day)

    budget = get_user_budget(user_id)
    
    cat_summary = {}
    total_expense = 0.0
    total_income = 0.0

    for row in final_data:
        cat = row.get("หมวดหมู่") or row.get("category") or "ค่าสินค้า"
        price = float(row.get("ราคา") or row.get("total_price") or 0)
        t_type = row.get("ประเภท") or row.get("transaction_type") or "รายจ่าย"

        if t_type == "รายรับ" or cat in ["เงินเดือน", "รายได้"]:
            total_income += price
        else:
            total_expense += price
            cat_summary[cat] = cat_summary.get(cat, 0.0) + price

    diff = total_income - total_expense
    remaining_budget = budget - total_expense
    daily_budget_left = (remaining_budget / days_left) if days_left > 0 and remaining_budget > 0 else 0

    summary_payload = {
        "สรุปยอดคำนวณจริงจากระบบ": {
            "รวมรายจ่าย": f"{total_expense:,.2f} บาท",
            "รวมรายรับ": f"{total_income:,.2f} บาท",
            "ส่วนต่างคงเหลือ (รายรับ-รายจ่าย)": f"{diff:,.2f} บาท",
            "สถานะการเงิน": "รายจ่ายเกินรายรับ" if total_expense > total_income else "ไม่เกินรายรับ (มีเงินเก็บ)",
            "ยอดแยกตามหมวดหมู่": {k: f"{v:,.2f} บาท" for k, v in cat_summary.items()}
        },
        "สถานะงบประมาณเดือนนี้": {
            "เพดานงบประมาณ": f"{budget:,.2f} บาท",
            "ใช้ไปแล้ว": f"{total_expense:,.2f} บาท",
            "งบที่เหลือ": f"{remaining_budget:,.2f} บาท",
            "จำนวนวันที่เหลือในเดือนนี้": f"{days_left} วัน",
            "งบเฉลี่ยใช้ได้ต่อวัน": f"{daily_budget_left:,.2f} บาท"
        }
    }

    summary_prompt = f"""คำถามของผู้ใช้: "{question}"
ข้อมูลตัวเลขจริงที่คำนวณแล้ว:
{json.dumps(summary_payload, ensure_ascii=False, indent=2)}

กฎการตอบ:
1. ตอบเฉพาะสิ่งที่ผู้ใช้ถามโดยตรง สั้นกระชับ (1-3 บรรทัด)
2. ห้ามคิดเลขหรือประเมินยอดใหม่เด็ดขาด ให้ดึงตัวเลขจากข้อมูลด้านบนไปตอบเท่านั้น
3. ถ้าถามเปรียบเทียบรายรับ vs รายจ่าย ให้บอก รวมรายจ่าย, รวมรายรับ, ส่วนต่าง และสรุปว่าเกินหรือไม่ชัดเจน
4. ห้ามสร้างตาราง Markdown เด็ดขาด"""

    config = types.GenerateContentConfig(temperature=0.0)
    response, used_model = await generate_with_fallback(summary_prompt, config)
    return f"{response.text.strip()}\n\n*(⚡ Model: `{used_model}`)*"


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
        if interaction.user.id != ALLOWED_USER_ID: 
            return
        
        try:
            # 1. บันทึกรายการลง Supabase
            for idx, item in enumerate(self.parsed_data["transactions"]):
                sub_id = f"{self.msg_id}_{idx}" if len(self.parsed_data["transactions"]) > 1 else self.msg_id
                insert_transaction(sub_id, self.raw_text, item)

            # 2. คำนวณยอดเงินรวมและยอดของรายการปัจจุบัน
            budget = get_user_budget(interaction.user.id)
            today = datetime.now()
            start_month = f"{today.year}-{today.month:02d}-01 00:00:00"
            res = supabase.table("transactions").select("total_price, transaction_type").gte("created_at", start_month).execute()
            
            total_expense = sum(float(x.get("total_price", 0)) for x in (res.data or []) if x.get("transaction_type") != "รายรับ")
            current_tx_total = sum(float(x.get("total_price", 0)) for x in self.parsed_data["transactions"] if x.get("transaction_type") != "รายรับ")
            
            # เปรียบเทียบ % ก่อนและหลังบันทึก
            old_expense = total_expense - current_tx_total
            old_pct = (old_expense / budget) * 100 if budget > 0 else 0
            new_pct = (total_expense / budget) * 100 if budget > 0 else 0
            remaining = budget - total_expense

            # 3. แจ้งเตือนเฉพาะ "จังหวะแรกที่ข้ามเส้นเกณฑ์" เท่านั้น (ไม่ส่งซ้ำซาก)
            warning_tag = ""
            if old_pct < 100 and new_pct >= 100:
                warning_tag = f"\n🚨 **เตือนสติ:** ใช้เงินเกินงบแล้ว! ({total_expense:,.2f}/{budget:,.2f} ฿)"
            elif old_pct < 90 and new_pct >= 90:
                warning_tag = f"\n⚠️ **เตือนวิกฤต:** ใช้ไปแล้ว {new_pct:.1f}% เหลืออีก {remaining:,.2f} ฿"
            elif old_pct < 80 and new_pct >= 80:
                warning_tag = f"\n⚠️ **เตือน:** ใช้ไปแล้ว {new_pct:.1f}% ใกล้ถึงเพดานงบแล้ว"
            elif old_pct < 50 and new_pct >= 50:
                warning_tag = f"\n⚡ **แจ้งเตือน:** ใช้เงินแตะครึ่งทาง (50%) แล้ว ({total_expense:,.2f}/{budget:,.2f} ฿)"

            status_text = "✅ **บันทึกแล้ว**" if self.mode == "insert" else "🔄 **อัปเดตเรียบร้อย**"
            await interaction.response.edit_message(content=f"{status_text}{warning_tag}", view=None)

        except Exception as e:
            traceback.print_exc()
            await interaction.response.send_message("❌ บันทึกข้อมูลไม่สำเร็จ กรุณาลองใหม่อีกครั้ง", ephemeral=True)

    @discord.ui.button(label="❌", style=discord.ButtonStyle.danger)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != ALLOWED_USER_ID: 
            return
        await interaction.response.edit_message(content="❌ **ยกเลิก**", view=None)

class QuickActionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="☕ กาแฟ 50฿", style=discord.ButtonStyle.primary)
    async def coffee_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != ALLOWED_USER_ID: 
            return
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
    """ส่ง Error สั้น กระชับ ตรงประเด็นเข้า Discord และแสดง Full Traceback ใน Terminal"""
    print("\n" + "="*50, flush=True)
    traceback.print_exc()
    print("="*50 + "\n", flush=True)

    err = str(error_obj).lower()

    if "429" in err or "resource_exhausted" in err:
        await channel.send("⏳ โควตา AI เต็มชั่วคราว กรุณารอสักครู่")
    elif "503" in err or "unavailable" in err or "high demand" in err:
        await channel.send("⚠️ เซิร์ฟเวอร์ AI กำลังโหลดหนัก กรุณาลองใหม่อีกครั้ง")
    elif "invalid json" in err or "json" in err:
        await channel.send("❌ รูปแบบข้อมูลไม่ถูกต้อง กรุณาระบุใหม่อีกครั้ง")
    elif any(kw in err for kw in ["timeout", "connect", "connection", "aiohttp"]):
        await channel.send("🌐 การเชื่อมต่อขัดข้อง กรุณาลองใหม่อีกครั้ง")
    elif any(kw in err for kw in ["postgrest", "supabase", "database"]):
        await channel.send("🗄️ ฐานข้อมูลขัดข้อง ไม่สามารถทำรายการได้")
    else:
        await channel.send("❌ ระบบขัดข้อง ไม่สามารถประมวลผลได้")

async def process_transaction_ui(channel, message, parsed_data, mode, used_ai, used_model=""):
    """ส่งสรุปรายการให้ผู้ใช้กด Approve / Cancel"""
    summary = ""
    for item in parsed_data["transactions"]:
        summary += f"• `{item['item_name']}` | {item['quantity']}x | **{item['total_price']}฿**\n"
    source_tag = f"🤖 `[AI: {used_model}]`" if used_ai else "⚡ `[RAM]`"
    final_msg = f"{summary.strip()}\n{source_tag}"
    view = ApproveView(message.id, message.content, parsed_data, mode=mode)
    await message.reply(final_msg, view=view)

async def process_query_ui(channel, message):
    """ประมวลผลคำถามและรายงานสรุป ดึงข้อมูลสดจาก Supabase 100%"""
    try:
        # 1. ให้ AI สกัดช่วงวันที่จากคำถาม
        analysis = await analyze_query_parameters(message.content)
        start_date = analysis.get("start_date")
        end_date = analysis.get("end_date")

        # 2. ยิงดึงข้อมูลสดจาก Supabase ทันที ข้อมูลสดใหม่ตรงกับความจริง 100%
        final_data = fetch_transactions_by_range(start_date, end_date)

        if not final_data:
            await message.reply("ไม่พบข้อมูลรายการในช่วงเวลาดังกล่าวครับ")
            return

        # 3. คำนวณและสรุปคำตอบ
        summary_text = await generate_query_summary(message.content, final_data, user_id=message.author.id)

        if len(summary_text) > 1900:
            summary_text = summary_text[:1900] + "\n...(ข้อมูลยาวเกินกำหนด พิมพ์ขอเป็นไฟล์ Excel เพื่อดูครบทุกรายการ)"

        # 4. จัดการไฟล์ Excel กรณีผู้ใช้ขอ
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

            file_name = f"Report_{start_date}_to_{end_date}.xlsx" if start_date and end_date else "Report_Summary.xlsx"
            await message.reply(summary_text, file=discord.File(fp=buffer, filename=file_name))
        else:
            await message.reply(summary_text)

    except Exception as e:
        await send_clean_error(channel, e)

async def handle_incoming_message(channel, message, mode="insert"):
    async with channel.typing():
        try:
            # 1. เช็ก Cache ใน RAM ก่อน (ถ้าเจอ แปลว่าเป็น Transaction แน่นอน 0 Token)
            parsed_data = extract_multiple_items(message.content, ITEM_CACHE)
            if parsed_data:
                await process_transaction_ui(channel, message, parsed_data, mode, used_ai=False)
                return

            # 2. ส่ง AI วิเคราะห์ Intent พร้อมรับ used_model
            intent_data, used_model = await parse_intent_with_retry(message.content)
            action = intent_data.get("action")

            # 3. แยกการทำงานตาม Intent จริง
            if action == "query":
                await process_query_ui(channel, message)

            elif action == "set_budget":
                budget_amount = intent_data.get("budget_amount")
                if budget_amount:
                    set_user_budget(message.author.id, float(budget_amount))
                    await channel.send(f"🎯 ตั้งงบประมาณเรียบร้อย: {float(budget_amount):,.2f} บาท\n*(⚡ Model: `{used_model}`)*")
                else:
                    await channel.send("❌ ไม่สามารถระบุตัวเลขงบประมาณได้ กรุณาลองใหม่อีกครั้ง")

            else:
                parsed_data = {"transactions": intent_data.get("transactions", [])}
                if not parsed_data["transactions"]:
                    return

                for item in parsed_data["transactions"]:
                    name = item.get("item_name", "").strip().lower()
                    if name:
                        ITEM_CACHE[name] = {
                            "category": item.get("category", "ค่าสินค้า"),
                            "transaction_type": item.get("transaction_type", "รายจ่าย")
                        }
                await process_transaction_ui(channel, message, parsed_data, mode, used_ai=True, used_model=used_model)

        except Exception as e:
            await send_clean_error(channel, e)

@client.event
async def on_message(message):
    if message.type not in (discord.MessageType.default, discord.MessageType.reply): 
        return
    if message.author.id != ALLOWED_USER_ID or message.author == client.user: 
        return

    text = message.content.strip()

    # 1. ข้ามทันทีถ้าเป็นคำสั่งของ workspace_bot (!dev)
    if text.lower().startswith("!dev"):
        return

    # 2. ข้ามทันทีถ้าพิมพ์อยู่ในห้อง codex
    if hasattr(message.channel, "name") and "codex" in message.channel.name.lower():
        return

    if text.lower() == "!menu":
        await message.channel.send("⚡ เมนูด่วน", view=QuickActionView())
        return

    await handle_incoming_message(message.channel, message, mode="insert")

@client.event
async def on_message_edit(before, after):
    if after.type not in (discord.MessageType.default, discord.MessageType.reply): 
        return
    if after.author.id != ALLOWED_USER_ID or after.author == client.user: 
        return
    if before.content == after.content: 
        return

    # ข้ามข้อความแก้ถ้าเป็น !dev หรืออยู่ในห้อง codex
    if after.content.strip().lower().startswith("!dev"):
        return
    if hasattr(after.channel, "name") and "codex" in after.channel.name.lower():
        return
    
    # ลบ UI ปุ่มเก่าย้อนหลังอัตโนมัติเมื่อกด Edit ข้อความ
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
        except Exception: 
            pass

@client.event
async def on_ready():
    print(f"[OK] Online as {client.user} | Multi-Model Auto Fallback: Active")
    load_item_cache_to_memory()
    if not daily_jobs.is_running(): 
        daily_jobs.start()

if __name__ == "__main__":
    keep_alive()
    client.run(DISCORD_TOKEN)