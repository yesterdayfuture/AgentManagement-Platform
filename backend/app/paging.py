"""管理界面共用的查询约定。

不传 page 时返回原始数组，旧测试和智能体工作室依赖这个形状。
传了 page 才返回 {items, total, page, page_size}。page_size 限制在 1 到 100。
模糊查询走 ILIKE；exact_field 必须在接口声明的字段里；只写日期的结束时间含当天整天。
"""

from datetime import datetime, timedelta

from sqlalchemy import or_


class Desk:
    def __init__(self, page=None, page_size=20, q="", exact_field="", exact="", time_from="", time_to=""):
        self.page = page
        self.page_size = page_size
        self.q = (q or "").strip()
        self.exact_field = (exact_field or "").strip()
        self.exact = (exact or "").strip()
        self.time_from = time_from or ""
        self.time_to = time_to or ""


def desk_params(
    page: int | None = None,
    page_size: int = 20,
    q: str = "",
    exact_field: str = "",
    exact: str = "",
    time_from: str = "",
    time_to: str = "",
) -> Desk:
    return Desk(page, page_size, q, exact_field, exact, time_from, time_to)


def stamp(value) -> str | None:
    return value.isoformat() if value else None


def _parsed(value: str):
    text = (value or "").strip().replace("Z", "")
    if not text:
        return None
    if len(text) == 10:
        return datetime.strptime(text, "%Y-%m-%d"), True
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt), False
        except ValueError:
            continue
    return None


def window(desk: Desk):
    start = _parsed(desk.time_from)
    end = _parsed(desk.time_to)
    if end and end[1]:
        return (start[0] if start else None, end[0] + timedelta(days=1), True)
    return (start[0] if start else None, end[0] if end else None, False)


def apply_desk(query, desk: Desk, *, text, exact, time_col):
    if desk.q and text:
        like = f"%{desk.q}%"
        query = query.filter(or_(*[column.ilike(like) for column in text]))
    if desk.exact and desk.exact_field in exact:
        column = exact[desk.exact_field]
        value = desk.exact
        try:
            if getattr(column.type, "python_type", str) is int:
                value = int(value)
        except (TypeError, ValueError):
            value = desk.exact
        query = query.filter(column == value)
    if time_col is not None:
        start = _parsed(desk.time_from)
        end = _parsed(desk.time_to)
        if start:
            query = query.filter(time_col >= start[0])
        if end:
            if end[1]:
                query = query.filter(time_col < end[0] + timedelta(days=1))
            else:
                query = query.filter(time_col <= end[0])
    return query


def finish(query, mapper, desk: Desk, *, order, text, exact, time_col, limit_unpaged: int | None = None):
    filtered = apply_desk(query, desk, text=text, exact=exact, time_col=time_col)
    if desk.page is None:
        ordered = filtered.order_by(order) if order is not None else filtered
        if limit_unpaged:
            ordered = ordered.limit(limit_unpaged)
        return [mapper(row) for row in ordered.all()]
    page = max(1, int(desk.page))
    size = min(100, max(1, int(desk.page_size or 20)))
    total = filtered.count()
    rows = filtered.order_by(order).offset((page - 1) * size).limit(size).all() if order is not None else filtered.offset((page - 1) * size).limit(size).all()
    return {"items": [mapper(row) for row in rows], "total": total, "page": page, "page_size": size}


def finish_rows(rows, mapper, desk: Desk):
    if desk.page is None:
        return [mapper(row) for row in rows]
    page = max(1, int(desk.page))
    size = min(100, max(1, int(desk.page_size or 20)))
    start = (page - 1) * size
    return {"items": [mapper(row) for row in rows[start : start + size]], "total": len(rows), "page": page, "page_size": size}
