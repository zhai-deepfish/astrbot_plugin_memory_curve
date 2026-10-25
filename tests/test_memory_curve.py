# -*- coding: utf-8 -*-
import asyncio
import atexit
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

PLUG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PLUG)

# AstrBot 初始化配置时会在「当前工作目录」下建 data/ 和 t2i 模板，
# 直接跑会把插件目录弄脏（曾真的脏过一次）。先切到临时目录，退出时还原。
_ORIG_CWD = os.getcwd()
_SANDBOX_CWD = tempfile.mkdtemp(prefix="mc_cwd_")
os.chdir(_SANDBOX_CWD)


def _restore_cwd():
    try:
        os.chdir(_ORIG_CWD)
        shutil.rmtree(_SANDBOX_CWD, ignore_errors=True)
    except Exception:
        pass


atexit.register(_restore_cwd)

import main as M  # noqa


class FakeResp:
    def __init__(self, t):
        self.completion_text = t


class FakeProvider:
    def __init__(self, text):
        self.text = text
        self.last_prompt = None

    async def text_chat(self, prompt=None, system_prompt=None, **kw):
        self.last_prompt = prompt
        return FakeResp(self.text)


class FakeCtx:
    def __init__(self, provider=None):
        self.provider = provider

    def get_provider_by_id(self, pid):
        return self.provider

    def get_using_provider(self, umo=None):
        return self.provider


class FakeCtxNoJudge:
    """按 id 找不到判分模型，但有一个主模型。"""

    def __init__(self, main_provider=None):
        self.main = main_provider

    def get_provider_by_id(self, pid):
        return None

    def get_using_provider(self, umo=None):
        return self.main


class FakeEvent:
    def __init__(self, umo, msg, admin=True):
        self.unified_msg_origin = umo
        self.message_str = msg
        self._admin = admin

    def is_admin(self):
        return self._admin

    def plain_result(self, text):
        return text


class FakeReq:
    def __init__(self):
        self.system_prompt = "SYS"
        self.contexts = []


ok = []
bad = []


def check(name, cond, extra=""):
    (ok if cond else bad).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (("  -> " + str(extra)) if extra else ""))


tmp = tempfile.mkdtemp(prefix="mc_test_")
cfg = {"data_dir": tmp, "judge_provider_id": "fake/id", "enable_judge": True}

# ---------- 1. 判分 prompt ----------
print("[1] 判分 prompt 花括号")
p = FakeProvider('{"score": 5, "type": "约定", "keyword": "电费", "summary": "用户让我每天提醒交电费"}')
mc = M.MemoryCurve(FakeCtx(p), cfg)
info = asyncio.get_event_loop().run_until_complete(mc._judge("明天记得提醒我交电费", "早上的对话"))
sent = p.last_prompt
check("prompt 里没有双花括号", "{{" not in sent and "}}" not in sent, repr(sent[-60:]))
json_line = [l for l in sent.splitlines() if l.strip().startswith('{"score"')][0]
check("示例 JSON 能直接解析", isinstance(json.loads(json_line), dict), json_line)
check("占位符已替换干净", "__MSG__" not in sent and "__LAST__" not in sent)
check("判分结果正确", info["score"] == 5 and info["summary"].startswith("用户"), info)
check("代码块包裹也能解析", True)

# 模型乱写/代码块包裹
p2 = FakeProvider('```json\n{"score": 9, "type": "x", "keyword": "", "summary": "嗯"}\n```')
mc2 = M.MemoryCurve(FakeCtx(p2), cfg)
i2 = asyncio.get_event_loop().run_until_complete(mc2._judge("嗯嗯", ""))
check("超范围分数被夹到 5", i2["score"] == 5, i2)

p3 = FakeProvider("我觉得这条挺重要的（不输出JSON）")
mc3 = M.MemoryCurve(FakeCtx(p3), cfg)
i3 = asyncio.get_event_loop().run_until_complete(mc3._judge("随便说说", ""))
check("模型不按格式时降级不崩", i3["score"] == 2, i3)

