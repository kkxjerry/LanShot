import http from "node:http";

const PORT = 18888;
const HOST = "127.0.0.1";

console.log("[AIM Daemon] Initializing Google AI Mode warm session with ego-browser...");

// 1. Initialize warm task space
const task = await taskSpace("google-aim-warm-daemon", { profileId: "Default" });
const page = task.page("p1");

let isReady = false;
let lastWarmTime = Date.now();

async function warmUp() {
  console.log("[AIM Daemon] Warming up Google AI Mode page...");
  try {
    await page.goto("https://www.google.com/search?q=test&udm=50", { 
      waitUntil: "domcontentloaded", 
      timeout: 20000 
    });
    
    // Check for captcha and click if needed
    const currentUrl = await page.url();
    if (currentUrl.includes("sorry")) {
      try {
        console.log("[AIM Daemon] Captcha detected during warmup, clicking checkbox...");
        await page.click("loc=role:checkbox[name=\"I'm not a robot\"]", { timeout: 5000 });
        await page.waitForTimeout(3000);
      } catch (e) {}
    }
    
    isReady = true;
    lastWarmTime = Date.now();
    console.log(`[AIM Daemon] Warmup complete! Page URL: ${await page.url()}`);
  } catch (err) {
    console.error("[AIM Daemon] Warmup failed:", err.message);
  }
}

await warmUp();

// Keep-alive routine every 10 minutes to prevent session expiration
setInterval(async () => {
  if (Date.now() - lastWarmTime > 10 * 60 * 1000) {
    console.log("[AIM Daemon] Performing background keep-alive ping...");
    try {
      await page.goto("https://www.google.com/search?q=ping&udm=50", { waitUntil: "commit", timeout: 15000 });
      lastWarmTime = Date.now();
    } catch (e) {}
  }
}, 60 * 1000);

// Query execution logic
async function queryAiMode(question, onChunk) {
  const startTime = Date.now();
  const targetUrl = `https://www.google.com/search?q=${encodeURIComponent(question)}&udm=50`;
  
  // Instant navigation in existing warm session
  await page.goto(targetUrl, { waitUntil: "commit", timeout: 20000 });
  
  let ttftMs = null;
  let lastText = "";
  const pollStart = Date.now();
  const maxWaitMs = 15000;
  
  while (Date.now() - pollStart < maxWaitMs) {
    const state = await page.evaluate(() => {
      // Find main answer container
      const body = document.body;
      const text = body ? body.innerText : "";
      const isSorry = window.location.href.includes("sorry");
      
      // Attempt to locate AI Mode content container
      // If full text includes known AI markers or is longer than search header
      const lines = text.split("\n").filter(l => l.trim().length > 0);
      return {
        url: window.location.href,
        isSorry: isSorry,
        title: document.title,
        text: text
      };
    });
    
    if (state.isSorry) {
      // Click captcha if it appears
      try {
        await page.click("loc=role:checkbox[name=\"I'm not a robot\"]", { timeout: 3000 });
        await page.waitForTimeout(2000);
        continue;
      } catch (e) {}
    }
    
    // Check if substantive answer content has started appearing
    if (state.text && state.text.length > 300) {
      if (!ttftMs) {
        ttftMs = Date.now() - startTime;
        if (onChunk) {
          onChunk({ type: "ttft", ms: ttftMs });
        }
      }
      
      if (state.text !== lastText) {
        const delta = state.text.slice(lastText.length);
        lastText = state.text;
        if (onChunk && delta) {
          onChunk({ type: "chunk", text: delta, full: state.text });
        }
      }
      
      // Stop condition: content is stable or sufficient
      if (Date.now() - pollStart > 3500) {
        break;
      }
    }
    
    await page.waitForTimeout(100);
  }
  
  const totalMs = Date.now() - startTime;
  lastWarmTime = Date.now();
  
  // Clean up extracted text: strip standard Google navigation header/footer
  let cleanedText = lastText;
  const lines = cleanedText.split("\n").map(l => l.trim()).filter(Boolean);
  let startIdx = 0;
  
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].includes("AI 模式对话：") || lines[i] === "AI 模式") {
      for (let j = i + 1; j < lines.length; j++) {
        const line = lines[j];
        if (line.includes("跳到主要内容") || line.includes("无障碍功能") ||
            line.includes("全部") || line.includes("图片") || line.includes("视频") ||
            line.includes("新闻") || line.includes("更多") || line.includes("登录") ||
            line.startsWith("下午") || line.startsWith("上午") || line.startsWith("您说：")) {
          continue;
        }
        // If line is repeat of question, answer starts on next line
        if (question.length > 5 && line.includes(question.slice(0, 15))) {
          startIdx = j + 1;
        } else if (line.length > 5) {
          startIdx = j;
          break;
        }
      }
      break;
    }
  }
  
  if (startIdx > 0 && startIdx < lines.length) {
    cleanedText = lines.slice(startIdx).join("\n\n").trim();
  }
  
  return {
    ttft_ms: ttftMs || totalMs,
    total_ms: totalMs,
    answer: cleanedText || lastText.trim()
  };
}

// 2. HTTP Server
const server = http.createServer(async (req, res) => {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
  
  if (req.method === "OPTIONS") {
    res.writeHead(204);
    res.end();
    return;
  }
  
  const url = new URL(req.url, `http://${HOST}:${PORT}`);
  
  if (url.pathname === "/health") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ 
      status: isReady ? "ready" : "warming", 
      warm: isReady,
      last_warm_ago_sec: Math.round((Date.now() - lastWarmTime) / 1000)
    }));
    return;
  }
  
  if (url.pathname === "/ask") {
    let body = "";
    req.on("data", chunk => { body += chunk; });
    req.on("end", async () => {
      let question = "";
      let stream = false;
      try {
        if (req.method === "POST" && body) {
          const parsed = JSON.parse(body);
          question = parsed.question || "";
          stream = Boolean(parsed.stream);
        } else {
          question = url.searchParams.get("q") || "";
          stream = url.searchParams.get("stream") === "true";
        }
      } catch (e) {
        res.writeHead(400, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: "Invalid JSON body" }));
        return;
      }
      
      if (!question.trim()) {
        res.writeHead(400, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: "question is required" }));
        return;
      }
      
      console.log(`[AIM Daemon] Processing question: "${question.slice(0, 60)}..." (stream=${stream})`);
      
      if (stream) {
        res.writeHead(200, {
          "Content-Type": "text/event-stream",
          "Cache-Control": "no-cache",
          "Connection": "keep-alive"
        });
        
        try {
          const result = await queryAiMode(question, (chunk) => {
            res.write(`data: ${JSON.stringify(chunk)}\n\n`);
          });
          res.write(`data: ${JSON.stringify({ type: "done", ...result })}\n\n`);
          res.end();
        } catch (err) {
          res.write(`data: ${JSON.stringify({ type: "error", message: err.message })}\n\n`);
          res.end();
        }
      } else {
        try {
          const result = await queryAiMode(question);
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ status: "ok", ...result }));
        } catch (err) {
          res.writeHead(500, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ error: err.message }));
        }
      }
    });
    return;
  }
  
  res.writeHead(404, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ error: "Not found" }));
});

server.listen(PORT, HOST, () => {
  console.log(`[AIM Daemon] 🚀 Google AI Mode Warm Session Daemon is listening on http://${HOST}:${PORT}`);
});

// Graceful shutdown handling
const shutdown = async () => {
  console.log("\n[AIM Daemon] Shutting down, closing server and task space...");
  server.close();
  try {
    await task.finish({ keep: [] });
  } catch (e) {}
  process.exit(0);
};

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
