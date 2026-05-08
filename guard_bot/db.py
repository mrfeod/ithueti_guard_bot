import aiosqlite


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA foreign_keys = ON")
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("database is not connected")
        return self._db

    async def migrate(self) -> None:
        await self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS registered_users (
                user_id INTEGER PRIMARY KEY,
                registered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                source TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS registered_usernames (
                username TEXT PRIMARY KEY,
                registered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                source TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS seen_users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS pending_challenges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                original_message_id INTEGER NOT NULL,
                challenge_message_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS banned_users (
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                banned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                reason TEXT NOT NULL,
                PRIMARY KEY (user_id, chat_id)
            );

            CREATE TABLE IF NOT EXISTS bot_admins (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS bot_moderators (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS bot_moderator_usernames (
                username TEXT PRIMARY KEY,
                added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS admin_message_links (
                admin_id INTEGER NOT NULL,
                admin_message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_message_id INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (admin_id, admin_message_id)
            );

            CREATE TABLE IF NOT EXISTS ignored_users (
                user_id INTEGER PRIMARY KEY,
                ignored_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        columns = await self.get_table_columns("admin_message_links")
        if "user_message_id" not in columns:
            await self.conn.execute(
                "ALTER TABLE admin_message_links ADD COLUMN user_message_id INTEGER NOT NULL DEFAULT 0"
            )
        await self.conn.commit()

    async def get_table_columns(self, table_name: str) -> set[str]:
        cursor = await self.conn.execute(f"PRAGMA table_info({table_name})")
        rows = await cursor.fetchall()
        return {str(row["name"]) for row in rows}

    async def upsert_seen_user(
        self,
        user_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO seen_users (user_id, username, first_name, last_name)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, username, first_name, last_name),
        )
        await self.conn.commit()

    async def upsert_seen_chat(
        self,
        chat_id: int,
        username: str | None,
        title: str | None,
        chat_type: str | None,
    ) -> None:
        await self.upsert_seen_user(
            user_id=chat_id,
            username=username,
            first_name=title,
            last_name=chat_type,
        )

    async def is_registered(self, user_id: int) -> bool:
        cursor = await self.conn.execute(
            "SELECT 1 FROM registered_users WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        return row is not None

    async def register_user(self, user_id: int, source: str) -> None:
        await self.conn.execute(
            """
            INSERT INTO registered_users (user_id, source)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET source = excluded.source
            """,
            (user_id, source),
        )
        await self.conn.commit()

    async def unregister_user(self, user_id: int) -> bool:
        cursor = await self.conn.execute(
            "DELETE FROM registered_users WHERE user_id = ?",
            (user_id,),
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def register_username(self, username: str, source: str) -> None:
        normalized = self.normalize_username(username)
        await self.conn.execute(
            """
            INSERT INTO registered_usernames (username, source)
            VALUES (?, ?)
            ON CONFLICT(username) DO UPDATE SET source = excluded.source
            """,
            (normalized, source),
        )
        await self.conn.commit()

    async def unregister_username(self, username: str) -> bool:
        normalized = self.normalize_username(username)
        cursor = await self.conn.execute(
            "DELETE FROM registered_usernames WHERE username = ?",
            (normalized,),
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def is_registered_username(self, username: str | None) -> bool:
        normalized = self.normalize_username(username)
        if not normalized:
            return False

        cursor = await self.conn.execute(
            "SELECT 1 FROM registered_usernames WHERE username = ?",
            (normalized,),
        )
        row = await cursor.fetchone()
        return row is not None

    async def is_moderator_username(self, username: str | None) -> bool:
        normalized = self.normalize_username(username)
        if not normalized:
            return False

        cursor = await self.conn.execute(
            "SELECT 1 FROM bot_moderator_usernames WHERE username = ?",
            (normalized,),
        )
        row = await cursor.fetchone()
        return row is not None

    async def add_challenge(
        self,
        chat_id: int,
        user_id: int,
        original_message_id: int,
        challenge_message_id: int,
    ) -> int:
        cursor = await self.conn.execute(
            """
            INSERT INTO pending_challenges (
                chat_id, user_id, original_message_id, challenge_message_id
            )
            VALUES (?, ?, ?, ?)
            """,
            (chat_id, user_id, original_message_id, challenge_message_id),
        )
        await self.conn.commit()
        return int(cursor.lastrowid)

    async def add_challenge_original_message(
        self,
        chat_id: int,
        user_id: int,
        original_message_id: int,
        challenge_message_id: int,
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO pending_challenges (
                chat_id, user_id, original_message_id, challenge_message_id
            )
            VALUES (?, ?, ?, ?)
            """,
            (chat_id, user_id, original_message_id, challenge_message_id),
        )
        await self.conn.commit()

    async def get_challenge(self, challenge_id: int) -> aiosqlite.Row | None:
        cursor = await self.conn.execute(
            "SELECT * FROM pending_challenges WHERE id = ?",
            (challenge_id,),
        )
        return await cursor.fetchone()

    async def get_user_challenges(self, chat_id: int, user_id: int) -> list[aiosqlite.Row]:
        cursor = await self.conn.execute(
            "SELECT * FROM pending_challenges WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        return await cursor.fetchall()

    async def delete_challenge(self, challenge_id: int) -> None:
        await self.conn.execute("DELETE FROM pending_challenges WHERE id = ?", (challenge_id,))
        await self.conn.commit()

    async def delete_user_challenges(self, chat_id: int, user_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM pending_challenges WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        await self.conn.commit()

    async def mark_banned(self, user_id: int, chat_id: int, reason: str) -> None:
        await self.conn.execute(
            """
            INSERT INTO banned_users (user_id, chat_id, reason)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, chat_id) DO UPDATE SET
                banned_at = CURRENT_TIMESTAMP,
                reason = excluded.reason
            """,
            (user_id, chat_id, reason),
        )
        await self.conn.commit()

    async def get_user_bans(self, user_id: int) -> list[aiosqlite.Row]:
        cursor = await self.conn.execute(
            "SELECT * FROM banned_users WHERE user_id = ?",
            (user_id,),
        )
        return await cursor.fetchall()

    async def get_banned_users(self, limit: int | None = None) -> list[aiosqlite.Row]:
        cursor = await self.conn.execute(
            """
            SELECT
                b.user_id,
                s.username,
                s.first_name,
                GROUP_CONCAT(b.chat_id || ':' || b.reason, ', ') AS bans,
                MAX(b.banned_at) AS latest_banned_at
            FROM banned_users b
            LEFT JOIN seen_users s ON s.user_id = b.user_id
            GROUP BY b.user_id, s.username, s.first_name
            ORDER BY latest_banned_at DESC, lower(s.username), b.user_id
            LIMIT COALESCE(?, -1)
            """,
            (limit,),
        )
        return await cursor.fetchall()

    async def get_username(self, user_id: int) -> str | None:
        cursor = await self.conn.execute(
            "SELECT username FROM seen_users WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return row["username"]

    async def get_display_name(self, user_id: int) -> str | None:
        cursor = await self.conn.execute(
            """
            SELECT username, first_name
            FROM seen_users
            WHERE user_id = ?
            """,
            (user_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        if row["username"]:
            return f"@{row['username']}"
        if row["first_name"]:
            return str(row["first_name"])
        return None

    async def get_user_id_by_username(self, username: str) -> int | None:
        normalized = self.normalize_username(username)
        if not normalized:
            return None

        cursor = await self.conn.execute(
            "SELECT user_id FROM seen_users WHERE lower(username) = ?",
            (normalized,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return int(row["user_id"])

    async def get_seen_users(self) -> list[aiosqlite.Row]:
        cursor = await self.conn.execute(
            """
            SELECT user_id, username, first_name, last_name
            FROM seen_users
            ORDER BY updated_at DESC, lower(username), user_id
            """
        )
        return await cursor.fetchall()

    async def clear_user_bans(self, user_id: int) -> None:
        await self.conn.execute("DELETE FROM banned_users WHERE user_id = ?", (user_id,))
        await self.conn.commit()

    async def clear_user_ban(self, user_id: int, chat_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM banned_users WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id),
        )
        await self.conn.commit()

    async def add_admin(self, user_id: int, username: str | None) -> None:
        await self.conn.execute(
            """
            INSERT INTO bot_admins (user_id, username)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET username = excluded.username
            """,
            (user_id, username),
        )
        await self.conn.commit()

    async def is_admin(self, user_id: int) -> bool:
        cursor = await self.conn.execute(
            "SELECT 1 FROM bot_admins WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        return row is not None

    async def get_admin_ids(self) -> list[int]:
        cursor = await self.conn.execute("SELECT user_id FROM bot_admins")
        rows = await cursor.fetchall()
        return [int(row["user_id"]) for row in rows]

    async def add_moderator(self, user_id: int, username: str | None) -> None:
        await self.conn.execute(
            """
            INSERT INTO bot_moderators (user_id, username)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                added_at = CURRENT_TIMESTAMP
            """,
            (user_id, username),
        )
        await self.conn.commit()

    async def add_moderator_username(self, username: str) -> None:
        normalized = self.normalize_username(username)
        await self.conn.execute(
            """
            INSERT INTO bot_moderator_usernames (username)
            VALUES (?)
            ON CONFLICT(username) DO UPDATE SET added_at = CURRENT_TIMESTAMP
            """,
            (normalized,),
        )
        await self.conn.commit()

    async def remove_moderator_username(self, username: str) -> bool:
        normalized = self.normalize_username(username)
        cursor = await self.conn.execute(
            "DELETE FROM bot_moderator_usernames WHERE username = ?",
            (normalized,),
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def remove_moderator(self, user_id: int) -> bool:
        cursor = await self.conn.execute(
            "DELETE FROM bot_moderators WHERE user_id = ?",
            (user_id,),
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def is_moderator(self, user_id: int) -> bool:
        cursor = await self.conn.execute(
            "SELECT 1 FROM bot_moderators WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        return row is not None

    async def get_moderators(self, limit: int | None = None) -> list[aiosqlite.Row]:
        cursor = await self.conn.execute(
            """
            SELECT *
            FROM (
                SELECT
                    m.user_id,
                    COALESCE(s.username, m.username) AS username,
                    'user' AS source,
                    m.added_at
                FROM bot_moderators m
                LEFT JOIN seen_users s ON s.user_id = m.user_id
                UNION ALL
                SELECT
                    NULL AS user_id,
                    username,
                    'username' AS source,
                    added_at
                FROM bot_moderator_usernames
            )
            ORDER BY added_at DESC, username COLLATE NOCASE, user_id
            LIMIT COALESCE(?, -1)
            """,
            (limit,),
        )
        return await cursor.fetchall()

    async def add_admin_message_link(
        self,
        admin_id: int,
        admin_message_id: int,
        user_id: int,
        user_message_id: int,
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO admin_message_links (
                admin_id, admin_message_id, user_id, user_message_id
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(admin_id, admin_message_id) DO UPDATE SET
                user_id = excluded.user_id,
                user_message_id = excluded.user_message_id,
                created_at = CURRENT_TIMESTAMP
            """,
            (admin_id, admin_message_id, user_id, user_message_id),
        )
        await self.conn.commit()

    async def get_admin_message_link_user_id(
        self,
        admin_id: int,
        admin_message_id: int,
    ) -> int | None:
        cursor = await self.conn.execute(
            """
            SELECT user_id
            FROM admin_message_links
            WHERE admin_id = ? AND admin_message_id = ?
            """,
            (admin_id, admin_message_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return int(row["user_id"])

    async def get_admin_message_link(
        self,
        admin_id: int,
        admin_message_id: int,
    ) -> aiosqlite.Row | None:
        cursor = await self.conn.execute(
            """
            SELECT user_id, user_message_id
            FROM admin_message_links
            WHERE admin_id = ? AND admin_message_id = ?
            """,
            (admin_id, admin_message_id),
        )
        return await cursor.fetchone()

    async def ignore_user(self, user_id: int) -> None:
        await self.conn.execute(
            """
            INSERT INTO ignored_users (user_id)
            VALUES (?)
            ON CONFLICT(user_id) DO UPDATE SET ignored_at = CURRENT_TIMESTAMP
            """,
            (user_id,),
        )
        await self.conn.commit()

    async def unignore_user(self, user_id: int) -> bool:
        cursor = await self.conn.execute(
            "DELETE FROM ignored_users WHERE user_id = ?",
            (user_id,),
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def is_ignored(self, user_id: int) -> bool:
        cursor = await self.conn.execute(
            "SELECT 1 FROM ignored_users WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        return row is not None

    async def get_ignored_users(self, limit: int | None = None) -> list[aiosqlite.Row]:
        cursor = await self.conn.execute(
            """
            SELECT i.user_id, s.username
            FROM ignored_users i
            LEFT JOIN seen_users s ON s.user_id = i.user_id
            ORDER BY i.ignored_at DESC, lower(s.username), i.user_id
            LIMIT COALESCE(?, -1)
            """,
            (limit,),
        )
        return await cursor.fetchall()

    async def get_registered_entries(self, limit: int | None = None) -> list[aiosqlite.Row]:
        cursor = await self.conn.execute(
            """
            SELECT *
            FROM (
                SELECT
                    r.user_id,
                    COALESCE(s.username, '') AS username,
                    COALESCE(s.first_name, '') AS first_name,
                    r.source,
                    'user' AS kind,
                    r.registered_at
                FROM registered_users r
                LEFT JOIN seen_users s ON s.user_id = r.user_id
                UNION ALL
                SELECT
                    NULL AS user_id,
                    username,
                    '' AS first_name,
                    source,
                    'username' AS kind,
                    registered_at
                FROM registered_usernames
            )
            ORDER BY registered_at DESC, lower(username), user_id
            LIMIT COALESCE(?, -1)
            """,
            (limit,),
        )
        return await cursor.fetchall()

    async def get_status_by_username(self, username: str) -> str:
        normalized = self.normalize_username(username)
        if not normalized:
            return self.format_status("не зарегистрирован", False)

        cursor = await self.conn.execute(
            "SELECT user_id FROM seen_users WHERE lower(username) = ?",
            (normalized,),
        )
        user = await cursor.fetchone()
        if user is None:
            if await self.is_registered_username(normalized):
                return self.format_status("зарегистрирован", False)
            return self.format_status("не зарегистрирован", False)

        user_id = int(user["user_id"])
        ignored = await self.is_ignored(user_id)
        bans = await self.get_user_bans(user_id)
        if bans:
            return self.format_status("забанен", ignored)

        if await self.is_registered(user_id) or await self.is_registered_username(normalized):
            return self.format_status("зарегистрирован", ignored)

        return self.format_status("не зарегистрирован", ignored)

    async def get_status_by_user(self, user_id: int, username: str | None = None) -> str:
        ignored = await self.is_ignored(user_id)
        bans = await self.get_user_bans(user_id)
        if bans:
            return self.format_status("забанен", ignored)

        if await self.is_registered(user_id) or await self.is_registered_username(username):
            return self.format_status("зарегистрирован", ignored)

        return self.format_status("не зарегистрирован", ignored)

    @staticmethod
    def format_status(chat_status: str, ignored: bool) -> str:
        bot_status = "игнорируется" if ignored else "не игнорируется"
        return f"чат: {chat_status}\nбот: {bot_status}"

    @staticmethod
    def normalize_username(username: str | None) -> str:
        return username.removeprefix("@").strip().lower() if username else ""