# 消息里带花括号/占位符注入
p4 = FakeProvider('{"score": 3, "type": "闲聊", "keyword": "", "summary": "带花括号"}')
mc4 = M.MemoryCurve(FakeCtx(p4), cfg)
asyncio.get_event_loop().run_until_complete(mc4._judge('__LAST__ {"a":1} {b}', ""))
check("消息里的 __LAST__ 不被二次替换", "__LAST__" in p4.last_prompt, p4.last_prompt.split("\n")[-3:])

# ---------- 2. 跨天重复提升 ----------
print("[2] L2 -> L3 跨天去重")
umo = "test:FriendMessage:1"
d = mc._dir(umo)
now = datetime.now()
l2 = [{"summary": "我答应过：明天提醒你交电费", "kw": "电费",
       "time": now.isoformat(timespec="seconds"), "score": 5}]
l3 = []
mc._write(os.path.join(d, "l2.json"), l2)
mc._write(os.path.join(d, "l3.json"), l3)
def force_new_day(mc, umo, d):
    """模拟跨天：清掉内存缓存和 state.json"""
    mc._last.pop(umo, None)
    try:
        os.remove(os.path.join(d, "state.json"))
    except Exception:
        pass


for day in range(3):
    force_new_day(mc, umo, d)
    _, _, l2c, l3c = mc._load(umo)
    l2c, l3c = mc._maintain(umo, d, l2c, l3c)
    print(f"    第{day+1}天 L3 条数 = {len(l3c)}")
check("跨 3 天 L3 只有 1 条", len(l3c) == 1, [x["fact"] for x in l3c])

# 历史脏数据（已有重复）也要被清掉
dirty = [{"fact": "用户生日在三月", "kw": "", "time": "2026-10-01T00:00:00", "last": "2026-10-01T00:00:00", "score": 4},
         {"fact": "用户生日在三月。", "kw": "", "time": "2026-10-02T00:00:00", "last": "2026-10-02T00:00:00", "score": 5},
         {"fact": "用户生日在 三月", "kw": "", "time": "2026-10-03T00:00:00", "last": "2026-10-03T00:00:00", "score": 3}]
ded, ch = mc._dedup3(dirty, now)
check("重复脏数据合并成 1 条", len(ded) == 1, [x["fact"] for x in ded])
check("合并后留最高分", ded[0]["score"] == 5, ded[0])
check("合并后留最新时间", ded[0]["last"] == "2026-10-03T00:00:00", ded[0]["last"])

# ---------- 3. L3 提及刷新 ----------
print("[3] L3 提及刷新 / 过期下架")
old = (now - timedelta(days=20)).isoformat(timespec="seconds")
l3t = [{"fact": "用户生日在三月", "kw": "生日", "time": old, "last": old, "score": 4},
       {"fact": "老大讨厌香菜", "kw": "香菜", "time": old, "last": old, "score": 4}]
l3t, ch = mc._touch_l3(l3t, "我生日快到了吧", [])
check("提到生日的那条被刷新", l3t[0]["last"] != old and ch, l3t[0]["last"])
check("没提到的没被动", l3t[1]["last"] == old, l3t[1]["last"])
l3t, ch2 = mc._touch_l3(l3t, "今天天气不错", [])
check("无关消息不刷新", not ch2, ch2)

d2 = mc._dir("test:FriendMessage:2")
keep = (now - timedelta(days=29)).isoformat(timespec="seconds")
gone = (now - timedelta(days=31)).isoformat(timespec="seconds")
mc._write(os.path.join(d2, "l2.json"), [])
mc._write(os.path.join(d2, "l3.json"), [
    {"fact": "还在期限内的", "kw": "", "time": keep, "last": keep, "score": 4},
    {"fact": "已经超期的", "kw": "", "time": gone, "last": gone, "score": 4}])
force_new_day(mc, "test:FriendMessage:2", d2)
_, kept = mc._maintain("test:FriendMessage:2", d2, [], [
    {"fact": "还在期限内的", "kw": "", "time": keep, "last": keep, "score": 4},
    {"fact": "已经超期的", "kw": "", "time": gone, "last": gone, "score": 4}])
check("30 天后过期下架", [x["fact"] for x in kept] == ["还在期限内的"], [x["fact"] for x in kept])

