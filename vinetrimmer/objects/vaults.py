import os
import json
import sqlite3
import logging
import traceback
import requests
from enum import Enum

import pymysql

from vinetrimmer.utils.AtomicSQL import AtomicSQL
from vinetrimmer.utils.collections import first_or_none


log = logging.getLogger("Vaults")

class InsertResult(Enum):
    FAILURE = 0
    SUCCESS = 1
    ALREADY_EXISTS = 2


class Vault:
    """
    Key Vault.
    This defines various details about the vault, including its Connection object.
    """

    def __init__(
        self,
        type_,
        name,
        ticket=None,
        path=None,
        username=None,
        password=None,
        database=None,
        host=None,
        port=3306,
    ):
        from vinetrimmer.config import directories

        try:
            self.type = self.Types[type_.upper()]
        except KeyError:
            raise ValueError(f"Invalid vault type [{type_}]")
        self.name = name
        self.con = None
        if self.type == Vault.Types.LOCAL:
            if not path:
                raise ValueError("Local vault has no path specified")
            self.con = sqlite3.connect(
                os.path.expanduser(path).format(data_dir=directories.data)
            )
        elif self.type == Vault.Types.REMOTE:
            self.con = pymysql.connect(
                user=username,
                password=password or "",
                db=database,
                host=host,
                port=port,
                cursorclass=pymysql.cursors.DictCursor,  # TODO: Needed? Maybe use it on sqlite3 too?
            )
        elif self.type == Vault.Types.HTTP:
            self.url = host
            self.username = username
            self.password = password
        elif self.type == Vault.Types.HTTPAPI:
            self.url = host
            self.password = password
        else:
            raise ValueError(f"Invalid vault type [{self.type.name}]")
        self.ph = {
            self.Types.LOCAL: "?",
            self.Types.REMOTE: "%s",
            self.Types.HTTP: None,
            self.Types.HTTPAPI: None,
        }[self.type]
        self.ticket = ticket

        self.perms = self.get_permissions()
        if not self.has_permission("SELECT"):
            raise ValueError(
                f"Cannot use vault. Vault {self.name} has no SELECT permission."
            )

    def __str__(self):
        return f"{self.name} ({self.type.name})"

    def get_permissions(self):
        if self.type == self.Types.LOCAL:
            return [tuple([["*"], tuple(["*", "*"])])]
        elif self.type == self.Types.HTTP:
            return [tuple([["*"], tuple(["*", "*"])])]
        elif self.type == self.Types.HTTPAPI:
            return [tuple([["*"], tuple(["*", "*"])])]

        with self.con.cursor() as c:
            c.execute("SHOW GRANTS")
            grants = c.fetchall()
            grants = [next(iter(x.values())) for x in grants]
        grants = [tuple(x[6:].split(" TO ")[0].split(" ON ")) for x in list(grants)]
        grants = [
            (
                list(map(str.strip, perms.replace("ALL PRIVILEGES", "*").split(","))),
                location.replace("`", "").split("."),
            )
            for perms, location in grants
        ]

        return grants

    def has_permission(self, operation, database=None, table=None):
        grants = [x for x in self.perms if x[0] == ["*"] or operation.upper() in x[0]]
        if grants and database:
            grants = [x for x in grants if x[1][0] in (database, "*")]
        if grants and table:
            grants = [x for x in grants if x[1][1] in (table, "*")]
        return bool(grants)

    class Types(Enum):
        LOCAL = 1
        REMOTE = 2
        HTTP = 3
        HTTPAPI = 4


