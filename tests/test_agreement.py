"""内测协议：注册以后、同意之前什么都用不了；同意时记下谁、什么时候、勾了哪几项、哪一版的全文。

这个文件测的是什么
    协议的文字在 data/agreements/beta.toml，读进来的样子见 syntherisk/agreement.py。
    没同意当前版本的人，除 /api/auth/me 和协议的几个接口以外都返回 403（api/deps.py 的 require_agreement）；
    同意走 POST /api/agreement/accept（api/agreement.py），带回看到的那一份的版本和哈希，每个勾选项都要勾上。
    协议第 2.4 款要求的记录：同意记录能对上当时的全文，版本升级以后旧的同意和旧的全文都还在。

涉及的接口
    GET  /api/auth/me                              agreement.accepted 告诉前端要不要弹协议
    GET  /api/agreement、/api/agreement/file        读协议、下载协议全文
    POST /api/agreement/accept                     同意
    GET  /api/agreement/acceptances[/{id}/file]    我的同意记录、下载同意确认

本文件里有什么
    _register_only()     只注册、不同意协议的账号
    test_the_agreement_file_has_the_same_clauses_in_every_language
    test_nothing_works_until_the_agreement_is_accepted
    test_accepting_checks_version_hash_and_every_box
    test_acceptance_is_recorded_with_the_text_and_survives_a_new_version
    test_french_interface_gets_the_french_agreement
    test_a_missing_language_falls_back_to_english
"""

from __future__ import annotations

import re
from dataclasses import replace

from syntherisk.agreement import digest
from tests.conftest import TEST_PASSWORD, accept_agreement


def _register_only(client, username: str = "new@example.com") -> dict:
    """注册一个账号，但不替这个账号同意协议；返回登录请求头。"""
    response = client.post("/api/auth/register", json={"username": username, "password": TEST_PASSWORD, "org_name": "组"})
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_the_agreement_file_has_the_same_clauses_in_every_language(ctx):
    """每种语言的协议条款编号一一对应：中、英、法都是 12 条 54 款，款号（1.1、1.2 …）的顺序完全一样。"""
    agreement = ctx.reference.agreement
    assert set(agreement.packages) == {"zh", "en", "fr"}
    numbering = {}
    for language, package in agreement.packages.items():
        paragraphs = package["full_text"].strip().split("\n\n")
        articles = [p for p in paragraphs if p.startswith("## ")]
        clauses = [m.group(0) for p in paragraphs if (m := re.match(r"^\d+\.\d+", p))]
        numbering[language] = (len(articles), clauses)
        # 三个勾选项：同意协议、选择用这种语言订立合同、已阅读隐私政策；隐私政策 11 节
        assert [box["key"] for box in package["checkboxes"]] == ["accept", "language", "privacy"]
        assert len(package["summary"]) == 3
        assert package["privacy_text"].count("## ") == 11 and "admin@syntherisk.com" in package["note"]
    assert numbering["zh"][0] == 12 and len(numbering["zh"][1]) == 54
    assert all(value == numbering["zh"] for value in numbering.values())


def test_nothing_works_until_the_agreement_is_accepted(client):
    """注册完：/api/auth/me 说还没同意，别的接口 403（code 是 agreement_required）；同意以后才能用。"""
    headers = _register_only(client)
    me = client.get("/api/auth/me", headers=headers).json()
    assert me["agreement"]["accepted"] is False and me["agreement"]["version"]

    blocked = client.get("/api/projects", headers=headers)
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["code"] == "agreement_required"
    # 报错也跟着语言走
    english = client.get("/api/projects", headers={**headers, "X-Language": "en"}).json()["detail"]
    assert english["message"] == "Please read and accept the beta agreement first"

    # 协议本身、下载全文不受限制
    current = client.get("/api/agreement", headers=headers).json()
    assert current["language"] == "zh" and current["acceptance"] is None
    assert current["content"]["accept"] == "同意协议并进入内测"
    file = client.get("/api/agreement/file", headers=headers)
    assert file.status_code == 200 and file.headers["content-type"].startswith("text/plain")
    assert "Syntherisk 内测使用与保密协议" in file.text and "第十二条" in file.text and "Syntherisk 隐私政策" in file.text

    accept_agreement(client, headers)
    assert client.get("/api/auth/me", headers=headers).json()["agreement"]["accepted"] is True
    assert client.get("/api/projects", headers=headers).status_code == 200


