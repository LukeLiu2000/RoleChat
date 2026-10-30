"""长期记忆引擎：提取、合并去重、纠错、遗忘，以及可解释的相关性检索。

检索为什么先用关键词打分而不是向量：
- 类 BM25 的词元重合打分完全可解释——每条被引用的记忆都能给出分数与命中词，
  便于验证、调试与审计；个人规模（几百条记忆）下可靠性足够。
- 如需升级向量检索：嵌入模型可用 Ollama 的 bge-m3 / nomic-embed-text，
  索引保存于 data/memory_index.json，余弦相似度本地计算；
  嵌入服务不可用时自动回退到本模块的关键词检索（回退方法已在代码层预留）。
"""
import datetime
import math
import re

# ---------------- 分词 ----------------
_CJK_RE = re.compile(r"[一-鿿]")
_WORD_RE = re.compile(r"[a-z0-9]{2,}")
_STOP = set("的了吗呢啊吧呀哦嘛我你他她它们是在有和也很与及了对嗯哈") | {
    "the", "and", "you", "are", "this", "that", "with", "for",
}


def tokenize(text):
    """中文拆为单字+相邻二字词，英文拆为单词；去停用词。"""
    text = (text or "").lower()
    words = [w for w in _WORD_RE.findall(text) if w not in _STOP]
    chars = _CJK_RE.findall(text)
    singles = [c for c in chars if c not in _STOP]
    bigrams = [a + b for a, b in zip(chars, chars[1:])]
    return words + singles + bigrams


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ---------------- 角色扮演防串扰：把 *动作*、引语等虚构成分剥离后再提取事实 ----------------
def strip_roleplay(text):
    text = re.sub(r"\*[^*\n]{0,300}\*", " ", text)      # *挥剑砍向恶龙*
    text = re.sub(r"＊[^＊\n]{0,300}＊", " ", text)
    text = re.sub(r"（[^）\n]{0,80}）", " ", text)        # （笑了笑）
    return text.strip()


# ---------------- 显式指令：记住 / 忘记（优先级最高） ----------------
_REMEMBER_RE = re.compile(r"^(?:请|帮我|麻烦)?(?:你)?记住[：:,，]?\s*(.+)")
_FORGET_RE = re.compile(r"^(?:请)?(?:忘记|忘掉|删掉记忆|删除记忆|不要再记)[：:,，]?\s*(.+)")


def parse_explicit(text):
    """识别以“记住/忘记”开头的显式指令。返回动作列表。"""
    text = (text or "").strip()
    if not text:
        return []
    m = _FORGET_RE.match(text)
    if m:
        q = m.group(1).strip().rstrip("。.!！?？")
        if q in ("全部", "所有", "所有记忆", "全部记忆"):
            return [{"type": "forget_all"}]
        if q:
            return [{"type": "forget", "query": q}]
        return []
    m = _REMEMBER_RE.match(text)
    if m:
        content = m.group(1).strip().rstrip("。.!！")
        if not content:
            return []
        if content in ("这一点", "这个", "它", "这些"):
            return [{"type": "extract_now"}]
        cm = re.match(r"^关于(?:这个)?角色[：:,，]?\s*(.+)$", content)
        if cm:
            return [{"type": "remember", "content": cm.group(1).strip(), "scope": "character"}]
        return [{"type": "remember", "content": content, "scope": "user"}]
    return []


# ---------------- 纠错检测 ----------------
_CORRECTION_MARKERS = ("不对", "说错了", "记错了", "搞错了", "更正", "不是这样的")
_RE_NOT_BUT = re.compile(r"不是([^，。,！？!?]{1,30})[，,]?而是([^，。,！？!?]{1,30})")
_RE_NOT_ONLY = re.compile(r"不是([^，。,！？!?]{1,30})")


def parse_correction(text):
    """检测“说错了，不是X而是Y”类纠错。返回 [{wrong, right}]。"""
    if not any(k in text for k in _CORRECTION_MARKERS):
        return []
    m = _RE_NOT_BUT.search(text)
    if m:
        return [{"wrong": m.group(1).strip(), "right": m.group(2).strip()}]
    m = _RE_NOT_ONLY.search(text)
    if m:
        g = m.group(1).strip()
        if g and g not in ("这样的", "吧", "吧？"):
            return [{"wrong": g, "right": ""}]
    return []


def strip_corrections(text):
    """删除纠错从句，避免启发式把被纠正的旧值再次提取成记忆。"""
    if not any(k in text for k in _CORRECTION_MARKERS):
        return text
    text = _RE_NOT_BUT.sub(lambda m: m.group(2), text)
    text = _RE_NOT_ONLY.sub(" ", text)
    return text


# ---------------- 启发式事实提取（离线可用；LLM 提取的兜底） ----------------
_NAME_RE = re.compile(r"(?:我叫|我的名字叫|我的名字是|叫我)\s*([一-鿿A-Za-z0-9·]{1,12})")
_LIKE_RE = re.compile(r"我(?:很)?(?:喜欢|喜爱|偏好)\s*([^，。,！？!?；;]{1,20})")
_DISLIKE_RE = re.compile(r"我(?:很)?(?:讨厌|不喜欢|反感)\s*([^，。,！？!?；;]{1,20})")
_JOB_RE = re.compile(r"我是一名?(学生|老师|医生|护士|工程师|程序员|设计师|律师|会计|"
                     r"司机|厨师|作家|研究员|分析师|经理|创业者|上班族|自由职业者)")
