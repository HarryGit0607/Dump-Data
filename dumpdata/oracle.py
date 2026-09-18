"""Connect to Oracle in python-oracledb thin mode (no Instant Client).

The Qc demo listener this repository talks to:

    user P10_DEMO @ 10.0.0.18:1532, database Qc

The password is never stored here. Pass it as ``ORACLE_PASSWORD`` or ``--password``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence


QC_USER = "P10_DEMO"
QC_HOST = "10.0.0.18"
QC_PORT = 1532
QC_DATABASE = "Qc"

PROBE_SQL = """
SELECT
  USER AS session_user,
  SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') AS current_schema,
  SYS_CONTEXT('USERENV', 'DB_NAME') AS db_name,
  SYS_CONTEXT('USERENV', 'INSTANCE_NAME') AS instance_name,
  SYS_CONTEXT('USERENV', 'SERVER_HOST') AS server_host,
  SYS_CONTEXT('USERENV', 'SERVICE_NAME') AS service_name
FROM dual
"""

PGIS_TABLES_SQL = """
SELECT table_name
FROM user_tables
WHERE table_name LIKE 'PGI%'
ORDER BY table_name
"""

UNREACHABLE_MARKERS = (
    "timed out",
    "timeout",
    "could not connect",
    "connection refused",
    "network is unreachable",
    "no route to host",
    "name or service not known",
    "temporarily unavailable",
    "dpy-6005",
    "dpy-6001",
    "dpy-4011",
    "errno 110",
    "errno 111",
    "errno 101",
    "errno 113",
)


class OracleConnectError(Exception):
    """A connection attempt failed in a way the CLI can explain."""

    def __init__(self, message: str, *, unreachable: bool = False) -> None:
        super().__init__(message)
        self.unreachable = unreachable


@dataclass(frozen=True)
class OracleTarget:
    user: str
    password: str
    host: str
    port: int
    service: Optional[str] = None
    sid: Optional[str] = None
    timeout: float = 10.0

    def dsn(self) -> str:
        """Easy Connect for a service name, or a full descriptor for a SID."""
        if self.sid and not self.service:
            return (
                "(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)"
                f"(HOST={self.host})(PORT={self.port}))"
                f"(CONNECT_DATA=(SID={self.sid})))"
            )
        if not self.service:
            raise OracleConnectError("either a service name or a SID is required")
        return f"{self.host}:{self.port}/{self.service}"

    def label(self) -> str:
        if self.sid and not self.service:
            return f"{self.user}@{self.host}:{self.port}/{self.sid} (SID)"
        return f"{self.user}@{self.host}:{self.port}/{self.service} (SERVICE)"


def env_value(name: str, default: Optional[str] = None, environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    source = os.environ if environ is None else environ
    value = source.get(name)
    if value is None or value.strip() == "":
        return default
    return value


def target_from_inputs(
    *,
    user: Optional[str] = None,
    password: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    service: Optional[str] = None,
    sid: Optional[str] = None,
    timeout: float = 10.0,
    environ: Optional[Mapping[str, str]] = None,
) -> OracleTarget:
    """Build a target, filling gaps from the environment then the Qc defaults."""
    resolved_user = user or env_value("ORACLE_USER", QC_USER, environ)
    resolved_password = password or env_value("ORACLE_PASSWORD", None, environ)
    resolved_host = host or env_value("ORACLE_HOST", QC_HOST, environ)
    port_text = env_value("ORACLE_PORT", None, environ)
    resolved_port = port if port is not None else (int(port_text) if port_text else QC_PORT)
    resolved_service = service if service is not None else env_value("ORACLE_SERVICE", None, environ)
    resolved_sid = sid if sid is not None else env_value("ORACLE_SID", None, environ)

    if not resolved_password:
        raise OracleConnectError(
            "Oracle password is missing. Set ORACLE_PASSWORD or pass --password."
        )
    if not resolved_user:
        raise OracleConnectError("Oracle user is missing. Set ORACLE_USER or pass --user.")
    if not resolved_host:
        raise OracleConnectError("Oracle host is missing. Set ORACLE_HOST or pass --host.")

    if resolved_service is None and resolved_sid is None:
        # `@Qc` in a SQL*Plus connect string is historically a SID; 12c+ listeners
        # more often register it as a service name. The caller tries both.
        resolved_service = QC_DATABASE

    return OracleTarget(
        user=resolved_user,
        password=resolved_password,
        host=resolved_host,
        port=int(resolved_port),
        service=resolved_service,
        sid=resolved_sid,
        timeout=timeout,
    )


def fallback_targets(primary: OracleTarget) -> Sequence[OracleTarget]:
    """Service name first, then SID, when the user named the database as `@Qc`."""
    if primary.sid and primary.service:
        return (primary,)
    if primary.sid and not primary.service:
        return (primary,)
    if not primary.service:
        return (primary,)

    as_sid = OracleTarget(
        user=primary.user,
        password=primary.password,
        host=primary.host,
        port=primary.port,
        service=None,
        sid=primary.service,
        timeout=primary.timeout,
    )
    return (primary, as_sid)


def is_unreachable(exc: BaseException) -> bool:
    text = str(exc).lower()
    if any(marker in text for marker in UNREACHABLE_MARKERS):
        return True
    cause = getattr(exc, "__cause__", None)
    if cause is not None and cause is not exc:
        return is_unreachable(cause)
    return False


def unreachable_message(target: OracleTarget) -> str:
    return (
        f"Cannot reach {target.host}:{target.port}. "
        f"{target.host} is a private address, so this Cloud Agent VM has no route "
        f"to the Qc listener. Run `python -m dumpdata connect` from a machine on "
        f"that network, or start a Cursor self-hosted worker there."
    )


def connect_oracle(
    target: OracleTarget,
    opener: Optional[Callable[..., Any]] = None,
):
    """Open a thin-mode connection. ``opener`` is injected in tests."""
    if opener is None:
        try:
            import oracledb  # type: ignore
        except ImportError as exc:
            raise OracleConnectError(
                "oracledb is not installed. Run: pip install 'dumpdata[oracle]'"
            ) from exc
        opener = oracledb.connect
    try:
        return opener(
            user=target.user,
            password=target.password,
            dsn=target.dsn(),
            tcp_connect_timeout=target.timeout,
        )
    except OracleConnectError:
        raise
    except Exception as exc:
        if is_unreachable(exc):
            raise OracleConnectError(
                unreachable_message(target), unreachable=True
            ) from exc
        raise OracleConnectError(f"{target.label()}: {exc}") from exc


def connect_with_fallback(
    primary: OracleTarget,
    opener: Optional[Callable[..., Any]] = None,
) -> tuple[Any, OracleTarget]:
    """Try the Easy Connect service name, then the same name as a SID."""
    errors: list[str] = []
    last_exc: Optional[BaseException] = None
    for target in fallback_targets(primary):
        try:
            return connect_oracle(target, opener=opener), target
        except OracleConnectError as exc:
            last_exc = exc
            if exc.unreachable:
                raise
            errors.append(str(exc))
    if last_exc is None:
        raise OracleConnectError("no Oracle targets to try")
    if len(errors) == 1:
        raise last_exc
    raise OracleConnectError(" ; ".join(errors)) from last_exc


def fetch_one_dict(cursor, sql: str) -> dict[str, Any]:
    cursor.execute(sql)
    row = cursor.fetchone()
    names = [column[0].lower() for column in cursor.description]
    if row is None:
        return {name: None for name in names}
    return dict(zip(names, row))


def probe_session(connection) -> dict[str, Any]:
    cursor = connection.cursor()
    try:
        info = fetch_one_dict(cursor, PROBE_SQL)
        cursor.execute(PGIS_TABLES_SQL)
        info["pgi_tables"] = [row[0] for row in cursor.fetchall()]
        return info
    finally:
        cursor.close()


def format_probe(target: OracleTarget, info: Mapping[str, Any]) -> str:
    tables: Iterable[str] = info.get("pgi_tables") or ()
    table_list = ", ".join(tables) if tables else "(none visible in USER_TABLES)"
    lines = [
        f"connected  {target.label()}",
        f"user       {info.get('session_user')}",
        f"schema     {info.get('current_schema')}",
        f"db_name    {info.get('db_name')}",
        f"instance   {info.get('instance_name')}",
        f"host       {info.get('server_host')}",
        f"service    {info.get('service_name')}",
        f"pgi tables {table_list}",
    ]
    return "\n".join(lines)
