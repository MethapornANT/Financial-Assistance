import express from "express";
import cors from "cors";
import fs from "fs/promises";
import path from "path";
import { exec } from "child_process";
import util from "util";

const execPromise = util.promisify(exec);
const WORKSPACE_DIR = "D:\\Financial-Assistance";
const PORT = 3001;

const app = express();
app.use(cors());
app.use(express.json());

// เช็กสถานะเครื่อง
app.get("/health", (req, res) => {
  res.json({ status: "ok", workspace: WORKSPACE_DIR });
});

// อ่านไฟล์
app.post("/tools/read_file", async (req, res) => {
  try {
    const { relative_path } = req.body;
    const filePath = path.join(WORKSPACE_DIR, relative_path);
    const data = await fs.readFile(filePath, "utf-8");
    res.json({ success: true, content: data });
  } catch (err) {
    res.status(500).json({ success: false, error: err.message });
  }
});

// แก้ไข/สร้างไฟล์
app.post("/tools/apply_patch", async (req, res) => {
  try {
    const { relative_path, content } = req.body;
    const filePath = path.join(WORKSPACE_DIR, relative_path);
    await fs.writeFile(filePath, content, "utf-8");
    res.json({ success: true, message: `อัปเดตไฟล์ ${relative_path} เรียบร้อย` });
  } catch (err) {
    res.status(500).json({ success: false, error: err.message });
  }
});

// รันคำสั่ง CLI (บล็อกคำสั่งลบ)
app.post("/tools/shell_run", async (req, res) => {
  try {
    const { command } = req.body;
    const cmd = command.trim().toLowerCase();
    const forbidden = ["rm", "del", "erase", "rmdir", "remove-item", "drop", "unlink"];
    const isDangerous = forbidden.some((word) => cmd.split(" ").includes(word) || cmd.includes(` ${word} `));

    if (isDangerous) {
      return res.status(403).json({
        success: false,
        error: "ถูกบล็อก: ไม่อนุญาตให้ใช้คำสั่งลบทุกกรณี กรุณาขออนุญาตผู้ใช้ก่อน"
      });
    }

    const { stdout, stderr } = await execPromise(command, { cwd: WORKSPACE_DIR });
    res.json({ success: true, output: stdout || stderr || "รันสำเร็จ (ไม่มี output)" });
  } catch (err) {
    res.status(500).json({ success: false, error: err.message });
  }
});

app.listen(PORT, () => {
  console.log(`MCP Local Bridge รันอยู่ที่พอร์ต ${PORT}`);
});