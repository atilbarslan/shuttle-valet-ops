"""Token revocation: a token stops working after logout, after the user's account changes and
when the company is deactivated, even though it is still validly signed and not expired."""
import pytest

from world import PASSWORD, auth, make_company, make_superadmin

pytestmark = pytest.mark.anyio


async def login(client, user):
    response = await client.post("/giris-yap", json={"kullanici_adi": user["kullanici_adi"], "sifre": PASSWORD})
    assert response.status_code == 200
    return response.json()["token"]


async def can_list_tasks(client, company, token):
    response = await client.get("/firma-talepleri", params={"firma_id": company.id}, headers=auth(token))
    return response.status_code


async def test_token_is_rejected_after_logout(client, db):
    company = make_company(db, "a")
    token = await login(client, company.user("DANISMAN"))
    assert await can_list_tasks(client, company, token) == 200

    assert (await client.post("/logout", headers=auth(token))).status_code == 200

    assert await can_list_tasks(client, company, token) == 401


async def test_logout_of_one_session_leaves_other_sessions_working(client, db):
    company = make_company(db, "a")
    phone = await login(client, company.user("DANISMAN"))
    laptop = await login(client, company.user("DANISMAN"))

    await client.post("/logout", headers=auth(phone))

    assert await can_list_tasks(client, company, phone) == 401
    assert await can_list_tasks(client, company, laptop) == 200


async def test_old_token_is_rejected_after_an_admin_resets_the_password(client, db):
    company = make_company(db, "a")
    valet = company.user("VALE")
    old_token = await login(client, valet)
    # Used once, so the token also sits in the in-process auth cache.
    assert (await client.get("/vale-gorevi", headers=auth(old_token))).status_code == 200

    reset = await client.put("/personel-sifre-sifirla", headers=auth(company.user("ADMIN")), json={
        "kullanici_adi": valet["kullanici_adi"], "yeni_sifre": "Newpass99",
    })
    assert reset.status_code == 200

    assert (await client.get("/vale-gorevi", headers=auth(old_token))).status_code == 401


async def test_token_of_a_deleted_user_is_rejected(client, db):
    company = make_company(db, "a")
    agent = company.user("DANISMAN")
    token = await login(client, agent)
    assert await can_list_tasks(client, company, token) == 200

    deleted = await client.delete(f"/personel-sil/{agent['kullanici_adi']}", headers=auth(company.user("ADMIN")))
    assert deleted.status_code == 200

    assert await can_list_tasks(client, company, token) == 401


async def test_tokens_stop_working_when_the_company_is_deactivated(client, db):
    """402 Payment Required: in this code base a company is deactivated when its subscription
    is unpaid, and the message tells the user to check the payment."""
    company = make_company(db, "a")
    other = make_company(db, "b")
    token = await login(client, company.user("DANISMAN"))
    other_token = await login(client, other.user("DANISMAN"))
    assert await can_list_tasks(client, company, token) == 200

    response = await client.post("/firma-aktiflik-guncelle", headers=auth(make_superadmin(db)),
                                 json={"firma_id": company.id, "durum": False})
    assert response.status_code == 200

    assert await can_list_tasks(client, company, token) == 402
    assert await can_list_tasks(client, other, other_token) == 200  # other companies unaffected
