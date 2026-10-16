#!/usr/bin/env python3
"""
牵牛花商品详情批量浏览脚本 —— 纯 ADB 驱动，不经过任何 LLM。

设计目标
--------
把"点商品 → 等详情页 → 读完整详情 → 返回"这套确定性流程从 ~2 分钟/商品
压到普通屏约 7-9 秒；`--tall-screen --pace ultra` 下详情整页一屏可见，
实测约 2-8 秒/商品（取决于 React Native 渲染和商品类型）。

核心策略
--------
1. 列表页用「规格/条码」锚点定位商品卡片，同时读取规格、月售、库存、
   活动品、三渠道零售价和采购价。
2. 详情页读取开放平台 ID、SPU/SKU、重量、采购价/单位、三渠道状态/零售价/
   库存、礼袋、可售日期、力荐、标品、店内码/货号；普通屏按样本标定滚动，
   高屏整页一次读取。
3. 翻页保留重叠卡片并对 React Native 虚拟列表显式等待；点击和滑动带轻微
   随机化，平台 429 时自动退避。

加速后端（可选）
----------------
手机上装了 ARTEMIS 无障碍助手时，点击 / 滑动 / 返回走助手（省掉每条
`input` 命令 ~760ms 的 JVM 启动），助手 v7 及以上时 UI 层级也走助手
（省掉 `uiautomator dump` 的 ~2 秒，而且不会像 uiautomator 那样把屏幕
底部 y>2129 的内容裁掉）。没装、没启用或连不上时自动回退到 input /
uiautomator。`--no-helper` 强制走老路，`--no-helper-dump` 只让层级走老路。

录制
----
用电脑端 scrcpy 录制，手机屏幕上不留任何悬浮条/状态栏图标，
视频直接落在 Mac 上，无时长限制。

依赖
----
python3（标准库即可，无第三方包）、adb、scrcpy（录制时才需要）

用法
----
  # 先干跑标定，确认识别到的商品和坐标都对，不做任何点击
  python3 qnh_product_scan.py --calibrate-only

  # 正式跑 20 个商品并录像
  python3 qnh_product_scan.py --count 20 --record

  # 跑当前列表全部商品并采完整字段；高屏会自动设置且结束自动还原
  python3 qnh_product_scan.py --count 0 --verify-every 1 --tall-screen --pace ultra --record

按 Ctrl-C 可随时中断，脚本会妥善收尾（保存 CSV、停止录像、还原屏幕）；
默认每 50 个商品原子保存一次断点，`--resume 上一轮.csv` 可续跑。
"""

import argparse
import atexit
import csv
import hashlib
import json
import os
import random
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime

import qnh_capture_common as C

# --------------------------------------------------------------------------
# 基础配置
# --------------------------------------------------------------------------

MODULE_DIR = os.path.dirname(os.path.abspath(globals().get("__file__", sys.argv[0])))
DEFAULT_ADB = os.environ.get("ADB") or "/Users/jalen/opt/platform-tools/adb"
if not os.path.exists(DEFAULT_ADB):
    DEFAULT_ADB = shutil.which("adb") or "adb"

REMOTE_DUMP = "/sdcard/qnh_ui_dump.xml"
A11Y_BACKUP = os.path.join(MODULE_DIR, ".a11y_backup.txt")

# 牵牛花的包名（实测取得），用于确认前台 App 正确
APP_PKG = "com.sankuai.scsx.android.shuguopai"

# ---- UI dump 策略（顺序即优先级，全部为真机实测结论）----
#   plain      : 默认动画下会卡死在 "ERROR: could not get idle state."
#                （牵牛花有持续的界面更新，等不到 idle）
#   compressed : 同样条件下成功。走精简遍历，不等完整 idle。
#                实测精简版与完整版里「规格」「条码」节点的 bounds 完全一致，
#                所以定位精度没有损失。
#   noanim     : 把系统动画全关掉后，plain 也能成功（兜底，会改系统设置，
#                脚本结束时自动还原）。
DUMP_ORDER = ["compressed", "plain", "noanim"]


class DumpState:
    working = None        # 已确认可用的策略，后续直接复用，不重复试探
    anim_changed = False  # 是否动过系统动画设置（需要在收尾时还原）


# 详情页里代表"找到了"的字段名，命中任意一个即算成功
TARGET_FIELD_PAT = re.compile(r"店内码|货号")

# 字段值同行里的操作按钮，不是值本身
ROW_NOISE = {"复制", "复制全部", "修改", "编辑", "查看", "改价"}

# 字段值列相对标签右边缘的起笔容差（像素）
VALUE_COL_SLACK = 80

# 翻列表：普通屏慢速滑动防惯性；虚拟高屏按逻辑像素折半显示在物理屏上，
# 用更高逻辑速度等价于同样的物理速度，否则一次翻页要 8 秒。
SCROLL_SPEED = 0.38
TALL_SCROLL_SPEED = 0.95
TALL_SCROLL_MAX = 1800
LIST_TOP_Y = 0.167
SCROLL_MIN = 500

# 判定"列表到底"之前，为分页懒加载多给几次翻页机会
LAZY_LOAD_RETRIES = 3

# 列表行里的元信息文本，不可能是商品标题。dump 降级到 plain / noanim
# 策略时节点结构会变，真标题可能抓不到，这时"规格上方最近的左对齐节点"
# 会落到上一行的元信息上（实测出现过把「月售：0」当成商品点进去）。
META_PAT = re.compile(
    r"^\s*(月售|月销|已售罄|售罄|活动品|改价|查看|无数据|"
    r"采购|渠道|零售价|库存|规格|条码|￥|¥)")

# 商品标题节点宽度下限（占屏宽的比例）。实测真标题 597/1080 ≈ 0.55，
# 「查看(2)」「月售：0」这类内联元素远低于此。
MIN_TITLE_WIDTH_RATIO = 0.22

# 判断卡片是否被截断时，"同一列"的左边界容差（像素）。商品文字列从
# x=452 起，价格图标略靠左；左侧分类栏在 x<240。
CARD_COL_SLACK = 60

# 列表页里用来定位商品行的锚点。必须带冒号：列表行是「规格：霸道榴莲味」，
# 而详情页渠道价格表的表头是裸的「规格」——不带冒号会把详情页也认成列表页。
SPEC_PAT = re.compile(r"^规格[:：]")
BARCODE_PAT = re.compile(r"^条码")

# 列表卡片上的价格区：每行一个渠道图标 + 被拆成几段的价格（「￥」「45」「.00」
# 「/份」），图标是图片，层级里没有任何文字或描述，只能按顺序认渠道。实测顺序
# 固定为 美团闪购 / 饿了么 / 京东到家 / 采购价（采购价可能是「无数据」）。行数
# 不是 4 的卡片不拆渠道，只留价格原文，免得把价格记到错的渠道上。
CARD_PRICE_ROWS = 4
PRICE_TOKEN_PAT = re.compile(r"^(￥|¥|\d+|\.\d+|/\S{1,4}|无数据)$")
MONTH_SALES_PAT = re.compile(r"^月售[:：]\s*(\S+)")
# 没售罄的商品在月售旁边显示「库存：2」，售罄的显示「已售罄」
STOCK_PAT = re.compile(r"^库存[:：]\s*(\S+)")
SOLD_OUT_TEXTS = {"已售罄", "售罄"}
PROMO_ITEM_TEXT = "活动品"
# 价格区最后一行下面的文字；看得到它才说明整张卡片都在屏上
CARD_END_TEXT = "查看全部价格"
CARD_COLUMNS = C.CARD_COLUMNS

# 详情页字段（--verify-every 校验时顺带解析）。详情页顶部：开放平台标识 / SPU /
# 条码 / SKU / 重量 / 采购价（渠道信息区块上方）；渠道信息区块：每个渠道一段
# 「已上架(渠道名)」状态 + 渠道表格（渠道 | 零售价 | 渠道库存）。卖点 / 可售日期 /
# 礼袋在页面更下方，解析得到的 dump 里出现的标签，出现就记。
DETAIL_COLUMNS = C.DETAIL_COLUMNS
LOWER_DETAIL_COLUMNS = ("可售日期", "礼袋", "是否力荐", "是否标品")
# 「其他信息」区块的标签 → CSV 列名；值是标签右侧同行节点（如「礼袋绑定 0个礼袋」）
OTHER_INFO_LABELS = {"礼袋绑定": "礼袋", "可售日期": "可售日期",
                     "是否力荐": "是否力荐", "是否标品": "是否标品"}
CHANNEL_SECTION_ORDER = ["美团闪购", "饿了么", "京东到家"]
OPEN_PLATFORM_PAT = re.compile(r"^开放平台标识[:：]\s*(\S+)")
SPU_PAT = re.compile(r"^SPU[:：]\s*(\S+)")
SKU_PAT = re.compile(r"^SKU[:：]\s*(\S+)")
DETAIL_UPC_PAT = re.compile(r"^条码[:：]\s*(\S(?:.*\S)?)\s*$")
WEIGHT_PAT = re.compile(
    r"^\d+(?:\.\d+)?\s*(?:克|kg|KG|g|G|毫升|ml|ML|L|升|斤|两|个|袋|包|盒|罐|瓶|片|条|支|桶|杯|双|组|提)"
    r"\s*[（(][^）)]*[）)]$")
DETAIL_PRICE_PAT = re.compile(r"^[￥¥](\d+(?:\.\d+)?)$")
CHANNEL_ROW_PAT = re.compile(r"^(美团闪购|饿了么|京东到家)$")
LISTING_STATUS_PAT = re.compile(r"^(已上架|已下架|未上线)\((.+)\)$")
DETAIL_LABEL_PATS = (
    (re.compile(r"^卖点[:：]\s*(\S.*)$"), "卖点"),
    (re.compile(r"^可售日期[:：]\s*(\S.*)$"), "可售日期"),
    (re.compile(r"^礼袋[:：]\s*(\S.*)$"), "礼袋"),
)
PURCHASE_UNIT_TEXTS = {"单品", "袋", "罐", "盒", "瓶", "包", "份", "个", "条",
                       "桶", "杯", "支", "片", "双", "组", "提", "箱"}

# 详情页标题栏文字。React Native 把详情页叠在列表页上面、共用一个窗口，
# 层级里两边的节点同时存在；只有看到这个标题才能确定最上层是详情页。
DETAIL_TITLE = "商品详情"

# 店里在线运营机上随时会弹出订单通知（实测「顾客修改了订单信息 / 订单号：…
# / 前往订单，查看顾客修改详情 / 我知道了」）。脚本绝不能替店员点掉或点进去：
# 盲点的列表坐标 (750,1350) 正好落在「修改详情」上，返回键也会直接关掉弹窗。
BLOCKING_DIALOG_PAT = re.compile(r"我知道了|订单号[:：]|顾客修改")
DIALOG_POLL = 2.0            # 弹窗期间多久看一次（秒）
DIALOG_MAX_WAIT = 15 * 60    # 超过这么久没人处理就收尾退出（秒）

# 平台限流：详情接口被服务端自适应限流拒绝时，页面上是
# 「[Rhino-Server] Request is Degraded … queryStoreSpuDetail … code:429,
#  msg:自适应限流策略生效」+「加载失败 / 点击重试」（实测）。
THROTTLE_PAT = re.compile(r"限流|Request is Degraded|code:\s*429|加载失败")
THROTTLE_BACKOFF = (30, 60, 120)   # 第 1/2/3+ 次撞限流后暂停多久（秒）
THROTTLE_RETRIES = 2               # 同一个商品最多重开几次
THROTTLE_SLOWDOWN = 1.0            # 每撞一次，之后每个商品多停几秒
THROTTLE_SLOWDOWN_MAX = 5.0

# 找回列表页时，按返回键之前先等这么久再看一次（秒），避开过渡状态
ENSURE_SETTLE = 1.5

# 点商品后等详情页出现的上限（秒），超时就按当前位置重点一次
DETAIL_OPEN_TIMEOUT = 5.5

BOUNDS_PAT = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

# 节奏预设：(详情页加载, 每次滑动后停顿, 字段处停留, 返回后停顿)
PACING = {
    "fast":    ((0.7, 1.1), (0.25, 0.40), (0.4, 0.7), (0.5, 0.8)),
    "natural": ((1.2, 1.9), (0.45, 0.70), (1.4, 2.1), (0.8, 1.3)),
    "demo":    ((1.8, 2.6), (0.70, 1.00), (2.4, 3.2), (1.2, 1.8)),
    "ultra":   ((0.45, 0.75), (0.12, 0.25), (0.25, 0.45), (0.3, 0.5)),
}


class Ctx:
    """跑动过程中的全局状态，方便中断时收尾。"""
    adb = DEFAULT_ADB
    serial = None
    scrcpy_proc = None
    a11y_disabled = False
    rows = []           # CSV 行
    t_record_start = None
    run_stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_path = None
    resumed = 0         # 续跑时从上一轮 CSV 载入的行数
    throttle_hits = 0   # 本轮撞平台限流的次数
    extra_pause = 0.0   # 撞限流后给每个商品额外加的停留（秒）
    record_raw = None   # scrcpy 实际写入的 MKV
    record_out = None   # 最终交付的录像（转封装成功是 MP4，否则就是 MKV）
    out_dir = "."
    interrupted = False
    dismiss_order_dialogs = False
    cards = {}          # (条码, 标题, 规格) → 列表卡片字段，见 product_identity
    resumed_cards = {}  # 续跑：稳定行身份 → 上一轮 CSV 里的卡片列
    row_product_ids = {}  # CSV 序号 → (条码, 标题, 规格)，隔离重复条码变体
    resume_missing_bindings = {}  # (条码,标题) → 已绑定的当前 UI 精确身份
    details = {}        # 稳定行身份 → 详情页字段（verify 时解析）
    resumed_details = {}  # 续跑：稳定行身份 → 上一轮 CSV 里的详情列
    carry_details = {}  # 每日增量：指纹一致的行 → 基线详情列（文本原样沿用）
    carry_baseline = None   # --carry-baseline 载入的基线（listing 定位串 → 条目）
    carry_seen = None       # 携带命中的 listing 定位串集合（结束时报未命中数）
    last_detail_nodes = []  # open_detail 最后一次确认过的详情页节点，避免调用方重复 dump
    last_detail_fingerprint = None
    display_backup = None   # --tall-screen 前的 (size_override, density_override, effective_density)
    profile = None
    profiles_file = None
    capture_id = None
    batch_id = None
    run_id = None
    parent_manifest_sha256 = None
    manifest_output = None
    progress_output = None
    started_at = None       # whole capture lineage start (includes resumed rows)
    run_started_at = None   # this process run start
    finished_at = None
    target_count = None
    list_title = None
    category = None
    stop_reason = ""
    final_state = None
    capture_options = {}
    cleaned = False


CTX = Ctx()


# --------------------------------------------------------------------------
# adb 封装
# --------------------------------------------------------------------------

def adb_args(*args):
    base = [CTX.adb]
    if CTX.serial:
        base += ["-s", CTX.serial]
    return base + list(args)


def adb(*args, timeout=30, check=False):
    """执行 adb 命令，返回 stdout 文本。"""
    p = subprocess.run(adb_args(*args), capture_output=True, timeout=timeout)
    out = p.stdout.decode("utf-8", "replace")
    if check and p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"adb {' '.join(args)} 失败: {err or out.strip()}")
    return out


def sh(cmd, timeout=30, check=False):
    """执行 adb shell 命令。"""
    return adb("shell", cmd, timeout=timeout, check=check)


def require_device():
    out = adb("devices")
    lines = [line for line in out.splitlines()[1:] if line.strip()]
    devices = [line.split()[0] for line in lines if line.split()[-1] == "device"]
    if not devices:
        die("没有已授权的安卓设备。请确认手机已连接、USB 调试已开启并已授权。\n"
            f"（adb devices 输出：\n{out.strip()}）")
    if CTX.serial and CTX.serial not in devices:
        die(f"指定的设备 {CTX.serial} 不在已连接列表里：{devices}")
    if not CTX.serial:
        if len(devices) > 1:
            die(f"检测到多台设备 {devices}，请用 --serial 指定一台。")
        CTX.serial = devices[0]
    # Wi-Fi ADB 下熄屏后 input tap 会被静默丢弃（实测 SM-G9730），启动前先唤醒，
    # 防止任务在屏幕已熄时启动导致后续点击全部丢失。采集期间持续点击会重置
    # 熄屏倒计时，无需额外保活。
    sh("input keyevent KEYCODE_WAKEUP")
    info(f"设备：{CTX.serial}")


def screen_size():
    out = sh("wm size")
    m = re.search(r"Override size:\s*(\d+)x(\d+)", out) or \
        re.search(r"Physical size:\s*(\d+)x(\d+)", out)
    if not m:
        die(f"无法获取屏幕分辨率，wm size 返回：{out.strip()}")
    return int(m.group(1)), int(m.group(2))


def setup_tall_screen():
    """把逻辑屏幕临时改成双高；备份原 override，进程退出时保证还原。"""
    size_out, density_out = sh("wm size"), sh("wm density")
    sm = re.search(r"Override size:\s*(\d+x\d+)", size_out)
    dm = re.search(r"Override density:\s*(\d+)", density_out)
    current = screen_size()
    physical_density = re.search(r"Physical density:\s*(\d+)", density_out)
    effective_density = int(dm.group(1)) if dm else (
        int(physical_density.group(1)) if physical_density else 420)
    CTX.display_backup = (sm.group(1) if sm else None,
                          int(dm.group(1)) if dm else None,
                          effective_density)
    target_w = 1080
    target_h = 4560
    target_density = 360
    if current == (target_w, target_h) and effective_density == target_density:
        # 上一进程崩溃可能把高屏 override 留在设备上。若把当前高屏再当
        # backup，后续每轮都会“还原”成高屏。此设备的正常逻辑高度是高屏
        # 一半，密度回到 physical；cleanup 时可靠恢复 1080x2280/420。
        normal_density = int(physical_density.group(1)) if physical_density else 420
        CTX.display_backup = (f"{target_w}x{target_h // 2}",
                              normal_density, normal_density)
        atexit.register(restore_display)
        info("高屏模式已是 1080x4560 / density 360；按遗留高屏接管，结束恢复正常显示")
        return
    sh(f"wm size {target_w}x{target_h}", check=True)
    sh(f"wm density {target_density}", check=True)
    time.sleep(2)
    atexit.register(restore_display)
    info(f"高屏模式：{current[0]}x{current[1]} → {target_w}x{target_h}，"
         f"density {effective_density} → {target_density}（结束自动还原）")


def restore_display():
    """还原 --tall-screen 前的 display override；可重复调用。"""
    backup = CTX.display_backup
    if backup is None:
        return
    CTX.display_backup = None
    size, density, effective_density = backup
    try:
        sh(f"wm size {size}" if size else "wm size reset", check=True)
        # Samsung 在 size override 后执行 density reset 会自行写入错误的 560；
        # 恢复原有效密度（等于 physical 时 wm 会自动清掉 override）。
        sh(f"wm density {density if density is not None else effective_density}", check=True)
        time.sleep(1)
        info("屏幕分辨率和密度已还原")
    except Exception as e:
        warn(f"自动还原屏幕失败：{e}；请手动执行 adb shell wm size reset / wm density reset")



# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------

def info(msg):
    print(f"  {msg}", flush=True)


def step(msg):
    print(f"\n\033[1m>> {msg}\033[0m", flush=True)


def warn(msg):
    print(f"  \033[33m! {msg}\033[0m", flush=True)


