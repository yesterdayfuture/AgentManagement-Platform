"""SQLAlchemy 声明基类。

所有表都继承 Base。create_all 只会建缺失的表，不会给旧表加列；
旧库补列放在 core._ensure_columns。
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
