"""Valet task flow through the real endpoints: transitions, cancellation and a concurrent race."""
import asyncio

import pytest

import main
from world import auth, make_company, make_valet_task

pytestmark = pytest.mark.anyio


async def move(client, company, task, new_status, **extra):
    valet = company.user("VALE")
    body = {"arac_id": company.valet_vehicle_id, "talep_id": task["id"], "yeni_durum": new_status}
    body.update(extra)
    return await client.post("/vale-durum-guncelle", json=body, headers=auth(valet))


# Transitions ------------------------------------------------------------------------------

async def test_valid_pickup_step_updates_task_vehicle_and_opens_a_milestone(client, db):
    company = make_company(db, "a")
    task = make_valet_task(db, company, "VALE_ALIM", "KONUM_ALINDI_VALE")

    response = await move(client, company, task, "VALE_YOLDA", lat=38.40, lng=27.10)

    assert response.status_code == 200
    assert db.one("talepler", id=task["id"])["durum"] == "VALE_YOLDA"
    assert db.one("araclar", id=company.valet_vehicle_id)["durum"] == "GÖREVDE"
    milestone = db.one("gorev_etaplari", talep_id=task["id"])
    assert milestone["etap"] == main.ETAP_MUSTERIYE_GIDIS
    assert milestone["hedef_dakika"] == 11  # promise frozen from the travel-time estimate


async def test_taking_the_car_erases_the_customer_location(client, db):
    company = make_company(db, "a")
    task = make_valet_task(db, company, "VALE_ALIM", "VALE_YOLDA")

    response = await move(client, company, task, "ARAC_ALINDI")

    assert response.status_code == 200
    stored = db.one("talepler", id=task["id"])
    assert stored["durum"] == "ARAC_ALINDI"
    assert stored["konum_lat"] is None and stored["konum_lng"] is None


@pytest.mark.parametrize("task_type, current, requested", [
    ("VALE_ALIM", "KONUM_ALINDI_VALE", "ARAC_ALINDI"),    # skips VALE_YOLDA
    ("VALE_TESLIM", "KONUM_ALINDI_VALE", "VALE_YOLDA"),   # pickup order applied to a delivery
    ("VALE_ALIM", "ARAC_ALINDI", "VALE_YOLDA"),           # going backwards
])
async def test_invalid_transition_is_rejected_and_nothing_changes(client, db, task_type, current, requested):
    company = make_company(db, "a")
    task = make_valet_task(db, company, task_type, current)

    response = await move(client, company, task, requested)

    assert response.status_code == 409
    assert db.one("talepler", id=task["id"])["durum"] == current
    assert db.rows("gorev_etaplari") == []


# Cancellation -----------------------------------------------------------------------------

async def cancel(client, company, task):
    agent = company.user("DANISMAN")
    return await client.post("/vale-gorev-iptal", json={"talep_id": task["id"]}, headers=auth(agent))


async def test_pickup_can_be_cancelled_while_the_valet_is_still_driving_to_the_customer(client, db):
    company = make_company(db, "a")
    task = make_valet_task(db, company, "VALE_ALIM", "VALE_YOLDA")

    response = await cancel(client, company, task)

    assert response.status_code == 200
    stored = db.one("talepler", id=task["id"])
    assert stored["durum"] == "IPTAL_EDILDI"
    assert stored["token"].startswith("BTT-")             # the customer link is destroyed
    assert stored["konum_lat"] is None                     # and the location erased
    assert db.one("araclar", id=company.valet_vehicle_id)["durum"] == "MERKEZDE"


@pytest.mark.parametrize("task_type, status", [
    ("VALE_TESLIM", "VALE_YOLDA"),   # delivery: the valet is already driving the customer's car
    ("VALE_TESLIM", "ARAC_ALINDI"),  # delivery: car taken out of the service center
    ("VALE_ALIM", "ARAC_ALINDI"),    # pickup: the valet has the car
])
async def test_task_cannot_be_cancelled_once_the_valet_has_the_car(client, db, task_type, status):
    company = make_company(db, "a")
    task = make_valet_task(db, company, task_type, status)

    response = await cancel(client, company, task)

    assert response.status_code == 409
    assert db.one("talepler", id=task["id"])["durum"] == status


# Concurrency ------------------------------------------------------------------------------

async def test_two_simultaneous_requests_for_the_same_step_apply_it_once(client, db, monkeypatch):
    """Two taps on "car taken" (or a retry racing the original) arrive together.

    Both requests read the task while it is still VALE_YOLDA. The barrier below holds each one
    at the travel-time call, which comes after that read and before the write, until both have
    arrived, so the race is certain rather than a matter of timing. The status is written with
    a compare-and-set on the old status, so only one request may apply the step and its side
    effects (closing one milestone, opening the next).
    """
    company = make_company(db, "a")
    task = make_valet_task(db, company, "VALE_ALIM", "VALE_YOLDA")
    db.seed("gorev_etaplari", {
        "talep_id": task["id"], "firma_id": company.id, "etap": main.ETAP_MUSTERIYE_GIDIS,
        "baslangic": task["kayit_tarihi"], "hedef_varis": None,
    })

    arrived = 0
    both_read = asyncio.Event()

    async def travel_time_after_both_have_read(*args, **kwargs):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both_read.set()
        await asyncio.wait_for(both_read.wait(), timeout=5)
        return {"km": 4.2, "dakika": 11}

    monkeypatch.setattr(main, "yol_mesafesi_verisi_async", travel_time_after_both_have_read)

    first, second = await asyncio.gather(
        move(client, company, task, "ARAC_ALINDI"),
        move(client, company, task, "ARAC_ALINDI"),
    )

    assert {first.status_code, second.status_code} == {200}
    assert both_read.is_set()  # the race really happened: both read the old status
    assert db.one("talepler", id=task["id"])["durum"] == "ARAC_ALINDI"
    return_legs = db.rows("gorev_etaplari", talep_id=task["id"], etap=main.ETAP_SERVISE_DONUS)
    assert len(return_legs) == 1, "the next milestone must be opened exactly once"
    closed = [m for m in db.rows("gorev_etaplari", etap=main.ETAP_MUSTERIYE_GIDIS) if m["gercek_varis"]]
    assert len(closed) == 1