_DOING_RE = re.compile(r"我(?:正在|在)(学|学习|做|写|开发|准备|读|研究)\s*([^，。,！？!?；;]{1,20})")
_LIVE_RE = re.compile(r"(?:我|本人)(?:其实|现在|目前|是)?住在([^，。,！？!?；;]{1,15})")
_AGE_RE = re.compile(r"我(?:今年)?(\d{1,3})岁")
_PET_RE = re.compile(r"我(?:养|有)(?:一只|一个|两条|两只)?(猫|狗|兔子|仓鼠|鹦鹉|乌龟)")
_MINE_RE = re.compile(r"我的(生日|家乡|公司|学校|专业|爱好|座右铭)是?([^，。,！？!?；;]{1,30})")


def heuristic_extract(text):
    """从用户消息中提取可能是长期事实的内容。只处理剥离了角色扮演成分的文本。

    明确保守：宁缺毋滥，拿不准的不提取（confidence 标记为 heuristic，界面可查）。
    """
    cleaned = strip_corrections(strip_roleplay(text))
    if len(cleaned) < 3:
        return []
    found = []

    def add(content):
        if content and content not in [f["content"] for f in found]:
            found.append({"content": content, "scope": "user", "confidence": "heuristic"})

    m = _NAME_RE.search(cleaned)
    if m:
        add("用户的名字是%s" % m.group(1))
    m = _LIKE_RE.search(cleaned)
    if m:
        add("用户喜欢%s" % m.group(1))
    m = _DISLIKE_RE.search(cleaned)
    if m:
        add("用户不喜欢%s" % m.group(1))
    m = _JOB_RE.search(cleaned)
    if m:
        add("用户的职业是%s" % m.group(1))
    m = _DOING_RE.search(cleaned)
    if m:
        add("用户正在%s%s" % (m.group(1), m.group(2)))
    m = _LIVE_RE.search(cleaned)
    if m:
        add("用户住在%s" % m.group(1))
    m = _AGE_RE.search(cleaned)
    if m:
        add("用户的年龄是%s岁" % m.group(1))
    m = _PET_RE.search(cleaned)
    if m:
        add("用户养了%s" % m.group(1))
    m = _MINE_RE.search(cleaned)
    if m:
        add("用户的%s是%s" % (m.group(1), m.group(2)))
    return found


# ---------------- 可解释检索（类 BM25） ----------------
def select_for_prompt(query_text, memories, character_id=None, top_k=5, min_score=0.15):
    """按查询词与记忆词元的重合度打分，返回 top_k 条 [{id,content,scope,score,matched}]。

    score = Σ idf(命中词) / √(记忆词数) + 角色匹配加成 + 近期加成
    """
    qtokens = list(dict.fromkeys(tokenize(query_text)))
    if not qtokens or not memories:
        return []
    df = {}
    tok_sets = {}
    for m in memories:
        ts = set(tokenize(m["content"]))
        tok_sets[m["id"]] = ts
        for t in ts:
            df[t] = df.get(t, 0) + 1
    total = len(memories)
    now = datetime.datetime.now()
    scored = []
    for m in memories:
        ts = tok_sets[m["id"]]
        matched = [t for t in qtokens if t in ts]
        if not matched:
            continue
        s = sum(math.log(1 + total / (1 + df[t])) for t in matched) / math.sqrt(len(ts) + 1)
        if m.get("scope") == "character" and character_id and m.get("character_id") == character_id:
            s += 0.35  # 与当前角色绑定的记忆轻微加权
        try:
            days = (now - datetime.datetime.fromisoformat(m["updated_at"])).days
        except (KeyError, ValueError, TypeError):
            days = 999
        s += 0.1 * math.exp(-max(days, 0) / 30)  # 一个月内的新记忆轻微加权
        if s >= min_score:
            matched.sort(key=lambda t: -1.0 / (1 + df.get(t, 0)))
            scored.append({"id": m["id"], "content": m["content"], "scope": m.get("scope"),
                           "character_id": m.get("character_id"),
                           "score": round(s, 3), "matched": matched[:6]})
    scored.sort(key=lambda x: -x["score"])
    return scored[:top_k]


# ---------------- 合并去重 / 遗忘 ----------------
def merge_items(store, items, session_id, character_id, threshold=0.55):
    """把提取到的记忆合并进存储：与已有条目相似度过高则更新而非新增。"""
    events = []
    for it in items:
        content = str(it.get("content", "")).strip()
        if not content or len(content) > 300:
            continue
        scope = it.get("scope") if it.get("scope") in ("user", "character") else "user"
        confidence = it.get("confidence") or "llm"
        new_tokens = set(tokenize(content))
        dup = None
        for m in store.list_memories(scope=scope):
            if scope == "character" and m.get("character_id") != character_id:
                continue
            if content == m["content"] or jaccard(new_tokens, set(tokenize(m["content"]))) >= threshold:
                dup = m
                break
        if dup:
            store.update_memory(dup["id"], {"content": content, "scope": scope})
            events.append({"kind": "merged",
                           "detail": "与已有记忆相似，已合并更新：%s" % content})
        else:
            store.add_memory(content, scope=scope,
                             character_id=character_id if scope == "character" else None,
                             source_session=session_id, confidence=confidence)
            events.append({"kind": "remembered",
                           "detail": "新增%s记忆：%s" % ("用户" if scope == "user" else "角色", content)})
    return events


def apply_forget(store, query):
    """按内容包含或词元相似度删除记忆，返回被删除的条目。"""
    q = (query or "").strip()
    if not q:
        return []
    qtokens = set(tokenize(q))
    deleted = []
    for m in store.list_memories():
        hit = q in m["content"] or (len(m["content"]) >= 4 and m["content"] in q)
        if not hit:
            hit = jaccard(qtokens, set(tokenize(m["content"]))) >= 0.25
        if hit:
            store.delete_memory(m["id"])
            deleted.append(m)
    return deleted
