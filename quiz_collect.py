# -*- coding: utf-8 -*-
"""
只抓题，不答题、不提交。

用法：
  python quiz_collect.py
  python quiz_collect.py --profile "D:\YuketangAuto\profile2" --out "D:\harness\work\yuketang\quiz2"

流程：
  1. 启动独立 Edge（复用已登录配置）；
  2. 你在浏览器里打开题目页面，显示第 1 题；
  3. 回到终端按 Enter；
  4. 脚本自动读取当前题，点“下一题”，直到最后一题；
  5. 输出 quiz.md / quiz.json / 每题截图。

不会点“提交”，不会填答案。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent


def default_profile_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return str(Path(base) / "YuketangAuto" / "profile")


EXTRACT_JS = r"""
() => {
  const bodyText = document.body.innerText || '';
  const all = [...document.querySelectorAll('*')];

  // 题型标签：1.判断题 / 2.单选题 …
  const label = all.find(e => {
    if (e.children.length !== 0) return false;
    const t = (e.innerText || '').trim();
    return /^\s*\d+\s*[.、]\s*(判断题|单选题|多选题|填空题|简答题|论述题|阅读理解)/.test(t);
  });

  let extracted = '';
  if (label) {
    let node = label;
    for (let i = 0; i < 8 && node.parentElement; i++) {
      node = node.parentElement;
      const t = (node.innerText || '').trim();
      if (t.length > 60 && t.length < 3000) break;
    }
    extracted = (node.innerText || '').trim();
  }

  const inputs = [...document.querySelectorAll('input, textarea')].map(e => ({
    tag: e.tagName,
    type: e.type || '',
    placeholder: e.placeholder || '',
    value: e.value || ''
  }));

  return {raw: bodyText, extracted, inputs};
}
"""


def clean(text: str) -> str:
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="只抓题，不答题不提交")
    parser.add_argument("--profile", default=default_profile_dir(), help="Edge 配置目录")
    parser.add_argument("--out", default=str(BASE_DIR / "quiz_output"), help="输出目录")
    parser.add_argument("--url", default="", help="可选：启动后直接打开的题目页网址")
    parser.add_argument("--max", type=int, default=100, help="最多抓多少题")
    args = parser.parse_args()

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    profile = Path(args.profile).expanduser()

    print("=" * 60)
    print("只抓题模式：不会填答案，也不会点提交。")
    print("=" * 60)

    records: List[Dict[str, Any]] = []

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            channel="msedge",
            headless=False,
            no_viewport=True,
            locale="zh-CN",
            args=["--no-first-run", "--no-default-browser-check", "--window-size=1360,900"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(15000)

        if args.url:
            page.goto(args.url, wait_until="domcontentloaded", timeout=60000)

        print("\n请在浏览器里打开题目页面，确认第 1 题已经显示。")
        print("然后回到这个终端，按 Enter 开始抓题。")
        input()

        for i in range(1, args.max + 1):
            try:
                data = page.evaluate(EXTRACT_JS)
            except Exception as exc:
                print(f"第 {i} 题读取失败：{exc}")
                break

            extracted = clean(data.get("extracted") or "")
            raw = clean(data.get("raw") or "")
            if not extracted and not raw:
                print(f"第 {i} 题没有读到内容，停止。")
                break

            shot = out_dir / f"q{i:03d}.png"
            try:
                page.screenshot(path=str(shot), full_page=False)
            except Exception:
                shot = None

            rec = {
                "index": i,
                "url": page.url,
                "question": extracted or raw,
                "raw": raw,
                "inputs": data.get("inputs") or [],
                "screenshot": str(shot) if shot else "",
            }
            records.append(rec)
            print(f"已抓第 {i} 题（{len(extracted or raw)} 字）")

            # 最后一题：下一题按钮通常不可用
            moved = False
            next_btn = page.get_by_text("下一题", exact=True)
            if next_btn.count() > 0:
                try:
                    if next_btn.first.is_visible() and next_btn.first.is_enabled():
                        next_btn.first.click(timeout=5000)
                        page.wait_for_timeout(1200)
                        moved = True
                except Exception:
                    pass

            if not moved:
                # 有些 quiz 不答题时“下一题”是灰的，改点左侧题号
                target = str(i + 1)
                try:
                    nums = page.get_by_text(target, exact=True)
                    for k in range(nums.count()):
                        el = nums.nth(k)
                        box = el.bounding_box()
                        if box and box["width"] < 100 and box["height"] < 100 and box["x"] < 360:
                            el.click(timeout=3000)
                            page.wait_for_timeout(1200)
                            moved = True
                            break
                except Exception:
                    pass

            if not moved:
                print("既点不了“下一题”，也找不到左侧题号，认为已到最后一题。")
                break

        try:
            ctx.close()
        except Exception:
            pass

    (out_dir / "quiz.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    md = ["# 抓题结果", ""]
    for rec in records:
        md.append(f"## 第 {rec['index']} 题")
        md.append("")
        md.append("```text")
        md.append(rec["question"])
        md.append("```")
        md.append("")
    (out_dir / "quiz.md").write_text("\n".join(md), encoding="utf-8")

    print(f"\n完成，共 {len(records)} 题。")
    print(f"文本：{out_dir / 'quiz.md'}")
    print(f"结构化：{out_dir / 'quiz.json'}")
    print(f"截图：{out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
