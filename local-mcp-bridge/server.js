import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import fs from "fs/promises";
import path from "path";
import { exec } from "child_process";
import util from "util";

const execPromise = util.promisify(exec);
const WORKSPACE_DIR = "D:\\Financial-Assistance";

const server = new Server(
  { name: "local-workspace-bridge", version: "1.0.0" },
  { capabilities: { tools: {} } }
);

// 1. ลงทะเบียนรายการ Tools
server.setRequestHandler(ListToolsRequestSchema, async () => {
  return {
    tools: [
      {
        name: "read_file",
        description: "อ่านเนื้อหาไฟล์ในโปรเจกต์",
        inputSchema: {
          type: "object",
          properties: {
            relative_path: { type: "string", description: "Path ไฟล์สัมพัทธ์จากโปรเจกต์" }
          },
          required: ["relative_path"]
        }
      },
      {
        name: "apply_patch",
        description: "เขียนหรือแก้ไขไฟล์ในโปรเจกต์ (ห้ามใช้ลบ)",
        inputSchema: {
          type: "object",
          properties: {
            relative_path: { type: "string", description: "Path ไฟล์ที่จะเขียนทับ/สร้างใหม่" },
            content: { type: "string", description: "เนื้อหาโค้ดใหม่" }
          },
          required: ["relative_path", "content"]
        }
      },
      {
        name: "shell_run",
        description: "รันคำสั่ง terminal (ระบบจะบล็อกคำสั่งลบทุกชนิด)",
        inputSchema: {
          type: "object",
          properties: {
            command: { type: "string", description: "คำสั่ง CLI เช่น git status, python main.py" }
          },
          required: ["command"]
        }
      }
    ]
  };
});

// 2. จัดการคำสั่งและ Guardrail ป้องกันการลบ
server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;

  try {
    if (name === "read_file") {
      const filePath = path.join(WORKSPACE_DIR, args.relative_path);
      const data = await fs.readFile(filePath, "utf-8");
      return { content: [{ type: "text", text: data }] };
    }

    if (name === "apply_patch") {
      const filePath = path.join(WORKSPACE_DIR, args.relative_path);
      await fs.writeFile(filePath, args.content, "utf-8");
      return { content: [{ type: "text", text: `อัปเดตไฟล์ ${args.relative_path} เรียบร้อยแล้ว` }] };
    }

    if (name === "shell_run") {
      const cmd = args.command.trim().toLowerCase();
      // Guardrail ดักคำสั่งลบ
      const forbidden = ["rm", "del", "erase", "rmdir", "remove-item", "drop", "unlink"];
      const isDangerous = forbidden.some((word) => cmd.split(" ").includes(word) || cmd.includes(` ${word} `));

      if (isDangerous) {
        return {
          isError: true,
          content: [{ type: "text", text: "ถูกบล็อก: ไม่อนุญาตให้ใช้คำสั่งลบทุกกรณี กรุณาขออนุญาตผู้ใช้ก่อน" }]
        };
      }

      const { stdout, stderr } = await execPromise(args.command, { cwd: WORKSPACE_DIR });
      return { content: [{ type: "text", text: stdout || stderr || "รันสำเร็จ (ไม่มี output)" }] };
    }

    throw new Error(`ไม่พบ Tool ชื่อ: ${name}`);
  } catch (error) {
    return { isError: true, content: [{ type: "text", text: error.message }] };
  }
});

const transport = new StdioServerTransport();
await server.connect(transport);