import json
import os
import re
import shutil
import time
from datetime import datetime, timedelta

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_memory_curve"
PLUGIN_VERSION = "1.0.3"
AUTHOR = "宅家大肥鱼"


def _default_data_dir() -> str:
    """没配 data_dir 时落到 AstrBot 的 plugin_data 目录，换机器也不用改配置。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        return os.path.join(get_astrbot_plugin_data_path(), PLUGIN_NAME)
    except Exception:
        # 拿不到 AstrBot 路径时的兜底，尽量不抛
        return os.path.join(os.getcwd(), "data", "plugin_data", PLUGIN_NAME)

JUDGE_SYS = "你是对话重要性判定器。只输出一行严格 JSON，不要解释、不要代码块。"

# 注意：这段模板是用正则替占位符拼的，不是 str.format，
# 所以 JSON 示例里的花括号必须是单层，写成 {{...}} 会原样发给模型导致解析失败。
JUDGE_TPL = """给下面这条用户消息打分，并按需给出摘要。

打分规则：
0-1：纯寒暄，没有实质信息（哈哈、嗯嗯、早）
2-3：普通闲聊，没有长期价值
4-5：重要信息：身份、偏好、约定、任务、明确要求记住的事

用户这条消息：__MSG__
上一轮对话：__LAST__

