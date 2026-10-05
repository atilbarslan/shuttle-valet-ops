"""Tests for the pure helper functions and rule tables in main.py.

Only code that needs no database, cache or external API is covered here. Endpoints and the
flows that write to the database are not tested in this repository.
"""
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import HTTPException, WebSocketDisconnect

import main


# Distance ---------------------------------------------------------------------------------

def test_distance_is_zero_for_the_same_point():
    assert main.mesafe_hesapla(38.42, 27.14, 38.42, 27.14) == 0


def test_one_degree_of_latitude_is_about_111_km():
    assert main.mesafe_hesapla(38.0, 27.0, 39.0, 27.0) == pytest.approx(111.19, abs=0.01)


def test_distance_is_symmetric():
    a = main.mesafe_hesapla(38.42, 27.14, 39.93, 32.85)
    b = main.mesafe_hesapla(39.93, 32.85, 38.42, 27.14)
    assert a == pytest.approx(b)


# Input validation -------------------------------------------------------------------------

def test_username_is_trimmed_and_lowercased():
    assert main.validate_kullanici_adi("  Ali.Veli_01 ") == "ali.veli_01"


@pytest.mark.parametrize("value", ["ab", "x" * 41, "pınar", "İsmail", "ali veli", "ali@veli"])
def test_username_rejects_short_long_and_non_ascii(value):
    with pytest.raises(HTTPException) as err:
        main.validate_kullanici_adi(value)
    assert err.value.status_code == 400


def test_plate_is_uppercased_and_spaces_removed():
    assert main.validate_plaka(" 34 abc 123 ") == "34ABC123"


@pytest.mark.parametrize("value", ["34A1", "34-ABC-123", "34ABC1234567"])
def test_plate_rejects_bad_length_and_characters(value):
    with pytest.raises(HTTPException):
        main.validate_plaka(value)


def test_display_name_allows_turkish_letters_and_spaces():
    assert main.validate_gorunen_ad(" Mehmet Yılmaz ") == "Mehmet Yılmaz"


@pytest.mark.parametrize("value", ["A", "<script>", "x" * 61])
def test_display_name_rejects_short_long_and_markup(value):
    with pytest.raises(HTTPException):
        main.validate_gorunen_ad(value)


# Role hierarchy ---------------------------------------------------------------------------

@pytest.mark.parametrize("actor, target, allowed", [
    ("SUPERADMIN", "SUPERADMIN", True),
    ("ADMIN", "OPERASYON", True),
    ("OPERASYON", "DANISMAN", True),
    ("DANISMAN", "SOFOR", True),
    ("DANISMAN", "ADMIN", False),
    ("ADMIN", "ADMIN", False),   # same level is not enough
    ("SOFOR", "VALE", False),    # field roles share level 0
])
def test_role_hierarchy(actor, target, allowed):
    assert main.yetki_hiyerarsi_kontrol(actor, target) is allowed


# Dates as returned by Supabase -----------------------------------------------------------

def test_parses_supabase_timestamp_format():
    parsed = main.supabase_tarih_parse("2026-05-12 15:15:51.21136+00")
    assert parsed == datetime(2026, 5, 12, 15, 15, 51, 211360, tzinfo=timezone.utc)


def test_parses_trailing_z_and_treats_naive_as_utc():
    assert main.supabase_tarih_parse("2026-05-12T15:15:51Z").tzinfo is not None
    assert main.supabase_tarih_parse("2026-05-12T15:15:51").tzinfo == timezone.utc


def test_empty_timestamp_is_rejected():
    with pytest.raises(ValueError):
        main.supabase_tarih_parse("")


# Customer link lifetime -------------------------------------------------------------------

def _opened_hours_ago(hours, task_type="TOPLAMA"):
    opened = datetime.now(timezone.utc) - timedelta(hours=hours)
    return {"kayit_tarihi": opened.strftime("%Y-%m-%d %H:%M:%S.%f+00"), "gorev_tipi": task_type}