class Vaults:
    """
    Key Vaults.
    Keeps hold of Vault objects, with convenience functions for
    using multiple vaults in one actions, e.g. searching vaults
    for a key based on kid.
    This object uses AtomicSQL for accessing the vault connections
    instead of directly. This is to provide thread safety but isn't
    strictly necessary.
    """

    def __init__(self, vaults, service):
        self.adb = AtomicSQL()
        self.api_session_id = None
        self.vaults = sorted(
            vaults, key=lambda v: 0 if v.type is Vault.Types.LOCAL else 1
        )
        self.service = service.lower()
        for vault in self.vaults:
            if vault.type is Vault.Types.HTTP or vault.type is Vault.Types.HTTPAPI:
                continue

            vault.ticket = self.adb.load(vault.con)
            self.create_table(vault, self.service, commit=True)

    def request(self, method, url, key, params=None):
        r = requests.post(
            url,
            json={
                "method": method,
                "params": {
                    **(params or {}),
                    "session_id": self.api_session_id,
                },
                "token": key,
            },
        )

        if not r.ok:
            raise ValueError(f"API returned HTTP Error {r.status_code}: {r.reason.title()}")

        try:
            res = r.json()
        except json.JSONDecodeError:
            raise ValueError(f"API returned an invalid response: {r.text}")

        if res.get("status_code") != 200:
            raise ValueError(f"API returned an error: {res['status_code']} - {res['message']}")

        if session_id := res["message"].get("session_id"):
            self.api_session_id = session_id

        return res["message"]

    def __iter__(self):
        return iter(self.vaults)

    def get(self, kid, title):
        for vault in self.vaults:
            if vault.type is Vault.Types.HTTP:
                keys = requests.get(
                    vault.url,
                    params={
                        "service": self.service,
                        "username": vault.username,
                        "password": vault.password,
                        "kid": kid,
                    },
                ).json()

                if keys["keys"]:
                    return keys["keys"][0]["key"], vault
            elif vault.type is Vault.Types.HTTPAPI:
                try:
                    keys = self.request(
                        "GetKey",
                        vault.url,
                        vault.password,
                        {
                            "kid": kid,
                            "service": self.service,
                            "title": title,
                        },
                    ).get("keys", [])
                    return first_or_none(x["key"] for x in keys if x["kid"] == kid), vault
                except (Exception, SystemExit) as e:
                    # TODO: We currently need to catch SystemExit because of log.exit, this should be refactored
                    log.debug(traceback.format_exc())
                    if not isinstance(e, SystemExit):
                        log.error(f"Failed to get key ({e.__class__.__name__}: {e}")
                    return None
            else:
                # Note on why it matches by KID instead of PSSH:
                # Matching cache by pssh is not efficient. The PSSH can be made differently by all different
                # clients for all different reasons, e.g. only having the init data, but the cached PSSH is
                # a manually crafted PSSH, which may not match other clients manually crafted PSSH, and such.
                # So it searches by KID instead for this reason, as the KID has no possibility of being different
                # client to client other than capitalization. There is an unknown with KID matching, It's unknown
                # for *sure* if the KIDs ever conflict or not with another bitrate/stream/title. I haven't seen
                # this happen ever and neither has anyone I have asked.
                if not self.table_exists(vault, self.service):
                    continue  # this service has no service table, so no keys, just skip
                if not vault.ticket:
                    raise ValueError(
                        f"Vault {vault.name} does not have a valid ticket available."
                    )
                c = self.adb.safe_execute(
                    vault.ticket,
                    lambda db, cursor: cursor.execute(
                        "SELECT `id`, `key_`, `title` FROM `{1}` WHERE `kid`={0}".format(
                            vault.ph, self.service
                        ),
                        [kid],
                    ),
                ).fetchone()
                if c:
                    if isinstance(c, dict):
                        c = list(c.values())
                    if not c[2] and vault.has_permission("UPDATE", table=self.service):
                        self.adb.safe_execute(
                            vault.ticket,
                            lambda db, cursor: cursor.execute(
                                "UPDATE `{1}` SET `title`={0} WHERE `id`={0}".format(
                                    vault.ph, self.service
                                ),
                                [title, c[0]],
                            ),
                        )
                        self.commit(vault)
                    return c[1], vault
        return None, None

    def table_exists(self, vault, table):
        if vault.type == Vault.Types.HTTP:
            return True
        if not vault.ticket:
            raise ValueError(
                f"Vault {vault.name} does not have a valid ticket available."
            )
        if vault.type == Vault.Types.LOCAL:
            return (
                self.adb.safe_execute(
                    vault.ticket,
                    lambda db, cursor: cursor.execute(
                        f"SELECT count(name) FROM sqlite_master WHERE type='table' AND name={vault.ph}",
                        [table],
                    ),
                ).fetchone()[0]
                == 1
            )
        return (
            list(
                self.adb.safe_execute(
                    vault.ticket,
                    lambda db, cursor: cursor.execute(
                        f"SELECT count(TABLE_NAME) FROM information_schema.TABLES WHERE TABLE_NAME={vault.ph}",
                        [table],
                    ),
                )
                .fetchone()
                .values()
            )[0]
            == 1
        )

    def create_table(self, vault, table, commit=False):
        if self.table_exists(vault, table):
            return
        if not vault.ticket:
            raise ValueError(
                f"Vault {vault.name} does not have a valid ticket available."
            )
        if vault.has_permission("CREATE"):
            print(f"Creating `{table}` table in {vault} key vault...")
            self.adb.safe_execute(
                vault.ticket,
                lambda db, cursor: cursor.execute(
                    "CREATE TABLE IF NOT EXISTS {} (".format(table)
                    + (
                        """
                        "id"        INTEGER NOT NULL UNIQUE,
                        "kid"       TEXT NOT NULL COLLATE NOCASE,
                        "key_"      TEXT NOT NULL COLLATE NOCASE,
                        "title"     TEXT,
                        PRIMARY KEY("id" AUTOINCREMENT),
                        UNIQUE("kid", "key_")
                        """
                        if vault.type == Vault.Types.LOCAL
                        else """
                        id          INTEGER AUTO_INCREMENT PRIMARY KEY,
                        kid         VARCHAR(255) NOT NULL,
                        key_        VARCHAR(255) NOT NULL,
                        title       TEXT,
                        UNIQUE(kid, key_)
                        """
                    )
                    + ");"
                ),
            )
            if commit:
                self.commit(vault)

    def insert_key(self, vault, table, kid, key, title, commit=False):
        if vault.type == Vault.Types.HTTP:
            keys = requests.get(
                        vault.url,
                        params={
                            "service": self.service,
                            "username": vault.username,
                            "password": vault.password,
                            "kid": kid,
                            "key": key,
                            "title": title
                        },
                    ).json()
            if keys["status_code"] == 200 and keys["inserted"]:
                return InsertResult.SUCCESS
            elif keys["status_code"] == 200:
                return InsertResult.ALREADY_EXISTS
            else:
                return InsertResult.FAILURE
        elif vault.type is Vault.Types.HTTPAPI:
            res = self.request(
                "InsertKey",
                vault.url,
                vault.password,
                {
                    "kid": kid,
                    "key": key,
                    "service": self.service,
                    "title": title,
                },
            )["inserted"]
            if res:
                return InsertResult.SUCCESS
       
            else:
                return InsertResult.FAILURE
        if not self.table_exists(vault, table):
            return InsertResult.FAILURE
        if not vault.ticket:
            raise ValueError(
                f"Vault {vault.name} does not have a valid ticket available."
            )
        if not vault.has_permission("INSERT", table=table):
            raise ValueError(
                f"Cannot insert key into Vault. Vault {vault.name} has no INSERT permission."
            )
        if self.adb.safe_execute(
            vault.ticket,
            lambda db, cursor: cursor.execute(
                "SELECT `id` FROM `{1}` WHERE `kid`={0} AND `key_`={0}".format(
                    vault.ph, self.service
                ),
                [kid, key],
            ),
        ).fetchone():
            return InsertResult.ALREADY_EXISTS
        self.adb.safe_execute(
            vault.ticket,
            lambda db, cursor: cursor.execute(
                "INSERT INTO `{1}` (kid, key_, title) VALUES ({0}, {0}, {0})".format(
                    vault.ph, table
                ),
                (kid, key, title),
            ),
        )
        if commit:
            self.commit(vault)
        return InsertResult.SUCCESS

    def commit(self, vault):
        if vault.type == Vault.Types.HTTP or vault.type == Vault.Types.HTTPAPI:
            return
        self.adb.commit(vault.ticket)
