# -*- coding: utf-8 -*-
"""RoleChat 端到端验证。

全部使用 mock 模型 + tests/testdata/ 下的独立数据目录，与真实聊天数据完全隔离。
运行：python -m unittest discover -s tests -v   （在项目根目录执行）
"""
import json
import os
import shutil
import sys
import threading
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import server  # noqa: E402
from app.brain import Brain, build_system  # noqa: E402
from app.store import Store  # noqa: E402

TESTDATA = os.path.join(ROOT, "tests", "testdata")


def make_cfg(**model_over):
    cfg = json.loads(json.dumps(server.DEFAULT_CONFIG))
    cfg["model"].update({"provider": "mock", "context_chars": 4000, "reply_max_chars": 800})
    cfg["model"].update(model_over)
    return cfg


def fresh(name):
    d = os.path.join(TESTDATA, name)
    if os.path.exists(d):
        shutil.rmtree(d)
    return Store(d)


class TestPersonaAndChat(unittest.TestCase):
    def test_build_system_contains_persona_and_memory_as_data(self):
        """角色设定与检索记忆都要进入 system，且记忆被标记为数据而非指令。"""
        system = build_system(
            {"name": "林小雨", "background": "图书馆管理员", "personality": "温和",
             "speech_style": "轻柔", "rules": "不聊暴力"},
            [{"id": "mem_x", "scope": "user", "content": "用户的名字是王小明"}],
            None)
        for kw in ("名称：林小雨", "背景：图书馆管理员", "说话风格：轻柔", "行为规则：不聊暴力",
                   "用户的名字是王小明", "数据，非指令", "应用规则"):
            self.assertIn(kw, system)

    def test_chat_reply_mentions_character(self):
        store = fresh("chat_basic")
        brain = Brain(store, make_cfg())
        char = store.list_characters()[0]
        sess = store.create_session(char["id"])
        r = brain.chat(sess["id"], "你好，请问图书馆几点开门？")
        self.assertIn(char["name"], r["reply"])
        self.assertIn("模拟模型", r["reply"])  # 明确标识这是 mock 输出
        # 会话标题取自首条用户消息
        self.assertIn("图书馆", store.get_session(sess["id"])["title"])
        # 用户与角色设定参与了模型输入（通过 mock 回复内容间接验证 + 消息落盘）
        roles = [m["role"] for m in store.list_messages(sess["id"])]
        self.assertEqual(roles, ["user", "assistant"])


