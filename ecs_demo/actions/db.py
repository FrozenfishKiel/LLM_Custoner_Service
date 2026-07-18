# pip install pymysql sqlacodegen
import os
import subprocess
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker

# 创建数据库引擎
load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")


def build_database_url(environ: Mapping[str, str] | None = None) -> URL:
    settings = os.environ if environ is None else environ
    password = settings.get("MYSQL_PASSWORD")
    if not password:
        raise RuntimeError("MYSQL_PASSWORD is required")

    return URL.create(
        drivername="mysql+pymysql",
        username=settings.get("MYSQL_USER", "root"),
        password=password,
        host=settings.get("MYSQL_HOST", "127.0.0.1"),
        port=int(settings.get("MYSQL_PORT", "3306")),
        database=settings.get("MYSQL_DATABASE", "ecs"),
        query={"charset": "utf8mb4"},
    )


url = build_database_url()

# 配置会话工厂
engine = create_engine(url)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


if __name__ == "__main__":

    def export_db_table_class(run=False):
        """将数据库表映射为Python类"""
        if not run:
            return
        output_path = "db_table_class.py"

        cmd = ["python", "-m", "sqlacodegen", str(url)]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(result.stdout)

    export_db_table_class(True)