@pytest.mark.parametrize("hours, task_type, expired", [
    (main.MUSTERI_LINK_OMRU_SAAT - 1, "TOPLAMA", False),
    (main.MUSTERI_LINK_OMRU_SAAT + 1, "TOPLAMA", True),
    (main.VALE_LINK_OMRU_SAAT - 1, "VALE_ALIM", False),
    (main.VALE_LINK_OMRU_SAAT + 1, "VALE_TESLIM", True),
])
def test_link_lifetime_depends_on_module(hours, task_type, expired):
    assert main.musteri_linki_suresi_doldu("token", _opened_hours_ago(hours, task_type)) is expired


@pytest.mark.parametrize("token", ["BTT-destroyed", "DONUS-return-row"])
def test_destroyed_and_synthetic_tokens_are_not_customer_links(token):
    assert main.musteri_linki_suresi_doldu(token, _opened_hours_ago(1000)) is False


def test_missing_date_lets_the_link_through():
    assert main.musteri_linki_suresi_doldu("token", {"gorev_tipi": "VALE_ALIM"}) is False


# Valet state machine ----------------------------------------------------------------------

def _chain(task_type):
    """Follow the transition table from the first status to the end."""
    table = main.VALE_GECISLER[task_type]
    status, chain = "KONUM_ALINDI_VALE", ["KONUM_ALINDI_VALE"]
    while status in table:
        (status,) = table[status]  # every status has exactly one next status
        chain.append(status)
    return chain


def test_pickup_order():
    assert _chain("VALE_ALIM") == ["KONUM_ALINDI_VALE", "VALE_YOLDA", "ARAC_ALINDI", "TAMAM_SERVIS"]


def test_delivery_order():
    assert _chain("VALE_TESLIM") == ["KONUM_ALINDI_VALE", "ARAC_ALINDI", "VALE_YOLDA", "TAMAM_MUSTERI"]


@pytest.mark.parametrize("task_type", ["VALE_ALIM", "VALE_TESLIM"])
def test_cancellable_exactly_until_the_car_is_picked_up(task_type):
    chain = _chain(task_type)
    before_car = chain[:chain.index("ARAC_ALINDI")]
    expected = {"BEKLIYOR_KONUM", *before_car}
    assert set(main.VALE_IPTAL_EDILEBILIR_DURUMLAR[task_type]) == expected


def test_cancellable_statuses_are_active_statuses():
    for statuses in main.VALE_IPTAL_EDILEBILIR_DURUMLAR.values():
        assert set(statuses) <= set(main.VALE_AKTIF_DURUMLAR)


# Tokens and passwords ---------------------------------------------------------------------

def _lifetime(role):
    payload = jwt.decode(main.token_olustur({"kullanici_adi": "u", "rol": role}),
                         main.JWT_SECRET, algorithms=[main.ALGORITHM])
    return timedelta(seconds=payload["exp"] - payload["iat"])


def test_field_roles_get_seven_day_tokens():
    assert _lifetime("SOFOR") == timedelta(days=7)
    assert _lifetime("VALE") == timedelta(days=7)


def test_office_roles_get_one_day_tokens():
    assert _lifetime("ADMIN") == timedelta(hours=24)


def test_every_token_has_its_own_id():
    decode = lambda t: jwt.decode(t, main.JWT_SECRET, algorithms=[main.ALGORITHM])["jti"]
    assert decode(main.token_olustur({"rol": "ADMIN"})) != decode(main.token_olustur({"rol": "ADMIN"}))


def test_password_hash_round_trip():
    hashed = main.sifreyi_hashle("correct horse")
    assert main.sifreyi_dogrula("correct horse", hashed)
    assert not main.sifreyi_dogrula("wrong horse", hashed)


def test_corrupt_hash_does_not_raise():
    assert main.sifreyi_dogrula("anything", "not-a-bcrypt-hash") is False


# Error reporting filter -------------------------------------------------------------------

def test_websocket_disconnects_are_not_reported():
    exc = WebSocketDisconnect()
    assert main.before_send_filter({"x": 1}, {"exc_info": (type(exc), exc, None)}) is None


def test_other_errors_are_reported():
    exc = ValueError("boom")
    event = {"x": 1}
    assert main.before_send_filter(event, {"exc_info": (type(exc), exc, None)}) is event
