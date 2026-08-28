import os
import subprocess
import traceback
import discord
from dotenv import load_dotenv
from google import genai
from google.genai import types

# ==========================================
# 1. SETUP & CONFIGURATION
# ==========================================
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ALLOWED_USER_ID = int(os.getenv("DISCORD_ALLOWED_USER_ID", 0))

WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# รายชื่อ Model จริงจาก API ของคุณ (เรียงจาก เล็กสุด/ประหยัดสุด -> ใหญ่สุด)
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
    
    # --- 3. กลุ่ม Pro (เก่งสุด/ใช้พลังประมวลผลสูง) ---
    "gemini-pro-latest",
    "gemini-2.5-pro",
    "gemini-3.1-pro-preview",
    "gemini-3.1-pro-preview-customtools"
]

# ==========================================
# 2. LOCAL WORKSPACE TOOLS & GUARDRAILS
# ==========================================
def tool_read_file(relative_path: str) -> str:
    """อ่านเนื้อหาไฟล์ในโปรเจกต์"""
    try:
        path = os.path.join(WORKSPACE_DIR, relative_path)
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Error reading file: {str(e)}"

def tool_apply_patch(relative_path: str, content: str) -> str:
    """เขียนหรือแก้ไขไฟล์ในโปรเจกต์ (ห้ามใช้ลบ)"""
    try:
        path = os.path.join(WORKSPACE_DIR, relative_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"อัปเดตไฟล์ {relative_path} เรียบร้อยแล้ว"
    except Exception as e:
        return f"Error updating file: {str(e)}"

def tool_shell_run(command: str) -> str:
    """รันคำสั่ง terminal บนเครื่อง (บล็อกคำสั่งลบทุกชนิด)"""
    cmd_lower = command.strip().lower()
    forbidden = ["rm ", "del ", "erase ", "rmdir ", "remove-item", "drop ", "unlink ", "format "]
    if any(kw in cmd_lower for kw in forbidden):
        return "❌ ถูกบล็อก: ไม่อนุญาตให้ใช้คำสั่งลบทุกกรณี กรุณาขออนุญาตผู้ใช้ก่อน"

    try:
        res = subprocess.run(
            command,
            shell=True,
            cwd=WORKSPACE_DIR,
            capture_output=True,
            text=True,
            timeout=60
        )
        output = res.stdout or res.stderr
        return output if output.strip() else "รันสำเร็จ (ไม่มี output)"
    except Exception as e:
        return f"Error executing shell: {str(e)}"

# ==========================================
# 3. AI MULTI-MODEL FALLBACK ENGINE
# ==========================================
async def execute_workspace_agent(user_prompt: str, chat_history: str) -> str:
    """ส่งคำสั่งให้ Gemini พร้อมประวัติแชท และวนลูปทดสอบทุกโมเดลตามคิว"""
    config = types.GenerateContentConfig(
        system_instruction="""คุณเป็น AI ช่วยเขียนโค้ดและจัดการโปรเจกต์
กฎสำคัญ:
1. อ้างอิงชื่อไฟล์และบริบทจาก [ประวัติการสนทนาล่าสุด] เสมอ ไม่ต้องรอให้ผู้ใช้บอกชื่อไฟล์ซ้ำ
2. หากผู้ใช้สั่ง "เพิ่ม" หรือ "แก้ไข" โค้ด ห้ามเขียนทับโค้ดเดิมทั้งหมดเด็ดขาด! ให้เรียกใช้ tool_read_file เพื่ออ่านโค้ดเดิมออกมาก่อน รวมโค้ดเก่าเข้ากับของใหม่ให้สมบูรณ์ แล้วค่อยใช้ tool_apply_patch บันทึก
3. เครื่องมือที่มี: tool_read_file, tool_apply_patch, tool_shell_run""",
        tools=[tool_read_file, tool_apply_patch, tool_shell_run],
        temperature=0.1
    )
    
    full_prompt = f"[ประวัติการสนทนาล่าสุด]\n{chat_history}\n\n[คำสั่งปัจจุบัน]\n{user_prompt}"
    failed_models = []
    
    for model_name in AI_MODELS:
        try:
            print(f"[Engine] Trying model: {model_name}...")
            response = await ai_client.aio.models.generate_content(
                model=model_name,
                contents=full_prompt,
                config=config
            )
            text = response.text.strip() if response.text else "ทำงานสำเร็จ"
            return f"{text}\n\n*(⚡ Model: `{model_name}`)*"
        except Exception as e:
            err_msg = str(e)
            print(f"[Fallback] {model_name} failed. Skipping...")
            failed_models.append(f"• `{model_name}`: {err_msg[:50]}")
            continue
            
    return "❌ ทุกโมเดลล้มเหลว\n" + "\n".join(failed_models)

# ==========================================
# 4. DISCORD SETUP (Prefix !dev)
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_message(message):
    if message.type not in (discord.MessageType.default, discord.MessageType.reply): 
        return
    if message.author.id != ALLOWED_USER_ID or message.author == client.user: 
        return

    # ทำงานเฉพาะเมื่ออยู่ในห้อง codex เท่านั้น
    is_codex_room = hasattr(message.channel, "name") and "codex" in message.channel.name.lower()
    if not is_codex_room:
        return

    prompt = message.content.strip()
    if not prompt:
        return

    async with message.channel.typing():
        try:
            # ดึงประวัติแชท 5 ข้อความก่อนหน้าในห้อง codex
            history_msgs = []
            async for msg in message.channel.history(limit=6, before=message):
                if msg.author == client.user:
                    clean_text = msg.content.split("\n\n*(⚡ Model:")[0]
                    history_msgs.append(f"AI: {clean_text}")
                elif msg.author.id == ALLOWED_USER_ID:
                    history_msgs.append(f"User: {msg.content.strip()}")

            history_msgs.reverse()
            chat_history = "\n".join(history_msgs) if history_msgs else "ไม่มีประวัติก่อนหน้า"

            res = await execute_workspace_agent(prompt, chat_history)

            if len(res) > 1900:
                res = res[:1900] + "\n...(ข้อมูลยาวเกินกำหนด)"
            await message.reply(res)
        except Exception as e:
            traceback.print_exc()
            await message.reply(f"❌ Error: {str(e)}")
            
@client.event
async def on_ready():
    print(f"[OK] Workspace Agent Online as {client.user}")
    print(f"[LIST] Loaded {len(AI_MODELS)} Models in active queue.")

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)