"""Oracle connection helpers — no live database required."""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest.mock import patch

from dumpdata.cli import main
from dumpdata.oracle import (
    QC_DATABASE,
    QC_HOST,
    QC_PORT,
    QC_USER,
    OracleConnectError,
    OracleTarget,
    connect_oracle,
    connect_with_fallback,
    format_probe,
    probe_session,
    target_from_inputs,
)


class FakeCursor:
    def __init__(self, probe_row, tables):
        self.probe_row = probe_row
        self.tables = tables
        self.description = None

    def execute(self, sql):
        if "user_tables" in sql.lower():
            self.description = [("TABLE_NAME",)]
        else:
            self.description = [
                ("SESSION_USER",),
                ("CURRENT_SCHEMA",),
                ("DB_NAME",),
                ("INSTANCE_NAME",),
                ("SERVER_HOST",),
                ("SERVICE_NAME",),
            ]

    def fetchone(self):
        return self.probe_row

    def fetchall(self):
        return [(name,) for name in self.tables]

    def close(self):
        pass


class FakeConnection:
    def __init__(self, tables=("PGITH_POLICY", "PGIS_POLICY_DTL")):
        self.tables = list(tables)
        self.closed = False
        self.probe_row = (
            "P10_DEMO",
            "P10_DEMO",
            "QC",
            "qc",
            "dbhost",
            "qc.world",
        )

    def cursor(self):
        return FakeCursor(self.probe_row, self.tables)

    def close(self):
        self.closed = True


def _target(**overrides):
    values = dict(
        user=QC_USER,
        password="secret",
        host=QC_HOST,
        port=QC_PORT,
        service=QC_DATABASE,
        sid=None,
        timeout=2.0,
    )
    values.update(overrides)
    return OracleTarget(**values)


class TargetTests(unittest.TestCase):
    def test_defaults_are_the_qc_listener(self):
        target = target_from_inputs(password="secret", environ={})
        self.assertEqual(target.user, "P10_DEMO")
        self.assertEqual(target.host, "10.0.0.18")
        self.assertEqual(target.port, 1532)
        self.assertEqual(target.service, "Qc")
        self.assertIsNone(target.sid)

    def test_env_overrides_defaults(self):
        target = target_from_inputs(
            environ={
                "ORACLE_USER": "OTHER",
                "ORACLE_PASSWORD": "pw",
                "ORACLE_HOST": "db.internal",
                "ORACLE_PORT": "1521",
                "ORACLE_SERVICE": "prod",
            }
        )
        self.assertEqual(target.user, "OTHER")
        self.assertEqual(target.host, "db.internal")
        self.assertEqual(target.port, 1521)
        self.assertEqual(target.service, "prod")

    def test_missing_password_is_a_clear_error(self):
        with self.assertRaises(OracleConnectError) as ctx:
            target_from_inputs(environ={})
        self.assertIn("ORACLE_PASSWORD", str(ctx.exception))

    def test_service_dsn_is_easy_connect(self):
        self.assertEqual(_target().dsn(), "10.0.0.18:1532/Qc")

    def test_sid_dsn_is_a_connect_descriptor(self):
        dsn = _target(service=None, sid="Qc").dsn()
        self.assertIn("(SID=Qc)", dsn)
        self.assertIn("(HOST=10.0.0.18)", dsn)
        self.assertIn("(PORT=1532)", dsn)


class ConnectTests(unittest.TestCase):
    def test_timeout_becomes_an_unreachable_error(self):
        def opener(**kwargs):
            raise TimeoutError("timed out waiting for network response")

        with self.assertRaises(OracleConnectError) as ctx:
            connect_oracle(_target(), opener=opener)
        self.assertTrue(ctx.exception.unreachable)
        self.assertIn("10.0.0.18:1532", str(ctx.exception))
        self.assertIn("private address", str(ctx.exception))

    def test_unreachable_does_not_retry_as_sid(self):
        calls = []

        def opener(**kwargs):
            calls.append(kwargs["dsn"])
            raise TimeoutError("timed out")

        with self.assertRaises(OracleConnectError):
            connect_with_fallback(_target(), opener=opener)
        self.assertEqual(calls, ["10.0.0.18:1532/Qc"])

    def test_unknown_service_is_retried_as_a_sid(self):
        calls = []
        conn = FakeConnection()

        def opener(**kwargs):
            calls.append(kwargs["dsn"])
            if "SID=" not in kwargs["dsn"]:
                raise OSError("ORA-12514: listener does not currently know of service requested")
            return conn

        got, used = connect_with_fallback(_target(), opener=opener)
        self.assertIs(got, conn)
        self.assertEqual(used.sid, "Qc")
        self.assertIsNone(used.service)
        self.assertEqual(len(calls), 2)
        self.assertIn("(SID=Qc)", calls[1])

    def test_probe_lists_pgi_tables(self):
        info = probe_session(FakeConnection())
        self.assertEqual(info["session_user"], "P10_DEMO")
        self.assertEqual(info["db_name"], "QC")
        self.assertEqual(info["pgi_tables"], ["PGITH_POLICY", "PGIS_POLICY_DTL"])
        text = format_probe(_target(), info)
        self.assertIn("connected  P10_DEMO@10.0.0.18:1532/Qc (SERVICE)", text)
        self.assertIn("PGITH_POLICY", text)


class ConnectCliTests(unittest.TestCase):
    def run_cli(self, *argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_connect_prints_the_session(self):
        conn = FakeConnection()
        used = _target()
        with patch("dumpdata.cli.connect_with_fallback", return_value=(conn, used)):
            code, output, stderr = self.run_cli("connect", "--password", "x")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("P10_DEMO@10.0.0.18:1532/Qc", output)
        self.assertIn("PGIS_POLICY_DTL", output)
        self.assertTrue(conn.closed)

    def test_connect_returns_one_when_the_listener_is_unreachable(self):
        with patch(
            "dumpdata.cli.connect_with_fallback",
            side_effect=OracleConnectError("Cannot reach 10.0.0.18:1532.", unreachable=True),
        ):
            code, output, stderr = self.run_cli("connect", "--password", "x")
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("10.0.0.18:1532", stderr)