只输出一行严格 JSON，不要代码块，格式示例：
{"score": 4, "type": "约定", "keyword": "生日", "summary": "用户生日在三月"}
summary 用一句话概括这条消息，不超过30字；不重要就给空字符串。"""

RECALL_WORDS = ("之前", "上次", "那天", "还记得", "我们说过", "以前", "那事", "那个事", "记不记得")

# assistant 回复里出现这些词，且句子里带「我」，才算一句承诺/约定
PROMISE_WORDS = ("我会", "我帮你", "我记着", "我记得", "提醒你", "提醒我", "答应", "明天", "后天",
                 "下次", "待会", "之后我", "一会儿", "回头我")

STOP_WORDS = {"什么", "怎么", "我们", "你们", "这个", "那个", "一下", "还是", "就是", "记得"}

# 一次性任务话术：这类「要我看看/改改/重载一下」办完就没了，不该去占 L3 名额
ONEOFF_MARKS = ("检查", "看看", "看一下", "重新加载", "重载", "修", "改", "跑一遍", "试一下", "重跑",
                "回顾", "整理", "测试", "移出", "移出去", "删", "装", "下载", "贴", "发出去", "移走",
                "接下来", "下一步", "确认一下", "算一下", "报一下", "汇报", "写完", "做完")
# 长期偏好/事实的标志词，出现任何一个就不算一次性任务
STABLE_MARKS = ("喜欢", "讨厌", "最爱", "爱吃", "不吃", "忌口", "过敏", "生日", "名字", "叫我", "叫",
                "住", "习惯", "每次", "以后", "之后都", "偏好", "重要", "记住", "怕", "不要", "别",
                "必须", "一律", "从来", "一直")


def _norm(s) -> str:
    """归一化文本，用于判重：去标点空格、转小写。"""
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5]+", "", str(s or "")).lower()


def _oneoff(fact) -> bool:
    """判一条事实是不是「一次性任务」。有长期标记的一律不算。"""
    f = str(fact or "")
    if not f:
        return False
    if any(w in f for w in STABLE_MARKS):
        return False
    return any(w in f for w in ONEOFF_MARKS)


@register(
    PLUGIN_NAME,
    AUTHOR,
    "分层记忆：最近对话留窗口，旧事压摘要，需要时检索回来",
    PLUGIN_VERSION,
)
class MemoryCurve(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.root = str(cfg.get("data_dir", "") or "").strip() or _default_data_dir()
        self.judge_id = str(cfg.get("judge_provider_id", "") or "").strip()
        self.l1_max = int(cfg.get("l1_max_turns", 10) or 10)
        self.l2_hours = int(cfg.get("l2_expire_hours", 24) or 24)
        self.l3_days = int(cfg.get("l3_expire_days", 30) or 30)
        self.l3_max = int(cfg.get("l3_max_items", 20) or 20)
        self.l2_max = int(cfg.get("l2_max_items", 10) or 10)
        self.recall_n = int(cfg.get("retrieve_limit", 3) or 3)
        self.use_judge = bool(cfg.get("enable_judge", True))
        self._pending = {}
        self._last = {}
        os.makedirs(self.root, exist_ok=True)
        logger.info(f"[memory_curve] 已启动，数据目录 {self.root}")

    # ---------- 文件读写 ----------
    def _dir(self, umo: str) -> str:
        safe = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fa5]", "_", umo)[:60]
        d = os.path.join(self.root, safe)
        os.makedirs(os.path.join(d, "archive"), exist_ok=True)
        return d

    def _read(self, path: str, default):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    def _write(self, path: str, data) -> None:
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except Exception:
            # Windows 上文件偶发被占用，退化成直接覆盖
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            try:
                os.remove(tmp)
            except Exception:
                pass

    # ---------- 状态文件：记住「今天已经提升过了」 ----------
    def _state_path(self, d: str) -> str:
        return os.path.join(d, "state.json")

    def _last_day(self, umo: str, d: str) -> str:
        """从 state.json 读上次提升的日期，重载插件后也不会忘。"""
        if umo not in self._last:
            try:
                self._last[umo] = str(self._read(self._state_path(d), {}).get("last_day") or "")
            except Exception:
                self._last[umo] = ""
        return self._last[umo]

    def _mark_day(self, umo: str, d: str, today: str) -> None:
        self._last[umo] = today
        self._write(self._state_path(d), {"last_day": today})

    def _backup(self, d: str, fname: str) -> str:
        """动记忆数据前先留一份，最多留 5 份。"""
        src = os.path.join(d, fname)
        if not os.path.exists(src):
            return ""
        stem = os.path.splitext(fname)[0]
        dst = os.path.join(d, f"{stem}.bak-{datetime.now():%Y%m%d-%H%M%S}.json")
        try:
            shutil.copyfile(src, dst)
        except Exception as e:
            logger.warning(f"[memory_curve] 备份失败：{e}")
            return ""
        olds = sorted([x for x in os.listdir(d) if x.startswith(f"{stem}.bak-")])
        for old in olds[:-5]:
            try:
                os.remove(os.path.join(d, old))
            except Exception:
                pass
        return dst

    def _load(self, umo: str):
        d = self._dir(umo)
        l1 = self._read(os.path.join(d, "l1.json"), [])
        l2 = self._read(os.path.join(d, "l2.json"), [])
        l3 = self._read(os.path.join(d, "l3.json"), [])
        return d, l1, l2, l3

    def _archive(self, d: str, item: dict) -> None:
        day = datetime.now().strftime("%Y-%m-%d")
        path = os.path.join(d, "archive", f"{day}.jsonl")
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"[memory_curve] 存档失败：{e}")

    # ---------- 判定 ----------
    async def _judge(self, msg: str, last: str, umo: str = "") -> dict:
        fallback = {"score": 2, "type": "日常", "keyword": "", "summary": msg[:30]}
        if not self.use_judge:
            return fallback

        provider = None
        if self.judge_id:
            try:
                provider = self.context.get_provider_by_id(self.judge_id)
            except Exception:
                provider = None
            if provider is None:
                logger.warning(
                    f"[memory_curve] 找不到判分模型 {self.judge_id}，本轮降级为规则模式"
                    "（检查一下配置里的模型 ID）"
                )
                return fallback
        else:
            # 没配判分模型就用当前会话的主模型顶一下，别让插件一装就哑火
            try:
                provider = self.context.get_using_provider(umo or None)
            except Exception:
                provider = None
            if provider is None:
                logger.warning("[memory_curve] 没配判分模型，也没找到可用主模型，本轮降级为规则模式")
                return fallback
        # 一次性替换，避免消息内容里的占位符被二次替换
        prompt = re.sub(
            r"__MSG__|__LAST__",
            lambda m: msg[:500] if m.group(0) == "__MSG__" else last[:300],
            JUDGE_TPL,
        )
        try:
            resp = await provider.text_chat(prompt=prompt, system_prompt=JUDGE_SYS)
            text = (getattr(resp, "completion_text", "") or "").strip()
            m = re.search(r"\{.*\}", text, re.S)
            if not m:
                return fallback
            data = json.loads(m.group(0))
            score = int(data.get("score", 2))
            score = max(0, min(5, score))
            return {
                "score": score,
                "type": str(data.get("type") or "日常")[:10],
                "keyword": str(data.get("keyword") or "")[:20],
                "summary": str(data.get("summary") or "")[:60],
            }
        except Exception as e:
            logger.warning(f"[memory_curve] 判定失败：{e}")
            return fallback

    # ---------- 检索 ----------
    def _recall(self, d: str, kw: str, msg: str) -> list:
        need = any(w in msg for w in RECALL_WORDS)
        if not need and not kw:
            return []
        words = []
        if kw:
            words += [w for w in re.split(r"[\s,，。？?！!、]", str(kw)) if len(w) >= 2]
        if need:
            clean = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", " ", msg)
            for chunk in clean.split():
                if len(chunk) > 4:
                    words += [chunk[i:i + 2] for i in range(len(chunk) - 1)]
                    words += [chunk[i:i + 3] for i in range(len(chunk) - 2)]
                elif len(chunk) >= 2:
                    words.append(chunk)
        words = [w for w in dict.fromkeys(words) if w not in STOP_WORDS][:12]
        if not words:
            return []
        hits = []
        seen = set()
        adir = os.path.join(d, "archive")
        try:
            files = sorted(os.listdir(adir))[-7:]
        except Exception:
            return []
        for fn in reversed(files):
            try:
                with open(os.path.join(adir, fn), encoding="utf-8") as f:
                    for line in f:
                        try:
                            item = json.loads(line)
                        except Exception:
                            continue
                        text = str(item.get("content") or "")
                        if not text or text in seen:
                            continue
                        if any(w in text for w in words):
                            hits.append(text[:200])
                            seen.add(text)
                        if len(hits) >= self.recall_n:
                            return hits
            except Exception:
                continue
        return hits

    # ---------- L2 去重 ----------
    def _upsert2(self, l2: list, item: dict) -> None:
        """同一件事只留一条：刷新时间、分数取最高，不再堆重复。"""
        key = _norm(item.get("summary"))
        if not key:
            return
        for it in l2:
            if _norm(it.get("summary")) == key:
                it["time"] = item["time"]
                it["kw"] = item.get("kw") or it.get("kw") or ""
                it["score"] = max(int(it.get("score") or 0), int(item.get("score") or 0))
                if item.get("src"):
                    it["src"] = item["src"]
                return
        l2.append(item)

    def _dedup2(self, l2: list) -> list:
        kept = []
        seen = {}
        for it in l2:
            key = _norm(it.get("summary"))
            if not key:
                continue
            if key in seen:
                old = seen[key]
                old["score"] = max(int(old.get("score") or 0), int(it.get("score") or 0))
                if str(it.get("time", "")) > str(old.get("time", "")):
                    old["time"] = it["time"]
                if it.get("kw") and not old.get("kw"):
                    old["kw"] = it["kw"]
                continue
            seen[key] = it
            kept.append(it)
        return kept

    # ---------- L3 去重 / 提及刷新 ----------
    def _upsert3(self, l3: list, item: dict) -> None:
        """往 L3 塞一条：已有的就改内容刷新时间，没有才追加。"""
        key = _norm(item.get("fact"))
        if not key:
            return
        for it in l3:
            if _norm(it.get("fact")) == key:
                it["last"] = item["last"]
                it["score"] = max(int(it.get("score") or 0), int(item.get("score") or 4))
                return
        l3.append(item)

    def _dedup3(self, l3: list, now: datetime):
        """同一事实只留一条，时间取最新，分数取最高。返回 (新列表, 是否有变化)。"""
        merged = {}
        order = []
        changed = False
        for it in l3:
            fact = it.get("fact") or ""
            key = _norm(fact)
            if not key:
                changed = True
                continue
            if key in merged:
                old = merged[key]

                def _ts(x):
                    try:
                        return datetime.fromisoformat(x.get("last") or x.get("time"))
                    except Exception:
                        return now

                if _ts(it) > _ts(old):
                    old["last"] = it.get("last") or it.get("time")
                old["score"] = max(int(old.get("score") or 0), int(it.get("score") or 0))
                changed = True
                continue
            merged[key] = dict(it)
            order.append(key)
        return [merged[k] for k in order], changed

    def _touch_l3(self, l3: list, msg: str, recall: list):
        """本轮提到过的事实，刷新最后提及时间，避免被一刀切下架。"""
        text = (msg or "") + " " + " ".join(recall or [])
        if not text.strip():
            return l3, False
        now = datetime.now().isoformat(timespec="seconds")
        changed = False
        for it in l3:
            fact = str(it.get("fact") or "")
            keys = [str(it.get("kw") or "")]
            keys += [p for p in re.split(r"[\s,，。、；;：:]+", fact) if len(p) >= 3]
            keys += [k for k in keys if k]
            if any(k and k in text for k in keys):
                if it.get("last") != now:
                    it["last"] = now
                    changed = True
        return l3, changed

    # ---------- 维护 ----------
    def _maintain(self, umo: str, d: str, l2: list, l3: list):
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        l3, changed = self._dedup3(l3, now)

        first_today = self._last_day(umo, d) != today
        kept2 = []
        raw2_len = len(l2)
        l2 = self._dedup2(l2)
        if first_today:
            self._mark_day(umo, d, today)
            idx = {_norm(it.get("fact")): it for it in l3}
            for it in l2:
                try:
                    t = datetime.fromisoformat(it["time"])
                except Exception:
                    t = None
                if t is not None and int(it.get("score", 0)) >= 4:
                    summary = (it.get("summary") or "").strip()
                    key = _norm(summary)
                    # 一次性任务（让我看看/改改/重载）不往 L3 提，留 L2 里自然过期
                    if key and _oneoff(summary):
                        logger.info(f"[memory_curve] 跳过一次性任务，不进 L3：{summary[:20]}")
                    elif key:
                        if key in idx:
                            # 已经在 L3 里了，只刷新时间，不复制一份
                            idx[key]["last"] = now.isoformat(timespec="seconds")
                            idx[key]["score"] = max(int(idx[key].get("score") or 0),
                                                    int(it.get("score") or 4))
                        else:
                            nf = {
                                "fact": summary,
                                "kw": it.get("kw", ""),
                                "time": now.isoformat(timespec="seconds"),
                                "last": now.isoformat(timespec="seconds"),
                                "score": int(it.get("score") or 4),
                            }
                            l3.append(nf)
                            idx[key] = nf
                        changed = True
                if t is not None and now - t < timedelta(hours=self.l2_hours):
                    kept2.append(it)
            self._write(os.path.join(d, "l2.json"), kept2)
        else:
            kept2 = l2
            if len(kept2) != raw2_len:
                self._write(os.path.join(d, "l2.json"), kept2)

        kept3 = []
        for it in l3:
            try:
                last = datetime.fromisoformat(it.get("last") or it.get("time"))
            except Exception:
                last = now
            if now - last < timedelta(days=self.l3_days):
                kept3.append(it)
        if len(kept3) > self.l3_max:
            kept3 = kept3[-self.l3_max:]
        if changed or first_today or len(kept3) != len(l3):
            self._write(os.path.join(d, "l3.json"), kept3)
        return kept2, kept3

    # ---------- 承诺提取 ----------
    def _promise(self, answer: str) -> str:
        """从我这边的回复里挑一句承诺，让「我答应你的事」也进记忆。"""
        if not answer:
            return ""
        for part in re.split(r"[。！？!?\n]+", answer):
            p = part.strip()
            p = re.sub(r"^(好的?|嗯+|行|好嘞|成|没问题)[，,、\s]*", "", p).strip()
            if not (4 <= len(p) <= 40):
                continue
            if "我" not in p:
                continue
            if any(w in p for w in PROMISE_WORDS):
                return p[:40]
        return ""

    # ---------- 主钩子 ----------
    @filter.on_llm_request()
    async def build_context(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.enabled:
            return
        umo = event.unified_msg_origin
        d, l1, l2, l3 = self._load(umo)
        l2, l3 = self._maintain(umo, d, l2, l3)
        msg = event.message_str or ""
        last = ""
        if len(l1) >= 2:
            last = f"{l1[-2].get('content','')} / {l1[-1].get('content','')}"
        elif l1:
            last = str(l1[-1].get("content", ""))
        info = await self._judge(msg, last, umo)
        recall = self._recall(d, info.get("keyword", ""), msg)

        l3, touched = self._touch_l3(l3, msg, recall)
        if touched:
            self._write(os.path.join(d, "l3.json"), l3)

        blocks = []
        if l3:
            blocks.append("【长期记忆】\n" + "\n".join(f"- {x.get('fact','')}" for x in l3[-self.l3_max:]))
        if l2:
            blocks.append("【短期记忆】\n" + "\n".join(f"- {x.get('summary','')}" for x in l2[-self.l2_max:]))
        if recall:
            blocks.append("【相关回忆】\n" + "\n".join(f"- {x}" for x in recall))
        if blocks:
            req.system_prompt = (req.system_prompt or "") + "\n\n" + "\n\n".join(blocks)

        hist = []
        for m in l1[-self.l1_max * 2:]:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role in ("user", "assistant") and content:
                hist.append({"role": role, "content": content})
        if hist:
            # 只有本插件有货时才接管上下文，空的时候别把平台自带历史清掉
            req.contexts = hist
        self._pending[umo] = {"info": info, "msg": msg}

    @filter.on_llm_response()
    async def save_turn(self, event: AstrMessageEvent, resp):
        if not self.enabled:
            return
        umo = event.unified_msg_origin
        pend = self._pending.pop(umo, None)
        if not pend:
            return
        try:
            d, l1, l2, l3 = self._load(umo)
            now = datetime.now().isoformat(timespec="seconds")
            msg = pend["msg"]
            info = pend["info"]
            answer = (getattr(resp, "completion_text", "") or "").strip()
            l1.append({"role": "user", "content": msg, "time": now, "score": info["score"]})
            if answer:
                l1.append({"role": "assistant", "content": answer, "time": now, "score": info["score"]})
            l1 = l1[-self.l1_max * 4:]
            self._write(os.path.join(d, "l1.json"), l1)
            self._archive(d, {"time": now, "role": "user", "content": msg, "score": info["score"]})
            if answer:
                self._archive(d, {"time": now, "role": "assistant", "content": answer, "score": info["score"]})

            if info["score"] >= 2 and info.get("summary"):
                self._upsert2(l2, {"summary": info["summary"], "kw": info.get("keyword", ""),
                                   "time": now, "score": info["score"]})

            # 我这边的承诺也要记，不然「明天提醒你」第二天就忘
            promise = self._promise(answer)
            if promise and info["score"] >= 3:
                self._upsert2(l2, {"summary": f"我答应过：{promise}", "kw": "",
                                   "time": now, "score": 4, "src": "assistant"})

            if l2:
                l2 = l2[-(self.l2_max * 5):]
                self._write(os.path.join(d, "l2.json"), l2)
        except Exception as e:
            logger.warning(f"[memory_curve] 回写失败：{e}")

    # ---------- 手动纠正入口 ----------
    def _fmt_list(self, l2: list, l3: list) -> str:
        out = [f"【长期记忆】{len(l3)} 条（上限 {self.l3_max}）"]
        if l3:
            for i, it in enumerate(l3, 1):
                tip = "  ← 一次性任务，建议清理" if _oneoff(it.get("fact")) else ""
                out.append(f"{i}. {it.get('fact', '')}{tip}")
        else:
            out.append("（空）")
        out.append("")
        out.append(f"【短期记忆】{len(l2)} 条")
        for i, it in enumerate(l2, 1):
            out.append(f"{i}. {it.get('summary', '')}")
        out.append("")
        out.append("用法：记忆 加 <内容> / 记忆 改 <编号> <新内容> / 记忆 删 <编号,编号> / 记忆 清理")
        return "\n".join(out)

    @filter.command("记忆")
    async def cmd_memory(self, event: AstrMessageEvent):
        """记忆 列表|加|改|删|清理 —— 手动查看和纠正长期记忆"""
        if not event.is_admin():
            yield event.plain_result("这个命令只有老大能动，别人别乱翻我记忆本")
            return
        umo = event.unified_msg_origin
        d, l1, l2, l3 = self._load(umo)
        body = re.sub(r"^\s*[/／]?\s*记忆", "", event.message_str or "").strip()
        parts = body.split(None, 1)
        sub = parts[0] if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""

        if sub in ("", "列表", "查看", "ls"):
            yield event.plain_result(self._fmt_list(l2, l3))
            return

        if sub in ("加", "添加", "add"):
            if not arg:
                yield event.plain_result("要加什么？写法：记忆 加 老大爱吃白米饭")
                return
            now = datetime.now().isoformat(timespec="seconds")
            self._backup(d, "l3.json")
            self._upsert3(l3, {"fact": arg[:60], "kw": "", "time": now, "last": now, "score": 5})
            self._write(os.path.join(d, "l3.json"), l3)
            yield event.plain_result(f"记住了：{arg[:60]}")
            return

        if sub in ("清", "清理", "clean"):
            gone3 = [it.get("fact", "") for it in l3 if _oneoff(it.get("fact"))]
            gone2 = [it.get("summary", "") for it in l2 if _oneoff(it.get("summary"))]
            if not gone3 and not gone2:
                yield event.plain_result("翻了一遍，没有一次性任务混在里面，挺干净的")
                return
            self._backup(d, "l3.json")
            self._backup(d, "l2.json")
            keep3 = [it for it in l3 if not _oneoff(it.get("fact"))]
            keep2 = [it for it in l2 if not _oneoff(it.get("summary"))]
            self._write(os.path.join(d, "l3.json"), keep3)
            self._write(os.path.join(d, "l2.json"), keep2)
            out = ["清掉了这些一次性任务："]
            out += [f"- L3 {x}" for x in gone3]
            out += [f"- L2 {x}" for x in gone2]
            out.append(f"现在 L3 {len(keep3)} 条，L2 {len(keep2)} 条")
            yield event.plain_result("\n".join(out))
            return

        if sub in ("删", "删除", "del"):
            nums = sorted({int(x) for x in re.findall(r"\d+", arg)})
            good = [n for n in nums if 1 <= n <= len(l3)]
            if not good:
                yield event.plain_result("编号不对，先「记忆 列表」看看是几号")
                return
            removed = [l3[n - 1] for n in good]
            keys = {_norm(it.get("fact")) for it in removed}
            self._backup(d, "l3.json")
            keep3 = [it for i, it in enumerate(l3, 1) if i not in set(good)]
            keep2 = [it for it in l2 if _norm(it.get("summary")) not in keys]
            self._write(os.path.join(d, "l3.json"), keep3)
            if len(keep2) != len(l2):
                self._backup(d, "l2.json")
                self._write(os.path.join(d, "l2.json"), keep2)
            out = ["删掉了："] + [f"- {it.get('fact', '')}" for it in removed]
            if len(keep2) != len(l2):
                out.append("（短期记忆里同一条也一起清了，免得明天又被提升回来）")
            yield event.plain_result("\n".join(out))
            return

        if sub in ("改", "纠正", "edit"):
            m = re.match(r"\s*(\d+)\s+(.+)$", arg, re.S)
            if not m:
                yield event.plain_result("写法：记忆 改 3 老大生日是三月十二")
                return
            n = int(m.group(1))
            text = m.group(2).strip()[:60]
            if not (1 <= n <= len(l3)):
                yield event.plain_result(f"只有 {len(l3)} 条，{n} 号不存在")
                return
            old_fact = l3[n - 1].get("fact", "")
            old_key = _norm(old_fact)
            self._backup(d, "l3.json")
            now = datetime.now().isoformat(timespec="seconds")
            l3[n - 1].update({"fact": text, "last": now, "score": 5})
            self._write(os.path.join(d, "l3.json"), l3)
            hit = 0
            for it in l2:
                if _norm(it.get("summary")) == old_key:
                    it["summary"] = text
                    hit += 1
            if hit:
                self._backup(d, "l2.json")
                self._write(os.path.join(d, "l2.json"), l2)
            yield event.plain_result(f"改好了：{old_fact}  →  {text}")
            return

        yield event.plain_result("认不出这个子命令，试试：记忆 列表 / 加 / 改 / 删 / 清理")