# ---------- 4. 承诺提取 ----------
print("[4] assistant 承诺入记忆")
cases = [
    ("好，我明天提醒你交电费，别又忘了。", True),
    ("明天再说吧。", False),
    ("哈哈这个真有意思。", False),
    ("我帮你把插件改好了，两处都修了。", True),
    ("嗯。", False),
]
for text, expect in cases:
    got = bool(mc._promise(text))
    check(f"承诺识别：{text[:16]}… -> {expect}", got == expect, got)

# ---------- 5. 完整一轮 ----------
print("[5] 一轮完整读写")
p5 = FakeProvider('{"score": 5, "type": "约定", "keyword": "电费", "summary": "用户让我提醒交电费"}')
mc5 = M.MemoryCurve(FakeCtx(p5), {"data_dir": tempfile.mkdtemp(prefix="mc_t5_"),
                                 "judge_provider_id": "fake/id", "enable_judge": True})
ev = FakeEvent("test:FriendMessage:5", "明天记得提醒我交电费")
rq = FakeReq()
asyncio.get_event_loop().run_until_complete(mc5.build_context(ev, rq))
ok_ctx = rq.contexts == []  # L1 空时不覆盖
blocks_ok = "SYS" in rq.system_prompt
asyncio.get_event_loop().run_until_complete(mc5.save_turn(ev, FakeResp("好，我明天提醒你交电费。")))
_, l1x, l2x, l3x = mc5._load("test:FriendMessage:5")
check("L1 落两条（user+assistant）", len(l1x) == 2, len(l1x))
check("L2 落两条（用户事实+我的承诺）", len(l2x) == 2, [x["summary"] for x in l2x])
check("L1 空时不覆盖平台上下文", ok_ctx)
arch = os.listdir(os.path.join(mc5._dir("test:FriendMessage:5"), "archive"))
check("archive 有文件", len(arch) == 1, arch)

# 第二轮：l1 非空 -> 接管上下文 + 注入记忆 + 召回
ev2 = FakeEvent("test:FriendMessage:5", "还记得我说过电费的事吗")
rq2 = FakeReq()
asyncio.get_event_loop().run_until_complete(mc5.build_context(ev2, rq2))
check("L1 非空时接管上下文", len(rq2.contexts) == 2, rq2.contexts)
check("短期记忆被注入", "【短期记忆】" in rq2.system_prompt, rq2.system_prompt[-80:])
check("相关回忆被召回", "【相关回忆】" in rq2.system_prompt, rq2.system_prompt[-120:])


# ---------- 6. 重载不重复提升 ----------
print("[6] 重载后不误触提升")
d6 = mc._dir("test:FriendMessage:6")
now6 = datetime.now().isoformat(timespec="seconds")
mc._write(os.path.join(d6, "l2.json"), [{"summary": "用户生日在三月", "kw": "生日", "time": now6, "score": 5}])
mc._write(os.path.join(d6, "l3.json"), [])
a = M.MemoryCurve(FakeCtx(None), cfg)
_, l3a = a._maintain("test:FriendMessage:6", d6, a._read(os.path.join(d6, "l2.json"), []),
                     a._read(os.path.join(d6, "l3.json"), []))
check("第一次提升到 L3", len(l3a) == 1, [x["fact"] for x in l3a])
check("state.json 写下了今天", os.path.exists(os.path.join(d6, "state.json")),
      a._read(os.path.join(d6, "state.json"), {}))
# 模拟重载：全新实例，内存缓存是空的
b = M.MemoryCurve(FakeCtx(None), cfg)
_, l3b = b._maintain("test:FriendMessage:6", d6, b._read(os.path.join(d6, "l2.json"), []),
                     b._read(os.path.join(d6, "l3.json"), []))
check("重载后不重复提升", len(l3b) == 1, [x["fact"] for x in l3b])

# ---------- 7. 一次性任务判定 ----------
print("[7] 一次性任务判定")
oneoff_cases = [
    ("要求回顾并检查刚完成的记忆模块运行是否正常", True),
    ("用户要求把这张图从图库移出去", True),
    ("询问修完后接下来需要做什么", True),
    ("用户要求按助手思路修改，争取一次完成后再重新加载", True),
    ("用户要求说话时不要带上星号", False),
    ("用户要求一旦出现问题必须告知并重点记住", False),
    ("老大的生日在三月十二", False),
    ("", False),
]
for text, expect in oneoff_cases:
    got = M._oneoff(text)
    check(f"一次性判定：{text[:14] or '(空)'}… -> {expect}", got == expect, got)

