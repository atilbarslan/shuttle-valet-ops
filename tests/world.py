"""Test data for the integration tests: companies, staff, valet vehicles and tasks.

All names are made up. Tokens are issued with main.token_olustur and the same payload as
/giris-yap, so they are the tokens a real login would return.
"""
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import main

PASSWORD = "Test1234"
_password_hash = None


def password_hash():
    global _password_hash
    if _password_hash is None:  # bcrypt is slow on purpose; hash the test password once
        _password_hash = main.sifreyi_hashle(PASSWORD)
    return _password_hash


@dataclass
class Company:
    id: str
    tag: str
    valet_vehicle_id: str
    users: dict = field(default_factory=dict)  # role -> user row

    def user(self, role):
        return self.users[role]


def make_company(db, tag):
    """A company with the valet module on, one valet with a virtual vehicle, and office staff."""
    company = db.seed("firmalar", {
        "firma_adi": f"Example Company {tag.upper()}", "is_active": True,
        "vale_aktif": True, "shuttle_aktif": True, "merkez_lat": 38.42, "merkez_lng": 27.14,
    })
    vehicle = db.seed("araclar", {
        "firma_id": company["id"], "plaka": f"{tag}_valet", "tip": "VALE",
        "durum": "MERKEZDE", "sube_id": None,
    })
    result = Company(id=company["id"], tag=tag, valet_vehicle_id=vehicle["id"])
    for role in ("ADMIN", "OPERASYON", "DANISMAN", "VALE"):
        result.users[role] = db.seed("kullanicilar", {
            "kullanici_adi": f"{tag}_{role.lower()}", "rol": role, "firma_id": company["id"],
            "marka": "Genel", "sube_id": None, "sifre": password_hash(),
            "arac_id": vehicle["id"] if role == "VALE" else None,
        })
    return result


def make_superadmin(db):
    return db.seed("kullanicilar", {
        "kullanici_adi": "root_admin", "rol": "SUPERADMIN", "firma_id": None,
        "marka": "Genel", "sube_id": None, "sifre": password_hash(), "arac_id": None,
    })


def make_valet_task(db, company, task_type, status, with_location=True):
    return db.seed("talepler", {
        "id": str(uuid.uuid4()), "firma_id": company.id, "sube_id": None, "marka": "Genel",
        "arac_id": company.valet_vehicle_id, "gorev_tipi": task_type, "durum": status,
        "musteri_ad": "Test Customer", "musteri_plaka": "00TEST00",
        "konum_lat": 38.45 if with_location else None,
        "konum_lng": 27.20 if with_location else None,
        "token": uuid.uuid4().hex, "kayit_tarihi": datetime.now(timezone.utc).isoformat(),
    })


def token_for(user):
    """The token /giris-yap would issue for this user."""
    return main.token_olustur({
        "kullanici_adi": user["kullanici_adi"], "rol": user["rol"],
        "firma_id": user.get("firma_id"), "marka": user.get("marka", "Genel"),
        "arac_id": user.get("arac_id"), "sube_id": user.get("sube_id"),
    })


def auth(user_or_token):
    token = user_or_token if isinstance(user_or_token, str) else token_for(user_or_token)
    return {"Authorization": f"Bearer {token}"}
