"""内测协议：注册或登录以后、用任何功能之前必须同意的《内测使用与保密协议》。

协议的文字在 data/agreements/beta.toml：一个版本号，加上每种语言一份内容（弹窗上的说明、两个勾选项、
按钮上的字、协议全文）。启动时读一次，放在 ctx.reference.agreement 上。

为什么要按哈希认内容
    协议第 2.4 款要求：记录要能对上“谁、什么时候、勾了哪几项、同意的是哪一版、全文是什么”，并且新版不能覆盖旧版。
    所以每种语言的内容算一个 SHA-256。前端同意时把看到的那份的哈希带回来，对得上才记；
    第一次有人同意某一份内容时，把这份内容原样存进 agreement_texts 表（见 models.AgreementText），
    以后 beta.toml 怎么改，当时同意的全文都还在。

什么时候要重新同意
    只看版本号：beta.toml 里的 version 改了，所有人下次打开页面都要重新同意（api/deps.py 的 require_agreement）。
    只改错别字不改版本号时，已经同意过的人不用重新同意。

本文件里有什么
    REQUIRED_KEYS     每种语言的内容里必须有的键
    digest            一份内容的 SHA-256
    Agreement         读进内存的协议：版本号、各语言的内容和哈希
    as_text           把一份内容（和一条同意记录）排成纯文本，给“下载协议全文”和“下载同意确认”用
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 每种语言的内容里必须有的键。summary 是几行 {label, text}；checkboxes 是勾选项 {key, label, text}，
# 每一项都要勾上才能同意；full_text 是协议全文，“## ”开头的行是条标题，其余每段一行、段与段之间空一行
REQUIRED_KEYS = (
    "eyebrow", "title", "intro", "summary", "read_full", "checkboxes",
    "accept", "decline", "footer", "full_title", "full_text",
)
# 可以没有的键：note（说明下面的一行小字）、guardian_link 和 guardian_text（未成年人怎么申请）。V0.5 有，V0.8 没有
OPTIONAL_KEYS = ("note", "guardian_link", "guardian_text")

# 没有某种语言的内容时用哪一种（V0.8 起中、英、法三种都有）
FALLBACK_LANGUAGE = "en"


def digest(package: dict[str, Any]) -> str:
    """一份内容的 SHA-256（十六进制，64 个字符）。

    算法：把内容（含 version 和 language）按键排序、不加空格地写成 JSON，按 UTF-8 编码后算哈希。
    同样的内容在任何机器上算出来都一样，所以前端带回来的哈希可以直接比。
    """
    canonical = json.dumps(package, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class Agreement:
    """读进内存的协议。

    属性：
        version   版本号，比如 "V0.5"
        packages  {语言: 内容}。内容是 beta.toml 里那种语言的整张表，外加 version 和 language 两个键
        hashes    {语言: 内容的 SHA-256}
    """

    version: str
    packages: dict[str, dict[str, Any]]
    hashes: dict[str, str]

    @classmethod
    def load(cls, path: Path) -> "Agreement":
        """读 data/agreements/beta.toml。

        抛出（都发生在启动的时候，服务会起不来）：
            FileNotFoundError  文件不存在。
            ValueError         没有英文版，或者某种语言少了 REQUIRED_KEYS 里的键。
        谁在用：
            reference.ReferenceData.load()。
        """
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
        version = str(raw["version"])
        packages: dict[str, dict[str, Any]] = {}
        for language, content in raw.items():
            if not isinstance(content, dict):
                continue  # version 这样的单个值
            missing = [key for key in REQUIRED_KEYS if key not in content]
            if missing:
                raise ValueError(f"{path.name}：[{language}] 少了 {missing}")
            packages[language] = {"version": version, "language": language, **content}
        if FALLBACK_LANGUAGE not in packages:
            raise ValueError(f"{path.name}：没有 [{FALLBACK_LANGUAGE}]")
        return cls(version=version, packages=packages, hashes={lang: digest(p) for lang, p in packages.items()})

    def package(self, language: str) -> tuple[str, dict[str, Any]]:
        """某种语言的内容。返回 (实际用的语言, 内容)：没有这种语言时用英文，实际用的语言就是 "en"。"""
        if language not in self.packages:
            language = FALLBACK_LANGUAGE
        return language, self.packages[language]


# 下载的纯文本文件里，几处说明文字。按内容的语言选：中文内容用中文，其余用英文
_TEXT_LABELS = {
    "zh": {
        "version": "版本", "confirmation": "同意确认", "user": "用户名", "time": "同意时间（UTC）",
        "checked": "勾选的选项", "record": "记录编号", "summary": "弹窗上的说明", "full": "协议全文",
        "language": {"zh": "中文", "en": "英文", "fr": "法文"},
    },
    "en": {
        "version": "Version", "confirmation": "Acceptance confirmation", "user": "Username", "time": "Accepted at (UTC)",
        "checked": "Options selected", "record": "Record ID", "summary": "Popup text", "full": "Full agreement",
        "language": {"zh": "Chinese", "en": "English", "fr": "French"},
    },
}


def as_text(package: dict[str, Any], sha256: str, acceptance: dict[str, Any] | None = None) -> str:
    """把一份内容排成纯文本：标题、版本和哈希，（有同意记录时）同意确认，弹窗上的说明，协议全文。

    参数：
        package     一份内容（Agreement.packages 里的一项，或者 agreement_texts 表里存的 content）。
        sha256      这份内容的哈希。
        acceptance  同意记录：{"username", "accepted_at"（UTC 时间的字符串）, "checked"（勾了的键）, "id"}；
                    None 时不写同意确认那一段（同意之前下载协议全文）。
    返回：
        纯文本，换行用 \\n。
    谁在用：
        api/agreement.py 的 download_current、download_acceptance。
    """
    labels = _TEXT_LABELS["zh" if package["language"] == "zh" else "en"]
    language = labels["language"].get(package["language"], package["language"])
    rule = "=" * 60
    lines = [package["full_title"], f"{labels['version']} {package['version']} · {language} · SHA-256 {sha256}", ""]
    if acceptance is not None:
        lines += [rule, labels["confirmation"], rule,
                  f"{labels['user']}: {acceptance['username']}",
                  f"{labels['time']}: {acceptance['accepted_at']}",
                  f"{labels['checked']}:"]
        checked = set(acceptance["checked"])
        lines += [f"  [{'x' if box['key'] in checked else ' '}] {box['label']}: {box['text']}" for box in package["checkboxes"]]
        lines += [f"{labels['record']}: {acceptance['id']}", ""]
    lines += [rule, labels["summary"], rule, package["title"], "", package["intro"], ""]
    lines += [f"{row['label']}: {row['text']}" for row in package["summary"]]
    lines += ["", package["note"], ""] if package.get("note") else [""]
    lines += [f"{box['label']}: {box['text']}" for box in package["checkboxes"]]
    lines += ["", package["guardian_text"], ""] if package.get("guardian_text") else [""]
    lines += [rule, labels["full"], rule, package["full_title"], ""]
    # 全文里 “## ” 开头的是条标题：去掉记号，前面空一行
    for paragraph in package["full_text"].strip().split("\n\n"):
        lines += [paragraph[3:] if paragraph.startswith("## ") else paragraph, ""]
    return "\n".join(lines)