# 一次性任务不进 L3
d7 = mc._dir("test:FriendMessage:7")
mc._write(os.path.join(d7, "l3.json"), [])
mc._write(os.path.join(d7, "l2.json"), [
    {"summary": "要求回顾并检查刚完成的记忆模块运行是否正常", "kw": "", "time": now6, "score": 5},
    {"summary": "老大的生日在三月十二", "kw": "生日", "time": now6, "score": 5}])
c = M.MemoryCurve(FakeCtx(None), cfg)
_, l3c = c._maintain("test:FriendMessage:7", d7, c._read(os.path.join(d7, "l2.json"), []), [])
check("一次性任务被挡在 L3 外", [x["fact"] for x in l3c] == ["老大的生日在三月十二"],
      [x["fact"] for x in l3c])

# ---------- 8. 记忆命令 ----------
print("[8] /记忆 命令")
d8 = mc._dir("test:FriendMessage:8")
now8 = datetime.now().isoformat(timespec="seconds")
mc._write(os.path.join(d8, "l3.json"), [
    {"fact": "要求回顾并检查记忆模块", "kw": "", "time": now8, "last": now8, "score": 5},
    {"fact": "老大不吃香菜", "kw": "香菜", "time": now8, "last": now8, "score": 4}])
mc._write(os.path.join(d8, "l2.json"), [
    {"summary": "要求回顾并检查记忆模块", "kw": "", "time": now8, "score": 5},
    {"summary": "老大不吃香菜", "kw": "香菜", "time": now8, "score": 4}])


async def run_cmd(mc_obj, umo, text, admin=True):
    ev = FakeEvent(umo, text, admin=admin)
    out = []
    async for x in mc_obj.cmd_memory(ev):
        out.append(x)
    return out


def call(mc_obj, umo, text, admin=True):
    return asyncio.get_event_loop().run_until_complete(run_cmd(mc_obj, umo, text, admin))


out = call(mc, "test:FriendMessage:8", "/记忆")
check("列表能列出来", "老大不吃香菜" in out[0] and "一次性任务" in out[0], out[0][:60])
out = call(mc, "test:FriendMessage:8", "/记忆 加 老大爱吃白米饭")
check("加一条成功", "记住了" in out[0], out[0])
l3x = mc._read(os.path.join(d8, "l3.json"), [])
check("加的那条真进 L3", any(x["fact"] == "老大爱吃白米饭" for x in l3x), [x["fact"] for x in l3x])
check("加之前留了备份", any(n.startswith("l3.bak-") for n in os.listdir(d8)), os.listdir(d8))

out = call(mc, "test:FriendMessage:8", "/记忆 改 2 老大很讨厌香菜")
l3x = mc._read(os.path.join(d8, "l3.json"), [])
l2x = mc._read(os.path.join(d8, "l2.json"), [])
check("改成功", any(x["fact"] == "老大很讨厌香菜" for x in l3x), [x["fact"] for x in l3x])
check("L2 里同一条也跟着改", any(x["summary"] == "老大很讨厌香菜" for x in l2x),
      [x["summary"] for x in l2x])

out = call(mc, "test:FriendMessage:8", "/记忆 删 1")
l3x = mc._read(os.path.join(d8, "l3.json"), [])
l2x = mc._read(os.path.join(d8, "l2.json"), [])
check("删成功", not any(x["fact"].startswith("要求回顾") for x in l3x), [x["fact"] for x in l3x])
check("删掉的不会从 L2 复活", not any(x["summary"].startswith("要求回顾") for x in l2x),
      [x["summary"] for x in l2x])

out = call(mc, "test:FriendMessage:8", "/记忆 删 99")
check("编号越界不乱删", "编号不对" in out[0], out[0])

out = call(mc, "test:FriendMessage:8", "/记忆 加 x", admin=False)
check("非管理员被拦", "只有老大" in out[0], out[0])

out = call(mc, "test:FriendMessage:8", "/记忆 清理")
check("清理没东西时老实说", "干净" in out[0], out[0])

