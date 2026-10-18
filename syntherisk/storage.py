"""文件存储：报告和实验附件加密后放在 var/files 下，文件名是随机的。

本地磁盘够验证版用；上云以后换成对象存储，只需要改这一个文件。

这个文件负责什么
    四个小函数，管“文件进出后端”的两头：
      存和取  save_file() / load_file()：生成的报告、对比表和用户上传的实验附件，加密后写到 var/files；
              数据库里只记随机的存储文件名（reports.storage_name、experiment_attachments.storage_name）
      收和发  read_upload()：读用户上传的文件并限制大小；
              download_response()：把文件内容包成“下载文件”的 HTTP 响应

对应 PPT 里的哪个模块、界面上的哪些按钮
    M13 报告生成   F3 生成并下载、D4 导出对比：save_file、load_file、download_response
    M14 回填       G2 附 TLC / NMR 照片：read_upload、save_file、load_file、download_response
    另外 read_upload 也用于 A4 上传结构文件、E1 导入库存表格（这两处读完当场处理，文件不存盘）

谁调用它、它依赖谁
    api/reports.py（四个函数都用）、api/inventory.py 和 api/structures.py（只用 read_upload）。
    它依赖 ctx.settings（files_dir、max_upload_mb）和 ctx.cipher（security.py 的 Cipher）。

数据从哪来到哪去
    写 / 读  <var_dir>/files/<32 个十六进制字符的随机文件名>，内容是 Fernet 密文，没有扩展名
    不碰数据库：记录存储文件名、原始文件名、大小的那一行由调用方写

本文件里有什么
    save_file()          加密并写盘，返回随机的存储文件名
    load_file()          按存储文件名读盘并解密
    read_upload()        读上传的文件，超过大小上限或者是空文件就报错
    download_response()  生成带“另存为”文件名的下载响应
"""

from __future__ import annotations

import uuid
from urllib.parse import quote

from fastapi import HTTPException, UploadFile, status
from fastapi.responses import Response

from syntherisk import i18n
from syntherisk.context import AppContext


def save_file(ctx: AppContext, data: bytes) -> str:
    """把一个文件的内容加密后存到 var/files 下，返回存储用的文件名。

    参数：
        ctx   运行时上下文（用它的 settings.files_dir 和 cipher）。
        data  文件的原始内容（PDF、Word、zip、表格、图片……的字节）。
    返回：
        存储文件名：32 个十六进制字符的随机串（UUID），和用户看到的文件名没有关系。
        调用方要把它记进数据库（storage_name 列），以后凭它调 load_file() 取回文件。
    副作用：
        在 <var_dir>/files/ 下新建一个文件，内容是密文。整个文件一次性在内存里加密，没有分块。
    抛出：
        磁盘写不进去时抛 OSError（这里不处理）。
    谁在用：
        api/reports.py 的 _store_report()（报告和对比表）和 add_attachment()（实验附件）。
    """
    # 文件名用随机串：磁盘上看不出是谁的什么文件，也不会重名
    name = uuid.uuid4().hex
    (ctx.settings.files_dir / name).write_bytes(ctx.cipher.encrypt_bytes(data))
    return name


def load_file(ctx: AppContext, storage_name: str) -> bytes:
    """按存储文件名读出文件并解密，返回原始内容。

    参数：
        ctx           运行时上下文。
        storage_name  save_file() 当初返回的文件名。它来自数据库的 storage_name 列，不是用户输入的。
    返回：
        文件的原始字节。
    抛出：
        HTTPException 404  磁盘上没有这个文件（“文件不存在或已被清理”）。
                           这里直接抛 HTTP 错误，因为这个函数只在接口里用。
        cryptography.fernet.InvalidToken  密钥和存的时候不一样，或者文件被改过。这里不处理，接口会返回 500。
    谁在用：
        api/reports.py 的 download_report()、download_attachment()。
        调用之前它们已经核对过这条记录属于当前登录者的课题组。
    """
    path = ctx.settings.files_dir / storage_name
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, i18n.tr("文件不存在或已被清理", "The file does not exist or has been removed"))
    return ctx.cipher.decrypt_bytes(path.read_bytes())


async def read_upload(ctx: AppContext, upload: UploadFile) -> bytes:
    """把用户上传的文件整个读进内存，同时检查大小。

    参数：
        ctx     运行时上下文（用它的 settings.max_upload_mb）。
        upload  FastAPI 的上传文件对象（接口参数 file: UploadFile = File(...) 拿到的那个）。
    返回：
        文件内容（字节）。
    抛出：
        HTTPException 413  文件超过 MAX_UPLOAD_MB（默认 20 MB）。
        HTTPException 422  文件是空的。
    说明：
        这是一个 async 函数（upload.read 是异步的），调用时要写 await read_upload(...)，
        所以用到它的三个接口函数也都是 async def。
    谁在用：
        api/structures.py 的 upload_structure()（A4）、api/inventory.py 的 import_inventory()（E1）、
        api/reports.py 的 add_attachment()（G2）。
    """
    # 上限从 MB 换算成字节
    limit = ctx.settings.max_upload_mb * 1024 * 1024
    # 最多只读“上限 + 1”个字节：读到的比上限多，就知道文件超了，不用把一个很大的文件全读进内存才发现
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, i18n.tr("文件超过 {mb} MB", "The file is larger than {mb} MB", mb=ctx.settings.max_upload_mb))
    if not data:
        raise HTTPException(422, i18n.tr("文件是空的", "The file is empty"))
    return data


def download_response(data: bytes, filename: str, media_type: str) -> Response:
    """把一段文件内容包成“下载文件”的 HTTP 响应：浏览器会弹出另存为，而不是在页面里直接打开。

    参数：
        data        文件内容。
        filename    下载时显示的文件名，可以有中文，比如 “布洛芬_格氏羧基化_….pdf”。
        media_type  内容类型（响应头 Content-Type），比如 application/pdf；见 api/reports.py 的 MEDIA_TYPES。
    返回：
        fastapi 的 Response，带 Content-Disposition 响应头。
    谁在用：
        api/reports.py 的 export_comparison()、download_report()、download_attachment()。
    """
    # 文件名里有中文：ASCII 的 filename 给老浏览器兜底，filename* 给出真正的名字
    # fallback：把文件名里的非 ASCII 字符直接丢掉（"布洛芬_报告.pdf" 变成 "_.pdf"）；丢完什么都不剩就叫 "download"
    fallback = filename.encode("ascii", "ignore").decode("ascii").strip() or "download"
    # Content-Disposition: attachment 表示“当作附件下载”。
    # filename*=UTF-8''… 是 RFC 5987 规定的写法：quote() 把文件名按 UTF-8 做百分号编码（“布”变成 %E5%B8%83），
    # 新一些的浏览器优先用它，所以用户看到的是完整的中文文件名
    disposition = f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"
    return Response(content=data, media_type=media_type, headers={"Content-Disposition": disposition})