def die(msg):
    print(f"\n\033[31m错误: {msg}\033[0m", file=sys.stderr, flush=True)
    if not CTX.interrupted:
        CTX.final_state = "failed"
        CTX.stop_reason = CTX.stop_reason or str(msg).splitlines()[0][:128]
    cleanup()
    sys.exit(1)


# --------------------------------------------------------------------------
# UI dump 与解析
# --------------------------------------------------------------------------

ANIM_KEYS = ("window_animation_scale", "transition_animation_scale",
             "animator_duration_scale")


def _set_animations(value):
    for k in ANIM_KEYS:
        sh(f"settings put global {k} {value}")


def _raw_dump(strategy):
    """按指定策略跑一次 dump。成功返回 XML 文本，失败返回 None。"""
    if strategy == "noanim" and not DumpState.anim_changed:
        warn("改用关闭系统动画的兜底方案（脚本结束时会自动还原）")
        _set_animations(0)
        DumpState.anim_changed = True
        time.sleep(1.2)

    flag = "--compressed " if strategy == "compressed" else ""
    sh(f"rm -f {REMOTE_DUMP}")
    out = sh(f"uiautomator dump {flag}{REMOTE_DUMP}", timeout=45)
    if "ERROR" in out.upper() or "could not" in out.lower():
        return None
    xml = sh(f"cat {REMOTE_DUMP}", timeout=45)
    return xml if xml.strip().startswith("<?xml") else None


def clear_stuck_uiautomator():
    """
    清掉卡死的 uiautomator 进程。

    dump 被中断（Ctrl-C、脚本崩溃）会留下一个 shell 用户的 app_process
    僵在那里占住 UiAutomation，之后每次 `uiautomator dump` 刚启动就被
    SIGKILL（rc=137），表现却是"三种策略全失败"，很容易误判成 App 有动画。

    只在所有 dump 策略都失败之后才调用，避免误杀正常并发的 dump。
    """
    killed = []
    for ln in sh("ps -A").splitlines():
        parts = ln.split()
        if len(parts) > 1 and parts[0] == "shell" and "app_process" in ln:
            sh(f"kill -9 {parts[1]}")
            killed.append(parts[1])
    if killed:
        warn(f"清掉了卡死的 uiautomator 进程 {', '.join(killed)}，重试 dump")
        time.sleep(1.5)
    return bool(killed)


def dump_xml(retries=2):
    """
    抓一次 UI 层级 XML。

    助手可用时直接走它的 /dump_xml（输出就是标准 UIAutomator 格式），
    省掉 uiautomator 那 ~2 秒的 UiAutomation 启动开销，也不会和别的
    自动化工具抢 UiAutomation。助手抓不到就退回下面的逐级降级方案。

    牵牛花上普通 dump 会卡死在 "could not get idle state"，所以这里按
    DUMP_ORDER 逐级降级尝试；一旦某个策略成功，后续直接复用它。
    """
    if HelperState.ready and HelperState.use_for_dump:
        xml = helper_dump()
        if not xml:
            HelperState.dump_failures += 1
            if HelperState.dump_failures >= HELPER_DUMP_RECONNECT_AFTER:
                HelperState.dump_failures = 0
                if _helper_reconnect():
                    info(f"助手端口转发丢失已重建（127.0.0.1:{HelperState.port}），"
                         "继续走助手层级")
                    xml = helper_dump()
                else:
                    warn("助手 forward 重建失败，回退到 input / uiautomator")
            if not xml:
                warn("助手取层级失败，本次回退到 uiautomator")
        else:
            HelperState.dump_failures = 0
        if xml:
            return xml

    def _try_all():
        order = ([DumpState.working] if DumpState.working else []) + \
                [s for s in DUMP_ORDER if s != DumpState.working]
        for strategy in order:
            for _ in range(retries):
                xml = _raw_dump(strategy)
                if xml:
                    if DumpState.working != strategy:
                        info(f"UI dump 策略：{strategy}")
                        DumpState.working = strategy
                    return xml
                time.sleep(0.8)
        return None

    xml = _try_all()
    if xml:
        return xml
    # 全挂了，可能是上一次中断留下的僵死进程占着 UiAutomation
    if clear_stuck_uiautomator():
        xml = _try_all()
        if xml:
            return xml
    die("三种 dump 策略（compressed / plain / 关动画）全部失败。\n"
        "常见原因：屏幕上有持续动画或视频、或有其他自动化工具正占用 "
        "UiAutomation（比如 Artemis、Appium、Mobly）。请先关掉再试。")


def dump_products():
    """
    抓一次并解析出商品列表。

    如果当前策略解析不出商品（例如 compressed 模式把标题节点裁掉了），
    自动升级到信息更完整的策略重试，而不是直接报错。
    """
    tried = []
    while True:
        nodes = parse_nodes(dump_xml())
        prods = find_products(nodes, detect_watermarks(nodes))
        if prods:
            return nodes, prods
        # 层级走助手时没有"更完整的策略"可升级，重试只是空转
        if HelperState.ready and HelperState.use_for_dump:
            return nodes, []
        tried.append(DumpState.working)
        nxt = [s for s in DUMP_ORDER if s not in tried]
        if not nxt:
            return nodes, []
        warn(f"{DumpState.working} 模式未解析出商品，升级到 {nxt[0]} 重试")
        DumpState.working = nxt[0]



def parse_nodes(xml):
    """把 XML 拍平成节点列表。"""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        die(f"UI XML 解析失败: {e}")
    nodes = []
    for el in root.iter("node"):
        b = el.get("bounds", "")
        m = BOUNDS_PAT.match(b)
        if not m:
            continue
        x1, y1, x2, y2 = (int(v) for v in m.groups())
        nodes.append({
            "text": (el.get("text") or "").strip(),
            "desc": (el.get("content-desc") or "").strip(),
            "cls": el.get("class") or "",
            "pkg": el.get("package") or "",
            "clickable": el.get("clickable") == "true",
            "x1": x1, "y1": y1, "x2": x2, "y2": y2,
            "cx": (x1 + x2) // 2, "cy": (y1 + y2) // 2,
            "w": x2 - x1, "h": y2 - y1,
        })
    return nodes


def foreground_pkg(nodes):
    """XML 里出现最多的包名即当前前台 App。"""
    counts = {}
    for n in nodes:
        if n["pkg"]:
            counts[n["pkg"]] = counts.get(n["pkg"], 0) + 1
    return max(counts, key=counts.get) if counts else ""


def detect_watermarks(nodes, threshold=8):
    """
    自动识别水印。这个 App 的水印是「账号名（账号 ID）」并在屏上重复铺开。

    旧逻辑把任何重复 4 次的文字都当水印；高屏一屏 6 张卡时，「规格：无」、
    「￥」「改价」也重复 4+ 次，商品锚点被整批删掉。现在账号格式可直接认；
    非账号文本只有重复很多且不是卡片结构文本时才兜底判水印。
    """
    counts = {}
    for n in nodes:
        if n["text"]:
            counts[n["text"]] = counts.get(n["text"], 0) + 1
    out = set()
    account_pat = re.compile(r".+[（(]\d{5,}[）)]$")
    card_literals = SOLD_OUT_TEXTS | {PROMO_ITEM_TEXT, CARD_END_TEXT, "改价", "无数据", "￥", "¥"}
    for text, count in counts.items():
        if count < 4:
            continue
        if account_pat.match(text):
            out.add(text)
            continue
        is_card = (SPEC_PAT.match(text) or BARCODE_PAT.match(text)
                   or MONTH_SALES_PAT.match(text) or STOCK_PAT.match(text)
                   or PRICE_TOKEN_PAT.match(text) or text in card_literals)
        if count >= threshold and not is_card:
            out.add(text)
    return out


def is_detail_page(nodes):
    """最上层是不是商品详情页（看顶部标题栏里有没有「商品详情」）。"""
    screen_h = max((n["y2"] for n in nodes), default=2280)
    return any(n["text"] == DETAIL_TITLE and n["y1"] < screen_h * 0.12
               for n in nodes)


def blocking_dialog(nodes):
    """屏幕上有店铺弹窗就返回它的标题（给人看的），否则返回 None。"""
    texts = [n for n in nodes if n["text"]]
    if not any(BLOCKING_DIALOG_PAT.search(n["text"]) for n in texts):
        return None
    screen_h = max((n["y2"] for n in nodes), default=2280)
    body = [n for n in texts if n["y1"] > screen_h * 0.05]   # 跳过状态栏时间
    return min(body, key=lambda n: n["y1"])["text"] if body else "店铺弹窗"


def current_blocking_dialog():
    """优先用 Helper v9 系统文字索引；再退到 v8 遍历 / 完整层级。"""
    match = helper_find_any_text(["我知道了", "订单号", "顾客修改"])
    if match is not None:
        return match or None
    match = helper_find_text(BLOCKING_DIALOG_PAT.pattern)
    if match is not None:
        return match or None
    return blocking_dialog(parse_nodes(dump_xml()))


def dismiss_blocking_dialog(nodes=None):
    """Close the authorized order-notification dialog: swipe it away first (the
    only gesture this MRN dialog layer accepts), then fall back to the button."""
    for attempt in range(3):
        nodes = parse_nodes(dump_xml())
        if not blocking_dialog(nodes):
            return True
        # The card body spans most of the width; swipe it left off-screen.
        card = max((n for n in nodes if n["text"] and n["w"] > 500 and n["h"] > 60
                    and n["cy"] > 300 and n["cy"] < 4000),
                   key=lambda n: n["w"] * n["h"], default=None)
        y = card["cy"] if card else 1500
        sh(f"input swipe 950 {y} 80 {y} 200")
        time.sleep(1.2)
        if not blocking_dialog(parse_nodes(dump_xml())):
            return True
    buttons = [node for node in parse_nodes(dump_xml())
               if node["text"] == "我知道了" and node["w"] > 0 and node["h"] > 0]
    if buttons:
        button = max(buttons, key=lambda item: item["w"] * item["h"])
        human_tap(button["cx"], button["cy"],
                  box=(button["x1"], button["y1"], button["x2"], button["y2"]))
        time.sleep(0.8)
    return not current_blocking_dialog()


def wait_while_dialog(where):
    """
    有店铺弹窗就原地等店员处理，绝不替人点掉或按返回。
    返回 True 表示等过一次（调用方应重新确认页面状态）。
    """
    d = current_blocking_dialog()
    if not d:
        return False
    if CTX.dismiss_order_dialogs and dismiss_blocking_dialog():
        info(f"已按授权关闭店铺订单弹窗「{d}」（{where}），继续")
        return True
    warn(f"\a检测到店铺弹窗「{d}」（{where}）——已暂停，不会点它。"
         "请店员看完后手动关闭，关闭后自动继续。")
    t0 = time.time()
    while not CTX.interrupted:
        time.sleep(DIALOG_POLL)
        if not current_blocking_dialog():
            info(f"弹窗已关闭（等了 {time.time() - t0:.0f} 秒），继续")
            time.sleep(0.8)
            return True
        if time.time() - t0 > DIALOG_MAX_WAIT:
            die(f"弹窗「{d}」超过 {DIALOG_MAX_WAIT // 60} 分钟无人处理，"
                "已收尾退出（结果和录像已保存）。")
    return True


def is_throttled(nodes):
    """详情页是不是平台限流的报错页（接口 429、「加载失败 / 点击重试」）。"""
    return any(n["text"] and THROTTLE_PAT.search(n["text"]) for n in nodes)


def dump_nodes_undisturbed(where):
    """取一次层级；被店铺弹窗挡住就先等它关掉再取，免得在弹窗上补滑。"""
    nodes = parse_nodes(dump_xml())
    if blocking_dialog(nodes):
        wait_while_dialog(where)
        nodes = parse_nodes(dump_xml())
    return nodes


def find_products(nodes, exclude_texts):
    """
    用「规格：」做锚点定位商品行。
    标题 = 规格节点正上方、左边缘对齐的那个文本节点。
    条码 = 规格节点正下方的条码文本，用作商品唯一 ID。
    """
    # 详情页叠在列表上时，底下被遮住的列表节点依然出现在层级里（而且报
    # "可见"）。不先排除掉，回退逻辑会以为已经回到列表页，接着把列表的
    # 坐标点到详情页上。
    if is_detail_page(nodes):
        return []
    texts = [n for n in nodes
             if n["text"] and n["text"] not in exclude_texts]
    specs = [n for n in texts if SPEC_PAT.match(n["text"])]

    # 商品标题是撑满卡片文字列的文本块（实测 w=597 / 1080 宽屏，且与文字
    # 长短无关）；而「查看(2)」「月售：0」这类内联元素只有一两百宽。用屏宽
    # 的比例做下限，比逐个堵字符串可靠。宁可漏判成"没有商品"（上层会报
    # 出来），也不要把它当商品点进去——那会把 App 带到未知页面。
    screen_w = max((n["x2"] for n in nodes), default=1080)
    min_title_w = screen_w * MIN_TITLE_WIDTH_RATIO

    products = []
    for sp in specs:
        # 标题：左边缘与规格对齐（允许几像素误差）、在规格上方、且离得最近
        cands = [n for n in texts
                 if abs(n["x1"] - sp["x1"]) <= 6
                 and n["y2"] <= sp["y1"] + 2
                 and sp["y1"] - n["y2"] < 260
                 and n["w"] >= min_title_w
                 and not SPEC_PAT.match(n["text"])
                 and not BARCODE_PAT.match(n["text"])
                 and not META_PAT.match(n["text"])]
        if not cands:
            continue
        title = max(cands, key=lambda n: n["y2"])

        # 条码：规格下方最近的条码节点
        bcs = [n for n in texts
               if BARCODE_PAT.match(n["text"])
               and n["y1"] >= sp["y2"] - 2
               and n["y1"] - sp["y2"] < 120]
        barcode = ""
        if bcs:
            bn = min(bcs, key=lambda n: n["y1"])
            digits = re.sub(r"\D", "", bn["text"])
            barcode = digits if len(digits) >= 6 else bn["text"]
        elif not any(n is not sp and n["y1"] >= sp["y2"] - 2
                     and n["x1"] >= sp["x1"] - CARD_COL_SLACK for n in texts):
            # 规格下面（同一列）已经没有可见文字：卡片被屏幕下沿截断，条码那
            # 一行还在屏幕外。这时读成"无条码"会被误判成自建商品（不取店内码
            # /货号），等下一屏它完整露出来再识别。实测因此错判并重复访问过
            # 3 个商品。条码行紧跟在规格下面、排在月售行之前，所以只要规格下面
            # 还有同列文字，有条码的话就一定已经读到了。只看同一列：左侧分类栏
            # 贯穿全屏，它的文字总在规格下面。
            continue

        products.append({
            "title": title["text"],
            "barcode": barcode,
            "tap": (title["cx"], title["cy"]),
            "box": (title["x1"], title["y1"], title["x2"], title["y2"]),
            "anchor_y": sp["y1"],
            "spec": SPEC_PAT.sub("", sp["text"]).strip(),
            "spec_node": sp,
        })

    products.sort(key=lambda p: p["anchor_y"])
    # 卡片字段顺手记下来：商品被点开时卡片可能只露出一半，翻页后它会被挪到
    # 列表顶部完整露出，那时再记上。写 CSV 时才按条码 / 标题取用。
    screen_h = max((n["y2"] for n in nodes), default=2280)
    for i, p in enumerate(products):
        end_y = products[i + 1]["box"][1] if i + 1 < len(products) else screen_h
        card = read_card(nodes, p["spec_node"], end_y)
        if card:
            CTX.cards[product_identity(p)] = card
    return products


def read_card(nodes, sp, end_y):
    """
    读一张商品卡片在列表上的字段：规格、月售、是否售罄、价格区每一行的原文。
    看不到「查看全部价格」（卡片被屏幕下沿截断）时返回 None。
    """
    body = [n for n in nodes
            if n["text"] and sp["y2"] - 2 <= n["y1"] < end_y
            and n["x1"] >= sp["x1"] - CARD_COL_SLACK]
    if not any(n["text"] == CARD_END_TEXT for n in body):
        return None
    # 价格被拆成几段文字，按行（中心 y 相近）拼回去
    rows = []
    for t in sorted((n for n in body if PRICE_TOKEN_PAT.match(n["text"])),
                    key=lambda n: (n["cy"], n["x1"])):
        if rows and abs(t["cy"] - rows[-1][-1]["cy"]) <= 25:
            rows[-1].append(t)
        else:
            rows.append([t])
    prices = ["".join(n["text"] for n in sorted(r, key=lambda n: n["x1"]))
              for r in rows]
    prices = [p.lstrip("￥¥") for p in prices if p[0] in "￥¥" or p == "无数据"]
    month = next((MONTH_SALES_PAT.match(n["text"]).group(1) for n in body
                  if MONTH_SALES_PAT.match(n["text"])), "")
    stock = next((STOCK_PAT.match(n["text"]).group(1) for n in body
                  if STOCK_PAT.match(n["text"])), "")
    return {"spec": SPEC_PAT.sub("", sp["text"]).strip(),
            "month_sales": month,
            "stock": stock,
            "sold_out": any(n["text"] in SOLD_OUT_TEXTS for n in body),
            "promo_item": any(n["text"] == PROMO_ITEM_TEXT for n in body),
            "prices": prices}


def card_cols(title, barcode, index=None, spec=None):
    """CSV card columns, isolated by row identity and barcode/title/spec."""
    row_key = C.row_identity(index, title, barcode) if index is not None else None
    product_key = (CTX.row_product_ids.get(int(index)) if index is not None else None)
    product_key = product_key or product_identity(title, barcode, spec)
    c = CTX.cards.get(product_key)
    if c is None:
        # Compatibility for tests and captures made before tuple-keyed card caches.
        c = (barcode and CTX.cards.get(barcode)) or CTX.cards.get(title)
    if not c:
        return CTX.resumed_cards.get(row_key, [""] * len(CARD_COLUMNS))
    p = c["prices"]
    split = len(p) == CARD_PRICE_ROWS
    return [c["spec"], c["month_sales"], c["stock"], "是" if c["sold_out"] else "否",
            "是" if c["promo_item"] else "否",
            *(p[:3] if split else ["", "", ""]),
            p[3] if split else "",
            " | ".join(p)]


