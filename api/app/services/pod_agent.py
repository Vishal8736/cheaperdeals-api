"""The POD agent's job: every run it makes a few new designs and puts them on the chosen product types at every connected POD supplier."""
from __future__ import annotations


from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import Connection, Design, PodItem
from . import pod_designer as D
from . import pod_publish as PP
from . import settings_service as S
from .agents import event


def connections(db: Session) -> list[Connection]:
    return db.query(Connection).filter(Connection.kind == "supplier", Connection.enabled.is_(True), Connection.type.in_(("pod_printful", "pod_printify"))).order_by(Connection.id).all()


def run(db: Session, rt, cfg) -> dict:
    if S.get_bool(db, "paused"):
        return {"paused": True}
    conns = connections(db)
    if not conns:
        return {"none": True}
    c = D.config(db)
    day = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    made_today = db.query(Design).filter(Design.created_at >= day).count()
    res = {"designs": 0, "products": 0, "errors": 0, "skipped": ""}
    # first retry nothing automatically (errors wait for a manual retry); only new designs are created, within the daily cap
    todo = min(c["batch"], c["daily"] - made_today)
    types = [t for t in c["types"] if t in PP.PRODUCT_TYPES]
    if not types:
        return {**res, "skipped": "koi product type chuna nahi"}
    for _ in range(max(0, todo)):
        niche = c["niches"][(db.query(Design).count()) % len(c["niches"])]
        pick = D.slogans(db, niche, 1)
        if not pick:
            res["skipped"] = f"'{niche}' ke naye slogans khatam (AI model jodo ya niche badlo)"
            break
        text, src = pick[0]
        d = D.make_design(db, niche, text, src)
        res["designs"] += 1
        for conn in conns:
            for t in types:
                it = PP.publish_item(db, rt, cfg, d, conn, t)
                res["products"] += 1 if it.status in ("created", "listed") else 0
                res["errors"] += 1 if it.status == "error" else 0
        db.commit()
    if res["designs"]:
        event(db, "pod", f"{res['designs']} naye design bane, {res['products']} product supplier par, {res['errors']} mein dikkat.", "ok" if not res["errors"] else "warn")
    return res


def retry_errors(db: Session, rt, cfg, limit: int = 6) -> int:
    n = 0
    for it in db.query(PodItem).filter(PodItem.status == "error").order_by(PodItem.id).limit(limit).all():
        d, conn = db.get(Design, it.design_id), db.get(Connection, it.connection_id)
        if d and conn and it.ptype in PP.PRODUCT_TYPES:
            n += 1 if PP.publish_item(db, rt, cfg, d, conn, it.ptype).status != "error" else 0
    return n
