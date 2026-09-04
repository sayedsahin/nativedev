from __future__ import annotations

import json
import os
import pwd
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..system import CommandRunner

DEFAULT_DATABASE_PASSWORD = "nativedev"
DATABASE_USERNAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")
PASSWORD_RE = re.compile(r"^[A-Za-z0-9!@#$%^&*()_+\-=.,:?/]{1,128}$")

DATABASES = {
    "mariadb": {"family": "mysql", "host": "localhost", "port": 3306, "database": ""},
    "mysql": {"family": "mysql", "host": "localhost", "port": 3306, "database": ""},
    "postgresql": {"family": "postgresql", "host": "localhost", "port": 5432, "database": "postgres"},
}


@dataclass(frozen=True, slots=True)
class DatabaseAccessState:
    key: str
    managed: bool
    conflict: bool
    username: str
    password: str
    host: str
    port: int
    database: str


class DatabaseAdminPasswordRequired(RuntimeError):
    """Raised when MariaDB/MySQL needs the database root password."""


class DatabaseAccessManager:
    """Manage local-development database credentials for the current Unix user.

    NativeDev provisions missing/default accounts through narrow privileged
    operations. An existing current-user database account can instead be adopted
    without DB-administrator credentials: the user supplies its current database
    password once, NativeDev verifies that password over an explicit password-auth
    connection, then stores it in the user-owned 0600 file.

    Future Change/Reset actions use only that saved current-user credential. For
    MariaDB/MySQL, password proof always uses TCP because Unix-socket plugins can
    authenticate the OS user while ignoring the supplied password. Every password
    mutation is followed by a fresh login before NativeDev updates its credential,
    so a zero-exit SQL statement can never create a false GUI success state.
    """

    def __init__(
        self,
        runner: CommandRunner,
        credential_file: Path | None = None,
        developer_username: str | None = None,
    ):
        self.runner = runner
        self.credential_file = credential_file or self._default_credential_file()
        username = developer_username or pwd.getpwuid(os.getuid()).pw_name
        if not DATABASE_USERNAME_RE.fullmatch(username):
            raise RuntimeError(f"Unsupported local developer username for database access: {username!r}")
        self.developer_username = username

    @staticmethod
    def _default_credential_file() -> Path:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
        return base / "nativedev" / "database-credentials.json"

    @staticmethod
    def supports(key: str) -> bool:
        return key in DATABASES

    @staticmethod
    def validate_password(password: str) -> str:
        if not isinstance(password, str) or not PASSWORD_RE.fullmatch(password):
            raise RuntimeError(
                "Database password must be 1-128 characters using letters, numbers, or !@#$%^&*()_+-=.,:?/"
            )
        return password

    def _load(self) -> dict:
        try:
            raw = json.loads(self.credential_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "databases": {}}
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not read NativeDev database credentials: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("databases"), dict):
            raise RuntimeError("NativeDev database credential file has an unsupported format")
        return raw

    def _write(self, data: dict) -> None:
        directory = self.credential_file.parent
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=directory,
            prefix=".database-credentials-",
            delete=False,
        ) as handle:
            temp = Path(handle.name)
            os.chmod(temp, 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temp, self.credential_file)
            os.chmod(self.credential_file, 0o600)
        finally:
            temp.unlink(missing_ok=True)

    def _record(self, key: str) -> dict | None:
        self._require_key(key)
        record = self._load()["databases"].get(key)
        return record if isinstance(record, dict) else None

    def state(self, key: str) -> DatabaseAccessState:
        self._require_key(key)
        record = self._record(key) or {}
        info = DATABASES[key]
        same_user = record.get("username") == self.developer_username
        managed = same_user and record.get("managed") is True
        conflict = same_user and record.get("conflict") is True and not managed
        password = record.get("password") if managed and isinstance(record.get("password"), str) else ""
        host = str(info["host"])
        return DatabaseAccessState(
            key=key,
            managed=managed,
            conflict=conflict,
            username=self.developer_username,
            password=password,
            host=host,
            port=int(info["port"]),
            database=str(info["database"]),
        )

    def forget(self, key: str) -> None:
        """Remove NativeDev's saved credential metadata for one database."""
        self._require_key(key)
        data = self._load()
        if key in data["databases"]:
            del data["databases"][key]
            if data["databases"]:
                self._write(data)
            else:
                self.credential_file.unlink(missing_ok=True)

    def ensure_after_install(self, key: str) -> DatabaseAccessState:
        """Provision/reconcile the current-user account after NativeDev installs a DB."""
        self._require_key(key)
        record = self._record(key)
        if record and record.get("managed") is True and record.get("username") == self.developer_username:
            password = self.validate_password(str(record.get("password") or DEFAULT_DATABASE_PASSWORD))
            # A previously managed MariaDB account may live on a customized host
            # where root socket auth is no longer available. If its saved login is
            # still valid, do not turn a package reinstall into an admin-auth error.
            if DATABASES[key]["family"] == "mysql":
                try:
                    transport = self._verify_mysql_login(password, record.get("transport"))
                    self._save_managed(key, password, transport=transport)
                    return self.state(key)
                except RuntimeError:
                    pass
            self._ensure_owned(key, password)
            transport = self._verify_current_user_login(key, password)
            self._save_managed(key, password, transport=transport)
            return self.state(key)

        if self._account_exists(key):
            self._save_conflict(key)
            return self.state(key)

        self._ensure_owned(key, DEFAULT_DATABASE_PASSWORD)
        transport = self._verify_current_user_login(key, DEFAULT_DATABASE_PASSWORD)
        self._save_managed(key, DEFAULT_DATABASE_PASSWORD, transport=transport)
        return self.state(key)

    def use_existing_account(self, key: str, password: str) -> DatabaseAccessState:
        """Adopt an existing current-user DB account after strict password proof.

        The supplied password must authenticate the current developer account.
        A mismatched password changes nothing. Once verified, NativeDev persists
        the credential so future Change/Reset actions can use the account's own
        self-service password path without database-administrator access.
        """
        self._require_key(key)
        password = self.validate_password(password)
        transport = self._verify_current_user_login(key, password)
        self._save_managed(key, password, transport=transport)
        return self.state(key)

    @staticmethod
    def validate_admin_password(password: str) -> str:
        if not isinstance(password, str) or not password or len(password) > 512:
            raise RuntimeError("MariaDB/MySQL root password must be 1-512 characters")
        if any(ch in password for ch in ("\0", "\n", "\r")):
            raise RuntimeError("MariaDB/MySQL root password must be a single line")
        return password

    def create_local_access(self, key: str, admin_password: str | None = None) -> DatabaseAccessState:
        """Create/reset the current-user DB account to NativeDev defaults.

        MariaDB/MySQL first tries local ``root`` access without a supplied password.
        If that login is unavailable, the caller may retry with the MariaDB/MySQL
        root password. The helper then creates the current Unix-user account when
        missing, or resets the existing account to ``nativedev`` when present.
        PostgreSQL continues to use its local ``postgres`` administrator context.
        """
        self._require_key(key)
        current = self.state(key)
        if current.managed and current.password == DEFAULT_DATABASE_PASSWORD:
            self._verify_current_user_login(key, current.password)
            return current

        if admin_password is not None:
            if DATABASES[key]["family"] != "mysql":
                raise RuntimeError("Database administrator password is only supported for MariaDB/MySQL")
            admin_password = self.validate_admin_password(admin_password)

        self._ensure_owned(key, DEFAULT_DATABASE_PASSWORD, admin_password=admin_password)
        transport = self._verify_current_user_login(key, DEFAULT_DATABASE_PASSWORD)
        self._save_managed(key, DEFAULT_DATABASE_PASSWORD, transport=transport)
        return self.state(key)


    def change_password(self, key: str, password: str) -> DatabaseAccessState:
        self._require_key(key)
        password = self.validate_password(password)
        current = self.state(key)
        if not current.managed:
            raise RuntimeError("NativeDev does not have a verified credential for this database account")
        record = self._record(key) or {}
        transport = self._change_current_user_password(
            key,
            current.password,
            password,
            preferred_transport=record.get("transport"),
        )
        self._save_managed(key, password, transport=transport)
        return self.state(key)

    def reset_password(self, key: str) -> DatabaseAccessState:
        return self.change_password(key, DEFAULT_DATABASE_PASSWORD)

    def _verify_current_user_login(self, key: str, password: str) -> str | None:
        family = DATABASES[key]["family"]
        if family == "mysql":
            return self._verify_mysql_login(password)

        result = self._postgres_user_sql(password, "SELECT current_user;\n")
        if not result.ok:
            raise RuntimeError(result.output or f'Could not authenticate PostgreSQL role "{self.developer_username}"')
        authenticated = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else ""
        if authenticated != self.developer_username:
            raise RuntimeError(
                f'PostgreSQL authenticated as "{authenticated or "unknown"}", not "{self.developer_username}"'
            )
        return None

    def _verify_mysql_login(self, password: str, preferred_transport: str | None = None) -> str:
        """Verify the actual password over TCP; never treat socket auth as proof.

        MariaDB's ``unix_socket`` plugin can authenticate the OS user while
        ignoring the supplied password. Falling back to a socket here therefore
        creates false positives ("password changed" while the old password still
        works in Adminer). NativeDev-managed password credentials always use the
        explicit 127.0.0.1 TCP path.
        """
        result = self._mysql_user_sql(password, "SELECT CURRENT_USER();\n", transport="tcp")
        if not result.ok:
            raise RuntimeError(result.output or f'Could not authenticate MariaDB/MySQL account "{self.developer_username}"')
        authenticated = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
        if authenticated.split("@", 1)[0] != self.developer_username:
            raise RuntimeError(
                f'MariaDB/MySQL authenticated as "{authenticated or "unknown"}", not "{self.developer_username}"'
            )
        return "tcp"

    def _change_current_user_password(
        self,
        key: str,
        current_password: str,
        new_password: str,
        *,
        preferred_transport: str | None = None,
    ) -> str | None:
        family = DATABASES[key]["family"]
        if family == "mysql":
            transport = self._verify_mysql_login(current_password, preferred_transport)
            version = self._mysql_user_sql(current_password, "SELECT VERSION();\n", transport=transport)
            if not version.ok:
                raise RuntimeError(version.output or "Could not identify MariaDB/MySQL server")
            if "mariadb" in version.stdout.lower():
                # SET PASSWORD changes only the current MariaDB account's password
                # and preserves its authentication plugin configuration.
                sql = f"SET PASSWORD = PASSWORD('{new_password}');\n"
            else:
                # MySQL 8 removed PASSWORD(); USER() is the documented self-service
                # account target for ALTER USER.
                sql = f"ALTER USER USER() IDENTIFIED BY '{new_password}';\n"
            result = self._mysql_user_sql(current_password, sql, transport=transport)
            if not result.ok:
                raise RuntimeError(result.output or "Could not change MariaDB/MySQL password")
            # Never claim success merely because ALTER/SET returned zero. Confirm
            # the requested credential works in a fresh client connection first.
            self._verify_mysql_login(new_password, transport)
            return transport

        result = self._postgres_user_sql(current_password, f"ALTER ROLE CURRENT_USER PASSWORD '{new_password}';\n")
        if not result.ok:
            raise RuntimeError(result.output or "Could not change PostgreSQL password")
        self._verify_current_user_login(key, new_password)
        return None

    def _mysql_user_sql(self, password: str, sql: str, *, transport: str = "tcp"):
        client = shutil.which("mariadb") or shutil.which("mysql")
        if not client:
            raise RuntimeError("MariaDB/MySQL client binary was not found")
        if transport not in {"tcp", "socket"}:
            raise RuntimeError("Unsupported MariaDB/MySQL connection transport")
        directory = self.credential_file.parent
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=directory,
            prefix=".db-client-",
            delete=False,
        ) as handle:
            defaults_file = Path(handle.name)
            os.chmod(defaults_file, 0o600)
            handle.write("[client]\n")
            handle.write(f'user="{self.developer_username}"\n')
            handle.write(f'password="{password}"\n')
            if transport == "tcp":
                handle.write("protocol=tcp\n")
                handle.write("host=127.0.0.1\n")
                handle.write("port=3306\n")
            else:
                handle.write("protocol=socket\n")
        try:
            return self.runner.run(
                [
                    client,
                    f"--defaults-extra-file={defaults_file}",
                    "--batch",
                    "--skip-column-names",
                    "--silent",
                ],
                input_text=sql,
                timeout=60,
            )
        finally:
            defaults_file.unlink(missing_ok=True)

    def _postgres_user_sql(self, password: str, sql: str):
        client = shutil.which("psql")
        if not client:
            raise RuntimeError("PostgreSQL psql client binary was not found")
        return self.runner.run(
            [
                client,
                "--no-psqlrc",
                "--no-align",
                "--tuples-only",
                "--host=127.0.0.1",
                "--port=5432",
                f"--username={self.developer_username}",
                "--dbname=postgres",
                "--set=ON_ERROR_STOP=1",
            ],
            env={"PGPASSWORD": password},
            input_text=sql,
            timeout=60,
        )

    def _account_exists(self, key: str) -> bool:
        action = self._action(key, "account_status")
        result = self.runner.privileged_operation(action, check=True, timeout=60)
        return result.stdout.strip() == "1"

    def _ensure_owned(self, key: str, password: str, *, admin_password: str | None = None) -> None:
        fields = {"password": self.validate_password(password)}
        if admin_password is not None:
            fields["admin_password"] = self.validate_admin_password(admin_password)
        result = self.runner.privileged_operation(
            self._action(key, "ensure_dev_account"),
            check=False,
            timeout=120,
            **fields,
        )
        if result.ok:
            return
        if DATABASES[key]["family"] == "mysql" and admin_password is None and (
            "NATIVEDEV_MYSQL_ROOT_PASSWORD_REQUIRED" in (result.stderr or "")
            or "NATIVEDEV_MYSQL_ROOT_PASSWORD_REQUIRED" in (result.stdout or "")
        ):
            raise DatabaseAdminPasswordRequired(
                "MariaDB/MySQL root password is required to create or reset the NativeDev default user"
            )
        if DATABASES[key]["family"] == "mysql" and admin_password is not None:
            raise RuntimeError(result.output or "MariaDB/MySQL root password was rejected")
        raise RuntimeError(result.output or "Could not configure NativeDev database account")


    @staticmethod
    def _action(key: str, suffix: str) -> str:
        family = DATABASES[key]["family"]
        return f"database.{family}.{suffix}"

    def _save_managed(self, key: str, password: str, *, transport: str | None = None) -> None:
        data = self._load()
        info = DATABASES[key]
        record = {
            "managed": True,
            "conflict": False,
            "username": self.developer_username,
            "password": password,
            "host": "127.0.0.1" if info["family"] == "mysql" and transport == "tcp" else info["host"],
            "port": info["port"],
            "database": info["database"],
        }
        if info["family"] == "mysql" and transport in {"tcp", "socket"}:
            record["transport"] = transport
        data["databases"][key] = record
        self._write(data)

    def _save_conflict(self, key: str) -> None:
        data = self._load()
        info = DATABASES[key]
        data["databases"][key] = {
            "managed": False,
            "conflict": True,
            "username": self.developer_username,
            "host": info["host"],
            "port": info["port"],
            "database": info["database"],
        }
        self._write(data)

    @staticmethod
    def _require_key(key: str) -> None:
        if key not in DATABASES:
            raise RuntimeError(f"Database access is not supported for component: {key}")