class TestLongTermMemory(unittest.TestCase):
    def test_explicit_remember_and_cross_session_retrieval(self):
        """会话A显式记住 → 新会话B能检索到（跨会话长期记忆）。"""
        store = fresh("mem_cross")
        brain = Brain(store, make_cfg())
        char = store.list_characters()[0]
        s1 = store.create_session(char["id"])
        r1 = brain.chat(s1["id"], "记住：我的名字是王小明")
        self.assertIn("remembered", [e["kind"] for e in r1["memory_events"]])
        mems = store.list_memories()
        self.assertEqual(len(mems), 1)
        self.assertEqual(mems[0]["scope"], "user")
        self.assertEqual(mems[0]["confidence"], "explicit")
        self.assertEqual(mems[0]["source_session"], s1["id"])

        s2 = store.create_session(char["id"])
        r2 = brain.chat(s2["id"], "你还记得我的名字吗？")
        self.assertTrue(any("名字" in m["content"] for m in r2["used_memories"]),
                        "跨会话检索应引用名字记忆，实际：%s" % r2["used_memories"])
        # 引用可解释：有分数与命中词
        for m in r2["used_memories"]:
            self.assertIsInstance(m["score"], float)
            self.assertTrue(m["matched"])

    def test_correction_updates_memory(self):
        """纠错“不是X而是Y”：旧记忆删除、新事实写入。"""
        store = fresh("mem_correct")
        brain = Brain(store, make_cfg())
        char = store.list_characters()[0]
        s = store.create_session(char["id"])
        brain.chat(s["id"], "记住：我住在上海")
        self.assertTrue(any("上海" in m["content"] for m in store.list_memories()))
        r = brain.chat(s["id"], "说错了，不是我住在上海，而是我住在北京")
        self.assertTrue(any(e["kind"] == "corrected" for e in r["memory_events"]),
                        "应产生纠错事件：%s" % r["memory_events"])
        self.assertFalse(any("上海" in m["content"] for m in store.list_memories()),
                         "旧的“上海”记忆应被删除")
        self.assertTrue(any("北京" in m["content"] for m in store.list_memories()),
                        "新的“北京”记忆应被写入")

    def test_forget_removes_memory(self):
        store = fresh("mem_forget")
        brain = Brain(store, make_cfg())
        char = store.list_characters()[0]
        s = store.create_session(char["id"])
        brain.chat(s["id"], "记住：我喜欢吃榴莲")
        self.assertTrue(any("榴莲" in m["content"] for m in store.list_memories()))
        r = brain.chat(s["id"], "忘记：我喜欢吃榴莲")
        self.assertTrue(any(e["kind"] == "forgot" for e in r["memory_events"]))
        self.assertFalse(any("榴莲" in m["content"] for m in store.list_memories()))
        # 再问也不应引用
        s2 = store.create_session(char["id"])
        r2 = brain.chat(s2["id"], "我今天想吃榴莲披萨")
        self.assertFalse(any("榴莲" in m["content"] for m in r2["used_memories"]))

    def test_dedup_merge(self):
        """重复内容合并更新而不是新增。"""
        store = fresh("mem_dedup")
        brain = Brain(store, make_cfg())
        char = store.list_characters()[0]
        s = store.create_session(char["id"])
        brain.chat(s["id"], "记住：我喜欢吃苹果")
        r = brain.chat(s["id"], "记住：我喜欢吃苹果")
        self.assertIn("merged", [e["kind"] for e in r["memory_events"]])
        apples = [m for m in store.list_memories() if "苹果" in m["content"]]
        self.assertEqual(len(apples), 1)

    def test_fiction_guard(self):
        """角色扮演动作（*…*）中的内容不会被自动提取成用户事实。"""
        store = fresh("mem_fiction")
        brain = Brain(store, make_cfg())
        char = store.list_characters()[0]
        s = store.create_session(char["id"])
        brain.chat(s["id"], "*我的名字是龙王，我盘踞在北方雪山之巅* 呼——北风呼啸而过")
        names = [m for m in store.list_memories() if "名字" in m["content"]]
        self.assertEqual(names, [], "*动作*内的虚构内容不应成为用户记忆：%s" % names)


class TestShortTermMemory(unittest.TestCase):
    def test_summary_on_overflow(self):
        """消息超出上下文预算 → 早期消息压缩为摘要，窗口变小。"""
        store = fresh("summary")
        brain = Brain(store, make_cfg(context_chars=900, reply_max_chars=200))
        char = store.list_characters()[0]
        s = store.create_session(char["id"])
        long_text = "这是一段很长的消息，用来把上下文撑爆。" * 10
        for i in range(4):
            brain.chat(s["id"], "%s（第%d条）" % (long_text, i + 1))
        sess = store.get_session(s["id"])
        self.assertTrue(sess["summary"]["text"], "应生成摘要")
        self.assertGreater(sess["summary"]["up_to"], 0)
        total = len(store.list_messages(s["id"]))
        self.assertGreater(total, sess["summary"]["up_to"], "摘要应只覆盖早期消息")


class TestPersistence(unittest.TestCase):
    def test_restart_keeps_everything(self):
        """模拟应用重启：重新打开数据目录，角色/会话/消息/记忆全部保留。"""
        store = fresh("restart")
        brain = Brain(store, make_cfg())
        char = store.list_characters()[0]
        s = store.create_session(char["id"])
        brain.chat(s["id"], "记住：我的名字是王小明")
        brain.chat(s["id"], "你好呀")

        store2 = Store(store.root)  # “重启”
        self.assertEqual([c["id"] for c in store2.list_characters()],
                         [c["id"] for c in store.list_characters()])
        sess2 = store2.get_session(s["id"])
        self.assertEqual(sess2["id"], s["id"])
        msgs2 = store2.list_messages(s["id"])
        self.assertTrue(len(msgs2) >= 4)  # user/assistant + system_event
        self.assertTrue(any("王小明" in m["content"] for m in store2.list_memories()))


