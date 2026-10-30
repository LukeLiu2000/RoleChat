// RoleChat 界面自动化测试：用 jsdom 加载真实页面、执行真实 app.js、连接真实服务器，
// 模拟用户点击「开始对话 / 发送 / 打开设置」等操作。
// 用法：先启动服务器（端口 8811），再 node ui_test.mjs
import { JSDOM } from "jsdom";

const BASE = process.env.RC_BASE || "http://127.0.0.1:8811";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitFor(fn, what, timeout = 10000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    let v;
    try { v = fn(); } catch (e) { v = null; }
    if (v) return v;
    await sleep(60);
  }
  throw new Error("等待超时: " + what);
}

const pageErrors = [];
const dom = await JSDOM.fromURL(BASE + "/", {
  runScripts: "dangerously",
  resources: "usable",
  pretendToBeVisual: true,
  beforeParse(window) {
    window.fetch = (path, opt) => fetch(path.startsWith("http") ? path : BASE + path, opt);
    window.confirm = () => true;
    window.prompt = () => null;
    window.addEventListener("error", (e) => pageErrors.push(String(e.message)));
  },
});
const doc = dom.window.document;
const $ = (id) => doc.getElementById(id);

let passed = 0, failed = 0;
function check(name, cond, detail) {
  if (cond) { passed++; console.log("  [OK] " + name); }
  else { failed++; console.log("  [FAIL] " + name + (detail ? " -- " + detail : "")); }
}

console.log("RoleChat UI 测试（jsdom 连接 %s）", BASE);

// 1) 页面加载：角色列表出现、无弹窗遮挡、浅色主题、Luke Liu 署名
await waitFor(() => doc.querySelectorAll("#charList .item").length >= 1, "角色列表渲染");
check("页面加载且角色列表渲染", true);
check("三个弹窗初始均为隐藏（无遮挡）",
  ["modalChar", "modalSettings", "modalSummary"].every((id) => $(id).hidden === true));
await waitFor(() => doc.querySelector(".brand .sub").textContent.includes("v1."), "版本号显示");
check("标题显示版本号", doc.querySelector(".brand .sub").textContent.includes("v1.1"));
check("页面带 Luke Liu 署名", doc.querySelector(".brand .sub").textContent.includes("Luke Liu"));
const cssText = await (await fetch(BASE + "/static/style.css")).text();
check("浅色主题生效（CSS 底色为浅色）", /--bg:\s*#f3f5fa/.test(cssText), cssText.slice(0, 80));
check("麦克风按钮存在", !!$("btnMic"));

// 2) 未创建会话时直接点发送/点麦克风：必须有提示而不是静默
await waitFor(() => doc.querySelector("#messages .empty-hint button"), "开始对话按钮出现");
$("input").value = "你好呀";
$("btnSend").click();
await waitFor(() => $("toast").hidden === false, "发送提示出现");
check("未建会话点发送 → 给出可见提示", $("toast").textContent.length > 3, $("toast").textContent);
$("toast").hidden = true;
$("btnMic").click(); // jsdom 无 getUserMedia → 走可见提示分支（或未建会话提示）
await waitFor(() => $("toast").hidden === false, "麦克风提示出现");
check("未建会话点麦克风 → 给出可见提示", $("toast").textContent.length > 3, $("toast").textContent);
await sleep(300);
$("toast").hidden = true; // 复位

// 3) 点击「开始对话」创建会话
doc.querySelector("#messages .empty-hint button").click();
await waitFor(() => doc.querySelectorAll("#sessionList .item").length >= 1, "会话出现在列表");
check("点击「开始对话」→ 创建会话并进入聊天", true);

// 4) 发送消息 → 收到回复（mock 模型，离线）
await waitFor(() => !$("btnSend").disabled, "发送按钮可用");
$("input").value = "记住：我的名字是王小明";
$("btnSend").click();
await waitFor(() => doc.querySelectorAll(".msg.assistant").length >= 1, "收到模型回复", 15000);
const reply = doc.querySelector(".msg.assistant .bubble").textContent;
check("发送后收到回复", reply.length > 0, reply.slice(0, 40));
check("回复带「模型生成」标签", !!doc.querySelector(".msg.assistant .chip.model"));
check("回复带朗读按钮 🔊", !!doc.querySelector(".msg.assistant .spk"));

// 5) 记忆写入并在右侧面板可见
await waitFor(() => doc.querySelectorAll("#memList .memcard").length >= 1, "长期记忆写入", 10000);
check("长期记忆已写入并显示",
  [...doc.querySelectorAll("#memList .memcard .body")].some((n) => n.textContent.includes("王小明")));
await waitFor(() => doc.querySelectorAll("#refList .refcard").length >= 1, "本轮引用显示", 5000);
check("「本轮引用」面板显示引用记忆", true);

// 6) 设置弹窗开合 + 预设切换
$("btnSettings").click();
await waitFor(() => $("modalSettings").hidden === false, "设置弹窗打开");
check("设置弹窗可打开", true);
check("设置含语音配置（音色/自动朗读）", !!$("setTtsVoice") && !!$("setAutoTts"));
check("音色下拉含女声/男声", $("setTtsVoice").textContent.includes("女声")
  && $("setTtsVoice").textContent.includes("男声"));
doc.querySelector('[data-preset="anthropic"]').click();
check("预设点击生效（provider 变为 anthropic）", $("setProvider").value === "anthropic");
$("btnCfgCancel").click();
await waitFor(() => $("modalSettings").hidden === true, "设置弹窗关闭");
check("设置弹窗可关闭", true);

// 7) 会话摘要弹窗
$("btnSummary").click();
await waitFor(() => $("modalSummary").hidden === false, "摘要弹窗打开");
$("btnSummaryClose").click();
await waitFor(() => $("modalSummary").hidden === true, "摘要弹窗关闭");
check("摘要弹窗可开合", true);

check("无未捕获页面脚本错误", pageErrors.length === 0, pageErrors.join(" | "));

console.log("\n结果: %d/%d 通过 %s", passed, passed + failed, failed ? "[FAIL]" : "[ALL OK]");
dom.window.close();
process.exit(failed ? 1 : 0);
