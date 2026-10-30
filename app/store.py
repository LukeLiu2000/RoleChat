"""RoleChat 本地持久化存储。

全部数据以 JSON 文件保存在工作区数据目录（data/）内：
- characters.json      角色设定
- sessions.json        会话（含短期记忆摘要状态）
- messages/<sid>.json  各会话完整消息（短期记忆原始数据）
- memories.json        长期记忆
- logs/app.log         运行日志（只记录事件与长度，不记录完整对话）

写操作经全局锁串行化并原子落盘（先写 .tmp 再 os.replace），重启后数据完整保留。
"""
import datetime
import json
import os
import threading
import uuid


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def new_id(prefix):
    return "%s_%s" % (prefix, uuid.uuid4().hex[:10])


DEFAULT_CHARACTERS = [
    {
        "name": "林小雨",
        "background": "大学图书馆的管理员，安静细心，喜欢诗歌和旧书，熟悉馆里每个角落。",
        "personality": "温和、耐心、有点书卷气，被夸奖时会害羞。",
        "speech_style": "说话轻柔简短，喜欢引用书里的句子打比方。",
        "rules": "不讨论暴力话题；察觉用户情绪低落时会温和地安慰。",
    },
    {
        "name": "阿铁",
        "background": "健身房教练，热情外向，坚信自律改变人生。",
        "personality": "豪爽、直接、鼓励型，永远给人打气。",
        "speech_style": "句子短促有力，爱用感叹号和运动比喻。",
        "rules": "不做医疗诊断；鼓励用户健康作息。",
    },
]


