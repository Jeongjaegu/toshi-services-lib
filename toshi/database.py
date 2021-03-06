import asyncio
import asyncpg
import os
import sys
import ssl
from collections import ItemsView
from toshi.config import config
from toshi.errors import DatabaseError
from toshi.log import log

if hasattr(asyncpg.pool.Pool, '_acquire_impl'):
    # pre 0.12.0 version
    class SafePool(asyncpg.pool.Pool):
        """changes the connection acquire implementation to deal with connections
        disconnecting when not in use"""

        async def _acquire_impl(self):
            while True:
                con = await super(SafePool, self)._acquire_impl()
                if con.is_closed():
                    await self.release(con)
                else:
                    return con
else:
    # 0.12.0 version
    class SafePool(asyncpg.pool.Pool):
        """changes the connection acquire implementation to deal with connections
        disconnecting when not in use"""

        async def _acquire(self, timeout):
            while True:
                con = await super(SafePool, self)._acquire(timeout)
                if con.is_closed():
                    await self.release(con)
                else:
                    return con

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

def create_pool(dsn=None, *,
                min_size=10,
                max_size=10,
                max_queries=50000,
                max_inactive_connection_lifetime=300.0,
                setup=None,
                loop=None,
                init=None,
                ssl=None,
                connection_class=asyncpg.connection.Connection,
                **connect_kwargs):
    try:
        # check for 0.11.0 support
        if '_connection_class' in asyncpg.pool.Pool.__slots__:
            connect_kwargs['connection_class'] = connection_class
        # check for 0.10.0 support
        from asyncpg.pool import PoolConnectionHolder
        connect_kwargs['max_inactive_connection_lifetime'] = max_inactive_connection_lifetime
        connect_kwargs['init'] = init
    except:
        # check for 0.9.0 support
        if '_init' in asyncpg.pool.Pool.__slots__:
            connect_kwargs['init'] = init
    # handle input from ConfigParser
    if isinstance(min_size, str):
        min_size = int(min_size)
    if isinstance(max_size, str):
        max_size = int(max_size)
    if min_size > max_size:
        min_size = max_size
    if ssl:
        if ssl is True:
            ssl = SSL_CTX
        connect_kwargs['ssl'] = ssl
    return SafePool(dsn,
                    min_size=min_size, max_size=max_size,
                    max_queries=max_queries, loop=loop, setup=setup,
                    **connect_kwargs)

def get_database_pool():
    assert _global_database_pool is not None, "database not prepared before use"
    return _global_database_pool

def set_database_pool(connection):
    global _global_database_pool
    _global_database_pool = connection

_global_database_pool = None

async def _prepare_global_pool():
    global _global_database_pool
    if _global_database_pool is None:
        dbconfig = dict(config['database'])
        dbconfig.pop('ssl', None)
        ssl = config['database'].getboolean('ssl')
        _global_database_pool = await create_pool(ssl=ssl, **dbconfig)
    return _global_database_pool

async def prepare_database(config=None, handle_migration=None):
    """If handle_migration is False, will instead wait until the database's
    version matches the expected"""

    if config is None:
        pool = await _prepare_global_pool()
    else:
        pool = await create_pool(**config)
    async with pool.acquire() as con:
        if handle_migration is True or (config is None and handle_migration is None):
            await create_tables(con)
        else:
            await wait_for_migration(con)

    return pool

async def create_tables(con):

    # make sure the create tables script exists
    if not os.path.exists("sql/create_tables.sql"):
        log.warning("Missing sql/create_tables.sql: cannot initialise database")
        return

    try:
        row = await con.fetchrow("SELECT version_number FROM database_version LIMIT 1")
        version = row['version_number']
        log.info("got database version: {}".format(version))
    except asyncpg.exceptions.UndefinedTableError:

        # fresh DB path

        await con.execute("CREATE TABLE database_version (version_number INTEGER)")
        await con.execute("INSERT INTO database_version (version_number) VALUES (0)")

        # fresh database, nothing to migrate
        with open("sql/create_tables.sql") as create_tables_file:

            sql = create_tables_file.read()

            await con.execute(sql)

        # verify that if there are any migration scripts, that the
        # database_version table has been updated appropriately
        version = 0
        while True:
            version += 1
            if not os.path.exists("sql/migrate_{:08}.sql".format(version)):
                version -= 1
                break

        if version > 0:
            row = await con.fetchrow("SELECT version_number FROM database_version LIMIT 1")
            if row['version_number'] != version:
                log.warning("Warning, migration scripts exist but database version has not been set in create_tables.sql")
                log.warning("DB version: {}, latest migration script: {}".format(row['version_number'], version))

        return

    # check for migration files

    exception = None
    while True:
        version += 1

        fn = "sql/migrate_{:08}.sql".format(version)
        if os.path.exists(fn):
            log.info("applying migration script: {:08}".format(version))
            with open(fn) as migrate_file:
                sql = migrate_file.read()
                try:
                    await con.execute(sql)
                except Exception as e:
                    version -= 1
                    exception = e
                    break
        else:
            version -= 1
            break

    await con.execute("UPDATE database_version SET version_number = $1", version)
    if exception:
        raise exception

async def wait_for_migration(con, poll_frequency=1):
    """finds the latest expected database version and only exits once the current
    version in the database matches. Use for sub processes that depend on a main
    process handling database migration"""

    if not os.path.exists("sql/create_tables.sql"):
        log.warning("Missing sql/create_tables.sql: cannot initialise database")
        return

    version = 0
    while True:
        version += 1
        if not os.path.exists("sql/migrate_{:08}.sql".format(version)):
            version -= 1
            break

    while True:
        try:
            row = await con.fetchrow("SELECT version_number FROM database_version LIMIT 1")
            if version == row['version_number']:
                break
        except asyncpg.exceptions.UndefinedTableError:
            # if this happens, it could just be the first time starting the app,
            # just keep waiting
            pass
        log.info("waiting for database migration...".format(version))
        # wait some time before checking again
        await asyncio.sleep(poll_frequency)
    # done!
    log.info("got database version: {}".format(version))
    return

