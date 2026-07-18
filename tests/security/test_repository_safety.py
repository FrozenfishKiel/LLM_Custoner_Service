import ast
import os
import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DB_MODULE_PATH = REPOSITORY_ROOT / "ecs_demo" / "actions" / "db.py"
START_SCRIPT_PATH = REPOSITORY_ROOT / "start_customer_service.ps1"


def _has_literal_db_password_assignment(source: str) -> bool:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            assigns_db_password = any(
                isinstance(target, ast.Name) and target.id == "db_password"
                for target in node.targets
            )
            if assigns_db_password and isinstance(node.value, ast.Constant):
                return isinstance(node.value.value, str)
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "db_password"
            and isinstance(node.value, ast.Constant)
        ):
            return isinstance(node.value.value, str)
    return False


def test_repository_root_resolves_required_sources() -> None:
    assert (REPOSITORY_ROOT / "pytest.ini").is_file()
    assert DB_MODULE_PATH.is_file()
    assert START_SCRIPT_PATH.is_file()


def test_database_url_is_built_from_the_supplied_environment(monkeypatch) -> None:
    settings = {
        "MYSQL_HOST": "db.internal.test",
        "MYSQL_PORT": "43306",
        "MYSQL_DATABASE": "customer_service_test",
        "MYSQL_USER": "service_test_user",
        "MYSQL_PASSWORD": "test-only-password",
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)

    child_code = """
import os
from ecs_demo.actions import db

names = ("MYSQL_HOST", "MYSQL_PORT", "MYSQL_DATABASE", "MYSQL_USER", "MYSQL_PASSWORD")
settings = {name: os.environ[name] for name in names}
url = db.build_database_url(settings)
assert url.drivername == "mysql+pymysql"
assert url.host == settings["MYSQL_HOST"]
assert url.port == int(settings["MYSQL_PORT"])
assert url.database == settings["MYSQL_DATABASE"]
assert url.username == settings["MYSQL_USER"]
assert url.password == settings["MYSQL_PASSWORD"]
"""
    result = subprocess.run(
        [sys.executable, "-c", child_code],
        cwd=REPOSITORY_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    returncode = result.returncode

    assert returncode == 0


def test_legacy_fixed_database_password_does_not_return() -> None:
    source = DB_MODULE_PATH.read_text(encoding="utf-8")
    contains_fixed_password = _has_literal_db_password_assignment(source)

    assert not contains_fixed_password, (
        "db.py must not assign a quoted literal to db_password"
    )


def test_start_script_does_not_create_a_direct_pymysql_connection() -> None:
    source = START_SCRIPT_PATH.read_text(encoding="utf-8")
    contains_pymysql_connect = "pymysql.connect" in source

    assert not contains_pymysql_connect


def test_get_order_count_runs_from_demo_directory_and_restores_location() -> None:
    source = START_SCRIPT_PATH.read_text(encoding="utf-8")
    function_body = source.split("function Get-OrderCount {", 1)[1].split(
        "\nfunction ", 1
    )[0]

    push = function_body.index("Push-Location $DemoDir")
    try_block = function_body.index("try {", push)
    invocation = function_body.index("$script | & $PythonExe -", try_block)
    finally_block = function_body.index("finally {", invocation)
    pop = function_body.index("Pop-Location", finally_block)
    result = function_body.index("return [int]", pop)

    assert push < try_block < invocation < finally_block < pop < result