def parse_detail(nodes, detail=None):
    """
    把详情页当前可见的字段解析出来，合并进 detail（后到的覆盖先到的）。

    详情页叠在列表上时底下有列表的幽灵节点，但它们没有「开放平台标识:」
    这类标签文字，也没有裸的渠道名，不会干扰。
    """
    detail = {} if detail is None else detail
    texts = [n for n in nodes if n["text"] and "kaixin" not in n["text"]]
    # 渠道信息区块的位置：采购价表格的值都在它上方
    section_y = next((n["y1"] for n in texts if n["text"] == "渠道信息"), None)
    # 「其他信息」区块：标签在左（如「礼袋绑定」），值是右侧同行节点
    for n in texts:
        col = OTHER_INFO_LABELS.get(n["text"])
        if col and col not in detail:
            row = [t for t in texts
                   if t is not n and abs(t["cy"] - n["cy"]) <= 25
                   and t["x1"] > n["x1"]]
            if row:
                detail[col] = min(row, key=lambda t: t["x1"])["text"]
    for pat, key in ((OPEN_PLATFORM_PAT, "开放平台标识"), (SPU_PAT, "SPU"),
                     (SKU_PAT, "SKU"), (DETAIL_UPC_PAT, "条码"),
                     *DETAIL_LABEL_PATS):
        for n in texts:
            if key == "条码" and n["x1"] >= 350:
                # The RN detail page overlays the list; list-card barcode ghost nodes start
                # around x=418, while the detail identity table starts at x=264.
                continue
            m = pat.match(n["text"])
            if m:
                detail[key] = m.group(1)
    # 采购价表格的左列：规格/重量表头下面从上到下是 规格、重量、采购单位
    # 三行，如「-」「75克(g)」「单品」或「组合品(1)」。最后一行是采购单位。
    header = next((n for n in texts if n["text"] == "规格/重量"), None)
    if header:
        left_col = [n for n in texts
                    if n is not header
                    and n["x1"] < 300 and n["w"] < 320
                    and header["y2"] - 2 <= n["y1"]
                    and (section_y is None or n["y2"] < section_y)]
        left_col.sort(key=lambda n: n["y1"])
        if "重量" not in detail:
            for n in left_col:
                if WEIGHT_PAT.match(n["text"]):
                    detail["重量"] = n["text"]
                    break
        if "采购单位_详情" not in detail and left_col:
            last = left_col[-1]["text"]
            if last != "-" and not WEIGHT_PAT.match(last):
                detail["采购单位_详情"] = last
    # 表头没渲染出来时的兜底：裸单位词
    if "采购单位_详情" not in detail:
        units = [n for n in texts if n["text"] in PURCHASE_UNIT_TEXTS
                 and n["x1"] < 300 and n["w"] < 320]
        if units:
            detail["采购单位_详情"] = units[-1]["text"]
    # 渠道上架状态：状态写在区块标题里，如「已上架(美团闪购)」
    for n in texts:
        m = LISTING_STATUS_PAT.match(n["text"])
        if m:
            detail[m.group(2) + "状态"] = m.group(1)
    # 采购价：渠道信息区块上方最上面的 ¥ 价格（区块下方的 ¥ 是渠道零售价）
    if "采购价_详情" not in detail:
        prices = sorted((n for n in texts if DETAIL_PRICE_PAT.match(n["text"])),
                        key=lambda n: n["y1"])
        if prices:
            above = [n for n in prices if section_y is None or n["y2"] < section_y]
            p = above[0] if above else prices[0]
            detail["采购价_详情"] = DETAIL_PRICE_PAT.match(p["text"]).group(1)
    # 渠道表格：渠道名所在行里，右边依次是 ¥零售价、库存（售罄时是「售罄」）
    for cn in (n for n in texts if CHANNEL_ROW_PAT.match(n["text"])):
        row = [n for n in texts
               if n is not cn and abs(n["cy"] - cn["cy"]) <= 25]
        price_nodes = [n for n in row
                       if DETAIL_PRICE_PAT.match(n["text"]) and n["x1"] > cn["x1"]]
        stock_nodes = [n for n in row
                       if (re.fullmatch(r"\d+", n["text"])
                           or n["text"] in SOLD_OUT_TEXTS)
                       and price_nodes
                       and n["x1"] > price_nodes[0]["x1"]]
        if price_nodes:
            detail[cn["text"] + "零售价"] = DETAIL_PRICE_PAT.match(
                price_nodes[0]["text"]).group(1)
        if stock_nodes:
            detail[cn["text"] + "库存"] = stock_nodes[0]["text"]
    return detail


def channels_complete(detail):
    """
    三个渠道信息是否完整：已上架必须有零售价和库存；未上线/已下架合法地没有
    价格和库存，看到状态标题即算完整。
    """
    for c in CHANNEL_SECTION_ORDER:
        status = detail.get(c + "状态")
        if not status:
            return False
        if status == "已上架" and not (detail.get(c + "零售价")
                                     and detail.get(c + "库存")):
            return False
    return True


def detail_cols(row):
    """CSV 的详情列；按序号+条码/名称隔离同名商品，续跑无串行。"""
    key = C.row_identity(row[0], row[1], row[2])
    detail = CTX.details.get(key)
    if detail is None:
        if key in CTX.carry_details:
            return list(CTX.carry_details[key])
        return CTX.resumed_details.get(key, [""] * len(DETAIL_COLUMNS))
    return [detail.get(column, "") for column in DETAIL_COLUMNS]


# --------------------------------------------------------------------------
# 可选加速后端：ARTEMIS 无障碍助手
#
# 实测每条 `input` 命令要 ~760ms（几乎全是设备上启动 JVM 的开销），
# `uiautomator dump` 要 ~2070ms。助手是常驻的无障碍服务，手势和层级都走
# loopback HTTP，省掉这两笔固定开销。连不上时整体回退到原来的路径。
# --------------------------------------------------------------------------

HELPER_PKG = "com.artemis.helper"
HELPER_DEVICE_PORT = 18888
HELPER_TOKEN_RECEIVER = f"{HELPER_PKG}/.TokenReceiver"
HELPER_TOKEN_ACTION = f"{HELPER_PKG}.SET_TOKEN"

# 从这个版本起助手每次 dump 都读当前屏幕（旧版读的是不会失效的节点缓存，
# 在本 App 上滚动 / 页面切换后返回过期层级）。更早的版本只用它做手势。
HELPER_FRESH_DUMP_VERSION = 7
HELPER_TEXT_QUERY_VERSION = 8
HELPER_ANY_TEXT_QUERY_VERSION = 9
HELPER_SEMANTIC_SCROLL_VERSION = 10


class HelperState:
    ready = False
    port = None
    token = None
    forwarded = False
    # 层级是否也走助手：由 helper_setup 按版本决定（>= HELPER_FRESH_DUMP_VERSION），
    # --no-helper-dump 可强制关掉。手势（dispatchGesture）不走节点缓存，
    # 任何版本都可用。
    use_for_dump = False
    allow_dump = True
    action_failures = 0
    version = 0
    has_text_query = False
    has_any_text_query = False
    has_semantic_scroll = False
    dump_failures = 0     # 连续 /dump_xml 失败数；达阈值重建 forward

HELPER_DUMP_RECONNECT_AFTER = 3   # 连续失败这么多次就重建 adb forward


def _helper_http(path, payload=None, timeout=10.0):
    """向助手发一次请求。payload 非空则 POST JSON。失败返回 None。"""
    url = f"http://127.0.0.1:{HelperState.port}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if HelperState.token:
        req.add_header("X-Artemis-Token", HelperState.token)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception:
        return None


def _helper_reconnect():
    """Wi-Fi ADB 的 adb server 可能清空端口转发表（forward 丢失但设备仍在线）：
    重建 forward + 重推 token。成功返回 True。"""
    HelperState.forwarded = False
    out = adb("forward", "tcp:0", f"tcp:{HELPER_DEVICE_PORT}").strip()
    m = re.search(r"(\d+)", out)
    if not m:
        return False
    HelperState.port = int(m.group(1))
    HelperState.forwarded = True
    sh(f"am broadcast -n {HELPER_TOKEN_RECEIVER} -a {HELPER_TOKEN_ACTION} "
       f"--es token {HelperState.token}")
    raw = _helper_http("/ping", timeout=4.0)
    if not raw:
        return False
    try:
        pong = json.loads(raw)
    except ValueError:
        return False
    return bool(pong.get("authenticated", pong.get("authed")))


def helper_setup():
    """建立转发 → 推 token → ping。成功返回 True。"""
    if HELPER_PKG not in sh(f"pm list packages {HELPER_PKG}"):
        info("未安装 ARTEMIS 无障碍助手，使用 input / uiautomator（较慢）")
        return False
    if HELPER_PKG not in sh("settings get secure enabled_accessibility_services"):
        info("ARTEMIS 助手未启用无障碍权限，使用 input / uiautomator（较慢）")
        return False

    out = adb("forward", "tcp:0", f"tcp:{HELPER_DEVICE_PORT}").strip()
    m = re.search(r"(\d+)", out)
    if not m:
        warn(f"adb forward 失败：{out!r}，回退到 input / uiautomator")
        return False
    HelperState.port = int(m.group(1))
    HelperState.forwarded = True

    HelperState.token = secrets.token_hex(24)
    sh(f"am broadcast -n {HELPER_TOKEN_RECEIVER} -a {HELPER_TOKEN_ACTION} "
       f"--es token {HelperState.token}")

    raw = _helper_http("/ping", timeout=4.0)
    if not raw:
        warn("助手端口无响应，回退到 input / uiautomator")
        return False
    try:
        pong = json.loads(raw)
    except ValueError:
        warn("助手 /ping 返回无法解析，回退到 input / uiautomator")
        return False
    if not pong.get("authenticated", pong.get("authed")):
        warn("助手拒绝了会话 token，回退到 input / uiautomator")
        return False

    HelperState.ready = True
    version = pong.get("version_code") or 0
    HelperState.version = version
    HelperState.has_text_query = version >= HELPER_TEXT_QUERY_VERSION
    HelperState.has_any_text_query = version >= HELPER_ANY_TEXT_QUERY_VERSION
    HelperState.has_semantic_scroll = version >= HELPER_SEMANTIC_SCROLL_VERSION
    HelperState.use_for_dump = (HelperState.allow_dump
                                and version >= HELPER_FRESH_DUMP_VERSION)
    if HelperState.use_for_dump:
        where = "，UI 层级也走助手"
    elif HelperState.allow_dump:
        where = (f"，UI 层级仍用 uiautomator（助手 v{version} 早于 "
                 f"v{HELPER_FRESH_DUMP_VERSION}，层级会过期；"
                 "`uv run artemis helper install` 可升级）")
    else:
        where = "，UI 层级仍用 uiautomator（--no-helper-dump）"
    fast_parts = []
    if HelperState.has_any_text_query:
        fast_parts.append("弹窗检查走系统文字索引")
    elif HelperState.has_text_query:
        fast_parts.append("弹窗检查走轻量遍历")
    if HelperState.has_semantic_scroll:
        fast_parts.append("列表语义翻页")
    fast = "；" + "，".join(fast_parts) if fast_parts else ""
    info(f"已接入 ARTEMIS 助手 v{version}（127.0.0.1:{HelperState.port}）："
         f"点击 / 滑动 / 返回走助手{where}{fast}")
    return True


def helper_dump():
    """通过助手取一次层级（标准 UIAutomator XML）。失败返回 None。"""
    xml = _helper_http("/dump_xml")
    return xml if xml and xml.strip().startswith("<?xml") else None


def helper_find_any_text(texts):
    """v9+ 系统侧字面检索；返回匹配文字、空串（未找到）或 None（不可用）。"""
    if not (HelperState.ready and HelperState.has_any_text_query):
        return None
    joined = "\x1f".join(texts)
    path = "/has_any_text?texts=" + urllib.parse.quote(joined, safe="")
    raw = _helper_http(path, timeout=2.0)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not data.get("success"):
        return None
    return data.get("match", "") if data.get("found") else ""


def helper_find_text(pattern):
    """v8+ 设备端早停正则查询；返回匹配文字、空串（未找到）或 None（不可用）。"""
    if not (HelperState.ready and HelperState.has_text_query):
        return None
    path = "/has_text?pattern=" + urllib.parse.quote(pattern, safe="")
    raw = _helper_http(path, timeout=3.0)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not data.get("success"):
        return None
    return data.get("match", "") if data.get("found") else ""



def helper_action(cmd, **params):
    """Run one helper action: True=confirmed success, False=confirmed failure, None=unknown."""
    timeout = 4.0 + params.get("duration", 0) / 1000.0
    raw = _helper_http("/action", {"cmd": cmd, **params}, timeout=timeout)
    if not raw:
        HelperState.action_failures += 1
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        HelperState.action_failures += 1
        return None
    if data.get("success"):
        return True
    HelperState.action_failures += 1
    return False


def helper_teardown():
    if HelperState.forwarded and HelperState.port:
        adb("forward", "--remove", f"tcp:{HelperState.port}")
        HelperState.forwarded = False


def find_target_field(nodes):
    """
    在详情页找『店内码/货号』，返回 (是否找到, [上行值, 下行值...])。

    这是一个「一个标签、两行值」的复合字段：标签节点异常高（实测
    h=133，普通行只有 ~40），右边纵向叠着两行——

        G502HERO                cy=1986   上行 = 店内码（商家自己填的，
        店内码/货号              cy=2024          实测有填成 '30g*1袋'、
        2102630291546685450     cy=2042          '风味鸡油辣椒味' 的）
        复制                    cy=2041   下行 = 19 位平台货号

    两个都是真值，所以按 y 从上到下全部取出来，由调用方决定写哪个。
    只按「离标签最近」取单值会随滚动位置在两行之间抖动（同一个商品
    两轮跑出不同结果）。
    """
    texts = [n for n in nodes if n["text"]]
    screen_h = max((n["y2"] for n in nodes), default=0)
    for n in texts:
        if TARGET_FIELD_PAT.search(n["text"]):
            # 值可能直接写在标签节点里
            m = re.search(r"[:：]\s*(\S.*)$", n["text"])
            if m and m.group(1).strip():
                return True, [m.group(1).strip()]
            cands = [t for t in texts
                     if t is not n
                     and t["text"].strip() not in ROW_NOISE
                     # 值列紧挨着标签列右边起笔（实测标签 x2=320、值 x1=342）。
                     # 限定起点范围，排除叠在详情页下面的列表节点（x1=452
                     # 起的月售/价格、水印等）——它们也会落在同一个 y 区间里，
                     # 实测把列表价格「￥6.90」的「6」当成了货号。
                     and n["x2"] - 4 <= t["x1"] <= n["x2"] + VALUE_COL_SLACK
                     # 必须和标签节点的 y 区间真正重叠才算属于这个字段。
                     # 用 |cy 差| < 标签高度 这种对称容差会把上一行（商品
                     # 卖点的值）也框进来，因为标签节点本身就有 133 高。
                     and (min(t["y2"], n["y2"]) - max(t["y1"], n["y1"]) > 0
                          or abs(t["cy"] - n["cy"]) <= 12)]
            # 按 y 区间重叠聚成若干视觉行，每行取最左的那个节点当值
            rows = []
            for t in sorted(cands, key=lambda t: t["cy"]):
                for row in rows:
                    if min(t["y2"], row[0]["y2"]) - max(t["y1"], row[0]["y1"]) > 0:
                        row.append(t)
                        break
                else:
                    rows.append([t])
            values = [min(r, key=lambda t: t["x1"])["text"] for r in rows]
            # 标签在屏幕下四分之一、却只抓到一行：多半是字段块被滚动区域
            # 下沿截断，下行的货号还在屏幕外，抓到的是上行店内码——而单值
            # 会被归进货号列（实测出现过）。算作没找到，让调用方再补滑一次；
            # 补滑后标签到了屏幕中部，那时仍只有一行就是真的只有一行。
            # 不用"贴着屏幕底边"来判断，因为详情页底部可能有按钮栏，滚动
            # 区域的下沿不在屏幕底部。
            if len(values) == 1 and n["y1"] > screen_h * 0.75:
                return False, []
            return True, values
    return False, []


def split_field_values(values):
    """
    把『店内码/货号』取到的几行值拆成 (店内码, 货号)。

    上行是商家自填的店内码，下行是 19 位平台货号。只抓到一行时无法
    判断是上行还是下行，归到货号——货号是平台生成的、总是存在，店内码
    是选填的。
    """
    if not values:
        return "", ""
    if len(values) == 1:
        return "", values[0]
    return values[0], values[-1]


def is_custom_product(prod):
    """
    判断是不是店内自建商品。

    标品在列表页有 13 位 EAN 条码；自建商品要么条码列为空，要么直接
    放的就是 19 位店内码（如 2090041147845984306）。这类商品的详情页
    不单独列『店内码/货号』那一行，补滑再多次也找不到，只会白白浪费
    ~30 秒。判定为自建商品时照常进详情页浏览，只是跳过找字段的补滑。
    """
    return len((prod.get("barcode") or "").strip()) != 13


def looks_like_list_page(nodes, exclude_texts):
    return len(find_products(nodes, exclude_texts)) >= 1


# --------------------------------------------------------------------------
# 人手化输入注入
# --------------------------------------------------------------------------

def jitter(v, amount):
    return int(round(v + random.uniform(-amount, amount)))


