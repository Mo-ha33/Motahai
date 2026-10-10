#!/usr/bin/env python3
"""
scripts/run_pilot_simulation.py: SIMULATION of the D-005 signal ladder for one pilot store.

Every order in this run is invented (SIM-100x). Nothing is sent anywhere:
  * in-memory SQLite database, created and discarded inside this process;
  * one Shopify tenant in SHADOW mode, so the pipeline records `shadow` and never calls the sender;
  * RecordingSender keeps each payload in a list instead of posting it. It overrides send_event, so no HTTP client
    is ever created;
  * a throwaway Fernet key generated in-process for the encrypted checkout IP/UA (D-006). The environment variable is
    restored afterwards.

The scenarios go through the REAL pipeline functions: process_webhook (Shopify REST-shaped payloads) and
send_due_events (the scheduler tick, run with `now` moved past the settlement window).

Usage:  python scripts/run_pilot_simulation.py
Exit code 0 on success.
"""

import asyncio
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, TextIO

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.ameen_workforce.capi_service import MetaCAPISender  # noqa: E402
from src.ameen_workforce.credentials import FERNET_KEY_ENV, generate_key  # noqa: E402
from src.ameen_workforce.db import CapiEvent, Order, create_tenant, init_db, make_engine  # noqa: E402
from src.ameen_workforce.order_pipeline import process_webhook, send_due_events  # noqa: E402

# Simulation clock. Every scenario is placed on this clock; no real time is read.
SIM_CLOCK_START = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
SIM_IP = "203.0.113.10"  # RFC 5737 documentation range
SIM_UA = "SIMULATION-ONLY user agent (no real device)"
COD_GATEWAYS = ["Cash on Delivery (COD)"]
PREPAID_GATEWAYS = ["shopify_payments"]


def _h(hours: float) -> timedelta:
    return timedelta(hours=hours)


