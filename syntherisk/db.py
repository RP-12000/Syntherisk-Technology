"""数据库连接。本地默认 SQLite，生产用 PostgreSQL，由 DATABASE_URL 决定。

这个文件负责什么
    1. 按连接串建 SQLAlchemy 的“引擎”和“会话工厂”（make_engine、make_session_factory）；
    2. 启动时建表、给旧数据库补上后来才加的列、在售目录为空时写入内置试剂（init_db）。
    表长什么样不在这里定义，在 models.py。它是 M0 应用服务的基础设施，不对应界面上的按钮。

几个 SQLAlchemy 的词
    Engine（引擎）   代表“怎么连这个数据库”，内部有连接池。整个进程建一个
    Session（会话）  干一件事用的工作区：查出来的行、要新增或修改的行都先记在它上面，commit() 时一起写进数据库，
                    出错可以 rollback() 全部撤销。每个请求、每个后台任务各用一个，用完 close()
    sessionmaker    会话工厂：调一下就得到一个新的 Session

谁调用它、它依赖谁
    只有 context.py 的 build_context() 调用 make_engine、make_session_factory、init_db。
    建好的会话工厂放在 ctx.session_factory 上，接口（api/deps.py 的 get_db）和后台任务都从那里拿会话。
    它依赖 models.py（表的定义）和 reference.py（内置常用试剂表）。

读写什么
    init_db 建 models.py 里定义的全部表；给 reaction_precedents 表补 center 列；
    向 catalog_compounds 表插入内置试剂表里可购买的物质。

本文件里有什么
    make_engine()            按连接串建引擎；SQLite 另外做几项设置
    make_session_factory()   建会话工厂
    session_scope()          “用完自动提交或回滚”的会话，配合 with 语句使用
    _ADDED_COLUMNS           建表之后才加进来的列的清单
    _add_missing_columns()   给旧数据库补上清单里缺的列
    init_db()                建表、补列、写入内置在售试剂；每次启动都调，重复调用不会出问题
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Engine, create_engine, event, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from syntherisk import models
from syntherisk.reference import ReferenceData


def make_engine(database_url: str) -> Engine:
    """按连接串建数据库引擎。SQLite 和别的数据库（PostgreSQL）各走一套设置。

    参数：
        database_url  SQLAlchemy 连接串，来自 Settings.resolved_database_url。例如
                        sqlite:///C:/…/var/syntherisk.db               本地文件
                        sqlite://（或者带 :memory:）                    内存数据库，进程结束就没了
                        postgresql+psycopg://用户:密码@主机:5432/库名    PostgreSQL
    返回：
        Engine。这时还没有真的连数据库，第一次用到时才连。
    SQLite 的几项特殊设置（原因见函数体里的注释）：
        允许跨线程使用同一个连接、等锁最多 30 秒、打开外键检查、文件库用 WAL 日志模式。
    谁在用：
        context.py 的 build_context()。
    """
    if database_url.startswith("sqlite"):
        # 内存库的两种写法：连接串里带 ":memory:"，或者干脆只有 "sqlite://"
        in_memory = ":memory:" in database_url or database_url.endswith("sqlite://")
        # connect_args 是原样交给 Python 自带的 sqlite3 模块的连接参数：
        #   check_same_thread=False  sqlite3 默认不许一个连接在创建它的线程以外使用；这里放开，
        #                            因为连接池里的连接会被不同的线程（处理请求的、跑后台任务的）轮流拿去用
        #   timeout=30               数据库正被别的连接锁着时（SQLite 同一时刻只让一个连接写），最多等 30 秒，
        #                            超过才报 “database is locked”；sqlite3 自己的默认值是 5 秒
        kwargs: dict = {"connect_args": {"check_same_thread": False, "timeout": 30}}
        if in_memory:
            from sqlalchemy.pool import StaticPool

            # 内存库只存在于“那一个连接”里，每开一个新连接得到的都是一个全新的空库。
            # StaticPool 让所有会话共用同一个连接，这样大家看到的才是同一个库
            kwargs["poolclass"] = StaticPool
        engine = create_engine(database_url, **kwargs)

        # event.listens_for(engine, "connect")：每当连接池真的新开一个数据库连接，就调一次下面这个函数。
        # PRAGMA 是 SQLite 特有的设置命令。外键检查这类设置只对当前连接有效，所以要在每个新连接上各设一遍
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):
            """新连接建好时设置 SQLite 的几个 PRAGMA。

            两个参数由 SQLAlchemy 传进来：dbapi_connection 是底层的 sqlite3 连接，_record 是连接池的记录（用不到）。
            """
            cursor = dbapi_connection.cursor()
            # SQLite 默认不检查外键，要显式打开。打开以后，比如 analyses.project_id 就必须指向真实存在的项目
            cursor.execute("PRAGMA foreign_keys=ON")
            if not in_memory:
                # WAL：后台任务写进度的同时，接口还能读
                # （WAL 是“预写日志”：改动先写进数据库文件旁边的 -wal 文件，读的人不会被写的人挡住。
                #   内存库不支持 WAL，所以只对文件库设置）
                cursor.execute("PRAGMA journal_mode=WAL")
                # synchronous=NORMAL：不是每次提交都强制把数据刷到磁盘，写得快一些。和 WAL 一起用时，
                # 突然断电可能丢掉最后几次提交，但数据库文件不会损坏
                cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

        return engine
    # 其他数据库（PostgreSQL）。pool_pre_ping=True：每次从连接池取连接之前先试一下它还通不通，
    # 断了就换一条新的。数据库重启过、或者连接闲置太久被断开以后，下一个请求不会撞上死连接
    return create_engine(database_url, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    """建会话工厂：之后每调一次 factory() 就得到一个连着这个引擎的新 Session。

    两个和 SQLAlchemy 默认值不一样的设置：
        expire_on_commit=False  默认情况下 commit() 之后，会话里的对象会被标成“过期”，再读它的属性要重新查库。
                                关掉以后，提交完还能直接用对象上已有的值
                                （接口里常常是先 commit，再把对象拼成返回值）。
        autoflush=False         默认情况下每次查询之前，会话会先把还没写的改动自动发给数据库（flush）。
                                关掉以后，什么时候发由代码自己决定：需要马上拿到新行的编号时显式调
                                session.flush()，否则等 commit()。
    谁在用：
        context.py 的 build_context()；结果放在 ctx.session_factory 上。
    """
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """开一个会话，用完自动收尾：with 块正常结束就提交，块里抛了异常就回滚，最后一定关闭。

    用法：
        with session_scope(factory) as session:
            session.add(...)        （不用自己写 commit）
    参数：
        factory  会话工厂（make_session_factory 的返回值）。
    产出：
        一个 Session，也就是 as 后面的那个变量。
    抛出：
        块里的异常在回滚之后原样继续往外抛。
    谁在用：
        现在只有本文件的 init_db()。接口用的是 api/deps.py 的 get_db()（不自动提交，接口自己 commit）；
        后台任务直接写 with ctx.session_factory() as session（同样要自己 commit）。
    """
    session = factory()
    try:
        # @contextmanager 的写法：yield 之前相当于“进入 with”，yield 出去的值就是 as 后面的变量；
        # with 块执行完（或者抛了异常）以后，从 yield 这里接着往下走
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# 表已经建好以后才加进来的列。create_all 只建新表、不改旧表，所以旧数据库要在这里补上
# 结构是 {表名: {列名: 列的 SQL 类型}}。现在只有一项：先例库的 center 列（反应中心，见 models.ReactionPrecedent）。
# 以后给已有的表加列，除了改 models.py，还要在这里登记一行。这里只管加列；改列的类型、删列它做不了
_ADDED_COLUMNS = {"reaction_precedents": {"center": "TEXT"}, "analyses": {"language": "VARCHAR(8)"}}


def _add_missing_columns(engine: Engine) -> None:
    """对照 _ADDED_COLUMNS，给数据库里已有的表补上缺的列。已经有的列不动，原有的数据不动。

    为什么需要：models.Base.metadata.create_all() 只会建“还不存在的表”，表已经在了就整张跳过，
    不会发现模型里多了一列。早先建的数据库因此缺列，查询时会报“没有这一列”。
    参数：
        engine  数据库引擎。
    副作用：
        对缺的每一列执行一条 ALTER TABLE … ADD COLUMN。原有的行在新列上是空值（NULL）。
    谁在用：
        本文件的 init_db()。对应的测试是 tests/test_integrations.py 的
        test_a_database_created_before_a_column_was_added_gets_it_on_startup。
    """
    # inspect(engine) 得到一个“检查器”，可以问数据库现在实际有哪些表、每张表有哪些列
    inspector = inspect(engine)
    for table, columns in _ADDED_COLUMNS.items():
        # 表不存在就跳过。init_db 是先 create_all 再调这里的，所以正常情况下表一定在，这个判断只是保险
        if not inspector.has_table(table):
            continue
        existing = {column["name"] for column in inspector.get_columns(table)}
        # engine.begin() 开一个事务，with 块正常结束时自动提交
        with engine.begin() as connection:
            for name, kind in columns.items():
                if name not in existing:
                    # text() 把一段原始 SQL 包成可以执行的语句。
                    # 表名、列名、类型都来自上面写死的清单，不含任何用户输入
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {kind}"))


def init_db(engine: Engine, factory: sessionmaker[Session], reference: ReferenceData) -> None:
    """建表，并把内置常用试剂表里能买到的物质写进在售目录（只在目录为空时做一次）。

    每次进程启动都会调用。三件事都可以重复做而不出问题：
        1. create_all：按 models.py 建还不存在的表，已经有的表不动；
        2. _add_missing_columns：给旧库补上后来加的列；
        3. 在售目录 catalog_compounds 一行都没有时，把 data/compounds.csv 里 purchasable 为 1 的物质写进去，
           source 记为 "seed"。目录里已经有数据（哪怕只是用 load-catalog 导入的）就不再写。
    参数：
        engine     数据库引擎。
        factory    会话工厂。
        reference  参考数据；这里只用 reference.compounds.compounds（内置常用试剂的列表）。
    副作用：
        建表、改表、插入数据并提交。
    注意：
        因为第 3 件事只在目录为空时做，以后往 compounds.csv 里新加的可购买试剂不会自动进 catalog_compounds 表。
    谁在用：
        context.py 的 build_context()。
    """
    # metadata 记着 models.py 里所有继承 Base 的表；create_all 对每张表发 CREATE TABLE（已经存在的跳过）
    models.Base.metadata.create_all(engine)
    _add_missing_columns(engine)
    with session_scope(factory) as session:
        # select(某一列).limit(1) 只取一行的一个值；session.scalar() 返回这个值，一行都没有时返回 None。
        # 所以这句是在问“在售目录里有没有任何数据”：有就直接返回，什么都不写
        if session.scalar(select(models.CatalogCompound.key14).limit(1)) is not None:
            return
        # seen 用来去重：key14 是这张表的主键，不能重复。compounds.csv 里如果有两行 key14 相同
        # （同一个结构；只是立体构型不同也算相同），只写第一行
        seen: set[str] = set()
        for compound in reference.compounds.compounds:
            if compound.purchasable and compound.key14 not in seen:
                seen.add(compound.key14)
                session.add(
                    models.CatalogCompound(
                        key14=compound.key14, smiles=compound.smiles, name=compound.display_name, source="seed"
                    )
                )
        # 这里不用写 commit：退出 with 块时 session_scope 会提交
