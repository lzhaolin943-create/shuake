#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
在线课程视频辅助脚本 —— 浏览器真实播放版

特点：
  * 使用独立的 Edge 配置目录，不碰你日常用的 Edge / 登录状态。
  * 第一次运行 --login，在弹出的窗口里扫码登录一次，之后复用。
  * 输入课程网址，自动读取课程树，逐个打开视频页面并真实播放。
  * 只处理视频（leaf_type=0），不碰作业、考试、讨论、弹题。
  * 断网/崩溃后重跑，会跳过本地已完成的视频，继续剩下的。

用法：
  python watch.py --login
  python watch.py "课程网址"
  python watch.py "课程网址" --from "3.2"
  python watch.py "课程网址" --dry-run

依赖：
  pip install playwright
  （不需要 playwright install，直接用系统已安装的 Edge）
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import (
    BrowserContext,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

HOST = "https://changjiang.yuketang.cn"
LEAF_TYPE_VIDEO = 0

# 先试“学习空间”播放页（历史记录里实际使用的地址），再试旧路由。
VIDEO_URL_TEMPLATES = (
    HOST + "/ai-workspace/lms-graph/{cid}/video/{leaf_id}?is_chapter=1",
    HOST + "/xcloud/video-student/{cid}/{leaf_id}",
    HOST + "/lms/{cid}/video/{leaf_id}",
    HOST + "/v2/web/studentLog/{cid}?leaf_id={leaf_id}",
)

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
DONE_FILE = STATE_DIR / "done.json"
LOG_FILE = STATE_DIR / "watch.log"

log = logging.getLogger("yuketang")


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def setup_console() -> None:
    """让 Windows 控制台按 UTF-8 输出中文。"""
    if os.name == "nt":
        try:
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def configure_state(path: str) -> None:
    """允许不同课程使用各自的进度/日志目录，避免并发时互相覆盖。"""
    global STATE_DIR, DONE_FILE, LOG_FILE
    STATE_DIR = Path(path).expanduser()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DONE_FILE = STATE_DIR / "done.json"
    LOG_FILE = STATE_DIR / "watch.log"


def setup_logging(verbose: bool = False) -> None:
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)

    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(fh)


def default_profile_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return str(Path(base) / "YuketangAuto" / "profile")


def load_done() -> Dict[str, Any]:
    if DONE_FILE.exists():
        try:
            return json.loads(DONE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"done": {}, "failed": {}}


def save_done(data: Dict[str, Any]) -> None:
    tmp = DONE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DONE_FILE)


class KeepAwake:
    """Windows 下阻止系统休眠/关屏；退出时自动恢复。"""

    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    ES_DISPLAY_REQUIRED = 0x00000002

    def __init__(self) -> None:
        self.active = False

    def __enter__(self) -> "KeepAwake":
        if os.name == "nt":
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(
                    self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED | self.ES_DISPLAY_REQUIRED
                )
                self.active = True
                log.info("已阻止系统休眠/关屏（脚本退出后恢复）")
            except Exception as exc:
                log.debug("SetThreadExecutionState 失败：%s", exc)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.active:
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS)
            except Exception:
                pass