class Store:
    def __init__(self, root):
        self.root = root
        self.msg_dir = os.path.join(root, "messages")
        os.makedirs(self.msg_dir, exist_ok=True)
        os.makedirs(os.path.join(root, "logs"), exist_ok=True)
        self.characters_file = os.path.join(root, "characters.json")
        self.sessions_file = os.path.join(root, "sessions.json")
        self.memories_file = os.path.join(root, "memories.json")
        self.lock = threading.RLock()
        for path in (self.characters_file, self.sessions_file, self.memories_file):
            if not os.path.exists(path):
                self._atomic_write(path, [])
        self._seed_characters()

    # ---------- 底层读写 ----------
    def _atomic_write(self, path, obj):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _load(self, path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save(self, path, obj):
        self._atomic_write(path, obj)

    def _seed_characters(self):
        with self.lock:
            if self._load(self.characters_file):
                return
            for c in DEFAULT_CHARACTERS:
                char = dict(c, id=new_id("char"), created_at=now_iso(), updated_at=now_iso())
                chars = self._load(self.characters_file)
                chars.append(char)
                self._save(self.characters_file, chars)

    # ---------- 角色 ----------
    def list_characters(self):
        return self._load(self.characters_file)

    def get_character(self, cid):
        for c in self._load(self.characters_file):
            if c["id"] == cid:
                return c
        raise KeyError("角色不存在: %s" % cid)

    def upsert_character(self, data):
        with self.lock:
            chars = self._load(self.characters_file)
            cid = data.get("id")
            fields = {k: str(data.get(k) or "").strip()
                      for k in ("name", "background", "personality", "speech_style", "rules")}
            if not fields["name"]:
                raise ValueError("角色名称不能为空")
            if cid:
                for c in chars:
                    if c["id"] == cid:
                        c.update(fields)
                        c["updated_at"] = now_iso()
                        self._save(self.characters_file, chars)
                        return c
                raise KeyError("角色不存在: %s" % cid)
            char = dict(fields, id=new_id("char"), created_at=now_iso(), updated_at=now_iso())
            chars.append(char)
            self._save(self.characters_file, chars)
            return char

    def delete_character(self, cid):
        """级联删除：该角色的会话、消息文件，以及角色范围的长期记忆。"""
        with self.lock:
            chars = self._load(self.characters_file)
            remaining = [c for c in chars if c["id"] != cid]
            if len(remaining) == len(chars):
                raise KeyError("角色不存在: %s" % cid)
            self._save(self.characters_file, remaining)
            for s in self._load(self.sessions_file):
                if s["character_id"] == cid:
                    self._delete_session_files(s["id"])
            self._save(self.sessions_file,
                       [s for s in self._load(self.sessions_file) if s["character_id"] != cid])
            self._save(self.memories_file,
                       [m for m in self._load(self.memories_file)
                        if not (m.get("scope") == "character" and m.get("character_id") == cid)])

    # ---------- 会话 ----------
    def list_sessions(self, character_id=None):
        sessions = self._load(self.sessions_file)
        if character_id:
            sessions = [s for s in sessions if s["character_id"] == character_id]
        out = []
        for s in sorted(sessions, key=lambda x: x["updated_at"], reverse=True):
            s2 = dict(s)
            s2["message_count"] = len(self._load_messages(s["id"]))
            out.append(s2)
        return out

    def get_session(self, sid):
        for s in self._load(self.sessions_file):
            if s["id"] == sid:
                return s
        raise KeyError("会话不存在: %s" % sid)

    def create_session(self, character_id, title=""):
        with self.lock:
            self.get_character(character_id)  # 校验角色存在
            sess = {
                "id": new_id("sess"),
                "character_id": character_id,
                "title": (title or "").strip() or "新会话",
                "created_at": now_iso(),
                "updated_at": now_iso(),
                # 短期记忆摘要状态：text=摘要文本，up_to=已覆盖的消息数
                "summary": {"text": None, "up_to": 0, "updated_at": None},
                "extracted_up_to": 0,  # 长期记忆提取已处理到的消息数
            }
            sessions = self._load(self.sessions_file)
            sessions.append(sess)
            self._save(self.sessions_file, sessions)
            self._atomic_write(self._msg_path(sess["id"]), [])
            return sess

    def update_session(self, sid, patch):
        with self.lock:
            sessions = self._load(self.sessions_file)
            for s in sessions:
                if s["id"] == sid:
                    s.update(patch)
                    s["updated_at"] = now_iso()
                    self._save(self.sessions_file, sessions)
                    return s
            raise KeyError("会话不存在: %s" % sid)

    def delete_session(self, sid):
        with self.lock:
            sessions = self._load(self.sessions_file)
            remaining = [s for s in sessions if s["id"] != sid]
            if len(remaining) == len(sessions):
                raise KeyError("会话不存在: %s" % sid)
            self._save(self.sessions_file, remaining)
            self._delete_session_files(sid)

    def _delete_session_files(self, sid):
        path = self._msg_path(sid)
        if os.path.exists(path):
            os.remove(path)

    # ---------- 消息 ----------
    def _msg_path(self, sid):
        return os.path.join(self.msg_dir, sid + ".json")

    def _load_messages(self, sid):
        path = self._msg_path(sid)
        if not os.path.exists(path):
            return []
        with self.lock:
            return self._load(path)

    def list_messages(self, sid):
        return self._load_messages(sid)

    def add_message(self, sid, role, content, extra=None):
        with self.lock:
            msgs = self._load_messages(sid)
            m = {"id": new_id("msg"), "session_id": sid, "role": role,
                 "content": content, "created_at": now_iso()}
            if extra:
                m.update(extra)
            msgs.append(m)
            self._save(self._msg_path(sid), msgs)
            sessions = self._load(self.sessions_file)
            for s in sessions:
                if s["id"] == sid:
                    s["updated_at"] = now_iso()
                    # 首条用户消息作为会话标题，方便历史列表辨认
                    if role == "user" and len([x for x in msgs if x["role"] == "user"]) == 1:
                        s["title"] = content[:20] or s["title"]
                    break
            self._save(self.sessions_file, sessions)
            return m

    # ---------- 长期记忆 ----------
    def list_memories(self, scope=None):
        mems = [m for m in self._load(self.memories_file)
                if m.get("status", "active") == "active"]
        if scope:
            mems = [m for m in mems if m.get("scope") == scope]
        return sorted(mems, key=lambda m: m.get("updated_at") or "", reverse=True)

    def add_memory(self, content, scope="user", character_id=None,
                   source_session=None, confidence="manual"):
        with self.lock:
            mems = self._load(self.memories_file)
            mem = {
                "id": new_id("mem"),
                "scope": scope if scope in ("user", "character") else "user",
                "character_id": character_id if scope == "character" else None,
                "content": str(content).strip(),
                "source_session": source_session,
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "confidence": confidence,  # explicit=用户明说 / heuristic=规则提取 / llm=模型提取 / manual=手动
                "status": "active",
            }
            mems.append(mem)
            self._save(self.memories_file, mems)
            return mem

    def update_memory(self, mid, patch):
        with self.lock:
            mems = self._load(self.memories_file)
            for m in mems:
                if m["id"] == mid:
                    if patch.get("content") and str(patch["content"]).strip():
                        m["content"] = str(patch["content"]).strip()
                    if patch.get("scope") in ("user", "character"):
                        m["scope"] = patch["scope"]
                        m["character_id"] = (patch.get("character_id")
                                             if patch["scope"] == "character" else None)
                    m["updated_at"] = now_iso()
                    self._save(self.memories_file, mems)
                    return m
            raise KeyError("记忆不存在: %s" % mid)

    def delete_memory(self, mid):
        """硬删除：记忆不再参与任何检索，界面上也立即消失。"""
        with self.lock:
            mems = self._load(self.memories_file)
            remaining = [m for m in mems if m["id"] != mid]
            if len(remaining) == len(mems):
                raise KeyError("记忆不存在: %s" % mid)
            self._save(self.memories_file, remaining)

    def clear_memories(self):
        with self.lock:
            n = len(self.list_memories())
            self._save(self.memories_file, [])
            return n
