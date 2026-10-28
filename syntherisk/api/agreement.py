"""内测协议：读协议、同意协议、下载协议全文和同意确认。

注册或登录以后，前端先调 GET /api/auth/me：返回里的 agreement.accepted 是 false 时，弹出协议，
同意之前别的接口一律返回 403（code 是 agreement_required，见 api/deps.py 的 require_agreement）。

本文件的全部接口（main.py 挂载时统一加了前缀 /api；都要登录，但不要求已经同意协议：没同意的人正是要用它们来同意）：
    GET   /api/agreement                          当前版本的协议（按界面语言），以及我同意过没有
    GET   /api/agreement/file                     当前版本的协议，纯文本文件（同意之前也能保存，第 2.1 款）
    POST  /api/agreement/accept                   同意协议
    GET   /api/agreement/acceptances              我的同意记录
    GET   /api/agreement/acceptances/{id}/file    一条同意记录的确认和当时的协议全文，纯文本文件（第 2.4 款）

谁调用它：前端（src/Agreement.tsx）。syntherisk/main.py 的 create_app() 把本文件的 router 挂到应用上。
它依赖谁：
    syntherisk/agreement.py  协议的内容、哈希、排成纯文本
    api/deps.py              get_signed_in（只要求登录）、数据库会话
    storage.py               download_response（下载文件的响应）
数据从哪来到哪去：
    协议内容：data/agreements/beta.toml → 启动时读进 ctx.reference.agreement → GET /api/agreement。
    同意：请求体 → agreement_texts（这份内容第一次有人同意时存一份）、agreement_acceptances（每次同意一行）。

本文件里有什么：
    _acceptance_view      一条同意记录的 JSON 样子
    _latest_acceptance    这个人对当前版本最近的一次同意
    current_agreement、download_current、accept、list_acceptances、download_acceptance   接口
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from syntherisk import i18n, models
from syntherisk.agreement import as_text
from syntherisk.api import schemas
from syntherisk.api.deps import Principal, get_ctx, get_db, get_signed_in
from syntherisk.api.views import iso
from syntherisk.context import AppContext
from syntherisk.storage import download_response

router = APIRouter()


def _acceptance_view(row: models.AgreementAcceptance, text: models.AgreementText, username: str) -> dict[str, Any]:
    """一条同意记录返回给前端的样子。

    返回：
        {id, version, language, sha256, accepted_at, username, checked: [{key, label, text}]}。
        checked 只列勾了的项，label、text 取自当时同意的那份内容（不是现在的 beta.toml）。
    """
    boxes = {box["key"]: box for box in text.content["checkboxes"]}
    return {
        "id": row.id,
        "version": row.version,
        "language": row.language,
        "sha256": text.sha256,
        "accepted_at": iso(row.accepted_at),
        "username": username,
        "checked": [{"key": key, "label": boxes[key]["label"], "text": boxes[key]["text"]} for key in row.checked if key in boxes],
    }


def _latest_acceptance(db: Session, user_id: str, version: str) -> tuple[models.AgreementAcceptance, models.AgreementText] | None:
    """这个人对某一版协议最近的一次同意，连同那份内容；没同意过返回 None。"""
    row = db.execute(
        select(models.AgreementAcceptance, models.AgreementText)
        .join(models.AgreementText, models.AgreementText.id == models.AgreementAcceptance.text_id)
        .where(models.AgreementAcceptance.user_id == user_id, models.AgreementAcceptance.version == version)
        .order_by(models.AgreementAcceptance.accepted_at.desc())
        .limit(1)
    ).first()
    return (row[0], row[1]) if row else None


@router.get("/agreement", tags=["账号"])
def current_agreement(principal: Principal = Depends(get_signed_in), db: Session = Depends(get_db),
                      ctx: AppContext = Depends(get_ctx)):
    """当前版本的内测协议，语言跟着界面（X-Language）；还有我同意过没有。

    需要登录，不要求已经同意协议。返回：

    - `version`：版本号，比如 `V0.5`
    - `language`：给的是哪种语言的那一份（`zh`、`en`）
    - `requested_language`：界面要的语言。要的是 `fr`、而协议还没有法语版时，`language` 是 `en`
    - `languages`：协议现在有哪几种语言
    - `sha256`：这一份内容的哈希，同意时原样带回
    - `content`：弹窗和全文要显示的文字：`eyebrow`、`title`、`intro`、`summary`（`[{label, text}]`）、`read_full`、
      `checkboxes`（`[{key, label, text}]`，每一项都要勾上才能同意）、`accept`、`decline`、`footer`、`full_title`、
      `full_text`（“## ”开头的行是条标题，段与段之间空一行）；可能还有 `note`、`guardian_link`、`guardian_text`（V0.8 没有）和 `privacy_button`、`privacy_title`、`privacy_text`（隐私政策，V0.8.1 起有）
    - `acceptance`：我对当前版本最近的一次同意（样子同 `POST /api/agreement/accept` 的返回）；还没同意过是 `null`
    """
    agreement = ctx.reference.agreement
    language, package = agreement.package(i18n.current_lang())
    latest = _latest_acceptance(db, principal.user_id, agreement.version)
    return {
        "version": agreement.version,
        "language": language,
        "requested_language": i18n.current_lang(),
        "languages": sorted(agreement.packages),
        "sha256": agreement.hashes[language],
        "content": {key: value for key, value in package.items() if key not in ("version", "language")},
        "acceptance": _acceptance_view(*latest, principal.user.username) if latest else None,
    }


@router.get("/agreement/file", tags=["账号"])
def download_current(_: Principal = Depends(get_signed_in), ctx: AppContext = Depends(get_ctx)):
    """把当前版本的协议下载成纯文本文件（UTF-8）：弹窗上的说明和协议全文。语言同 `GET /api/agreement`。

    需要登录，不要求已经同意协议：协议第 2.1 款要求同意之前就能保存全文。
    """
    agreement = ctx.reference.agreement
    language, package = agreement.package(i18n.current_lang())
    text = as_text(package, agreement.hashes[language])
    return download_response(text.encode("utf-8"), f"Syntherisk_beta_agreement_{agreement.version}_{language}.txt",
                             "text/plain; charset=utf-8")


@router.post("/agreement/accept", status_code=status.HTTP_201_CREATED, tags=["账号"])
def accept(body: schemas.AgreementAccept, principal: Principal = Depends(get_signed_in), db: Session = Depends(get_db),
           ctx: AppContext = Depends(get_ctx)):
    """同意内测协议。请求体见 `AgreementAccept`。

    需要登录。成功返回 201：这条同意记录 `{id, version, language, sha256, accepted_at, username, checked}`，
    `checked` 是勾了的项 `[{key, label, text}]`。之后别的接口就能用了。
    同一个人可以同意多次（比如换了语言再同意），每次都新记一行，旧的不动。

    出错：409 协议已经更新（版本号或哈希和服务器上的对不上），要重新读一遍再同意；
    422 不是每一项都勾上了，或者没有这种语言的协议。
    """
    # 实现：先核对版本和哈希，确认同意的就是服务器现在给的那一份；再核对勾选项。
    # 那份内容第一次有人同意时，原样存进 agreement_texts（按哈希去重），然后记一行 agreement_acceptances
    agreement = ctx.reference.agreement
    if body.language not in agreement.packages:
        raise HTTPException(422, i18n.tr("没有这种语言的协议", "The agreement is not available in this language"))
    if body.version != agreement.version or body.sha256 != agreement.hashes[body.language]:
        raise HTTPException(status.HTTP_409_CONFLICT, i18n.tr("协议已经更新，请重新阅读后再同意", "The agreement has been updated; please read it again before accepting"))
    package = agreement.packages[body.language]
    required = [box["key"] for box in package["checkboxes"]]
    if not set(required) <= set(body.checked):
        raise HTTPException(422, i18n.tr("每一项都勾选以后才能同意", "Tick every box to accept"))
    text = db.scalars(select(models.AgreementText).where(models.AgreementText.sha256 == body.sha256)).first()
    if text is None:
        text = models.AgreementText(sha256=body.sha256, version=agreement.version, language=body.language, content=package)
        db.add(text)
        # flush 以后 text.id 才有值，下面记同意时要用
        db.flush()
    row = models.AgreementAcceptance(user_id=principal.user_id, text_id=text.id, version=agreement.version,
                                     language=body.language, checked=[key for key in required if key in body.checked])
    db.add(row)
    db.commit()
    return _acceptance_view(row, text, principal.user.username)


@router.get("/agreement/acceptances", tags=["账号"])
def list_acceptances(principal: Principal = Depends(get_signed_in), db: Session = Depends(get_db)):
    """我的全部同意记录，新的在前。每一项的样子同 `POST /api/agreement/accept` 的返回。需要登录。"""
    rows = db.execute(
        select(models.AgreementAcceptance, models.AgreementText)
        .join(models.AgreementText, models.AgreementText.id == models.AgreementAcceptance.text_id)
        .where(models.AgreementAcceptance.user_id == principal.user_id)
        .order_by(models.AgreementAcceptance.accepted_at.desc())
    ).all()
    return [_acceptance_view(row, text, principal.user.username) for row, text in rows]


@router.get("/agreement/acceptances/{acceptance_id}/file", tags=["账号"])
def download_acceptance(acceptance_id: str, principal: Principal = Depends(get_signed_in), db: Session = Depends(get_db)):
    """把一条同意记录下载成纯文本文件（UTF-8）：同意确认（用户名、时间、勾了哪几项、记录编号），
    加上当时同意的那份协议的全文（从数据库里存的那份取，不是现在的 beta.toml）。

    需要登录，只能下载自己的记录。出错：404 没有这条记录（或者是别人的）。
    """
    row = db.get(models.AgreementAcceptance, acceptance_id)
    if row is None or row.user_id != principal.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, i18n.tr("没有这条同意记录", "Acceptance record not found"))
    text = db.get(models.AgreementText, row.text_id)
    view = _acceptance_view(row, text, principal.user.username)
    content = as_text(text.content, text.sha256, {
        "id": row.id, "username": principal.user.username, "checked": row.checked,
        # 纯文本里写成 “2026-10-27 01:23:45”：去掉微秒和时区（标题里已经写明是 UTC）
        "accepted_at": view["accepted_at"][:19].replace("T", " "),
    })
    return download_response(content.encode("utf-8"), f"Syntherisk_beta_agreement_{row.version}_accepted_{row.accepted_at:%Y%m%d}.txt",
                             "text/plain; charset=utf-8")