def parse_classroom_id(url: str) -> Optional[str]:
    m = re.search(r"/studentLog/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"/classroom/(\d+)", url)
    if m:
        return m.group(1)
    qs = parse_qs(urlparse(url).query)
    if qs.get("classroom_id"):
        return qs["classroom_id"][0]
    if url.strip().isdigit():
        return url.strip()
    return None


def compact(text: str, n: int = 42) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


# --------------------------------------------------------------------------- #
# 主体
# --------------------------------------------------------------------------- #

class YuketangWatcher:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.cid: str = ""
        self.course_id: str = ""
        self.user_id: str = ""
        self.course_url: str = args.url or ""
        self.pw = None
        self.ctx: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.state = load_done()
        self.done = self.state.setdefault("done", {})
        self.failed = self.state.setdefault("failed", {})
        self.leaf_info_cache: Dict[str, Dict[str, Any]] = {}

    # ---------------------------------------------------------------- 浏览器

    def launch(self, headless: bool = False) -> None:
        profile = Path(self.args.profile).expanduser()
        profile.mkdir(parents=True, exist_ok=True)

        log.info("启动独立 Edge 配置：%s", profile)
        self.pw = sync_playwright().start()
        self.ctx = self.pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            channel="msedge",
            headless=headless,
            no_viewport=True,
            locale="zh-CN",
            args=[
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-features=CalculateNativeWinOcclusion",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1360,900",
            ],
        )
        self.ctx.set_default_timeout(15_000)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.on("dialog", lambda d: self._safe_accept(d))

    def close(self) -> None:
        try:
            if self.ctx:
                self.ctx.close()
        except Exception:
            pass
        try:
            if self.pw:
                self.pw.stop()
        except Exception:
            pass

    @staticmethod
    def _safe_accept(dialog: Any) -> None:
        try:
            dialog.accept()
        except Exception:
            pass

    # ---------------------------------------------------------------- 登录态

    def api_headers(self) -> Dict[str, str]:
        cookies = {}
        try:
            cookies = {c["name"]: c["value"] for c in self.ctx.cookies(HOST + "/")}
        except Exception:
            pass
        uv = cookies.get("university_id", "")
        return {
            "accept": "application/json, text/plain, */*",
            "xtbz": "ykt",
            "classroom_id": str(self.cid or ""),
            "platform_id": "3",
            "university-id": uv,
            "uv-id": uv,
            "x-csrftoken": cookies.get("csrftoken", ""),
            "referer": HOST + "/v2/web/index",
        }

    def api_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        assert self.ctx is not None
        try:
            resp = self.ctx.request.get(
                HOST + path,
                params=params or {},
                headers=self.api_headers(),
                timeout=30_000,
            )
            if resp.status != 200:
                log.warning("GET %s 返回 HTTP %s", path, resp.status)
                return None
            return resp.json()
        except Exception as exc:
            log.warning("GET %s 失败：%s", path, exc)
            return None

    def api_post(self, path: str, payload: Dict[str, Any]) -> bool:
        assert self.ctx is not None
        try:
            headers = self.api_headers()
            headers["content-type"] = "application/json"
            resp = self.ctx.request.post(
                HOST + path,
                data=payload,
                headers=headers,
                timeout=30_000,
            )
            if resp.status != 200:
                log.warning("POST %s 返回 HTTP %s", path, resp.status)
                return False
            return True
        except Exception as exc:
            log.warning("POST %s 失败：%s", path, exc)
            return False

    def check_login(self) -> bool:
        """静默检查登录态，登录过程中不刷警告。"""
        assert self.ctx is not None
        try:
            resp = self.ctx.request.get(
                HOST + "/v2/api/web/userinfo",
                headers=self.api_headers(),
                timeout=15_000,
            )
            if resp.status != 200:
                return False
            data = resp.json()
        except Exception:
            return False
        if data and data.get("errmsg") == "Success":
            info = (data.get("data") or [{}])[0]
            log.info("登录状态正常：%s（user_id=%s）", info.get("name"), info.get("user_id"))
            return True
        return False

    def login(self) -> None:
        assert self.page is not None
        log.info("打开登录页，请在 Edge 窗口里扫码登录（最多等 5 分钟）……")
        self.page.goto(HOST + "/v2/web/index", wait_until="domcontentloaded", timeout=60_000)
        deadline = time.time() + 300
        while time.time() < deadline:
            if self.check_login():
                log.info("登录成功，配置已保存。")
                return
            time.sleep(2)
        raise RuntimeError("登录超时。请重新运行 --login，并在窗口里完成扫码。")

    # ---------------------------------------------------------------- 课程数据

    def resolve_course_context(self) -> None:
        """补齐 course_id / user_id：leaf_info 里有时不返回 course_id。"""
        info = self.api_get("/v2/api/web/userinfo")
        if info and info.get("errmsg") == "Success":
            first = (info.get("data") or [{}])[0]
            self.user_id = str(first.get("user_id") or self.user_id)

        data = self.api_get("/v2/api/web/courses/list", {"identity": 2})
        items = ((data or {}).get("data") or {}).get("list") or []
        for item in items:
            if str(item.get("classroom_id")) != str(self.cid):
                continue
            course = item.get("course") or {}
            self.course_id = str(
                course.get("id")
                or course.get("course_id")
                or item.get("course_id")
                or ""
            )
            break
        log.info("课程上下文：course_id=%s user_id=%s", self.course_id or "-", self.user_id or "-")

    def fetch_chapters(self) -> List[Dict[str, Any]]:
        data = self.api_get("/mooc-api/v1/lms/learn/course/chapter", {"cid": self.cid})
        if not data or not data.get("data"):
            msg = json.dumps(data, ensure_ascii=False)[:300] if data else "无返回"
            raise RuntimeError(f"读取课程树失败：{msg}")
        return (data["data"] or {}).get("course_chapter") or []

    def flatten_videos(self, chapters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        idx = 0
        for chapter in chapters:
            ch_name = str(chapter.get("name") or "").strip()
            for section in chapter.get("section_leaf_list") or []:
                sec_name = str(section.get("name") or "").strip()
                leaves = section.get("leaf_list")
                if leaves is None:
                    leaves = [section]
                for leaf in leaves or []:
                    try:
                        leaf_type = int(leaf.get("leaf_type", -1))
                    except (TypeError, ValueError):
                        leaf_type = -1
                    if leaf_type != LEAF_TYPE_VIDEO:
                        continue
                    idx += 1
                    title = " / ".join(x for x in (ch_name, sec_name, str(leaf.get("name") or "")) if x)
                    out.append(
                        {
                            "index": idx,
                            "leaf_id": str(leaf.get("id")),
                            "chapter": ch_name,
                            "section": sec_name,
                            "name": str(leaf.get("name") or ""),
                            "title": title,
                            "locked": bool(leaf.get("is_locked")),
                        }
                    )
        return out

    def leaf_info(self, leaf_id: str) -> Dict[str, Any]:
        if leaf_id in self.leaf_info_cache:
            return self.leaf_info_cache[leaf_id]
        data = self.api_get(f"/mooc-api/v1/lms/learn/leaf_info/{self.cid}/{leaf_id}/", {"classroom_id": self.cid})
        info = (data or {}).get("data") or {}
        self.leaf_info_cache[leaf_id] = info
        return info

    def video_meta(self, leaf_id: str) -> Dict[str, Any]:
        info = self.leaf_info(leaf_id)
        media = ((info.get("content_info") or {}).get("media") or {})
        return {
            "duration": float(media.get("duration") or 0),
            "video_id": str(media.get("video_id") or info.get("courseware_id") or leaf_id),
            "ccid": str(media.get("ccid") or ""),
            "leaf_id": str(leaf_id),
            "course_id": str(info.get("course_id") or self.course_id or ""),
            "user_id": str(info.get("user_id") or self.user_id or ""),
            "sku_id": str(info.get("sku_id") or ""),
        }

    def progress(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        if not meta.get("course_id") or not meta.get("user_id"):
            return {}
        cookies = {c["name"]: c["value"] for c in self.ctx.cookies(HOST + "/")}
        ids = [meta["video_id"]]
        if meta.get("leaf_id") and meta["leaf_id"] not in ids:
            ids.append(meta["leaf_id"])
        for vid in ids:
            params = {
                "cid": meta["course_id"],
                "user_id": meta["user_id"],
                "classroom_id": self.cid,
                "video_type": "video",
                "vtype": "rate",
                "video_id": vid,
                "snapshot": 1,
                "term": "latest",
                "uv_id": cookies.get("university_id", ""),
            }
            data = self.api_get("/video-log/get_video_watch_progress/", params)
            obj = (data or {}).get("data") or {}
            found = obj.get(str(vid)) or obj.get(vid) or {}
            if found:
                return found
        return {}

    @staticmethod
    def is_completed(progress: Dict[str, Any]) -> bool:
        return str(progress.get("completed")) == "1"

    # ---------------------------------------------------------------- 播放

    def wait_for_video(self, timeout: float = 25) -> Optional[Locator]:
        assert self.page is not None
        deadline = time.time() + timeout
        while time.time() < deadline:
            if "/web/?next=" in self.page.url:
                raise RuntimeError("登录已失效，请重新运行 --login")
            for frame in self.page.frames:
                try:
                    loc = frame.locator("video")
                    if loc.count() > 0:
                        candidate = loc.first
                        if candidate.is_visible():
                            return candidate
                except Exception:
                    continue
            self.page.wait_for_timeout(1000)
        return None

    def dismiss_overlays(self) -> None:
        assert self.page is not None
        for text in ("继续学习", "继续观看", "继续播放", "我知道了", "知道了"):
            try:
                loc = self.page.get_by_text(text, exact=False)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=1200)
                    self.page.wait_for_timeout(400)
            except Exception:
                continue

    def apply_rate(self, video: Locator) -> None:
        """把倍速锁在 video 元素上，播放器初始化/续播后重置也能拉回来。"""
        try:
            video.evaluate(
                """(v, r) => {
                    v.playbackRate = r;
                    if (!v.__ytRateLock) {
                        v.__ytRateLock = r;
                        v.addEventListener('ratechange', () => {
                            if (Math.abs(v.playbackRate - v.__ytRateLock) > 0.01) {
                                v.playbackRate = v.__ytRateLock;
                            }
                        });
                    } else {
                        v.__ytRateLock = r;
                    }
                }""",
                self.args.rate,
            )
        except Exception:
            pass

    def start_playback(self, video: Locator) -> None:
        assert self.page is not None
        try:
            video.scroll_into_view_if_needed(timeout=5_000)
        except Exception:
            pass
        try:
            video.click(force=True, timeout=3_000)
            self.page.wait_for_timeout(300)
        except Exception:
            pass
        self.apply_rate(video)
        try:
            video.evaluate(
                """(v, opts) => {
                    v.muted = !!opts.mute;
                    v.volume = 0;
                    v.playbackRate = opts.rate;
                    const p = v.play();
                    if (p && p.catch) p.catch(() => {});
                }""",
                {"mute": self.args.mute, "rate": self.args.rate},
            )
        except Exception as exc:
            log.debug("play() 失败：%s", exc)

    def resume(self, video: Locator) -> None:
        assert self.page is not None
        # 只点“继续播放/继续观看”，避免点到“继续学习”跳到下一节
        for text in ("继续播放", "继续观看"):
            try:
                loc = self.page.get_by_text(text, exact=False)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=1200)
                    self.page.wait_for_timeout(300)
                    break
            except Exception:
                pass
        try:
            box = video.bounding_box()
            if box:
                self.page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        except Exception:
            pass
        try:
            video.evaluate(
                """(v, opts) => {
                    v.muted = !!opts.mute;
                    v.playbackRate = opts.rate;
                    const p = v.play();
                    if (p && p.catch) p.catch(() => {});
                }""",
                {"mute": self.args.mute, "rate": self.args.rate},
            )
        except Exception:
            pass
        self.apply_rate(video)

    def wait_until_finished(self, video: Locator, duration: float) -> bool:
        """播放到结束或进度 >= 99.5%。返回是否认为播放完成。"""
        assert self.page is not None
        start = time.time()
        if duration > 0:
            timeout = max(900.0, duration / max(self.args.rate, 0.25) + 1800)
        else:
            timeout = 3600.0
        last_t = 0.0
        last_advance = time.time()
        last_mouse = time.time()
        last_log = 0.0

        while time.time() - start < timeout:
            if "/web/?next=" in self.page.url:
                raise RuntimeError("登录已失效，请重新运行 --login")

            try:
                st = video.evaluate(
                    """v => ({
                        t: v.currentTime || 0,
                        d: v.duration || 0,
                        p: !!v.paused,
                        e: !!v.ended,
                        rs: v.readyState || 0
                    })"""
                )
            except Exception:
                # 页面跳转/元素失效，尝试重新找 video
                self.page.wait_for_timeout(2000)
                v2 = self.wait_for_video(10)
                if not v2:
                    return False
                video = v2
                continue

            t, d = float(st["t"]), float(st["d"])
            self.apply_rate(video)

            # 有些视频元数据比实际画面长 1~2 秒，画面已经停在最后一帧但
            # currentTime 永远到不了 duration，这里按“接近结尾”判定完成。
            near_end = d > 0 and t >= d - 2.0
            if st["e"] or near_end:
                if not st["e"]:
                    try:
                        video.evaluate(
                            """v => {
                                if (v.duration && isFinite(v.duration)) {
                                    v.currentTime = Math.max(0, v.duration - 0.05);
                                    const p = v.play();
                                    if (p && p.catch) p.catch(() => {});
                                }
                            }"""
                        )
                    except Exception:
                        pass
                    self.page.wait_for_timeout(1500)
                return True

            if st["p"]:
                self.resume(video)

            if t > last_t + 0.15:
                last_t = t
                last_advance = time.time()
            elif time.time() - last_advance > 25 and d > 0 and t / d > 0.95:
                log.info("画面已停在结尾（%.0f/%.0f 秒），按完成处理", t, d)
                try:
                    video.evaluate(
                        """v => {
                            if (v.duration && isFinite(v.duration)) {
                                v.currentTime = Math.max(0, v.duration - 0.05);
                                const p = v.play();
                                if (p && p.catch) p.catch(() => {});
                            }
                        }"""
                    )
                except Exception:
                    pass
                self.page.wait_for_timeout(1500)
                return True
            elif time.time() - last_advance > 75:
                log.warning("播放卡住 75 秒，尝试刷新并从 %.0f 秒继续", last_t)
                video = self._reload_and_resume(last_t)
                if not video:
                    return False
                last_t = -1.0
                last_advance = time.time()
                continue

            if t - last_log >= 60:
                pct = (t / d * 100) if d > 0 else 0
                log.info("    进度 %.0f%%（%.0f/%.0f 秒）", pct, t, d)
                last_log = t

            if time.time() - last_mouse > 60:
                try:
                    self.page.mouse.move(random.randint(100, 900), random.randint(100, 600), steps=3)
                except Exception:
                    pass
                last_mouse = time.time()

            self.page.wait_for_timeout(2000)

        log.warning("超过最长等待时间仍未结束")
        return False

    def _reload_and_resume(self, position: float) -> Optional[Locator]:
        assert self.page is not None
        try:
            self.page.reload(wait_until="domcontentloaded", timeout=60_000)
        except Exception:
            return None
        video = self.wait_for_video(30)
        if not video:
            return None
        self.dismiss_overlays()
        if position > 5:
            try:
                video.evaluate(
                    """(v, pos) => {
                        if (v.readyState >= 1) { v.currentTime = pos; }
                        else { v.addEventListener('loadedmetadata', () => { v.currentTime = pos; }, {once:true}); }
                    }""",
                    position,
                )
            except Exception:
                pass
        self.start_playback(video)
        return video

    def goto_content_page(self) -> None:
        assert self.page is not None
        self.page.goto(self.course_url, wait_until="domcontentloaded", timeout=60_000)
        self.page.wait_for_timeout(5000)
        # &tab=content 不会自动切页，必须点标签
        try:
            tab = self.page.get_by_text("学习内容", exact=True)
            if tab.count() > 0:
                tab.first.click(timeout=3000)
                self.page.wait_for_timeout(4000)
        except Exception:
            pass
        try:
            self.page.evaluate(
                "() => { const c = document.querySelector('.viewContainer'); if (c) c.scrollTop = 0; window.scrollTo(0, 0); }"
            )
        except Exception:
            pass
        for text in ("我知道了", "知道了"):
            try:
                loc = self.page.get_by_text(text, exact=True)
                if loc.count() and loc.first.is_visible():
                    loc.first.click(timeout=1500)
            except Exception:
                pass

    def expand_content(self, chapter_hint: str = "") -> None:
        assert self.page is not None
        if chapter_hint:
            # 不猜 class：把所有含“第三章”的元素都点一遍，直到右边出现 3.0
            try:
                loc = self.page.get_by_text(chapter_hint, exact=False)
                total = loc.count()
                log.info("含「%s」的元素共 %d 个，逐个尝试点击", chapter_hint, total)
                for i in range(min(total, 12)):
                    try:
                        loc.nth(i).click(timeout=1200)
                        self.page.wait_for_timeout(700)
                        if self.page.get_by_text("3.0 导论", exact=False).count() > 0:
                            log.info("第 %d 个元素点开后出现了 3.0 导论", i + 1)
                            break
                    except Exception:
                        continue
            except Exception as exc:
                log.debug("章节点击失败：%s", exc)
        else:
            numerals = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二"]
            for num in numerals:
                label = f"第{num}章"
                try:
                    loc = self.page.get_by_text(label, exact=False)
                    if loc.count() > 0:
                        loc.first.click(timeout=800)
                        self.page.wait_for_timeout(250)
                except Exception:
                    pass
        for _ in range(2):
            for el in self.page.locator(".sub-info .gray").all():
                try:
                    if "展开" in el.inner_text():
                        el.click(timeout=1000)
                except Exception:
                    pass
            for _ in range(3):
                try:
                    self.page.mouse.wheel(0, 2500)
                except Exception:
                    break
                self.page.wait_for_timeout(350)
            self.page.wait_for_timeout(600)

    def scroll_to_text(self, text: str, steps: int = 60) -> bool:
        """在“学习内容”长列表里逐屏滚动，直到目标文字出现。"""
        assert self.page is not None
        for _ in range(steps):
            try:
                if self.page.get_by_text(text, exact=False).count() > 0:
                    return True
            except Exception:
                pass
            try:
                self.page.evaluate(
                    """() => {
                        const c = document.querySelector('.viewContainer');
                        if (c) c.scrollTop += 550;
                        else window.scrollBy(0, 550);
                    }"""
                )
            except Exception:
                break
            self.page.wait_for_timeout(350)
        return False

    def find_activity(self, leaf: Dict[str, Any]) -> Optional[Locator]:
        assert self.page is not None
        name = leaf.get("name") or ""
        candidates = [
            name,
            leaf.get("title", "").split(" / ")[-1],
            name[:18],
        ]
        for text in candidates:
            if not text:
                continue
            self.scroll_to_text(text)
            try:
                loc = self.page.locator(".activity__wrap").filter(has_text=text)
                if loc.count() > 0:
                    loc.first.scroll_into_view_if_needed(timeout=3000)
                    return loc.first
            except Exception:
                continue
        return None

    def watch_one_dom(self, leaf: Dict[str, Any]) -> str:
        """直接点“学习内容”里的视频行，走课程自己的播放器。"""
        assert self.page is not None
        leaf_id = leaf["leaf_id"]
        title = leaf["title"]
        meta = self.video_meta(leaf_id)

        if self.args.skip_done and meta.get("course_id"):
            if self.is_completed(self.progress(meta)):
                log.info("已学完，跳过：%s", compact(title))
                self._mark_done(leaf, "server")
                return "skipped"

        log.info("开始播放（目录点击）：%s", compact(title))
        self.goto_content_page()
        chapter_hint = (leaf.get("chapter") or "").split(" ")[0]
        self.expand_content(chapter_hint)

        row = self.find_activity(leaf)
        if not row:
            log.error("在“学习内容”里找不到这一行：%s", compact(title))
            try:
                self.page.screenshot(path=str(STATE_DIR / f"no_row_{leaf_id}.png"))
            except Exception:
                pass
            return "no-row"

        try:
            status = row.locator(".statistics-box .aside").inner_text(timeout=2000)
        except Exception:
            status = ""
        if "已完成" in status or "已读" in status:
            log.info("目录显示已完成，跳过：%s", compact(title))
            self._mark_done(leaf, "server")
            return "skipped"

        before_url = self.page.url
        try:
            row.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        try:
            row.click(timeout=8000)
        except Exception as exc:
            log.error("点击视频行失败：%s", exc)
            return "click-failed"

        video = self.wait_for_video(30)
        if not video:
            log.error("点击后没找到 <video>：%s", compact(title))
            try:
                self.page.screenshot(path=str(STATE_DIR / f"no_video_dom_{leaf_id}.png"))
            except Exception:
                pass
            return "no-video"

        self.dismiss_overlays()
        self.start_playback(video)
        self.page.wait_for_timeout(3000)
        try:
            log.info("实际倍速：%.2fx", float(video.evaluate("v => v.playbackRate || 1")))
        except Exception:
            pass

        if not self.wait_until_finished(video, meta["duration"]):
            log.warning("未确认播放完成：%s", compact(title))
            return "incomplete"

        self.page.wait_for_timeout(6000)

        if meta.get("course_id"):
            for _ in range(30):
                if self.is_completed(self.progress(meta)):
                    break
                time.sleep(2)
            if self.is_completed(self.progress(meta)):
                log.info("已完成：%s", compact(title))
                self._mark_done(leaf, "server")
                self._return_to_content(before_url)
                return "done"
            log.warning("视频已播完，但服务端尚未标记完成：%s", compact(title))
            self._mark_failed(leaf, "server-not-completed")
            self._return_to_content(before_url)
            return "unverified"

        self._mark_done(leaf, "played")
        self._return_to_content(before_url)
        return "done"

    def _return_to_content(self, before_url: str) -> None:
        assert self.page is not None
        try:
            if self.page.url != before_url:
                self.page.go_back(wait_until="domcontentloaded", timeout=30_000)
            else:
                self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(2000)
        except Exception:
            pass

    def watch_one(self, leaf: Dict[str, Any]) -> str:
        assert self.page is not None
        leaf_id = leaf["leaf_id"]
        title = leaf["title"]

        if leaf["locked"]:
            log.warning("[%s] 未解锁，跳过：%s", leaf_id, title)
            return "locked"

        meta = self.video_meta(leaf_id)
        if self.args.skip_done and meta.get("course_id"):
            prog = self.progress(meta)
            if self.is_completed(prog):
                log.info("已学完，跳过：%s", compact(title))
                self._mark_done(leaf, "server")
                return "skipped"

        log.info("开始播放：%s（时长 %.0f 秒）", compact(title), meta["duration"])

        video = None
        for tpl in VIDEO_URL_TEMPLATES:
            url = tpl.format(cid=self.cid, leaf_id=leaf_id)
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            except PlaywrightTimeoutError:
                log.debug("打开超时：%s", url)
                continue
            self.dismiss_overlays()
            try:
                if self.page.get_by_text("页面走丢了", exact=False).count() > 0:
                    log.debug("该地址 404，换下一个：%s", url)
                    continue
            except Exception:
                pass
            video = self.wait_for_video(20)
            if video:
                log.debug("播放页：%s", url)
                break

        if not video:
            log.error("没有找到 <video>，可能页面结构变化：%s", compact(title))
            log.error("最后打开的地址：%s", self.page.url)
            try:
                shot = STATE_DIR / f"no_video_{leaf_id}.png"
                self.page.screenshot(path=str(shot))
                log.error("已截图：%s", shot)
            except Exception:
                pass
            return "no-video"

        self.dismiss_overlays()
        self.start_playback(video)
        self.page.wait_for_timeout(3000)
        try:
            log.info("实际倍速：%.2fx", float(video.evaluate("v => v.playbackRate || 1")))
        except Exception:
            pass

        # 再检查一次，防止页面加载慢导致 play 没生效
        if not self.wait_until_finished(video, meta["duration"]):
            log.warning("未确认播放完成：%s", compact(title))
            return "incomplete"

        # 等播放器把最后的 videoend 心跳发出去，再离开页面
        self.page.wait_for_timeout(6000)

        # 给播放器心跳一点时间，再向服务端确认
        if meta.get("course_id"):
            for _ in range(30):
                if self.is_completed(self.progress(meta)):
                    break
                time.sleep(2)
            if self.is_completed(self.progress(meta)):
                log.info("已完成：%s", compact(title))
                self._mark_done(leaf, "server")
                return "done"
            log.warning("视频已播完，但服务端尚未标记完成（心跳可能延迟）：%s", compact(title))
            self._mark_failed(leaf, "server-not-completed")
            return "unverified"

        # 拿不到 course_id 时只能以本地播放为准
        self._mark_done(leaf, "played")
        return "done"

    def _mark_done(self, leaf: Dict[str, Any], how: str) -> None:
        self.done[leaf["leaf_id"]] = {
            "title": leaf["title"],
            "how": how,
            "at": datetime.now().isoformat(timespec="seconds"),
        }
        self.failed.pop(leaf["leaf_id"], None)
        save_done(self.state)

    def _mark_failed(self, leaf: Dict[str, Any], reason: str) -> None:
        self.failed[leaf["leaf_id"]] = {
            "title": leaf["title"],
            "reason": reason,
            "at": datetime.now().isoformat(timespec="seconds"),
        }
        save_done(self.state)

    # ---------------------------------------------------------------- 纯接口补进度

    def _heartbeat_entry(
        self,
        meta: Dict[str, Any],
        cp: float,
        seq: int,
        ts_ms: int,
        pg: str,
        duration: float,
    ) -> Dict[str, Any]:
        def as_int(value: Any) -> Any:
            text = str(value)
            return int(text) if text.isdigit() else value

        return {
            "i": 5,
            "et": "videoend" if cp >= duration else "loadeddata",
            "p": "web",
            "n": "ws",
            "lob": "cloud4",
            "cp": round(float(cp), 1),
            "fp": 0,
            "tp": 0,
            "sp": 1,
            "ts": str(ts_ms),
            "u": int(meta["user_id"]),
            "uip": "",
            "c": str(meta["course_id"]),
            "v": as_int(meta["leaf_id"]),
            "skuid": str(meta["sku_id"]),
            "classroomid": str(self.cid),
            "cc": meta.get("ccid") or as_int(meta["video_id"]),
            "d": float(duration),
            "pg": pg,
            "sq": seq,
            "t": "video",
            "cards_id": 0,
            "slide": 0,
            "v_url": "",
        }

    def run_api(self, targets: List[Dict[str, Any]]) -> int:
        """不播放，直接按官方播放器的格式上报观看进度。"""
        log.warning("API 补进度模式：不会播放视频，只上报进度；可能违反课程规则。")
        stats = {"done": 0, "skipped": 0, "failed": 0}

        for n, leaf in enumerate(targets, 1):
            if not self.args.force and leaf["leaf_id"] in self.done:
                log.info("[%d/%d] 本地记录已完成，跳过：%s", n, len(targets), compact(leaf["title"]))
                stats["skipped"] += 1
                continue

            leaf_id = leaf["leaf_id"]
            log.info("[%d/%d] %s", n, len(targets), compact(leaf["title"]))
            meta = self.video_meta(leaf_id)
            if not (meta.get("course_id") and meta.get("user_id") and meta.get("sku_id")):
                log.error("缺少必要参数，跳过：course_id=%s user_id=%s sku_id=%s",
                          meta.get("course_id"), meta.get("user_id"), meta.get("sku_id"))
                stats["failed"] += 1
                self._mark_failed(leaf, "missing-context")
                continue

            prog = self.progress(meta)
            if self.is_completed(prog):
                log.info("服务端已标记完成，跳过：%s", compact(leaf["title"]))
                self._mark_done(leaf, "server")
                stats["skipped"] += 1
                continue

            duration = float(meta.get("duration") or 0)
            if duration <= 0:
                duration = float(prog.get("video_length") or 0)
            if duration <= 0:
                duration = max(float(prog.get("watch_length") or 0) + 120, 600)
            log.info("时长 %.0f 秒，当前进度 %.1f%%，开始补进度",
                     duration, float(prog.get("rate") or 0) * 100)

            pg = f"{leaf_id}_{''.join(random.choices('abcdefghijklmnopqrstuvwxyz', k=5))}"
            ts_base = int(time.time() * 1000)
            seq = 1
            cp = 0.0
            deadline = time.time() + max(120, duration / 20 * 2.5)
            completed = False

            while cp < duration and time.time() < deadline:
                batch = []
                while cp < duration and len(batch) < 50:
                    cp = min(cp + 20.0, duration)
                    batch.append(self._heartbeat_entry(meta, cp, seq, ts_base + seq * 300, pg, duration))
                    seq += 1
                if not self.send_heartbeat(batch):
                    log.warning("心跳上报失败，重试一次")
                    time.sleep(2)
                    if not self.send_heartbeat(batch):
                        break

                for _ in range(8):
                    time.sleep(1.2)
                    prog = self.progress(meta)
                    if self.is_completed(prog):
                        completed = True
                        break
                    server_len = float(prog.get("video_length") or 0)
                    if server_len > duration:
                        duration = server_len
                if completed:
                    break

            if not completed:
                # 最后再给服务端一点时间
                for _ in range(15):
                    time.sleep(2)
                    if self.is_completed(self.progress(meta)):
                        completed = True
                        break

            if completed:
                log.info("已完成：%s", compact(leaf["title"]))
                self._mark_done(leaf, "server")
                stats["done"] += 1
            else:
                log.warning("补进度后仍未完成：%s", compact(leaf["title"]))
                self._mark_failed(leaf, "api-not-completed")
                stats["failed"] += 1

        log.info("API 模式结束：完成/跳过 %d，失败 %d。", stats["done"] + stats["skipped"], stats["failed"])
        return 0 if stats["failed"] == 0 else 1

    def send_heartbeat(self, entries: List[Dict[str, Any]]) -> bool:
        return self.api_post("/video-log/heartbeat/", {"heart_data": entries})

    # ---------------------------------------------------------------- 主流程

    def run(self) -> int:
        if not self.args.login:
            if not self.args.url:
                self.args.url = input("请输入课程网址：").strip()
            self.course_url = self.args.url
            self.cid = parse_classroom_id(self.course_url) or ""
            if not self.cid:
                log.error("网址里没有 classroom_id。请粘贴具体课程页，例如：")
                log.error("  请粘贴具体课程页网址")
                return 2

        self.launch(headless=self.args.api)
        try:
            if self.args.login:
                self.login()
                return 0

            if not self.check_login():
                log.error("未登录。请先运行：python %s --login", Path(__file__).name)
                return 3

            log.info("课程 classroom_id=%s", self.cid)
            self.resolve_course_context()
            chapters = self.fetch_chapters()
            videos = self.flatten_videos(chapters)
            if not videos:
                log.error("没有找到视频单元。")
                return 4

            start = 0
            if self.args.start_from:
                key = self.args.start_from.strip()
                for i, v in enumerate(videos):
                    if key in v["title"] or key == v["leaf_id"]:
                        start = i
                        break
                else:
                    log.warning("没找到包含「%s」的单元，从第一个开始。", key)
                log.info("从第 %d 个视频开始：%s", start + 1, compact(videos[start]["title"]))

            targets = videos[start:]
            if self.args.limit:
                targets = targets[: self.args.limit]

            log.info("共 %d 个视频，本次处理 %d 个。", len(videos), len(targets))
            if self.args.dry_run:
                for v in targets:
                    flag = "已学" if v["leaf_id"] in self.done else "待学"
                    print(f"  [{flag}] {v['index']:>3}. {v['title']}  (leaf_id={v['leaf_id']})")
                return 0

            if self.args.api:
                return self.run_api(targets)

            stats = {"done": 0, "skipped": 0, "failed": 0}
            for n, leaf in enumerate(targets, 1):
                if not self.args.force and leaf["leaf_id"] in self.done:
                    log.info("[%d/%d] 本地记录已完成，跳过：%s", n, len(targets), compact(leaf["title"]))
                    stats["skipped"] += 1
                    continue

                log.info("[%d/%d] %s", n, len(targets), compact(leaf["title"]))
                try:
                    if self.args.dom:
                        result = self.watch_one_dom(leaf)
                    else:
                        result = self.watch_one(leaf)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    log.exception("处理失败：%s", exc)
                    result = "error"

                if result in ("done", "played", "skipped"):
                    stats["done"] += 1
                else:
                    stats["failed"] += 1
                    self._mark_failed(leaf, result)

            log.info("本次结束：完成/跳过 %d，失败 %d。", stats["done"], stats["failed"])
            if self.failed:
                log.info("失败列表已写入：%s", DONE_FILE)
            return 0 if stats["failed"] == 0 else 1

        except KeyboardInterrupt:
            log.warning("收到 Ctrl+C，已保存进度。")
            return 130
        finally:
            self.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="在线课程视频辅助脚本（真实播放/补进度）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python watch.py --login\n"
            '  python watch.py "课程网址" --from 3.2\n'
            '  python watch.py "课程网址" --dry-run\n'
        ),
    )
    p.add_argument("url", nargs="?", help="课程网址（含 studentLog/<classroom_id>）")
    p.add_argument("--login", action="store_true", help="只打开登录页，扫码登录后退出")
    p.add_argument("--from", dest="start_from", help='从包含该文字的单元开始，例如 "3.2"')
    p.add_argument("--limit", type=int, default=0, help="本次最多处理多少个视频（调试用）")
    p.add_argument("--dry-run", action="store_true", help="只列出视频，不播放")
    p.add_argument("--force", action="store_true", help="忽略本地完成记录，全部重播")
    p.add_argument("--skip-done", action="store_true", default=True, help="跳过服务端已完成视频（默认开）")
    p.add_argument("--no-skip-done", dest="skip_done", action="store_false")
    p.add_argument("--rate", type=float, default=1.0, help="播放倍速，默认 1.0；不建议超过 1.5")
    p.add_argument("--no-mute", dest="mute", action="store_false", default=True, help="不静音（默认静音）")
    p.add_argument("--api", action="store_true", help="直接补进度：不播放视频，只向服务端上报观看进度（可能违反课程规则）")
    p.add_argument("--dom", action="store_true", help="不按 URL 跳转，直接点“学习内容”里的视频行（更贴近真实播放器）")
    p.add_argument("--profile", default=default_profile_dir(), help="独立浏览器配置目录")
    p.add_argument("--state-dir", default=str(STATE_DIR), help="进度/日志目录（同时跑多门课时各用一个）")
    p.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    return p


def main() -> int:
    setup_console()
    args = build_parser().parse_args()
    configure_state(args.state_dir)
    setup_logging(args.verbose)
    with KeepAwake():
        return YuketangWatcher(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
