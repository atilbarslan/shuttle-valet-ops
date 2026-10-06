"""Company isolation: staff of company A cannot read or change company B's records (IDOR).

Each test first checks that the record really exists and that B's own staff can reach it, so a
403 for A means "refused because of the company", not "not found".
"""
import pytest

from world import PASSWORD, auth, make_company, make_valet_task

pytestmark = pytest.mark.anyio


@pytest.fixture
def companies(db):
    return make_company(db, "a"), make_company(db, "b")


async def test_listing_another_companys_tasks_is_refused(client, db, companies):
    a, b = companies
    task = make_valet_task(db, b, "VALE_ALIM", "KONUM_ALINDI_VALE")

    own = await client.get("/firma-talepleri", params={"firma_id": b.id}, headers=auth(b.user("DANISMAN")))
    assert own.status_code == 200
    assert [t["id"] for t in own.json()] == [task["id"]]

    other = await client.get("/firma-talepleri", params={"firma_id": b.id}, headers=auth(a.user("DANISMAN")))
    assert other.status_code == 403


async def test_moving_another_companys_valet_task_is_refused(client, db, companies):
    a, b = companies
    task = make_valet_task(db, b, "VALE_ALIM", "KONUM_ALINDI_VALE")

    # A's admin may move valet tasks in general, and sends B's real task and vehicle ids.
    response = await client.post("/vale-durum-guncelle", headers=auth(a.user("ADMIN")), json={
        "arac_id": b.valet_vehicle_id, "talep_id": task["id"], "yeni_durum": "VALE_YOLDA",
    })

    assert response.status_code == 403
    assert db.one("talepler", id=task["id"])["durum"] == "KONUM_ALINDI_VALE"


async def test_cancelling_another_companys_valet_task_is_refused(client, db, companies):
    a, b = companies
    task = make_valet_task(db, b, "VALE_ALIM", "KONUM_ALINDI_VALE")

    response = await client.post("/vale-gorev-iptal", json={"talep_id": task["id"]},
                                 headers=auth(a.user("DANISMAN")))

    assert response.status_code == 403
    stored = db.one("talepler", id=task["id"])
    assert stored["durum"] == "KONUM_ALINDI_VALE"
    assert stored["token"] == task["token"]  # B's customer link still works


async def test_resetting_a_password_in_another_company_is_refused(client, db, companies):
    a, b = companies
    target = b.user("DANISMAN")

    response = await client.put("/personel-sifre-sifirla", headers=auth(a.user("ADMIN")), json={
        "kullanici_adi": target["kullanici_adi"], "yeni_sifre": "Other5678",
    })

    assert response.status_code == 403
    assert db.one("kullanicilar", kullanici_adi=target["kullanici_adi"])["sifre"] == target["sifre"]
    login = await client.post("/giris-yap", json={"kullanici_adi": target["kullanici_adi"], "sifre": PASSWORD})
    assert login.status_code == 200  # B's user can still log in with the old password


async def test_deleting_staff_of_another_company_is_refused(client, db, companies):
    a, b = companies
    target = b.user("OPERASYON")

    response = await client.delete(f"/personel-sil/{target['kullanici_adi']}", headers=auth(a.user("ADMIN")))

    assert response.status_code == 403
    assert db.rows("kullanicilar", kullanici_adi=target["kullanici_adi"])