class HandlerDatabasePoolContext():

    __slots__ = ('timeout', 'connection', 'transaction', 'autocommit', 'pool', 'done', 'callbacks')

    def __init__(self, pool, autocommit=False, timeout=None):
        self.pool = pool
        self.timeout = timeout
        self.autocommit = autocommit
        self.connection = None
        self.transaction = None
        self.done = False
        self.callbacks = []

    def acquire(self, autocommit=None):
        """creates a new context with the values of this one"""
        if autocommit is None:
            autocommit = self.autocommit
        return HandlerDatabasePoolContext(self.pool, autocommit, self.timeout)

    async def __aenter__(self):
        if self.connection is not None:
            raise DatabaseError("Connection already in progress")
        try:
            self.connection = await self.pool.acquire(timeout=self.timeout)
        except asyncpg.exceptions.ConnectionDoesNotExistError:
            log.exception("Error acquiring connection")
            # attempt to recover the database connection
            if self.pool is get_database_pool():
                set_database_pool(None)
                try:
                    await self.pool.close()
                    self.pool = await prepare_database(handle_migration=False)
                    self.connection = await self.pool.acquire(timeout=self.timeout)
                except:
                    log.exception("Unable to recover global database pool")
                    # fail hard in the hope that restarting the system will fix things
                    sys.exit(1)
            else:
                raise
        self.transaction = self.connection.transaction()
        await self.transaction.start()
        return self

    async def __aexit__(self, extype, ex, tb):
        try:
            if self.transaction:
                if extype is not None or self.autocommit is False:
                    await self.transaction.rollback()
                elif self.autocommit:
                    await self.commit()
        finally:
            con = self.connection
            self.transaction = None
            self.connection = None
            self.done = True
            await self.pool.release(con)

    async def commit(self, create_new_transaction=False):
        if self.transaction:
            try:
                callbacks = self.callbacks[:]
                self.callbacks.clear()
                rval = await self.transaction.commit()
                for callback in callbacks:
                    f = callback()
                    if asyncio.iscoroutine(f):
                        await f
                return rval
            finally:
                if create_new_transaction:
                    self.transaction = self.connection.transaction()
                    await self.transaction.start()
                else:
                    self.done = True
                    self.transaction = None
        else:
            raise DatabaseError("No transaction to commit")

    def on_commit(self, callback):
        """used to trigger functions on commit"""
        if callback not in self.callbacks:
            self.callbacks.append(callback)

    def execute(self, query: str, *args, timeout: float=None) -> str:
        if self.transaction:
            return self.connection.execute(query, *args, timeout=timeout)
        else:
            raise DatabaseError("No transaction in progress")

    def executemany(self, command: str, args, *, timeout: float=None):
        if self.transaction:
            return self.connection.executemany(command, args, timeout=timeout)
        else:
            raise DatabaseError("No transaction in progress")

    def fetch(self, query, *args, timeout=None):
        if self.transaction:
            return self.connection.fetch(query, *args, timeout=timeout)
        else:
            raise DatabaseError("No transaction in progress")

    def fetchval(self, query, *args, column=0, timeout=None):
        if self.transaction:
            return self.connection.fetchval(query, *args, column=column, timeout=timeout)
        else:
            raise DatabaseError("No transaction in progress")

    def fetchrow(self, query, *args, timeout=None):
        if self.transaction:
            return self.connection.fetchrow(query, *args, timeout=timeout)
        else:
            raise DatabaseError("No transaction in progress")

    async def update(self, tablename, update_args, query_args=None):
        """Very simple "generic" update helper.
        will generate the update statement, converting the `update_args`
        dict into "key = value, key = value" statements, and converting
        the `query_args` dict into "key = value AND key = value"
        statements. string values will be wrapped in 'quotes', while
        other types will be left as their python representation.
        """

        if not self.transaction:
            raise DatabaseError("No transaction in progress")

        query = "UPDATE {} SET ".format(tablename)
        arglist = []
        qnum = 1
        if isinstance(update_args, dict):
            update_args = update_args.items()
        if isinstance(update_args, (list, tuple, ItemsView)):
            setstmts = []
            for k, v in update_args:
                setstmts.append("{} = ${}".format(k, qnum))
                qnum += 1
                arglist.append(v)
            query += ', '.join(setstmts)
        else:
            raise DatabaseError("expected dict or list for update_args")
        if isinstance(query_args, dict):
            query_args = query_args.items()
        if isinstance(query_args, (list, tuple, ItemsView)):
            query += " WHERE "
            wherestmts = []
            # TODO: support OR somehow?
            for k, v in query_args:
                wherestmts.append("{} = ${}".format(k, qnum))
                qnum += 1
                arglist.append(v)
            query += ' AND '.join(wherestmts)
        elif query_args is not None:
            raise DatabaseError("expected dict or list or None for query_args")

        resp = await self.connection.execute(query, *arglist)

        if resp and resp[0].startswith("ERROR:"):
            raise DatabaseError(resp)
        return resp

def with_database(fn):
    async def wrapper(self, *args, **kwargs):
        async with self.db:
            r = fn(self, *args, **kwargs)
            if asyncio.iscoroutine(r):
                r = await r
            return r
    return wrapper

class DatabaseMixin:
    @property
    def db(self):
        if not hasattr(self, '_dbcontext'):
            self._dbcontext = HandlerDatabasePoolContext(get_database_pool())
        return self._dbcontext