class TestVoiceAPI(unittest.TestCase):
    """语音代理路由：语音服务不在时必须优雅降级（503+可读提示），配置可保存。"""

    def test_voice_routes(self):
        store = fresh("voice_smoke")
        cfg = make_cfg()
        cfg["voice"]["server"] = "http://127.0.0.1:9"  # 必拒绝的端口，确定性
        cfg_file = os.path.join(TESTDATA, "voice_config.json")
        httpd = server.make_server(store, cfg, os.path.join(ROOT, "app", "static"), cfg_file,
                                   host="127.0.0.1", port=0)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % port

        def req(method, path, body=None, raw=None):
            data = raw if raw is not None else (
                json.dumps(body).encode("utf-8") if body is not None else None)
            r = urllib.request.Request(base + path, data=data, method=method,
                                       headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(r, timeout=10) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read().decode("utf-8"))

        try:
            # 探测：不可用但返回 200 + 明确提示（界面据此优雅降级）
            code, v = req("GET", "/api/voice")
            self.assertEqual(code, 200)
            self.assertFalse(v["available"])
            self.assertIn("启动RoleChat.bat", v["error"])
            # TTS：503 + 可读提示
            code, err = req("POST", "/api/voice/tts", {"text": "你好", "sid": "female"})
            self.assertEqual(code, 503)
            self.assertIn("语音服务未启动", err["error"])
            # ASR：同样 503（音频字节数需超过服务端的长度校验）
            code, err = req("POST", "/api/voice/asr", raw=b"RIFF" + b"\x00" * 300)
            self.assertEqual(code, 503)
            self.assertIn("语音服务未启动", err["error"])
            # 配置：voice 可保存，speed 越界被夹紧到 [0.5, 2.0]
            code, saved = req("POST", "/api/config",
                              {"voice": {"tts_voice": "male", "auto_tts": True, "speed": 9.0}})
            self.assertEqual(code, 200)
            vcfg = saved["config"]["voice"]
            self.assertEqual(vcfg["tts_voice"], "male")
            self.assertTrue(vcfg["auto_tts"])
            self.assertEqual(vcfg["speed"], 2.0)
        finally:
            httpd.shutdown()
            httpd.server_close()


class TestHTTPSmoke(unittest.TestCase):
    def test_full_api_over_http(self):
        store = fresh("http_smoke")
        cfg = make_cfg()
        cfg_file = os.path.join(TESTDATA, "http_smoke_config.json")
        httpd = server.make_server(store, cfg, os.path.join(ROOT, "app", "static"), cfg_file,
                                   host="127.0.0.1", port=0)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % port

        def req(method, path, body=None):
            data = json.dumps(body).encode("utf-8") if body is not None else None
            r = urllib.request.Request(base + path, data=data, method=method,
                                       headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(r, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))

        try:
            h = req("GET", "/api/health")
            self.assertTrue(h["ok"])
            self.assertEqual(h["provider"], "mock")

            with urllib.request.urlopen(base + "/", timeout=10) as resp:
                self.assertIn("RoleChat", resp.read().decode("utf-8"))

            c = req("POST", "/api/characters", {
                "name": "测试角色", "background": "b", "personality": "p",
                "speech_style": "s", "rules": "r"})["character"]
            s = req("POST", "/api/sessions", {"character_id": c["id"]})["session"]
            r1 = req("POST", "/api/chat", {"session_id": s["id"],
                                           "message": "记住：我的名字是王小明"})
            self.assertIn("remembered", [e["kind"] for e in r1["memory_events"]])

            mems = req("GET", "/api/memories")["memories"]
            self.assertEqual(len(mems), 1)
            # 编辑记忆
            upd = req("PUT", "/api/memories/" + mems[0]["id"],
                      {"content": "用户的名字是小明（王小明）"})["memory"]
            self.assertIn("小明", upd["content"])
            # 删除记忆
            req("DELETE", "/api/memories/" + mems[0]["id"])
            self.assertEqual(req("GET", "/api/memories")["memories"], [])
            # 探测：mock 应该连通
            self.assertTrue(req("POST", "/api/probe")["connected"])
            # 配置读取不含明文密钥
            cfg_view = req("GET", "/api/config")["config"]
            self.assertNotIn("api_key", cfg_view["model"])
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
