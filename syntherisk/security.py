"""密码、登录令牌、数据加密。

PPT 的要求：客户库存、目标结构和实验回填加密存储、按组织隔离。
这里做的是应用层加密：敏感内容整段加密后再入库，数据库里看不到明文。

这个文件负责什么（三件互相独立的事）
    1. 密码：注册时把密码算成 scrypt 哈希再存（hash_password），登录时核对（verify_password）。
       数据库里没有明文密码，也没法从哈希反推出密码。
    2. 登录令牌：登录成功后发一个 JWT（create_access_token），之后每个请求带着它，
       后端验证签名并读出“是谁、哪个课题组”（decode_access_token）。
    3. 数据加密：Cipher 用一把对称密钥（Fernet）加解密 JSON 和文件；EncryptedJSON 是套在数据库列上的一层，
       写入时自动加密、读出时自动解密，业务代码看到的始终是普通的字典和列表。
       “盲索引”让库存可以按结构精确查找，而不用解密整张表。

对应 PPT 里的哪个模块
    M0 应用服务（账号和权限），以及全局的数据安全要求；不直接对应按钮。
    注册、登录接口在 api/core.py；“当前登录的是谁”在 api/deps.py。

谁调用它、它依赖谁
    api/core.py          hash_password、verify_password、create_access_token（注册、登录、加成员）
    api/deps.py          decode_access_token（每个要登录的接口）
    cli.py               hash_password（seed-demo 建演示账号）
    context.py           Cipher、set_cipher（启动时用 DATA_ENCRYPTION_KEY 建加密器并登记）
    models.py            EncryptedJSON（各表的加密列）
    storage.py           通过 ctx.cipher 调 Cipher.encrypt_bytes / decrypt_bytes（报告和附件文件）
    m04_availability.py  get_cipher().blind_index（库存的结构盲索引）
    它依赖标准库的 hashlib、hmac、secrets，以及 PyJWT（import jwt）和 cryptography（Fernet）。

两把密钥（都是 config.py 里 Settings 的配置项）
    SECRET_KEY           只用来给登录令牌签名。换了它，已经发出的令牌全部失效，大家重新登录即可，数据不受影响
    DATA_ENCRYPTION_KEY  加密数据用。换了或丢了它，已经加密的数据就解不开，库存的盲索引也对不上

本文件里有什么
    _SCRYPT_N / _SCRYPT_R / _SCRYPT_P             scrypt 的三个强度参数
    _b64() / _unb64()                             字节串和 URL 安全的 base64 文本互转
    hash_password() / verify_password()           密码哈希和核对
    create_access_token() / decode_access_token() 发令牌、验令牌
    Cipher                                        加解密和盲索引
    set_cipher() / get_cipher()                   全局加密器的登记和取用
    EncryptedJSON                                 加密的 JSON 列类型
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

# ---------------------------------------------------------------- 密码

# scrypt 的强度参数（标准库 hashlib.scrypt 的 n、r、p）。scrypt 是专门给密码设计的慢哈希：
# 故意算得慢、占内存多，这样即使哈希值泄露了，也很难靠大量试密码反推出来。
#   N = 2**14 = 16384  迭代规模（CPU 和内存开销都随它增长），必须是 2 的整数次幂
#   r = 8              块大小；算一次大约占 128 × N × r 字节 = 16 MB 内存
#   p = 1              并行度
# N=16384、r=8、p=1 是 scrypt 最常见的一组取值。
# 这三个数会和哈希一起存进 users.password_hash，所以以后把它们调大，旧密码照样能核对
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1


def _b64(raw: bytes) -> str:
    """把字节串编成 URL 安全的 base64 文本（只含字母、数字、- 和 _，末尾可能有 = 补位）。存盐和哈希值用。"""
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    """_b64 的逆操作：base64 文本还原成字节串。文本不是合法的 base64 时抛 ValueError。"""
    return base64.urlsafe_b64decode(text.encode("ascii"))


def hash_password(password: str) -> str:
    """把明文密码算成可以存进数据库的哈希字符串。

    参数：
        password  用户输入的明文密码。
    返回：
        形如 "scrypt$16384$8$1$<盐>$<哈希>" 的字符串，用 $ 分成六段：
        算法名、N、r、p、盐（16 个随机字节的 base64）、哈希值（32 个字节的 base64）。
        存在 users.password_hash 里（那一列最长 300 个字符，这个字符串不到 100 个字符）。
    说明：
        每次调用都随机生成新的盐，所以同一个密码算两次，结果不一样；
        核对密码要用 verify_password()，不能直接比两个字符串。
    谁在用：
        api/core.py 的 register()、add_member()；cli.py 的 cmd_seed_demo()。
    """
    # 盐：16 个随机字节，和哈希一起明文保存。它让相同的密码得到不同的哈希，别人预先算好的密码表就用不上了
    salt = secrets.token_bytes(16)
    # dklen=32：输出 32 个字节的哈希。maxmem：允许 scrypt 最多用 64 MB 内存（按上面的参数实际需要约 16 MB）
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        dklen=32, maxmem=64 * 1024 * 1024,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """核对密码：用存着的盐和参数把输入的密码再算一遍，看结果和存着的哈希是不是一样。

    参数：
        password  用户登录时输入的明文密码。
        stored    数据库里存的哈希字符串（hash_password 的返回值）。
    返回：
        True 密码对。
        False 密码不对，或者 stored 的格式不认识：段数不是六、不是 scrypt 开头、参数不是数字、盐解不开。
    抛出：
        正常由 hash_password() 生成的值不会抛异常。只有一种情况例外：前面几段都合法、唯独最后的哈希那一段
        不是合法的 base64 时，会抛 ValueError（那一段是在函数最后、try 之外才解码的）。
    谁在用：
        api/core.py 的 login()。
    """
    try:
        # 按 $ 拆成六段；段数不是六时，这一行赋值就抛 ValueError，落到下面的 except
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        # 用存下来的 n、r、p 和盐重算，而不是用文件开头的常量：以后改了常量，旧账号的密码照样能核对
        candidate = hashlib.scrypt(
            password.encode("utf-8"), salt=_unb64(salt), n=int(n), r=int(r), p=int(p),
            dklen=32, maxmem=64 * 1024 * 1024,
        )
    except (ValueError, TypeError):
        return False
    # hmac.compare_digest 做“恒定时间”的比较：不管两个值从第几个字节开始不一样，花的时间都相同，
    # 别人没法靠测量响应时间一点点猜出哈希值。普通的 == 一发现不同就返回，会泄露这个信息
    return hmac.compare_digest(candidate, _unb64(digest))


# ---------------------------------------------------------------- 登录令牌


def create_access_token(*, secret: str, user_id: str, org_id: str, minutes: int) -> str:
    """签发登录令牌（JWT）。注册或登录成功后发给前端，前端之后放在请求头 Authorization: Bearer <令牌> 里。

    JWT 是一段分成三节、用点隔开的文本：头部.内容.签名。内容只是 base64 编码，谁拿到都能读，
    所以里面只放编号，不放敏感数据；签名保证内容没被改过（改了就验不过）。
    参数（都必须用关键字传）：
        secret   签名密钥，Settings.secret_key。
        user_id  用户编号，写进令牌的 sub 字段。
        org_id   课题组编号，写进 org 字段。接口按它隔离数据。
        minutes  有效期，单位分钟，Settings.access_token_minutes（默认 720，即 12 小时）。
    返回：
        令牌字符串。内容是 {"sub": 用户编号, "org": 课题组编号, "iat": 签发时间, "exp": 过期时间}，
        用 HS256 签名（HMAC-SHA256，对称签名：签发和验证用的是同一把密钥）。
    谁在用：
        api/core.py 的 _token_response()（注册和登录接口的返回值）。
    """
    now = datetime.now(timezone.utc)
    # iat（签发时间）和 exp（过期时间）是 JWT 的标准字段。PyJWT 会把 datetime 换成时间戳（1970 年起的秒数），
    # 解码时自动检查 exp：过了这个时间令牌就无效
    payload = {"sub": user_id, "org": org_id, "iat": now, "exp": now + timedelta(minutes=minutes)}
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_access_token(token: str, secret: str) -> dict[str, Any] | None:
    """验证登录令牌，并读出里面的内容。

    参数：
        token   前端带来的令牌字符串。
        secret  签名密钥，Settings.secret_key。
    返回：
        令牌有效：内容字典 {"sub": 用户编号, "org": 课题组编号, "iat": …, "exp": …}（iat、exp 是时间戳整数）。
        令牌无效：None。包括签名对不上（内容被改过，或者 SECRET_KEY 换了）、已经过期、根本不是 JWT。
    说明：
        algorithms=["HS256"] 明确只接受这一种签名算法，令牌自称用别的算法的一律不认。
        这里只验令牌本身；用户是否还存在、是否被停用、是否还在这个课题组，由调用方接着查数据库。
    谁在用：
        api/deps.py 的 authenticate()。
    """
    try:
        return jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        # PyJWTError 是 PyJWT 所有错误的基类：签名不对、过期、格式不对都算在内
        return None


# ---------------------------------------------------------------- 数据加密


class Cipher:
    """对称加密。同一把密钥还派生出“盲索引”，用来在不解密的情况下按结构精确匹配库存。

    加密用的是 cryptography 库的 Fernet：AES-128-CBC 加密，再加 HMAC-SHA256 防篡改；密文里带时间戳和随机数，
    所以同一段明文每次加密出来的密文都不一样。密文被改过或者密钥不对，解密时会报错，而不是得到一堆乱码。

    盲索引要解决的问题：库存的 SMILES 加密以后，没法再用 SQL 的 WHERE 去问“库存里有没有这个结构”。
    办法是另存一列结构的“指纹”：HMAC-SHA256(索引密钥, "组织编号:结构的 key14")。
    同一个组织、同一个结构算出来的值总是一样的，可以直接用等号查；
    而没有密钥的人既不能从它反推出结构，也没法自己算出某个结构的值来比对。

    谁在用：
        context.py 的 build_context() 建一个，放在 ctx.cipher 上，并用 set_cipher() 登记成全局的。
    """

    def __init__(self, key: str):
        """用 DATA_ENCRYPTION_KEY 建加密器。

        参数：
            key  Fernet 密钥：32 个随机字节的 URL 安全 base64 文本（44 个字符）。生成方法：
                 python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
        抛出：
            key 不是合法的 Fernet 密钥时，Fernet 抛 ValueError。
        """
        self._fernet = Fernet(key.encode("ascii") if isinstance(key, str) else key)
        # 盲索引另用一把密钥，由主密钥算出来：SHA-256("syntherisk-blind-index:" + 主密钥)，得到 32 个字节。
        # 前面那段固定的文字是用途标签：派生出来的这把密钥只用于盲索引，和加密用的不是同一把
        self._index_key = hashlib.sha256(b"syntherisk-blind-index:" + key.encode("ascii")).digest()

    def encrypt_bytes(self, data: bytes) -> bytes:
        """加密一段二进制数据（文件内容）。返回 Fernet 密文（ASCII 字节串，比原文长三分之一多一点）。

        谁在用：storage.py 的 save_file()（报告、对比表、实验附件写盘之前）。
        """
        return self._fernet.encrypt(data)

    def decrypt_bytes(self, token: bytes) -> bytes:
        """解密 encrypt_bytes() 的结果，返回原来的二进制数据。

        抛出：密钥不对或者密文被改过时，抛 cryptography.fernet.InvalidToken（这里没有换成别的错误）。
        谁在用：storage.py 的 load_file()（下载报告和附件时）。
        """
        return self._fernet.decrypt(token)

    def encrypt_json(self, value: Any) -> str:
        """把一个能转成 JSON 的值（字典、列表、字符串、数字……）加密成一段文本，用来存进数据库的加密列。

        返回：Fernet 密文的字符串形式（只含 ASCII 字符）。
        抛出：value 里有不能转成 JSON 的东西（比如 datetime、set）时，json.dumps 抛 TypeError。
        谁在用：本文件的 EncryptedJSON.process_bind_param()。
        """
        # 先转成紧凑的 JSON 文本，再转成 UTF-8 字节：
        #   ensure_ascii=False      中文原样保留，不转成 \uXXXX 这种转义写法（省空间）
        #   separators=(",", ":")   逗号和冒号后面不加空格（省空间）
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self._fernet.encrypt(raw).decode("ascii")

    def decrypt_json(self, token: str) -> Any:
        """解密 encrypt_json() 的结果，还原成原来的字典或列表。

        抛出：
            RuntimeError  密钥和加密时用的不一样，或者密文被改过。报错信息直接点明要检查 DATA_ENCRYPTION_KEY。
        谁在用：本文件的 EncryptedJSON.process_result_value()。
        """
        try:
            return json.loads(self._fernet.decrypt(token.encode("ascii")).decode("utf-8"))
        except InvalidToken as exc:
            # 换成一句看得懂的话再抛。典型的情形：.env 里的 DATA_ENCRYPTION_KEY 换过，
            # 或者开发环境的 var/dev_secrets.json 被删掉后重新生成了一把新密钥
            raise RuntimeError(
                "数据解密失败：DATA_ENCRYPTION_KEY 和加密这批数据时用的密钥不一致"
            ) from exc

    def blind_index(self, scope: str, value: str) -> str:
        """算盲索引：HMAC-SHA256(索引密钥, "scope:value")，返回 64 个十六进制字符。

        参数：
            scope  范围。库存用的是组织编号：两个课题组存了同一个结构，索引值也不一样，互相看不出来。
            value  要建索引的值。库存用的是结构的 key14（InChIKey 的前 14 位，只看原子怎么连，不分立体异构）。
        返回：
            同样的 scope 和 value 永远得到同样的结果，所以可以存在普通的列里，用等号查询。
        谁在用：
            m04_availability.py 的 InventoryService.structure_index()，结果存在 inventory_items.structure_index。
        """
        return hmac.new(self._index_key, f"{scope}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()


# 全局的加密器。数据库的加密列（EncryptedJSON）是 SQLAlchemy 在读写时调用的，拿不到 ctx，
# 所以加密器另外登记在这个模块级变量里。build_context() 调 set_cipher() 之前它是 None
_cipher: Cipher | None = None


def set_cipher(cipher: Cipher) -> None:
    """登记全局加密器。登记之后，数据库的加密列才能读写。

    一个进程里只有一个：再调一次会换掉原来的（测试里每建一次上下文就重新登记一次）。
    谁在用：context.py 的 build_context()。
    """
    global _cipher
    _cipher = cipher


def get_cipher() -> Cipher:
    """取全局加密器。

    抛出：RuntimeError，如果还没有调用过 build_context()（比如脱离后端单独导入 models 去查库）。
    谁在用：本文件的 EncryptedJSON；m04_availability.py 的 InventoryService.structure_index()。
    """
    if _cipher is None:
        raise RuntimeError("加密组件还没初始化：先调用 syntherisk.context.build_context()")
    return _cipher


class EncryptedJSON(TypeDecorator):
    """加密后存成文本的 JSON 列。

    改内容时要整体重新赋值（row.payload = {**row.payload, "k": v}），原地修改不会被保存。
    （另一种办法是改完以后调 flag_modified(row, "payload")，明确告诉 SQLAlchemy 这一列变了。）

    TypeDecorator 是 SQLAlchemy 提供的“在已有的列类型外面包一层转换”的办法：
        写入时（INSERT / UPDATE）  process_bind_param()    Python 值 -> 存进数据库的值
        读出时（SELECT）           process_result_value()  数据库里的值 -> Python 值
    这里包的是 Text（不限长度的文本）：数据库里存的是一长串 Fernet 密文，业务代码拿到的是解密好的字典或列表。

    因为整列是一段密文，数据库没法按里面的内容查询、排序或者建索引。要按内容找，只能读出来在内存里筛
    （库存搜索就是这样做的），或者另存一列盲索引（见 Cipher.blind_index）。

    谁在用：models.py 里的这些列——
        drafts.payload、analyses.target / constraints / user_exclusions / result、
        inventory_items.payload、inventory_import_rows.payload、experiment_records.payload。
    """

    # impl：底层实际用的列类型
    impl = Text
    # cache_ok：告诉 SQLAlchemy 这个类型没有会影响 SQL 生成的可变状态，用到它的语句可以放进编译缓存
    cache_ok = True

    def process_bind_param(self, value, dialect):
        """写入数据库之前由 SQLAlchemy 调用：把 Python 值加密成密文字符串。None 原样存成 NULL，不加密。

        参数 value 是赋给这一列的值；dialect 是数据库方言（SQLite / PostgreSQL），这里用不到。
        """
        return None if value is None else get_cipher().encrypt_json(value)

    def process_result_value(self, value, dialect):
        """从数据库读出之后由 SQLAlchemy 调用：把密文字符串解密成 Python 值。NULL 读出来是 None。

        抛出：密钥不对时抛 RuntimeError（见 Cipher.decrypt_json）。
        """
        return None if value is None else get_cipher().decrypt_json(value)