def human_point(x, y, box=None, spread=10):
    """Return a human-like point inside box without injecting input."""
    if box:
        x1, y1, x2, y2 = box
        w, h = x2 - x1, y2 - y1
        mx = min(max(4, min(18, w // 6)), max(0, w // 2))
        my = min(max(3, min(12, h // 4)), max(0, h // 2))
        lo_x, hi_x = x1 + mx, x2 - mx
        lo_y, hi_y = y1 + my, y2 - my
        return (random.randint(lo_x, hi_x) if hi_x >= lo_x else (x1 + x2) // 2,
                random.randint(lo_y, hi_y) if hi_y >= lo_y else (y1 + y2) // 2)
    return jitter(x, spread), jitter(y, spread // 2)


def human_tap(x, y, box=None, spread=10):
    """
    点击。默认在目标点周围小范围随机取点；若给了元素 box，
    则在元素内部安全区域内随机，避免每次都精确命中同一像素。
    """
    tx, ty = human_point(x, y, box, spread)
    # 屏外坐标必须拒绝：App 被回退键打穿回桌面后，旧列表坐标（如高屏
    # y=4117）会 clamp 到屏底命中桌面图标——2026-10-12 实测点开钉钉。
    try:
        sw_now, sh_now = screen_size()
    except SystemExit:
        sw_now, sh_now = 0, 0
    if sh_now and not (0 <= tx < sw_now and 0 <= ty < sh_now):
        warn(f"点击坐标 ({tx},{ty}) 超出当前屏 {sw_now}x{sh_now}，拒绝注入")
        return tx, ty
    helper_result = (helper_action("tap", x=tx, y=ty) if HelperState.ready else False)
    if helper_result is False:
        sh(f"input tap {tx} {ty}")
    elif helper_result is None:
        warn("助手点击回执不确定；不盲目补点，交由页面状态确认")
    return tx, ty


def human_swipe(x1, y1, x2, y2, dur_range=(280, 520)):
    """滑动。终点带横向漂移，时长随机，避免完美直线 + 固定耗时。"""
    dur = random.randint(*dur_range)
    pts = [max(0, jitter(x1, 9)), max(0, jitter(y1, 7)),
           max(0, jitter(x2, 14)), max(0, jitter(y2, 7))]
    helper_result = (helper_action("swipe", x1=pts[0], y1=pts[1],
                                   x2=pts[2], y2=pts[3], duration=dur)
                     if HelperState.ready else False)
    if helper_result is False:
        sh(f"input swipe {pts[0]} {pts[1]} {pts[2]} {pts[3]} {dur}")
    elif helper_result is None:
        warn("助手滑动回执不确定；不盲目补滑，交由列表连续性确认")
    return dur


def nap(rng):
    t = random.uniform(*rng)
    time.sleep(t)
    return t


# --------------------------------------------------------------------------
# 无障碍服务处理
# --------------------------------------------------------------------------

def disable_artemis_a11y():
    """
    把 com.artemis.helper 从已启用的无障碍服务列表里摘掉（其它服务保留）。
    原始值备份到 .a11y_backup.txt，可用 --restore-a11y 还原。
    """
    cur = sh("settings get secure enabled_accessibility_services").strip()
    if cur in ("null", "None"):
        cur = ""
    if "artemis" not in cur.lower():
        info("Artemis 无障碍服务当前未启用，无需处理。")
        return
    with open(A11Y_BACKUP, "w") as f:
        f.write(cur)
    kept = [s for s in cur.split(":") if s and "artemis" not in s.lower()]
    new = ":".join(kept)
    if new:
        sh(f"settings put secure enabled_accessibility_services '{new}'")
    else:
        sh("settings put secure enabled_accessibility_services ''")
        sh("settings put secure accessibility_enabled 0")
    CTX.a11y_disabled = True
    info(f"已摘除 Artemis 无障碍服务（原值已备份到 {A11Y_BACKUP}）")
    time.sleep(1.0)


def restore_a11y():
    if not os.path.exists(A11Y_BACKUP):
        die(f"找不到备份文件 {A11Y_BACKUP}，无法还原。")
    with open(A11Y_BACKUP) as f:
        val = f.read().strip()
    sh(f"settings put secure enabled_accessibility_services '{val}'")
    sh("settings put secure accessibility_enabled 1")
    CTX.a11y_disabled = False
    info(f"已还原无障碍服务设置：{val}")


# --------------------------------------------------------------------------
# scrcpy 录制
# --------------------------------------------------------------------------

def start_recording(path):
    """
    开始录制。先录成 MKV，收尾时再无损转封装成 path（MP4）。

    实测 scrcpy 4.1 在 --no-playback 下 SIGINT、SIGTERM 都不响应，只能强杀；
    而 MP4 的索引（moov）要正常退出时才写，强杀后整个文件打不开（一轮 168 个
    商品、16 分钟、521 MB 的录像就这样报废过）。MKV 边录边可播，强杀也不丢。
    """
    exe = shutil.which("scrcpy")
    if not exe:
        die("找不到 scrcpy。请先 `brew install scrcpy`，或去掉 --record 跑。")

    raw = os.path.splitext(path)[0] + ".mkv"
    log_path = os.path.splitext(path)[0] + ".scrcpy.log"
    # scrcpy 2.7+ 用 --no-playback，老版本用 --no-display
    for flag in ("--no-playback", "--no-display"):
        cmd = [exe, "--no-control", flag, f"--record={raw}"]
        if CTX.serial:
            cmd += ["-s", CTX.serial]
        # stderr 写文件而不是 PIPE：录一整轮时没人读管道，写满就会卡住 scrcpy
        with open(log_path, "wb") as log:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log)
        time.sleep(3.0)
        if proc.poll() is None:
            CTX.scrcpy_proc = proc
            CTX.t_record_start = time.time()
            CTX.record_raw, CTX.record_out = raw, path
            info(f"scrcpy 录制已启动（{flag}）→ {path}")
            return
        with open(log_path, encoding="utf-8", errors="replace") as log:
            err = log.read()
        if "unknown option" not in err.lower():
            die(f"scrcpy 启动失败：\n{err.strip()}")
    die("scrcpy 启动失败：--no-playback 和 --no-display 都不被接受。")


def stop_recording():
    proc = CTX.scrcpy_proc
    if not proc:
        return
    CTX.scrcpy_proc = None
    if proc.poll() is None:
        info("正在停止录制…")
        time.sleep(1.5)                  # 让最后一屏进编码缓冲再停
        proc.send_signal(signal.SIGINT)  # 能正常收尾的版本就让它收尾
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()                  # MKV 被强杀也能播，不丢录像
            proc.wait()
    remux_recording()


def remux_recording():
    """把 MKV 无损转封装成 MP4（不重新编码）；失败就保留 MKV。"""
    raw, out = CTX.record_raw, CTX.record_out
    if not raw or not os.path.exists(raw):
        return
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        warn(f"没有 ffmpeg，录像保留为 MKV：{raw}")
        CTX.record_out = raw
        return
    info("正在把录像转封装为 MP4…")
    # 强杀留下的 MKV 结尾不完整，ffmpeg 会提示 "File ended prematurely"，
    # 不影响结果
    r = subprocess.run([ffmpeg, "-v", "error", "-y", "-i", raw,
                        "-c", "copy", out], capture_output=True)
    if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
        os.remove(raw)
    else:
        warn(f"转封装 MP4 失败，录像保留为 MKV：{raw}\n"
             + r.stderr.decode("utf-8", "replace").strip()[-300:])
        CTX.record_out = raw


def video_offset():
    if CTX.t_record_start is None:
        return ""
    s = int(time.time() - CTX.t_record_start)
    return f"{s // 60:02d}:{s % 60:02d}"


# --------------------------------------------------------------------------
# 收尾
# --------------------------------------------------------------------------

CSV_COLUMNS_LEGACY = C.BASE_COLUMNS
CSV_COLUMNS = C.BASE_COLUMNS + C.META_COLUMNS
RESUME_DONE = {"成功", "自建商品", "携带"}   # 续跑时视为已完成、不再点开的状态

# 携带基线（每日增量）：状态名与行构造。携带行不点详情页，卡片列来自
# 本次滚动读到的卡片，详情列沿用基线快照。
CARRY_STATUS = "携带"


def load_carry_baseline(path):
    """载入每日增量携带基线：{listing 定位串 → {fingerprint, detail}}。

    fingerprint 为一级触发字段（三渠道价/采购价/售罄/规格/活动品）的
    归一化键，与 compare_card_baseline 的输出可直接 == 比较。
    """
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise C.CaptureContractError(f"携带基线格式不支持：{path}")
    CTX.carry_baseline = data["products"]
    CTX.carry_seen = set()
    info(f"携带基线：{len(CTX.carry_baseline)} 个商品（{os.path.basename(path)}）")


def compare_card_baseline(card, baseline_entry):
    """今日卡片 vs 基线：一级触发字段全一致 → 返回携带详情列，否则 None。

    触发字段即用户拍板的口径：三渠道价格、采购价、售罄、规格、活动品。
    月售/库存不计触发（二级变化，卡片今日值经 CTX.cards 照常入 CSV）。
    """
    p = card["prices"]
    split = len(p) == CARD_PRICE_ROWS
    today = {
        "spec": C.normalize_name(card["spec"]),
        "sold_out": bool(card["sold_out"]),
        "promo_item": bool(card["promo_item"]),
        "prices": [C.carry_price_key(p[i]) if split and i < len(p) else None
                   for i in range(4)],
    }
    if any(today[field] != baseline_entry["fingerprint"][field]
           for field in ("spec", "sold_out", "promo_item", "prices")):
        return None
    return baseline_entry["detail"]


def _carry_row(idx, prod, detail_values):
    """构造携带行：列表读卡今日值 + 基线详情列，不点开详情页。

    卡片列不需要单独存：compare 命中时该卡的完整字段刚由 find_products
    写入 CTX.cards，card_cols() 会输出与其他行一致的今日值。
    """
    info(f"[{idx:>3}] ≡ {prod['title'][:36]}  指纹一致，携带基线")
    CTX.rows.append([idx, prod["title"], prod["barcode"], "", "",
                     CARRY_STATUS, video_offset(), *capture_meta()])
    remember_row_product(idx, prod)
    CTX.carry_details[C.row_identity(idx, prod["title"], prod["barcode"])] = detail_values


def capture_meta(now=None):
    """当前行的本地时间、完整 ISO 时间，以及 CSV / 录像共用的文件名前缀。"""
    now = now or datetime.now()
    prefix = os.path.abspath(os.path.join(CTX.out_dir, f"qnh_scan_{CTX.run_stamp}"))
    return [now.strftime("%H:%M:%S"), now.isoformat(timespec="seconds"), prefix]


def resume_meta(path, row):
    """兼容旧 CSV：没有完整时间/证据前缀时，从旧文件名和「时刻」补出来。"""
    prefix = row.get("证据前缀") or os.path.splitext(os.path.abspath(path))[0]
    collected = row.get("采集时间")
    if not collected:
        m = re.search(r"qnh_scan_(\d{8})-\d{6}", os.path.basename(prefix))
        if m and row.get("时刻"):
            try:
                collected = datetime.strptime(
                    f"{m.group(1)} {row['时刻']}", "%Y%m%d %H:%M:%S").isoformat()
            except ValueError:
                collected = ""
    return [row.get("时刻", ""), collected or "", prefix]


def load_resume(path, *, strict_manifest=False):
    """Load completed rows from a verified manifest or explicit legacy CSV."""
    if not os.path.exists(path):
        die(f"--resume 指定的文件不存在：{path}")
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fields = list(reader.fieldnames or [])
    if not rows or any(column not in fields for column in CSV_COLUMNS_LEGACY):
        die(f"{path} 不是本脚本输出的结果（缺少列，或是空文件）")
    if strict_manifest and fields != C.FULL_COLUMNS:
        die("父 manifest 的 CSV 不符合精确 40 列契约")
    if any(None in row or any(row.get(field) is None for field in fields) for row in rows):
        die("续跑 CSV 存在字段数量错误")
    kept = [row for row in rows
            if C.text_value(row.get("状态")) in RESUME_DONE
            and (not strict_manifest or not C.row_detail_errors(row))]
    suppressed = 0
    if strict_manifest:
        # 保留同一原始 run 内的重复出现（平台确有重复列表项），但剔除后续
        # attempt 对同一条码+标题+规格的再次追加。后者是 resume 点击漂移造成的
        # 跨 run 污染；shard44 曾因此用 6 条同商品填满 182 行伪装 success。
        def resume_rank(row):
            detail_score = sum(bool(C.field_value(row.get(column)))
                               for column in CARD_COLUMNS + DETAIL_COLUMNS)
            return detail_score, bool(C.field_value(row.get("规格"))), int(row.get("序号") or 0)

        groups = {}
        for row in kept:
            identity = (C.text_value(row.get("条码")),
                        C.normalize_name(row.get("商品名称")),
                        C.normalize_name(row.get("规格")))
            prefix = os.path.abspath(row.get("证据前缀") or "")
            groups.setdefault(identity, {}).setdefault(prefix, []).append(row)
        selected_ids = set()
        for by_prefix in groups.values():
            chosen = max(by_prefix.values(),
                         key=lambda items: (len(items), -min(int(item.get("序号") or 0)
                                                            for item in items)))
            selected_ids.update(id(row) for row in chosen)
            suppressed += sum(map(len, by_prefix.values())) - len(chosen)
        kept = [row for row in sorted(kept, key=lambda item: int(item.get("序号") or 0))
                if id(row) in selected_ids]
        if suppressed:
            info(f"续跑：剔除了 {suppressed} 条跨 run 重复追加记录")

        # 无条码旧记录无法区分同名重叠卡，沿用旧策略保留信息最全的一条。
        by_name = {}
        output = []
        for row in kept:
            if C.text_value(row.get("条码")):
                output.append(row)
                continue
            key = C.listing_identity(row.get("商品名称"), "")
            previous = by_name.get(key)
            if previous is None:
                by_name[key] = row
                output.append(row)
            elif resume_rank(row) > resume_rank(previous):
                output[output.index(previous)] = row
                by_name[key] = row
        kept = sorted(output, key=lambda row: int(row.get("序号") or 0))
    if not strict_manifest:
        # Legacy captures could duplicate a clipped card; modern verified manifests preserve
        # every row because repeated barcodes are valid and use deterministic #2/#3 keys.
        def rank(row):
            return row["状态"] == "成功", bool(row["条码"])

        best = []
        for row in kept:
            product = {"title": row["商品名称"], "barcode": row["条码"],
                       "spec": row.get("规格", "")}
            twin = next((item for item in best if same_product(
                product, {"title": item["商品名称"], "barcode": item["条码"],
                          "spec": item.get("规格", "")})), None)
            if twin is None:
                best.append(row)
            elif rank(row) > rank(twin):
                best[best.index(twin)] = row
        if len(best) < len(kept):
            info(f"续跑：清理了上一轮 {len(kept) - len(best)} 条旧版重复记录")
        kept = best
    CTX.resumed = len(kept)
    CTX.resume_missing_bindings = {}
    # 清理后序号会有空洞，而新行按"已有行数 + 1"编号，所以统一重新编号
    CTX.rows = [[i, r["商品名称"], r["条码"], r["店内码"],
                 r["货号"], r["状态"], r["视频时间点"],
                 *resume_meta(path, r)]
                for i, r in enumerate(kept, 1)]
    for out_row, source in zip(CTX.rows, kept):
        CTX.row_product_ids[out_row[0]] = product_identity(
            source["商品名称"], source["条码"], source.get("规格", ""))
    if all(column in fields for column in CARD_COLUMNS):
        CTX.resumed_cards = {
            C.row_identity(out_row[0], out_row[1], out_row[2]):
                [source[column] for column in CARD_COLUMNS]
            for out_row, source in zip(CTX.rows, kept)
        }
    if all(column in fields for column in DETAIL_COLUMNS):
        CTX.resumed_details = {
            C.row_identity(out_row[0], out_row[1], out_row[2]):
                [source[column] for column in DETAIL_COLUMNS]
            for out_row, source in zip(CTX.rows, kept)
        }
    redo = (suppressed + sum(
        C.text_value(row.get("状态")) not in RESUME_DONE
        or (strict_manifest and bool(C.row_detail_errors(row)))
        for row in rows))
    info(f"续跑：从 {os.path.basename(path)} 载入已完成的 {len(kept)} 个（只翻页经过），"
         f"另外 {redo} 个未完成的会重做。"
         "这些旧行的「视频时间点」对应上一轮的录像。")


def scanner_version():
    here = MODULE_DIR
    try:
        rev = subprocess.run(["git", "-C", here, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        rev = ""
    files = [os.path.join(MODULE_DIR, "qnh_product_scan.py"), C.__file__]
    if CTX.profiles_file:
        files.append(CTX.profiles_file)
    digest = hashlib.sha256()
    for path in files:
        digest.update(os.path.basename(path).encode("utf-8"))
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
    return "qnh-capture-contract" + (f"@{rev}" if rev else "") + f"+{digest.hexdigest()[:16]}"


def _manifest_paths(csv_path):
    prefix = os.path.splitext(csv_path)[0]
    final_path = os.path.abspath(CTX.manifest_output or (prefix + ".manifest.json"))
    if CTX.progress_output:
        progress_path = os.path.abspath(CTX.progress_output)
    elif CTX.manifest_output and final_path.endswith(".manifest.json"):
        progress_path = final_path[:-len(".manifest.json")] + ".progress.json"
    else:
        progress_path = prefix + ".progress.json"
    return final_path, os.path.abspath(progress_path)


def _manifest_for(csv_path, state):
    try:
        rows, _ = C.read_csv_rows(csv_path)
    except C.CaptureContractError:
        if state in C.TERMINAL_MANIFEST_STATES and os.path.isfile(csv_path):
            rows = []
        else:
            raise
    final_path, _ = _manifest_paths(csv_path)
    video = CTX.record_out
    if not video or not os.path.exists(video):
        video = next((os.path.splitext(csv_path)[0] + ext for ext in (".mp4", ".mkv")
                      if os.path.exists(os.path.splitext(csv_path)[0] + ext)), None)
    artifacts = {
        "csv_path": os.path.abspath(csv_path),
        "evidence_prefix": os.path.splitext(os.path.abspath(csv_path))[0],
        "video_path": os.path.abspath(video) if video else None,
        "csv_sha256": (C.sha256_file(csv_path)
                       if state in C.TERMINAL_MANIFEST_STATES else None),
    }
    scope = {
        "list_title": CTX.list_title,
        "category": CTX.category,
        "expected_product_count": CTX.target_count,
        "requested_count": CTX.capture_options.get("requested_count"),
    }
    capture = {
        "method": "app_ui",
        "started_at": CTX.started_at.isoformat(timespec="seconds") if CTX.started_at else None,
        "run_started_at": (CTX.run_started_at.isoformat(timespec="seconds")
                           if CTX.run_started_at else None),
        "finished_at": CTX.finished_at.isoformat(timespec="seconds") if CTX.finished_at else None,
        "collector_version": scanner_version(),
        **CTX.capture_options,
    }
    return C.build_manifest(
        capture_id=CTX.capture_id,
        batch_id=CTX.batch_id,
        run_id=CTX.run_id,
        state=state,
        profile=CTX.profile,
        device_id=CTX.serial,
        scope=scope,
        capture=capture,
        artifacts=artifacts,
        rows=rows,
        stop_reason=CTX.stop_reason,
        parent_manifest_sha256=CTX.parent_manifest_sha256,
    )


def write_checkpoint():
    csv_path = write_csv()
    if csv_path and CTX.profile:
        _, progress_path = _manifest_paths(csv_path)
        C.atomic_write_json(progress_path, _manifest_for(csv_path, "running"))
    return csv_path


def finalize_manifest(csv_path, state):
    if not (csv_path and CTX.profile):
        return None
    final_path, progress_path = _manifest_paths(csv_path)
    value = _manifest_for(csv_path, state)
    if os.path.lexists(final_path):
        raise C.CaptureContractError(f"final manifest 已存在，拒绝覆盖：{final_path}")
    C.atomic_write_json(final_path, value, overwrite=False)
    C.atomic_write_json(progress_path, value)
    return final_path


def write_csv():
    if not CTX.rows and not CTX.profile:
        return None
    if CTX.csv_path:
        path = CTX.csv_path
    else:
        path = os.path.join(CTX.out_dir, f"qnh_scan_{CTX.run_stamp}.csv")
        if os.path.lexists(path):
            raise C.CaptureContractError(f"本次 CSV 路径已存在，拒绝覆盖：{path}")
        CTX.csv_path = path
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS + CARD_COLUMNS + DETAIL_COLUMNS)
        w.writerows(row + card_cols(row[1], row[2], index=row[0]) + detail_cols(row)
                    for row in CTX.rows)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def cleanup():
    if CTX.cleaned:
        return
    CTX.cleaned = True
    stop_recording()
    helper_teardown()
    if CTX.a11y_disabled:
        try:
            restore_a11y()
        except Exception as exc:
            warn(f"自动还原 Artemis 无障碍服务失败：{exc}")
    if DumpState.anim_changed:
        _set_animations(1)
        DumpState.anim_changed = False
        info("系统动画设置已还原")
    restore_display()
    CTX.finished_at = CTX.finished_at or datetime.now()
    path = write_csv()
    if path:
        info(f"结果已写入 {path}")
        state = CTX.final_state
        if not state:
            if not CTX.rows:
                state = "partial"
            else:
                rows, _ = C.read_csv_rows(path)
                complete = (CTX.target_count is not None
                            and len(rows) == CTX.target_count
                            and all(row.get("状态") in C.COMPLETE_ROW_STATUSES for row in rows)
                            and all(not C.row_detail_errors(row) for row in rows))
                state = "interrupted" if CTX.interrupted else ("success" if complete else "partial")
        manifest = finalize_manifest(path, state)
        if manifest:
            info(f"运行清单已写入 {manifest}（{state}）")


def on_unhandled_exception(exc_type, exc, traceback):
    if not CTX.interrupted:
        CTX.final_state = "failed"
        CTX.stop_reason = CTX.stop_reason or f"{exc_type.__name__}: {exc}"[:128]
    sys.__excepthook__(exc_type, exc, traceback)


def on_sigint(signum, frame):
    CTX.interrupted = True
    CTX.final_state = "interrupted"
    CTX.stop_reason = CTX.stop_reason or "收到中断信号"
    print("\n\n收到中断信号，正在收尾…", flush=True)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def calibrate_detail_scrolls(prod, sw, sh_, pace, max_scrolls):
    """
    在第一个商品上实测：详情页需要滑几次才能看到『店内码/货号』。
    这个次数会被后续所有商品复用，从而避免每个商品都 dump。
    """
    load, pause, dwell, settle = pace
    step(f"标定详情页滚动次数（样本：{prod['title'][:28]}）")

    wait_while_dialog("点商品前")
    if not open_detail(prod, load, screen=(sw, sh_), pace=pace):
        warn(f"标定样本「{prod['title'][:20]}」的详情页没打开")
        return 0, False, [], None, {}

    detail = {}
    nodes = CTX.last_detail_nodes or dump_nodes_undisturbed("标定详情页")
    detail = parse_detail(nodes)
    if whole_page_visible(nodes) and not channels_complete(detail):
        # 整页一屏可见时，渠道表格没看全多半只是渲染慢
        detail = parse_detail_with_retry(lambda: dump_xml(), detail,
                                         tries=3, wait=0.6,
                                         need=channels_complete)
    found, values = find_target_field(nodes)
    detail = parse_detail(nodes, detail)
    n_scroll = 0

    x_mid = sw // 2
    y_from, y_to = int(sh_ * 0.72), int(sh_ * 0.30)

    if sh_ >= 3500 and not found:
        # 高屏整页都在一屏内；字段没出现只是 React Native 还没渲染，原地等
        for _ in range(4):
            time.sleep(0.5)
            nodes = dump_nodes_undisturbed("标定详情页")
            found, values = find_target_field(nodes)
            detail = parse_detail(nodes, detail)
            if found and channels_complete(detail):
                break

    while not found and n_scroll < max_scrolls and sh_ < 3500:
        if n_scroll == 0:
            # 整页一屏可见时，底部没渲染出来多半只是慢：先等一拍重抓，
            # 别急着滑动（标定出的次数会被每个商品复用）
            time.sleep(0.6)
            nodes = dump_nodes_undisturbed("标定详情页")
            found, values = find_target_field(nodes)
            detail = parse_detail(nodes, detail)
            if found:
                break
        human_swipe(x_mid, y_from, x_mid, y_to)
        n_scroll += 1
        nap(pause)
        nodes = dump_nodes_undisturbed("详情页")
        found, values = find_target_field(nodes)
        detail = parse_detail(nodes, detail)

    if not channels_complete(detail):
        detail = parse_top_detail(detail, up_swipes=n_scroll + 1)

    back = find_back_button(nodes, sw, sh_)

    if not found:
        warn(f"滑了 {max_scrolls} 次仍未找到『店内码/货号』。"
             "请确认详情页确实有这个字段，或用 --max-scrolls 调大。")
    else:
        code, artno = split_field_values(values)
        info(f"标定完成：需要滑动 {n_scroll} 次 → "
             f"店内码「{code}」货号「{artno}」")

    nap(dwell)
    wait_while_dialog("返回前")
    remember_detail_fingerprint(nodes)
    tap_back(back)
    nap(settle)
    recover_list_child_after_back()
    return n_scroll, found, values, back, detail


def parse_detail_with_retry(nodes_fn, detail=None, tries=3, wait=0.7,
                            need=None):
    """
    解析详情页字段，带重试。need 给"什么算够"（默认解析到任何字段）；
    整页一屏可见时传 channels_complete——渠道表格没看全多半只是渲染慢，
    等一拍重抓就行。已解析进 detail 的字段不丢。
    """
    need = need or (lambda d: bool(d))
    for attempt in range(tries):
        detail = parse_detail(parse_nodes(nodes_fn()), detail)
        if need(detail):
            return detail
        time.sleep(wait)
    return detail


def whole_page_visible(nodes):
    """详情页是否整屏可见（能看到最底下的『店内码/货号』）。虚拟加高屏幕时为真。"""
    return find_target_field(nodes)[0]


def parse_top_detail(detail=None, up_swipes=3, tries=3):
    """滚回详情页顶部，把顶部字段（渠道表格）补齐一次。up_swipes 按滚下去的
    次数给（每次上滑距离和下滑一样），不用固定滑 6 次。"""
    x, y_from, y_to = 540, 300, 1700
    for _ in range(up_swipes):
        human_swipe(x, y_from, x, y_to, (300, 400))
        time.sleep(0.4)
    return parse_detail_with_retry(lambda: dump_xml(), detail, tries=tries)


def find_back_button(nodes, sw, sh_):
    """找详情页左上角的返回控件；找不到就退回用系统返回键。"""
    cands = [n for n in nodes
             if n["clickable"]
             and n["x2"] < sw * 0.25
             and n["y1"] < sh_ * 0.12
             and n["w"] > 0 and n["h"] > 0]
    if cands:
        n = min(cands, key=lambda n: (n["y1"], n["x1"]))
        return {"mode": "tap", "x": n["cx"], "y": n["cy"],
                "box": (n["x1"], n["y1"], n["x2"], n["y2"])}
    return {"mode": "key"}


def remember_detail_fingerprint(nodes):
    fingerprint = detail_fingerprint(nodes)
    if any(fingerprint):
        CTX.last_detail_fingerprint = fingerprint


def tap_back(back):
    if back and back.get("mode") == "tap":
        human_tap(back["x"], back["y"], box=back.get("box"))
    else:
        helper_result = (helper_action("global", action="back")
                         if HelperState.ready else False)
        if helper_result is False:
            sh("input keyevent 4")
        elif helper_result is None:
            warn("助手返回回执不确定；不盲目补发 Back，交由新鲜页面确认")


def product_identity(product_or_title, barcode=None, spec=None):
    """UI identity; title/spec prevent repeated barcodes from overwriting each other."""
    if isinstance(product_or_title, dict):
        title = product_or_title.get("title")
        barcode = product_or_title.get("barcode")
        spec = product_or_title.get("spec")
    else:
        title = product_or_title
    barcode_value = C.text_value(barcode)
    spec_value = C.normalize_name(spec)
    return ((barcode_value, "", spec_value) if barcode_value
            else ("", C.normalize_name(title), ""))


def remember_row_product(index, prod):
    CTX.row_product_ids[int(index)] = product_identity(prod)


def same_product(a, b):
    """Only the same normalized barcode/title/spec tuple is the same UI row."""
    return product_identity(a) == product_identity(b)


def done_marks():
    """UI identities already persisted in CTX.rows."""
    return {CTX.row_product_ids.get(int(row[0]), product_identity(row[1], row[2]))
            for row in CTX.rows}


def resume_marks():
    """已续跑行的精确身份和规格缺失兼容键。

    同条码不同规格可能是合法变体，不能全局按条码判 done。只有旧 CSV 因
    卡片被屏幕边缘截断而缺少规格时，才用 (条码, 规范标题) 兼容匹配同卡。
    """
    exact = done_marks()
    missing_spec = {
        (identity[0], C.normalize_name(row[1]))
        for row in CTX.rows
        for identity in [CTX.row_product_ids.get(
            int(row[0]), product_identity(row[1], row[2]))]
        if identity[0] and not identity[2]
    }
    return exact, missing_spec


def is_done(product, marks):
    exact, missing_spec = marks if isinstance(marks, tuple) else (marks, set())
    identity = product_identity(product)
    if identity in exact:
        return True
    barcode = C.text_value(product.get("barcode"))
    fallback = (barcode, C.normalize_name(product.get("title")))
    if not barcode or fallback not in missing_spec:
        return False
    bound = CTX.resume_missing_bindings.setdefault(fallback, identity)
    return identity == bound


def relocate(prod, nodes):
    """在当前画面里重新找到这个商品，返回带新坐标的商品或 None。"""
    for p in find_products(nodes, detect_watermarks(nodes)):
        if same_product(p, prod):
            return p
    return None


def wait_list_after_scroll(sw, sh_, retries=5):
    """
    翻列表后的专用等待：FlatList 会短暂卸载旧卡片、新卡片尚未挂载，此时
    解析不到商品并不代表离开列表。这里只等 / 微调，绝不按返回键（旧逻辑
    因此把 App 退到桌面）。返回 (nodes, products)，实在等不到才返回空。
    """
    last_nodes = []
    t0 = time.time()
    for attempt in range(retries):
        nodes, products = dump_products()
        last_nodes = nodes
        if products:
            if attempt:
                info(f"    （列表渲染等待 {time.time() - t0:.1f}s / {attempt} 次）")
            return nodes, products
        if blocking_dialog(nodes):
            wait_while_dialog("翻列表时")
        fg = foreground_pkg(nodes)
        if fg and fg != APP_PKG:
            return nodes, []
        time.sleep(0.65 + 0.2 * attempt)
    # 卡片可能刚好全被上下边缘截断，轻推半张卡让一张完整露出
    x = int(sw * 0.65)
    d = 450 if sh_ >= 3500 else 260
    speed = TALL_SCROLL_SPEED if sh_ >= 3500 else SCROLL_SPEED
    human_swipe(x, int(sh_ * 0.65), x, int(sh_ * 0.65) - d,
                (int(d / speed), int(d / speed * 1.1)))
    time.sleep(0.8)
    nodes, products = dump_products()
    return nodes or last_nodes, products


def close_gap(new, prev_last, sw, sh_, max_back=3):
    """
    翻页后确认没有滑过头：上一屏最下面那个商品必须仍在屏上，两屏才是连续的，
    中间不会有商品从没完整露面过。不在就往回拉一点，直到它重新出现。
    实测一次翻页带着惯性滑过了一整张卡片，那个商品整轮都没被访问。
    返回当前屏的商品列表。
    """
    if prev_last is None or not new:
        return new
    for attempt in range(max_back):
        if any(same_product(p, prev_last) for p in new):
            return new
        if attempt == 0:
            info("    （翻页滑过头了，往回拉一点）")
        x = int(sw * 0.65)
        back_d = int(sh_ * (0.14 if sh_ >= 3500 else 0.22))
        speed = TALL_SCROLL_SPEED if sh_ >= 3500 else SCROLL_SPEED
        human_swipe(x, int(sh_ * 0.40), x, int(sh_ * 0.40) + back_d,
                    (int(back_d / speed), int(back_d / speed * 1.1)))
        time.sleep(0.5 if sh_ >= 3500 else 0.4)
        _, back_on = wait_list_after_scroll(sw, sh_, retries=3)
        if not back_on:
            return new
        new = back_on
    if not any(same_product(p, prev_last) for p in new):
        warn(f"    往回拉了 {max_back} 次仍没看到「{prev_last['title'][:16]}」，"
             "这两屏之间可能漏了商品")
    return new


def detail_fingerprint(nodes):
    detail = parse_detail(nodes)
    return tuple(C.text_value(detail.get(key)) for key in ("开放平台标识", "SPU", "SKU"))


def normalized_digits(value):
    return re.sub(r"\D", "", C.text_value(value))


def detail_matches_product(nodes, prod):
    """Require detail identity, not merely the reused MRN '商品详情' shell."""
    if not is_detail_page(nodes):
        return False
    barcode = normalized_digits(prod.get("barcode"))
    detail = parse_detail(nodes)
    detail_upc = normalized_digits(detail.get("条码"))
    detail_sku = normalized_digits(detail.get("SKU"))
    if len(barcode) == 13:
        return detail_upc == barcode
    if len(barcode) >= 14:
        # ≥14 位内部码：详情页可能把它摆在「条码」（如 16978497750788 的自建
        # 商品，SKU 另有 19 位平台码），也可能摆在「SKU」。两者任一相等即认。
        return detail_sku == barcode or detail_upc == barcode
    fingerprint = tuple(C.text_value(detail.get(key))
                        for key in ("开放平台标识", "SPU", "SKU"))
    return bool(any(fingerprint) and fingerprint != CTX.last_detail_fingerprint)


def open_detail(prod, load, tries=2, *, screen=None, pace=None):
    """
    点商品并等详情页真正出现，用显式等待代替固定的"等加载"停顿。
    成功时把一份等过节奏后的新鲜层级放进 CTX.last_detail_nodes，调用方不必
    立刻重复 dump；打不开返回 False。
    """
    CTX.last_detail_nodes = []
    for _ in range(tries):
        t0 = time.time()
        pace_target = random.uniform(*load)
        # 视口下部卡片首次点击就会撞 MRN 热区压缩（真机 2026-10-11/12：
        # y>0.45*sh 的标题怎么点都不开，白等 DETAIL_OPEN_TIMEOUT 还可能
        # 误触出列表外）。点击前先把它滚到视口上部，一次都不浪费。
        nodes = parse_nodes(dump_xml())
        seated = relocate(prod, nodes)
        tap_screen = screen or screen_size()
        if (seated and not is_detail_page(nodes)
                and seated["tap"][1] > max((n["y2"] for n in nodes),
                                           default=2280) * 0.45):
            scroll_list(tap_screen[0], tap_screen[1],
                        pace or PACING["ultra"], anchor=seated)
            time.sleep(0.4)
            nodes = parse_nodes(dump_xml())
            prod = relocate(prod, nodes) or seated
        # 商品详情标题点击必须走直接 ADB：2026-10-12 shard46 三件顽固
        # "未打开"用同一动态 bounds 的 input tap 均一次成功，Helper tap
        # 则跨多轮失败。身份校验仍在下方逐条码/SKU执行。
        tx, ty = human_point(prod["tap"][0], prod["tap"][1], prod.get("box"))
        sw_now, sh_now = screen_size()
        if not (0 <= tx < sw_now and 0 <= ty < sh_now):
            warn(f"商品点击坐标 ({tx},{ty}) 超出当前屏 {sw_now}x{sh_now}")
            return False
        sh(f"input tap {tx} {ty}")
        time.sleep(0.15 if HelperState.has_any_text_query else 0.3)
        fast_check = HelperState.has_any_text_query
        while time.time() - t0 < DETAIL_OPEN_TIMEOUT:
            if fast_check:
                match = helper_find_any_text(
                    ["我知道了", "订单号", "顾客修改", "商品详情"])
                if match is None:
                    fast_check = False
                    continue
                if match and BLOCKING_DIALOG_PAT.search(match):
                    wait_while_dialog("等详情页时")
                elif match == "商品详情":
                    time.sleep(max(0.0, pace_target - (time.time() - t0)))
                    fresh_nodes = parse_nodes(dump_xml())
                    if detail_matches_product(fresh_nodes, prod):
                        CTX.last_detail_nodes = fresh_nodes
                        return True
                time.sleep(0.08)
                continue

            nodes = parse_nodes(dump_xml())
            if detail_matches_product(nodes, prod):
                # 页面出来了，但仍按原来的节奏停够再抓一份新鲜层级，给内容渲染时间
                time.sleep(max(0.0, pace_target - (time.time() - t0)))
                fresh_nodes = parse_nodes(dump_xml())
                CTX.last_detail_nodes = (fresh_nodes if detail_matches_product(fresh_nodes, prod)
                                         else nodes)
                return True
            if blocking_dialog(nodes):
                wait_while_dialog("等详情页时")
            time.sleep(0.2)
        # 超时：先确认是不是只是开得慢，再决定要不要重点
        time.sleep(0.8)
        nodes = parse_nodes(dump_xml())
        if detail_matches_product(nodes, prod):
            CTX.last_detail_nodes = nodes
            return True
        if is_detail_page(nodes):
            # 详情 shell 已出现但身份对不上：多半是上一单残留或点错卡。
            # 先给 0.8s 让身份区渲染完再复核一次，别把"渲染中"的目标
            # 详情误退掉——退掉后列表乱、重试链拖 26s+（2026-10-12 shard46）。
            time.sleep(0.8)
            nodes = parse_nodes(dump_xml())
            if detail_matches_product(nodes, prod):
                CTX.last_detail_nodes = nodes
                return True
            # A stale previous detail shell swallowed the tap; return once before retrying.
            tap_back(find_back_button(nodes, *screen_size()))
            time.sleep(0.7)
            nodes = parse_nodes(dump_xml())
        fresh = relocate(prod, nodes)
        if fresh is None:
            # 既不在详情页也看不到这个商品：可能点开了别的东西，先回到列表
            _, on_list = ensure_list_page()
            fresh = relocate(prod, parse_nodes(dump_xml())) if on_list else None
            if fresh is None:
                return False
        if fresh["tap"][1] > max((n["y2"] for n in nodes), default=2280) * 0.45:
            # 卡片停在视口下部时 MRN 的点击热区会被压缩（真机 2026-10-11
            # 验证：同一张卡标题在 y≈2833 怎么点都不开，滚到 y≈690 一点
            # 就开）。先把卡片挪到视口上部再点，别在原位置死磕。
            sw, sh_ = screen or screen_size()
            scroll_list(sw, sh_, pace or PACING["ultra"], anchor=fresh)
            time.sleep(0.4)
            nodes = parse_nodes(dump_xml())
            fresh = relocate(prod, nodes) or fresh
        warn(f"    详情页没打开，按当前位置重点一次：{prod['title'][:20]}")
        prod = fresh
    return False


def tall_detail_ready(detail):
    """高屏详情核心是否齐全；重量/采购价等字段允许业务上为空。
    开放平台标识部分商品天生缺失（真机 2026-10-10 验证），允许为空。"""
    core = ("SPU", "SKU", "采购单位_详情",
            "美团闪购状态", "饿了么状态", "京东到家状态")
    return channels_complete(detail) and all(detail.get(k) for k in core)


def product_detail_errors(detail, prod):
    """Validate one detail capture with the same card fallbacks as the manifest gate."""
    row = dict(detail)
    row.update(dict(zip(
        CARD_COLUMNS,
        card_cols(prod["title"], prod.get("barcode", ""), spec=prod.get("spec")),
    )))
    return C.row_detail_errors(row)


def collect_tall_detail(prod, nodes, sw, sh_, timeout=2.8):
    """
    高屏专用：原地轮询同一整页，合并详情字段。核心字段和（标品的）店内码/
    货号齐全立即返回；超长页的 Other Information 只补一次小滑。
    """
    detail, found, values = {}, False, []
    deadline = time.time() + timeout
    while True:
        if blocking_dialog(nodes):
            wait_while_dialog("读取详情时")
            nodes = dump_nodes_undisturbed("详情页")
        detail = parse_detail(nodes, detail)
        found2, values2 = find_target_field(nodes)
        if found2:
            found, values = True, values2
        target_ready = found or is_custom_product(prod)
        if tall_detail_ready(detail) and target_ready:
            break
        if time.time() >= deadline:
            break
        time.sleep(0.18)
        nodes = dump_nodes_undisturbed("详情页")

    # 多规格商品会把「其他信息」推到高屏之外。一次约 1000px 的旧补滑在
    # 10 个规格的筹码商品上连续多轮都读不到；真机 2026-10-14 验证约
    # 2000px 后四个字段完整出现。逐次合并、齐全即停，普通商品不多滑。
    for _ in range(3):
        if all(detail.get(k) for k in LOWER_DETAIL_COLUMNS):
            break
        human_swipe(sw // 2, int(sh_ * 0.78), sw // 2, int(sh_ * 0.34),
                    (700, 850))
        time.sleep(0.5)
        lower_nodes = dump_nodes_undisturbed("详情页下方")
        detail = parse_detail(lower_nodes, detail)
        found2, values2 = find_target_field(lower_nodes)
        if found2:
            found, values = True, values2
        nodes = lower_nodes
    return detail, found, values, nodes


def visit_product(idx, prod, n_scroll, back, sw, sh_, pace,
                  verify, max_scrolls):
    """
    访问一个商品：点标题 → 等加载 → 滑 N 次 → 停留 → 返回。
    verify=True 时滚动过程中逐屏解析：渠道表格（状态/零售价/库存）和
    『店内码/货号』依次经过屏幕，合并成详情字段。
    """
    load, pause, dwell, settle = pace
    t0 = time.time()
    offset = video_offset()

    title = prod["title"][:36] + ("…" if len(prod["title"]) > 36 else "")

    def skip(status, why):
        info(f"[{idx:>3}] ✗ {title}  ({time.time() - t0:.1f}s)  {why}")
        CTX.rows.append([idx, prod["title"], prod["barcode"], "", "",
                         status, offset, *capture_meta()])
        remember_row_product(idx, prod)
        return time.time() - t0

    # 每日增量：指纹一致的商品不点详情页，直接写携带行。卡片此刻可能
    # 只露出一半（read_card 返回 None）；翻页后完整露出时会更新 CTX.cards，
    # 因此这里拿不到完整卡片就当"有变化"处理，走完整详情页路径。
    if CTX.carry_baseline is not None:
        listing = C.listing_identity(prod["title"], prod["barcode"])
        baseline_entry = CTX.carry_baseline.get(listing)
        card = CTX.cards.get(product_identity(prod))
        if baseline_entry is not None and card:
            detail_values = compare_card_baseline(card, baseline_entry)
            if detail_values is not None:
                CTX.carry_seen.add(listing)
                _carry_row(idx, prod, detail_values)
                return time.time() - t0

    if not open_detail(prod, load, screen=(sw, sh_), pace=pace):
        return skip("未打开", "详情页没打开，已跳过")

    detail = {}
    nodes = CTX.last_detail_nodes or parse_nodes(dump_xml())

    # 平台限流：复用 open_detail 的层级，正常路径不再多 dump 一次；
    # 退回列表、等一阵再重开，并给之后的每个商品加停留。
    current_nodes = nodes
    for attempt in range(THROTTLE_RETRIES + 1):
        if not is_throttled(current_nodes):
            break
        wait = THROTTLE_BACKOFF[min(CTX.throttle_hits, len(THROTTLE_BACKOFF) - 1)]
        CTX.throttle_hits += 1
        CTX.extra_pause = min(CTX.extra_pause + THROTTLE_SLOWDOWN,
                              THROTTLE_SLOWDOWN_MAX)
        tap_back(back)
        if attempt == THROTTLE_RETRIES:
            warn(f"    平台限流：{title[:20]} 重开 {THROTTLE_RETRIES} 次仍被拒，"
                 f"暂停 {wait} 秒后继续下一个")
            time.sleep(wait)
            return skip("限流", "平台限流，已跳过（续跑时会重做）")
        warn(f"    平台限流（接口 429「自适应限流策略生效」）：暂停 {wait} 秒后重开；"
             f"之后每个商品多停 {CTX.extra_pause:.0f} 秒")
        time.sleep(wait)
        _, on_list = ensure_list_page()
        fresh = relocate(prod, parse_nodes(dump_xml())) if on_list else None
        if fresh is None or not open_detail(
                fresh, load, screen=(sw, sh_), pace=pace):
            return skip("未打开", "限流后重开失败，已跳过")
        current_nodes = CTX.last_detail_nodes or parse_nodes(dump_xml())
        detail = parse_detail(current_nodes) if verify else {}

    # 高屏快路径：一套就绪循环拿齐整页字段，不进入下面普通屏的多轮 dump/补滑。
    if verify and sh_ >= 3500:
        detail, found, values, current_nodes = collect_tall_detail(
            prod, current_nodes, sw, sh_)
        complete = not product_detail_errors(detail, prod)
        if not complete:
            status = "详情不全"
        elif is_custom_product(prod):
            status = "自建商品"
        else:
            # 店内码/货号区块与开放平台标识一样会天生缺失（同一批商品，
            # 真机 2026-10-10 验证）；详情字段已齐即视为成功。
            status = "成功"
        code, artno = split_field_values(values)
        CTX.details[C.row_identity(idx, prod["title"], prod["barcode"])] = detail
        if CTX.extra_pause:
            time.sleep(CTX.extra_pause)
        wait_while_dialog("返回前")
        remember_detail_fingerprint(current_nodes)
        tap_back(back)
        time.sleep(random.uniform(0.45, 0.65))
        recover_list_child_after_back()
        dt = time.time() - t0
        mark = {"成功": "✓", "未找到": "✗", "详情不全": "✗",
                "自建商品": "○"}[status]
        shown = "  ".join(filter(None, [f"店内码={code}" if code else "",
                                        f"货号={artno}" if artno else ""]))
        info(f"[{idx:>3}] {mark} {title}  ({dt:.1f}s)"
             + (f"  {shown}" if shown else ""))
        CTX.rows.append([idx, prod["title"], prod["barcode"], code, artno,
                         status, offset, *capture_meta()])
        remember_row_product(idx, prod)
        return dt

    # 普通屏路径：按标定次数滚动，逐屏合并字段。
    if verify:
        detail = parse_detail(current_nodes, detail)
    x_mid = sw // 2
    y_from, y_to = int(sh_ * 0.72), int(sh_ * 0.30)
    for _ in range(n_scroll):
        human_swipe(x_mid, y_from, x_mid, y_to)
        nap(pause)
        if verify:
            # Ordinary-screen verification must merge every scrolled section; stopping dumps
            # once channel rows are complete would miss the lower "其他信息" fields.
            time.sleep(0.35)
            detail = parse_detail(parse_nodes(dump_xml()), detail)

    status, values, found = "未校验", [], False
    if verify and is_custom_product(prod):
        # 自建商品详情页没有『店内码/货号』这一行，不补滑，照常返回
        status = "自建商品"
    elif verify:
        # 高屏模式（>=3500px）整页都在一屏内：字段没出现是渲染慢，不是页面长，
        # 原地重抓；绝不能往下滑 8 次。普通屏才按标定次数补滑。
        nodes = (current_nodes if sh_ >= 3500 and n_scroll == 0
                 else dump_nodes_undisturbed("详情页"))
        found, values = find_target_field(nodes)
        detail = parse_detail(nodes, detail)
        extra = 0
        if sh_ >= 3500:
            for _ in range(4):
                if found and channels_complete(detail):
                    break
                time.sleep(0.5)
                nodes = dump_nodes_undisturbed("详情页")
                found2, values2 = find_target_field(nodes)
                if found2:
                    found, values = True, values2
                detail = parse_detail(nodes, detail)
            # 超长标题/多规格会把「其他信息」挤到高屏下沿外；只对缺字段的
            # 少数商品补一次小滑，普通商品不增加任何开销。
            if any(not detail.get(k) for k in LOWER_DETAIL_COLUMNS):
                human_swipe(x_mid, int(sh_ * 0.72), x_mid, int(sh_ * 0.50),
                            (500, 650))
                time.sleep(0.35)
                lower_nodes = dump_nodes_undisturbed("详情页下方")
                detail = parse_detail(lower_nodes, detail)
        else:
            # 补滑只为『店内码/货号』没找到的商品（页面比标定样本长）；渠道表格
            # 在上方，往下多滑永远看不到，缺失时在找到后滚回页顶补一次。
            while not found and extra < max(0, max_scrolls - n_scroll):
                human_swipe(x_mid, y_from, x_mid, y_to)
                extra += 1
                nap(pause)
                nodes = dump_nodes_undisturbed("详情页")
                found2, values2 = find_target_field(nodes)
                if found2:
                    found, values = True, values2
                detail = parse_detail(nodes, detail)
                if found:
                    break
        status = "成功" if found else "未找到"
        if extra:
            info(f"    （比标定多滑了 {extra} 次）")
        # 普通屏的顶部渠道表格没看全才滚回页顶；高屏原地重抓已经看完整页
        if sh_ < 3500 and not channels_complete(detail):
            detail = parse_top_detail(detail, up_swipes=n_scroll + 1)
            info("    （回滚到页顶补了渠道信息）")
    if verify:
        complete = not product_detail_errors(detail, prod)
        if not complete:
            status = "详情不全"
        elif is_custom_product(prod):
            status = "自建商品"
        else:
            # 同上：店内码/货号区块可天生缺失，详情字段已齐即成功。
            status = "成功"

    if sh_ >= 3500:
        time.sleep(random.uniform(0.03, 0.08))
    else:
        nap(dwell)
    if CTX.extra_pause:
        time.sleep(CTX.extra_pause)
    wait_while_dialog("返回前")   # 返回键会直接关掉弹窗
    remember_detail_fingerprint(nodes if 'nodes' in locals() else current_nodes)
    tap_back(back)
    if sh_ >= 3500:
        time.sleep(random.uniform(0.45, 0.65))
    else:
        nap(settle)
    recover_list_child_after_back()

    code, artno = split_field_values(values)
    if verify:
        CTX.details[C.row_identity(idx, prod["title"], prod["barcode"])] = detail
    dt = time.time() - t0
    mark = {"成功": "✓", "未找到": "✗", "详情不全": "✗",
            "未打开": "✗", "限流": "✗", "自建商品": "○", "未校验": "·"}[status]
    title = prod["title"][:36] + ("…" if len(prod["title"]) > 36 else "")
    shown = "  ".join(filter(None, [f"店内码={code}" if code else "",
                                    f"货号={artno}" if artno else ""]))
    info(f"[{idx:>3}] {mark} {title}  ({dt:.1f}s)"
         + (f"  {shown}" if shown else ""))

    CTX.rows.append([idx, prod["title"], prod["barcode"], code, artno,
                     status, offset, *capture_meta()])
    remember_row_product(idx, prod)
    return dt


def scroll_list(sw, sh_, pace, anchor=None):
    """
    翻列表（右侧列表区域，避开左侧分类栏）。

    给了 anchor（上一屏最下面那个商品）时，精确地把它挪到列表顶部：两屏之间
    留一个商品作衔接，中间不会有商品从没完整露面过。

    必须慢速滑：实测手指 1.2px/ms 时带惯性，同样滑 600px 内容实际走了
    375~700px，还出现过一次带走 ~1500px、整张卡片被跳过；<=0.4px/ms 时
    内容移动 ≈ 手指移动（600→593~597，900→890），可以精确控制。
    """
    x = int(sw * 0.65)
    if anchor is None and sh_ >= 3500 and HelperState.has_semantic_scroll:
        semantic_result = helper_action(
            "scroll_at", x=x, y=int(sh_ * 0.55), direction="forward")
        if semantic_result is True:
            time.sleep(0.8)
            return
        if semantic_result is None:
            time.sleep(0.8)
            return
        # A confirmed rejection can safely fall back to one gesture.
    if anchor is not None:
        d = anchor["box"][1] - int(sh_ * LIST_TOP_Y)
    else:
        d = int(sh_ * 0.45)
    d = max(SCROLL_MIN, min(d, TALL_SCROLL_MAX if sh_ >= 3500
                            else int(sh_ * 0.70)))
    y1 = int(sh_ * 0.80)
    speed = SCROLL_SPEED if anchor is not None else (
        TALL_SCROLL_SPEED if sh_ >= 3500 else SCROLL_SPEED)
    dur = int(d / speed)
    # 锚点翻页必须精确：Helper semantic scroll 不接受距离，普通 Helper swipe
    # 又曾有回执成功但 MRN 未滚动。直接 ADB 慢滑，真机验证内容位移≈手指位移。
    if anchor is not None:
        sh(f"input swipe {x} {y1} {x} {y1 - d} {dur}")
    else:
        human_swipe(x, y1, x, y1 - d, (dur, int(dur * 1.1)))
    if sh_ >= 3500:
        time.sleep(0.5)  # FlatList 重新挂载卡片；ultra 的 0.12s 不够
    else:
        nap(pace[1])


def find_click_target_xml(xml, text, *, max_x=None, min_y=0):
    """Return nearest clickable ancestor bounds for a visible matching text node."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    parents = {child: parent for parent in root.iter() for child in parent}
    for element in root.iter("node"):
        if (element.get("text") or "").strip() != text:
            continue
        own = BOUNDS_PAT.match(element.get("bounds") or "")
        own_box = tuple(map(int, own.groups())) if own else None
        if not own_box or own_box[3] <= min_y or (max_x is not None and own_box[2] > max_x):
            continue
        current = element
        fallback = element.get("bounds")
        while current is not None:
            bounds = current.get("bounds")
            if current.get("clickable") == "true" and bounds:
                match = BOUNDS_PAT.match(bounds)
                return tuple(map(int, match.groups())) if match else None
            current = parents.get(current)
        match = BOUNDS_PAT.match(fallback or "")
        return tuple(map(int, match.groups())) if match else None
    return None


def tap_text_xml(text, *, max_x=None, min_y=0, force_adb=False):
    xml = dump_xml()
    box = find_click_target_xml(xml, text, max_x=max_x, min_y=min_y)
    if not box:
        return False
    x1, y1, x2, y2 = box
    x, y = (x1 + x2) // 2, (y1 + y2) // 2
    if force_adb:
        sh(f"input tap {x} {y}")
    else:
        human_tap(x, y, box=box)
    return True


def _tap_text(nodes, text, *, min_y=0):
    """Tap a visible text node or its surrounding card; return whether one was found."""
    matches = [node for node in nodes
               if node["text"] == text and node["y2"] > min_y
               and node["w"] > 0 and node["h"] > 0]
    if not matches:
        return False
    node = max(matches, key=lambda item: item["w"] * item["h"])
    human_tap(node["cx"], node["cy"], box=(node["x1"], node["y1"], node["x2"], node["y2"]))
    return True


# 误触卡片里的价格/改价热区会进入这些页；它们都不是详情页，直接返回即可。
LIST_CHILD_PAGE_TEXTS = {"修改价格", "零售价趋势"}


def on_list_child_page(nodes):
    return any(any(marker in node["text"] for marker in LIST_CHILD_PAGE_TEXTS)
               for node in nodes if node["text"])


def recover_list_child_after_back():
    """返回详情后若误落到价格/趋势页，再返回一次；不做整套导航。

    Helper v9 文字索引命中只是初筛，必须用新鲜 XML 复核后才能决定
    是否返回——系统文字索引可能残留上一页文字，命中不代表当前页。
    查询不可用（None/超时）时同样回落 fresh XML 判定，绝不无条件
    多退一层（会把正常列表页退到经营页）。
    """
    if HelperState.has_any_text_query:
        match = helper_find_any_text(sorted(LIST_CHILD_PAGE_TEXTS))
        if match == "":
            return
        # 命中可能是系统索引残留；None 表示查询失败。两者都必须用
        # fresh XML 判断，绝不能直接 Back。
        nodes = parse_nodes(dump_xml())
        if not on_list_child_page(nodes):
            return
    else:
        nodes = parse_nodes(dump_xml())
        if not on_list_child_page(nodes):
            return
    info("返回后检测到商品列表误触页，自动再返回一次")
    helper_result = (helper_action("global", action="back")
                     if HelperState.ready else False)
    if helper_result is False:
        sh("input keyevent 4")
    time.sleep(1.5)


def valid_product_list(nodes, products):
    """Product rows are valid only when no list-child overlay/page is active."""
    return bool(products) and not is_detail_page(nodes) and not on_list_child_page(nodes)


def wait_product_list_ready(timeout=12.0):
    """Wait through MRN activity rebuild without issuing navigation actions."""
    deadline = time.time() + timeout
    last_nodes = []
    while time.time() < deadline:
        nodes, products = dump_products()
        last_nodes = nodes
        if valid_product_list(nodes, products):
            return nodes, products
        if blocking_dialog(nodes):
            wait_while_dialog("等待门店商品页时")
        time.sleep(0.6)
    return last_nodes, []


def operation_tab_point(screen):
    return int(screen[0] * 0.463), int(screen[1] * 0.965)


def title_bar_back_target(nodes, title):
    """Find an MRN page's drawn back button, falling back beside its title."""
    titles = [node for node in nodes
              if node["text"] == title and node["w"] > 0 and node["h"] > 0]
    if not titles:
        return None
    heading = min(titles, key=lambda node: node["y1"])
    buttons = [node for node in nodes
               if node["clickable"] and node["w"] > 0 and node["h"] > 0
               and node["x2"] <= heading["x1"]
               and node["y1"] <= heading["cy"] <= node["y2"]]
    if buttons:
        button = max(buttons, key=lambda node: node["w"] * node["h"])
        return button["cx"], button["cy"], (
            button["x1"], button["y1"], button["x2"], button["y2"])
    screen_w = max((node["x2"] for node in nodes), default=1080)
    return int(screen_w * 0.06), heading["cy"], None


def tap_title_bar_back(nodes, title):
    target = title_bar_back_target(nodes, title)
    if not target:
        return False
    x, y, box = target
    if box:
        human_tap(x, y, box=box)
    else:
        sh(f"input tap {x} {y}")
    return True


def navigate_to_product_list(max_steps=8):
    """Recover from launcher/workbench/operation pages back to 门店商品."""
    for attempt in range(max_steps):
        nodes, products = dump_products()
        if valid_product_list(nodes, products):
            info(f"已自动恢复商品列表页（导航 {attempt} 步）")
            return nodes, products
        if blocking_dialog(nodes):
            wait_while_dialog("自动恢复列表时")
            continue
        if on_list_child_page(nodes):
            info("检测到商品列表误触页，自动返回")
            helper_result = (helper_action("global", action="back")
                             if HelperState.ready else False)
            if helper_result is False:
                sh("input keyevent 4")
            time.sleep(1.5)
            continue
        texts = {node["text"] for node in nodes if node["text"]}
        if "退款订单" in texts and tap_title_bar_back(nodes, "退款订单"):
            info("检测到退款订单页，点击标题栏返回经营页")
            time.sleep(1.5)
            continue
        if "门店商品" in texts and tap_text_xml("门店商品"):
            ready_nodes, ready_products = wait_product_list_ready()
            if ready_products:
                info(f"已自动恢复商品列表页（导航 {attempt + 1} 步）")
                return ready_nodes, ready_products
            continue
        if "今日数据" in texts and "经营" in texts:
            # MRN 底栏文字节点有时 bounds=[0,0][0,0]；优先动态点击，失效时
            # 用真实 wm size 的经营 tab 比例。旧代码用层级 max bounds + x=屏幕
            # 中心，实际落在订单/空白区，导致连续启动均导航失败。
            if not tap_text_xml("经营", min_y=220, force_adb=True):
                screen_w, screen_h = screen_size()
                x, y = operation_tab_point((screen_w, screen_h))
                sh(f"input tap {x} {y}")
            time.sleep(2.0)
            continue
        fg = foreground_pkg(nodes)
        if fg and fg != APP_PKG:
            sh(f"monkey -p {APP_PKG} -c android.intent.category.LAUNCHER 1")
            time.sleep(3.0)
            continue
        # From another in-app page, one cautious back may expose workbench/operation.
        if fg == APP_PKG:
            helper_result = (helper_action("global", action="back")
                             if HelperState.ready else False)
            if helper_result is False:
                sh("input keyevent 4")
            time.sleep(1.5)
            continue
        warn("自动恢复列表失败：无法识别当前页面")
        break
    return [], []


def ensure_list_page(max_back=3):
    """
    确认当前在商品列表页；不在就按返回键逐层找回来。

    详情页里字段找不到时返回路径可能走偏，把 App 带出商品列表甚至退回
    首页。这时 dump 解析不出任何商品，绝不能当成"列表到底了"——那会让
    整轮静默截断（目标 20 实际只跑 8 个，CSV 看起来还很正常）。

    返回 (nodes, products)；找不回列表页时 products 为空。
    """
    for attempt in range(max_back + 1):
        nodes, products = dump_products()
        if valid_product_list(nodes, products):
            if attempt:
                info(f"已返回商品列表页（回退 {attempt} 层）")
            return nodes, products
        # 挡住列表的是店铺弹窗：按返回会替店员把它关掉，只能等
        if blocking_dialog(nodes):
            wait_while_dialog("找回列表页时")
            continue
        if on_list_child_page(nodes):
            info("检测到商品列表误触页，自动返回")
            helper_result = (helper_action("global", action="back")
                             if HelperState.ready else False)
            if helper_result is False:
                sh("input keyevent 4")
            time.sleep(1.5)
            continue
        # 先别急着按返回：可能只是过渡状态。实测订单弹窗弹出前会先有约 1 秒
        # 的加载转圈，那一刻列表读不到、弹窗也还没出来；这时按返回，弹窗
        # 若恰好跟着弹出就会被直接关掉。
        time.sleep(ENSURE_SETTLE)
        nodes, products = dump_products()
        if valid_product_list(nodes, products):
            return nodes, products
        if blocking_dialog(nodes):
            wait_while_dialog("找回列表页时")
            continue
        # 已经被退出 App 了，再按返回只会离列表页越来越远（一路退到桌面）
        fg = foreground_pkg(nodes)
        if fg and fg != APP_PKG:
            warn(f"已经退出牵牛花（当前是 {fg}），停止回退")
            return [], []
        if attempt < max_back:
            warn(f"当前不在商品列表页，按返回键找回（第 {attempt + 1} 次）")
            # MRN 是单 Activity：goods-list 只是内部页。连按返回会把
            # MRNActivity 整个 finish 掉、App 退穿到桌面（2026-10-12
            # 21:56 logcat 实锤，随后桌面 tap 误开钉钉）。第 2 次返回前
            # 先 monkey 拉回 App 首页重来，绝不退穿。
            if attempt >= 1:
                info("连续返回会退穿 App，改用重新拉起牵牛花")
                sh(f"monkey -p {APP_PKG} -c android.intent.category.LAUNCHER 1")
                time.sleep(3.0)
                nodes, products = dump_products()
                if products:
                    return nodes, products
                continue
            helper_result = (helper_action("global", action="back")
                             if HelperState.ready else False)
            if helper_result is False:
                sh("input keyevent 4")
            time.sleep(1.8)
    return [], []


def category_selection_valid(category, nodes):
    if not category:
        return True
    leaf = category.rsplit("/", 1)[-1]
    return selected_list_count(nodes, leaf) is not None


def restore_category_after_navigation(category):
    if not category:
        return dump_products()
    count, nodes = select_category_path(category)
    products = find_products(nodes, detect_watermarks(nodes))
    if count > 0 and not products:
        return wait_product_list_ready()
    return nodes, products


def reset_category_to_top(category):
    """Switch to 全部商品 and back so MRN resets the target leaf list to top."""
    before_nodes, before_products = dump_products()
    before_keys = {product_identity(product) for product in before_products}
    if not tap_text_xml("全部商品", max_x=260, min_y=220, force_adb=True):
        return [], []
    if not wait_category_list_refresh("全部商品", before_keys, 8.0):
        return [], []
    try:
        return restore_category_after_navigation(category)
    except C.CaptureContractError:
        return [], []


def selected_list_count(nodes, expected_title):
    screen_width = max((node["x2"] for node in nodes), default=0)
    candidates = []
    for node in nodes:
        match = re.fullmatch(r"(.+)（([\d,]+)）", node["text"] or "")
        if (match and match.group(1) == expected_title and "kaixin" not in node["text"]
                and node["x1"] > screen_width * 0.15):
            candidates.append(int(match.group(2).replace(",", "")))
    return candidates[0] if candidates else None


def category_list_ready(count, new_keys, before_keys, last_new_keys):
    """Require target title plus a changed, stable FlatList identity set."""
    return (count is not None and bool(new_keys)
            and (not before_keys or new_keys != before_keys)
            and new_keys == last_new_keys)


def wait_category_list_refresh(leaf, before_keys, timeout):
    """Wait for a clicked leaf's title and FlatList to refresh and stabilize."""
    deadline = time.monotonic() + timeout
    last_new_keys = None
    while time.monotonic() < deadline:
        nodes = parse_nodes(dump_xml())
        count = selected_list_count(nodes, leaf)
        products = find_products(nodes, detect_watermarks(nodes))
        new_keys = {product_identity(product) for product in products}
        # MRN标题会先切换、FlatList稍后才刷新。要求新列表身份变化后
        # 连续两次一致，不能把上一叶子幽灵卡当目标类目商品。
        if category_list_ready(count, new_keys, before_keys, last_new_keys):
            return count, nodes
        changed = bool(new_keys) and (not before_keys or new_keys != before_keys)
        last_new_keys = new_keys if changed else None
        time.sleep(0.35)
    return None


def select_category_path(category, timeout=8.0):
    """Select 一级/叶子 on the left category panel and return its live product count."""
    if not category:
        return None
    parts = [part.strip() for part in category.split("/", 1) if part.strip()]
    if not parts:
        raise C.CaptureContractError("--category 为空")
    top = parts[0]
    leaf = parts[-1]
    xml = dump_xml()
    before_nodes = parse_nodes(xml)
    before_products = find_products(before_nodes, detect_watermarks(before_nodes))
    before_keys = {product_identity(product) for product in before_products}
    current_count = selected_list_count(before_nodes, leaf)
    if current_count is not None:
        if not tap_text_xml("全部商品", max_x=260, min_y=220, force_adb=True):
            raise C.CaptureContractError(f"目标类目已选中但无法切出重置：{category}")
        refreshed = wait_category_list_refresh("全部商品", before_keys, timeout)
        if not refreshed:
            raise C.CaptureContractError(f"目标类目重置到全部商品失败：{category}")
        _, before_nodes = refreshed
        before_products = find_products(before_nodes, detect_watermarks(before_nodes))
        before_keys = {product_identity(product) for product in before_products}
        xml = dump_xml()
    if len(parts) == 2 and find_click_target_xml(xml, leaf, max_x=260, min_y=220) is None:
        if not tap_text_xml(top, max_x=260, min_y=220, force_adb=True):
            raise C.CaptureContractError(f"找不到一级类目：{top}")
        deadline = time.time() + timeout
        while time.time() < deadline:
            xml = dump_xml()
            if find_click_target_xml(xml, leaf, max_x=260, min_y=220) is not None:
                break
            time.sleep(0.35)
        else:
            raise C.CaptureContractError(f"一级类目未展开：{top}")
    elif len(parts) == 1 and find_click_target_xml(xml, top, max_x=260, min_y=220) is None:
        raise C.CaptureContractError(f"找不到一级类目：{top}")
    if len(parts) == 2:
        for _ in range(3):
            time.sleep(0.8)
            if not tap_text_xml(leaf, max_x=260, min_y=220, force_adb=True):
                continue
            refreshed = wait_category_list_refresh(leaf, before_keys, timeout)
            if refreshed:
                return refreshed
        raise C.CaptureContractError(f"选择类目后未读到标题计数：{category}")
    if not tap_text_xml(top, max_x=260, min_y=220, force_adb=True):
        raise C.CaptureContractError(f"找不到一级类目：{top}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        nodes = parse_nodes(dump_xml())
        count = selected_list_count(nodes, leaf)
        if count is not None:
            return count, nodes
        time.sleep(0.35)
    raise C.CaptureContractError(f"选择一级类目后未读到标题计数：{top}")


def find_list_total(nodes, screen_width, screen_height, watermarks=(),
                    expected_title="全部商品"):
    totals = []
    for node in nodes:
        match = re.search(r"（([\d,]+)）$", node["text"])
        title = re.sub(r"（[\d,]+）$", "", node["text"])
        if (match and title == expected_title and node["text"] not in watermarks
                and node["x1"] > screen_width * 0.15
                and node["y1"] < screen_height * 0.12):
            totals.append((node["y1"], int(match.group(1).replace(",", "")), node["text"]))
    return min(totals) if totals else None


def resolve_resume_count(requested_count, expected_count):
    """Return the immutable full target for a resumed capture."""
    if not isinstance(expected_count, int) or isinstance(expected_count, bool) or expected_count <= 0:
        raise C.CaptureContractError("父 manifest expected_product_count 必须是正整数")
    if requested_count not in (None, 0, expected_count):
        raise C.CaptureContractError(
            f"--count {requested_count} 与父 manifest 全量范围 {expected_count} 不一致")
    return expected_count


def main():
    ap = argparse.ArgumentParser(
        description="牵牛花商品详情批量浏览（纯 ADB，无 LLM）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--count", type=int, default=None,
                    help="要浏览的商品数量；0=读取当前列表标题里的平台总数；"
                         "不传时新采集默认 20、续跑继承父 manifest 全量范围")
    ap.add_argument("--serial", help="指定设备序列号")
    ap.add_argument("--adb", default=DEFAULT_ADB, help="adb 可执行文件路径")
    ap.add_argument("--pace", choices=list(PACING), default="natural",
                    help="操作节奏：ultra 最快（建议配 --tall-screen）/ fast / natural 默认 / demo 录像演示")
    ap.add_argument("--tall-screen", action="store_true",
                    help="临时把逻辑屏幕改成双高并降低密度：列表一屏约 6 张卡、详情整页"
                         "一次读完；结束/中断/报错自动恢复原分辨率和密度")
    ap.add_argument("--no-helper", action="store_true",
                    help="不使用 ARTEMIS 无障碍助手加速，强制走 input / uiautomator")
    ap.add_argument("--no-helper-dump", action="store_true",
                    help="UI 层级不走助手、仍用 uiautomator（助手只做手势）。"
                         "默认在助手 v7 及以上时层级也走助手")
    ap.add_argument("--require-helper", action="store_true",
                    help="ARTEMIS 助手不可用时快速失败（正式分片采集用），"
                         "不静默降级到慢速 input/uiautomator")
    ap.add_argument("--record", action="store_true", help="用 scrcpy 录制")
    ap.add_argument("--video", default=None, help="录制输出路径")
    ap.add_argument("--verify-every", type=int, default=5,
                    help="每隔几个商品 dump 校验一次字段（1=每个都校验并取值，"
                         "0=完全不校验最快最隐蔽；默认 5）")
    ap.add_argument("--max-scrolls", type=int, default=8,
                    help="详情页最多滑动次数（默认 8）")
    ap.add_argument("--calibrate-only", action="store_true",
                    help="只做标定并打印识别结果，不执行浏览")
    ap.add_argument("--keep-a11y", action="store_true",
                    help="保留 Artemis 无障碍服务（默认会摘掉）")
    ap.add_argument("--restore-a11y", action="store_true",
                    help="还原之前备份的无障碍服务设置后退出")
    ap.add_argument("--out-dir", default=os.path.dirname(
        os.path.abspath(__file__)), help="CSV / 视频输出目录")
    ap.add_argument("--seed", type=int, help="随机种子（便于复现）")
    ap.add_argument("--resume", metavar="MANIFEST_OR_CSV",
                    help="从上一轮 final manifest 接着跑；旧 CSV 需显式 --allow-legacy-resume")
    ap.add_argument("--allow-legacy-resume", action="store_true",
                    help="允许没有 final manifest 的旧 CSV（不用于正式 profile 流水线）")
    ap.add_argument("--checkpoint-every", type=int, default=50,
                    help="每处理多少个商品原子保存一次 CSV（默认 50；0=只在结束时保存）")
    ap.add_argument("--dismiss-order-dialogs", action="store_true",
                    help="关闭‘顾客修改了订单信息’弹窗的‘我知道了’按钮（需业务授权）")
    ap.add_argument("--carry-baseline", metavar="JSON",
                    help="每日增量携带基线：指纹一致的商品跳过详情页，"
                         "详情列沿用基线（由 qnh_daily_baseline.py 导出）")
    ap.add_argument("--profile", help="config/qnh_capture_profiles.json 中的门店 profile")
    ap.add_argument("--profiles-file", default=C.DEFAULT_PROFILE_FILE,
                    help="门店 profile 文件")
    ap.add_argument("--capture-id", help="稳定 capture UUID（通常由 pipeline 生成）")
    ap.add_argument("--batch-id", help="稳定 batch UUID（通常由 pipeline 生成）")
    ap.add_argument("--run-id", help="本次运行 UUID")
    ap.add_argument("--manifest-output", help="final manifest 输出路径")
    ap.add_argument("--category", help="采集范围标签，如 休闲零食/薯片薯条")
    args = ap.parse_args()

    for name in ("count", "verify_every", "max_scrolls", "checkpoint_every"):
        value = getattr(args, name)
        if value is not None and value < 0:
            ap.error(f"--{name.replace('_', '-')} 不能为负数")
    for name in ("capture_id", "batch_id", "run_id"):
        value = getattr(args, name)
        if value:
            try:
                uuid.UUID(value)
            except ValueError:
                ap.error(f"--{name.replace('_', '-')} 必须是 UUID")

    if args.seed is not None:
        random.seed(args.seed)

    if args.carry_baseline:
        load_carry_baseline(os.path.abspath(args.carry_baseline))

    parent_manifest = None
    parent_manifest_path = None
    parent_scope = {}
    resume_csv = args.resume
    profile = C.load_profile(args.profile, args.profiles_file) if args.profile else None
    original_requested_count = args.count
    if args.resume:
        candidate = os.path.abspath(args.resume)
        if candidate.endswith(".json"):
            parent_manifest_path = candidate
        else:
            sibling = os.path.splitext(candidate)[0] + ".manifest.json"
            if os.path.isfile(sibling):
                parent_manifest_path = sibling
        if parent_manifest_path:
            parent_report = C.validate_manifest(parent_manifest_path, require_complete=False)
            if not parent_report.ok:
                die("父 manifest 校验失败：\n" + C.format_gate_report(parent_report))
            parent_manifest = parent_report.manifest
            if parent_manifest.get("state") not in {"success", "partial", "interrupted", "failed"}:
                die(f"父 manifest 不是可续跑的终态：{parent_manifest.get('state')}")
            resume_csv = parent_manifest["artifacts"]["csv_path"]
            parent_scope = parent_manifest["scope"]
        else:
            if not args.allow_legacy_resume:
                die("旧 CSV 没有 final manifest；如确需兼容，请显式加 --allow-legacy-resume")
            if profile:
                die("正式 profile 流水线不允许从未验证的旧 CSV 续跑")
            resume_csv = candidate

    if parent_manifest:
        parent_profile = parent_manifest["profile"]
        trusted_profile = profile or C.load_profile(parent_profile["name"], args.profiles_file)
        profile_errors = C.profile_mismatches(parent_profile, trusted_profile)
        if profile_errors:
            die("--profile 与父 manifest 身份不一致：" + "；".join(profile_errors))
        profile = trusted_profile
        parent_expected = parent_scope["expected_product_count"]
        try:
            args.count = resolve_resume_count(args.count, parent_expected)
        except C.CaptureContractError as exc:
            die(str(exc))
        parent_category = parent_scope.get("category")
        if args.category is not None and args.category != parent_category:
            die("--category 与父 manifest 范围不一致")
        args.category = parent_category
        if args.capture_id and args.capture_id != parent_manifest.get("capture_id"):
            die("--capture-id 与父 manifest 不一致")
        if args.batch_id and args.batch_id != parent_manifest.get("batch_id"):
            die("--batch-id 与父 manifest 不一致")
        if args.run_id and args.run_id == parent_manifest.get("run_id"):
            die("--run-id 必须与父 manifest 不同")
        parent_device = C.text_value(parent_manifest.get("device_id"))
        if args.serial and parent_device and args.serial != parent_device:
            die("--serial 与父 manifest 设备不一致")
    elif args.count is None:
        args.count = 20

    scope_requested_count = (parent_scope.get("requested_count")
                             if parent_manifest else original_requested_count)
    if scope_requested_count is None:
        scope_requested_count = args.count

    CTX.adb = args.adb
    CTX.profile = profile
    CTX.dismiss_order_dialogs = bool(args.dismiss_order_dialogs)
    CTX.profiles_file = os.path.abspath(args.profiles_file)
    parent_device = C.text_value((parent_manifest or {}).get("device_id"))
    CTX.serial = args.serial or parent_device or ((profile or {}).get("default_device_serial"))
    CTX.out_dir = os.path.abspath(args.out_dir)
    CTX.capture_id = (args.capture_id or (parent_manifest or {}).get("capture_id")
                      or str(uuid.uuid4()))
    CTX.batch_id = (args.batch_id or (parent_manifest or {}).get("batch_id")
                    or str(uuid.uuid4()))
    CTX.run_id = args.run_id or str(uuid.uuid4())
    CTX.parent_manifest_sha256 = (C.sha256_file(parent_manifest_path)
                                  if parent_manifest_path else None)
    CTX.manifest_output = os.path.abspath(args.manifest_output) if args.manifest_output else None
    if CTX.manifest_output and CTX.manifest_output.endswith(".manifest.json"):
        output_name = os.path.basename(CTX.manifest_output[:-len(".manifest.json")])
        CTX.run_stamp = (output_name[len("qnh_scan_"):]
                         if output_name.startswith("qnh_scan_") else output_name)
    else:
        CTX.run_stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + f"_{CTX.run_id[:8]}"
    CTX.category = args.category
    CTX.run_started_at = datetime.now()
    parent_started = C.text_value(((parent_manifest or {}).get("capture") or {}).get("started_at"))
    try:
        parent_started_at = datetime.fromisoformat(parent_started) if parent_started else None
    except ValueError:
        die("父 manifest capture.started_at 无效")
    if parent_started_at and parent_started_at.tzinfo is not None:
        die("父 manifest 使用带时区时间，当前扫描器要求本地无时区 ISO 时间")
    CTX.started_at = min(
        [value for value in (CTX.run_started_at, parent_started_at) if value is not None])
    CTX.capture_options = {
        "requested_count": scope_requested_count,
        "pace": args.pace,
        "tall_screen": bool(args.tall_screen),
        "verify_every": args.verify_every,
        "checkpoint_every": args.checkpoint_every,
        "record": bool(args.record),
        "dismiss_order_dialogs": bool(args.dismiss_order_dialogs),
    }
    os.makedirs(CTX.out_dir, exist_ok=True)
    atexit.register(cleanup)
    sys.excepthook = on_unhandled_exception
    if resume_csv:
        load_resume(os.path.abspath(resume_csv), strict_manifest=bool(parent_manifest))

    signal.signal(signal.SIGINT, on_sigint)
    signal.signal(signal.SIGTERM, on_sigint)

    step("环境检查")
    require_device()

    if args.restore_a11y:
        restore_a11y()
        return

    if args.tall_screen:
        setup_tall_screen()

    sw, sh_ = screen_size()
    info(f"屏幕分辨率：{sw}x{sh_}")

    # 助手本身就是那个无障碍服务，用它加速就绝不能再把它关掉。
    if not args.no_helper:
        HelperState.allow_dump = not args.no_helper_dump
        helper_setup()

    # capture.helper 必须如实记录 setup 后的真实状态：此前在 setup 前
    # 快照默认值，所有 manifest 都记 ready=false，误导排障。
    CTX.capture_options["helper"] = {
        "ready": HelperState.ready,
        "version": HelperState.version,
        "use_for_dump": HelperState.use_for_dump,
        "has_any_text_query": HelperState.has_any_text_query,
        "has_semantic_scroll": HelperState.has_semantic_scroll,
    }

    if HelperState.ready:
        if not args.keep_a11y:
            info("使用助手加速，保留其无障碍服务（--keep-a11y 已隐含）")
    if args.require_helper and not (HelperState.ready and HelperState.use_for_dump):
        # 正式分片采集拒绝 4× 慢速降级：仅 ready 不够——旧版助手
        # （< v{HELPER_FRESH_DUMP_VERSION}）或 --no-helper-dump 都会让
        # 层级退回 uiautomator 慢路。快速失败交由编排器退避重试。
        # 诊断模式（--no-helper / --no-helper-dump 显式传入）不受此限。
        reason = ("不可用" if not HelperState.ready
                  else f"v{HelperState.version} 早于 v{HELPER_FRESH_DUMP_VERSION}，"
                       "层级走 uiautomator 慢路")
        die(f"--require-helper：ARTEMIS 助手{reason}，快速失败交由编排器恢复")
    elif not HelperState.ready and not args.keep_a11y:
        disable_artemis_a11y()

    pace = PACING[args.pace]

    step("标定商品列表")
    wait_while_dialog("开始前")
    if CTX.interrupted:
        return
    selected_category = None
    nodes, products = [], []
    # 分辨率切换会触发 MRN Activity 重建，列表通常要 3-5 秒重新挂载；
    # 显式等商品出现，不能把重绘过渡误报成「不在列表页」。
    for attempt in range(8):
        nodes, products = dump_products()
        if products:
            break
        if blocking_dialog(nodes):
            wait_while_dialog("开始前")
        time.sleep(0.8 + 0.15 * attempt)

    fg = foreground_pkg(nodes)
    if fg and fg != APP_PKG:
        warn(f"当前前台 App 是 {fg}，预期是牵牛花 {APP_PKG}")

    wm = detect_watermarks(nodes)
    if wm:
        info(f"已自动忽略水印文本：{', '.join(list(wm)[:2])}")

    if not products:
        warn("当前不在商品列表页，尝试自动导航到经营 → 门店商品")
        nodes, products = navigate_to_product_list()
    if not products:
        die("自动导航后仍没有识别到商品行；已安全保存，可由守护器重试。")
    if CTX.category:
        step(f"定位类目：{CTX.category}")
        # 续跑必须从叶子列表顶部开始，不能继承 App 当前中部滚动位置：否则只向下
        # 扫会漏掉当前位置上方的缺口，反复到列表底部仍少商品。切到全部商品再
        # 切回目标叶子，强制归顶；已完成卡由 resume_marks 快速跳过。
        if resume_csv and category_selection_valid(CTX.category, nodes):
            info(f"续跑：重置 {CTX.category} 到列表顶部，避免遗漏上方缺口")
            reset_nodes, reset_products = reset_category_to_top(CTX.category)
            if not reset_products:
                die(f"续跑重置类目到顶部失败：{CTX.category}")
            leaf = CTX.category.rsplit("/", 1)[-1]
            selected_category = (selected_list_count(reset_nodes, leaf) or 0,
                                 reset_nodes)
        else:
            selected_category = select_category_path(CTX.category)
        nodes = selected_category[1]
        products = find_products(nodes, detect_watermarks(nodes))
        if not products and selected_category[0] > 0:
            for _ in range(6):
                time.sleep(0.5)
                nodes, products = dump_products()
                if products:
                    break
        if selected_category[0] > 0 and not products:
            die(f"类目已选中但商品未渲染：{CTX.category}")
        info(f"类目定位成功：{CTX.category}（{selected_category[0]}）")

    expected_list_title = CTX.category.rsplit("/", 1)[-1] if CTX.category else "全部商品"
    total_entry = find_list_total(nodes, sw, sh_, wm, expected_list_title)
    if total_entry:
        _, platform_total, total_text = total_entry
        CTX.target_count = platform_total
        CTX.list_title = re.sub(r"（[\d,]+）$", "", total_text)
        if parent_manifest:
            if platform_total != parent_scope.get("expected_product_count"):
                die(f"当前列表总数 {platform_total} 与父 manifest "
                    f"{parent_scope.get('expected_product_count')} 不一致")
            if CTX.list_title != parent_scope.get("list_title"):
                die(f"当前列表标题 {CTX.list_title!r} 与父 manifest "
                    f"{parent_scope.get('list_title')!r} 不一致")
    elif CTX.profile:
        die("profile 模式需要当前列表标题含商品总数，但未读到")

    if args.count <= 0 or CTX.category:
        if not total_entry:
            die("当前范围需要标题含商品总数，但未读到")
        if args.count > 0 and args.count != platform_total:
            die(f"--count {args.count} 与当前类目实时数量 {platform_total} 不一致")
        args.count = platform_total
        info(f"范围模式：{total_text} → 目标 {args.count} 个")

    info(f"当前屏识别到 {len(products)} 个商品：")
    for i, p in enumerate(products, 1):
        info(f"    {i}. {p['title'][:40]}  "
             f"[条码 {p['barcode'] or '?'}]  点击点 {p['tap']}")

    if args.calibrate_only:
        step("标定模式：不执行任何点击，退出")
        return

    video_path = args.video or os.path.join(
        CTX.out_dir,
        f"qnh_scan_{CTX.run_stamp}.mp4")
    if args.record:
        step("启动录制")
        start_recording(video_path)

    try:
        # 高屏整页详情本来就在一屏内（n_scroll 恒 0），断点续跑时不再重复
        # 打开标定商品：473 次 attempt 每次约 8-15s 纯开销（2026-10-12 审计）。
        # back=None 由 tap_back 的 helper global-back / keyevent 兜底。
        if resume_csv and sh_ >= 3500:
            step("标定详情页")
            info("高屏续跑：跳过标定（n_scroll=0，back 用系统返回兜底）")
            si, n_scroll, ok, vals, back = -1, 0, True, [], None
            sample_detail = {}
            first_screen = list(products)
            marks = resume_marks()
            products = [p for i, p in enumerate(products)
                        if not is_done(p, marks)]
        else:
            step("标定详情页")
            # 标定样本必须是标品：自建商品详情页根本没有目标字段，
            # 拿它当样本只会滑满 max_scrolls 然后标出一个错的滚动次数
            cands = [i for i, p in enumerate(products)
                     if not is_custom_product(p)] or [0]
            if cands[0]:
                info(f"第 1 个是自建商品，改用第 {cands[0] + 1} 个作标定样本")

            si, n_scroll, ok, vals, back = cands[0], 1, False, [], None
            sample_detail = {}
            for si in cands[:2]:          # 最多换一次样本
                sample = products[si]
                n_scroll, ok, vals, back, sample_detail = calibrate_detail_scrolls(
                    sample, sw, sh_, pace, args.max_scrolls)
                if ok:
                    break
                warn(f"样本「{sample['title'][:20]}」标定失败，换下一个再试")

            if not ok:
                # 标定失败：高屏整页可见，不允许盲滑；普通屏回退 1 次，宁可少滑
                # 也不要拿 max_scrolls 拖慢整轮或滑出页面。
                n_scroll = 0 if sh_ >= 3500 else 1
                warn(f"标定未成功，详情页滑动次数回退为 {n_scroll} 次。"
                     "校验结果可能偏少，但不会拖慢整轮或滑出页面。")

            sample = products[si]
            if not is_done(sample, resume_marks()):   # 续跑时样本可能早就做过了
                cal_code, cal_artno = split_field_values(vals)
                CTX.rows.append([len(CTX.rows) + 1, sample["title"], sample["barcode"],
                                 cal_code, cal_artno,
                                 "成功" if ok else "未找到", video_offset(),
                                 *capture_meta()])
                remember_row_product(CTX.rows[-1][0], sample)
                if sample_detail:
                    CTX.details[C.row_identity(CTX.rows[-1][0], sample["title"],
                                               sample["barcode"])] = sample_detail
            first_screen = list(products)
            marks = resume_marks()
            products = [p for i, p in enumerate(products)
                        if i != si and not is_done(p, marks)]

        step(f"开始批量浏览（目标 {args.count} 个，详情页滑动 {n_scroll} 次/个）")

        done = len(CTX.rows)
        cursor = 0          # 当前屏里下一个要点的商品下标
        t_start = time.time()
        stop_reason = ""
        last_keys = {product_identity(product) for product in first_screen}
        screen_last = first_screen[-1]      # 当前屏最下面一个商品（翻页连续性的锚）
        skipping_noted = False
        top_reset_done = False
        fast_resume_search = bool(
            CTX.resumed and done < args.count and sh_ >= 3500
            and HelperState.has_semantic_scroll)
        scan_screens = 1
        scan_identities = set(last_keys)
        if fast_resume_search:
            info(f"续跑快速定位：语义翻页寻找 {args.count - done} 个缺口；"
                 "若快速扫到底仍缺，会自动回顶部精确补扫")

        def retry_from_top():
            nonlocal products, cursor, first_screen, last_keys, screen_last, top_reset_done
            nonlocal fast_resume_search, scan_screens, scan_identities
            if top_reset_done or not CTX.category or len(CTX.rows) >= args.count:
                return False
            top_reset_done = True
            mode = "快速定位到底" if fast_resume_search else "已到列表底部"
            fast_resume_search = False
            info(f"{mode}但仍缺 {args.count - len(CTX.rows)} 个；"
                 "重置类目到顶部，改用精确锚点补扫")
            nodes2, products2 = reset_category_to_top(CTX.category)
            if not products2 or not category_selection_valid(CTX.category, nodes2):
                return False
            first_screen = list(products2)
            last_keys = {product_identity(product) for product in products2}
            screen_last = products2[-1]
            scan_screens = 1
            scan_identities = set(last_keys)
            marks2 = resume_marks()
            products = [product for product in products2 if not is_done(product, marks2)]
            cursor = 0
            return True

        while done < args.count and not CTX.interrupted:
            if cursor >= len(products):
                # 续跑第一遍用语义翻页快速找稀疏缺口；兜底遍才用精确锚点。
                scroll_list(sw, sh_, pace,
                            anchor=None if fast_resume_search else screen_last)
                _, new = wait_list_after_scroll(sw, sh_)
                if not new:
                    recovery_nodes, recovery_products = dump_products()
                    if (CTX.category and recovery_products
                            and not category_selection_valid(CTX.category, recovery_nodes)):
                        warn(f"类目选择已丢失，重新定位：{CTX.category}")
                        recovery_nodes, recovery_products = restore_category_after_navigation(
                            CTX.category)
                    if recovery_products and (not CTX.category or category_selection_valid(
                            CTX.category, recovery_nodes)):
                        new = recovery_products
                    else:
                        if retry_from_top():
                            continue
                        stop_reason = ("翻页后列表连续多次没有完整商品卡片；已停止，"
                                       "不会盲按返回退出 App。可用 --resume 续跑。")
                        warn(stop_reason)
                        break
                if sh_ < 3500:
                    new = close_gap(new, screen_last, sw, sh_)
                new_keys = {product_identity(product) for product in new}
                if new_keys == last_keys:
                    # 翻页后画面没变：可能是分页懒加载还没渲染出下一屏，多等
                    # 几次；空画面不算"变了"，避免 new[-1] 崩溃。
                    advanced = False
                    for _ in range(LAZY_LOAD_RETRIES):
                        time.sleep(0.8 if sh_ >= 3500 else 1.6)
                        scroll_list(sw, sh_, pace,
                                    anchor=None if fast_resume_search else screen_last)
                        _, candidate = wait_list_after_scroll(sw, sh_)
                        if not candidate:
                            continue
                        if sh_ < 3500:
                            candidate = close_gap(candidate, screen_last, sw, sh_)
                        candidate_keys = {product_identity(product) for product in candidate}
                        if candidate_keys and candidate_keys != last_keys:
                            new, new_keys = candidate, candidate_keys
                            info("（等到了新加载的商品）")
                            advanced = True
                            break
                    if not advanced:
                        if retry_from_top():
                            continue
                        stop_reason = "多次翻页画面不再变化，已到列表底部。"
                        warn(stop_reason)
                        break
                if not new:
                    stop_reason = "翻页结果为空，已安全停止；可用 --resume 续跑。"
                    warn(stop_reason)
                    break
                if CTX.category:
                    category_nodes = parse_nodes(dump_xml())
                    if not category_selection_valid(CTX.category, category_nodes):
                        warn(f"翻页后类目选择丢失，重新定位：{CTX.category}")
                        category_nodes, new = restore_category_after_navigation(CTX.category)
                        if not new or not category_selection_valid(CTX.category, category_nodes):
                            stop_reason = "无法恢复当前类目，已安全停止；可用 --resume 续跑。"
                            warn(stop_reason)
                            break
                last_keys = {product_identity(product) for product in new}
                screen_last = new[-1]
                scan_screens += 1
                scan_identities.update(last_keys)
                if scan_screens % 5 == 0:
                    mode = "快速" if fast_resume_search else "精确"
                    info(f"— {mode}扫描进度：{scan_screens} 屏，"
                         f"已见 {len(scan_identities)}/{args.count} 个列表身份，"
                         f"仍缺 {args.count - len(CTX.rows)} 个 —")
                marks = resume_marks()
                fresh = [p for p in new if not is_done(p, marks)]
                if not fresh:
                    if not skipping_noted:
                        info("— 翻过已完成的商品… —")
                        skipping_noted = True
                    products, cursor = [], 0
                    continue
                skipping_noted = False
                products, cursor = fresh, 0
                info(f"— 翻页，本屏新增 {len(products)} 个商品 —")
                continue

            # 高屏 + Helper v8：设备端早停查询只检查「订单弹窗 / 仍在详情页」，
            # 正常列表路径直接复用本屏已标定坐标，不再为每个商品序列化整棵 XML。
            fast_safe = False
            if sh_ >= 3500 and (HelperState.has_any_text_query
                                or HelperState.has_text_query):
                guard = helper_find_any_text(
                    ["我知道了", "订单号", "顾客修改", "商品详情"])
                if guard is None:
                    guard_pattern = "(?:" + BLOCKING_DIALOG_PAT.pattern + ")|^商品详情$"
                    guard = helper_find_text(guard_pattern)
                if guard is not None:
                    if guard and BLOCKING_DIALOG_PAT.search(guard):
                        wait_while_dialog("点商品前")
                    elif guard == "商品详情":
                        # 系统文字索引可能在返回列表后短暂保留上一页节点；只有一份
                        # 新鲜 XML 也确认详情页时才允许返回，防止从列表多退一层。
                        fresh_nodes = parse_nodes(dump_xml())
                        if is_detail_page(fresh_nodes):
                            tap_back(back)
                            time.sleep(0.7)
                        else:
                            fast_safe = bool(find_products(
                                fresh_nodes, detect_watermarks(fresh_nodes)))
                    else:
                        fast_safe = True

            if fast_safe:
                target = products[cursor]
            else:
                # 普通屏 / 助手降级 / 异常恢复：完整层级重新定位坐标。
                nodes = parse_nodes(dump_xml())
                if blocking_dialog(nodes):
                    wait_while_dialog("点商品前")
                    nodes = parse_nodes(dump_xml())
                on_screen = find_products(nodes, detect_watermarks(nodes))
                if not on_screen:
                    if is_detail_page(nodes):
                        tap_back(back)
                        time.sleep(0.7)
                    _, on_screen = wait_list_after_scroll(sw, sh_, retries=4)
                    if not on_screen:
                        stop_reason = ("点下一个商品前无法读到列表，正在自动导航回门店商品。")
                        warn(stop_reason)
                        recovery_nodes, on_screen = navigate_to_product_list()
                        if on_screen and CTX.category:
                            if not category_selection_valid(CTX.category, recovery_nodes):
                                info(f"恢复列表后重新定位类目：{CTX.category}")
                                recovery_nodes, on_screen = restore_category_after_navigation(
                                    CTX.category)
                            if on_screen and not category_selection_valid(
                                    CTX.category, recovery_nodes):
                                on_screen = []
                    if not on_screen:
                        stop_reason = ("点下一个商品前无法读到列表，自动导航也失败；"
                                       "已安全停止，可用 --resume 续跑。")
                        warn(stop_reason)
                        break
                screen_last = on_screen[-1]
                target = next((p for p in on_screen
                               if same_product(p, products[cursor])), None)
                if target is None:
                    # 目标已不在当前屏：改用屏上还没处理过的商品；没有就翻页
                    marks = resume_marks()
                    products = [p for p in on_screen if not is_done(p, marks)]
                    cursor = 0
                    continue

            done += 1
            verify = (args.verify_every > 0 and
                      done % args.verify_every == 0)
            visit_product(done, target, n_scroll, back,
                          sw, sh_, pace, verify, args.max_scrolls)
            cursor += 1
            if (args.checkpoint_every > 0
                    and len(CTX.rows) % args.checkpoint_every == 0):
                checkpoint = write_checkpoint()
                info(f"    （断点已保存：{len(CTX.rows)} 个 → {checkpoint}）")

        elapsed = time.time() - t_start
        step("完成")
        n = len(CTX.rows)
        fresh_n = n - CTX.resumed        # 本轮实际点开处理的（不含续跑载入的）
        info(f"共 {n} 个商品"
             + (f"（续跑载入 {CTX.resumed} 个，本轮新处理 {fresh_n} 个）"
                if CTX.resumed else "")
             + f"，本轮耗时 {elapsed/60:.1f} 分钟"
             + ("（含翻过已完成部分的时间）" if CTX.resumed
                else f"（平均 {elapsed/max(1, fresh_n - 1):.1f} 秒/个）"
                if fresh_n > 1 else ""))
        if n < args.count:
            warn(f"未跑满目标：目标 {args.count} 个，实际 {n} 个"
                 + (f"。原因：{stop_reason}" if stop_reason else ""))
        okn = sum(1 for r in CTX.rows if r[5] == "成功")
        bad = sum(1 for r in CTX.rows if r[5] == "未找到")
        thin = sum(1 for r in CTX.rows if r[5] == "详情不全")
        cus = sum(1 for r in CTX.rows if r[5] == "自建商品")
        nop = sum(1 for r in CTX.rows if r[5] == "未打开")
        thr = sum(1 for r in CTX.rows if r[5] == "限流")
        info(f"校验：成功 {okn}，未找到 {bad}，详情不全 {thin}，"
             f"自建商品 {cus}，未打开 {nop}，限流 {thr}，"
             f"未校验 {n - okn - bad - thin - cus - nop - thr}")
        CTX.stop_reason = stop_reason
        CTX.finished_at = datetime.now()
        if CTX.throttle_hits:
            warn(f"本轮撞平台限流 {CTX.throttle_hits} 次（接口 429）。频繁触发可能影响账号，"
                 "建议放慢节奏（--pace natural）或拆成几段错开时间跑。")
        if HelperState.ready and HelperState.action_failures:
            warn(f"助手手势失败 {HelperState.action_failures} 次，已自动改用 input 补上")
        if CTX.carry_baseline is not None:
            unseen = len(CTX.carry_baseline) - len(CTX.carry_seen or ())
            carried_n = sum(1 for row in CTX.rows if row[5] == CARRY_STATUS)
            info(f"携带基线：命中 {len(CTX.carry_seen)} / {len(CTX.carry_baseline)}"
                 + (f"，未命中 {unseen} 个（按当日实际可见处理，已全部走详情页或不在列表）"
                    if unseen else ""))

    except Exception as exc:
        if not CTX.interrupted:
            CTX.final_state = "failed"
            CTX.stop_reason = CTX.stop_reason or f"{type(exc).__name__}: {exc}"[:128]
        raise
    finally:
        cleanup()
        final_video = CTX.record_out or video_path
        if args.record and os.path.exists(final_video):
            size = os.path.getsize(final_video) / 1024 / 1024
            info(f"视频：{final_video}（{size:.1f} MB）")
        if CTX.a11y_disabled:
            info(f"提示：如需恢复 Artemis 无障碍服务，跑 "
                 f"`python3 {os.path.basename(__file__)} --restore-a11y`")


if __name__ == "__main__":
    main()
