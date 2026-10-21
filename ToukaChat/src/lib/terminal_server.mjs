// Touka Terminal & IDE Server (PTY + MCP Bridge + Persistence)
import { createServer } from "http";
import { Server } from "socket.io";
import pty from "node-pty";
import os from "os";
import fs from "fs";
import path from "path";
import crypto from "crypto";

const PORT = 3002;
const AUTH_TOKEN = crypto.randomBytes(16).toString("hex");
const PID = process.pid;

// ── 1. Session & PTY Storage ────────────────────────────────────────────────
// 儲存結構: sessionId -> { pty, buffer, lastSeen }
const sessions = new Map();
const MAX_BUFFER_SIZE = 10000; // 每個 session 保留 10,000 字元的歷史紀錄

// ── 2. IDE 發現機制 (Discovery) ───────────────────────────────────────
const tempDir = path.join(os.tmpdir(), "gemini", "ide");
if (!fs.existsSync(tempDir)) {
  fs.mkdirSync(tempDir, { recursive: true });
}

const discoveryFile = path.join(tempDir, `gemini-ide-server-${PID}-${PORT}.json`);
const discoveryData = {
  port: PORT,
  authToken: AUTH_TOKEN,
  workspacePath: process.cwd(),
  ideInfo: { name: "ToukaUI", displayName: "Touka AI Studio" }
};

fs.writeFileSync(discoveryFile, JSON.stringify(discoveryData));
console.log(`[IDE] Discovery file created: ${discoveryFile}`);

process.on("exit", () => fs.existsSync(discoveryFile) && fs.unlinkSync(discoveryFile));
process.on("SIGINT", () => {
  if (fs.existsSync(discoveryFile)) fs.unlinkSync(discoveryFile);
  process.exit();
});

// ── 3. HTTP Server ──────────────────────────────────────────────────────────
const httpServer = createServer((req, res) => {
  if (req.method === "POST" && req.url === "/mcp") {
    let body = "";
    req.on("data", chunk => { body += chunk; });
    req.on("end", () => {
      if (req.headers.authorization !== `Bearer ${AUTH_TOKEN}`) {
        res.writeHead(401); return res.end("Unauthorized");
      }
      handleMCPRequest(JSON.parse(body), res);
    });
  } else {
    res.writeHead(404); res.end();
  }
});

const io = new Server(httpServer, { cors: { origin: "*", methods: ["GET", "POST"] } });

// ── 4. MCP Request Handling ──────────────────────────────────────────────────
function handleMCPRequest(request, res) {
  const { method, params, id } = request;
  io.emit("ide_connection_active", { timestamp: new Date().toISOString() });

  if (method === "tools/call") {
    const { name, arguments: args } = params;
    if (name === "openDiff") {
      io.emit("ide_open_diff", { 
        filePath: args.filePath, 
        newContent: args.newContent, 
        mcpId: id,
        sessionId: args.sessionId 
      });
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ jsonrpc: "2.0", id, result: { status: "Diff opened" } }));
      return;
    }
  }
  res.writeHead(200, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ jsonrpc: "2.0", id, result: {} }));
}

// ── 5. PTY Management ───────────────────────────────────────────────────────
const shell = process.platform === 'win32' ? 'pwsh.exe' : 'bash';

io.on("connection", (socket) => {
  console.log(`[Socket] Client connected: ${socket.id}`);

  // 客戶端要求建立或加入 Session
  socket.on("join_session", (sessionId) => {
    let session = sessions.get(sessionId);

    if (!session) {
      console.log(`[PTY] Creating new session: ${sessionId}`);
      const ptyProcess = pty.spawn(shell, [], {
        name: 'xterm-color',
        cols: 80,
        rows: 24,
        cwd: process.cwd(),
        env: { 
          ...process.env, 
          GEMINI_CLI_IDE_SERVER_PORT: PORT.toString(),
          GEMINI_CLI_IDE_AUTH_TOKEN: AUTH_TOKEN 
        }
      });

      session = {
        pty: ptyProcess,
        buffer: "",
        lastSeen: Date.now()
      };

      ptyProcess.onData((data) => {
        session.buffer += data;
        if (session.buffer.length > MAX_BUFFER_SIZE) {
          session.buffer = session.buffer.slice(-MAX_BUFFER_SIZE);
        }
        io.to(sessionId).emit("data", data);
      });

      sessions.set(sessionId, session);
    } else {
      console.log(`[PTY] Re-attaching to session: ${sessionId}`);
      // 發送快取的歷史資料給剛連上的客戶端
      socket.emit("data", session.buffer);
    }

    socket.join(sessionId);
    session.lastSeen = Date.now();
  });

  socket.on("input", ({ sessionId, data }) => {
    const session = sessions.get(sessionId);
    if (session) session.pty.write(data);
  });

  socket.on("resize", ({ sessionId, cols, rows }) => {
    const session = sessions.get(sessionId);
    if (session) session.pty.resize(cols, rows);
  });

  socket.on("kill_session", (sessionId) => {
    const session = sessions.get(sessionId);
    if (session) {
      session.pty.kill();
      sessions.delete(sessionId);
      console.log(`[PTY] Session killed: ${sessionId}`);
    }
  });

  socket.on("disconnect", () => {
    console.log(`[Socket] Client disconnected: ${socket.id}`);
    // 注意：我們在這裡不殺死 PTY，這就是持久化！
  });
});

// 定期清理過期的 Session (例如超過 24 小時無人使用的)
setInterval(() => {
  const now = Date.now();
  for (const [id, session] of sessions) {
    if (now - session.lastSeen > 24 * 60 * 60 * 1000) {
      console.log(`[PTY] Cleaning up stale session: ${id}`);
      session.pty.kill();
      sessions.delete(id);
    }
  }
}, 60 * 60 * 1000);

httpServer.listen(PORT, () => {
  console.log(`[Terminal/IDE] Bridge running on http://localhost:${PORT} (Persistent Mode)`);
});
