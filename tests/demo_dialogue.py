# -*- coding: utf-8 -*-
"""示例对话全流程演示：角色设定、跨会话记忆检索、用户纠错、删除记忆、重启保留、摘要压缩。

运行（项目根目录）：python tests\\demo_dialogue.py
使用 mock 模型与 tests/testdata/demo 独立数据目录，不联网、不碰真实数据。
"""
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import server  # noqa: E402
from app.brain import Brain  # noqa: E402
from app.store import Store  # noqa: E402

DATA = os.path.join(ROOT, "tests", "testdata", "demo")
if os.path.exists(DATA):
    shutil.rmtree(DATA)

cfg = json.loads(json.dumps(server.DEFAULT_CONFIG))
cfg["model"].update({"provider": "mock", "context_chars": 4000, "reply_max_chars": 800})

PASSED = []


def check(name, cond, detail=""):
    mark = "✔" if cond else "✘"
    print("  %s %s %s" % (mark, name, ("- " + detail) if detail else ""))
    PASSED.append((name, bool(cond)))


print("=" * 62)
print("RoleChat 示例对话验证（mock 模型 · 数据目录：%s）" % DATA)
print("=" * 62)

store = Store(DATA)
brain = Brain(store, cfg)

print("\n[1] 选择角色并开始会话")
char = [c for c in store.list_characters() if c["name"] == "林小雨"][0]
s1 = store.create_session(char["id"])
r = brain.chat(s1["id"], "你好，请问图书馆几点开门？")
print("  用户: 你好，请问图书馆几点开门？")
print("  角色: %s" % r["reply"].splitlines()[0])
check("角色设定参与对话（回复以角色名生成）", char["name"] in r["reply"])

print("\n[2] 显式记住（应立即写入长期记忆）")
r = brain.chat(s1["id"], "记住：我的名字是王小明")
kinds = [e["kind"] for e in r["memory_events"]]
print("  记忆事件: %s" % [e["detail"] for e in r["memory_events"]])
check("写入用户记忆", "remembered" in kinds)
brain.chat(s1["id"], "记住：我住在上海")
brain.chat(s1["id"], "记住：我最近在学吉他")

print("\n[3] 记忆库当前内容")
for m in store.list_memories():
    print("   · [%s|%s] %s" % (m["scope"], m["confidence"], m["content"]))
check("共 3 条记忆", len(store.list_memories()) == 3)

print("\n[4] 跨会话检索：开一个全新会话问名字")
s2 = store.create_session(char["id"])
r = brain.chat(s2["id"], "你还记得我的名字吗？")
print("  引用记忆: %s" % [(m["content"], m["score"], m["matched"][:3]) for m in r["used_memories"]])
check("新会话检索到名字记忆", any("名字" in m["content"] for m in r["used_memories"]))

print("\n[5] 用户纠错：说我住在上海是错的，其实住在北京")
r = brain.chat(s2["id"], "说错了，不是我住在上海，而是我住在北京")
print("  记忆事件: %s" % [e["detail"] for e in r["memory_events"]])
check("旧记忆(上海)已删除", not any("上海" in m["content"] for m in store.list_memories()))
check("新记忆(北京)已写入", any("北京" in m["content"] for m in store.list_memories()))

print("\n[6] 忘记指定记忆")
r = brain.chat(s2["id"], "忘记：我最近在学吉他")
print("  记忆事件: %s" % [e["detail"] for e in r["memory_events"]])
check("吉他记忆已删除", not any("吉他" in m["content"] for m in store.list_memories()))

print("\n[7] 界面等价操作：手动编辑 + 删除记忆（直接调用存储接口）")
mem = [m for m in store.list_memories() if "名字" in m["content"]][0]
store.update_memory(mem["id"], {"content": "用户的名字是王小明（平时叫小明）"})
check("记忆编辑成功", any("小明）" in m["content"] for m in store.list_memories()))

print("\n[8] 模拟应用重启：重新打开同一数据目录")
store2 = Store(DATA)
check("重启后角色保留", len(store2.list_characters()) == len(store.list_characters()))
check("重启后会话与消息保留", len(store2.list_messages(s1["id"])) == len(store.list_messages(s1["id"])))
check("重启后记忆保留", any("小明" in m["content"] for m in store2.list_memories()))

print("\n[9] 短期记忆摘要：把上下文预算调小后继续会话")
cfg2 = json.loads(json.dumps(cfg))
cfg2["model"].update({"context_chars": 900, "reply_max_chars": 200})
brain2 = Brain(store2, cfg2)
long_text = "这是一段很长的对话内容，用来超出上下文预算。" * 10
for i in range(4):
    brain2.chat(s1["id"], "%s（第%d条）" % (long_text, i + 1))
sess = store2.get_session(s1["id"])
print("  摘要: %s" % (sess["summary"]["text"] or "（无）")[:80])
check("已生成早期摘要", bool(sess["summary"]["text"]))

print("\n" + "=" * 62)
ok = all(c for _, c in PASSED)
print("结果: %d/%d 项通过 %s" % (sum(1 for _, c in PASSED if c), len(PASSED), "✅ 全部通过" if ok else "❌ 存在失败"))
sys.exit(0 if ok else 1)