def test_accepting_checks_version_hash_and_every_box(client):
    """版本或哈希对不上 409；少勾一项 422；没有这种语言 422。都不会留下同意记录。"""
    headers = _register_only(client)
    current = client.get("/api/agreement", headers={**headers, "X-Language": "en"}).json()
    body = {"version": current["version"], "language": "en", "sha256": current["sha256"], "checked": ["accept", "language", "privacy"]}

    assert client.post("/api/agreement/accept", headers=headers, json={**body, "version": "V0.1"}).status_code == 409
    # 拿英文那份的哈希说自己同意的是中文版：对不上
    assert client.post("/api/agreement/accept", headers=headers, json={**body, "language": "zh"}).status_code == 409
    assert client.post("/api/agreement/accept", headers=headers, json={**body, "checked": ["accept"]}).status_code == 422
    assert client.post("/api/agreement/accept", headers=headers, json={**body, "checked": ["accept", "language"]}).status_code == 422
    assert client.post("/api/agreement/accept", headers=headers, json={**body, "language": "de"}).status_code == 422
    assert client.get("/api/agreement/acceptances", headers=headers).json() == []

    accepted = client.post("/api/agreement/accept", headers=headers, json=body)
    assert accepted.status_code == 201
    assert [box["key"] for box in accepted.json()["checked"]] == ["accept", "language", "privacy"]


def test_acceptance_is_recorded_with_the_text_and_survives_a_new_version(client, ctx):
    """同意记录对得上当时的全文；协议升级以后要重新同意，旧的同意和旧的全文都还在（第 2.4、13.2 款）。"""
    headers = _register_only(client, "anna-two")
    record = accept_agreement(client, headers)
    old = ctx.reference.agreement
    assert record["version"] == old.version and record["sha256"] == old.hashes["zh"]
    assert record["username"] == "anna-two"

    # 下载同意确认：用户名、勾了的两项、记录编号、当时的全文
    confirmation = client.get(f"/api/agreement/acceptances/{record['id']}/file", headers=headers).text
    assert "anna-two" in confirmation and record["id"] in confirmation
    assert confirmation.count("[x]") == 3 and "第一条 协议主体及组成" in confirmation
    # 别人看不到这条记录
    other = _register_only(client, "someone-else")
    assert client.get(f"/api/agreement/acceptances/{record['id']}/file", headers=other).status_code == 404

    # 协议升级到新版本（改了全文）：原来同意过的人也要重新同意
    packages = {lang: {**p, "version": "V0.6", "full_text": p["full_text"] + "\n\n14.1 新加的一款。"} for lang, p in old.packages.items()}
    ctx.reference.agreement = replace(old, version="V0.6", packages=packages,
                                      hashes={lang: digest(p) for lang, p in packages.items()})
    try:
        assert client.get("/api/projects", headers=headers).status_code == 403
        renewed = accept_agreement(client, headers)
        assert renewed["version"] == "V0.6" and renewed["sha256"] != record["sha256"]
        # 两条记录都在，旧的那条下载出来仍是旧的全文
        assert [r["version"] for r in client.get("/api/agreement/acceptances", headers=headers).json()] == ["V0.6", old.version]
        assert "新加的一款" not in client.get(f"/api/agreement/acceptances/{record['id']}/file", headers=headers).text
        assert "新加的一款" in client.get(f"/api/agreement/acceptances/{renewed['id']}/file", headers=headers).text
    finally:
        ctx.reference.agreement = old


def test_french_interface_gets_the_french_agreement(client):
    """法语界面拿到的是法语版协议，同意时记下的语言是法语。"""
    headers = _register_only(client)
    current = client.get("/api/agreement", headers={**headers, "X-Language": "fr"}).json()
    assert current["requested_language"] == "fr" and current["language"] == "fr"
    assert current["content"]["accept"] == "Accepter la Convention et accéder à l'essai"
    accepted = client.post("/api/agreement/accept", headers=headers, json={
        "version": current["version"], "language": "fr", "sha256": current["sha256"], "checked": ["accept", "language", "privacy"]})
    assert accepted.status_code == 201 and accepted.json()["language"] == "fr"


def test_a_missing_language_falls_back_to_english(ctx):
    """内容里没有的语言给英文版。"""
    language, package = ctx.reference.agreement.package("de")
    assert language == "en" and package["accept"] == "Accept Agreement and Enter Beta"