class RecordingSender(MetaCAPISender):
    """Keeps every payload it would have posted. Overrides send_event, so no HTTP request is made."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: List[Dict[str, Any]] = []

    async def send_event(self, pixel_id, access_token, payload):  # noqa: D401 (signature matches the base class)
        self.calls.append({"dataset_id": pixel_id, "payload": payload})
        return {"status": "success", "events_received": 1, "fbtrace_id": "SIMULATED-NOT-SENT", "raw": {}}


@contextmanager
def throwaway_fernet_key() -> Iterator[None]:
    """Sets MOTAHAI_FERNET_KEY to a freshly generated key for this run only, then restores the previous value."""
    previous = os.environ.get(FERNET_KEY_ENV)
    os.environ[FERNET_KEY_ENV] = generate_key()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(FERNET_KEY_ENV, None)
        else:
            os.environ[FERNET_KEY_ENV] = previous


def _shopify_order(order_id: str, *, customer_id: int, placed: datetime, updated: datetime, phone: str,
                   financial: str = "pending", fulfillment: Optional[str] = None,
                   gateways: Optional[List[str]] = None, total: float = 1250.0, tags: str = "",
                   cancelled_at: Optional[datetime] = None, client_details: bool = True) -> Dict[str, Any]:
    """A Shopify REST-shaped order payload (the fields the parser reads; no real customer data)."""
    payload: Dict[str, Any] = {
        "id": order_id,
        "name": f"#{order_id}",
        "created_at": placed.isoformat(),
        "updated_at": updated.isoformat(),
        "currency": "EGP",
        "total_price": f"{total:.2f}",
        "financial_status": financial,
        "fulfillment_status": fulfillment,
        "payment_gateway_names": list(gateways if gateways is not None else COD_GATEWAYS),
        "tags": tags,
        "cancelled_at": cancelled_at.isoformat() if cancelled_at else None,
        "customer": {"id": customer_id, "email": f"sim-buyer-{customer_id}@example.com", "phone": phone},
        "shipping_address": {"first_name": "Simulated", "last_name": "Buyer", "phone": phone,
                             "city": "Cairo", "country_code": "EG"},
    }
    if client_details:
        payload["client_details"] = {"browser_ip": SIM_IP, "user_agent": SIM_UA}
    return payload


def _fmt(dt: Optional[datetime]) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if dt else "-"


def _fmt_epoch(epoch: Optional[int]) -> str:
    return "-" if epoch is None else _fmt(datetime.fromtimestamp(int(epoch), tz=timezone.utc))


def _rel(at: datetime, placed: datetime) -> str:
    minutes = int((at - placed).total_seconds() // 60)
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    return f"T+{days}d{hours:02d}h{mins:02d}m"


def _snapshot(session, tenant, code: str) -> Dict[str, str]:
    """event_name -> status for one simulated order (empty before the order exists)."""
    order = session.scalar(select(Order).where(Order.tenant_id == tenant.id, Order.platform_order_id == code))
    if order is None:
        return {}
    rows = session.scalars(select(CapiEvent).where(CapiEvent.order_id == order.id)).all()
    return {r.event_name: r.status for r in rows}


class _Run:
    """Feeds webhooks and scheduler ticks through the real pipeline and records what happened."""

    def __init__(self, session, tenant, sender: RecordingSender) -> None:
        self.session = session
        self.tenant = tenant
        self.sender = sender

    async def webhook(self, sc: Dict[str, Any], at: datetime, label: str, payload: Dict[str, Any],
                      topic: str = "orders/updated") -> None:
        before = _snapshot(self.session, self.tenant, sc["code"])
        result = await process_webhook(self.session, self.tenant, "shopify", topic, payload,
                                       sender=self.sender, now=at)
        after = _snapshot(self.session, self.tenant, sc["code"])
        changed = [f"{name}={status}" for name, status in after.items() if before.get(name) != status]
        decision = result.get("d005_decision") or {}
        sc["steps"].append({
            "at": at, "kind": "webhook", "label": label, "action": result["action"],
            "events": changed, "reason": decision.get("reason") if result["action"] not in ("SHADOW", "SENT",
                                                                                            "SCHEDULED") else None,
        })

    async def tick(self, sc: Dict[str, Any], at: datetime) -> None:
        before = _snapshot(self.session, self.tenant, sc["code"])
        counts = await send_due_events(self.session, now=at, sender=self.sender)
        after = _snapshot(self.session, self.tenant, sc["code"])
        changed = [f"{name}={status}" for name, status in after.items() if before.get(name) != status]
        sc["steps"].append({"at": at, "kind": "tick", "label": "scheduler tick (send_due_events)",
                            "counts": counts, "events": changed})


def _scenario(code: str, title: str, placed: datetime, customer_id: int, phone: str, cod: bool) -> Dict[str, Any]:
    return {"code": code, "title": title, "placed": placed, "customer_id": customer_id, "phone": phone,
            "cod": cod, "steps": []}


async def _run_scenarios(run: _Run, base: datetime) -> List[Dict[str, Any]]:
    scenarios: List[Dict[str, Any]] = []

    # SIM-1001: COD. Customer confirms by tag; later the courier delivers and the cash is collected.
    placed = base
    sc = _scenario("SIM-1001", "COD confirmed by tag, then delivered", placed, 880001, "01000000001", cod=True)
    scenarios.append(sc)
    tags = "confirmed"
    await run.webhook(sc, placed + _h(1), "orders/updated: tag 'confirmed' added (unpaid, unfulfilled)",
                      _shopify_order(sc["code"], customer_id=880001, placed=placed, updated=placed + _h(1),
                                     phone="01000000001", tags=tags))
    await run.webhook(sc, placed + _h(30), "orders/updated: marked paid (cash collected) and fulfilled",
                      _shopify_order(sc["code"], customer_id=880001, placed=placed, updated=placed + _h(30),
                                     phone="01000000001", financial="paid", fulfillment="fulfilled", tags=tags))
    await run.tick(sc, placed + _h(36))  # inside the 12h settlement window: nothing due yet
    await run.tick(sc, placed + _h(43))  # past the window: due and sent (shadow)

    # SIM-1002: COD. Courier takes the parcel, the customer refuses it at the door, the order is cancelled.
    placed = base + _h(1)
    sc = _scenario("SIM-1002", "COD shipped, then refused (cancelled after shipping)", placed, 880002,
                   "01000000002", cod=True)
    scenarios.append(sc)
    await run.webhook(sc, placed + _h(2), "orders/updated: fulfilled (handed to courier), unpaid",
                      _shopify_order(sc["code"], customer_id=880002, placed=placed, updated=placed + _h(2),
                                     phone="01000000002", fulfillment="fulfilled"))
    await run.webhook(sc, placed + _h(72), "orders/updated: cancelled (refused at the door)",
                      _shopify_order(sc["code"], customer_id=880002, placed=placed, updated=placed + _h(72),
                                     phone="01000000002", fulfillment="fulfilled",
                                     cancelled_at=placed + _h(72)))
    await run.tick(sc, placed + _h(96))  # nothing is scheduled for a cancelled order

    # SIM-1003: prepaid. Payment is the confirmation and the delivered event. No client_details on purpose,
    # so the pipeline shows the missing_user_agent quality flag.
    placed = base + _h(2)
    sc = _scenario("SIM-1003", "Prepaid order, paid (no checkout user agent captured)", placed, 880003,
                   "01000000003", cod=False)
    scenarios.append(sc)
    await run.webhook(sc, placed + _h(1), "orders/paid: payment captured (prepaid)",
                      _shopify_order(sc["code"], customer_id=880003, placed=placed, updated=placed + _h(1),
                                     phone="01000000003", financial="paid", gateways=PREPAID_GATEWAYS,
                                     client_details=False),
                      topic="orders/paid")
    await run.tick(sc, placed + _h(14))  # 12h settlement after payment: sent (shadow)

    # SIM-1004: COD. Confirmed early, but the courier only reports delivery after the placed+6.5d cutoff.
    placed = base + _h(3)
    sc = _scenario("SIM-1004", "COD delivered after the placed+6.5d cutoff (late_delivery)", placed, 880004,
                   "01000000004", cod=True)
    scenarios.append(sc)
    await run.webhook(sc, placed + _h(1), "orders/updated: tag 'confirmed' added (unpaid, unfulfilled)",
                      _shopify_order(sc["code"], customer_id=880004, placed=placed, updated=placed + _h(1),
                                     phone="01000000004", tags="confirmed"))
    await run.webhook(sc, placed + _h(7 * 24), "orders/updated: marked paid and fulfilled, 7 days later",
                      _shopify_order(sc["code"], customer_id=880004, placed=placed, updated=placed + _h(7 * 24),
                                     phone="01000000004", financial="paid", fulfillment="fulfilled", tags="confirmed"))
    await run.tick(sc, placed + _h(7 * 24 + 1))  # late_delivery is terminal: nothing is due

    return scenarios


def _collect(session, tenant, sc: Dict[str, Any]) -> Dict[str, Any]:
    order = session.scalar(select(Order).where(Order.tenant_id == tenant.id, Order.platform_order_id == sc["code"]))
    rows = session.scalars(select(CapiEvent).where(CapiEvent.order_id == order.id).order_by(CapiEvent.id)).all()
    by_name = {r.event_name: r for r in rows}

    def row(name: str, value: float) -> Optional[Dict[str, Any]]:
        r = by_name.get(name)
        if r is None:
            return None
        return {"status": r.status, "event_id": r.event_id, "event_time": int(r.event_time), "value": value,
                "currency": order.currency, "error_type": r.error_type, "due_at": r.due_at,
                "quality_flags": r.quality_flags}

    confirmed = row("ConfirmedOrder", float(order.order_total or 0.0))
    if confirmed is not None:
        confirmed["source"] = order.confirmation_source
    delivered = row("DeliveredPurchase", float(order.value or 0.0))
    return {
        "title": sc["title"],
        "placed": sc["placed"],
        "cod": sc["cod"],
        "order_status": order.current_status,
        "confirmed": confirmed,
        "delivered": delivered,
        "steps": sc["steps"],
    }


def _print_order(say, record: Dict[str, Any], code: str) -> None:
    placed = record["placed"]
    say(f"[{code}] {record['title']}")
    say(f"  placed {_fmt(placed)}   payment: {'COD' if record['cod'] else 'prepaid'}")
    say("  Native Purchase: sent by the merchant's own Shopify/Meta integration at order creation.")
    say("                   Motahai does NOT send it (not simulated here).")
    say("  Timeline:")
    for step in record["steps"]:
        when = _rel(step["at"], placed)
        if step["kind"] == "webhook":
            events = ", ".join(step["events"]) if step["events"] else "no event"
            extra = f"  [{step['action']}]"
            reason = f"  ({step['reason']})" if step.get("reason") else ""
            say(f"    {when}  webhook  {step['label']}")
            say(f"                       -> {events}{extra}{reason}")
        else:
            c = step["counts"]
            if c["due"] == 0:
                outcome = "nothing due"
            else:
                outcome = ", ".join(f"{k}={v}" for k, v in c.items() if v)
            say(f"    {when}  tick     send_due_events: {outcome}")
            if step["events"]:
                say(f"                       -> {', '.join(step['events'])}")

    def line(name: str, row: Optional[Dict[str, Any]], missing: str) -> None:
        if row is None:
            say(f"  {name:<18}: {missing}")
            return
        parts = [f"status={row['status']}"]
        if row.get("source"):
            parts.append(f"source={row['source']}")
        parts.append(f"event_time={_fmt_epoch(row['event_time'])}")
        parts.append(f"value={row['value']:.2f} {row['currency']}")
        parts.append(f"event_id={row['event_id']}")
        if row.get("due_at") is not None and row["status"] != "shadow":
            parts.append(f"due_at={_fmt(row['due_at'])}")
        if row.get("error_type"):
            parts.append(f"error_type={row['error_type']}")
        if row.get("quality_flags"):
            parts.append(f"quality_flags={row['quality_flags']}")
        say(f"  {name:<18}: " + "  ".join(parts))

    line("ConfirmedOrder", record["confirmed"], "no row")
    line("DeliveredPurchase", record["delivered"],
         "no row (cancelled: never a delivered conversion)" if record["order_status"] == "cancelled" else "no row")
    say("")


def _print_summary(say, records: Dict[str, Dict[str, Any]], sender_calls: int) -> None:
    say("SUMMARY (all events are shadow or held: nothing reached Meta)")
    say(f"  {'Order':<10}{'Scenario':<62}{'ConfirmedOrder':<18}{'DeliveredPurchase':<30}{'Value sent':<12}")
    for code, r in records.items():
        conf = r["confirmed"]["status"] if r["confirmed"] else "none"
        if r["delivered"] is None:
            deliv = "none (cancelled)" if r["order_status"] == "cancelled" else "none"
            value = "-"
        else:
            deliv = r["delivered"]["status"]
            if r["delivered"]["status"] == "late_delivery":
                deliv += " (never sent)"
            value = f"{r['delivered']['value']:.2f}"
        say(f"  {code:<10}{r['title']:<62}{conf:<18}{deliv:<30}{value:<12}")
    say("")
    say(f"Meta calls made: 0 (recording sender invoked {sender_calls} times; tenant mode is shadow).")


def run_simulation(out: Optional[TextIO] = None) -> Dict[str, Any]:
    """
    Runs the four scenarios through the real pipeline, prints a readable timeline and a summary, and returns
    structured results for the tests. Uses an in-memory database and a throwaway Fernet key; restores the environment.
    """
    out = out if out is not None else sys.stdout

    def say(text: str = "") -> None:
        print(text, file=out)

    say("=" * 100)
    say("SIMULATION ONLY. Invented orders SIM-1001..SIM-1004. Nothing is sent to Meta or any other service.")
    say("Tenant is SHADOW mode, the sender only records payloads, the database is in memory.")
    say(f"Simulation clock starts {_fmt(SIM_CLOCK_START)}. Settlement window: tenant default (12h).")
    say("=" * 100)

    with throwaway_fernet_key():
        engine = make_engine("sqlite://")
        init_db(engine)
        session = sessionmaker(bind=engine, expire_on_commit=False)()
        try:
            tenant = create_tenant(session, name="SIMULATION pilot store (shadow)", platform="shopify",
                                   shop_domain="sim-pilot.myshopify.com", meta_dataset_id="SIMULATED-DATASET",
                                   mode="shadow")
            sender = RecordingSender()
            run = _Run(session, tenant, sender)
            scenarios = asyncio.run(_run_scenarios(run, SIM_CLOCK_START))

            records: Dict[str, Dict[str, Any]] = {}
            for sc in scenarios:
                records[sc["code"]] = _collect(session, tenant, sc)

            say("")
            for code, record in records.items():
                _print_order(say, record, code)
            _print_summary(say, records, len(sender.calls))
            result = {
                "tenant_mode": tenant.mode,
                "recording_sender_calls": len(sender.calls),
                "orders": records,
            }
        finally:
            session.close()
            engine.dispose()
    return result


def main() -> int:
    run_simulation(sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