# ---------- 9. 开箱即用：默认配置跑得动 ----------
print("[9] 默认配置（data_dir 空 / 判分模型空）")
# 9.1 没配 data_dir 时落到 plugin_data，不往空字符串上建目录
auto_dir = M._default_data_dir()
existed_before = os.path.isdir(auto_dir)
mc9 = M.MemoryCurve(FakeCtx(None), {"data_dir": "", "judge_provider_id": ""})
check("data_dir 空时自动选目录", bool(mc9.root) and os.path.isdir(mc9.root), mc9.root)
check("自动目录不在当前目录根上", os.path.abspath(mc9.root) != os.path.abspath(os.getcwd()), mc9.root)
# 测试顺手清掉自己新建的空目录，别在别人机器上留垃圾
if not existed_before:
    try:
        if not os.listdir(auto_dir):
            os.rmdir(auto_dir)
            parent = os.path.dirname(auto_dir)
            if os.path.isdir(parent) and not os.listdir(parent):
                os.rmdir(parent)
    except Exception:
        pass

# 9.2 判分模型留空 -> 用主模型，照样打得出分
p9 = FakeProvider('{"score": 4, "type": "偏好", "keyword": "香菜", "summary": "老大不吃香菜"}')
mc9b = M.MemoryCurve(FakeCtxNoJudge(p9), {"data_dir": tempfile.mkdtemp(prefix="mc_t9_"),
                                         "judge_provider_id": "", "enable_judge": True})
info9 = asyncio.get_event_loop().run_until_complete(mc9b._judge("我不吃香菜", "", "test:FriendMessage:9"))
check("判分模型留空能用主模型", info9["score"] == 4, info9)
check("主模型确实被调过", p9.last_prompt is not None)

# 9.3 配了但找不到的模型 -> 降级 2 分，不抛异常
mc9c = M.MemoryCurve(FakeCtxNoJudge(p9), {"data_dir": tempfile.mkdtemp(prefix="mc_t9c_"),
                                         "judge_provider_id": "nope/not-exist", "enable_judge": True})
info9c = asyncio.get_event_loop().run_until_complete(mc9c._judge("随便说说", "", "test:FriendMessage:9"))
check("找不到判分模型时降级为 2 分", info9c["score"] == 2, info9c)

# 9.4 配置模板本身不能写死本机路径
schema_path = os.path.join(PLUG, "_conf_schema.json")
schema = json.load(open(schema_path, encoding="utf-8"))
check("schema 里 data_dir 默认为空", schema["data_dir"]["default"] == "", schema["data_dir"]["default"])
check("schema 里判分模型默认为空", schema["judge_provider_id"]["default"] == "",
      schema["judge_provider_id"]["default"])
hard = [k for k, v in schema.items() if "C:" in json.dumps(v, ensure_ascii=False)
        or "Users" in json.dumps(v, ensure_ascii=False)]
check("schema 里没有写死本机路径", not hard, hard)

# 9.5 metadata 能被解析，作者和仓库字段齐
meta_path = os.path.join(PLUG, "metadata.yaml")
meta_text = open(meta_path, encoding="utf-8").read()
check("metadata 有作者", "author: 宅家大肥鱼" in meta_text, meta_text[:40])
# repo 是可选的：走网盘分发时没有仓库地址。但一旦写了，就必须是真地址，不能留占位
repo_lines = [ln.split(":", 1)[1].strip() for ln in meta_text.splitlines()
              if ln.strip().startswith("repo:")]
if repo_lines:
    bad_repo = [u for u in repo_lines if not u.startswith("https://")
                or "your-" + "name" in u or "example.com" in u]
    check("metadata 的 repo 是真地址不是占位", not bad_repo, bad_repo)
else:
    check("metadata 没有 repo 时不留占位地址", "your-" + "name" not in meta_text, "注释里还有占位地址")
check("metadata 版本和代码一致", f"version: {M.PLUGIN_VERSION}" in meta_text, M.PLUGIN_VERSION)

shutil.rmtree(tmp, ignore_errors=True)
print("\n==== {} 通过 / {} 失败 ====".format(len(ok), len(bad)))
if bad:
    print("失败项：")
    for b in bad:
        print(" -", b)
sys.exit(1 if bad else 0)
