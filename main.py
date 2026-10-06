from fastapi import FastAPI, HTTPException, Request, Depends, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt
import os
from dotenv import load_dotenv
load_dotenv()
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel
from supabase import create_client, Client
import uuid
from datetime import datetime, timedelta, timezone
import math
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import bcrypt
import html
import re
import httpx
import asyncio
import sentry_sdk
import time
from typing import Optional
import secrets
import redis.asyncio as redis
import json
import logging
from zoneinfo import ZoneInfo

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# Refuse to start if a required environment variable is missing or blank. Failing at boot is
# better than a server that runs with an empty JWT secret or no database and breaks per request.
_KRITIK_ENV_DEGISKENLERI = {
    "JWT_SECRET_KEY": "JWT imzalama anahtarı (token güvenliği için zorunlu)",
    "SUPABASE_URL": "Supabase proje URL'si (veritabanı bağlantısı)",
    "SUPABASE_KEY": "Supabase service key (veritabanı yetkisi)",
    "MAPBOX_API_KEY": "Mapbox API anahtarı (rota hesaplama)",
    "RESEND_API_KEY": "Resend API anahtarı (mail gönderimi)",
    "REDIS_URL": "Redis bağlantı URL'si (cache ve rate limiting)",
    "BASE_URL": "Sistemin public URL'si (mail linkleri için)",
}

_eksik_env = []
for _env_adi, _aciklama in _KRITIK_ENV_DEGISKENLERI.items():
    _deger = os.getenv(_env_adi)
    if not _deger or not _deger.strip():
        _eksik_env.append(f"  - {_env_adi}: {_aciklama}")

if _eksik_env:
    _hata_mesaji = (
        "\n" + "=" * 60 + "\n"
        "🚨 KRİTİK HATA: .env dosyasında eksik değişkenler var!\n"
        "Sistem güvenliği ve doğru çalışması için başlatılamadı.\n\n"
        "Eksik değişkenler:\n"
        + "\n".join(_eksik_env)
        + "\n\n"
        ".env dosyanızı kontrol edin ve eksik değişkenleri ekleyin.\n"
        + "=" * 60
    )
    raise RuntimeError(_hata_mesaji)

JWT_SECRET = os.getenv("JWT_SECRET_KEY")
ALGORITHM = "HS256"

def before_send_filter(event, hint):
    """Sentry hook that drops WebSocket disconnects before they are reported.

    Phones suspend the browser whenever the screen locks, so these errors are
    routine and would otherwise flood the error tracker.
    """
    if 'exc_info' in hint:
        exc_type, exc_value, _ = hint['exc_info']
        if isinstance(exc_value, WebSocketDisconnect):
            return None
        # Some mobile browsers report the same event as a generic "suspension" error.
        if 'suspension' in str(exc_value).lower():
            return None
    return event

# Error tracking with Sentry is optional. To turn it on, set SENTRY_DSN to your project's DSN
# (sentry.io > Settings > Projects > your project > Client Keys (DSN)). If it is not set,
# Sentry is never initialised and the app runs normally without error tracking.
SENTRY_DSN = os.getenv("SENTRY_DSN")
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        before_send=before_send_filter,
        send_default_pii=False,
        traces_sample_rate=0.1,
        environment="production"
    )

# Application log. Under systemd this ends up in the journal. Sentry's default logging
# integration also turns ERROR records into Sentry events. Never log personal data here.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("app")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
security = HTTPBearer()

MAPBOX_API_KEY = os.getenv("MAPBOX_API_KEY")
REDIS_URL = os.getenv("REDIS_URL")
redis_client = redis.from_url(REDIS_URL, decode_responses=True)
# Lifetime of cached Mapbox route legs (distance/duration). Kept at 15 minutes because the
# "driving-traffic" profile reflects live traffic, so older values would skew ETAs.
CACHE_TTL = 900

# Lifetime of a customer tracking link (talep-detay). The main protection is that the link is
# destroyed when the job ends (the token is rewritten with a "BTT-" prefix); this limit is only a
# safety net for links nobody closed. Shuttle trips finish on the same day, hence 12 hours.
MUSTERI_LINK_OMRU_SAAT = 12
# After a valet delivery is completed, the link stays alive this many more hours so the
# customer can fill in the satisfaction survey. During this window the link opens the survey
# screen only; name, phone and plate are not returned (see the survey mode in `talep-detay`).
# The token dies when the window ends or the survey is submitted, whichever comes first.
ANKET_PENCERESI_SAAT = 24
# Maximum length of the free-text survey comment. The database has a CHECK constraint with the
# same number and the client shows a counter, so all three must stay equal. Comments are
# deleted after 90 days; a shorter limit also means less room for personal data.
ANKET_YORUM_MAKS = 500
# Survey questions (field name -> report label). Single source for the backend.
# The order and field names must match `ANKET_SORULARI` in frontend/js/musteri.js: the
# customer page submits these field names and the report aggregates by them.
ANKET_SORU_ETIKETLERI = {
    "puan_genel": "Genel memnuniyet",
    "puan_dakiklik": "Dakiklik",
    "puan_ilgi": "Personel ilgisi",
    "puan_arac_durumu": "Araç durumu",
    "puan_bilgilendirme": "Bilgilendirme",
}
# Valet (vale) links live longer than shuttle links: the car can stay at the service center for
# days, the customer should still be able to see that, and the delivery may slip to the next day.
VALE_LINK_OMRU_SAAT = 72

# Statuses in which a valet task counts as "in progress". Single source, used in three places
# that must all see the same list:
#   1) yeni-talep       a valet cannot be given a second task while one is active
#   2) personel-sil     a valet with an active task cannot be deleted (the task would be orphaned)
#   3) firma-talepleri  an active task carried over from an earlier day still appears on the
#                       panel even though it falls outside the date filter
# BEKLIYOR_KONUM (waiting for the customer's location) is included: the valet is already bound
# to the task even if the customer has not confirmed a location yet.
VALE_AKTIF_DURUMLAR = ["BEKLIYOR_KONUM", "KONUM_ALINDI_VALE", "VALE_YOLDA", "ARAC_ALINDI"]

# Statuses from which a valet task may be cancelled. Cancelling is allowed only while the
# customer's car has not been picked up. Once the valet has the car, closing the record as
# "cancelled" would drop the car out of the system and break the chain of custody, so the task
# has to be completed instead (a pickup ends at the service center, a delivery at the customer).
#
# The allowed set depends on the task type, so this cannot be a flat list:
#   VALE_ALIM (pickup)     KONUM_ALINDI_VALE -> VALE_YOLDA -> ARAC_ALINDI
#       In VALE_YOLDA the valet is driving to the customer and has no car yet: cancellable.
#   VALE_TESLIM (delivery) KONUM_ALINDI_VALE -> ARAC_ALINDI -> VALE_YOLDA
#       In VALE_YOLDA the valet is already driving the customer's car: not cancellable.
# A flat list containing VALE_YOLDA would let a delivery be cancelled mid-drive and would put the
# car back on the "waiting at service" list while it is actually on the road.
# Keep this in sync with VALE_GECISLER (the transition table), which has the same asymmetry.
# Frontend copies: VALE_IPTAL_EDILEBILIR in danisman.js and operasyon.js must match this dict;
# the status order is mirrored by the buttons in vale.js and the progress view in musteri.js.
VALE_IPTAL_EDILEBILIR_DURUMLAR = {
    "VALE_ALIM":   ["BEKLIYOR_KONUM", "KONUM_ALINDI_VALE", "VALE_YOLDA"],
    "VALE_TESLIM": ["BEKLIYOR_KONUM", "KONUM_ALINDI_VALE"],
}

# KVKK (Turkish personal data protection law): privacy notices and explicit consent.
# The consent log table is defined in db/kvkk_riza.sql.
# ADD THESE FILES: the notice pages are NOT included in this repository. Create them yourself with
# your own privacy notices (KVKK "aydınlatma metni"):
#   frontend/kvkk/aydinlatma-yolcu.html     customer notice, linked from musteri.html
#   frontend/kvkk/aydinlatma-personel.html  staff notice, linked from sifre.html
#
# Version of the notice and consent texts. Bump it every time the content of any of those texts
# changes. Each consent record stores this version, so years later it is still possible to tell
# which text a person agreed to. Forgetting the bump makes old consents look like consents to
# the new text, which is the worst mistake to have in an audit.
# There is one shared version for the whole set of texts. When only one text changes, records
# for the unchanged texts also get the new number. That is deliberate: over-stamping still
# answers "which set of texts was in force" correctly, under-stamping does not.
KVKK_METIN_VERSIYONU = "1.2"

# Text codes stored in the consent log's metin_kodu column. They follow the section numbering
# of the legal documents the texts were taken from.
KVKK_METIN_YOLCU_RIZA = "EK6-1"      # explicit consent to location processing (passenger and valet customer)
KVKK_METIN_YOLCU_AYDINLATMA = "EK5-1"
KVKK_METIN_PERSONEL_AYDINLATMA = "EK5-2"

# Whether explicit consent is required before processing a customer's location. Set per company
# (firmalar.kvkk_riza_gerekli); this is the default. If a company's legal review concludes that
# location processing is covered by performance of the contract (KVKK art. 5/2-c), the flag is
# turned off for that company and no code change is needed: the privacy notice is still shown,
# the consent checkbox is not. Asking for consent that is not needed can itself weaken the
# "freely given" quality of consent, which is why the switch exists.
KVKK_RIZA_GEREKLI_VARSAYILAN = True

limiter = Limiter(key_func=get_remote_address)


def istemci_ip(request: Request):
    """The client's IP address, for the audit and consent logs.

    Behind nginx, uvicorn's proxy-header handling (on by default, trusting only 127.0.0.1)
    already puts the address nginx reports into request.client.host; the rate limiter uses the
    same value. X-Forwarded-For is deliberately not read here: its first entry is whatever the
    client sent, so it could be forged.
    """
    return request.client.host if request.client else None
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# WebSocket connection limits. Without a cap, a single client could keep opening sockets
# until the server runs out of memory. Both limits are far above normal use and only
# kick in on abuse.
MAX_WS_TOPLAM = 3000
# Per identity (a staff user or a customer's request token). Generous enough for several open
# tabs plus reconnects.
MAX_WS_KIMLIK_BASINA = 8

class ConnectionManager:
    """In-memory registry of open WebSocket connections and the channels to push to them.

    Each connection is stored with the company, role, vehicle and identity it authenticated
    as, so a message can be sent only to the people who should see it. The server runs a
    single worker process, so one in-memory list is enough.
    """
    def __init__(self):
        self.active_connections: list[dict] = []

    async def connect(self, websocket: WebSocket, firma_id: str = None, rol: str = None, arac_id: str = None, kimlik: str = None):
        self.active_connections.append({
            "ws": websocket,
            "firma_id": firma_id,
            "rol": rol,
            "arac_id": arac_id,
            "kimlik": kimlik
        })

    def disconnect(self, websocket: WebSocket):
        self.active_connections = [conn for conn in self.active_connections if conn["ws"] != websocket]

    async def _paralel_gonder(self, hedef_baglantilar: list, message: str):
        """Send one message to many sockets concurrently and drop the ones that failed.

        return_exceptions=True keeps one dead socket from aborting the whole broadcast.
        Failed sockets are removed here so dead connections do not pile up between
        disconnect events.
        """
        if not hedef_baglantilar:
            return

        gorevler = [conn["ws"].send_text(message) for conn in hedef_baglantilar]

        sonuclar = await asyncio.gather(*gorevler, return_exceptions=True)

        kopmuslar = [
            hedef_baglantilar[i]["ws"]
            for i, sonuc in enumerate(sonuclar)
            if isinstance(sonuc, Exception)
        ]
        
        if kopmuslar:
            self.active_connections = [
                conn for conn in self.active_connections 
                if conn["ws"] not in kopmuslar
            ]

    # Staff of one company, plus every SUPERADMIN.
    async def broadcast_firma(self, firma_id: str, message: str):
        hedef = [
            conn for conn in self.active_connections
            if conn.get("firma_id") == firma_id or conn.get("rol") == "SUPERADMIN"
        ]
        await self._paralel_gonder(hedef, message)

    # Everyone tied to one vehicle (arac): in practice, the customers tracking it.
    async def broadcast_arac(self, arac_id: str, message: str):
        hedef = [
            conn for conn in self.active_connections
            if conn.get("arac_id") == arac_id
        ]
        await self._paralel_gonder(hedef, message)

    # Every connection. Meant for emergencies only.
    async def broadcast_all(self, message: str):
        # Copy the list: connections may be added or removed while the broadcast is awaiting.
        hedef = list(self.active_connections)
        await self._paralel_gonder(hedef, message)

    # SUPERADMIN users only, for system-wide changes.
    async def broadcast_superadmin(self, message: str):
        hedef = [
            conn for conn in self.active_connections
            if conn.get("rol") == "SUPERADMIN"
        ]
        await self._paralel_gonder(hedef, message)

manager = ConnectionManager()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """The application's single WebSocket endpoint, used only to push refresh signals.

    The client sends {"token": ...} as its first message. The token is either a staff JWT or
    a customer's request token from a tracking link. Authentication happens once, when the
    connection opens; incoming messages after that are read and ignored.
    """
    await websocket.accept()

    try:
        # Wait at most 10 seconds for the token, so connections that never authenticate
        # (slowloris-style) cannot hold memory open.
        try:
            ilk_mesaj = await asyncio.wait_for(websocket.receive_json(), timeout=10.0)
        except asyncio.TimeoutError:
            await websocket.close(code=1008)  # policy violation: no token in time
            return
        token = ilk_mesaj.get("token")

        if not token:
            await websocket.close(code=1008)  # policy violation
            return

        # A valid JWT means staff; anything else is tried as a customer's request token.
        personel = False
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
            personel = True
            rol = payload.get("rol")
            firma_id = payload.get("firma_id")
            arac_id = payload.get("arac_id")
            kimlik = payload.get("kullanici_adi")  # identity used for the per-identity cap
        except jwt.InvalidTokenError:
            res = await run_query(supabase.table("talepler").select("arac_id, firma_id, kayit_tarihi, gorev_tipi").eq("token", token))
            if not res.data or musteri_linki_suresi_doldu(token, res.data[0]):
                await websocket.close(code=1008)
                return
            rol = "MUSTERI"
            firma_id = res.data[0].get("firma_id")
            arac_id = res.data[0].get("arac_id")
            kimlik = token  # a customer's identity is their request token

        # Connection caps: a global one to protect memory and a per-identity one so a single
        # client cannot open many sockets. Since authentication happens only once per
        # connection, these caps are what stops a flood of connections.
        if len(manager.active_connections) >= MAX_WS_TOPLAM:
            await websocket.close(code=1013)  # try again later: server is full
            return
        if kimlik and sum(1 for c in manager.active_connections if c.get("kimlik") == kimlik) >= MAX_WS_KIMLIK_BASINA:
            await websocket.close(code=1008)  # policy violation: per-identity cap reached
            return

        # A staff token must pass the same revocation checks as on the REST endpoints (logout,
        # per-user cutoff after a password reset or deletion, inactive company); a valid
        # signature alone is not enough. Placed after the caps for the same reason as below.
        if personel:
            try:
                await yetki_kontrol(HTTPAuthorizationCredentials(scheme="Bearer", credentials=token))
            except HTTPException:
                await websocket.close(code=1008)  # policy violation: token revoked
                return

        # Reject connections for a deactivated or deleted company, matching the 402 that REST
        # endpoints return. This costs one Redis lookup per connection (firma_pasif_mi caches the
        # company status), not one per message. It runs after the caps on purpose, so a
        # flood is turned away before it reaches Redis.
        # Limitation: only new connections are refused. Sockets that are already open when the
        # company is deactivated stay open; in practice the client gets a 402 on its next REST
        # call and logs out.
        if firma_id and await firma_pasif_mi(firma_id):
            await websocket.close(code=1008)  # policy violation: company is inactive
            return

        await manager.connect(websocket, firma_id=firma_id, rol=rol, arac_id=arac_id, kimlik=kimlik)

        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            # Expected: the phone suspended the page, the tab closed or the network dropped.
            pass
        finally:
            manager.disconnect(websocket)

    except WebSocketDisconnect:
        # The client went away before sending its token (for example, the phone locked at once).
        pass
    except Exception:
        # A real error: clean up, close the socket if it is still open, then re-raise so
        # Sentry records it.
        manager.disconnect(websocket)
        if websocket.client_state != WebSocketState.DISCONNECTED:
            try:
                await websocket.close(code=1011)  # internal error
            except RuntimeError:
                pass  # already closed
        raise

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
BASE_URL = os.getenv("BASE_URL")  # no default on purpose; the startup check above requires it

# CORS: in production only our own origin is allowed; local development also allows localhost.
# An https BASE_URL is taken as the production signal.
_PRODUCTION = BASE_URL.startswith("https://")

_izinli_originler = [BASE_URL]
if not _PRODUCTION:
    _izinli_originler.extend([
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ])

app.add_middleware(
    CORSMiddleware,
    allow_origins=_izinli_originler,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Freshness of code files served by StaticFiles.
# StaticFiles sends ETag and Last-Modified but no Cache-Control header. Without that header,
# browsers apply heuristic freshness (roughly 10% of the time since the last modification) and
# may serve a file from cache without asking the server at all. That has two bad effects:
#   1) While a service worker installs, `cache.addAll` can pick up an old JS file from the HTTP
#      cache and store it under the new version, so the version number moves but the code does
#      not. (sw.js also fetches with `cache: 'reload'` to guard against this.)
#   2) The staff panels have no service worker, and a reload could still serve old JS.
# `no-cache` means "you may store it, but revalidate before every use". It is not `no-store`:
# with no-cache plus ETag, an unchanged file costs a 304 with no body, so freshness is nearly
# free. Fonts and images are left alone because caching them is what we want.
_TAZE_TUTULACAK_UZANTILAR = (".html", ".js", ".css")


@app.middleware("http")
async def statik_cache_kontrolu(request: Request, call_next):
    """Add `Cache-Control: no-cache` to HTML, JS and CSS responses (see the note above)."""
    yanit = await call_next(request)
    yol = request.url.path
    if yol.endswith(_TAZE_TUTULACAK_UZANTILAR) or yol == "/":
        # setdefault, so routes that set their own header (such as /sw.js) are not overridden.
        yanit.headers.setdefault("Cache-Control", "no-cache")
    return yanit

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# The Supabase client is synchronous: every query is a blocking HTTP call. Called directly from
# an `async def` endpoint it would stop the event loop, and with it every other request, for the
# length of the round trip. So:
#   - endpoints that await nothing are plain `def`; FastAPI runs them in its thread pool;
#   - `async def` endpoints and helpers run each query through run_query (below), and call the
#     synchronous helpers that query the database through run_in_threadpool.
# Both use the same anyio thread pool (40 threads by default). See docs/PERFORMANCE.md.
async def run_query(query):
    """Execute a Supabase query in the thread pool so it does not block the event loop."""
    return await run_in_threadpool(query.execute)

# Request bodies. Declaring each body as a model (instead of accepting a raw dict) means only the
# listed fields can reach the database, which prevents mass assignment.
class MarkaIstek(BaseModel): firma_id: str; marka_adi: str; sube_id: Optional[str] = None
class FirmaIstek(BaseModel): firma_adi: str; sistem_modu: str = "HARITA"; subeli: bool = False; max_sube: int = 0; toplam_kota: int = 1; vale_aktif: bool = False; shuttle_aktif: bool = True; vale_kota: int = 0; kvkk_unvan: str = ""; kvkk_basvuru_kanali: str = ""; kvkk_adres: str = ""
class SubeIstek(BaseModel): firma_id: str; sube_adi: str; aktif_kota: int = 0
class SubeGuncelleIstek(BaseModel): sube_id: str; sube_adi: Optional[str] = None; aktif_kota: Optional[int] = None; aktif: Optional[bool] = None
class FirmaKotaIstek(BaseModel): firma_id: str; max_sube: Optional[int] = None; toplam_kota: Optional[int] = None; vale_kota: Optional[int] = None
class SubeliYapIstek(BaseModel): firma_id: str; max_sube: int
# When shuttle is switched on, the operating mode (sistem_modu: HARITA or DURAK) is chosen too.
# A valet-only company still has a stored mode, but it was never actually picked.
class ShuttleGuncelleIstek(BaseModel): firma_id: str; durum: bool; sistem_modu: Optional[str] = None
class SifreBelirleIstek(BaseModel): token: str; yeni_sifre: str; gorunen_ad: str = ""
class KullaniciIstek(BaseModel): ekleyen_kisi: str; kullanici_adi: str; email: str = ""; telefon: str = ""; rol: str; firma_id: str; arac_id: str = None; marka: str = "Genel"; sube_id: Optional[str] = None; gorunen_ad: str = ""
class GorunenAdGuncelleIstek(BaseModel): kullanici_adi: str; gorunen_ad: str
class SifreSifirlaIstek(BaseModel): kullanici_adi: str; yeni_sifre: str
class IletisimGuncelleIstek(BaseModel): kullanici_adi: str; email: str = ""; telefon: str = ""
class TopluMailIstek(BaseModel): konu: str; mesaj: str
class AracAtaIstek(BaseModel): kullanici_adi: str; yeni_arac_id: str
class MailIstek(BaseModel): email: str; kullanici_adi: str; link: str; firma_id: str; marka: str
class LoginIstek(BaseModel): kullanici_adi: str; sifre: str
class YeniTalep(BaseModel): musteri_ad: str; musteri_tel: str = ""; arac_id: str; gorev_tipi: str; firma_id: str; marka: str = "Genel"; musteri_plaka: str = ""; iliskili_talep_id: Optional[str] = None
# riza_onay (consent given) is optional and defaults to False so older clients do not get a 422.
# When consent is required and the value is False, the endpoint returns 400, so a missing field
# never results in processing a location without consent.
class KonumOnay(BaseModel): token: str; lat: float; lng: float; secilen_durak_id: str = None; riza_onay: bool = False
class AktiflikIstek(BaseModel): firma_id: str; durum: bool
class GuzergahIstek(BaseModel): firma_id: str; guzergah_adi: str; sube_id: Optional[str] = None
class DurakIstek(BaseModel): guzergah_id: str; firma_id: str; durak_adi: str; konum_lat: float = 0.0; konum_lng: float = 0.0; sira_no: int = 0
class AracGuzergahAtaIstek(BaseModel): 
    arac_id: str
    guzergah_id: Optional[str] = None
class TopluYolcu(BaseModel): token: str; islem: str # 'alindi' (picked up), 'gelmedi' (no-show), 'indi' (dropped off)
class DurakTamamlaIstek(BaseModel): arac_id: str; durak_lat: float; durak_lng: float; yolcular: list[TopluYolcu]
class SablonIstek(BaseModel): arac_id: str; saat: str
class SaatGuncelleIstek(BaseModel): arac_id: str; hareket_saati: str = ""

class MailDavetIstek(BaseModel):
    kullanici_adi: str

# Role levels. A user may only act on users whose level is strictly lower than their own.
# SOFOR (driver) and VALE (valet) are both field roles at the same level.
ROL_HIYERARSI = {"SUPERADMIN": 4, "ADMIN": 3, "OPERASYON": 2, "DANISMAN": 1, "SOFOR": 0, "VALE": 0}

# Shared input validation. It keeps junk out of the database and bounds the size of what we
# store. Length limits and allowed characters differ per field depending on where the value ends up.
_OKUNABILIR_METIN_REGEX = re.compile(r'^[a-zA-Z0-9ğüşıöçĞÜŞİÖÇ\s\-_.,&/()]+$')
# Usernames are ASCII only. Uniqueness is checked case-insensitively, and Turkish case folding
# ("İ".lower() is "i" plus a combining dot, "I" lowers to "i") would let two different names collide.
_KULLANICI_ADI_REGEX = re.compile(r'^[a-zA-Z0-9_.\-]+$')
_PLAKA_REGEX = re.compile(r'^[A-Z0-9]+$')  # applied after uppercasing and removing spaces

def validate_metin(deger: str, alan_adi: str, max_uzunluk: int = 100, min_uzunluk: int = 1) -> str:
    """Validate and trim a human-readable text field such as a name or title.

    Allows letters (including Turkish ones), digits, whitespace and basic punctuation.
    Raises HTTP 400 with a message naming the field.
    """
    if not isinstance(deger, str):
        raise HTTPException(status_code=400, detail=f"{alan_adi} metin olmalı.")
    deger = deger.strip()
    if len(deger) < min_uzunluk:
        raise HTTPException(status_code=400, detail=f"{alan_adi} boş olamaz.")
    if len(deger) > max_uzunluk:
        raise HTTPException(status_code=400, detail=f"{alan_adi} en fazla {max_uzunluk} karakter olabilir.")
    if not _OKUNABILIR_METIN_REGEX.match(deger):
        raise HTTPException(status_code=400, detail=f"{alan_adi} sadece harf, rakam ve temel noktalama içerebilir.")
    return deger

async def riza_kaydi_yaz(
    kanal: str,
    islem: str,
    metin_kodu: str,
    firma_id: str = None,
    talep_id: str = None,
    kullanici_adi: str = None,
    request: Request = None
) -> bool:
    """Append one row to the consent log (riza_kayitlari, see db/kvkk_riza.sql).

    Unlike audit_log_yaz, this fails closed. If an audit log write fails, the operation goes
    on and the log entry is lost. If this write fails, it returns False and the caller must
    stop: under KVKK the burden of proof is on the data controller, so processing a location
    under a consent that was never recorded is the same as processing it without consent.
    Callers therefore record consent before they store the location. The worst possible
    outcome is "consent recorded, no location", never "location stored, no consent record".
    See "Failure handling" in docs/SECURITY-PRIVACY.md.

    No personal data (name, phone, location) is written to this table.
    """
    try:
        ip_adresi = None
        user_agent = None
        if request:
            ip_adresi = istemci_ip(request)
            user_agent = (request.headers.get("user-agent") or "")[:200] or None

        await run_query(supabase.table("riza_kayitlari").insert({
            "firma_id": firma_id,
            "talep_id": talep_id,
            "kullanici_adi": kullanici_adi,
            "kanal": kanal,
            "islem": islem,
            "metin_kodu": metin_kodu,
            "metin_versiyonu": KVKK_METIN_VERSIYONU,
            "ip_adresi": ip_adresi,
            "user_agent": user_agent,
            "kayit_tarihi": datetime.now(timezone.utc).isoformat()
        }))
        return True
    except Exception as e:
        # Report to Sentry so a lost consent record does not go unnoticed.
        sentry_sdk.capture_message(
            f"RIZA KAYDI YAZILAMADI: kanal={kanal} islem={islem} talep={talep_id} | {str(e)[:200]}",
            level="error"
        )
        return False


# ============================================================
# TASK MILESTONES (gorev_etaplari): promised time vs actual time
# ============================================================
# Table definition and design notes: db/gorev_etaplari.sql. No coordinates are stored, there are
# no foreign keys and rows are not purged.
# Each leg of a valet task records the ETA that was promised when the leg started and the time
# it actually ended. Without continuous GPS tracking, these timestamps are how punctuality is
# audited ("did the valet keep the promised time?"). The same rows also form a labelled dataset
# of predicted vs actual travel times.
#
# These helpers never raise. The milestone log is a measurement; failing to write it must not
# stop the operation. (riza_kaydi_yaz fails closed because it is legal evidence; this is not.)

ETAP_MUSTERIYE_GIDIS = "MUSTERIYE_GIDIS"     # pickup:   VALE_YOLDA -> ARAC_ALINDI
ETAP_SERVISE_DONUS = "SERVISE_DONUS"         # pickup:   ARAC_ALINDI -> TAMAM_SERVIS
ETAP_MUSTERIYE_TESLIM = "MUSTERIYE_TESLIM"   # delivery: VALE_YOLDA -> TAMAM_MUSTERI


def vale_merkez_koordinati(firma_id: str, arac_id: str):
    """Return (lat, lng) of the point a valet drives back to.

    That is the vehicle's branch (sube) location if it has one, otherwise the company
    headquarters. Returns (None, None) on any error, since it only feeds the milestone log.
    """
    try:
        f_row = supabase.table("firmalar").select("merkez_lat, merkez_lng").eq("id", firma_id).execute().data
        if not f_row:
            return (None, None)
        arac_row = supabase.table("araclar").select("sube_id").eq("id", arac_id).execute().data
        return referans_konum(firma_id, arac_row[0].get("sube_id") if arac_row else None,
                              (f_row[0].get("merkez_lat"), f_row[0].get("merkez_lng")))
    except Exception:
        return (None, None)

async def etap_ac(talep: dict, etap: str, baslangic_lat=None, baslangic_lng=None,
                  hedef_lat=None, hedef_lng=None):
    """Open a milestone and freeze the promised arrival time.

    `hedef_varis` (promised arrival) is written once and never updated. Punctuality cannot be
    measured against a moving target: "did you keep the time you promised" only makes sense if
    the promise stays fixed. The coordinates are used only to compute the estimate and are not
    stored (data minimisation under KVKK, see the SQL file).
    """
    try:
        simdi = datetime.now(timezone.utc)
        hedef_varis = None
        hedef_dakika = None
        mesafe_km = None

        # A promise needs both endpoints. Some legs have no destination, so they get no promise.
        if None not in (baslangic_lat, baslangic_lng, hedef_lat, hedef_lng):
            eta = await yol_mesafesi_verisi_async(baslangic_lat, baslangic_lng, hedef_lat, hedef_lng)
            if eta and eta.get("dakika"):
                hedef_dakika = int(eta["dakika"])
                hedef_varis = (simdi + timedelta(minutes=hedef_dakika)).isoformat()
                mesafe_km = eta.get("km")

        await run_query(supabase.table("gorev_etaplari").insert({
            "talep_id": talep.get("id"),
            "firma_id": talep.get("firma_id"),
            "sube_id": talep.get("sube_id"),
            # The brand is copied here because `talepler` rows are deleted after 30 days while this
            # log is kept; otherwise the brand could not be recovered later. For a dealer selling
            # several brands, a single average across brands says little. It is stored as text
            # (like talepler.marka), so a later rename does not rewrite history; the same reasoning
            # applies to `vale_kullanici_adi`.
            "marka": talep.get("marka"),
            "arac_id": talep.get("arac_id"),
            "vale_kullanici_adi": talep.get("_vale_kullanici_adi"),
            "gorev_tipi": talep.get("gorev_tipi"),
            "etap": etap,
            "baslangic": simdi.isoformat(),
            "hedef_varis": hedef_varis,
            "hedef_dakika": hedef_dakika,
            "mesafe_km": mesafe_km,
        }))
    except Exception as e:
        sentry_sdk.capture_message(f"ETAP AÇILAMADI: {etap} talep={talep.get('id')} | {str(e)[:200]}",
                                   level="warning")


async def etap_kapat(talep_id: str, etap: str, basma_lat=None, basma_lng=None,
                     hedef_lat=None, hedef_lng=None, konum_yasi_sn=None, konum_dogruluk_m=None):
    """Close a milestone: write the actual arrival, the deviation and data-quality fields.

    The "actual arrival" is really the moment the valet pressed the button. If the valet
    arrives and presses six minutes later, human delay leaks into the label, and because
    button habits differ per person it gets mixed up with the per-driver difference we are
    trying to measure. `basma_mesafe_m` (distance from the destination when the button was
    pressed) lets such records be excluded from analysis. Only the distance in metres is
    stored, never the coordinates.
    """
    try:
        kayit = (await run_query(supabase.table("gorev_etaplari").select("id, hedef_varis, baslangic").eq(
            "talep_id", talep_id).eq("etap", etap).is_("gercek_varis", "null").eq(
            "iptal_edildi", False).order("baslangic", desc=True).limit(1))).data
        if not kayit:
            return  # no open milestone (an older task, or a replayed request)

        simdi = datetime.now(timezone.utc)
        guncelleme = {"gercek_varis": simdi.isoformat()}

        if kayit[0].get("hedef_varis"):
            try:
                hedef_dt = supabase_tarih_parse(kayit[0]["hedef_varis"])
                guncelleme["sapma_dk"] = int(round((simdi - hedef_dt).total_seconds() / 60))
            except (ValueError, TypeError):
                pass  # unparseable timestamp: leave the deviation empty but still close the record

        # Did the valet's device report its own location when the button was pressed?
        # An empty basma_mesafe_m alone does not answer this, because the destination may be
        # missing too (for example, the customer withdrew consent). This field reflects only the
        # valet's side.
        guncelleme["konum_bildirildi"] = (basma_lat is not None and basma_lng is not None)

        if None not in (basma_lat, basma_lng, hedef_lat, hedef_lng):
            guncelleme["basma_mesafe_m"] = int(round(
                mesafe_hesapla(basma_lat, basma_lng, hedef_lat, hedef_lng) * 1000))

        # Age and accuracy of the GPS reading. The distance is not evidence on its own; it has to
        # be read together with these.
        if konum_yasi_sn is not None:
            guncelleme["basma_konum_yasi_sn"] = int(konum_yasi_sn)
        if konum_dogruluk_m is not None:
            guncelleme["basma_dogruluk_m"] = int(konum_dogruluk_m)

        await run_query(supabase.table("gorev_etaplari").update(guncelleme).eq("id", kayit[0]["id"]))
    except Exception as e:
        sentry_sdk.capture_message(f"ETAP KAPATILAMADI: {etap} talep={talep_id} | {str(e)[:200]}",
                                   level="warning")

def kvkk_riza_gerekli_mi(firma_id: str) -> bool:
    """Return whether this company requires explicit consent before storing a location.

    Reads firmalar.kvkk_riza_gerekli. If the value is missing or cannot be read, it errs on
    the safe side and requires consent (fail closed, see "Failure handling" in docs/SECURITY-PRIVACY.md). See KVKK_RIZA_GEREKLI_VARSAYILAN for why this is a
    per-company flag.
    """
    if not firma_id:
        return KVKK_RIZA_GEREKLI_VARSAYILAN
    try:
        res = supabase.table("firmalar").select("kvkk_riza_gerekli").eq("id", firma_id).execute().data
        if not res:
            return KVKK_RIZA_GEREKLI_VARSAYILAN
        deger = res[0].get("kvkk_riza_gerekli")
        return KVKK_RIZA_GEREKLI_VARSAYILAN if deger is None else bool(deger)
    except Exception as e:
        sentry_sdk.capture_exception(e)
        return KVKK_RIZA_GEREKLI_VARSAYILAN


def validate_kvkk_kanal(deger: str, alan_adi: str = "KVKK başvuru kanalı", max_uzunluk: int = 120) -> str:
    """Validate a company's KVKK contact fields: the application channel and postal address.

    The channel is usually an e-mail or registered e-mail (KEP) address. validate_metin cannot
    be used here because its pattern rejects '@'; this is the same allow-list plus '@', '+'
    and ':'.
    """
    if not isinstance(deger, str):
        raise HTTPException(status_code=400, detail=f"{alan_adi} metin olmalı.")
    deger = deger.strip()
    if len(deger) < 3:
        raise HTTPException(status_code=400, detail=f"{alan_adi} çok kısa.")
    if len(deger) > max_uzunluk:
        raise HTTPException(status_code=400, detail=f"{alan_adi} en fazla {max_uzunluk} karakter olabilir.")
    if not re.match(r'^[a-zA-Z0-9ğüşıöçĞÜŞİÖÇ\s\-_.,&/()@+:]+$', deger):
        raise HTTPException(status_code=400, detail=f"{alan_adi} geçersiz karakter içeriyor.")
    return deger

def validate_gorunen_ad(deger: str) -> str:
    """Validate a display name (gorunen_ad), the name shown for a person on the panels.

    Do not confuse it with kullanici_adi. The username is the identity: ASCII, unique, and
    used for login, JWT revocation and Redis keys. The display name is only text on screen,
    so Turkish letters and spaces are allowed and it does not have to be unique (one company
    can have two people with the same name). Never match on it.
    """
    return validate_metin(deger, "Görünen ad", max_uzunluk=60, min_uzunluk=2)

def validate_kullanici_adi(deger: str) -> str:
    """Validate a username: lowercased, 3-40 ASCII characters. It is the login key."""
    if not isinstance(deger, str):
        raise HTTPException(status_code=400, detail="Kullanıcı adı metin olmalı.")
    deger = deger.strip().lower()
    if len(deger) < 3:
        raise HTTPException(status_code=400, detail="Kullanıcı adı en az 3 karakter olmalı.")
    if len(deger) > 40:
        raise HTTPException(status_code=400, detail="Kullanıcı adı en fazla 40 karakter olabilir.")
    if not _KULLANICI_ADI_REGEX.match(deger):
        # The message spells out the Turkish letters on purpose. A shorter "letters only" message
        # confused people who typed a Turkish name, since their input did contain only letters.
        # The ASCII rule itself stays: see the note on _KULLANICI_ADI_REGEX.
        raise HTTPException(
            status_code=400,
            detail="Kullanıcı adı yalnız İngilizce harf, rakam, nokta, alt-tire ve tire içerebilir. "
                   "Türkçe karakter kullanılamaz (ı, ğ, ü, ş, ö, ç) — örn. 'Ayşe_Yılmaz' yerine 'Ayse_Yilmaz'."
        )
    return deger

def validate_plaka(deger: str) -> str:
    """Validate a licence plate. Uppercases it and removes spaces before checking."""
    if not isinstance(deger, str):
        raise HTTPException(status_code=400, detail="Plaka metin olmalı.")
    deger = deger.strip().upper().replace(" ", "")  # "34 ABC 123" -> "34ABC123"
    if len(deger) < 5 or len(deger) > 10:
        raise HTTPException(status_code=400, detail="Plaka 5-10 karakter arasında olmalı.")
    if not _PLAKA_REGEX.match(deger):
        raise HTTPException(status_code=400, detail="Plaka sadece büyük harf ve rakam içerebilir.")
    return deger

# ============================================================
# AUDIT LOG
# ============================================================
async def audit_log_yaz(
    yapan: dict,
    eylem: str,
    hedef_tip: str = None,
    hedef_id: str = None,
    hedef_aciklama: str = None,
    detay: dict = None,
    basarili: bool = True,
    request: Request = None
):
    """Record a sensitive action (deletes, password resets and the like) in audit_log.

    Never raises: if the write fails, the action still goes through and a warning is sent
    to Sentry (fail open, see "Failure handling" in docs/SECURITY-PRIVACY.md).
    """
    try:
        ip_adresi = None
        if request:
            ip_adresi = istemci_ip(request)
        
        kayit = {
            "yapan_kullanici_adi": yapan.get("kullanici_adi") if yapan else None,
            "yapan_rol": yapan.get("rol") if yapan else None,
            "yapan_firma_id": yapan.get("firma_id") if yapan else None,
            "yapan_ip": ip_adresi,
            "eylem": eylem,
            "hedef_tip": hedef_tip,
            "hedef_id": str(hedef_id) if hedef_id else None,
            "hedef_aciklama": hedef_aciklama,
            "detay": detay,
            "basarili": basarili,
            "tarih": datetime.now(timezone.utc).isoformat()
        }
        
        await run_query(supabase.table("audit_log").insert(kayit))
    
    except Exception as e:
        # A failed audit write must never break the request it records; report it instead.
        sentry_sdk.capture_message(
            f"Audit log yazılamadı: {eylem} | Hata: {str(e)[:200]}",
            level="warning"
        )

def yetki_hiyerarsi_kontrol(aktor_rol: str, hedef_rol: str) -> bool:
    """Return True if the actor's role may act on the target's role.

    SUPERADMIN always may; everyone else needs a strictly higher level in ROL_HIYERARSI.
    """
    if aktor_rol == "SUPERADMIN":
        return True
    return ROL_HIYERARSI.get(aktor_rol, 0) > ROL_HIYERARSI.get(hedef_rol, 0)


def hedef_sube_belirle(yetkili: dict, firma_id: str, istek_sube_id=None):
    """Decide which branch (sube) a new operational record (vehicle, brand, ...) belongs to.

    - A user scoped to a branch (sube_id set) always gets their own branch.
    - SUPERADMIN (platform staff) may pick any branch of the company; an unknown branch is an
      error, and no branch means headquarters (NULL). Used for support and setup.
    - A headquarters admin (sube_id NULL) always gets headquarters. They can create branches
      but not operational records inside them; that data belongs to the branch's own manager.
    A company without branches never sends istek_sube_id, so the result is NULL.
    """
    yetkili_sube = yetkili.get("sube_id")
    if yetkili_sube:
        return yetkili_sube
    # Unscoped caller: only SUPERADMIN may target another branch; HQ users stay at HQ (NULL).
    if yetkili.get("rol") == "SUPERADMIN" and istek_sube_id:
        s = supabase.table("subeler").select("id").eq("id", istek_sube_id).eq("firma_id", firma_id).execute().data
        if not s:
            raise HTTPException(status_code=400, detail="Geçersiz şube veya bu firmaya ait değil.")
        return istek_sube_id
    return None


def kapsam_kota_bilgisi(firma_id, sube_id):
    """Return (quota, set of vehicle ids) for a scope: one branch, or headquarters.

    - Branch (sube_id set): quota is subeler.aktif_kota; vehicles are those in that branch.
    - Headquarters (sube_id NULL): vehicles are those without a branch. The quota is
        * toplam_kota for a company without branches (everything belongs to HQ);
        * toplam_kota minus the sum of all branch quotas for a company with branches, i.e.
          only the part not handed out. Giving HQ the full toplam_kota would let the company
          exceed its total once the branches use their own quotas.
    """
    if sube_id:
        row = supabase.table("subeler").select("aktif_kota").eq("id", sube_id).execute().data
        kota = (row[0].get("aktif_kota") or 0) if row else 0
        # Virtual valet vehicles (tip='VALE') do not count against the vehicle quota: the quota
        # is the shuttle pricing basis, and valet staff are billed by their own quota.
        arac_ids = {a["id"] for a in supabase.table("araclar").select("id, tip").eq("sube_id", sube_id).execute().data
                    if (a.get("tip") or "SERVIS") != "VALE"}
    else:
        firma_row = supabase.table("firmalar").select("toplam_kota, subeli").eq("id", firma_id).execute().data
        toplam = (firma_row[0].get("toplam_kota") or 1) if firma_row else 1
        if firma_row and firma_row[0].get("subeli"):
            dagitilmis = sum((s.get("aktif_kota") or 0) for s in supabase.table("subeler").select("aktif_kota").eq("firma_id", firma_id).execute().data)
            kota = max(0, toplam - dagitilmis)
        else:
            kota = toplam
        # Virtual valet vehicles are excluded here too (see above).
        arac_ids = {a["id"] for a in supabase.table("araclar").select("id, sube_id, tip").eq("firma_id", firma_id).execute().data
                    if not a.get("sube_id") and (a.get("tip") or "SERVIS") != "VALE"}
    return kota, arac_ids


def kapsam_sube_filtrele(yetkili: dict, kayitlar):
    """Read isolation: keep only the records in the caller's own branch scope.

    - SUPERADMIN: no filter. This is the only cross-branch view, used for support and audits.
    - A user with sube_id set: records of that branch only.
    - A headquarters user (sube_id NULL) in a company with branches: HQ records only
      (sube_id NULL). Headquarters is treated as one more branch, not as a supervisor, so it
      does not see the other branches' data.
    - A company without branches: every record has sube_id NULL, so everything is visible.
    `kayitlar` is a list of dicts that each have a 'sube_id' key.
    """
    if yetkili.get("rol") == "SUPERADMIN":
        return kayitlar
    ysube = yetkili.get("sube_id")
    return [k for k in kayitlar if k.get("sube_id") == ysube]


def referans_konum(firma_id, sube_id=None, firma_merkez=None):
    """Return (lat, lng) of the reference point for routing: where vehicles start and return.

    That is the branch location if the vehicle or route belongs to a branch with a location
    set, otherwise the company headquarters. Pass firma_merkez=(lat, lng) when the caller
    already has it, to skip a second query on hot paths.
    """
    if sube_id:
        s = supabase.table("subeler").select("konum_lat, konum_lng").eq("id", sube_id).execute().data
        if s and s[0].get("konum_lat") is not None and s[0].get("konum_lng") is not None:
            return s[0]["konum_lat"], s[0]["konum_lng"]
    if firma_merkez is not None:
        return firma_merkez
    f = supabase.table("firmalar").select("merkez_lat, merkez_lng").eq("id", firma_id).execute().data
    return (f[0].get("merkez_lat"), f[0].get("merkez_lng")) if f else (None, None)


def sube_yazma_guard(yetkili: dict, hedef_sube_id):
    """Write isolation: raise 403 unless the record is in the caller's own branch scope.

    Same rules as kapsam_sube_filtrele: SUPERADMIN may write anywhere, a branch user only in
    their branch, a headquarters user only in HQ records (sube_id NULL). In a company without
    branches both sides are NULL, so the check always passes. Use this in update and delete
    endpoints; kapsam_sube_filtrele covers reads.
    """
    if yetkili.get("rol") == "SUPERADMIN":
        return
    if (hedef_sube_id or None) != (yetkili.get("sube_id") or None):
        raise HTTPException(status_code=403, detail="Bu kayıt başka bir şubeye ait; işlem yapılamaz.")


def sofor_kendi_araci_mi(yetkili: dict, arac_id) -> bool:
    """Field roles (SOFOR, VALE) may only act on the vehicle currently assigned to them.

    The arac_id inside the JWT can be stale: a vehicle assigned after login does not reissue
    the token. So the current assignment is read from kullanicilar, which also makes
    reassignment safe. Office roles always get True; company isolation is checked separately.
    VALE must stay in this check. Without it, a valet could load the route of any shuttle
    vehicle in their company (exposing passenger personal data) and start or end its trips.
    The valet app only uses the /vale-* endpoints, so the restriction costs nothing.
    """
    if yetkili.get("rol") not in ("SOFOR", "VALE"):
        return True
    row = supabase.table("kullanicilar").select("arac_id").eq("kullanici_adi", yetkili.get("kullanici_adi")).execute().data
    return bool(row) and row[0].get("arac_id") == arac_id


# Company active/inactive status, cached in Redis so authentication does not hit the database on
# every request.
def _firma_pasif_db_kontrol(firma_id: str) -> bool:
    """Read the company's status from the database (used on a cache miss)."""
    if not firma_id:
        return False
    f_res = supabase.table("firmalar").select("is_active").eq("id", firma_id).execute()
    # A company that no longer exists counts as inactive. Otherwise users of a deleted company
    # would keep passing authentication for the rest of their token lifetime (24 hours, or
    # 7 days for drivers).
    if not f_res.data:
        return True
    return f_res.data[0].get("is_active") is False

async def firma_pasif_mi(firma_id: str) -> bool:
    """Return True if the company is inactive or deleted. Redis first, database on a miss.

    Inactive results are cached for 60 seconds and active ones for 1800 seconds. Changing a
    company's status calls firma_cache_invalidate, so the long TTL does not delay it.
    """
    if not firma_id:
        return False

    cache_key = f"firma_pasif:{firma_id}"
    try:
        cached = await redis_client.get(cache_key)
        if cached is not None:
            return cached == "1"
    except Exception as e:
        # Redis is down: report it and fall back to the database.
        sentry_sdk.capture_exception(e)

    # Not wrapped in try/except on purpose: if the database is unreachable too, the request fails
    # instead of serving a company whose status is unknown ("Failure handling" in docs/SECURITY-PRIVACY.md).
    pasif = await run_in_threadpool(_firma_pasif_db_kontrol, firma_id)

    try:
        await redis_client.setex(cache_key, 60 if pasif else 1800, "1" if pasif else "0")
    except Exception:
        pass  # the result is still valid even if caching it failed

    return pasif

async def firma_cache_invalidate(firma_id: str):
    """Drop the cached company status after it changes, so the change applies at once."""
    if not firma_id:
        return
    try:
        await redis_client.delete(f"firma_pasif:{firma_id}")
    except Exception:
        pass  # the cached value expires on its own (TTL) and the database stays the source of truth
    # Also clear the in-process auth cache. Status changes are rare, so clearing all of it is fine.
    _auth_cache.clear()

# ============================================================
# TOKEN REVOCATION
# ============================================================
async def token_revoked_mi(jti: str) -> bool:
    """Return True if this token's jti is in revoked_tokens (filled on logout).

    Cached in Redis so authentication does not query the database on every call.
    """
    if not jti:
        return False  # tokens issued before jti existed cannot be checked

    cache_key = f"revoked:{jti}"

    try:
        cached = await redis_client.get(cache_key)
        if cached is not None:
            return cached == "1"  # decode_responses=True, so values are always str
    except Exception:
        pass  # Redis error: fall back to the database

    try:
        res = await run_query(supabase.table("revoked_tokens").select("jti").eq("jti", jti))
        is_revoked = bool(res.data)

        # Cache revoked for 1 hour and not-revoked for 5 minutes.
        try:
            ttl = 3600 if is_revoked else 300
            await redis_client.setex(cache_key, ttl, "1" if is_revoked else "0")
        except Exception:
            pass  # the result is still valid even if caching it failed
        
        return is_revoked
    except Exception:
        # Database error: fail open and treat the token as valid, so an outage does not lock
        # every user out. Rationale and risk: "Failure handling" in docs/SECURITY-PRIVACY.md.
        return False
    
async def kullanicinin_tum_tokenlarini_revoke_et(
    kullanici_adi: str, 
    sebep: str, 
    iptal_eden: str = None
):
    """Invalidate every token issued to a user so far (password change, account changes).

    Issued jti values are not stored, so the tokens cannot be revoked one by one. Instead a
    per-user cutoff time is recorded, and yetki_kontrol rejects any token issued before it.
    """
    try:
        gecersizlestirme_zamani = datetime.now(timezone.utc).isoformat()

        # The cutoff is written to the database as well as Redis, so it survives a Redis flush.
        # It lives in its own table rather than a column on kullanicilar, because deleting the
        # user row would otherwise delete the cutoff with it.
        try:
            await run_query(supabase.table("kullanici_token_iptal").upsert({
                "kullanici_adi": kullanici_adi,
                "gecersiz_before": gecersizlestirme_zamani,
                "sebep": sebep,
                "guncelleme_tarihi": gecersizlestirme_zamani
            }))
        except Exception as e:
            sentry_sdk.capture_exception(e)  # still write the Redis copy below

        cache_key = f"user_token_invalidated:{kullanici_adi}"

        # Redis copy for the fast path. 24 hours matches the token lifetime of office roles.
        await redis_client.setex(cache_key, 86400, gecersizlestirme_zamani)
        # Clear the in-process auth cache so the change applies immediately (rare event).
        _auth_cache.clear()

        return True
    except Exception as e:
        sentry_sdk.capture_exception(e)
        return False

async def kullanici_gecersiz_zamani(kullanici_adi: str):
    """Return the user's token cutoff time as an ISO string, or None if there is none.

    Reads Redis (`user_token_invalidated:{name}`) first and the database
    (`kullanici_token_iptal`) on a miss, so the cutoff survives a Redis flush. A database
    result is written back to Redis, with "0" meaning "no cutoff", so later requests stay
    off the database. Same pattern as firma_pasif_mi.
    """
    if not kullanici_adi:
        return None
    cache_key = f"user_token_invalidated:{kullanici_adi}"
    try:
        cached = await redis_client.get(cache_key)
        if cached is not None:
            return None if cached == "0" else cached
    except Exception:
        pass  # Redis is down: use the database
    val = None
    try:
        row = (await run_query(supabase.table("kullanici_token_iptal").select("gecersiz_before").eq("kullanici_adi", kullanici_adi))).data
        val = row[0].get("gecersiz_before") if row else None
    except Exception:
        # Database error too: fail open so an outage does not lock everyone out ("Failure handling" in docs/SECURITY-PRIVACY.md).
        return None
    try:
        await redis_client.setex(cache_key, 86400, val or "0")
    except Exception:
        pass  # the result is still valid even if caching it failed
    return val

# ============================================================
# IN-PROCESS AUTH CACHE
# A token that passed every check in the last _AUTH_CACHE_TTL seconds is accepted again without
# going to Redis. The server runs a single worker process, so one dict is consistent for all
# requests. This absorbs the bursts of parallel API calls a panel makes on load and refresh.
# ============================================================
_auth_cache: dict = {}
# 30 seconds is safe because every revocation path evicts: logout removes its own jti, and
# company deactivation and per-user invalidation clear the whole cache (see
# firma_cache_invalidate and kullanicinin_tum_tokenlarini_revoke_et).
_AUTH_CACHE_TTL = 30

def _auth_cache_al(jti):
    if not jti:
        return None
    kayit = _auth_cache.get(jti)
    if kayit and kayit[0] > time.time():
        return kayit[1]
    if kayit:
        _auth_cache.pop(jti, None)  # expired
    return None

def _auth_cache_yaz(jti, payload):
    if not jti:
        return
    # Size guard: once the dict grows past 2000 entries, sweep out expired ones.
    if len(_auth_cache) > 2000:
        simdi = time.time()
        for k in [k for k, v in _auth_cache.items() if v[0] <= simdi]:
            _auth_cache.pop(k, None)
    _auth_cache[jti] = (time.time() + _AUTH_CACHE_TTL, payload)

async def yetki_kontrol(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """FastAPI dependency that authenticates a staff request and returns the JWT payload.

    After the signature check it applies three revocation checks: the company is active
    (402 if not), the token's jti was not logged out, and the token was issued after the
    user's invalidation cutoff. The three Redis keys are fetched with one MGET, and each
    check falls back to the database on a cache miss.
    """
    token = credentials.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])

        jti = payload.get("jti")

        cached_payload = _auth_cache_al(jti)
        if cached_payload is not None:
            return cached_payload

        firma_id = payload.get("firma_id")
        kullanici_adi_check = payload.get("kullanici_adi")
        token_iat = payload.get("iat")  # issue time, epoch seconds

        # One MGET for all three keys instead of three GETs.
        istenecek = []
        if firma_id:
            istenecek.append(("firma", f"firma_pasif:{firma_id}"))
        if jti:
            istenecek.append(("revoked", f"revoked:{jti}"))
        if kullanici_adi_check:
            istenecek.append(("inval", f"user_token_invalidated:{kullanici_adi_check}"))

        cache_degerleri = {}
        if istenecek:
            try:
                sonuclar = await redis_client.mget([k for _, k in istenecek])
                cache_degerleri = {ad: sonuclar[i] for i, (ad, _) in enumerate(istenecek)}
            except Exception:
                cache_degerleri = {}  # Redis down: treat all as misses and use the database below

        # In the checks below, None means a cache miss and triggers the database helper.
        if firma_id:
            firma_val = cache_degerleri.get("firma")
            pasif = (firma_val == "1") if firma_val is not None else await firma_pasif_mi(firma_id)
            if pasif:
                raise HTTPException(status_code=402, detail="Firma pasif durumda. Lütfen ödemenizi kontrol edin.")

        if jti:
            revoked_val = cache_degerleri.get("revoked")
            revoked = (revoked_val == "1") if revoked_val is not None else await token_revoked_mi(jti)
            if revoked:
                raise HTTPException(status_code=401, detail="Bu token iptal edilmiş. Lütfen tekrar giriş yapın.")

        # Per-user cutoff (set on password change, account deletion and similar).
        if kullanici_adi_check and token_iat:
            gecersiz_zaman_str = cache_degerleri.get("inval")
            # "0" is the cached "no cutoff" marker; None is a miss and goes to the database.
            if gecersiz_zaman_str == "0":
                gecersiz_zaman_str = None
            elif gecersiz_zaman_str is None:
                gecersiz_zaman_str = await kullanici_gecersiz_zamani(kullanici_adi_check)
            if gecersiz_zaman_str:
                try:
                    # The value is either our ISO string (Redis) or Supabase's format (database).
                    gecersiz_dt = supabase_tarih_parse(gecersiz_zaman_str)
                    token_iat_dt = datetime.fromtimestamp(token_iat, tz=timezone.utc)
                    if token_iat_dt < gecersiz_dt:
                        raise HTTPException(status_code=401, detail="Hesabınızda değişiklik yapıldı. Lütfen tekrar giriş yapın.")
                except HTTPException:
                    raise
                except Exception:
                    pass  # unparseable cutoff: fail open and accept the token ("Failure handling" in docs/SECURITY-PRIVACY.md)

        _auth_cache_yaz(jti, payload)
        return payload

    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Pasaportun süresi dolmuş.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Geçersiz pasaport!")

def token_olustur(data: dict):
    """Issue a signed JWT with exp, iat and a random jti added to `data`.

    Field roles (SOFOR, VALE) get 7 days so drivers are not logged out mid-week on a phone;
    office roles get 24 hours. A stolen token can still be cut off at once through the
    revocation checks in yetki_kontrol.
    """
    to_encode = data.copy()
    simdi = datetime.now(timezone.utc)

    rol = data.get("rol", "")
    if rol in ("SOFOR", "VALE"):
        sure = timedelta(days=7)
    else:
        sure = timedelta(hours=24)
    
    to_encode.update({
        "exp": simdi + sure,
        "iat": simdi,
        "jti": str(uuid.uuid4())
    })
    return jwt.encode(to_encode, JWT_SECRET, algorithm=ALGORITHM)

def sifreyi_hashle(sifre: str) -> str:
    return bcrypt.hashpw(sifre.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def sifreyi_dogrula(duz_sifre: str, hashli_sifre: str) -> bool:
    try: return bcrypt.checkpw(duz_sifre.encode('utf-8'), hashli_sifre.encode('utf-8'))
    except Exception: return False

def mesafe_hesapla(lat1, lon1, lat2, lon2):
    """Great-circle (haversine) distance in kilometres. Straight line, not road distance."""
    R = 6371.0
    dlon, dlat = math.radians(lon2 - lon1), math.radians(lat2 - lat1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    return R * (2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))

# "Today" and other day boundaries are always computed in Turkish time (UTC+3, no DST),
# regardless of the server's own time zone. Timestamps are stored in UTC.
TR = ZoneInfo("Europe/Istanbul")

def tr_bugun_baslangic_utc_iso() -> str:
    """Return today's 00:00 in Turkey as a UTC ISO string, for gte() filters in queries."""
    bugun_tr = datetime.now(TR).replace(hour=0, minute=0, second=0, microsecond=0)
    return bugun_tr.astimezone(timezone.utc).isoformat()

def supabase_tarih_parse(s: str) -> datetime:
    """Parse a timestamp as returned by Supabase into an aware datetime (UTC if no zone).

    Supabase returns values like '2026-05-12 15:15:51.21136+00': a space instead of 'T', a
    short '+00' offset and a variable number of fractional digits. Older Python versions of
    fromisoformat() reject all three, so the string is normalised to
    '2026-05-12T15:15:51.211360+00:00' first. A trailing 'Z' is accepted too.
    """
    if not s:
        raise ValueError("Boş tarih")
    fixed = s.replace('Z', '+00:00')
    fixed = fixed.replace(' ', 'T')
    if fixed.endswith('+00') or fixed.endswith('-00'):
        fixed = fixed + ':00'
    # Pad or truncate the fraction to exactly 6 digits.
    m = re.match(r'^(.*?T\d{2}:\d{2}:\d{2})\.(\d+)(.*)$', fixed)
    if m:
        base, frac, rest = m.groups()
        frac = frac[:6].ljust(6, '0')
        fixed = f'{base}.{frac}{rest}'
    dt = datetime.fromisoformat(fixed)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

def musteri_linki_suresi_doldu(token: str, talep: dict) -> bool:
    """True if a customer link is past its lifetime, counted from when the request was opened:
    MUSTERI_LINK_OMRU_SAAT for shuttle, VALE_LINK_OMRU_SAAT for valet.

    Every endpoint that accepts a customer token and returns data or writes a location calls
    this. Only riza-red and riza-geri-cek skip it on purpose: they only reduce the data held,
    and withdrawing consent must stay possible after the link has expired.
    Tokens starting with "DONUS-" (synthetic return rows) or "BTT-" (destroyed links) are not
    customer links. An unparseable date lets the link through rather than lock the customer out
    (fail open, see "Failure handling" in docs/SECURITY-PRIVACY.md).
    """
    if token.startswith("DONUS-") or token.startswith("BTT-") or not talep.get("kayit_tarihi"):
        return False
    try:
        kayit_dt = supabase_tarih_parse(talep["kayit_tarihi"])
    except (ValueError, TypeError) as e:
        sentry_sdk.capture_exception(e)
        return False
    omur = (VALE_LINK_OMRU_SAAT
            if str(talep.get("gorev_tipi") or "").startswith("VALE_")
            else MUSTERI_LINK_OMRU_SAAT)
    return (datetime.now(timezone.utc) - kayit_dt) > timedelta(hours=omur)

def tr_gun_oncesi_baslangic_utc_iso(gun: int) -> str:
    """Return 00:00 Turkish time `gun` days ago as a UTC ISO string (weekly/monthly reports)."""
    bugun_tr = datetime.now(TR).replace(hour=0, minute=0, second=0, microsecond=0)
    onceki_tr = bugun_tr - timedelta(days=gun)
    return onceki_tr.astimezone(timezone.utc).isoformat()

async def yol_mesafesi_verisi_async(start_lat, start_lng, end_lat, end_lng):
    """Return {"km", "dakika"} (minutes) for driving from start to end, using Mapbox Directions.

    Tries the live-traffic profile first and falls back to plain "driving". Results are
    cached in Redis for CACHE_TTL. On any failure it returns zeros rather than raising,
    so callers must treat 0 minutes as "unknown".
    """
    # Round coordinates to 4 decimals (about 11 m) so nearby requests share a cache entry.
    cache_key = f"rota:{round(start_lat, 4)},{round(start_lng, 4)}_{round(end_lat, 4)},{round(end_lng, 4)}"

    try:
        cached_data = await redis_client.get(cache_key)
        if cached_data:
            return json.loads(cached_data)
    except Exception as e:
        logger.warning("Redis read failed, continuing without the route cache: %s", e)

    if not MAPBOX_API_KEY:
        logger.error("MAPBOX_API_KEY is not set")
        return {"km": 0, "dakika": 0}

    async with httpx.AsyncClient() as client:
        url_traffic = f"https://api.mapbox.com/directions/v5/mapbox/driving-traffic/{start_lng},{start_lat};{end_lng},{end_lat}"
        params = {"access_token": MAPBOX_API_KEY, "overview": "false"}
        
        try:
            res = await client.get(url_traffic, params=params, timeout=5.0)
            if res.status_code == 200:
                data = res.json()
                if data.get("code") == "Ok" and len(data["routes"]) > 0:
                    sonuc = {
                        "km": round(data['routes'][0]['distance'] / 1000, 2),
                        "dakika": round(data['routes'][0]['duration'] / 60)
                    }
                    try:
                        await redis_client.setex(cache_key, CACHE_TTL, json.dumps(sonuc))
                    except Exception:
                        pass  # the result is still valid even if caching it failed
                    return sonuc
        except Exception:
            pass  # fall through to the plain driving profile below
            
        # Live-traffic profile failed or returned nothing: retry with the plain driving profile.
        url_driving = f"https://api.mapbox.com/directions/v5/mapbox/driving/{start_lng},{start_lat};{end_lng},{end_lat}"
        try:
            res = await client.get(url_driving, params=params, timeout=5.0)
            if res.status_code == 200:
                data = res.json()
                if data.get("code") == "Ok" and len(data["routes"]) > 0:
                    sonuc = {
                        "km": round(data['routes'][0]['distance'] / 1000, 2),
                        "dakika": round(data['routes'][0]['duration'] / 60)
                    }
                    try:
                        await redis_client.setex(cache_key, CACHE_TTL, json.dumps(sonuc))
                    except Exception:
                        pass  # the result is still valid even if caching it failed
                    return sonuc
        except Exception as e:
            logger.error("Mapbox request failed: %s", e)

    return {"km": 0, "dakika": 0}
    
async def mail_gonder(hedef_mail, kullanici_adi, link, firma_adi, marka, rol="ADMIN"):
    """Send an account invitation e-mail through the Resend API. Returns True on success.

    Subject, heading and colour depend on the invited role. Uses an async HTTP client so the
    event loop is not blocked while the mail is sent.
    """
    try:
        api_key = os.getenv("RESEND_API_KEY")
        if not api_key:
            logger.error("RESEND_API_KEY is not set")
            return False

        ROL_ICERIK = {
            "SUPERADMIN": {
                "konu": "Shuttle & Valet Ops Süper Yönetici Davet",
                "vurgu": "Süper Yönetici",
                "aciklama": "Tüm sistem yönetimine, firma kurulumuna ve global ayarlara erişim yetkiniz bulunmaktadır.",
                "renk": "#7c3aed"  # purple
            },
            "ADMIN": {
                "konu": f"{firma_adi} — Shuttle & Valet Ops Yönetici Davetiniz",
                "vurgu": "Firma Yöneticisi",
                "aciklama": "Firmanızın araç filosunu, personelini ve operasyonlarını tek panelden yönetebileceksiniz.",
                "renk": "#2563eb"  # blue
            },
            "OPERASYON": {
                "konu": f"{firma_adi} — Operasyon Sorumlusu Davetiniz",
                "vurgu": "Operasyon Sorumlusu",
                "aciklama": "Filo araçlarını, hareket saatlerini ve günlük operasyonu canlı olarak takip edebileceksiniz.",
                "renk": "#0891b2"  # teal
            },
            "DANISMAN": {
                "konu": f"{firma_adi} — Danışman Davetiniz",
                "vurgu": "Müşteri Danışmanı",
                "aciklama": "Müşteri yolculuklarını oluşturabilir, mevcut talepleri canlı takip edebilir ve müşteri iletişimini yönetebilirsiniz.",
                "renk": "#059669"  # green
            },
            "SOFOR": {
                "konu": f"{firma_adi} — Şoför Hesabınız Hazır",
                "vurgu": "Şoför",
                "aciklama": "Günlük yolcu listenizi, optimum rotanızı ve görev durumunuzu telefonunuzdan takip edebileceksiniz.",
                "renk": "#ea580c"  # orange
            },
            # Every role in ROL_HIYERARSI needs an entry; a missing role falls back to the ADMIN
            # text below and the person is invited as a company administrator.
            "VALE": {
                "konu": f"{firma_adi} — Vale Hesabınız Hazır",
                "vurgu": "Vale",
                "aciklama": "Size atanan araç alım ve teslim görevlerini, müşteri adreslerine navigasyonu ve görev durumunuzu telefonunuzdan takip edebileceksiniz.",
                "renk": "#ca8a04"  # dark gold, like the valet marker on the admin map
            }
        }
        
        rol_bilgi = ROL_ICERIK.get(rol, ROL_ICERIK["ADMIN"])  # unknown roles fall back to the ADMIN text
        konu = rol_bilgi["konu"]
        
        # Escape every user-supplied value that goes into the HTML body.
        safe_kullanici = html.escape(kullanici_adi or "")
        safe_firma = html.escape(firma_adi or "")
        safe_vurgu = html.escape(rol_bilgi["vurgu"])
        safe_aciklama = html.escape(rol_bilgi["aciklama"])
        rol_renk = rol_bilgi["renk"]

        icerik = f"""
        <div style="font-family: -apple-system, Segoe UI, Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #eaeaea; border-radius: 10px; box-shadow: 0 4px 8px rgba(0,0,0,0.05); background: #fff;">
            <div style="text-align: center; margin-bottom: 24px;">
                <div style="display: inline-block; padding: 6px 14px; background: {rol_renk}15; color: {rol_renk}; border-radius: 20px; font-size: 12px; font-weight: 600; letter-spacing: 0.5px; text-transform: uppercase; margin-bottom: 12px;">{safe_vurgu}</div>
                <h2 style="color: #1a1a1a; margin: 8px 0 0 0; font-size: 24px;">Aramıza Hoş Geldiniz, {safe_kullanici}!</h2>
            </div>

            <p style="color: #444; font-size: 15px; line-height: 1.6;">
                Sizi <b>{safe_firma}</b> bünyesinde <b>{safe_vurgu}</b> olarak Shuttle &amp; Valet Ops sistemine davet ediyoruz.
            </p>

            <p style="color: #444; font-size: 15px; line-height: 1.6;">
                {safe_aciklama}
            </p>

            <div style="background: #f8f9fb; border: 1px solid #eaeaea; border-radius: 8px; padding: 14px 18px; margin: 22px 0; text-align: center;">
                <div style="font-size: 12px; color: #999; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px;">Giriş Kullanıcı Adınız</div>
                <div style="font-size: 18px; font-weight: 700; color: #1a1a1a; font-family: 'SFMono-Regular', Consolas, monospace;">{safe_kullanici}</div>
                <div style="font-size: 12px; color: #999; margin-top: 6px;">Şifrenizi belirledikten sonra girişte bu kullanıcı adını kullanın.</div>
            </div>

            <div style="text-align: center; margin: 36px 0;">
                <a href="{link}" style="background-color: {rol_renk}; color: white; padding: 14px 32px; text-decoration: none; border-radius: 6px; font-weight: bold; font-size: 16px; display: inline-block; box-shadow: 0 2px 6px {rol_renk}40;">Hesabımı Aktive Et</a>
            </div>

            <p style="font-size: 12px; color: #999; word-break: break-all; line-height: 1.5;">
                Butona tıklayamıyorsanız, aşağıdaki linki tarayıcınıza yapıştırın:<br>
                <span style="color: #666;">{link}</span>
            </p>

            <div style="background: #fafafa; border-left: 3px solid {rol_renk}; padding: 12px 16px; margin: 24px 0; border-radius: 4px;">
                <small style="color: #666; font-size: 12px;">
                    🔒 Bu davet linki <b>7 gün</b> içinde kullanılmazsa devre dışı kalır.
                </small>
            </div>

            <hr style="border: none; border-top: 1px solid #eaeaea; margin: 32px 0 16px 0;">

            <p style="font-size: 11px; color: #aaa; text-align: center; line-height: 1.5;">
                Shuttle &amp; Valet Ops Lojistik Yönetim Sistemi<br>
                Bu otomatik bir mesajdır, lütfen yanıtlamayınız.
            </p>
        </div>
        """

        payload = {
            # Sender shown on outgoing e-mail. Use a domain you have verified in Resend
            # (https://resend.com/domains); mail from an unverified domain is rejected.
            "from": "YOUR_APP_NAME <noreply@YOUR_DOMAIN>",
            "to": [hedef_mail],
            "subject": konu,
            "html": icerik
        }

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post("https://api.resend.com/emails", json=payload, headers=headers)

        # Resend answers with either 200 or 202.
        if response.status_code in (200, 202):
            logger.info("Invitation email sent")
            return True
        else:
            logger.error("Resend API error %s: %s", response.status_code, response.text[:200])
            return False

    except Exception as e:
        logger.error("Invitation email failed: %s", e)
        sentry_sdk.capture_exception(e)
        return False

async def basit_mail_gonder(hedef_mail, konu, html_icerik):
    """Send one e-mail with a ready-made HTML body through Resend. Returns True on success."""
    try:
        api_key = os.getenv("RESEND_API_KEY")
        if not api_key:
            logger.error("RESEND_API_KEY is not set")
            return False
        payload = {
            # Sender shown on outgoing e-mail. Use a domain you have verified in Resend
            # (https://resend.com/domains); mail from an unverified domain is rejected.
            "from": "YOUR_APP_NAME <noreply@YOUR_DOMAIN>",
            "to": [hedef_mail],
            "subject": konu,
            "html": html_icerik
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post("https://api.resend.com/emails", json=payload, headers=headers)
        return response.status_code in (200, 202)
    except Exception as e:
        logger.error("Email failed: %s", e)
        sentry_sdk.capture_exception(e)
        return False

@app.post("/toplu-mail-gonder")
async def toplu_mail_gonder(istek: TopluMailIstek, request: Request, yetkili = Depends(yetki_kontrol)):
    """SUPERADMIN only: send an announcement to every ADMIN user that has an e-mail address."""
    if yetkili["rol"] != "SUPERADMIN":
        raise HTTPException(status_code=403, detail="Yetkisiz.")
    konu = (istek.konu or "").strip().replace("\n", " ").replace("\r", " ")
    mesaj = (istek.mesaj or "").strip()
    if not konu or not mesaj:
        raise HTTPException(status_code=400, detail="Konu ve mesaj zorunludur.")
    if len(konu) > 200:
        raise HTTPException(status_code=400, detail="Konu çok uzun (en fazla 200 karakter).")
    if len(mesaj) > 5000:
        raise HTTPException(status_code=400, detail="Mesaj çok uzun (en fazla 5000 karakter).")

    adminler = (await run_query(supabase.table("kullanicilar").select("kullanici_adi, email").eq("rol", "ADMIN"))).data
    alicilar = [a for a in adminler if (a.get("email") or "").strip()]
    if not alicilar:
        raise HTTPException(status_code=400, detail="E-posta adresi tanımlı admin bulunamadı.")

    # Escape the message, then turn newlines into <br> so plain text keeps its line breaks.
    safe_mesaj = html.escape(mesaj).replace("\n", "<br>")
    safe_konu = html.escape(konu)
    icerik = f"""
    <div style="font-family: -apple-system, Segoe UI, Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 24px; border: 1px solid #eaeaea; border-radius: 10px; background: #fff;">
        <div style="text-align: center; margin-bottom: 20px;">
            <span style="font-weight: 800; font-size: 22px; color: #0a0a0a;">pax<span style="background:#DFFF00; color:#0a0a0a; padding:1px 7px; border-radius:5px;">route</span></span>
        </div>
        <h2 style="color:#1a1a1a; font-size:19px; margin:0 0 16px 0;">{safe_konu}</h2>
        <div style="color:#333; font-size:15px; line-height:1.6;">{safe_mesaj}</div>
        <hr style="border:none; border-top:1px solid #eaeaea; margin:28px 0 14px 0;">
        <p style="font-size:11px; color:#aaa; text-align:center; line-height:1.5;">
            Shuttle &amp; Valet Ops — Servis &amp; Shuttle Yönetim Platformu<br>Bu bir bilgilendirme mesajıdır.
        </p>
    </div>
    """
    basarili = 0
    for a in alicilar:
        if await basit_mail_gonder(a["email"], konu, icerik):
            basarili += 1
    basarisiz = len(alicilar) - basarili

    await audit_log_yaz(
        yapan=yetkili, eylem="TOPLU_MAIL", hedef_tip="SISTEM", hedef_id="TOPLU_MAIL",
        hedef_aciklama=f"toplu mail gönderildi: {basarili}/{len(alicilar)} admin",
        detay={"konu": konu, "toplam": len(alicilar), "basarili": basarili, "basarisiz": basarisiz},
        request=request
    )
    sonuc = f"{basarili} yöneticiye gönderildi."
    if basarisiz:
        sonuc += f" {basarisiz} gönderim başarısız oldu."
    return {"mesaj": sonuc, "basarili": basarili, "basarisiz": basarisiz, "toplam": len(alicilar)}

@app.post("/mail-davet-at")
@limiter.limit("10/minute")
async def mail_davet_at(istek: MailDavetIstek, request: Request, yetkili = Depends(yetki_kontrol)):
    """E-mail the invitation link of an existing, not yet activated user.

    Flow: /kullanici-ekle creates the user and returns the invitation link; if the admin
    then clicks "send mail" in the dialog, the panel calls this endpoint. The client sends
    only the username. The address, token and link are all read or built on the server, so
    a caller cannot redirect the invitation elsewhere.
    """
    if yetkili.get("rol") in ["SOFOR", "DANISMAN"]:
        raise HTTPException(status_code=403, detail="Mail gönderme yetkiniz yok.")
    
    hedef_res = await run_query(supabase.table("kullanicilar").select("kullanici_adi, email, davet_token, firma_id, rol, marka").eq("kullanici_adi", istek.kullanici_adi))
    if not hedef_res.data:
        raise HTTPException(status_code=404, detail="Kullanıcı bulunamadı.")
    hedef = hedef_res.data[0]

    if yetkili["rol"] != "SUPERADMIN" and hedef.get("firma_id") != yetkili.get("firma_id"):
        raise HTTPException(status_code=403, detail="Bu kullanıcıya yetkiniz yok.")

    if not yetki_hiyerarsi_kontrol(yetkili["rol"], hedef["rol"]):
        raise HTTPException(status_code=403, detail="Eşit veya yüksek yetkili kullanıcıya işlem yapamazsınız.")

    hedef_email = hedef.get("email", "")
    if not hedef_email or not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", hedef_email):
        raise HTTPException(status_code=400, detail="Bu kullanıcı için geçerli bir e-posta yok.")
    
    # A user who already set a password has no invitation token left.
    davet_token = hedef.get("davet_token")
    if not davet_token:
        raise HTTPException(status_code=400, detail="Bu kullanıcı zaten aktifleşmiş, davet maili gönderilemez.")

    firma_res = await run_query(supabase.table("firmalar").select("firma_adi").eq("id", hedef.get("firma_id")))
    firma_adi = firma_res.data[0]["firma_adi"] if firma_res.data else "Sistem"

    davet_linki = f"{BASE_URL.rstrip('/')}/sifre.html?token={davet_token}"
    
    kullanici_marka = hedef.get("marka") or "Genel"

    basarili = await mail_gonder(
        hedef_mail=hedef_email,
        kullanici_adi=hedef["kullanici_adi"],
        link=davet_linki,
        firma_adi=firma_adi,
        marka=kullanici_marka,
        rol=hedef.get("rol", "ADMIN")
    )
    
    if not basarili:
        raise HTTPException(status_code=500, detail="Mail gönderilemedi.")
    
    return {"mesaj": f"{hedef_email} adresine davet maili gönderildi."}

@app.get("/rota-baslat")
async def rota_baslat(arac_id: str, lat: float, lng: float, yetkili = Depends(yetki_kontrol)):
    """Driver app: start the shuttle trip. The driver's current position becomes the start point.

    The vehicle is marked active (rota_aktif) and its position fields are set from lat/lng,
    which is the starting point for the first leg's distance.
    """
    # lat/lng are written straight into the vehicle row, so reject impossible values.
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
        raise HTTPException(status_code=400, detail="Geçersiz harita koordinatları.")
    if not (await run_in_threadpool(sofor_kendi_araci_mi, yetkili, arac_id)):
        raise HTTPException(status_code=403, detail="Sadece atandığınız araçta işlem yapabilirsiniz.")
    if yetkili["rol"] != "SUPERADMIN":
        if not (k:=(await run_query(supabase.table("araclar").select("firma_id").eq("id", arac_id)))).data or k.data[0].get("firma_id") != yetkili.get("firma_id"): raise HTTPException(status_code=403, detail="Yetkisiz.")

    await run_query(supabase.table("araclar").update({
        "rota_aktif": True,
        "durum": "GÖREVDE",
        "son_durak_lat": lat,  # last completed stop: the first leg is measured from here
        "son_durak_lng": lng,
        "son_lat": lat,        # last known position, shown on the map
        "son_lng": lng,
        "son_hareket_zamani": datetime.now(timezone.utc).isoformat(),
        "hareket_saati": None
    }).eq("id", arac_id))
    
    await manager.broadcast_firma(yetkili.get("firma_id"), "YENILE")
    await manager.broadcast_arac(arac_id, "YENILE") 
    return {"mesaj": "Rota aktif."}

@app.post("/rota-bitir")
async def rota_bitir(arac_id: str, merkeze_donus: bool = True, yetkili = Depends(yetki_kontrol)):
    """Driver app: end the shuttle trip.

    With merkeze_donus (return to base), a synthetic "MERKEZE DONUS" request row records the
    empty return distance for reports, and passengers are left as they are. Without it, the
    trip ends where the vehicle is and the remaining passengers are closed out here.
    This endpoint does several updates without a transaction; replays are made safe by the
    rota_aktif check below, but a failure halfway can leave partial state.
    """
    arac_data = (await run_query(supabase.table("araclar").select("son_durak_lat, son_durak_lng, firma_id, rota_aktif, sube_id").eq("id", arac_id))).data
    if not arac_data:
        raise HTTPException(status_code=404, detail="Araç bulunamadı.")
    arac = arac_data[0]
    if yetkili["rol"] != "SUPERADMIN" and arac.get("firma_id") != yetkili.get("firma_id"):
        raise HTTPException(status_code=403, detail="Yetkisiz.")
    if not (await run_in_threadpool(sofor_kendi_araci_mi, yetkili, arac_id)):
        raise HTTPException(status_code=403, detail="Sadece atandığınız araçta işlem yapabilirsiniz.")
    # Idempotency: if the trip is already over, do nothing. The driver app replays queued
    # requests after being offline, and a second run would insert a second return row and
    # double the return distance in reports.
    if not arac.get("rota_aktif"):
        return {"mesaj": "Rota zaten sonlandırılmış."}
    ref_lat, ref_lng = await run_in_threadpool(referans_konum, arac["firma_id"], arac.get("sube_id"))

    if merkeze_donus:
        # Returning to base: passengers may still be on board, so their requests stay open.
        donus_km = 0
        if arac.get("son_durak_lat") and ref_lat is not None:
            yol_verisi = await yol_mesafesi_verisi_async(arac["son_durak_lat"], arac["son_durak_lng"], ref_lat, ref_lng)
            donus_km = yol_verisi["km"]

        await run_query(supabase.table("talepler").insert({
            "token": f"DONUS-{secrets.token_hex(8)}",
            "firma_id": arac["firma_id"],
            "arac_id": arac_id,
            "durum": "MERKEZE DONUS",
            "mesafe_km": donus_km,
            "musteri_ad": "Sistem: Merkeze Boş Dönüş",
            "konum_lat": ref_lat,
            "konum_lng": ref_lng,
            "kayit_tarihi": datetime.now(timezone.utc).isoformat()
        }))

        son_lat, son_lng = ref_lat, ref_lng
        mesaj = f"Rota bitti. {donus_km} KM dönüş yolu rapora eklendi. Araç merkeze bekleniyor."
        yeni_durum = "MERKEZE DÖNÜYOR"
    else:
        # Not returning: the shift ends where the vehicle is.
        son_lat, son_lng = arac.get("son_durak_lat"), arac.get("son_durak_lng")
        mesaj = "Rota bulunduğunuz konumda sonlandırıldı. Dönüş yolu eklenmedi."
        yeni_durum = "ARAÇ DIŞARIDA"
        
        # There is no arrival at base to close the passengers later, so close them now. Each
        # tracking link is destroyed by rewriting its token with a "BTT-" prefix.
        aktif_yolcular = (await run_query(supabase.table("talepler").select("id, durum, token, tamamlanma_tarihi").eq("arac_id", arac_id).in_("durum", ["YOLCU ALINDI", "YOLCU INDI", "YOLCU GELMEDİ"]))).data
        for y in aktif_yolcular:
            kalici_durum = "TAMAM_ALINDI" if y["durum"] in ["YOLCU ALINDI", "YOLCU INDI"] else "TAMAM_GELMEDI"
            imha_token = f"BTT-{secrets.token_hex(8)}"
            y_guncelleme = {"durum": kalici_durum, "token": imha_token}
            # Safety net: the completion time is normally written when the passenger is marked
            # picked up / dropped off / no-show. Rows processed before that column existed have
            # no value, so fill it here. An existing value is kept, since the real action time is
            # more accurate than the trip's end.
            if not y.get("tamamlanma_tarihi"):
                y_guncelleme["tamamlanma_tarihi"] = datetime.now(timezone.utc).isoformat()
            await run_query(supabase.table("talepler").update(y_guncelleme).eq("id", y["id"]))

    await run_query(supabase.table("araclar").update({
        "rota_aktif": False,
        "son_durak_lat": son_lat,
        "son_durak_lng": son_lng,
        "durum": yeni_durum
    }).eq("id", arac_id))

    await manager.broadcast_firma(yetkili.get("firma_id"), "YENILE")
    await manager.broadcast_arac(arac_id, "YENILE")
    
    return {"mesaj": mesaj}

@app.post("/arac-durum-guncelle")
async def arac_durum_guncelle(arac_id: str, durum: str, yetkili = Depends(yetki_kontrol)):
    """Set a shuttle vehicle's status (at base, on duty, returning, out).

    MERKEZDE (arrived at base) also ends the trip and closes every passenger still on the
    vehicle. Like rota-bitir, this makes several updates without a transaction.
    """

    gecerli_durumlar = ["MERKEZDE", "GÖREVDE", "MERKEZE DÖNÜYOR", "ARAÇ DIŞARIDA"]
    if durum not in gecerli_durumlar:
        raise HTTPException(status_code=400, detail="Geçersiz araç durumu gönderildi.")

    if not (await run_in_threadpool(sofor_kendi_araci_mi, yetkili, arac_id)):
        raise HTTPException(status_code=403, detail="Sadece atandığınız araçta işlem yapabilirsiniz.")

    if yetkili["rol"] != "SUPERADMIN":
        arac_kontrol = (await run_query(supabase.table("araclar").select("firma_id").eq("id", arac_id))).data
        if not arac_kontrol or arac_kontrol[0].get("firma_id") != yetkili.get("firma_id"):
            raise HTTPException(status_code=403, detail="Sadece kendi firmanıza ait araçları güncelleyebilirsiniz.")

    # Guard against a late signal: a queued "returning" request that arrives after the
    # vehicle is already at base must not move it back to "returning".
    arac_durum_kontrol = (await run_query(supabase.table("araclar").select("durum").eq("id", arac_id))).data
    if not arac_durum_kontrol:
        raise HTTPException(status_code=404, detail="Araç bulunamadı (silinmiş veya geçersiz ID).")
    
    mevcut_durum = arac_durum_kontrol[0].get("durum")
    if mevcut_durum == "MERKEZDE" and durum == "MERKEZE DÖNÜYOR":
        return {"mesaj": "Araç zaten merkeze ulaşmış, gecikmeli sinyal yoksayıldı."}

    guncelleme_verisi = {"durum": durum}
    if durum == "MERKEZDE":
        guncelleme_verisi["rota_aktif"] = False
        guncelleme_verisi["hareket_saati"] = None 

    await run_query(supabase.table("araclar").update(guncelleme_verisi).eq("id", arac_id))
    
    # Arrived at base: close every passenger still on the vehicle and destroy their links.
    # talepler.token is UNIQUE, so each row needs its own "BTT-" token and its own update; a
    # single bulk update would write the same token to every row and violate the constraint.
    # Same pattern as rota-bitir.
    if durum == "MERKEZDE":
        aktif_yolcular = (await run_query(supabase.table("talepler").select("id, durum, tamamlanma_tarihi").eq("arac_id", arac_id).in_("durum", ["YOLCU ALINDI", "YOLCU INDI", "YOLCU GELMEDİ"]))).data

        for y in aktif_yolcular:
            kalici_durum = "TAMAM_ALINDI" if y["durum"] in ["YOLCU ALINDI", "YOLCU INDI"] else "TAMAM_GELMEDI"
            imha_token = f"BTT-{secrets.token_hex(8)}"
            y_guncelleme = {"durum": kalici_durum, "token": imha_token}
            # Same safety net as in rota-bitir for rows without a completion time.
            if not y.get("tamamlanma_tarihi"):
                y_guncelleme["tamamlanma_tarihi"] = datetime.now(timezone.utc).isoformat()
            await run_query(supabase.table("talepler").update(y_guncelleme).eq("id", y["id"]))

    await manager.broadcast_firma(yetkili.get("firma_id"), "YENILE")
    await manager.broadcast_arac(arac_id, "YENILE")
    
    return {"mesaj": f"Araç durumu {durum} olarak güncellendi."}

@app.post("/yeni-talep")
async def talep_olustur(talep: YeniTalep, request: Request, yetkili = Depends(yetki_kontrol)):
    """Advisor/operations panels: create a request (a shuttle passenger or a valet task).

    Returns the customer's tracking link. For a valet delivery started from the "waiting at
    service" list, iliskili_talep_id links it to the pickup task that brought the car in,
    and that pickup's link is destroyed.
    """
    # Only advisors and operations create requests. Admin roles have no reason to, and a
    # SUPERADMIN token has no company, which would produce a request with firma_id NULL.
    if yetkili["rol"] not in ["DANISMAN", "OPERASYON"]:
        raise HTTPException(status_code=403, detail="Yolcu kaydını yalnız danışman/operasyon açabilir.")

    if talep.musteri_tel and not re.match(r'^\+90[0-9]{10}$', talep.musteri_tel):
        raise HTTPException(status_code=400, detail="Geçersiz telefon formatı.")

    if talep.gorev_tipi not in ("TOPLAMA", "DAGITIM", "VALE_ALIM", "VALE_TESLIM"):
        raise HTTPException(status_code=400, detail="Geçersiz görev tipi.")
    
    # Valet tasks require the customer's plate, and only companies with the valet module may
    # create them. Shuttle requests likewise need the shuttle module.
    temiz_musteri_plaka = ""
    if talep.gorev_tipi.startswith("VALE_"):
        f_vale = (await run_query(supabase.table("firmalar").select("vale_aktif").eq("id", talep.firma_id))).data
        if not f_vale or not f_vale[0].get("vale_aktif"):
            raise HTTPException(status_code=400, detail="Bu firmada vale hizmeti tanımlı değil.")
        if not talep.musteri_plaka.strip():
            raise HTTPException(status_code=400, detail="Vale görevlerinde araç plakası girilmesi zorunludur.")
        temiz_musteri_plaka = validate_plaka(talep.musteri_plaka)
    else:
        f_sh = (await run_query(supabase.table("firmalar").select("shuttle_aktif").eq("id", talep.firma_id))).data
        if f_sh and f_sh[0].get("shuttle_aktif") is False:
            raise HTTPException(status_code=400, detail="Bu firmada shuttle hizmeti tanımlı değil.")

    # Delivery started from a car waiting at the service center: validate the source pickup task.
    iliskili_id = None
    if talep.iliskili_talep_id:
        if talep.gorev_tipi != "VALE_TESLIM":
            raise HTTPException(status_code=400, detail="Servisteki araç bağlantısı yalnız teslim görevinde kullanılır.")
        if not _gecerli_uuid(talep.iliskili_talep_id):
            raise HTTPException(status_code=400, detail="Geçersiz araç kaydı.")
        kaynak = (await run_query(supabase.table("talepler").select("id, firma_id, gorev_tipi, durum, musteri_plaka").eq("id", talep.iliskili_talep_id))).data
        if not kaynak:
            raise HTTPException(status_code=404, detail="Servisteki araç kaydı bulunamadı.")
        k = kaynak[0]
        if yetkili["rol"] != "SUPERADMIN" and k.get("firma_id") != yetkili.get("firma_id"):
            raise HTTPException(status_code=403, detail="Bu kayda yetkiniz yok.")
        if k.get("gorev_tipi") != "VALE_ALIM" or k.get("durum") != "TAMAM_SERVIS":
            raise HTTPException(status_code=400, detail="Bu araç serviste bekleyen araçlar arasında değil.")
        # The plate must match the source task. If the advisor edits the plate after choosing
        # the car, the link would point at the wrong car and the wrong entry would leave the list.
        if (k.get("musteri_plaka") or "") != temiz_musteri_plaka:
            raise HTTPException(status_code=400, detail="Plaka, serviste bekleyen araçla eşleşmiyor. Listeden yeniden seçin.")
        # Only one delivery per car. A cancelled delivery does not count, so a new one can be
        # opened after a cancellation. The filter runs in Python on purpose: `.neq("durum", ...)`
        # in SQL also drops rows whose status is NULL (NULL != 'X' is NULL), so such a row would
        # slip past the check. The serviste_kapandi filter avoids the same trap the same way.
        bagli_teslimler = (await run_query(supabase.table("talepler").select("id, durum").eq("iliskili_talep_id", k["id"]))).data
        if any(b.get("durum") != "IPTAL_EDILDI" for b in bagli_teslimler):
            raise HTTPException(status_code=409, detail="Bu araç için zaten bir teslim görevi açılmış.")
        iliskili_id = k["id"]

    # The customer's name is shown on the panels and on the customer page.
    temiz_musteri_ad = validate_metin(talep.musteri_ad, "Müşteri adı", max_uzunluk=80)
    
    # Company isolation: both the company in the body and the chosen vehicle must be the caller's.
    if yetkili["rol"] != "SUPERADMIN":
        if talep.firma_id != yetkili.get("firma_id"):
            raise HTTPException(status_code=403, detail="Sadece kendi firmanıza yolcu ekleyebilirsiniz.")
        
        arac_res = await run_query(supabase.table("araclar").select("firma_id").eq("id", talep.arac_id))
        if not arac_res.data or arac_res.data[0]["firma_id"] != yetkili.get("firma_id"):
            raise HTTPException(status_code=403, detail="Seçilen araç firmanıza ait değil.")

    # Advisors and operations staff can only create requests under their own brand (marka),
    # whatever the body says.
    if yetkili["rol"] in ["DANISMAN", "OPERASYON"]:
        talep.marka = yetkili.get("marka", "Genel")

    # A request inherits the branch of its vehicle (NULL if the vehicle has no branch).
    talep_sube_id = None
    arac_tip = "SERVIS"
    if talep.arac_id:
        arac_sube = (await run_query(supabase.table("araclar").select("sube_id, tip").eq("id", talep.arac_id))).data
        talep_sube_id = arac_sube[0].get("sube_id") if arac_sube else None
        arac_tip = (arac_sube[0].get("tip") or "SERVIS") if arac_sube else "SERVIS"
    # Task type must match vehicle type. The panel already filters this; the API must too.
    if talep.gorev_tipi.startswith("VALE_") and arac_tip != "VALE":
        raise HTTPException(status_code=400, detail="Vale görevi yalnızca vale personeline atanabilir.")
    if not talep.gorev_tipi.startswith("VALE_") and arac_tip == "VALE":
        raise HTTPException(status_code=400, detail="Servis görevi vale personeline atanamaz.")
    if yetkili.get("sube_id") and talep_sube_id != yetkili.get("sube_id"):
        raise HTTPException(status_code=403, detail="Bu araç sizin şubenize ait değil.")

    # A valet can have only one active task at a time. /vale-gorevi returns a single task to the
    # valet app, so a second task would sit invisibly in a queue while the advisor believed it
    # had been handed over. There is deliberately no date filter: an unfinished task from an
    # earlier day still keeps the valet busy.
    if talep.gorev_tipi.startswith("VALE_"):
        mevcut_gorev = (await run_query(supabase.table("talepler").select("musteri_plaka, musteri_ad, durum").eq(
            "arac_id", talep.arac_id).in_("durum", VALE_AKTIF_DURUMLAR).limit(1))).data
        if mevcut_gorev:
            m = mevcut_gorev[0]
            tanim = (m.get("musteri_plaka") or m.get("musteri_ad") or "").strip()
            raise HTTPException(
                status_code=409,
                detail=f"Bu valenin devam eden görevi var{f' ({tanim})' if tanim else ''}. Görev tamamlanmadan yeni görev verilemez."
            )

    token = secrets.token_hex(16)

    baslangic_durumu = "BEKLIYOR_KONUM" if talep.gorev_tipi.startswith("VALE_") else "BEKLİYOR"

    await run_query(supabase.table("talepler").insert({
        "token": token,
        "musteri_ad": temiz_musteri_ad,
        "musteri_tel": talep.musteri_tel,
        "arac_id": talep.arac_id,
        "durum": baslangic_durumu,
        "gorev_tipi": talep.gorev_tipi,
        "firma_id": yetkili["firma_id"],
        "marka": talep.marka,
        "sube_id": talep_sube_id,
        "musteri_plaka": temiz_musteri_plaka,
        # Linking to the pickup task removes the car from the "waiting at service" list.
        "iliskili_talep_id": iliskili_id,
        "kayit_tarihi": datetime.now(timezone.utc).isoformat()
    }))

    # Destroy the pickup task's link now that a delivery link exists. While the car waited, the
    # old link showed "your car is at the service center"; keeping only one live link per car
    # also limits how much personal data is reachable.
    if iliskili_id:
        await run_query(supabase.table("talepler").update({"token": f"BTT-{secrets.token_hex(8)}"}).eq("id", iliskili_id))
    
    await manager.broadcast_firma(yetkili["firma_id"], "YENILE")

    if talep.arac_id:
        await manager.broadcast_arac(str(talep.arac_id), "YENILE")

    return {"mesaj": "Oluşturuldu", "link": f"{BASE_URL.rstrip('/')}/musteri.html?token={token}", "token": token}

@app.post("/konum-dogrula")
@limiter.limit("15/minute")  # per IP
async def konum_dogrula(onay: KonumOnay, request: Request):
    """Customer page (no login, authorised by the request token): submit the pickup location.

    Records consent first when the company requires it, then stores the location. Accepted
    only before the passenger has been picked up; BEKLİYOR is the first submission and
    KONUM ALINDI is a correction of the pin.
    """
    if not (-90.0 <= onay.lat <= 90.0) or not (-180.0 <= onay.lng <= 180.0):
        raise HTTPException(status_code=400, detail="Geçersiz konum koordinatları.")
    # (0, 0) is what a client sends when it failed to get a position; it is never a real pick.
    if onay.lat == 0.0 and onay.lng == 0.0:
        raise HTTPException(status_code=400, detail="Konum seçilmemiş görünüyor. Lütfen haritadan geçerli bir nokta seçin.")

    # Needed for both the stop ownership check and the consent check below.
    talep_kayit = (await run_query(supabase.table("talepler").select("id, firma_id, kayit_tarihi, gorev_tipi").eq("token", onay.token))).data
    if not talep_kayit:
        raise HTTPException(status_code=404, detail="Talep bulunamadı.")
    if musteri_linki_suresi_doldu(onay.token, talep_kayit[0]):
        raise HTTPException(status_code=410, detail="Bu link süresi dolmuş.")
    talep_firma_id = talep_kayit[0].get("firma_id")

    # The chosen stop must belong to the same company as the request.
    if onay.secilen_durak_id:
        durak_kontrol = (await run_query(supabase.table("duraklar").select("firma_id").eq("id", onay.secilen_durak_id))).data

        if not durak_kontrol or durak_kontrol[0].get("firma_id") != talep_firma_id:
            raise HTTPException(status_code=403, detail="Seçilen durak geçersiz veya bu firmaya ait değil!")

    # Consent check, before any location is written. If consent is required: no consent means
    # 400; otherwise the consent log is written first (fail closed) and only then the location.
    riza_gerekli = await run_in_threadpool(kvkk_riza_gerekli_mi, talep_firma_id)
    if riza_gerekli:
        if not onay.riza_onay:
            raise HTTPException(
                status_code=400,
                detail="Konumunuzu işleyebilmemiz için aydınlatma metnini okuyup açık rıza vermeniz gerekir."
            )
        if not await riza_kaydi_yaz(
            kanal="YOLCU_KONUM", islem="ONAY", metin_kodu=KVKK_METIN_YOLCU_RIZA,
            firma_id=talep_firma_id, talep_id=talep_kayit[0].get("id"), request=request
        ):
            # Consent that cannot be proven does not count, so the location is not stored.
            raise HTTPException(
                status_code=503,
                detail="Onayınız kaydedilemedi, konumunuz işlenmedi. Lütfen birazdan tekrar deneyin."
            )

    # The status filter in the update is the guard: a request that has moved on (picked up,
    # dropped off, completed) must not be rolled back to KONUM ALINDI, or the passenger would
    # reappear on the driver's route and shift every ETA on that vehicle.
    guncelleme = await run_query(supabase.table("talepler").update({
        "durum": "KONUM ALINDI",
        "konum_lat": onay.lat,
        "konum_lng": onay.lng,
        "secilen_durak_id": onay.secilen_durak_id,
        # Quick flag for flow control only; the proof lives in the consent log. It is also set
        # when the company does not require consent, meaning "this location was collected
        # lawfully under the rules in force".
        "riza_alindi": True,
        # A customer who first refused and then came back loses the "refused" badge (both
        # events remain in the consent log).
        "riza_reddedildi": False
    }).eq("token", onay.token).in_("durum", ["BEKLİYOR", "KONUM ALINDI"]))

    if not guncelleme.data:
        # Either the token does not exist or the request has already moved on.
        raise HTTPException(status_code=409, detail="Bu link artık aktif değil veya talebiniz zaten işleme alınmış.")
        
    firma_id = guncelleme.data[0].get("firma_id")

    await manager.broadcast_firma(firma_id, "YENILE")
    
    return {"mesaj": "Konum alındı."}

@app.get("/sofor-rotasi")
async def sofor_rotasi(arac_id: str, lat: float, lng: float, yetkili = Depends(yetki_kontrol)):
    """Driver app: today's open requests for the vehicle, in the order they should be served.

    Also stores the driver's position (lat, lng) as the vehicle's last known location; the
    app has no background GPS, so these calls are how the position stays current.
    The ordering logic here and in /canli-sira (customer ETAs) must stay identical. If you
    change one, change the other.
    """
    # The coordinates are written to the vehicle row below.
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
        raise HTTPException(status_code=400, detail="Geçersiz harita koordinatları.")
    # Drivers only see their own vehicle's route, which contains passenger personal data.
    if not (await run_in_threadpool(sofor_kendi_araci_mi, yetkili, arac_id)):
        raise HTTPException(status_code=403, detail="Sadece atandığınız araçta işlem yapabilirsiniz.")
    if yetkili["rol"] != "SUPERADMIN":
        if not (k:=(await run_query(supabase.table("araclar").select("firma_id").eq("id", arac_id)))).data or k.data[0].get("firma_id") != yetkili.get("firma_id"): raise HTTPException(status_code=403)
    # son_hareket_zamani records when the position was written. The admin map shows it as
    # "last seen", so a stale marker is not mistaken for a live position.
    await run_query(supabase.table("araclar").update({
        "son_lat": lat, "son_lng": lng, "son_hareket_zamani": datetime.now(timezone.utc).isoformat()
    }).eq("id", arac_id))

    talepler = (await run_query(supabase.table("talepler").select("*").eq("arac_id", arac_id).in_("durum", ["KONUM ALINDI", "SERVIS_HAZIR"]).gte("kayit_tarihi", tr_bugun_baslangic_utc_iso()))).data
    # Drop requests without a location. Their coordinates go straight into the distance
    # functions below, and a single None would raise a TypeError and fail the whole route for
    # the driver. This happens when a location was erased while the request stayed active
    # (the customer withdrew consent).
    talepler = [t for t in talepler if t.get("konum_lat") is not None and t.get("konum_lng") is not None]
    if not talepler: return []

    firma_id = talepler[0].get("firma_id")
    firma_data = (await run_query(supabase.table("firmalar").select("merkez_lat, merkez_lng, sistem_modu").eq("id", firma_id))).data
    if not firma_data:
        return []
    firma = firma_data[0]
    s_mod = firma.get("sistem_modu", "HARITA")
    # Base point: the vehicle's branch, else company HQ. A request carries its vehicle's branch.
    m_lat, m_lng = await run_in_threadpool(referans_konum, firma_id, talepler[0].get("sube_id"), (firma.get("merkez_lat"), firma.get("merkez_lng")))

    # Road distance/time from the driver (fetched in parallel), and straight-line distance
    # from base, which drives the ordering.
    rota_gorevleri = []
    for t in talepler:
        rota_gorevleri.append(yol_mesafesi_verisi_async(lat, lng, t["konum_lat"], t["konum_lng"]))

        if m_lat and m_lng:
            t["mesafe_merkez"] = mesafe_hesapla(m_lat, m_lng, t["konum_lat"], t["konum_lng"])
        else:
            t["mesafe_merkez"] = 0

    if rota_gorevleri:
        sonuclar = await asyncio.gather(*rota_gorevleri)
        for i, t in enumerate(talepler):
            t["mesafe_km"] = sonuclar[i]["km"]
            t["tahmini_dakika"] = sonuclar[i]["dakika"]
            t["sistem_modu"] = s_mod

    # Ordering depends on the company's mode: fixed stops (DURAK) or door-to-door (HARITA).
    if s_mod == "DURAK":
        durak_verileri = (await run_query(supabase.table("duraklar").select("id, sira_no, durak_adi").eq("firma_id", firma_id))).data
        durak_dict = {d["id"]: {"sira_no": d["sira_no"], "durak_adi": d["durak_adi"]} for d in durak_verileri}

        for t in talepler:
            d_id = t.get("secilen_durak_id")
            if d_id and d_id in durak_dict:
                t["sira_no"] = durak_dict[d_id]["sira_no"]
                t["durak_adi"] = durak_dict[d_id]["durak_adi"]
            else:
                t["sira_no"] = 999
                t["durak_adi"] = "Bilinmeyen Durak"

        # Ordering depends on the route's shape (guzergahlar.tip, computed by
        # /guzergah-tipi-hesapla): LINE is distance based (out and back); YARIM_AY ("half
        # moon", a loop) and NULL follow the stops' sira_no.
        guzergah_tip = None
        arac_guz = (await run_query(supabase.table("araclar").select("guzergah_id").eq("id", arac_id))).data
        if arac_guz and arac_guz[0].get("guzergah_id"):
            guz_row = (await run_query(supabase.table("guzergahlar").select("tip").eq("id", arac_guz[0]["guzergah_id"]))).data
            if guz_row:
                guzergah_tip = guz_row[0].get("tip")

        # On a LINE route the driver app shows the same physical stop as two cards, one for
        # drop-offs on the way out and one for pickups on the way back. Otherwise a pickup
        # passenger would be collected on the way out. The app needs the route type for that.
        for t in talepler:
            t["guzergah_tip"] = guzergah_tip

        if guzergah_tip == "LINE":
            # LINE: drop-offs nearest to farthest on the way out, then pickups farthest to
            # nearest on the way back.
            toplama = sorted([t for t in talepler if t.get("gorev_tipi") == "TOPLAMA"], key=lambda x: -x["mesafe_merkez"])
            dagitim = sorted([t for t in talepler if t.get("gorev_tipi") == "DAGITIM"], key=lambda x: x["mesafe_merkez"])
        else:
            # YARIM_AY / NULL: every stop in sira_no order, regardless of pickup or drop-off.
            # NULL keeps the behaviour routes had before the type existed.
            return sorted(talepler, key=lambda x: x.get("sira_no") or 999)

    else:
        # HARITA mode: by straight-line distance from base. Drop-offs nearest first, pickups
        # farthest first.
        toplama = sorted([t for t in talepler if t.get("gorev_tipi") == "TOPLAMA"], key=lambda x: -x["mesafe_merkez"])
        dagitim = sorted([t for t in talepler if t.get("gorev_tipi") == "DAGITIM"], key=lambda x: x["mesafe_merkez"])

    return dagitim + toplama  # drop-offs first, while leaving base

@app.post("/yolcu-alindi")
async def yolcu_alindi(token: str, yetkili = Depends(yetki_kontrol)):
    """Driver app: mark a passenger as picked up.

    Records the road distance from the previous stop and moves the vehicle's "previous stop"
    to this passenger. /yolcu-gelmedi and /yolcu-indi follow the same pattern.
    """
    talep_kontrol = await run_query(supabase.table("talepler").select("firma_id, arac_id, konum_lat, konum_lng, durum").eq("token", token))
    if not talep_kontrol.data: raise HTTPException(status_code=404, detail="Talep bulunamadı.")
    talep = talep_kontrol.data[0]
    # Idempotency: the driver app replays queued actions after being offline, so a repeat
    # must be a no-op instead of recording the distance twice.
    if talep.get("durum") == "YOLCU ALINDI":
        return {"mesaj": "Yolcu zaten alındı olarak işaretlenmiş."}

    if yetkili["rol"] != "SUPERADMIN" and talep.get("firma_id") != yetkili.get("firma_id"): 
        raise HTTPException(status_code=403, detail="Yetkisiz işlem.")
    # Same rule as the route endpoints: a field role may only mark passengers of the vehicle
    # assigned to them, not any vehicle in their company.
    if not (await run_in_threadpool(sofor_kendi_araci_mi, yetkili, talep.get("arac_id"))):
        raise HTTPException(status_code=403, detail="Sadece atandığınız araçta işlem yapabilirsiniz.")

    # Distance is measured from the previous stop (son_durak_lat/lng), not from a live GPS
    # position: the driver app has no continuous tracking.
    arac_data = (await run_query(supabase.table("araclar").select("son_durak_lat, son_durak_lng, rota_aktif").eq("id", talep["arac_id"]))).data
    if not arac_data:
        raise HTTPException(status_code=404, detail="Araç bulunamadı.")
    arac = arac_data[0]
    # Passenger actions require a started trip. The app disables the buttons, but the API must
    # enforce it too. Replays are unaffected because the idempotency check above returns first.
    if not arac.get("rota_aktif"):
        raise HTTPException(status_code=409, detail="Rota başlatılmadan yolcu işlemi yapılamaz.")
    
    yapilan_km = 0
    if arac.get("son_durak_lat"):
        yol_verisi = await yol_mesafesi_verisi_async(arac["son_durak_lat"], arac["son_durak_lng"], talep["konum_lat"], talep["konum_lng"])
        yapilan_km = yol_verisi["km"]

    # tamamlanma_tarihi (completion time) feeds the start-end times on the panels and the
    # "completed today" filter. Stored in UTC; the panels display it in Istanbul time.
    await run_query(supabase.table("talepler").update({
        "durum": "YOLCU ALINDI",
        "mesafe_km": yapilan_km,
        "tamamlanma_tarihi": datetime.now(timezone.utc).isoformat()
    }).eq("token", token))
    
    # This passenger's location becomes the starting point of the next leg and the vehicle's
    # shown position.
    await run_query(supabase.table("araclar").update({
        "son_durak_lat": talep["konum_lat"], 
        "son_durak_lng": talep["konum_lng"],
        "son_lat": talep["konum_lat"],
        "son_lng": talep["konum_lng"],
        "son_hareket_zamani": datetime.now(timezone.utc).isoformat()
    }).eq("id", talep["arac_id"]))

    await manager.broadcast_firma(yetkili["firma_id"], "YENILE")
    await manager.broadcast_arac(talep["arac_id"], "YENILE")
    return {"mesaj": f"Yolcu alındı. Rota: {yapilan_km} KM kaydedildi."}

@app.post("/yolcu-gelmedi")
async def yolcu_gelmedi(token: str, yetkili = Depends(yetki_kontrol)):
    """Driver app: mark a passenger as a no-show. The distance driven is still recorded."""
    talep_kontrol = await run_query(supabase.table("talepler").select("firma_id, arac_id, konum_lat, konum_lng, durum").eq("token", token))
    if not talep_kontrol.data: raise HTTPException(status_code=404, detail="Talep bulunamadı.")
    talep = talep_kontrol.data[0]
    if talep.get("durum") == "YOLCU GELMEDİ":  # idempotent replay
        return {"mesaj": "Yolcu zaten 'gelmedi' olarak işaretlenmiş."}

    if yetkili["rol"] != "SUPERADMIN" and talep.get("firma_id") != yetkili.get("firma_id"): 
        raise HTTPException(status_code=403, detail="Yetkisiz işlem.")
    # A field role may only act on its own vehicle (see /yolcu-alindi).
    if not (await run_in_threadpool(sofor_kendi_araci_mi, yetkili, talep.get("arac_id"))):
        raise HTTPException(status_code=403, detail="Sadece atandığınız araçta işlem yapabilirsiniz.")

    arac_data = (await run_query(supabase.table("araclar").select("son_durak_lat, son_durak_lng, rota_aktif").eq("id", talep["arac_id"]))).data
    if not arac_data:
        raise HTTPException(status_code=404, detail="Araç bulunamadı.")
    arac = arac_data[0]
    # Passenger actions require a started trip. The app disables the buttons, but the API must
    # enforce it too. Replays are unaffected because the idempotency check above returns first.
    if not arac.get("rota_aktif"):
        raise HTTPException(status_code=409, detail="Rota başlatılmadan yolcu işlemi yapılamaz.")
    
    yapilan_km = 0
    if arac.get("son_durak_lat"):
        yol_verisi = await yol_mesafesi_verisi_async(arac["son_durak_lat"], arac["son_durak_lng"], talep["konum_lat"], talep["konum_lng"])
        yapilan_km = yol_verisi["km"]

    # A no-show also completes the request, so it gets a completion time too.
    await run_query(supabase.table("talepler").update({
        "durum": "YOLCU GELMEDİ",
        "mesafe_km": yapilan_km,
        "tamamlanma_tarihi": datetime.now(timezone.utc).isoformat()
    }).eq("token", token))
    
    # The next leg starts from here.
    await run_query(supabase.table("araclar").update({
        "son_durak_lat": talep["konum_lat"], 
        "son_durak_lng": talep["konum_lng"],
        "son_lat": talep["konum_lat"],
        "son_lng": talep["konum_lng"],
        "son_hareket_zamani": datetime.now(timezone.utc).isoformat()
    }).eq("id", talep["arac_id"]))

    await manager.broadcast_firma(yetkili["firma_id"], "YENILE")
    await manager.broadcast_arac(talep["arac_id"], "YENILE")
    return {"mesaj": f"Yolcu gelmedi. Boşa gidilen {yapilan_km} KM kaydedildi."}

@app.post("/yolcu-devret")
async def yolcu_devret(token: str, yeni_arac_id: str, yetkili = Depends(yetki_kontrol)):
    """Panels: move a passenger who has not boarded yet to another vehicle of the same company."""
    if yetkili["rol"] not in ["ADMIN", "SUPERADMIN", "OPERASYON"]: 
        raise HTTPException(status_code=403, detail="Sadece yöneticiler ve operasyon sorumluları yolcu aktarabilir.")

    talep = (await run_query(supabase.table("talepler").select("*").eq("token", token))).data
    if not talep: raise HTTPException(status_code=404, detail="Yolcu bulunamadı.")
    
    eski_arac_id = talep[0]["arac_id"]
    talep_firma_id = talep[0]["firma_id"]

    yeni_arac_kontrol = (await run_query(supabase.table("araclar").select("firma_id, sube_id").eq("id", yeni_arac_id))).data
    if not yeni_arac_kontrol:
        raise HTTPException(status_code=404, detail="Hedef araç bulunamadı.")
    yeni_arac_sube = yeni_arac_kontrol[0].get("sube_id")

    # Both the passenger and the target vehicle must belong to the caller's company, and a
    # branch-scoped user may only move passengers onto their own branch's vehicles.
    if yetkili["rol"] != "SUPERADMIN":
        if talep_firma_id != yetkili.get("firma_id"):
            raise HTTPException(status_code=403, detail="Bu yolcu sizin firmanıza ait değil.")
        if yeni_arac_kontrol[0]["firma_id"] != talep_firma_id:
            raise HTTPException(status_code=403, detail="Sadece kendi firmanıza ait araçlara devir yapabilirsiniz.")
        sube_yazma_guard(yetkili, yeni_arac_sube)

    if talep[0]["durum"] not in ["SERVIS_HAZIR", "KONUM ALINDI"]:
        raise HTTPException(status_code=400, detail="Sadece araca henüz binmemiş yolcular aktarılabilir.")

    # The request takes the branch of its new vehicle.
    await run_query(supabase.table("talepler").update({"arac_id": yeni_arac_id, "sube_id": yeni_arac_sube}).eq("token", token))

    await manager.broadcast_arac(eski_arac_id, "YENILE") 
    await manager.broadcast_arac(yeni_arac_id, "YENILE") 
    await manager.broadcast_firma(yetkili["firma_id"], "YENILE") 

    return {"mesaj": "Yolcu başarıyla diğer araca devredildi."}

@app.get("/talep-detay/{token}")
@limiter.limit("20/minute")  # limits guessing of tokens, which would expose customer data
async def talep_detay_getir(token: str, request: Request):
    """Customer page (no login): everything the tracking page shows for one request token.

    The response is the request row plus vehicle, company, KVKK notice and valet fields.
    After a valet delivery it switches to a reduced "survey mode" response.
    """
    res = await run_query(supabase.table("talepler").select("*").eq("token", token))
    if not res.data: raise HTTPException(status_code=404, detail="Geçersiz link.")
    yolcu = res.data[0]

    # Expire old links so a forwarded link does not stay usable.
    if musteri_linki_suresi_doldu(token, yolcu):
        raise HTTPException(status_code=410, detail="Bu link süresi dolmuş.")
    
    # ------------------------------------------------------------
    # Survey mode: the window after a valet delivery is completed.
    # ------------------------------------------------------------
    # The link stays alive for ANKET_PENCERESI_SAAT so the customer can rate the service, but
    # that must not keep their data readable. In this window the response is cut down to what
    # the survey screen needs: no name, phone, plate, vehicle or valet name. (The location was
    # already erased at delivery.)
    # This block must stay right after the select("*") and before everything else; the
    # enrichment below (vehicle, valet name, ETA) is not needed here and would leak data.
    if (str(yolcu.get("gorev_tipi") or "") == "VALE_TESLIM"
            and yolcu.get("durum") == "TAMAM_MUSTERI"):
        anket_acik = False
        if yolcu.get("tamamlanma_tarihi"):
            try:
                _bitis = supabase_tarih_parse(yolcu["tamamlanma_tarihi"])
                anket_acik = (datetime.now(timezone.utc) - _bitis) <= timedelta(hours=ANKET_PENCERESI_SAAT)
            except (ValueError, TypeError) as e:
                sentry_sdk.capture_exception(e)   # bad timestamp: treat the window as closed

        # Once the survey is submitted the window closes, so the form is never shown twice.
        if anket_acik:
            try:
                _var = (await run_query(supabase.table("memnuniyet_anketleri").select("id").eq(
                    "talep_id", yolcu.get("id")).limit(1))).data
                if _var:
                    anket_acik = False
            except Exception as e:
                sentry_sdk.capture_exception(e)   # cannot check: hide the form to avoid duplicates
                anket_acik = False

        if not anket_acik:
            # Lazy destruction: the token is killed on the first visit after the window closes.
            # No separate job is needed; if nobody opens the link again, the 30-day KVKK
            # cleanup deletes the row anyway.
            if not token.startswith("BTT-"):
                try:
                    await run_query(supabase.table("talepler").update(
                        {"token": f"BTT-{secrets.token_hex(8)}"}).eq("id", yolcu.get("id")))
                except Exception as e:
                    sentry_sdk.capture_exception(e)
            raise HTTPException(status_code=410, detail="Bu link süresi dolmuş.")

        # This is everything the survey screen needs. Anything more would be a leak.
        return {
            "durum": yolcu.get("durum"),
            "gorev_tipi": yolcu.get("gorev_tipi"),
            "anket_gosterilsin": True,
            "anket_yorum_limiti": ANKET_YORUM_MAKS,
        }

    # Last position fields are read for the valet ETA further down.
    arac_res = await run_query(supabase.table("araclar").select("rota_aktif, plaka, guzergah_id, hareket_saati, son_lat, son_lng, son_hareket_zamani").eq("id", yolcu.get("arac_id")))
    if arac_res.data:
        yolcu["rota_aktif"] = arac_res.data[0].get("rota_aktif")
        yolcu["arac_plaka"] = arac_res.data[0].get("plaka") 
        yolcu["guzergah_id"] = arac_res.data[0].get("guzergah_id")
        yolcu["hareket_saati"] = arac_res.data[0].get("hareket_saati")
    else:
        yolcu["rota_aktif"] = False
        yolcu["arac_plaka"] = "Plaka Bekleniyor"
        yolcu["guzergah_id"] = None
        yolcu["hareket_saati"] = None
    
    # Company mode plus its KVKK details, in one query. For passenger data the data controller
    # is the customer's company (the platform is only the processor), so the company's legal
    # name and contact channel must appear in the privacy notice on the customer page.
    firma_res = await run_query(supabase.table("firmalar").select(
        "merkez_lat, merkez_lng, sistem_modu, kvkk_unvan, kvkk_basvuru_kanali, kvkk_adres, kvkk_riza_gerekli"
    ).eq("id", yolcu.get("firma_id")))
    if firma_res.data:
        _fm = (firma_res.data[0].get("merkez_lat"), firma_res.data[0].get("merkez_lng"))
        # Base point shown to the customer: the request's branch, else company HQ.
        _rl, _rg = await run_in_threadpool(referans_konum, yolcu.get("firma_id"), yolcu.get("sube_id"), _fm)
        yolcu["merkez_lat"] = _rl
        yolcu["merkez_lng"] = _rg
        yolcu["sistem_modu"] = firma_res.data[0].get("sistem_modu", "HARITA")

    # Fields for the privacy notice and consent box on the customer page. They are always
    # present, even if the company row could not be read, so the page renders and the
    # "consent required" flag never silently becomes False (the safe default is to ask).
    _firma = firma_res.data[0] if firma_res.data else {}
    yolcu["kvkk_firma_unvan"] = (_firma.get("kvkk_unvan") or "").strip()
    yolcu["kvkk_firma_basvuru"] = (_firma.get("kvkk_basvuru_kanali") or "").strip()
    # The address is optional; when empty the customer page leaves that line out.
    yolcu["kvkk_firma_adres"] = (_firma.get("kvkk_adres") or "").strip()
    _riza_bayrak = _firma.get("kvkk_riza_gerekli")
    yolcu["kvkk_riza_gerekli"] = KVKK_RIZA_GEREKLI_VARSAYILAN if _riza_bayrak is None else bool(_riza_bayrak)
    yolcu["kvkk_metin_versiyonu"] = KVKK_METIN_VERSIYONU
    yolcu["kvkk_metin_kodu"] = KVKK_METIN_YOLCU_RIZA
    # riza_alindi is already part of the row from select("*").

    # Valet's display name for the customer page. For valet tasks `arac_plaka` is not a real
    # plate: the virtual vehicle's plate field holds the valet's username, which must not be
    # shown to customers. Only valet tasks pay for this extra query.
    if str(yolcu.get("gorev_tipi") or "").startswith("VALE_") and yolcu.get("arac_id"):
        _v = (await run_query(supabase.table("kullanicilar").select("kullanici_adi, gorunen_ad").eq(
            "arac_id", yolcu["arac_id"]).eq("rol", "VALE").limit(1))).data
        if _v:
            # No display name: leave it empty instead of falling back to the username. Hiding
            # the staff line looks better than showing a login name.
            yolcu["vale_adi"] = (_v[0].get("gorunen_ad") or "").strip()
        else:
            yolcu["vale_adi"] = ""

    # ------------------------------------------------------------
    # Valet arrival time shown to the customer: an anchored live estimate.
    # ------------------------------------------------------------
    # Two different numbers exist; do not mix them up:
    #   1) gorev_etaplari.hedef_varis is the promise frozen when the leg started. Punctuality
    #      reports use it and it never changes.
    #   2) vale_varis_saati here is the current estimate shown to the customer. It should follow
    #      the valet's actual movement (if the valet stops for 20 minutes, the customer sees it).
    #
    # The formula is anchored to when the valet's position was written:
    #     arrival = son_hareket_zamani + travel time (last position -> customer)
    # "now + travel time" would push the time forward on every refresh even if the valet had
    # not moved. With both terms fixed, every refresh shows the same time until the valet
    # reports a new position.
    #
    # Cost: route results are cached by coordinates, so a valet who has not moved is served
    # from Redis and only real movement triggers a Mapbox call.
    if yolcu.get("gorev_tipi", "").startswith("VALE_") and yolcu.get("durum") == "VALE_YOLDA":
        _arac = arac_res.data[0] if arac_res.data else {}
        _damga = _arac.get("son_hareket_zamani")

        if (_arac.get("son_lat") is not None and yolcu.get("konum_lat") is not None and _damga):
            eta = await yol_mesafesi_verisi_async(
                _arac["son_lat"], _arac["son_lng"], yolcu["konum_lat"], yolcu["konum_lng"])
            if eta.get("dakika"):
                try:
                    _cikis = supabase_tarih_parse(_damga)
                    # No per-stop allowance is added here, unlike the +2 minutes in canli-sira. A
                    # valet only drives to one door and boards no passengers, and an extra
                    # allowance made the customer's time differ from the promise on the panel.
                    _varis = _cikis + timedelta(minutes=eta["dakika"])
                    # An estimate already in the past (the valet is late and has not reported a
                    # new position) would mislead; send nothing and the page shows neutral text.
                    if _varis > datetime.now(timezone.utc):
                        yolcu["vale_varis_saati"] = _varis.isoformat()
                except (ValueError, TypeError) as e:
                    sentry_sdk.capture_exception(e)

        # Fallback when there is no position or timestamp: show the frozen promise from the
        # milestone log. It does not drift, so it is at least consistent.
        # The fallback must pass the same "not in the past" check. Otherwise, when the live
        # estimate is rejected for being in the past, the even older promise would be shown
        # instead. The client does not check this (musteri.js formats the raw ISO string), so
        # if both are in the past nothing is sent and the customer sees a neutral message.
        # A wrong time is worse than a vague one.
        if not yolcu.get("vale_varis_saati"):
            try:
                _etap = (await run_query(supabase.table("gorev_etaplari").select("hedef_varis").eq(
                    "talep_id", yolcu.get("id")).is_("gercek_varis", "null").eq(
                    "iptal_edildi", False).order("baslangic", desc=True).limit(1))).data
                if _etap and _etap[0].get("hedef_varis"):
                    if supabase_tarih_parse(_etap[0]["hedef_varis"]) > datetime.now(timezone.utc):
                        yolcu["vale_varis_saati"] = _etap[0]["hedef_varis"]
            except Exception as e:
                sentry_sdk.capture_exception(e)


    return yolcu

@app.post("/servis-hazir")
async def servis_hazir(arac_id: str, yetkili = Depends(yetki_kontrol)):
    """Driver app: tell the vehicle's drop-off passengers that the shuttle is ready to leave.

    Moves the vehicle's DAGITIM (drop-off) requests in KONUM ALINDI to SERVIS_HAZIR.
    """
    if not (await run_in_threadpool(sofor_kendi_araci_mi, yetkili, arac_id)):
        raise HTTPException(status_code=403, detail="Sadece atandığınız araçta işlem yapabilirsiniz.")
    if yetkili["rol"] != "SUPERADMIN":
        if not (k:=(await run_query(supabase.table("araclar").select("firma_id").eq("id", arac_id)))).data or k.data[0].get("firma_id") != yetkili.get("firma_id"): raise HTTPException(status_code=403)
    # Only today's requests, with the same filter as sofor-rotasi: a drop-off request left open
    # from an earlier day is not on today's route and must not be told the shuttle is ready.
    guncelleme = await run_query(supabase.table("talepler").update({"durum": "SERVIS_HAZIR"}).eq("arac_id", arac_id).eq("gorev_tipi", "DAGITIM").eq("durum", "KONUM ALINDI").gte("kayit_tarihi", tr_bugun_baslangic_utc_iso()))
    await manager.broadcast_firma(yetkili["firma_id"], "YENILE")
    await manager.broadcast_arac(arac_id, "YENILE")
    return {"mesaj": f"{len(guncelleme.data)} mesaj iletildi."}

@app.post("/yolcu-indi")
async def yolcu_indi(token: str, yetkili = Depends(yetki_kontrol)):
    """Driver app: mark a drop-off passenger as delivered to their destination."""
    talep_kontrol = await run_query(supabase.table("talepler").select("firma_id, arac_id, konum_lat, konum_lng, durum").eq("token", token))
    if not talep_kontrol.data: raise HTTPException(status_code=404, detail="Talep bulunamadı.")
    talep = talep_kontrol.data[0]
    # Idempotent replay. Note the stored value is "YOLCU INDI", without the Turkish dotless i.
    if talep.get("durum") == "YOLCU INDI":
        return {"mesaj": "Yolcu zaten 'indi' olarak işaretlenmiş."}

    if yetkili["rol"] != "SUPERADMIN" and talep.get("firma_id") != yetkili.get("firma_id"): 
        raise HTTPException(status_code=403, detail="Yetkisiz işlem.")
    # A field role may only act on its own vehicle (see /yolcu-alindi).
    if not (await run_in_threadpool(sofor_kendi_araci_mi, yetkili, talep.get("arac_id"))):
        raise HTTPException(status_code=403, detail="Sadece atandığınız araçta işlem yapabilirsiniz.")

    arac_data = (await run_query(supabase.table("araclar").select("son_durak_lat, son_durak_lng, rota_aktif").eq("id", talep["arac_id"]))).data
    if not arac_data:
        raise HTTPException(status_code=404, detail="Araç bulunamadı.")
    arac = arac_data[0]
    # Passenger actions require a started trip. The app disables the buttons, but the API must
    # enforce it too. Replays are unaffected because the idempotency check above returns first.
    if not arac.get("rota_aktif"):
        raise HTTPException(status_code=409, detail="Rota başlatılmadan yolcu işlemi yapılamaz.")
    
    yapilan_km = 0
    if arac.get("son_durak_lat"):
        yol_verisi = await yol_mesafesi_verisi_async(arac["son_durak_lat"], arac["son_durak_lng"], talep["konum_lat"], talep["konum_lng"])
        yapilan_km = yol_verisi["km"]

    await run_query(supabase.table("talepler").update({
        "durum": "YOLCU INDI",
        "mesafe_km": yapilan_km,
        "tamamlanma_tarihi": datetime.now(timezone.utc).isoformat()
    }).eq("token", token))
    
    # The next leg starts from here.
    await run_query(supabase.table("araclar").update({
        "son_durak_lat": talep["konum_lat"], 
        "son_durak_lng": talep["konum_lng"],
        "son_lat": talep["konum_lat"],
        "son_lng": talep["konum_lng"],
        "son_hareket_zamani": datetime.now(timezone.utc).isoformat()
    }).eq("id", talep["arac_id"]))

    await manager.broadcast_firma(yetkili["firma_id"], "YENILE")
    await manager.broadcast_arac(talep["arac_id"], "YENILE")
    return {"mesaj": f"Yolcu evine bırakıldı. {yapilan_km} KM kaydedildi."}

@app.get("/canli-sira/{token}")
@limiter.limit("30/minute")
async def canli_sira_getir(token: str, request: Request):
    """Customer page (no login): the passenger's place in the queue and an ETA in minutes.

    The ordering must stay identical to /sofor-rotasi; change both together. The ETA is
    the sum of Mapbox driving times from the vehicle's last known position through every
    stop before this one, plus 2 minutes per stop. It is recomputed from scratch on each
    poll rather than counted down.
    """
    yolcu_res = await run_query(supabase.table("talepler").select("*").eq("token", token))
    if not yolcu_res.data: 
        return {"sira": "-"}
    yolcu = yolcu_res.data[0]
    # An expired link gets the same answer as an unknown one.
    if musteri_linki_suresi_doldu(token, yolcu):
        return {"sira": "-"}
    
    if yolcu["durum"] not in ["KONUM ALINDI", "SERVIS_HAZIR"]: 
        return {"mesaj": "Sıra dışı"}
    
    arac = (await run_query(supabase.table("araclar").select("*").eq("id", yolcu["arac_id"]))).data
    if not arac or not arac[0].get("son_lat"): 
        return {"sira": "-", "mesaj": "Araç henüz yola çıkmadı"}

    # Everyone still waiting on this vehicle, with no task type filter: in DURAK mode the
    # queue covers the whole route (mixed order on YARIM_AY, drop-offs then pickups on LINE).
    # The HARITA branch filters by task type itself.
    # Only today's requests, with the same filter as sofor-rotasi: a request left open from an
    # earlier day is not on the driver's route, so it must not shift the customer's queue either.
    bekleyenler = (await run_query(supabase.table("talepler").select("*").eq("arac_id", yolcu["arac_id"]).in_("durum", ["KONUM ALINDI", "SERVIS_HAZIR"]).gte("kayit_tarihi", tr_bugun_baslangic_utc_iso()))).data
    # Drop requests without a location, as in sofor-rotasi. One such row would otherwise make
    # the ETA fail for everyone on the vehicle.
    bekleyenler = [b for b in bekleyenler if b.get("konum_lat") is not None and b.get("konum_lng") is not None]

    firma = (await run_query(supabase.table("firmalar").select("merkez_lat, merkez_lng, sistem_modu").eq("id", yolcu.get("firma_id")))).data
    s_mod = firma[0].get("sistem_modu", "HARITA") if firma else "HARITA"
    _fm = (firma[0].get("merkez_lat"), firma[0].get("merkez_lng")) if firma else (None, None)
    merkez_lat, merkez_lng = await run_in_threadpool(referans_konum, yolcu.get("firma_id"), arac[0].get("sube_id"), _fm)
    if merkez_lat is None: merkez_lat, merkez_lng = 0.0, 0.0

    if s_mod == "DURAK":
        # DURAK mode: the queue is made of stops, so everyone at one stop shares one position.
        durak_verileri = (await run_query(supabase.table("duraklar").select("id, sira_no").eq("firma_id", yolcu.get("firma_id")))).data
        durak_dict = {d["id"]: d["sira_no"] for d in durak_verileri}
        
        aktif_duraklar = {}
        for y in bekleyenler:
            d_id = y.get("secilen_durak_id")
            if d_id and d_id not in aktif_duraklar:
                aktif_duraklar[d_id] = {
                    "id": d_id, "lat": y["konum_lat"], "lng": y["konum_lng"], "sira": durak_dict.get(d_id, 999)
                }
                
        # YARIM_AY / NULL: stops in ascending sira_no, task type ignored. LINE overrides below.
        sirali = sorted(list(aktif_duraklar.values()), key=lambda x: x["sira"])

        # LINE: the same out-and-back order as sofor-rotasi.
        guzergah_tip = None
        if arac[0].get("guzergah_id"):
            _gz = (await run_query(supabase.table("guzergahlar").select("tip").eq("id", arac[0]["guzergah_id"]))).data
            if _gz:
                guzergah_tip = _gz[0].get("tip")
        if guzergah_tip == "LINE":
            # All drop-offs first (nearest to base first), then pickups (farthest first).
            def _grupla_duraklar(kayitlar, gorev):
                gruplu = {}
                for y in kayitlar:
                    d_id = y.get("secilen_durak_id")
                    if d_id and d_id not in gruplu:
                        gruplu[d_id] = {"id": d_id, "lat": y["konum_lat"], "lng": y["konum_lng"], "gorev": gorev,
                                        "mesafe_merkez": mesafe_hesapla(merkez_lat, merkez_lng, y["konum_lat"], y["konum_lng"])}
                return list(gruplu.values())
            dagitim_d = sorted(_grupla_duraklar([y for y in bekleyenler if y.get("gorev_tipi") == "DAGITIM"], "DAGITIM"), key=lambda x: x["mesafe_merkez"])
            toplama_d = sorted(_grupla_duraklar([y for y in bekleyenler if y.get("gorev_tipi") == "TOPLAMA"], "TOPLAMA"), key=lambda x: -x["mesafe_merkez"])
            sirali = dagitim_d + toplama_d
        
        musteri_durak_id = yolcu.get("secilen_durak_id")
        # On LINE the same stop can appear in both blocks, so match on the task type too.
        sira = next((i for i, d in enumerate(sirali) if d.get("id") == musteri_durak_id and (guzergah_tip != "LINE" or d.get("gorev") == yolcu["gorev_tipi"])), None)

    else:
        # HARITA mode: one queue entry per passenger, ordered by straight-line distance from base.
        sirali = sorted(
            [{"token": y["token"], "lat": y["konum_lat"], "lng": y["konum_lng"],
              "mesafe": mesafe_hesapla(merkez_lat, merkez_lng, y["konum_lat"], y["konum_lng"])}
             for y in bekleyenler if y.get("gorev_tipi") == yolcu["gorev_tipi"]],
            key=lambda x: x["mesafe"], reverse=(yolcu["gorev_tipi"] == "TOPLAMA")
        )
        sira = next((i for i, v in enumerate(sirali) if v.get("token") == token), None)

    if sira is None: 
        return {"sira": "-"}

    # Leg by leg from the vehicle's last position up to this passenger, fetched from Mapbox in
    # parallel.
    rota_gorevleri = []
    onceki_lat, onceki_lng = arac[0]["son_lat"], arac[0]["son_lng"]
    
    for i in range(sira + 1):
        hedef = sirali[i]
        rota_gorevleri.append(yol_mesafesi_verisi_async(onceki_lat, onceki_lng, hedef["lat"], hedef["lng"]))
        onceki_lat, onceki_lng = hedef["lat"], hedef["lng"]

    toplam_dakika = 0
    if rota_gorevleri:
        yol_sonuclari = await asyncio.gather(*rota_gorevleri)
        for i, sonuc in enumerate(yol_sonuclari):
            toplam_dakika += sonuc["dakika"]
            # +2 minutes for each stop before this one (per stop, not per passenger).
            if i < sira:
                toplam_dakika += 2

    gercek_kalan_dakika = max(1, int(toplam_dakika))

    return {
        "sira": sira + 1,
        "toplam": len(sirali),
        "tahmini_dakika": gercek_kalan_dakika,
        "sistem_modu": s_mod
    }

@app.post("/giris-yap")
@limiter.limit("5/minute")
async def giris_yap(bilgi: LoginIstek, request: Request):
    """Login for all staff roles. Returns a JWT plus what the panels need to draw themselves.

    The module flags (shuttle_aktif, vale_aktif) are part of the response so the panels
    can render the right sections immediately, without waiting for another request.
    """
    # Usernames are stored lowercased, so normalise the input the same way.
    arama_kullanici_adi = (bilgi.kullanici_adi or "").strip().lower()
    res = await run_query(supabase.table("kullanicilar").select("*").eq("kullanici_adi", arama_kullanici_adi))
    if not res.data or not sifreyi_dogrula(bilgi.sifre, res.data[0].get("sifre", "")):
        # Log failed attempts so brute force can be spotted. There is no verified actor, so
        # yapan is None and the attempted name goes into the details.
        await audit_log_yaz(
            yapan=None,
            eylem="GIRIS_DENEME_BASARISIZ",
            hedef_tip="KULLANICI",
            hedef_id=arama_kullanici_adi,
            hedef_aciklama=f"başarısız giriş: {arama_kullanici_adi}",
            detay={
                "denenen_kullanici": arama_kullanici_adi,
                "kullanici_var_mi": bool(res.data)  # unknown user vs. wrong password
            },
            basarili=False,
            request=request
        )
        raise HTTPException(status_code=401, detail="Hatalı giriş.")
    k = res.data[0]
    firma_pasif, firma_adi = False, ""
    shuttle_aktif, vale_aktif = True, False
    if k["rol"] != "SUPERADMIN" and k.get("firma_id"):
        f = (await run_query(supabase.table("firmalar").select("firma_adi, is_active, shuttle_aktif, vale_aktif").eq("id", k["firma_id"]))).data
        if f:
            firma_adi = f[0].get("firma_adi", "")
            shuttle_aktif = f[0].get("shuttle_aktif") is not False  # NULL counts as enabled
            vale_aktif = bool(f[0].get("vale_aktif"))
            if f[0].get("is_active", True) == False:
                if k["rol"] != "ADMIN": raise HTTPException(status_code=403, detail=f"PASIF_FIRMA:{firma_adi}")
                firma_pasif = True
    return {"mesaj": "Başarılı", "token": token_olustur({"kullanici_adi": k["kullanici_adi"], "rol": k["rol"], "firma_id": k.get("firma_id"), "marka": k.get("marka", "Genel"), "arac_id": k.get("arac_id"), "sube_id": k.get("sube_id")}), "rol": k["rol"], "firma_id": k.get("firma_id"), "kullanici_adi": k["kullanici_adi"],
            # Display name for panel headers. It is kept out of the JWT, which only carries
            # identity and permissions, so renaming someone does not require a new token.
            "gorunen_ad": (k.get("gorunen_ad") or "").strip() or k["kullanici_adi"], "marka": k.get("marka", "Genel"), "arac_id": k.get("arac_id"), "sube_id": k.get("sube_id"), "firma_pasif": firma_pasif, "firma_adi": firma_adi, "shuttle_aktif": shuttle_aktif, "vale_aktif": vale_aktif}

@app.post("/logout")
async def logout(request: Request, credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Log out: add the token's jti to revoked_tokens so it is never accepted again.

    Always answers "logged out", even for invalid tokens or when recording fails, because
    the client clears its stored session in any case.
    """
    token = credentials.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
    except jwt.InvalidTokenError:
        return {"mesaj": "Çıkış yapıldı."}
    
    jti = payload.get("jti")
    exp = payload.get("exp")
    kullanici_adi = payload.get("kullanici_adi")
    
    if not jti:
        # Tokens issued before jti existed cannot be revoked individually.
        return {"mesaj": "Çıkış yapıldı."}
    
    try:
        expire_dt = datetime.fromtimestamp(exp, tz=timezone.utc)
        await run_query(supabase.table("revoked_tokens").insert({
            "jti": jti,
            "kullanici_adi": kullanici_adi,
            "sebep": "LOGOUT",
            "iptal_eden": kullanici_adi,
            "iptal_tarihi": datetime.now(timezone.utc).isoformat(),
            "expire_tarihi": expire_dt.isoformat()
        }))
        
        # Mark it revoked in Redis too, so the very next request is rejected.
        try:
            await redis_client.setex(f"revoked:{jti}", 3600, "1")
        except Exception:
            pass  # the revocation is already stored in the database, which is checked on a cache miss
        # And evict it from the in-process auth cache, otherwise it would stay valid for up
        # to _AUTH_CACHE_TTL seconds.
        _auth_cache.pop(jti, None)

        await audit_log_yaz(
            yapan=payload,
            eylem="LOGOUT",
            hedef_tip="KULLANICI",
            hedef_id=kullanici_adi,
            hedef_aciklama=f"çıkış yapıldı: {kullanici_adi}",
            request=request
        )
    except Exception as e:
        # Report the failure but still answer "logged out"; the client discards the token.
        # Fail open: if the revocation was not stored, the token stays valid until it expires
        # ("Failure handling" in docs/SECURITY-PRIVACY.md).
        sentry_sdk.capture_exception(e)
    
    return {"mesaj": "Çıkış yapıldı."}

@app.get("/firmalar")
def firmalari_getir(yetkili = Depends(yetki_kontrol)):
    """SUPERADMIN only: every company row."""
    if yetkili["rol"] != "SUPERADMIN":
        raise HTTPException(status_code=403, detail="Bu listeye erişim yetkiniz yok.")
    return supabase.table("firmalar").select("*").execute().data

@app.get("/firma-admin-rehberi")
def firma_admin_rehberi(yetkili = Depends(yetki_kontrol)):
    """SUPERADMIN only: contact list of every company's ADMIN users.

    Companies without an admin still get one empty row so they show up in the table.
    """
    if yetkili["rol"] != "SUPERADMIN":
        raise HTTPException(status_code=403, detail="Bu listeye erişim yetkiniz yok.")
    firmalar = supabase.table("firmalar").select("id, firma_adi, is_active").execute().data
    adminler = supabase.table("kullanicilar").select("firma_id, kullanici_adi, gorunen_ad, email, telefon").eq("rol", "ADMIN").execute().data
    admin_map = {}
    for a in adminler:
        admin_map.setdefault(a.get("firma_id"), []).append(a)
    rehber = []
    for f in firmalar:
        f_adminler = admin_map.get(f["id"], [])
        if f_adminler:
            for a in f_adminler:
                rehber.append({
                    "firma_adi": f.get("firma_adi"), "is_active": f.get("is_active", True),
                    # kullanici_adi is the identity the edit/reset actions use; gorunen_ad is
                    # what the table shows (the UI falls back to the username).
                    "kullanici_adi": a.get("kullanici_adi"), "gorunen_ad": a.get("gorunen_ad"),
                    "email": a.get("email"), "telefon": a.get("telefon")
                })
        else:
            rehber.append({
                "firma_adi": f.get("firma_adi"), "is_active": f.get("is_active", True),
                "kullanici_adi": None, "gorunen_ad": None, "email": None, "telefon": None
            })
    return rehber

@app.post("/firma-ekle")
async def firma_ekle(bilgi: FirmaIstek, request: Request, yetkili = Depends(yetki_kontrol)):
    """SUPERADMIN only: create a customer company with its modules, quotas and KVKK details."""
    if yetkili["rol"] != "SUPERADMIN": raise HTTPException(status_code=403, detail="Yetkisiz.")

    temiz_firma_adi = validate_metin(bilgi.firma_adi, "Firma adı", max_uzunluk=80)

    # KVKK details are required. For passengers and valet customers the data controller is
    # this company, not the platform, and the privacy notice on the customer page is filled in
    # with the company's legal name and contact channel. A company created without them would
    # show its customers an incomplete notice. kvkk_unvan is the full registered legal name,
    # not firma_adi (the short name shown on screens).
    temiz_kvkk_unvan = validate_metin(bilgi.kvkk_unvan, "KVKK tam ünvanı", max_uzunluk=120, min_uzunluk=3)
    temiz_kvkk_basvuru = validate_kvkk_kanal(bilgi.kvkk_basvuru_kanali)
    # The address is optional: the legal name and contact channel satisfy the notice duty
    # (KVKK art. 10); an address only matters for written applications. Empty becomes NULL.
    temiz_kvkk_adres = (validate_kvkk_kanal(bilgi.kvkk_adres, "KVKK tebligat adresi", max_uzunluk=200)
                        if (bilgi.kvkk_adres or "").strip() else None)

    if bilgi.sistem_modu not in ("HARITA", "DURAK"):
        raise HTTPException(status_code=400, detail="Geçersiz sistem modu (HARITA veya DURAK olmalı).")

    # At least one module (shuttle or valet) must be on, or the company could do nothing.
    if not bilgi.shuttle_aktif and not bilgi.vale_aktif:
        raise HTTPException(status_code=400, detail="En az bir hizmet seçilmeli: Shuttle veya Vale.")
    # Valet quota: number of valet staff, the pricing basis of the valet module.
    vale_kota = bilgi.vale_kota if (bilgi.vale_kota and bilgi.vale_kota >= 0) else 0
    if vale_kota > 500:
        raise HTTPException(status_code=400, detail="Vale kotası çok yüksek.")
    if bilgi.vale_aktif and vale_kota < 1:
        raise HTTPException(status_code=400, detail="Vale hizmeti seçildiyse vale kotası en az 1 olmalı.")

    # Branch setup and the vehicle quota (the pricing basis of the shuttle module).
    subeli = bool(bilgi.subeli)
    toplam_kota = bilgi.toplam_kota if (bilgi.toplam_kota and bilgi.toplam_kota >= 1) else 1
    if toplam_kota > 1000:
        raise HTTPException(status_code=400, detail="Toplam araç kotası çok yüksek.")
    max_sube = bilgi.max_sube if subeli else 0
    if subeli and not (1 <= max_sube <= 100):
        raise HTTPException(status_code=400, detail="Şubeli firma için azami şube sayısı 1–100 arası olmalı.")

    res = await run_query(supabase.table("firmalar").insert({
        "firma_adi": temiz_firma_adi,
        "sistem_modu": bilgi.sistem_modu,
        "vale_aktif": bilgi.vale_aktif,
        "shuttle_aktif": bilgi.shuttle_aktif,
        "vale_kota": vale_kota,
        "is_active": True,
        "subeli": subeli,
        "max_sube": max_sube,
        "toplam_kota": toplam_kota,
        "kvkk_unvan": temiz_kvkk_unvan,
        "kvkk_basvuru_kanali": temiz_kvkk_basvuru,
        "kvkk_adres": temiz_kvkk_adres,
        "kayit_tarihi": datetime.now(timezone.utc).isoformat()
    }))
    
    yeni_firma = res.data[0]
    
    await audit_log_yaz(
        yapan=yetkili,
        eylem="FIRMA_EKLE",
        hedef_tip="FIRMA",
        hedef_id=yeni_firma.get("id"),
        hedef_aciklama=f"firma eklendi: {temiz_firma_adi}",
        detay={
            "firma_adi": temiz_firma_adi,
            "sistem_modu": bilgi.sistem_modu,
            "subeli": subeli,
            "max_sube": max_sube,
            "toplam_kota": toplam_kota,
            # Log that the KVKK fields are filled, not their content, to keep personal data
            # out of the audit log (same as the "email_dolu" flag elsewhere).
            "kvkk_alanlari_dolu": True
        },
        request=request
    )
    await manager.broadcast_superadmin("YENILE")
    return {"mesaj": "Firma eklendi", "data": yeni_firma}

@app.post("/kullanici-ekle")
async def kullanici_ekle(bilgi: KullaniciIstek, request: Request, yetkili = Depends(yetki_kontrol)):
    """Panels: create a staff user and return an invitation link for setting the password.

    Enforces company isolation, the role hierarchy (with one delegation exception, below),
    branch scope, module flags and the valet quota. Creating a VALE user also creates the
    valet's virtual vehicle.
    """
    if bilgi.telefon and not re.match(r'^\+90[0-9]{10}$', bilgi.telefon): raise HTTPException(status_code=400, detail="Geçersiz telefon formatı.")
    
    # E-mail addresses are stored lowercased so the uniqueness check is case-insensitive.
    if bilgi.email:
        bilgi.email = bilgi.email.strip().lower()
        if not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", bilgi.email):
            raise HTTPException(status_code=400, detail="Geçersiz e-posta formatı.")

    if bilgi.rol not in ROL_HIYERARSI:
        raise HTTPException(status_code=400, detail="Geçersiz rol.")

    if yetkili["rol"] != "SUPERADMIN" and yetkili.get("firma_id") != bilgi.firma_id: raise HTTPException(status_code=403, detail="Yetkisiz işlem!")
    if bilgi.rol == "SUPERADMIN" and yetkili["rol"] != "SUPERADMIN": raise HTTPException(status_code=403, detail="Yetki verme hakkınız yok!")
    
    # SUPERADMIN only creates company ADMINs. Everyone else in a company is added by that
    # company's admins, which keeps a single SUPERADMIN and a clear audit trail.
    if yetkili["rol"] == "SUPERADMIN" and bilgi.rol != "ADMIN":
        raise HTTPException(status_code=400, detail="SUPERADMIN sadece ADMIN ekleyebilir. Diğer roller, ilgili firma admininden eklenmelidir.")

    # Branch of the new user.
    hedef_sube_id = None
    if yetkili["rol"] != "SUPERADMIN":
        yetkili_sube = yetkili.get("sube_id")
        if yetkili_sube:
            # A branch-scoped creator can only add users to their own branch.
            hedef_sube_id = yetkili_sube
        elif bilgi.sube_id:
            # Headquarters may put only an ADMIN (the branch manager) into a branch. The
            # branch's own staff are then added by that manager; HQ sets branches up but does
            # not run them.
            if bilgi.rol != "ADMIN":
                raise HTTPException(status_code=403, detail="Merkez başka şubeye yalnız şube yöneticisi (ADMIN) atayabilir. Şube personelini o şubenin yöneticisi ekler.")
            s = (await run_query(supabase.table("subeler").select("id").eq("id", bilgi.sube_id).eq("firma_id", bilgi.firma_id))).data
            if not s:
                raise HTTPException(status_code=400, detail="Geçersiz şube veya bu firmaya ait değil.")
            hedef_sube_id = bilgi.sube_id

    # Drivers only exist in companies with the shuttle module.
    if bilgi.rol == "SOFOR":
        _fs = (await run_query(supabase.table("firmalar").select("shuttle_aktif").eq("id", bilgi.firma_id))).data
        if _fs and _fs[0].get("shuttle_aktif") is False:
            raise HTTPException(status_code=400, detail="Bu firmada shuttle hizmeti tanımlı değil; şoför eklenemez.")

    # Role hierarchy: nobody may create a user at their own level or above. The one exception
    # is delegation: an ADMIN may create another ADMIN whose scope is strictly narrower
    # (HQ admin -> branch admin, branch admin -> brand admin, or, in a company without
    # branches, HQ admin -> brand admin).
    kapsam_daraliyor = False
    if yetkili["rol"] == "ADMIN" and bilgi.rol == "ADMIN":
        yetkili_sube = yetkili.get("sube_id")
        yetkili_marka = yetkili.get("marka") or "Genel"
        if not yetkili_sube and hedef_sube_id:
            kapsam_daraliyor = True  # HQ -> branch
        elif yetkili_sube and hedef_sube_id == yetkili_sube and yetkili_marka == "Genel" and bilgi.marka and bilgi.marka != "Genel":
            kapsam_daraliyor = True  # branch -> brand
        elif not yetkili_sube and not hedef_sube_id and yetkili_marka == "Genel" and bilgi.marka and bilgi.marka != "Genel":
            # HQ -> brand is only allowed when the company has no branches. In a company with
            # branches it would let HQ skip the branch level and create a company-wide brand admin.
            _f = (await run_query(supabase.table("firmalar").select("subeli").eq("id", bilgi.firma_id))).data
            if _f and not _f[0].get("subeli"):
                kapsam_daraliyor = True  # HQ -> brand (company without branches)
    if not kapsam_daraliyor and not yetki_hiyerarsi_kontrol(yetkili["rol"], bilgi.rol):
        raise HTTPException(status_code=403, detail="Eşit veya yüksek yetkili kullanıcı ekleyemezsiniz.")

    # Advisors and operations staff can only add people to their own brand.
    if yetkili["rol"] in ["DANISMAN", "OPERASYON"]:
        bilgi.marka = yetkili.get("marka", "Genel")

    temiz_kullanici_adi = validate_kullanici_adi(bilgi.kullanici_adi)
    
    # Check uniqueness with the normalised (lowercased) name, the same form that is stored.
    mevcut = await run_query(supabase.table("kullanicilar").select("kullanici_adi").eq("kullanici_adi", temiz_kullanici_adi))
    if mevcut.data:
        raise HTTPException(status_code=400, detail="Bu kullanıcı adı zaten kullanımda. Lütfen başka bir kullanıcı adı seçin.")
    
    if bilgi.email:
        mevcut_email = await run_query(supabase.table("kullanicilar").select("kullanici_adi").eq("email", bilgi.email))
        if mevcut_email.data:
            raise HTTPException(status_code=400, detail="Bu e-posta adresi zaten kayıtlı.")
    
    davet_token = secrets.token_hex(24)
    
    # A VALE user gets a virtual vehicle (araclar row with tip='VALE'), which ties valets into
    # the vehicle-centred data model. It is an implementation detail and never shown as a
    # vehicle. The vehicle is created first because kullanicilar.arac_id points to it; if the
    # user insert then fails, the except block below deletes it again.
    arac_id_valet = None
    if bilgi.rol == "VALE":
        f_vale = (await run_query(supabase.table("firmalar").select("vale_aktif, vale_kota").eq("id", bilgi.firma_id))).data
        if not f_vale or not f_vale[0].get("vale_aktif"):
            raise HTTPException(status_code=400, detail="Bu firmada vale hizmeti tanımlı değil.")
        # The valet quota counts valet staff and is separate from the vehicle quota.
        vale_kota = f_vale[0].get("vale_kota") or 0
        mevcut_vale = len((await run_query(supabase.table("kullanicilar").select("kullanici_adi").eq("firma_id", bilgi.firma_id).eq("rol", "VALE"))).data)
        if mevcut_vale >= vale_kota:
            raise HTTPException(status_code=400, detail=f"Vale kotanız dolu ({mevcut_vale}/{vale_kota}). Kota artışı için Shuttle & Valet Ops ekibiyle iletişime geçin.")
        arac_id_valet = str(uuid.uuid4())
        await run_query(supabase.table("araclar").insert({
            "id": arac_id_valet,
            # For a valet the plate field holds the username. No "Vale" prefix: the panels
            # already list these under valet headings.
            "plaka": temiz_kullanici_adi,
            "firma_id": bilgi.firma_id,
            "marka": bilgi.marka,
            "sube_id": hedef_sube_id,
            "tip": "VALE",
            "kayit_tarihi": datetime.now(timezone.utc).isoformat()
        }))

    # davet_token_olusturma records when the invitation was issued, for its expiry check.
    try:
        await run_query(supabase.table("kullanicilar").insert({
            "kullanici_adi": temiz_kullanici_adi,
            # Optional here. The person enters their own display name when setting a password
            # (required there); an admin may fill it in up front.
            "gorunen_ad": validate_gorunen_ad(bilgi.gorunen_ad) if (bilgi.gorunen_ad or "").strip() else None,
            "email": bilgi.email,
            "davet_token": davet_token,
            "davet_token_olusturma": datetime.now(timezone.utc).isoformat(),
            "rol": bilgi.rol,
            "firma_id": bilgi.firma_id,
            "marka": bilgi.marka,
            "sube_id": hedef_sube_id,
            "telefon": bilgi.telefon,
            "kayit_tarihi": datetime.now(timezone.utc).isoformat(),
            "arac_id": arac_id_valet
        }))
    except Exception:
        # Do not leave an orphaned virtual vehicle behind.
        if arac_id_valet:
            await run_query(supabase.table("araclar").delete().eq("id", arac_id_valet))
        raise
    if arac_id_valet:
        await manager.broadcast_firma(bilgi.firma_id, "YENILE")
    await audit_log_yaz(
        yapan=yetkili,
        eylem="PERSONEL_EKLE",
        hedef_tip="KULLANICI",
        hedef_id=bilgi.kullanici_adi,
        hedef_aciklama=f"personel eklendi: {bilgi.kullanici_adi} (rol: {bilgi.rol})",
        detay={
            "yeni_kullanici_adi": bilgi.kullanici_adi,
            "rol": bilgi.rol,
            "marka": bilgi.marka,
            "sube_id": hedef_sube_id,
            "email_var_mi": bool(bilgi.email),
            "telefon_var_mi": bool(bilgi.telefon)
        },
        request=request
    )
    
    return {"mesaj": "Personel eklendi.", "davet_linki": f"{BASE_URL.rstrip('/')}/sifre.html?token={davet_token}"}

@app.put("/sofor-arac-ata")
async def sofor_arac_ata(istek: AracAtaIstek, request: Request, yetkili = Depends(yetki_kontrol)):
    """Panels: assign a driver to a vehicle, subject to the active-vehicle quota."""
    if yetkili["rol"] not in ["ADMIN", "SUPERADMIN", "OPERASYON"]:
        raise HTTPException(status_code=403)

    if yetkili["rol"] != "SUPERADMIN":
        k1 = await run_query(supabase.table("kullanicilar").select("firma_id").eq("kullanici_adi", istek.kullanici_adi))
        if not k1.data or k1.data[0].get("firma_id") != yetkili.get("firma_id"): 
            raise HTTPException(status_code=403)
        k2 = await run_query(supabase.table("araclar").select("firma_id").eq("id", istek.yeni_arac_id))
        if not k2.data or k2.data[0].get("firma_id") != yetkili.get("firma_id"): 
            raise HTTPException(status_code=403)
    
    # One driver per vehicle.
    mevcut_sofor = await run_query(supabase.table("kullanicilar").select("kullanici_adi").eq("arac_id", istek.yeni_arac_id).eq("rol", "SOFOR").neq("kullanici_adi", istek.kullanici_adi))
    if mevcut_sofor.data:
        baska_sofor = mevcut_sofor.data[0].get("kullanici_adi", "bilinmeyen")
        raise HTTPException(
            status_code=409,
            detail=f"Bu araç zaten '{baska_sofor}' adlı şoföre atanmış. Önce onun atamasını kaldırın."
        )

    # Quota check. A vehicle counts as active when a driver is assigned to it, and the number
    # of active vehicles in the scope may not exceed the quota. The assignment is simulated
    # and the resulting set of active vehicles is counted:
    #   - moving a driver from vehicle X to Y frees X and fills Y, net zero, so swapping to a
    #     spare vehicle after a breakdown always works;
    #   - giving a vehicle to another driver adds one and is refused if over quota.
    arac_y = (await run_query(supabase.table("araclar").select("sube_id, firma_id").eq("id", istek.yeni_arac_id))).data
    if arac_y:
        y_sube = arac_y[0].get("sube_id")
        y_firma = arac_y[0].get("firma_id")
        aktif_kota, kapsam_arac_ids = await run_in_threadpool(kapsam_kota_bilgisi, y_firma, y_sube)
        soforlar = (await run_query(supabase.table("kullanicilar").select("kullanici_adi, arac_id").eq("firma_id", y_firma).eq("rol", "SOFOR"))).data
        aktif_araclar = set()
        for s in soforlar:
            aid = istek.yeni_arac_id if s.get("kullanici_adi") == istek.kullanici_adi else s.get("arac_id")
            if aid and aid in kapsam_arac_ids:
                aktif_araclar.add(aid)
        aktif_araclar.add(istek.yeni_arac_id)  # Y is active after this, even if the user is not listed as SOFOR
        if len(aktif_araclar) > aktif_kota:
            if y_sube:
                kota_mesaji = f"Bu şubenin aktif araç kotası dolu ({aktif_kota}). Önce bir şoförün atamasını kaldırın; ek kota için başka şubeden kaydırılabilir ya da Shuttle & Valet Ops ekibiyle iletişime geçin."
            else:
                kota_mesaji = f"Aktif araç kotası dolu ({aktif_kota}). Önce bir şoförün atamasını kaldırın ya da kota artışı için Shuttle & Valet Ops ekibiyle iletişime geçin."
            raise HTTPException(status_code=400, detail=kota_mesaji)

    if not (await run_query(supabase.table("kullanicilar").update({"arac_id": istek.yeni_arac_id}).eq("kullanici_adi", istek.kullanici_adi))).data: 
        raise HTTPException(status_code=404)
    
    # A SUPERADMIN token has no company, so look it up from the vehicle.
    if yetkili["rol"] != "SUPERADMIN":
        firma_id_hedef = yetkili.get("firma_id")
    else:
        _arac_firma = (await run_query(supabase.table("araclar").select("firma_id").eq("id", istek.yeni_arac_id))).data
        firma_id_hedef = _arac_firma[0]["firma_id"] if _arac_firma else None
    if firma_id_hedef:
        await manager.broadcast_firma(firma_id_hedef, "YENILE")
    
    arac_bilgi = (await run_query(supabase.table("araclar").select("plaka").eq("id", istek.yeni_arac_id))).data
    plaka_log = arac_bilgi[0].get("plaka", "bilinmeyen") if arac_bilgi else "bilinmeyen"
    
    await audit_log_yaz(
        yapan=yetkili,
        eylem="SOFOR_ARAC_ATA",
        hedef_tip="KULLANICI",
        hedef_id=istek.kullanici_adi,
        hedef_aciklama=f"şoför: {istek.kullanici_adi} → araç: {plaka_log}",
        detay={
            "sofor": istek.kullanici_adi,
            "arac_id": istek.yeni_arac_id,
            "arac_plaka": plaka_log
        },
        request=request
    )
    
    return {"mesaj": "Atandı."}

@app.get("/sofor-durumu")
def sofor_durumu(yetkili = Depends(yetki_kontrol)):
    """Driver app on start-up: current vehicle, whether a trip is running, plate and base point.

    The base point (branch, else HQ) is used for the "return to base" navigation button.
    """
    if yetkili["rol"] != "SOFOR": raise HTTPException(status_code=403)
    
    k = supabase.table("kullanicilar").select("arac_id").eq("kullanici_adi", yetkili["kullanici_adi"]).execute()
    arac_id = k.data[0].get("arac_id") if k.data else None
    
    rota_aktif = False
    arac_plaka = None
    merkez_lat = None
    merkez_lng = None

    if arac_id:
        a = supabase.table("araclar").select("rota_aktif, plaka, firma_id, sube_id").eq("id", arac_id).execute()
        if a.data:
            rota_aktif = a.data[0].get("rota_aktif", False)
            arac_plaka = a.data[0].get("plaka")
            merkez_lat, merkez_lng = referans_konum(a.data[0].get("firma_id"), a.data[0].get("sube_id"))

    return {"arac_id": arac_id, "rota_aktif": rota_aktif, "arac_plaka": arac_plaka, "merkez_lat": merkez_lat, "merkez_lng": merkez_lng}

def _gecerli_uuid(deger) -> bool:
    """Return True if the value is a valid UUID.

    While a session is ending, the frontend sometimes sends the string 'null' as an id.
    Checking first avoids a 500 from Supabase ("invalid input syntax for type uuid").
    """
    try:
        uuid.UUID(str(deger))
        return True
    except (ValueError, TypeError, AttributeError):
        return False

@app.get("/firma-talepleri")
def firma_talepleri(firma_id: str, marka: str = None, yetkili = Depends(yetki_kontrol)):
    """Panels: the company's requests for today's view, enriched with plate, stop and milestones.

    Includes requests opened today, plus two kinds marked devreden ("carried over"): active
    valet tasks opened on an earlier day, and older requests completed today.
    """
    if yetkili["rol"] != "SUPERADMIN" and yetkili.get("firma_id") != firma_id: raise HTTPException(status_code=403)
    if not _gecerli_uuid(firma_id): return []

    if yetkili["rol"] in ["OPERASYON", "DANISMAN"]:
        marka = yetkili.get("marka", "Genel")

    bugun_baslangic = tr_bugun_baslangic_utc_iso()

    def _marka_uygula(q):
        return q.eq("marka", marka) if (marka and marka != "Genel") else q

    # Requests opened today.
    talepler = _marka_uygula(
        supabase.table("talepler").select("*").eq("firma_id", firma_id).gte("kayit_tarihi", bugun_baslangic)
    ).execute().data or []
    goruldu = {t["id"] for t in talepler}

    # Active valet tasks from earlier days. /vale-gorevi has no date filter, so such a task is
    # still on the valet's phone and must also appear on the panels.
    # This is valet only on purpose: a shuttle day ends with "end trip" and sofor-rotasi is
    # filtered by day too, so the shuttle side is consistent as it is.
    devredenler = _marka_uygula(
        supabase.table("talepler").select("*").eq("firma_id", firma_id)
        .in_("durum", VALE_AKTIF_DURUMLAR).lt("kayit_tarihi", bugun_baslangic)
    ).execute().data or []

    # Requests opened earlier but completed today: the "completed today" list goes by the
    # completion time, not the creation time. The try/except keeps the panel working if the
    # tamamlanma_tarihi column has not been added to the database yet.
    try:
        bugun_bitenler = _marka_uygula(
            supabase.table("talepler").select("*").eq("firma_id", firma_id)
            .gte("tamamlanma_tarihi", bugun_baslangic).lt("kayit_tarihi", bugun_baslangic)
        ).execute().data or []
    except Exception as e:
        sentry_sdk.capture_exception(e)
        bugun_bitenler = []

    for t in devredenler + bugun_bitenler:
        if t["id"] not in goruldu:
            t["devreden"] = True  # shown as a "carried over" badge on the panel
            talepler.append(t)
            goruldu.add(t["id"])

    talepler = kapsam_sube_filtrele(yetkili, talepler)

    if not talepler: return []

    # Plates and vehicle types for all requests in one IN query.
    arac_idleri = [t["arac_id"] for t in talepler if t.get("arac_id")]
    if arac_idleri:
        araclar = supabase.table("araclar").select("id, plaka, tip").in_("id", list(set(arac_idleri))).execute().data
        arac_dict = {a["id"]: a for a in araclar}

        # arac_tip lets the panels group completed tasks by valet.
        for t in talepler:
            if t.get("arac_id"):
                _a = arac_dict.get(t["arac_id"])
                t["arac_plaka"] = _a.get("plaka", "Plaka Yok") if _a else "Plaka Yok"
                t["arac_tip"] = (_a.get("tip") or "SERVIS") if _a else "SERVIS"
            else:
                t["arac_plaka"] = "Araç Atanmadı"
                t["arac_tip"] = "SERVIS"
    # Task milestones (promised vs actual times) for the timeline on the panel. They ride on
    # this response, which the panel loads anyway, at the cost of one extra query. Only valet
    # tasks have milestones, so shuttle requests are skipped.
    _vale_talep_idleri = [t["id"] for t in talepler if str(t.get("gorev_tipi") or "").startswith("VALE_")]
    if _vale_talep_idleri:
        try:
            _etaplar = supabase.table("gorev_etaplari").select(
                "talep_id, etap, baslangic, hedef_varis, gercek_varis, sapma_dk, iptal_edildi, basma_mesafe_m, konum_bildirildi, basma_konum_yasi_sn, basma_dogruluk_m"
            ).in_("talep_id", _vale_talep_idleri).order("baslangic", desc=False).execute().data or []
            _etap_dict = {}
            for e in _etaplar:
                _etap_dict.setdefault(e["talep_id"], []).append(e)
            for t in talepler:
                if t["id"] in _etap_dict:
                    t["etaplar"] = _etap_dict[t["id"]]
        except Exception as e:
            # Display only: without milestones the panel still renders.
            sentry_sdk.capture_exception(e)

    # Stop names for DURAK-mode requests.
    duraklar = supabase.table("duraklar").select("id, durak_adi").eq("firma_id", firma_id).execute().data
    durak_dict = {d["id"]: d["durak_adi"] for d in duraklar}
    for t in talepler:
        if t.get("secilen_durak_id"):
            t["durak_adi"] = durak_dict.get(t["secilen_durak_id"], "Bilinmeyen Durak")
                
    return talepler

@app.get("/firma-araclari")
def firma_araclari(firma_id: str, marka: str = None, yetkili = Depends(yetki_kontrol)):
    """Panels: the company's vehicles (shuttle vehicles and valets) with driver and route names."""
    if yetkili["rol"] != "SUPERADMIN" and yetkili.get("firma_id") != firma_id: raise HTTPException(status_code=403)
    if not _gecerli_uuid(firma_id): return []

    # Advisors and operations staff only see their own brand.
    if yetkili["rol"] in ["OPERASYON", "DANISMAN"]:
        marka = yetkili.get("marka", "Genel")

    araclar = supabase.table("araclar").select("*").eq("firma_id", firma_id).execute().data
    araclar = kapsam_sube_filtrele(yetkili, araclar)

    if araclar:
        arac_ids = [a["id"] for a in araclar]
        tum_soforler = supabase.table("kullanicilar").select("arac_id, kullanici_adi, gorunen_ad, telefon").eq("firma_id", firma_id).in_("rol", ["SOFOR", "VALE"]).in_("arac_id", arac_ids).execute().data
        sofor_dict = {s["arac_id"]: s for s in tum_soforler}
        
        for a in araclar:
            sofor = sofor_dict.get(a["id"])
            # sofor_adi is display only: the display name, else the username. Never match on it.
            a["sofor_adi"] = ((sofor.get("gorunen_ad") or "").strip() or sofor["kullanici_adi"]) if sofor else ""
            a["sofor_tel"] = sofor["telefon"] if sofor else ""
    # Route names for the operations screen.
    guzergahlar = supabase.table("guzergahlar").select("id, guzergah_adi").eq("firma_id", firma_id).execute().data
    guz_dict = {g["id"]: g["guzergah_adi"] for g in guzergahlar}
    for a in araclar:
        if a.get("guzergah_id"):
            a["guzergah_adi"] = guz_dict.get(a["guzergah_id"])
    
    # Hide "retired" valets: virtual valet vehicles whose user was deleted but which were kept
    # because they have history (see personel_sil). Otherwise a deleted valet would still show
    # up as a card on the panels and could even be given a task. arac_rapor runs its own
    # query, so reports still include them.
    araclar = [a for a in araclar if (a.get("tip") or "SERVIS") != "VALE" or a.get("sofor_adi")]

    # With a brand filter, return that brand's vehicles plus the shared "Genel" ones.
    return [a for a in araclar if (a.get("marka") or "Genel") in [marka, "Genel"]] if marka and marka != "Genel" else araclar

@app.get("/firma-rapor")
def firma_rapor(firma_id: str, periyot: str = "gunluk", yetkili = Depends(yetki_kontrol)):
    """Panels: company-wide shuttle totals for a day, week or 30 days."""
    if yetkili["rol"] != "SUPERADMIN" and yetkili.get("firma_id") != firma_id:
        raise HTTPException(status_code=403, detail="Yetkisiz erişim.")

    # Periods start at midnight Turkish time.
    if periyot == "gunluk":
        baslangic = tr_bugun_baslangic_utc_iso()
    elif periyot == "haftalik":
        baslangic = tr_gun_oncesi_baslangic_utc_iso(7)
    else:
        baslangic = tr_gun_oncesi_baslangic_utc_iso(30)

    res = supabase.table("talepler").select("durum, mesafe_km, sube_id").eq("firma_id", firma_id).gte("kayit_tarihi", baslangic).execute().data
    res = kapsam_sube_filtrele(yetkili, res)
    basarili = [t for t in res if t["durum"] in ["YOLCU ALINDI", "YOLCU INDI", "TAMAM_ALINDI"]]
    tum_yapilan_yol = round(sum([(t.get("mesafe_km") or 0) for t in res]), 1)
    
    return {
        "toplam_basarili": len(basarili),
        "toplam_gelmeyen": len([t for t in res if t["durum"] in ["YOLCU GELMEDİ", "TAMAM_GELMEDI"]]),
        "toplam_yol_km": tum_yapilan_yol
    }

@app.get("/arac-rapor")
def arac_rapor(firma_id: str, periyot: str = "gunluk", yetkili = Depends(yetki_kontrol)):
    """Panels: per-vehicle report. Shuttle vehicles and valets get different columns.

    Uses a fixed number of queries (vehicles, requests, milestones, drivers) and joins
    them in memory instead of querying per vehicle.
    """
    if yetkili["rol"] != "SUPERADMIN" and yetkili.get("firma_id") != firma_id:
        raise HTTPException(status_code=403)

    if periyot == "gunluk":
        baslangic = tr_bugun_baslangic_utc_iso()
    elif periyot == "haftalik":
        baslangic = tr_gun_oncesi_baslangic_utc_iso(7)
    else:
        baslangic = tr_gun_oncesi_baslangic_utc_iso(30)

    araclar = supabase.table("araclar").select("id, plaka, sube_id, tip").eq("firma_id", firma_id).execute().data
    araclar = kapsam_sube_filtrele(yetkili, araclar)
    if not araclar:
        return []
        
    arac_ids = [a["id"] for a in araclar]

    tum_talepler = supabase.table("talepler").select("arac_id, durum, mesafe_km, gorev_tipi").eq("firma_id", firma_id).in_("arac_id", arac_ids).gte("kayit_tarihi", baslangic).execute().data

    # Valet distance answers a different question from talepler.mesafe_km and does not replace it:
    #   talepler.mesafe_km  how far the customer's car travelled (door <-> base, one leg)
    #   vale_yolu_km        how far the operation travelled (start -> door, door -> service)
    # It sums gorev_etaplari.mesafe_km, where each leg starts at the valet's own GPS position.
    # Two exclusions are deliberate: cancelled tasks (the valet may have turned back and the
    # real distance is unknown) and open legs (the work is not finished). It is still not a
    # real track, only the road distance between two points.
    vale_yolu = {}
    vale_arac_ids = [a["id"] for a in araclar if a.get("tip") == "VALE"]
    if vale_arac_ids:
        try:
            _etaplar = supabase.table("gorev_etaplari").select("arac_id, mesafe_km").in_(
                "arac_id", vale_arac_ids).gte("baslangic", baslangic).eq(
                "iptal_edildi", False).not_.is_("gercek_varis", "null").execute().data or []
            for _e in _etaplar:
                if _e.get("mesafe_km") is not None and _e.get("arac_id"):
                    vale_yolu[_e["arac_id"]] = vale_yolu.get(_e["arac_id"], 0) + float(_e["mesafe_km"])
        except Exception as _hata:
            sentry_sdk.capture_exception(_hata)  # an extra column; must not break the report

    tum_soforler = supabase.table("kullanicilar").select("arac_id, kullanici_adi, gorunen_ad").eq("firma_id", firma_id).in_("rol", ["SOFOR", "VALE"]).in_("arac_id", arac_ids).execute().data

    # Display name, else username.
    sofor_dict = {s["arac_id"]: ((s.get("gorunen_ad") or "").strip() or s["kullanici_adi"])
                  for s in tum_soforler if s.get("arac_id")}
    
    talep_dict = {a_id: [] for a_id in arac_ids}
    for t in tum_talepler:
        if t.get("arac_id"):
            talep_dict[t["arac_id"]].append(t)

    sonuc = []
    for arac in araclar:
        a_id = arac["id"]
        talepler = talep_dict.get(a_id, [])
        sofor_adi = sofor_dict.get(a_id, "Atanmamış")

        if arac.get("tip") == "VALE":
            # Cancelled tasks are not work the valet did, so they are shown in their own
            # column and not counted in the total (otherwise every mistaken entry would inflate
            # the valet's numbers).
            vale_iptal = [t for t in talepler if t["durum"] == "IPTAL_EDILDI"]
            # A retired valet (user deleted) is listed only with real work in the period;
            # otherwise it would add an all-zero row to every report.
            if a_id not in sofor_dict and len(talepler) == len(vale_iptal):
                continue
            vale_teslim = [t for t in talepler if t["durum"] in ["TAMAM_SERVIS", "TAMAM_MUSTERI"]]
            toplam_km = round(sum([(t.get("mesafe_km") or 0) for t in talepler]), 1)
            sonuc.append({
                "arac_id": a_id,
                "plaka": arac["plaka"],
                "tip": "VALE",
                "iptal_edilen": len(vale_iptal),
                # A retired valet has no user row, which would read "Atanmamış" (unassigned).
                # The virtual vehicle's plate is the valet's username, so use that instead.
                "sofor_adi": sofor_adi if sofor_adi != "Atanmamış" else (arac.get("plaka") or "Atanmamış"),
                "toplam_gorev": len(talepler) - len(vale_iptal),
                "teslim_edilen": len(vale_teslim),
                "toplam_km": toplam_km,
                # 0 when there is no data, matching the "toplam_km" column next to it.
                "vale_yolu_km": round(vale_yolu.get(a_id, 0), 1)
            })
        else:
            basarili = [t for t in talepler if t["durum"] in ["YOLCU ALINDI", "YOLCU INDI", "TAMAM_ALINDI"]]
            gelmeyen = [t for t in talepler if t["durum"] in ["YOLCU GELMEDİ", "TAMAM_GELMEDI"]]
            toplam_km = round(sum([(t.get("mesafe_km") or 0) for t in talepler]), 1)
            toplama = len([t for t in basarili if t.get("gorev_tipi") == "TOPLAMA"])
            dagitim = len([t for t in basarili if t.get("gorev_tipi") == "DAGITIM"])

            sonuc.append({
                "arac_id": a_id,
                "plaka": arac["plaka"],
                "tip": "SERVIS",
                "sofor_adi": sofor_adi,
                "basarili": len(basarili),
                "gelmeyen": len(gelmeyen),
                "toplam_km": toplam_km,
                "toplama_sayisi": toplama,
                "dagitim_sayisi": dagitim
            })

    return sonuc

# ============================================================
# PUNCTUALITY REPORT (promised vs actual time)
# ============================================================
# Built from gorev_etaplari (db/gorev_etaplari.sql). Valet work is audited through timestamps
# rather than live GPS, and this is the report managers see.
#
# The thresholds live here only. The server makes the judgements (on time, needs attention)
# and admin.js just draws them, so "on time" cannot mean different things in two files.
# The basma_* thresholds must match the milestone badge in operasyon.js (ETAP_MESAFE_ESIGI_M,
# ETAP_KONUM_TAZE_SN, ETAP_KONUM_DOGRULUK_M). Change them together.
DAKIKLIK_ZAMANINDA_ESIK_DK = 10    # |deviation| up to this many minutes counts as on time
DAKIKLIK_MESAFE_ESIK_M = 300       # button pressed farther than this from the target = marked remotely
DAKIKLIK_KONUM_TAZE_SN = 60        # GPS readings older than this are not used to judge distance
DAKIKLIK_KONUM_DOGRULUK_M = 200    # nor are readings less accurate than this
DAKIKLIK_ASGARI_ORNEK = 5          # below this many samples, no "consistently late" flag


@app.get("/dakiklik-raporu")
def dakiklik_raporu(firma_id: str, periyot: str = "aylik", yetkili = Depends(yetki_kontrol)):
    """Per-valet punctuality: average deviation, on-time rate and data-quality flags.

    ADMIN and SUPERADMIN only. This is a staff performance report; advisors create tasks
    and operations runs the field, and neither is meant to evaluate staff.
    """
    if yetkili["rol"] not in ["ADMIN", "SUPERADMIN"]:
        raise HTTPException(status_code=403)
    if yetkili["rol"] != "SUPERADMIN" and yetkili.get("firma_id") != firma_id:
        raise HTTPException(status_code=403)
    if not _gecerli_uuid(firma_id):
        return {"valeler": [], "ozet": None}

    if periyot == "gunluk":
        baslangic = tr_bugun_baslangic_utc_iso()
    elif periyot == "haftalik":
        baslangic = tr_gun_oncesi_baslangic_utc_iso(7)
    else:
        baslangic = tr_gun_oncesi_baslangic_utc_iso(30)

    # Only closed legs that had a promise can be measured:
    #   - hedef_varis NULL: no promise was made (no valet position, or Mapbox did not answer)
    #   - gercek_varis NULL: the leg is still open
    #   - iptal_edildi: the task was cancelled, which says nothing about the valet
    # Legs that never have a destination drop out through the same filter.
    etaplar = supabase.table("gorev_etaplari").select(
        "vale_kullanici_adi, sube_id, etap, gorev_tipi, baslangic, sapma_dk, hedef_dakika, "
        "mesafe_km, basma_mesafe_m, konum_bildirildi, basma_konum_yasi_sn, basma_dogruluk_m"
    ).eq("firma_id", firma_id).gte("baslangic", baslangic).eq("iptal_edildi", False).not_.is_(
        "gercek_varis", "null").not_.is_("hedef_varis", "null").execute().data or []

    etaplar = kapsam_sube_filtrele(yetkili, etaplar)
    if not etaplar:
        return {"valeler": [], "ozet": None, "esikler": {
            "zamaninda_dk": DAKIKLIK_ZAMANINDA_ESIK_DK, "mesafe_m": DAKIKLIK_MESAFE_ESIK_M,
            "asgari_ornek": DAKIKLIK_ASGARI_ORNEK}}

    # Username -> display name. Retired valets have no user row, so their username is shown.
    adlar = {}
    for k in (supabase.table("kullanicilar").select("kullanici_adi, gorunen_ad").eq(
            "firma_id", firma_id).eq("rol", "VALE").execute().data or []):
        adlar[k["kullanici_adi"]] = (k.get("gorunen_ad") or "").strip() or k["kullanici_adi"]

    def okuma_guvenilir(e):
        """Return True if the press distance of this leg can be trusted.

        A stale or imprecise GPS reading would make a "pressed remotely" verdict unfair.
        This is deliberately stricter than the single-record badge in operasyon.js, which is
        still shown when age/accuracy are unknown (older records). The report produces a
        count, and "marked remotely 6 times" is a much heavier claim than one neutral badge,
        so a record we cannot judge is not counted against the person.
        """
        yas = e.get("basma_konum_yasi_sn")
        dog = e.get("basma_dogruluk_m")
        if not isinstance(yas, int) or not isinstance(dog, int):
            return False   # reading quality unknown
        return yas <= DAKIKLIK_KONUM_TAZE_SN and dog <= DAKIKLIK_KONUM_DOGRULUK_M

    # Customer rating next to the measured punctuality, so measurement and perception can be
    # compared: a high deviation with a low rating points at lateness, a clean deviation with a
    # low rating points at something else. The denominators differ (punctuality counts legs,
    # surveys count tasks), so the rating is an average and not comparable with the leg count.
    puanlar = {}
    try:
        _anketler = supabase.table("memnuniyet_anketleri").select(
            "vale_kullanici_adi, puan_genel").eq("firma_id", firma_id).gte(
            "kayit_tarihi", baslangic).execute().data or []
        for a in _anketler:
            if isinstance(a.get("puan_genel"), int):
                puanlar.setdefault(a.get("vale_kullanici_adi") or "", []).append(a["puan_genel"])
    except Exception as e:
        sentry_sdk.capture_exception(e)   # the report still works without ratings

    gruplar = {}
    for e in etaplar:
        gruplar.setdefault(e.get("vale_kullanici_adi") or "", []).append(e)

    valeler = []
    for kadi, kayitlar in gruplar.items():
        sapmalar = [e["sapma_dk"] for e in kayitlar if isinstance(e.get("sapma_dk"), int)]
        if not sapmalar:
            continue
        # On time means |deviation| <= threshold, in both directions. With a one-sided check
        # (deviation <= threshold), arriving very early would count as on time, and a valet who
        # pressed every button without driving at all would look 100% punctual.
        zamaninda = len([x for x in sapmalar if abs(x) <= DAKIKLIK_ZAMANINDA_ESIK_DK])
        yeterli_veri = len(sapmalar) >= DAKIKLIK_ASGARI_ORNEK
        ort = round(sum(sapmalar) / len(sapmalar), 1)
        valeler.append({
            "vale_kullanici_adi": kadi,
            "ad": adlar.get(kadi) or kadi or "Bilinmiyor",
            "etap_sayisi": len(sapmalar),
            "ort_sapma_dk": ort,
            # "Worst" is the largest deviation in either direction. A plain max() would pick
            # the best record for a valet who is always early.
            "en_kotu_sapma_dk": max(sapmalar, key=abs),
            "zamaninda": zamaninda,
            "zamaninda_yuzde": round(100.0 * zamaninda / len(sapmalar), 1),
            "gec": len([x for x in sapmalar if x > DAKIKLIK_ZAMANINDA_ESIK_DK]),
            # The early count is needed: with a symmetric band, "0% on time, 0 late" would be
            # unreadable without it.
            "erken": len([x for x in sapmalar if x < -DAKIKLIK_ZAMANINDA_ESIK_DK]),
            # Data-quality flags: they measure how much the deviation can be trusted, not the
            # deviation itself.
            "konum_bildirilmeyen": len([e for e in kayitlar if e.get("konum_bildirildi") is False]),
            "uzaktan_basilan": len([e for e in kayitlar
                                    if isinstance(e.get("basma_mesafe_m"), int)
                                    and e["basma_mesafe_m"] > DAKIKLIK_MESAFE_ESIK_M
                                    and okuma_guvenilir(e)]),
            # No "needs attention" flag below DAKIKLIK_ASGARI_ORNEK samples: two late tasks may
            # just be one day of bad traffic.
            "yeterli_veri": yeterli_veri,
            "dikkat": yeterli_veri and ort > DAKIKLIK_ZAMANINDA_ESIK_DK,
            # None without surveys, never 0: ratings are 1-5, so 0 would read as a very bad
            # score. (Unlike vale_yolu_km in arac_rapor, where 0 is a real value.)
            "musteri_puani": (round(sum(puanlar[kadi]) / len(puanlar[kadi]), 1)
                              if puanlar.get(kadi) else None),
            "anket_sayisi": len(puanlar.get(kadi, [])),
        })

    valeler.sort(key=lambda v: v["ort_sapma_dk"], reverse=True)

    tum_sapmalar = [e["sapma_dk"] for e in etaplar if isinstance(e.get("sapma_dk"), int)]
    ozet = None
    if tum_sapmalar:
        zam = len([x for x in tum_sapmalar if abs(x) <= DAKIKLIK_ZAMANINDA_ESIK_DK])
        ozet = {
            "etap_sayisi": len(tum_sapmalar),
            "ort_sapma_dk": round(sum(tum_sapmalar) / len(tum_sapmalar), 1),
            "zamaninda_yuzde": round(100.0 * zam / len(tum_sapmalar), 1),
            "vale_sayisi": len(valeler),
        }

    return {"periyot": periyot, "ozet": ozet, "valeler": valeler, "esikler": {
        "zamaninda_dk": DAKIKLIK_ZAMANINDA_ESIK_DK, "mesafe_m": DAKIKLIK_MESAFE_ESIK_M,
        "asgari_ornek": DAKIKLIK_ASGARI_ORNEK}}


@app.get("/memnuniyet-raporu")
def memnuniyet_raporu(firma_id: str, periyot: str = "aylik", yetkili = Depends(yetki_kontrol)):
    """Satisfaction survey report: per-question averages, per-valet breakdown and comments.

    ADMIN and SUPERADMIN only, for the same reason as the punctuality report; free-text
    comments may also mention staff by name.
    """
    if yetkili["rol"] not in ["ADMIN", "SUPERADMIN"]:
        raise HTTPException(status_code=403)
    if yetkili["rol"] != "SUPERADMIN" and yetkili.get("firma_id") != firma_id:
        raise HTTPException(status_code=403)
    if not _gecerli_uuid(firma_id):
        return {"valeler": [], "yorumlar": [], "ozet": None}

    if periyot == "gunluk":
        baslangic = tr_bugun_baslangic_utc_iso()
    elif periyot == "haftalik":
        baslangic = tr_gun_oncesi_baslangic_utc_iso(7)
    else:
        baslangic = tr_gun_oncesi_baslangic_utc_iso(30)

    anketler = supabase.table("memnuniyet_anketleri").select(
        "vale_kullanici_adi, sube_id, marka, gorev_tipi, kayit_tarihi, yorum, yorum_yazildi, "
        "puan_genel, puan_dakiklik, puan_ilgi, puan_arac_durumu, puan_bilgilendirme"
    ).eq("firma_id", firma_id).gte("kayit_tarihi", baslangic).order(
        "kayit_tarihi", desc=True).execute().data or []

    anketler = kapsam_sube_filtrele(yetkili, anketler)
    if not anketler:
        return {"periyot": periyot, "ozet": None, "valeler": [], "yorumlar": [], "sorular": ANKET_SORU_ETIKETLERI}

    adlar = {}
    for k in (supabase.table("kullanicilar").select("kullanici_adi, gorunen_ad").eq(
            "firma_id", firma_id).eq("rol", "VALE").execute().data or []):
        adlar[k["kullanici_adi"]] = (k.get("gorunen_ad") or "").strip() or k["kullanici_adi"]

    def _ort(kayitlar, alan):
        """Average of one question, or None if nobody answered it.

        NULL means the question was skipped and is left out of the average (see
        db/memnuniyet_anketleri.sql). The panel shows None as a dash, not 0, since ratings
        are 1-5.
        """
        d = [k[alan] for k in kayitlar if isinstance(k.get(alan), int)]
        return round(sum(d) / len(d), 1) if d else None

    gruplar = {}
    for a in anketler:
        gruplar.setdefault(a.get("vale_kullanici_adi") or "", []).append(a)

    valeler = []
    for kadi, kayitlar in gruplar.items():
        valeler.append({
            "vale_kullanici_adi": kadi,
            "ad": adlar.get(kadi) or kadi or "Bilinmiyor",
            "anket_sayisi": len(kayitlar),
            "yorum_sayisi": len([k for k in kayitlar if k.get("yorum_yazildi")]),
            **{alan: _ort(kayitlar, alan) for alan in ANKET_SORU_ETIKETLERI},
        })
    # Lowest overall rating first: that is the row a manager wants to see first.
    valeler.sort(key=lambda v: (v["puan_genel"] is None, v["puan_genel"]))

    # Comments are erased after 90 days, so a missing comment may be erased or never written;
    # the yorum_yazildi flag tells the two apart.
    yorumlar = [{
        "kayit_tarihi": a.get("kayit_tarihi"),
        "ad": adlar.get(a.get("vale_kullanici_adi")) or a.get("vale_kullanici_adi") or "Bilinmiyor",
        "marka": a.get("marka"),
        "gorev_tipi": a.get("gorev_tipi"),
        "puan_genel": a.get("puan_genel"),
        "yorum": a.get("yorum"),
    } for a in anketler if a.get("yorum")]

    ozet = {
        "anket_sayisi": len(anketler),
        "yorum_sayisi": len([a for a in anketler if a.get("yorum_yazildi")]),
        "vale_sayisi": len(valeler),
        **{alan: _ort(anketler, alan) for alan in ANKET_SORU_ETIKETLERI},
    }
    return {"periyot": periyot, "ozet": ozet, "valeler": valeler,
            "yorumlar": yorumlar, "sorular": ANKET_SORU_ETIKETLERI}


@app.get("/firma-detay/{firma_id}")
def firma_detay(firma_id: str, yetkili = Depends(yetki_kontrol)):
    """Panels: company name, HQ location, operating mode and module flags."""
    if yetkili["rol"] != "SUPERADMIN" and yetkili.get("firma_id") != firma_id: raise HTTPException(status_code=403)
    res = supabase.table("firmalar").select("firma_adi, merkez_lat, merkez_lng, sistem_modu, vale_aktif, shuttle_aktif, vale_kota").eq("id", firma_id).execute()
    return res.data[0] if res.data else {"firma_adi": "Sistem"}

@app.get("/firma-personelleri/{firma_id}")
def personel_getir(firma_id: str, yetkili: dict = Depends(yetki_kontrol)):
    """Panels: the company's staff, with the plate of each person's assigned vehicle."""
    if yetkili["rol"] != "SUPERADMIN" and yetkili.get("firma_id") != firma_id:
        raise HTTPException(status_code=403)

    query = supabase.table("kullanicilar").select("*").eq("firma_id", firma_id).in_("rol", ["ADMIN", "DANISMAN", "SOFOR", "OPERASYON", "VALE"])
    
    # Advisors and operations staff only see their own brand.
    if yetkili["rol"] in ["DANISMAN", "OPERASYON"]:
        yetkili_marka = yetkili.get("marka", "Genel")
        query = query.eq("marka", yetkili_marka)

    personeller = query.execute().data
    personeller = kapsam_sube_filtrele(yetkili, personeller)

    # All plates in one query instead of one per person.
    araclar = supabase.table("araclar").select("id, plaka").eq("firma_id", firma_id).execute().data
    arac_dict = {a["id"]: a["plaka"] for a in araclar}
    
    for p in personeller:
        if p.get("arac_id"):
            p["arac_plaka"] = arac_dict.get(p["arac_id"], "Bilinmeyen Araç")
            
    return personeller

@app.delete("/personel-sil/{kullanici_adi}")
async def personel_sil(kullanici_adi: str, request: Request, yetkili = Depends(yetki_kontrol)):
    """Panels: delete a staff user and invalidate their tokens.

    For a valet, the virtual vehicle is deleted only if it has no task history (see below).
    """
    if kullanici_adi == yetkili.get("kullanici_adi"):
        raise HTTPException(status_code=400, detail="Kendi yöneticilik hesabınızı silemezsiniz.")

    hedef_res = await run_query(supabase.table("kullanicilar").select("firma_id, rol, sube_id, arac_id").eq("kullanici_adi", kullanici_adi))
    if not hedef_res.data:
        raise HTTPException(status_code=404, detail="Personel bulunamadı.")
    hedef = hedef_res.data[0]

    if yetkili["rol"] != "SUPERADMIN":
        if hedef.get("firma_id") != yetkili.get("firma_id"):
            raise HTTPException(status_code=403, detail="Bu personeli silme yetkiniz yok.")

        if not yetki_hiyerarsi_kontrol(yetkili["rol"], hedef["rol"]):
            raise HTTPException(status_code=403, detail="Eşit veya yüksek yetkili kullanıcıya işlem yapamazsınız.")

        sube_yazma_guard(yetkili, hedef.get("sube_id"))
            
    # Collect audit details now; the row is gone afterwards.
    silme_detayi = {
        "silinen_rol": hedef.get("rol"),
        "silinen_firma_id": str(hedef.get("firma_id")) if hedef.get("firma_id") else None
    }

    # A valet with an active task cannot be deleted; the task and its customer would be
    # left hanging.
    if hedef.get("rol") == "VALE" and hedef.get("arac_id"):
        aktif_vale_gorev = (await run_query(supabase.table("talepler").select("id").eq("arac_id", hedef["arac_id"]).in_(
            "durum", VALE_AKTIF_DURUMLAR))).data
        if aktif_vale_gorev:
            raise HTTPException(status_code=409, detail="Bu valenin devam eden görevi var. Önce görev tamamlanmalı.")

    # Revoke the tokens only after every check that can refuse the deletion. Revoking earlier
    # would log the person out even when the deletion is refused, for example a valet in the
    # middle of a task.
    await kullanicinin_tum_tokenlarini_revoke_et(
        kullanici_adi=kullanici_adi,
        sebep="PERSONEL_SILINDI",
        iptal_eden=yetkili.get("kullanici_adi")
    )

    await run_query(supabase.table("kullanicilar").delete().eq("kullanici_adi", kullanici_adi))

    # Delete the valet's virtual vehicle, but only if it has no history. talepler.arac_id has a
    # foreign key to araclar.id, so deleting a vehicle that tasks point to fails with a 23503
    # FK violation, and deleting those tasks would erase report history. A vehicle with history
    # is therefore kept as a "retired valet": firma_araclari hides it from the operational lists
    # and reports keep showing it. Any code that deletes from araclar must handle this FK,
    # either by deleting the tasks first or by refusing with a clear 400.
    if hedef.get("rol") == "VALE" and hedef.get("arac_id"):
        gecmis = (await run_query(supabase.table("talepler").select("id").eq("arac_id", hedef["arac_id"]).limit(1))).data
        if not gecmis:
            await run_query(supabase.table("araclar").delete().eq("id", hedef["arac_id"]))
        await manager.broadcast_firma(hedef.get("firma_id", ""), "YENILE")

    await audit_log_yaz(
        yapan=yetkili,
        eylem="PERSONEL_SIL",
        hedef_tip="KULLANICI",
        hedef_id=kullanici_adi,
        hedef_aciklama=f"personel: {kullanici_adi} (rol: {hedef.get('rol')})",
        detay=silme_detayi,
        request=request
    )
    
    return {"mesaj": "Personel başarıyla silindi."}

@app.post("/sube-ekle")
async def sube_ekle(bilgi: SubeIstek, request: Request, yetkili = Depends(yetki_kontrol)):
    """Create a branch. Only SUPERADMIN or a headquarters ADMIN (no sube_id) may do this."""
    if yetkili["rol"] not in ["SUPERADMIN", "ADMIN"]:
        raise HTTPException(status_code=403, detail="Şube ekleme yetkiniz yok.")
    if yetkili["rol"] != "SUPERADMIN" and yetkili.get("sube_id"):
        raise HTTPException(status_code=403, detail="Şube yönetimi yalnız merkez yöneticisinindir.")
    if yetkili["rol"] != "SUPERADMIN" and bilgi.firma_id != yetkili.get("firma_id"):
        raise HTTPException(status_code=403, detail="Bu firmaya yetkiniz yok.")

    # Same allowed characters as brand names.
    sube_adi = (bilgi.sube_adi or "").strip()
    if not sube_adi:
        raise HTTPException(status_code=400, detail="Şube adı boş olamaz.")
    if len(sube_adi) > 60:
        raise HTTPException(status_code=400, detail="Şube adı en fazla 60 karakter olabilir.")
    if not re.match(r"^[a-zA-Z0-9ğüşıöçĞÜŞİÖÇ\s\-_.,&/]+$", sube_adi):
        raise HTTPException(status_code=400, detail="Şube adı sadece harf, rakam ve temel noktalama içerebilir.")

    firma_res = (await run_query(supabase.table("firmalar").select("subeli, max_sube, toplam_kota").eq("id", bilgi.firma_id))).data
    if not firma_res:
        raise HTTPException(status_code=404, detail="Firma bulunamadı.")
    firma = firma_res[0]
    if not firma.get("subeli"):
        raise HTTPException(status_code=400, detail="Bu firma şubeli değil; önce firmayı şubeli yapın.")

    mevcut = (await run_query(supabase.table("subeler").select("id, aktif_kota").eq("firma_id", bilgi.firma_id))).data
    if len(mevcut) >= (firma.get("max_sube") or 0):
        raise HTTPException(status_code=400, detail=f"Azami şube sayısına ulaşıldı ({firma.get('max_sube') or 0}). Daha fazlası için kota/fiyat artışı gerekir.")
    # The branch quotas together may not exceed the company's total vehicle quota.
    aktif_kota = max(0, bilgi.aktif_kota or 0)
    mevcut_toplam = sum((s.get("aktif_kota") or 0) for s in mevcut)
    toplam_kota = firma.get("toplam_kota") or 0
    if mevcut_toplam + aktif_kota > toplam_kota:
        raise HTTPException(status_code=400, detail=f"Toplam araç kotası aşıldı (toplam {toplam_kota}, kalan {toplam_kota - mevcut_toplam}).")

    res = await run_query(supabase.table("subeler").insert({
        "firma_id": bilgi.firma_id, "sube_adi": sube_adi, "aktif_kota": aktif_kota, "aktif": True
    }))
    yeni_sube = res.data[0] if res.data else None

    await audit_log_yaz(
        yapan=yetkili, eylem="SUBE_EKLE", hedef_tip="SUBE",
        hedef_id=(yeni_sube or {}).get("id"),
        hedef_aciklama=f"şube eklendi: {sube_adi} (aktif_kota {aktif_kota})",
        detay={"firma_id": bilgi.firma_id, "sube_adi": sube_adi, "aktif_kota": aktif_kota},
        request=request
    )
    await manager.broadcast_firma(bilgi.firma_id, "YENILE")
    return {"mesaj": "Şube eklendi", "data": yeni_sube}


@app.get("/sube-listele/{firma_id}")
def sube_listele(firma_id: str, yetkili = Depends(yetki_kontrol)):
    """Admins only: the company's branches with quota totals (handed out and remaining)."""
    if yetkili["rol"] not in ["SUPERADMIN", "ADMIN"]:
        raise HTTPException(status_code=403, detail="Yetkisiz.")
    if yetkili["rol"] != "SUPERADMIN" and yetkili.get("firma_id") != firma_id:
        raise HTTPException(status_code=403, detail="Bu firmaya yetkiniz yok.")
    firma_res = supabase.table("firmalar").select("subeli, max_sube, toplam_kota").eq("id", firma_id).execute().data
    if not firma_res:
        raise HTTPException(status_code=404, detail="Firma bulunamadı.")
    firma = firma_res[0]
    subeler = supabase.table("subeler").select("*").eq("firma_id", firma_id).order("created_at").execute().data
    dagitilmis = sum((s.get("aktif_kota") or 0) for s in subeler)
    toplam = firma.get("toplam_kota") or 0
    return {
        "subeli": bool(firma.get("subeli")),
        "max_sube": firma.get("max_sube") or 0,
        "toplam_kota": toplam,
        "dagitilmis": dagitilmis,
        "kalan": toplam - dagitilmis,
        "sube_sayisi": len(subeler),
        "subeler": subeler
    }


@app.put("/sube-guncelle")
async def sube_guncelle(bilgi: SubeGuncelleIstek, request: Request, yetkili = Depends(yetki_kontrol)):
    """Rename a branch, change its quota or toggle it. SUPERADMIN or a headquarters ADMIN."""
    if yetkili["rol"] not in ["SUPERADMIN", "ADMIN"]:
        raise HTTPException(status_code=403, detail="Şube güncelleme yetkiniz yok.")
    if yetkili["rol"] != "SUPERADMIN" and yetkili.get("sube_id"):
        raise HTTPException(status_code=403, detail="Şube yönetimi yalnız merkez yöneticisinindir.")
    sube_res = (await run_query(supabase.table("subeler").select("*").eq("id", bilgi.sube_id))).data
    if not sube_res:
        raise HTTPException(status_code=404, detail="Şube bulunamadı.")
    sube = sube_res[0]
    firma_id = sube.get("firma_id")
    if yetkili["rol"] != "SUPERADMIN" and firma_id != yetkili.get("firma_id"):
        raise HTTPException(status_code=403, detail="Bu firmaya yetkiniz yok.")

    guncelleme = {}
    if bilgi.sube_adi is not None:
        ad = bilgi.sube_adi.strip()
        if not ad or len(ad) > 60 or not re.match(r"^[a-zA-Z0-9ğüşıöçĞÜŞİÖÇ\s\-_.,&/]+$", ad):
            raise HTTPException(status_code=400, detail="Geçersiz şube adı.")
        guncelleme["sube_adi"] = ad
    if bilgi.aktif is not None:
        guncelleme["aktif"] = bool(bilgi.aktif)
    if bilgi.aktif_kota is not None:
        yeni_kota = max(0, bilgi.aktif_kota)
        # Other branches' quotas plus the new value may not exceed the company total.
        firma_res = (await run_query(supabase.table("firmalar").select("toplam_kota").eq("id", firma_id))).data
        toplam_kota = (firma_res[0].get("toplam_kota") or 0) if firma_res else 0
        digerleri = (await run_query(supabase.table("subeler").select("aktif_kota").eq("firma_id", firma_id).neq("id", bilgi.sube_id))).data
        diger_toplam = sum((s.get("aktif_kota") or 0) for s in digerleri)
        if diger_toplam + yeni_kota > toplam_kota:
            raise HTTPException(status_code=400, detail=f"Toplam kota aşıldı (toplam {toplam_kota}, diğer şubeler {diger_toplam}, bu şubeye en fazla {toplam_kota - diger_toplam}).")
        guncelleme["aktif_kota"] = yeni_kota

    if not guncelleme:
        raise HTTPException(status_code=400, detail="Güncellenecek alan yok.")

    await run_query(supabase.table("subeler").update(guncelleme).eq("id", bilgi.sube_id))
    await audit_log_yaz(
        yapan=yetkili, eylem="SUBE_GUNCELLE", hedef_tip="SUBE",
        hedef_id=bilgi.sube_id, hedef_aciklama=f"şube güncellendi: {sube.get('sube_adi')}",
        detay=guncelleme, request=request
    )
    await manager.broadcast_firma(firma_id, "YENILE")
    return {"mesaj": "Şube güncellendi", "data": guncelleme}


@app.delete("/sube-sil/{sube_id}")
async def sube_sil(sube_id: str, request: Request, yetkili = Depends(yetki_kontrol)):
    """Delete an empty branch. SUPERADMIN or a headquarters ADMIN."""
    if yetkili["rol"] not in ["SUPERADMIN", "ADMIN"]:
        raise HTTPException(status_code=403, detail="Şube silme yetkiniz yok.")
    if yetkili["rol"] != "SUPERADMIN" and yetkili.get("sube_id"):
        raise HTTPException(status_code=403, detail="Şube yönetimi yalnız merkez yöneticisinindir.")
    sube_res = (await run_query(supabase.table("subeler").select("*").eq("id", sube_id))).data
    if not sube_res:
        raise HTTPException(status_code=404, detail="Şube bulunamadı.")
    sube = sube_res[0]
    firma_id = sube.get("firma_id")
    if yetkili["rol"] != "SUPERADMIN" and firma_id != yetkili.get("firma_id"):
        raise HTTPException(status_code=403, detail="Bu firmaya yetkiniz yok.")
    # Refuse while anything still belongs to the branch. The check selects sube_id because
    # every table has that column (kullanicilar has no id column).
    for tablo in ("kullanicilar", "araclar", "guzergahlar", "talepler", "markalar"):
        if (await run_query(supabase.table(tablo).select("sube_id").eq("sube_id", sube_id).limit(1))).data:
            raise HTTPException(status_code=400, detail=f"Şubeye bağlı {tablo} kaydı var; önce onları başka şubeye taşıyın veya silin.")
    await run_query(supabase.table("subeler").delete().eq("id", sube_id))
    await audit_log_yaz(
        yapan=yetkili, eylem="SUBE_SIL", hedef_tip="SUBE",
        hedef_id=sube_id, hedef_aciklama=f"şube silindi: {sube.get('sube_adi')}",
        detay={"firma_id": firma_id, "sube_adi": sube.get("sube_adi")}, request=request
    )
    await manager.broadcast_firma(firma_id, "YENILE")
    return {"mesaj": "Şube silindi."}


@app.post("/firma-kota-guncelle")
async def firma_kota_guncelle(bilgi: FirmaKotaIstek, request: Request, yetkili = Depends(yetki_kontrol)):
    """SUPERADMIN only: change a company's limits (max branches, vehicle quota, valet quota).

    These limits follow the customer's price plan. A limit cannot drop below what is
    already in use.
    """
    if yetkili["rol"] != "SUPERADMIN":
        raise HTTPException(status_code=403, detail="Yetkisiz.")
    firma_res = (await run_query(supabase.table("firmalar").select("subeli, max_sube, toplam_kota").eq("id", bilgi.firma_id))).data
    if not firma_res:
        raise HTTPException(status_code=404, detail="Firma bulunamadı.")

    guncelleme = {}
    if bilgi.max_sube is not None:
        if not (0 <= bilgi.max_sube <= 100):
            raise HTTPException(status_code=400, detail="Azami şube 0–100 arası olmalı.")
        mevcut_sube = len((await run_query(supabase.table("subeler").select("id").eq("firma_id", bilgi.firma_id))).data)
        if bilgi.max_sube < mevcut_sube:
            raise HTTPException(status_code=400, detail=f"Azami şube ({bilgi.max_sube}), mevcut şube sayısından ({mevcut_sube}) az olamaz; önce şube silin.")
        guncelleme["max_sube"] = bilgi.max_sube
    if bilgi.toplam_kota is not None:
        if not (1 <= bilgi.toplam_kota <= 1000):
            raise HTTPException(status_code=400, detail="Toplam kota 1–1000 arası olmalı.")
        dagitilmis = sum((s.get("aktif_kota") or 0) for s in (await run_query(supabase.table("subeler").select("aktif_kota").eq("firma_id", bilgi.firma_id))).data)
        if bilgi.toplam_kota < dagitilmis:
            raise HTTPException(status_code=400, detail=f"Toplam kota ({bilgi.toplam_kota}), şubelere dağıtılmış kotadan ({dagitilmis}) az olamaz; önce şube kotalarını düşürün.")
        guncelleme["toplam_kota"] = bilgi.toplam_kota
    if bilgi.vale_kota is not None:
        if not (0 <= bilgi.vale_kota <= 500):
            raise HTTPException(status_code=400, detail="Vale kotası 0–500 arası olmalı.")
        mevcut_vale = len((await run_query(supabase.table("kullanicilar").select("kullanici_adi").eq("firma_id", bilgi.firma_id).eq("rol", "VALE"))).data)
        if bilgi.vale_kota < mevcut_vale:
            raise HTTPException(status_code=400, detail=f"Vale kotası ({bilgi.vale_kota}), kayıtlı vale sayısından ({mevcut_vale}) az olamaz; önce vale silin.")
        guncelleme["vale_kota"] = bilgi.vale_kota

    if not guncelleme:
        raise HTTPException(status_code=400, detail="Güncellenecek alan yok.")

    await run_query(supabase.table("firmalar").update(guncelleme).eq("id", bilgi.firma_id))
    await audit_log_yaz(
        yapan=yetkili, eylem="FIRMA_KOTA_GUNCELLE", hedef_tip="FIRMA",
        hedef_id=bilgi.firma_id, hedef_aciklama="firma kotası güncellendi",
        detay=guncelleme, request=request
    )
    await manager.broadcast_superadmin("YENILE")
    await manager.broadcast_firma(bilgi.firma_id, "YENILE")
    return {"mesaj": "Firma kotası güncellendi", "data": guncelleme}


@app.post("/firma-subeli-yap")
async def firma_subeli_yap(bilgi: SubeliYapIstek, request: Request, yetkili = Depends(yetki_kontrol)):
    """SUPERADMIN only: turn a company without branches into one with branches (one way).

    Existing records keep sube_id NULL and so belong to headquarters, which is treated as
    one more branch. Until quota is handed out to branches, HQ keeps the whole quota.
    """
    if yetkili["rol"] != "SUPERADMIN":
        raise HTTPException(status_code=403, detail="Yetkisiz.")

    firma_res = (await run_query(supabase.table("firmalar").select("firma_adi, subeli").eq("id", bilgi.firma_id))).data
    if not firma_res:
        raise HTTPException(status_code=404, detail="Firma bulunamadı.")
    if firma_res[0].get("subeli"):
        raise HTTPException(status_code=400, detail="Firma zaten şubeli. Azami şube sayısını Kota'dan güncelleyebilirsiniz.")
    if not (1 <= bilgi.max_sube <= 100):
        raise HTTPException(status_code=400, detail="Azami şube sayısı 1–100 arası olmalı.")

    await run_query(supabase.table("firmalar").update({"subeli": True, "max_sube": bilgi.max_sube}).eq("id", bilgi.firma_id))
    await audit_log_yaz(
        yapan=yetkili, eylem="FIRMA_SUBELI_YAP", hedef_tip="FIRMA",
        hedef_id=bilgi.firma_id,
        hedef_aciklama=f"firma şubeli yapıldı: {firma_res[0].get('firma_adi')}",
        detay={"max_sube": bilgi.max_sube}, request=request
    )
    await manager.broadcast_superadmin("YENILE")
    await manager.broadcast_firma(bilgi.firma_id, "YENILE")
    return {"mesaj": "Firma şubeli yapıldı. Mevcut kayıtlar Merkez şubede kalır; admin panelinden şube açılabilir."}


@app.get("/firma-markalari")
def firma_markalari_getir(firma_id: str, yetkili = Depends(yetki_kontrol)):
    """The company's brands (marka), also called departments in the UI, within the caller's branch scope."""
    if yetkili["rol"] != "SUPERADMIN" and yetkili.get("firma_id") != firma_id:
        raise HTTPException(status_code=403, detail="Bu firmaya yetkiniz yok.")
    markalar = supabase.table("markalar").select("*").eq("firma_id", firma_id).execute().data
    return kapsam_sube_filtrele(yetkili, markalar)

@app.post("/marka-ekle")
async def marka_ekle(bilgi: MarkaIstek, request: Request, yetkili = Depends(yetki_kontrol)):
    """Admins: add a brand/department. Names are unique within a branch."""
    if yetkili["rol"] not in ["SUPERADMIN", "ADMIN"]:
        raise HTTPException(status_code=403, detail="Bölüm/marka ekleme yetkiniz yok.")

    marka_adi = (bilgi.marka_adi or "").strip()
    if not marka_adi:
        raise HTTPException(status_code=400, detail="Bölüm adı boş olamaz.")
    if len(marka_adi) > 50:
        raise HTTPException(status_code=400, detail="Bölüm adı en fazla 50 karakter olabilir.")
    # Letters (Turkish included), digits, space and - _ . , & /
    if not re.match(r"^[a-zA-Z0-9ğüşıöçĞÜŞİÖÇ\s\-_.,&/]+$", marka_adi):
        raise HTTPException(status_code=400, detail="Bölüm adı sadece harf, rakam ve temel noktalama içerebilir. <, >, script gibi karakterler kabul edilmez.")
    
    if yetkili["rol"] != "SUPERADMIN" and bilgi.firma_id != yetkili.get("firma_id"):
        raise HTTPException(status_code=403, detail="Bu firmaya yetkiniz yok.")

    hedef_sube_id = await run_in_threadpool(hedef_sube_belirle, yetkili, bilgi.firma_id, bilgi.sube_id)
    # Case-insensitive uniqueness within the branch; NULL (HQ) is compared as its own scope.
    for m in (await run_query(supabase.table("markalar").select("marka_adi, sube_id").eq("firma_id", bilgi.firma_id))).data:
        if (m.get("sube_id") or None) == hedef_sube_id and (m.get("marka_adi") or "").strip().lower() == marka_adi.lower():
            raise HTTPException(status_code=400, detail="Bu şubede bu isimde bir bölüm/marka zaten var.")

    await run_query(supabase.table("markalar").insert({
        "firma_id": bilgi.firma_id,
        "marka_adi": marka_adi,  # no escaping needed: the pattern above allows no markup characters
        "sube_id": hedef_sube_id
    }))
    
    await audit_log_yaz(
        yapan=yetkili,
        eylem="MARKA_EKLE",
        hedef_tip="MARKA",
        hedef_id=bilgi.firma_id,
        hedef_aciklama=f"bölüm/marka eklendi: {marka_adi}",
        detay={
            "marka_adi": marka_adi,
            "firma_id": bilgi.firma_id,
            "sube_id": hedef_sube_id
        },
        request=request
    )
    
    return {"mesaj": "Bölüm eklendi."}

@app.delete("/marka-sil/{marka_id}")
async def marka_sil(marka_id: str, request: Request, yetkili = Depends(yetki_kontrol)):
    """Admins: delete a brand/department that no user or vehicle uses any more."""
    if yetkili["rol"] not in ["SUPERADMIN", "ADMIN"]:
        raise HTTPException(status_code=403, detail="Bölüm/marka silme yetkiniz yok.")

    m = (await run_query(supabase.table("markalar").select("id, firma_id, marka_adi, sube_id").eq("id", marka_id))).data
    if not m:
        raise HTTPException(status_code=404, detail="Bölüm/marka bulunamadı.")
    marka = m[0]
    if yetkili["rol"] != "SUPERADMIN" and marka.get("firma_id") != yetkili.get("firma_id"):
        raise HTTPException(status_code=403, detail="Bu firmaya yetkiniz yok.")
    sube_yazma_guard(yetkili, marka.get("sube_id"))

    marka_adi = (marka.get("marka_adi") or "").strip()
    firma_id = marka.get("firma_id")
    marka_sube = marka.get("sube_id") or None

    # Brands are referenced by name (a plain string, no foreign key) in users and vehicles, so
    # nothing stops a delete from orphaning them; check by hand. The same name may exist in
    # another branch, so match on sube_id as well.
    def _kapsamda(kayitlar):
        return [k for k in kayitlar if (k.get("sube_id") or None) == marka_sube]
    bagli_kullanici = _kapsamda((await run_query(supabase.table("kullanicilar").select("kullanici_adi, sube_id").eq("firma_id", firma_id).eq("marka", marka_adi))).data)
    bagli_arac = _kapsamda((await run_query(supabase.table("araclar").select("id, sube_id").eq("firma_id", firma_id).eq("marka", marka_adi))).data)
    if bagli_kullanici or bagli_arac:
        raise HTTPException(status_code=400, detail=f"Bu bölüme bağlı {len(bagli_kullanici)} kullanıcı ve {len(bagli_arac)} araç var. Önce onları başka bir bölüme taşıyın veya silin.")

    await run_query(supabase.table("markalar").delete().eq("id", marka_id))

    await audit_log_yaz(
        yapan=yetkili,
        eylem="MARKA_SIL",
        hedef_tip="MARKA",
        hedef_id=marka_id,
        hedef_aciklama=f"bölüm/marka silindi: {marka_adi}",
        detay={"marka_adi": marka_adi, "firma_id": firma_id, "sube_id": marka_sube},
        request=request
    )
    return {"mesaj": "Bölüm/marka silindi."}

@app.post("/firma-vale-guncelle")
async def firma_vale_guncelle(bilgi: AktiflikIstek, request: Request, yetkili = Depends(yetki_kontrol)):
    """SUPERADMIN only: switch the valet module on or off for a company.

    Switching it off is a cascade delete: valet staff, valet tasks and virtual valet vehicles
    are removed permanently. The shuttle side is untouched. Follows the same token revocation
    and delete order as firma-sil.
    """
    if yetkili["rol"] != "SUPERADMIN": raise HTTPException(status_code=403)

    firma = (await run_query(supabase.table("firmalar").select("firma_adi, vale_aktif").eq("id", bilgi.firma_id))).data
    if not firma:
        raise HTTPException(status_code=404, detail="Firma bulunamadı.")
    firma_adi_log = firma[0].get("firma_adi", "bilinmeyen")

    # Switching on only sets the flag.
    if bilgi.durum:
        await run_query(supabase.table("firmalar").update({"vale_aktif": True}).eq("id", bilgi.firma_id))
        await audit_log_yaz(
            yapan=yetkili, eylem="VALE_MODUL_AC", hedef_tip="FIRMA", hedef_id=bilgi.firma_id,
            hedef_aciklama=f"vale modülü açıldı: {firma_adi_log}",
            detay={"firma_adi": firma_adi_log}, request=request)
        await manager.broadcast_firma(bilgi.firma_id, "YENILE")
        return {"mesaj": "Vale hizmeti açıldı."}

    vale_kullanicilar = (await run_query(supabase.table("kullanicilar").select("kullanici_adi").eq("firma_id", bilgi.firma_id).eq("rol", "VALE"))).data

    # Revoke the valets' tokens first so they are logged out at once instead of keeping
    # access for the rest of the token lifetime.
    try:
        revoke_gorevleri = [
            kullanicinin_tum_tokenlarini_revoke_et(
                kullanici_adi=k["kullanici_adi"],
                sebep="VALE_MODUL_KAPATILDI",
                iptal_eden=yetkili.get("kullanici_adi"))
            for k in vale_kullanicilar if k.get("kullanici_adi")
        ]
        if revoke_gorevleri:
            await asyncio.gather(*revoke_gorevleri, return_exceptions=True)
    except Exception as e:
        sentry_sdk.capture_exception(e)  # carry on deleting even if revocation failed

    # Valet tasks, active ones included; their customer links will return 404, on purpose.
    silinen_talepler = (await run_query(supabase.table("talepler").delete().eq("firma_id", bilgi.firma_id).in_(
        "gorev_tipi", ["VALE_ALIM", "VALE_TESLIM"]))).data or []

    await run_query(supabase.table("kullanicilar").delete().eq("firma_id", bilgi.firma_id).eq("rol", "VALE"))

    # Virtual valet vehicles. arac_saat_sablonlari references araclar, so it goes first.
    vale_araclar = (await run_query(supabase.table("araclar").select("id").eq("firma_id", bilgi.firma_id).eq("tip", "VALE"))).data
    vale_arac_ids = [a["id"] for a in vale_araclar]
    if vale_arac_ids:
        await run_query(supabase.table("arac_saat_sablonlari").delete().in_("arac_id", vale_arac_ids))
        await run_query(supabase.table("araclar").delete().in_("id", vale_arac_ids))

    await run_query(supabase.table("firmalar").update({"vale_aktif": False}).eq("id", bilgi.firma_id))
    await audit_log_yaz(
        yapan=yetkili, eylem="VALE_MODUL_KAPAT", hedef_tip="FIRMA", hedef_id=bilgi.firma_id,
        hedef_aciklama=f"vale modülü kapatıldı (cascade): {firma_adi_log}",
        detay={
            "firma_adi": firma_adi_log, "cascade": True,
            "silinen_vale": len(vale_kullanicilar), "silinen_gorev": len(silinen_talepler),
            "silinen_arac": len(vale_arac_ids)
        }, request=request)
    await manager.broadcast_firma(bilgi.firma_id, "YENILE")
    await manager.broadcast_superadmin("YENILE")
    return {"mesaj": f"Vale hizmeti kapatıldı. Silinen: {len(vale_kullanicilar)} vale, {len(silinen_talepler)} görev, {len(vale_arac_ids)} sanal araç."}

@app.post("/firma-shuttle-guncelle")
async def firma_shuttle_guncelle(bilgi: ShuttleGuncelleIstek, request: Request, yetkili = Depends(yetki_kontrol)):
    """SUPERADMIN only: switch the shuttle module on or off for a company.

    Switching it off is a cascade delete: drivers, shuttle vehicles, routes, stops and
    shuttle requests are removed permanently. Valet staff, virtual vehicles and valet tasks
    are kept. The valet module must be on, since a company cannot have both off.
    """
    if yetkili["rol"] != "SUPERADMIN": raise HTTPException(status_code=403)

    firma = (await run_query(supabase.table("firmalar").select("firma_adi, shuttle_aktif, vale_aktif").eq("id", bilgi.firma_id))).data
    if not firma:
        raise HTTPException(status_code=404, detail="Firma bulunamadı.")
    firma_adi_log = firma[0].get("firma_adi", "bilinmeyen")

    # Switching on sets the flag and, if given, the operating mode (otherwise the stored mode stays).
    if bilgi.durum:
        guncelleme = {"shuttle_aktif": True}
        if bilgi.sistem_modu is not None:
            if bilgi.sistem_modu not in ("HARITA", "DURAK"):
                raise HTTPException(status_code=400, detail="Geçersiz sistem modu (HARITA veya DURAK olmalı).")
            guncelleme["sistem_modu"] = bilgi.sistem_modu
        await run_query(supabase.table("firmalar").update(guncelleme).eq("id", bilgi.firma_id))
        await audit_log_yaz(
            yapan=yetkili, eylem="SHUTTLE_MODUL_AC", hedef_tip="FIRMA", hedef_id=bilgi.firma_id,
            hedef_aciklama=f"shuttle modülü açıldı: {firma_adi_log}",
            detay={"firma_adi": firma_adi_log, "sistem_modu": guncelleme.get("sistem_modu")}, request=request)
        await manager.broadcast_firma(bilgi.firma_id, "YENILE")
        await manager.broadcast_superadmin("YENILE")
        return {"mesaj": "Shuttle hizmeti açıldı." + (f" Çalışma modu: {bilgi.sistem_modu}." if bilgi.sistem_modu else "")}

    if not firma[0].get("vale_aktif"):
        raise HTTPException(status_code=400, detail="Shuttle kapatılamaz: firmanın açık başka hizmeti yok. Önce vale hizmetini açın.")

    soforler = (await run_query(supabase.table("kullanicilar").select("kullanici_adi").eq("firma_id", bilgi.firma_id).eq("rol", "SOFOR"))).data
    try:
        gorevler = [
            kullanicinin_tum_tokenlarini_revoke_et(
                kullanici_adi=k["kullanici_adi"], sebep="SHUTTLE_MODUL_KAPATILDI",
                iptal_eden=yetkili.get("kullanici_adi"))
            for k in soforler if k.get("kullanici_adi")
        ]
        if gorevler:
            await asyncio.gather(*gorevler, return_exceptions=True)
    except Exception as e:
        sentry_sdk.capture_exception(e)

    # Shuttle vehicles only; virtual valet vehicles (tip='VALE') are kept.
    servis_araclar = [a["id"] for a in (await run_query(supabase.table("araclar").select("id, tip").eq("firma_id", bilgi.firma_id))).data
                      if (a.get("tip") or "SERVIS") != "VALE"]

    # Shuttle requests are deleted by vehicle, not by task type. The synthetic "MERKEZE DONUS"
    # rows have gorev_tipi NULL, and a task-type filter would not match NULL, so those rows
    # would survive and then block the vehicle delete through the foreign key.
    silinen_talepler = []
    if servis_araclar:
        silinen_talepler = (await run_query(supabase.table("talepler").delete().eq("firma_id", bilgi.firma_id).in_(
            "arac_id", servis_araclar))).data or []
    await run_query(supabase.table("kullanicilar").delete().eq("firma_id", bilgi.firma_id).eq("rol", "SOFOR"))

    if servis_araclar:
        await run_query(supabase.table("arac_saat_sablonlari").delete().in_("arac_id", servis_araclar))  # references araclar
        await run_query(supabase.table("araclar").delete().in_("id", servis_araclar))
    # Routes and stops only exist for shuttle.
    await run_query(supabase.table("duraklar").delete().eq("firma_id", bilgi.firma_id))
    await run_query(supabase.table("guzergahlar").delete().eq("firma_id", bilgi.firma_id))

    await run_query(supabase.table("firmalar").update({"shuttle_aktif": False}).eq("id", bilgi.firma_id))
    await audit_log_yaz(
        yapan=yetkili, eylem="SHUTTLE_MODUL_KAPAT", hedef_tip="FIRMA", hedef_id=bilgi.firma_id,
        hedef_aciklama=f"shuttle modülü kapatıldı (cascade): {firma_adi_log}",
        detay={"firma_adi": firma_adi_log, "cascade": True, "silinen_sofor": len(soforler),
               "silinen_gorev": len(silinen_talepler), "silinen_arac": len(servis_araclar)}, request=request)
    await manager.broadcast_firma(bilgi.firma_id, "YENILE")
    await manager.broadcast_superadmin("YENILE")
    return {"mesaj": f"Shuttle hizmeti kapatıldı. Silinen: {len(soforler)} şoför, {len(servis_araclar)} araç, {len(silinen_talepler)} görev kaydı."}


@app.delete("/firma-sil/{firma_id}")
async def firma_sil(firma_id: str, request: Request, yetkili = Depends(yetki_kontrol)):
    """SUPERADMIN only: delete a company and everything that belongs to it."""
    if yetkili["rol"] != "SUPERADMIN": raise HTTPException(status_code=403)

    firma_bilgi = (await run_query(supabase.table("firmalar").select("firma_adi, sistem_modu").eq("id", firma_id))).data
    if not firma_bilgi:
        raise HTTPException(status_code=404, detail="Firma bulunamadı.")
    firma_adi_log = firma_bilgi[0].get("firma_adi", "bilinmeyen")
    
    pers = await run_query(supabase.table("kullanicilar").select("kullanici_adi", count="exact").eq("firma_id", firma_id))
    personel_sayisi = pers.count or 0

    # Revoke every user's tokens before deleting, as firma-aktiflik-guncelle does.
    try:
        revoke_gorevleri = [
            kullanicinin_tum_tokenlarini_revoke_et(
                kullanici_adi=k.get("kullanici_adi"),
                sebep="FIRMA_SILINDI",
                iptal_eden=yetkili.get("kullanici_adi")
            )
            for k in (pers.data or []) if k.get("kullanici_adi")
        ]
        if revoke_gorevleri:
            await asyncio.gather(*revoke_gorevleri, return_exceptions=True)
    except Exception as e:
        sentry_sdk.capture_exception(e)  # carry on deleting even if revocation failed

    # Delete children before parents so no foreign key blocks a step. arac_saat_sablonlari
    # references araclar and must go before it; any new table referencing these must be
    # added to this list in the right place.
    for tablo in ("arac_saat_sablonlari", "duraklar", "talepler", "kullanicilar", "araclar", "guzergahlar", "markalar", "subeler"):
        await run_query(supabase.table(tablo).delete().eq("firma_id", firma_id))
    await run_query(supabase.table("firmalar").delete().eq("id", firma_id))
    await firma_cache_invalidate(firma_id)

    await audit_log_yaz(
        yapan=yetkili,
        eylem="FIRMA_SIL",
        hedef_tip="FIRMA",
        hedef_id=firma_id,
        hedef_aciklama=f"firma silindi: {firma_adi_log}",
        detay={
            "firma_adi": firma_adi_log,
            "sistem_modu": firma_bilgi[0].get("sistem_modu"),
            "cascade": True,
            "silinen_personel": personel_sayisi
        },
        request=request
    )
    await manager.broadcast_superadmin("YENILE")
    return {"mesaj": "Firma ve bağlı tüm kayıtlar silindi."}

@app.post("/firma-aktiflik-guncelle")
async def firma_aktiflik(bilgi: AktiflikIstek, request: Request, yetkili = Depends(yetki_kontrol)):
    """SUPERADMIN only: activate or deactivate a company (for example, unpaid subscription).

    Deactivating logs every user of the company out. Its ADMINs can still log in and see
    a notice; other roles are refused at login.
    """
    if yetkili["rol"] != "SUPERADMIN": raise HTTPException(status_code=403)
    await run_query(supabase.table("firmalar").update({"is_active": bilgi.durum}).eq("id", bilgi.firma_id))
    await firma_cache_invalidate(bilgi.firma_id)

    # On deactivation, revoke all the company's tokens, in parallel for large companies.
    if not bilgi.durum:
        try:
            firma_kullanicilari = (await run_query(supabase.table("kullanicilar").select("kullanici_adi").eq("firma_id", bilgi.firma_id))).data

            revoke_gorevleri = [
                kullanicinin_tum_tokenlarini_revoke_et(
                    kullanici_adi=k.get("kullanici_adi"),
                    sebep="FIRMA_PASIF",
                    iptal_eden=yetkili.get("kullanici_adi")
                )
                for k in firma_kullanicilari if k.get("kullanici_adi")
            ]
            
            await asyncio.gather(*revoke_gorevleri, return_exceptions=True)
        except Exception as e:
            # The company stays inactive even if revocation failed.
            sentry_sdk.capture_exception(e)

    yeni_durum = "AKTİF" if bilgi.durum else "PASİF"
    await audit_log_yaz(
        yapan=yetkili,
        eylem="FIRMA_PASIF_AKTIF",
        hedef_tip="FIRMA",
        hedef_id=bilgi.firma_id,
        hedef_aciklama=f"firma durumu: {yeni_durum}",
        detay={"yeni_durum": yeni_durum, "is_active": bilgi.durum},
        request=request
    )
    await manager.broadcast_superadmin("YENILE")
    return {"mesaj": "Güncellendi."}

@app.post("/firma-konum-kaydet")
def firma_konum_kaydet(firma_id: str, lat: float, lng: float, yetkili = Depends(yetki_kontrol)):
    """Admins: set the base location that routes and ETAs are measured from.

    A branch admin sets their own branch's location; HQ admins and SUPERADMIN set the
    company headquarters. Only admins may do this, since a wrong base point skews every
    route and ETA of the company.
    """
    if yetkili["rol"] not in ["ADMIN", "SUPERADMIN"]:
        raise HTTPException(status_code=403, detail="Konum belirleme yetkiniz yok.")
    if yetkili["rol"] != "SUPERADMIN" and yetkili.get("firma_id") != firma_id: raise HTTPException(status_code=403)
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
        raise HTTPException(status_code=400, detail="Geçersiz harita koordinatları.")
    # (0, 0) means the client failed to get a position; it is never a real base location.
    if lat == 0.0 and lng == 0.0:
        raise HTTPException(status_code=400, detail="Konum seçilmemiş görünüyor. Lütfen haritadan geçerli bir nokta seçin.")
    if yetkili.get("sube_id"):
        supabase.table("subeler").update({"konum_lat": lat, "konum_lng": lng}).eq("id", yetkili["sube_id"]).execute()
        return {"mesaj": "Şube konumu kaydedildi."}
    supabase.table("firmalar").update({"merkez_lat": lat, "merkez_lng": lng}).eq("id", firma_id).execute()
    return {"mesaj": "Firma merkez konumu kaydedildi."}

@app.post("/sifre-belirle")
@limiter.limit("5/minute")  # per IP, against guessing invitation tokens
def sifre_belirle(request: Request, istek: SifreBelirleIstek):
    """Invitation page (no login): set the password and display name for a new account.

    The invitation token is single use and expires after 7 days.
    """
    token = istek.token
    # Leading and trailing spaces are dropped, because the login page trims what is typed. A
    # password stored with a space at either end could never be entered again.
    yeni_sifre = (istek.yeni_sifre or "").strip()

    if not token or not yeni_sifre:
        raise HTTPException(status_code=400, detail="Eksik bilgi gönderildi.")

    # The display name is required here: people enter their own name once and the panels show
    # it. The username stays an ASCII identity and is not meant for display.
    temiz_gorunen_ad = validate_gorunen_ad(istek.gorunen_ad or "")

    # Password rules are enforced here as well as in the page.
    if len(yeni_sifre) < 8:
        raise HTTPException(status_code=400, detail="Şifre minimum 8 karakter olmalı.")
    # bcrypt rejects input over 72 bytes with a ValueError (a 500). Turkish letters take two
    # bytes in UTF-8, so the limit is checked in bytes and answered with a clear 400.
    if len(yeni_sifre.encode("utf-8")) > 72:
        raise HTTPException(status_code=400, detail="Şifre çok uzun. Lütfen daha kısa bir şifre seçin (Türkçe karakterler iki kez sayılır).")
    if not re.search(r"[A-Za-z]", yeni_sifre) or not re.search(r"\d", yeni_sifre):
        raise HTTPException(status_code=400, detail="Şifre en az 1 harf ve 1 rakam içermeli.")

    res = supabase.table("kullanicilar").select("davet_token, davet_token_olusturma, kayit_tarihi").eq("davet_token", token).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Geçersiz veya süresi dolmuş davet linki.")

    # Rows created before davet_token_olusturma existed fall back to kayit_tarihi.
    olusturma_str = res.data[0].get("davet_token_olusturma") or res.data[0].get("kayit_tarihi")
    if olusturma_str:
        try:
            olusturma_dt = supabase_tarih_parse(olusturma_str)
            if (datetime.now(timezone.utc) - olusturma_dt) > timedelta(days=7):
                raise HTTPException(status_code=410, detail="Davet linki süresi dolmuş (7 gün). Yöneticinizden yeni link isteyin.")
        except HTTPException:
            raise  # let the 410 through instead of the handler below
        except (ValueError, TypeError) as e:
            # Unparseable date: fail closed, and report it so the format can be investigated
            # ("Failure handling" in docs/SECURITY-PRIVACY.md).
            sentry_sdk.capture_exception(e)
            raise HTTPException(status_code=410, detail="Davet linki doğrulanamadı. Yöneticinizden yeni link isteyin.")
        
    hashed_pw = bcrypt.hashpw(yeni_sifre.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    
    # Update by token and clear it in the same statement, which makes the link single use.
    supabase.table("kullanicilar").update({
        "sifre": hashed_pw,
        "gorunen_ad": temiz_gorunen_ad,
        "davet_token": None,
        "davet_token_olusturma": None,
        "is_active": True  # older invitation flows created users inactive
    }).eq("davet_token", token).execute()
    
    return {"mesaj": "Şifreniz başarıyla oluşturuldu, giriş yapabilirsiniz."}

@app.put("/gorunen-ad-guncelle")
async def gorunen_ad_guncelle(istek: GorunenAdGuncelleIstek, request: Request, yetkili = Depends(yetki_kontrol)):
    """Admins: correct a person's display name (typo, new surname and so on).

    Only ADMIN and SUPERADMIN may do this; drivers and valets cannot rename themselves, so
    the names customers see stay under management control. The username is not touched,
    so no session or token is affected.
    """
    if yetkili["rol"] not in ["ADMIN", "SUPERADMIN"]:
        raise HTTPException(status_code=403, detail="Görünen adı yalnız yöneticiler düzenleyebilir.")

    yeni_ad = validate_gorunen_ad(istek.gorunen_ad or "")

    hedef_res = await run_query(supabase.table("kullanicilar").select("firma_id, rol, sube_id, gorunen_ad").eq(
        "kullanici_adi", istek.kullanici_adi))
    if not hedef_res.data:
        raise HTTPException(status_code=404, detail="Kullanıcı bulunamadı.")
    hedef = hedef_res.data[0]

    if yetkili["rol"] != "SUPERADMIN":
        # Same three checks as personel-sifre-sifirla: company, role hierarchy, branch.
        if hedef.get("firma_id") != yetkili.get("firma_id"):
            raise HTTPException(status_code=403, detail="Bu kullanıcıya yetkiniz yok.")
        # An admin may fix their own name, so the hierarchy check applies only to others.
        if istek.kullanici_adi != yetkili.get("kullanici_adi") and not yetki_hiyerarsi_kontrol(yetkili["rol"], hedef["rol"]):
            raise HTTPException(status_code=403, detail="Eşit veya yüksek yetkili kullanıcının adını değiştiremezsiniz.")
        sube_yazma_guard(yetkili, hedef.get("sube_id"))

    if not (await run_query(supabase.table("kullanicilar").update({"gorunen_ad": yeni_ad}).eq(
            "kullanici_adi", istek.kullanici_adi))).data:
        raise HTTPException(status_code=404, detail="Kullanıcı bulunamadı.")

    await audit_log_yaz(
        yapan=yetkili, eylem="GORUNEN_AD_GUNCELLE", hedef_tip="KULLANICI", hedef_id=istek.kullanici_adi,
        hedef_aciklama=f"görünen ad: {hedef.get('gorunen_ad') or '—'} → {yeni_ad}",
        detay={"onceki": hedef.get("gorunen_ad"), "yeni": yeni_ad, "hedef_rol": hedef.get("rol")},
        request=request)

    if hedef.get("firma_id"):
        await manager.broadcast_firma(hedef["firma_id"], "YENILE")
    return {"mesaj": "Görünen ad güncellendi.", "gorunen_ad": yeni_ad}


@app.put("/personel-sifre-sifirla")
async def personel_sifre_sifirla(istek: SifreSifirlaIstek, request: Request, yetkili = Depends(yetki_kontrol)):
    """Admins: set a new password for someone below them and log that person out everywhere.

    Not for one's own password.
    """
    if istek.kullanici_adi == yetkili.get("kullanici_adi"):
        raise HTTPException(status_code=400, detail="Kendi şifrenizi bu yöntemle sıfırlayamazsınız. 'Şifremi unuttum' akışını kullanın.")

    # Trimmed for the same reason as in sifre-belirle: the login page trims what is typed.
    istek.yeni_sifre = (istek.yeni_sifre or "").strip()
    if len(istek.yeni_sifre) < 8:
        raise HTTPException(status_code=400, detail="Şifre minimum 8 karakter olmalı.")
    # bcrypt's 72-byte limit, as in sifre-belirle.
    if len((istek.yeni_sifre or "").encode("utf-8")) > 72:
        raise HTTPException(status_code=400, detail="Şifre çok uzun. Lütfen daha kısa bir şifre seçin (Türkçe karakterler iki kez sayılır).")
    if not re.search(r"[A-Za-z]", istek.yeni_sifre) or not re.search(r"\d", istek.yeni_sifre):
        raise HTTPException(status_code=400, detail="Şifre en az 1 harf ve 1 rakam içermeli.")

    hedef_res = await run_query(supabase.table("kullanicilar").select("firma_id, rol, sube_id").eq("kullanici_adi", istek.kullanici_adi))
    if not hedef_res.data:
        raise HTTPException(status_code=404, detail="Kullanıcı bulunamadı.")
    hedef = hedef_res.data[0]

    if yetkili["rol"] != "SUPERADMIN":
        if hedef.get("firma_id") != yetkili.get("firma_id"):
            raise HTTPException(status_code=403, detail="Bu kullanıcıya yetkiniz yok.")

        if not yetki_hiyerarsi_kontrol(yetkili["rol"], hedef["rol"]):
            raise HTTPException(status_code=403, detail="Eşit veya yüksek yetkili kullanıcının şifresini sıfırlayamazsınız.")

        sube_yazma_guard(yetkili, hedef.get("sube_id"))

    if not (await run_query(supabase.table("kullanicilar").update({"sifre": sifreyi_hashle(istek.yeni_sifre)}).eq("kullanici_adi", istek.kullanici_adi))).data:
        raise HTTPException(status_code=404)
    
    await kullanicinin_tum_tokenlarini_revoke_et(
        kullanici_adi=istek.kullanici_adi,
        sebep="SIFRE_SIFIRLA",
        iptal_eden=yetkili.get("kullanici_adi")
    )
    
    # Who reset whose password; never the password itself.
    await audit_log_yaz(
        yapan=yetkili,
        eylem="SIFRE_SIFIRLA",
        hedef_tip="KULLANICI",
        hedef_id=istek.kullanici_adi,
        hedef_aciklama=f"şifre sıfırlandı: {istek.kullanici_adi} (rol: {hedef.get('rol')})",
        detay={"hedef_rol": hedef.get("rol")},
        request=request
    )
    
    return {"mesaj": "Güncellendi"}

@app.put("/yonetici-iletisim-guncelle")
async def yonetici_iletisim_guncelle(istek: IletisimGuncelleIstek, request: Request, yetkili = Depends(yetki_kontrol)):
    """SUPERADMIN only: edit a user's e-mail and phone from the platform admin panel."""
    hedef_res = await run_query(supabase.table("kullanicilar").select("firma_id, rol").eq("kullanici_adi", istek.kullanici_adi))
    if not hedef_res.data:
        raise HTTPException(status_code=404, detail="Kullanıcı bulunamadı.")
    hedef = hedef_res.data[0]

    # Company admins have no such action in their panel, so the endpoint is not open to them.
    if yetkili["rol"] != "SUPERADMIN":
        raise HTTPException(status_code=403, detail="Yetkisiz.")

    guncelle = {}
    # E-mail: lowercased, validated, unique among other users. Empty clears it.
    em = (istek.email or "").strip().lower()
    if em and not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", em):
        raise HTTPException(status_code=400, detail="Geçersiz e-posta formatı.")
    if em:
        dup = await run_query(supabase.table("kullanicilar").select("kullanici_adi").eq("email", em).neq("kullanici_adi", istek.kullanici_adi))
        if dup.data:
            raise HTTPException(status_code=400, detail="Bu e-posta adresi başka bir kullanıcıda kayıtlı.")
    guncelle["email"] = em or None

    # Phone: +90 followed by 10 digits. Empty clears it.
    tel = (istek.telefon or "").strip()
    if tel and not re.match(r'^\+90[0-9]{10}$', tel):
        raise HTTPException(status_code=400, detail="Geçersiz telefon formatı.")
    guncelle["telefon"] = tel or None

    if not (await run_query(supabase.table("kullanicilar").update(guncelle).eq("kullanici_adi", istek.kullanici_adi))).data:
        raise HTTPException(status_code=404, detail="Güncelleme başarısız.")

    # Log which fields are filled, not their values, to keep personal data out of the audit log.
    await audit_log_yaz(
        yapan=yetkili, eylem="ILETISIM_GUNCELLE", hedef_tip="KULLANICI",
        hedef_id=istek.kullanici_adi,
        hedef_aciklama=f"iletişim güncellendi: {istek.kullanici_adi} (rol: {hedef.get('rol')})",
        detay={"email_dolu": bool(em), "telefon_dolu": bool(tel)}, request=request
    )
    await manager.broadcast_superadmin("YENILE")
    return {"mesaj": "Güncellendi"}

@app.post("/arac-ekle")
async def arac_ekle(plaka: str, firma_id: str, marka: str = "Genel", sube_id: str = None, tip: str = "SERVIS", yetkili = Depends(yetki_kontrol)):
    """Panels: register a shuttle vehicle, within the registration quota (quota + 1 spare)."""
    if yetkili["rol"] not in ["ADMIN", "SUPERADMIN", "OPERASYON"]: raise HTTPException(status_code=403)
    if yetkili["rol"] != "SUPERADMIN" and yetkili.get("firma_id") != firma_id: raise HTTPException(status_code=403)

    if tip not in ("SERVIS", "VALE"):
        raise HTTPException(status_code=400, detail="Geçersiz araç tipi.")

    # Shuttle vehicles need the shuttle module. Virtual valet vehicles are created by
    # kullanici-ekle, not here, so this check does not affect them.
    _fs = (await run_query(supabase.table("firmalar").select("shuttle_aktif").eq("id", firma_id))).data
    if _fs and _fs[0].get("shuttle_aktif") is False:
        raise HTTPException(status_code=400, detail="Bu firmada shuttle hizmeti tanımlı değil; servis aracı eklenemez.")

    temiz_plaka = validate_plaka(plaka)

    temiz_marka = validate_metin(marka or "Genel", "Marka", max_uzunluk=40)

    hedef_sube_id = await run_in_threadpool(hedef_sube_belirle, yetkili, firma_id, sube_id)

    # Registration limit is the quota plus one: each scope may keep one spare vehicle for
    # breakdowns and accidents. (The active limit in sofor-arac-ata is the quota itself.)
    kayit_kota, kapsam_arac_ids = await run_in_threadpool(kapsam_kota_bilgisi, firma_id, hedef_sube_id)
    if len(kapsam_arac_ids) >= kayit_kota + 1:
        if hedef_sube_id:
            kota_mesaji = f"Bu şubenin araç kayıt kotası dolu ({len(kapsam_arac_ids)}/{kayit_kota}+1 yedek). Başka şubeden kota kaydırılabilir ya da kalıcı artış için Shuttle & Valet Ops ekibiyle iletişime geçin."
        else:
            kota_mesaji = f"Araç kayıt kotanız dolu ({len(kapsam_arac_ids)}/{kayit_kota}+1 yedek). Kota artışı için Shuttle & Valet Ops ekibiyle iletişime geçin."
        raise HTTPException(status_code=400, detail=kota_mesaji)

    # Plates are unique per company, not globally.
    if (await run_query(supabase.table("araclar").select("id").eq("plaka", temiz_plaka).eq("firma_id", firma_id))).data:
        raise HTTPException(status_code=400, detail="Bu plaka firmanıza zaten kayıtlı.")

    yeni_id = str(uuid.uuid4())

    await run_query(supabase.table("araclar").insert({
        "id": yeni_id,
        "plaka": temiz_plaka,
        "firma_id": firma_id,
        "marka": temiz_marka,
        "sube_id": hedef_sube_id,
        "tip": tip,
        "kayit_tarihi": datetime.now(timezone.utc).isoformat()
    }))

    await manager.broadcast_firma(firma_id, "YENILE")
    
    return {"mesaj": "Eklendi."}

@app.delete("/arac-sil/{arac_id}")
async def arac_sil(arac_id: str, request: Request, yetkili = Depends(yetki_kontrol)):
    """Panels: delete a vehicle that has no assigned driver and no requests."""
    if yetkili["rol"] not in ["ADMIN", "SUPERADMIN", "OPERASYON"]: raise HTTPException(status_code=403)
    if yetkili["rol"] != "SUPERADMIN":
        if not (k:=(await run_query(supabase.table("araclar").select("firma_id, sube_id").eq("id", arac_id)))).data or k.data[0].get("firma_id") != yetkili.get("firma_id"): raise HTTPException(status_code=403)
        sube_yazma_guard(yetkili, k.data[0].get("sube_id"))
    if (await run_query(supabase.table("kullanicilar").select("kullanici_adi").eq("arac_id", arac_id))).data: raise HTTPException(status_code=400, detail="Önce şoför bağını koparın.")

    # talepler.arac_id has a foreign key to araclar, so a vehicle with requests cannot be
    # deleted (Postgres error 23503, a 500). Answer with a clear 400 instead; the requests are
    # report history and must not be deleted silently.
    if (await run_query(supabase.table("talepler").select("id").eq("arac_id", arac_id).limit(1))).data:
        raise HTTPException(status_code=400, detail="Bu araca ait görev kayıtları var; araç silinemez. Kayıtlar 30 gün sonra otomatik temizlenir.")

    arac_bilgi = (await run_query(supabase.table("araclar").select("plaka, firma_id").eq("id", arac_id))).data
    plaka_log = arac_bilgi[0].get("plaka", "bilinmeyen") if arac_bilgi else "bilinmeyen"

    await run_query(supabase.table("araclar").delete().eq("id", arac_id))

    await audit_log_yaz(
        yapan=yetkili,
        eylem="ARAC_SIL",
        hedef_tip="ARAC",
        hedef_id=arac_id,
        hedef_aciklama=f"araç silindi: {plaka_log}",
        detay={"plaka": plaka_log},
        request=request
    )
    return {"mesaj": "Silindi."}

# ============================================================
# ROUTES AND STOPS (DURAK mode)
# ============================================================

@app.get("/firma-guzergahlari")
def firma_guzergahlari_getir(firma_id: str, yetkili = Depends(yetki_kontrol)):
    """The company's routes (guzergah) within the caller's branch scope."""
    if yetkili["rol"] != "SUPERADMIN" and yetkili.get("firma_id") != firma_id:
        raise HTTPException(status_code=403, detail="Bu firmaya yetkiniz yok.")
    guzergahlar = supabase.table("guzergahlar").select("*").eq("firma_id", firma_id).execute().data
    return kapsam_sube_filtrele(yetkili, guzergahlar)

@app.post("/guzergah-ekle")
def guzergah_ekle(bilgi: GuzergahIstek, yetkili = Depends(yetki_kontrol)):
    """Admins: create a route. Routes and stops exist only for the shuttle module."""
    if yetkili["rol"] not in ["ADMIN", "SUPERADMIN"] or (yetkili["rol"] == "ADMIN" and yetkili.get("firma_id") != bilgi.firma_id):
        raise HTTPException(status_code=403)

    # The UI already hides this for valet-only companies; the API must refuse it too.
    _fs = supabase.table("firmalar").select("shuttle_aktif").eq("id", bilgi.firma_id).execute().data
    if _fs and _fs[0].get("shuttle_aktif") is False:
        raise HTTPException(status_code=400, detail="Bu firmada shuttle hizmeti tanımlı değil; güzergah eklenemez.")

    temiz_guzergah_adi = validate_metin(bilgi.guzergah_adi, "Güzergah adı", max_uzunluk=60)

    # A route always belongs to its creator's scope: HQ (NULL) for HQ admins, the own branch
    # for branch admins. HQ cannot create routes inside a branch (that is the branch manager's
    # job), so any sube_id in the request is ignored on purpose.
    hedef_sube_id = yetkili.get("sube_id")

    supabase.table("guzergahlar").insert({
        "firma_id": bilgi.firma_id,
        "guzergah_adi": temiz_guzergah_adi,
        "sube_id": hedef_sube_id
    }).execute()
    return {"mesaj": "Güzergah başarıyla eklendi."}

@app.post("/guzergah-tipi-hesapla/{guzergah_id}")
def guzergah_tipi_hesapla(guzergah_id: str, yetkili = Depends(yetki_kontrol)):
    """Admin panel ("Done" on a route): detect the route's shape and store it in guzergahlar.tip.

    Compares the straight-line distance from base to the middle stop (stop ceil(n/2)) and
    to the last stop. If the last stop is closer than the middle one, the route bends back
    towards base: YARIM_AY ("half moon"). Otherwise it runs outward: LINE. Routes with two
    stops or fewer are LINE. The type decides the stop order in sofor-rotasi and canli-sira.
    """
    if yetkili["rol"] not in ["ADMIN", "SUPERADMIN"]:
        raise HTTPException(status_code=403)

    guz = supabase.table("guzergahlar").select("id, firma_id, sube_id").eq("id", guzergah_id).execute().data
    if not guz:
        raise HTTPException(status_code=404, detail="Güzergah bulunamadı.")
    guz_firma_id = guz[0].get("firma_id")
    if yetkili["rol"] != "SUPERADMIN" and guz_firma_id != yetkili.get("firma_id"):
        raise HTTPException(status_code=403, detail="Bu güzergah firmanıza ait değil.")

    # Base point: the route's branch, else company HQ.
    firma = supabase.table("firmalar").select("merkez_lat, merkez_lng").eq("id", guz_firma_id).execute().data
    _fm = (firma[0].get("merkez_lat"), firma[0].get("merkez_lng")) if firma else (None, None)
    m_lat, m_lng = referans_konum(guz_firma_id, guz[0].get("sube_id"), _fm)
    if m_lat is None or m_lng is None:
        raise HTTPException(status_code=400, detail="Konum tanımlı değil (şube veya firma merkezi). Önce konumu belirleyin.")

    duraklar = supabase.table("duraklar").select("sira_no, konum_lat, konum_lng").eq("guzergah_id", guzergah_id).execute().data
    duraklar = sorted(duraklar, key=lambda d: d.get("sira_no") or 999)
    n = len(duraklar)

    if n <= 2:
        tip = "LINE"  # too few stops to tell; LINE is the safe default
    else:
        orta = duraklar[math.ceil(n / 2) - 1]   # 1-based middle position, 0-based index
        son = duraklar[n - 1]
        orta_mesafe = mesafe_hesapla(m_lat, m_lng, orta["konum_lat"], orta["konum_lng"])
        son_mesafe = mesafe_hesapla(m_lat, m_lng, son["konum_lat"], son["konum_lng"])
        tip = "YARIM_AY" if son_mesafe < orta_mesafe else "LINE"  # a tie counts as LINE

    supabase.table("guzergahlar").update({"tip": tip}).eq("id", guzergah_id).execute()
    return {"tip": tip, "durak_sayisi": n}

@app.delete("/guzergah-sil/{guzergah_id}")
async def guzergah_sil(guzergah_id: str, request: Request, yetkili = Depends(yetki_kontrol)):
    """Admins: delete a route and its stops, unless a vehicle still uses it."""
    if yetkili["rol"] not in ["ADMIN", "SUPERADMIN"]:
        raise HTTPException(status_code=403)

    if yetkili["rol"] != "SUPERADMIN":
        guz_kontrol = (await run_query(supabase.table("guzergahlar").select("firma_id, guzergah_adi, sube_id").eq("id", guzergah_id))).data
        if not guz_kontrol or guz_kontrol[0].get("firma_id") != yetkili.get("firma_id"):
            raise HTTPException(status_code=403, detail="Bu güzergah firmanıza ait değil.")
        sube_yazma_guard(yetkili, guz_kontrol[0].get("sube_id"))

    bagli_araclar = (await run_query(supabase.table("araclar").select("id, plaka").eq("guzergah_id", guzergah_id))).data
    
    if bagli_araclar:
        plakalar = ", ".join([a["plaka"] for a in bagli_araclar])
        raise HTTPException(
            status_code=400,
            detail=f"Bu güzergah {len(bagli_araclar)} araca bağlı ({plakalar}). Önce bu araçları başka güzergaha atayın."
        )

    guz_bilgi = (await run_query(supabase.table("guzergahlar").select("guzergah_adi").eq("id", guzergah_id))).data
    guz_adi_log = guz_bilgi[0].get("guzergah_adi", "bilinmeyen") if guz_bilgi else "bilinmeyen"

    # Stops first, then the route itself.
    await run_query(supabase.table("duraklar").delete().eq("guzergah_id", guzergah_id))

    await run_query(supabase.table("guzergahlar").delete().eq("id", guzergah_id))

    await audit_log_yaz(
        yapan=yetkili,
        eylem="GUZERGAH_SIL",
        hedef_tip="GUZERGAH",
        hedef_id=guzergah_id,
        hedef_aciklama=f"güzergah silindi: {guz_adi_log}",
        detay={"guzergah_adi": guz_adi_log},
        request=request
    )
    return {"mesaj": "Güzergah silindi."}

@app.get("/firma-duraklari")
async def firma_duraklari_getir(
    firma_id: str,
    talep_token: str = None,
    credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer(auto_error=False))
):
    """The company's stops. Reachable two ways: staff with a JWT, or a customer whose request
    token belongs to the same company (the stop picker on the customer page).
    """
    if credentials:
        # The bearer token is optional here so the customer path can share the endpoint, which
        # is why yetki_kontrol is called directly instead of as a dependency. It applies the same
        # checks as every staff endpoint (inactive company, logout, per-user cutoff). A rejected
        # token is not an error yet: the request may still be authorised by talep_token below.
        try:
            payload = await yetki_kontrol(credentials)
        except HTTPException:
            payload = None
        if payload and (payload.get("rol") == "SUPERADMIN" or payload.get("firma_id") == firma_id):
            return (await run_query(supabase.table("duraklar").select("*").eq("firma_id", firma_id))).data

    if talep_token:
        kontrol = await run_query(supabase.table("talepler").select("firma_id, kayit_tarihi, gorev_tipi").eq("token", talep_token))
        if (kontrol.data and kontrol.data[0]["firma_id"] == firma_id
                and not musteri_linki_suresi_doldu(talep_token, kontrol.data[0])):
            return (await run_query(supabase.table("duraklar").select("*").eq("firma_id", firma_id))).data

    raise HTTPException(status_code=403, detail="Yetkisiz erişim.")

@app.post("/durak-ekle")
def durak_ekle(bilgi: DurakIstek, yetkili = Depends(yetki_kontrol)):
    """Admins: add a stop to a route at the given position (sira_no)."""
    if yetkili["rol"] not in ["ADMIN", "SUPERADMIN"] or (yetkili["rol"] == "ADMIN" and yetkili.get("firma_id") != bilgi.firma_id):
        raise HTTPException(status_code=403)

    _fs = supabase.table("firmalar").select("shuttle_aktif").eq("id", bilgi.firma_id).execute().data
    if _fs and _fs[0].get("shuttle_aktif") is False:
        raise HTTPException(status_code=400, detail="Bu firmada shuttle hizmeti tanımlı değil; durak eklenemez.")

    if yetkili.get("sube_id"):
        _gz = supabase.table("guzergahlar").select("sube_id").eq("id", bilgi.guzergah_id).execute().data
        sube_yazma_guard(yetkili, _gz[0].get("sube_id") if _gz else None)

    temiz_durak_adi = validate_metin(bilgi.durak_adi, "Durak adı", max_uzunluk=80)

    if not (-90.0 <= bilgi.konum_lat <= 90.0) or not (-180.0 <= bilgi.konum_lng <= 180.0):
        raise HTTPException(status_code=400, detail="Geçersiz durak koordinatları.")
    # (0, 0) is the model default and what clients send when no point was picked.
    if bilgi.konum_lat == 0.0 and bilgi.konum_lng == 0.0:
        raise HTTPException(status_code=400, detail="Durak konumu seçilmemiş görünüyor. Lütfen haritadan geçerli bir nokta seçin.")

    if bilgi.sira_no < 1:
        raise HTTPException(status_code=400, detail="Durak sıra numarası 1 veya üzeri olmalıdır.")

    # The insert runs in a Postgres function (atomik_durak_ekle) so that concurrent edits to
    # the same route cannot race on sira_no. The function is defined in the database; its
    # source is not part of db/.
    try:
        result = supabase.rpc("atomik_durak_ekle", {
            "p_guzergah_id": bilgi.guzergah_id,
            "p_firma_id": bilgi.firma_id,
            "p_durak_adi": temiz_durak_adi,
            "p_konum_lat": bilgi.konum_lat,
            "p_konum_lng": bilgi.konum_lng,
            "p_sira_no": bilgi.sira_no
        }).execute()
        
        if not result.data:
            raise HTTPException(status_code=500, detail="Durak ekleme başarısız.")

        return {"mesaj": "Durak eklendi.", "durak": result.data[0]}

    except HTTPException:
        raise  # pass our own HTTPException through unchanged
    except Exception as e:
        # Do not leak database or schema details to the client: generic message, details to Sentry.
        sentry_sdk.capture_exception(e)
        raise HTTPException(status_code=500, detail="Durak eklenemedi. Lütfen tekrar deneyin.")

@app.delete("/durak-sil/{durak_id}")
def durak_sil(durak_id: str, yetkili = Depends(yetki_kontrol)):
    """Admins: delete a stop and renumber the rest of the route."""
    if yetkili["rol"] not in ["ADMIN", "SUPERADMIN"]:
        raise HTTPException(status_code=403)

    if yetkili["rol"] != "SUPERADMIN":
        durak_kontrol = supabase.table("duraklar").select("firma_id, guzergah_id").eq("id", durak_id).execute().data
        if not durak_kontrol or durak_kontrol[0].get("firma_id") != yetkili.get("firma_id"):
            raise HTTPException(status_code=403, detail="Bu durak firmanıza ait değil.")
        if yetkili.get("sube_id"):
            _gz = supabase.table("guzergahlar").select("sube_id").eq("id", durak_kontrol[0].get("guzergah_id")).execute().data
            sube_yazma_guard(yetkili, _gz[0].get("sube_id") if _gz else None)
    
    # Delete and renumber the remaining stops in one Postgres function (atomik_durak_sil).
    try:
        result = supabase.rpc("atomik_durak_sil", {"p_durak_id": durak_id}).execute()
        
        if not result.data:
            raise HTTPException(status_code=404, detail="Durak bulunamadı veya silinemedi.")
        
        return {
            "mesaj": "Durak silindi, sıra numaraları güncellendi.",
            "kalan_durak_sayisi": result.data[0].get("kalan_durak_sayisi", 0)
        }
    
    except HTTPException:
        raise
    except Exception as e:
        # The function raises "Durak bulunamadı" for a missing stop: map it to 404. Anything
        # else becomes a generic 500 so database details do not leak.
        if "Durak bulunamadı" in str(e):
            raise HTTPException(status_code=404, detail="Durak bulunamadı.")
        sentry_sdk.capture_exception(e)
        raise HTTPException(status_code=500, detail="Durak silinemedi. Lütfen tekrar deneyin.")

@app.put("/arac-guzergah-ata")
def arac_guzergah_ata(istek: AracGuzergahAtaIstek, yetkili = Depends(yetki_kontrol)):
    """Panels: put a vehicle on a route, or take it off (guzergah_id empty)."""
    if yetkili["rol"] not in ["ADMIN", "SUPERADMIN", "OPERASYON"]: raise HTTPException(status_code=403)

    # Both the vehicle and the route must be in the caller's company and branch scope.
    if yetkili["rol"] != "SUPERADMIN":
        arac_kontrol = supabase.table("araclar").select("firma_id, sube_id").eq("id", istek.arac_id).execute().data
        if not arac_kontrol or arac_kontrol[0].get("firma_id") != yetkili.get("firma_id"):
            raise HTTPException(status_code=403, detail="Bu araç firmanıza ait değil.")
        sube_yazma_guard(yetkili, arac_kontrol[0].get("sube_id"))

        if istek.guzergah_id:
            guzergah_kontrol = supabase.table("guzergahlar").select("firma_id, sube_id").eq("id", istek.guzergah_id).execute().data
            if not guzergah_kontrol or guzergah_kontrol[0].get("firma_id") != yetkili.get("firma_id"):
                raise HTTPException(status_code=403, detail="Bu güzergah firmanıza ait değil.")
            sube_yazma_guard(yetkili, guzergah_kontrol[0].get("sube_id"))

    hedef_guzergah = istek.guzergah_id if istek.guzergah_id else None
    
    if not supabase.table("araclar").update({"guzergah_id": hedef_guzergah}).eq("id", istek.arac_id).execute().data: 
        raise HTTPException(status_code=404, detail="Araç bulunamadı.")
    return {"mesaj": "Araç güzergahı güncellendi."}

@app.post("/durak-islem-tamamla")
async def durak_islem_tamamla(istek: DurakTamamlaIstek, yetkili = Depends(yetki_kontrol)):
    """Driver app, DURAK mode: complete a stop for all its passengers in one call.

    Each passenger is marked picked up, no-show or dropped off, then the vehicle's previous
    stop moves here. Several updates without a transaction; replays are made safe by the
    status filter in _tek_yolcu_guncelle, but a failure halfway can leave partial state.
    """
    if yetkili["rol"] not in ["SUPERADMIN", "SOFOR"]: raise HTTPException(status_code=403)
    if not (await run_in_threadpool(sofor_kendi_araci_mi, yetkili, istek.arac_id)):
        raise HTTPException(status_code=403, detail="Sadece atandığınız araçta işlem yapabilirsiniz.")
    # Cap the list: more than 100 passengers at one stop is not realistic, and an unbounded
    # list would start one thread and one update per entry.
    if len(istek.yolcular) > 100:
        raise HTTPException(status_code=400, detail="Tek durakta en fazla 100 yolcu işlenebilir.")

    # One distance from the previous stop to this one, shared by everyone boarding here.
    arac_data = (await run_query(supabase.table("araclar").select("son_durak_lat, son_durak_lng, firma_id, rota_aktif").eq("id", istek.arac_id))).data
    if not arac_data:
        raise HTTPException(status_code=404, detail="Araç bulunamadı.")
    arac = arac_data[0]
    if not arac.get("rota_aktif"):
        raise HTTPException(status_code=409, detail="Rota başlatılmadan durak işlemi yapılamaz.")

    yapilan_km = 0
    if arac.get("son_durak_lat"):
        yol_verisi = await yol_mesafesi_verisi_async(arac["son_durak_lat"], arac["son_durak_lng"], istek.durak_lat, istek.durak_lng)
        yapilan_km = yol_verisi["km"]

    # The Supabase client is synchronous, so each update runs in a worker thread and the
    # results are gathered; a busy stop does not block the event loop.
    def _tek_yolcu_guncelle(token: str, yeni_durum: str, km: float):
        # .neq(durum): skip passengers already in the target status. On a replay the previous
        # stop has already moved here, so the distance would come out near 0 and overwrite the
        # real mesafe_km.
        # .eq(arac_id): only this vehicle's requests match, so a token belonging to another
        # vehicle updates nothing.
        return supabase.table("talepler").update({
            "durum": yeni_durum,
            "mesafe_km": km,
            # Same completion time as the single-passenger endpoints write.
            "tamamlanma_tarihi": datetime.now(timezone.utc).isoformat()
        }).eq("token", token).eq("arac_id", istek.arac_id).neq("durum", yeni_durum).execute()

    guncelleme_gorevleri = []
    for y in istek.yolcular:
        yeni_durum = "YOLCU ALINDI"
        if y.islem == "gelmedi": yeni_durum = "YOLCU GELMEDİ"
        elif y.islem == "indi": yeni_durum = "YOLCU INDI"

        km = yapilan_km if y.islem != "indi" else 0  # the trip distance was recorded at pickup
        guncelleme_gorevleri.append(run_in_threadpool(_tek_yolcu_guncelle, y.token, yeni_durum, km))

    if guncelleme_gorevleri:
        sonuclar = await asyncio.gather(*guncelleme_gorevleri, return_exceptions=True)
        hatalar = [r for r in sonuclar if isinstance(r, Exception)]
        if hatalar:
            # Fail the whole call so the driver app queues it and sends it again. The replay is
            # safe: passengers already updated are skipped by the status filter. This check has
            # to come before the vehicle's previous stop moves here below; otherwise the replay
            # would measure a near-zero distance for the passengers that failed.
            sentry_sdk.capture_exception(hatalar[0])
            raise HTTPException(status_code=500, detail="Bazı yolcular güncellenemedi. İşlem tekrar denenecek.")

    # This stop becomes the vehicle's previous stop and its shown position.
    await run_query(supabase.table("araclar").update({
        "son_durak_lat": istek.durak_lat, 
        "son_durak_lng": istek.durak_lng,
        "son_lat": istek.durak_lat,   
        "son_lng": istek.durak_lng,
        "son_hareket_zamani": datetime.now(timezone.utc).isoformat()
    }).eq("id", istek.arac_id))
    
    await manager.broadcast_firma(arac["firma_id"], "YENILE")
    await manager.broadcast_arac(istek.arac_id, "YENILE")
    
    return {"mesaj": f"{len(istek.yolcular)} yolcu işlendi. Sonraki durağa geçiliyor."}

@app.post("/admin/arac-saat-guncelle")
async def arac_saat_guncelle(istek: SaatGuncelleIstek, yetkili = Depends(yetki_kontrol)):
    """Panels: set or clear a vehicle's departure time (hareket_saati, "HH:MM").

    Customers see this time on the tracking page. Setting the same value again is a no-op,
    so repeated clicks do not trigger refresh broadcasts.
    """
    izin_verilen_roller = ["SUPERADMIN", "ADMIN", "DANISMAN", "OPERASYON"]
    
    if yetkili.get("rol") not in izin_verilen_roller: 
        raise HTTPException(status_code=403)
        
    arac_id = istek.arac_id
    saat = istek.hareket_saati or ""

    if not arac_id:
        raise HTTPException(status_code=400, detail="arac_id gerekli.")

    # One query serves both the company check and the no-op check.
    arac_kontrol = (await run_query(supabase.table("araclar").select("firma_id, hareket_saati, sube_id").eq("id", arac_id))).data
    if not arac_kontrol:
        raise HTTPException(status_code=404, detail="Araç bulunamadı.")
    mevcut_arac = arac_kontrol[0]

    if yetkili["rol"] != "SUPERADMIN" and mevcut_arac.get("firma_id") != yetkili.get("firma_id"):
        raise HTTPException(status_code=403, detail="Bu araç firmanıza ait değil.")
    sube_yazma_guard(yetkili, mevcut_arac.get("sube_id"))

    # Empty (clear) or HH:MM.
    if saat and not re.match(r'^([01]\d|2[0-3]):[0-5]\d$', saat):
        raise HTTPException(status_code=400, detail="Geçersiz saat formatı (HH:MM beklenir).")

    # The database may return "HH:MM:SS" while the client sends "HH:MM", so compare the
    # first five characters.
    yeni_saat = saat or None
    mevcut_saat_raw = mevcut_arac.get("hareket_saati")
    mevcut_saat = mevcut_saat_raw[:5] if mevcut_saat_raw else None  # "08:00:00" -> "08:00"
    
    if yeni_saat == mevcut_saat:
        return {"status": "noop", "mesaj": "Saat zaten aynı, güncelleme yapılmadı."}

    await run_query(supabase.table("araclar").update({"hareket_saati": yeni_saat}).eq("id", arac_id))
    
    await manager.broadcast_firma(mevcut_arac.get("firma_id"), "YENILE")
    
    return {"status": "ok", "mesaj": "Saat güncellendi."}

# Departure time templates: saved "HH:MM" values per vehicle that the panels offer as quick picks.
@app.get("/admin/arac-saat-sablonlari/{arac_id}")
def get_sablonlar(arac_id: str, yetkili = Depends(yetki_kontrol)):
    """Templates of one vehicle."""
    if yetkili["rol"] != "SUPERADMIN":
        _a = supabase.table("araclar").select("firma_id, sube_id").eq("id", arac_id).execute().data
        if not _a or _a[0].get("firma_id") != yetkili.get("firma_id"):
            raise HTTPException(status_code=403, detail="Bu araç firmanıza ait değil.")
        sube_yazma_guard(yetkili, _a[0].get("sube_id"))
    return supabase.table("arac_saat_sablonlari").select("*").eq("arac_id", arac_id).execute().data

@app.get("/admin/tum-saat-sablonlari")
def get_tum_sablonlar(yetkili = Depends(yetki_kontrol)):
    """All templates of the caller's company.

    Office roles only. SUPERADMIN is excluded because its token has no company.
    """
    if yetkili["rol"] not in ["ADMIN", "OPERASYON", "DANISMAN"]:
        raise HTTPException(status_code=403, detail="Yetkisiz.")
    return supabase.table("arac_saat_sablonlari").select("*").eq("firma_id", yetkili["firma_id"]).execute().data

@app.post("/admin/arac-saat-sablonlari")
def add_sablon(istek: SablonIstek, yetkili = Depends(yetki_kontrol)):
    """Add a template to a vehicle of the caller's company."""
    if istek.saat and not re.match(r'^([01]\d|2[0-3]):[0-5]\d$', istek.saat):
        raise HTTPException(status_code=400, detail="Geçersiz saat formatı (HH:MM beklenir).")

    arac_kontrol = supabase.table("araclar").select("id, sube_id").eq("id", istek.arac_id).eq("firma_id", yetkili["firma_id"]).execute()
    if not arac_kontrol.data:
        raise HTTPException(status_code=403, detail="Bu araç size ait değil veya bulunamadı!")
    sube_yazma_guard(yetkili, arac_kontrol.data[0].get("sube_id"))

    # firma_id always comes from the token, never from the client.
    return supabase.table("arac_saat_sablonlari").insert({
        "arac_id": istek.arac_id,
        "saat": istek.saat,
        "firma_id": yetkili["firma_id"]
    }).execute()

@app.delete("/admin/arac-saat-sablonlari/{sablon_id}")
def delete_sablon(sablon_id: int, yetkili = Depends(yetki_kontrol)):
    """Delete a template. The firma_id filter limits this to the caller's own company."""
    return supabase.table("arac_saat_sablonlari").delete().eq("id", sablon_id).eq("firma_id", yetkili["firma_id"]).execute()

@app.post("/sofor-baglantisini-kes")
async def sofor_baglantisi_kes(kullanici_adi: str, yetkili = Depends(yetki_kontrol)):
    """Panels: unassign a driver from their vehicle."""
    if yetkili["rol"] not in ["ADMIN", "SUPERADMIN", "OPERASYON"]: raise HTTPException(status_code=403)
    # The old vehicle id is needed for the broadcast after unassigning.
    k = (await run_query(supabase.table("kullanicilar").select("firma_id, sube_id, arac_id").eq("kullanici_adi", kullanici_adi))).data
    if not k:
        raise HTTPException(status_code=404, detail="Kullanıcı bulunamadı.")
    sofor_firma_id = k[0].get("firma_id")
    eski_arac_id = k[0].get("arac_id")
    if yetkili["rol"] != "SUPERADMIN":
        if sofor_firma_id != yetkili.get("firma_id"): raise HTTPException(status_code=403)
        sube_yazma_guard(yetkili, k[0].get("sube_id"))
    await run_query(supabase.table("kullanicilar").update({"arac_id": None}).eq("kullanici_adi", kullanici_adi))
    # Broadcast, like sofor-arac-ata, so the driver app switches to "no vehicle" right away.
    if sofor_firma_id:
        await manager.broadcast_firma(sofor_firma_id, "YENILE")
    if eski_arac_id:
        await manager.broadcast_arac(str(eski_arac_id), "YENILE")
    return {"mesaj": "Kesildi."}

# The service worker script must always be fetched fresh, iOS included. If a browser served
# sw.js from its cache, it would never notice a new version and the PWA would stay on old
# code. This route is declared before the StaticFiles mount so it takes precedence.
@app.get("/sw.js")
def service_worker_dosyasi():
    return FileResponse(
        "frontend/sw.js",
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )

# ============================================================
# VALET ENDPOINTS (picking up and returning a customer's car)
# Like every route, these must be declared before app.mount("/") at the end of the file:
# the catch-all mount swallows later routes (POST gives 405, GET gives 404).
# ============================================================

class ValeKonumIstek(BaseModel):
    token: str
    lat: float
    lng: float
    # Optional for older clients, as in KonumOnay. False when consent is required means 400.
    riza_onay: bool = False

@app.post("/vale-konum-onay")
@limiter.limit("20/minute")  # limits token guessing, as on talep-detay
async def vale_konum_onay(bilgi: ValeKonumIstek, request: Request):
    """Customer page (no login): the valet customer marks where the car is to be picked up
    or delivered. Records consent first when required, like /konum-dogrula.
    """
    if not (-90.0 <= bilgi.lat <= 90.0) or not (-180.0 <= bilgi.lng <= 180.0):
        raise HTTPException(status_code=400, detail="Geçersiz konum koordinatları.")
    # (0, 0) means the client failed to get a position.
    if bilgi.lat == 0.0 and bilgi.lng == 0.0:
        raise HTTPException(status_code=400, detail="Konum seçilmemiş görünüyor. Lütfen haritadan geçerli bir nokta seçin.")

    res = await run_query(supabase.table("talepler").select("id, firma_id, arac_id, durum, gorev_tipi, kayit_tarihi").eq("token", bilgi.token))
    if not res.data:
        raise HTTPException(status_code=404, detail="Geçersiz link.")

    talep = res.data[0]
    if musteri_linki_suresi_doldu(bilgi.token, talep):
        raise HTTPException(status_code=410, detail="Bu link süresi dolmuş.")
    if talep.get("durum") != "BEKLIYOR_KONUM":
        raise HTTPException(status_code=400, detail="Konum zaten işaretlenmiş veya görev ilerlemiş.")

    if not talep.get("gorev_tipi", "").startswith("VALE_"):
        raise HTTPException(status_code=400, detail="Bu görev bir vale işlemi değil.")

    # Consent check, identical to /konum-dogrula; keep the two in sync. Valet customers see
    # the same privacy notice and consent text as shuttle passengers.
    if (await run_in_threadpool(kvkk_riza_gerekli_mi, talep.get("firma_id"))):
        if not bilgi.riza_onay:
            raise HTTPException(
                status_code=400,
                detail="Konumunuzu işleyebilmemiz için aydınlatma metnini okuyup açık rıza vermeniz gerekir."
            )
        if not await riza_kaydi_yaz(
            kanal="VALE_KONUM", islem="ONAY", metin_kodu=KVKK_METIN_YOLCU_RIZA,
            firma_id=talep.get("firma_id"), talep_id=talep["id"], request=request
        ):
            raise HTTPException(
                status_code=503,
                detail="Onayınız kaydedilemedi, konumunuz işlenmedi. Lütfen birazdan tekrar deneyin."
            )

    # The .eq("durum") filter means that two confirmations from two tabs are applied once.
    # Note the column names are konum_lat/konum_lng; talepler has no lat/lng columns.
    await run_query(supabase.table("talepler").update({
        "konum_lat": bilgi.lat,
        "konum_lng": bilgi.lng,
        "durum": "KONUM_ALINDI_VALE",
        "riza_alindi": True,
        "riza_reddedildi": False   # clears the badge if the customer refused earlier and came back
    }).eq("id", talep["id"]).eq("durum", "BEKLIYOR_KONUM"))

    await manager.broadcast_firma(talep["firma_id"], "YENILE")
    if talep.get("arac_id"):
        await manager.broadcast_arac(talep["arac_id"], "YENILE")

    return {"mesaj": "Konum başarıyla kaydedildi, vale yönlendiriliyor."}


# ============================================================
# KVKK: refusing and withdrawing consent (customer page, no login, authorised by token)
# ============================================================

class RizaIstek(BaseModel):
    token: str


@app.post("/riza-red")
@limiter.limit("15/minute")
async def riza_red(bilgi: RizaIstek, request: Request):
    """The customer chose to continue without giving consent.

    A refusal neither cancels the request nor is it final:
      - talepler.durum is not touched, so the state machine, valet transitions, canli-sira
        and reports are unaffected;
      - the link stays alive, so a customer who refused by mistake can come back and accept
        (a new consent row is then written; the latest row is the current state);
      - the panel shows a badge and staff continue by phone.
    Refusing has to be possible and harmless, otherwise consent given here would not be free.
    """
    res = (await run_query(supabase.table("talepler").select("id, firma_id, gorev_tipi, durum").eq("token", bilgi.token))).data
    if not res:
        raise HTTPException(status_code=404, detail="Geçersiz link.")
    talep = res[0]

    kanal = "VALE_KONUM" if str(talep.get("gorev_tipi") or "").startswith("VALE_") else "YOLCU_KONUM"
    await riza_kaydi_yaz(
        kanal=kanal, islem="RED", metin_kodu=KVKK_METIN_YOLCU_RIZA,
        firma_id=talep.get("firma_id"), talep_id=talep["id"], request=request
    )
    # Not fail-closed: if the refusal cannot be logged there is still no reason to block the
    # customer, since no personal data is being processed (no location was taken). See
    # "Failure handling" in docs/SECURITY-PRIVACY.md.

    # Quick flag for the panel badge, to tell "refused" apart from "never opened the link"
    # (see db/kvkk_riza.sql). Only applied while no location has been given yet; for a
    # request already in progress the call is silently ignored so no wrong badge appears.
    await run_query(supabase.table("talepler").update({"riza_reddedildi": True}).eq(
        "id", talep["id"]).in_("durum", ["BEKLİYOR", "BEKLIYOR_KONUM"]))

    await manager.broadcast_firma(talep.get("firma_id"), "YENILE")
    return {"mesaj": "Onay vermediniz. Konum bilginiz alınmadı; firma sizinle iletişime geçecektir."}


@app.post("/riza-geri-cek")
@limiter.limit("15/minute")
async def riza_geri_cek(bilgi: RizaIstek, request: Request):
    """The customer withdraws consent, which KVKK art. 7 allows at any time (not retroactively).

    Unlike a refusal, data has already been processed here: the location is erased and the
    link is destroyed (the same "BTT-" path as valet delivery and cancellation).
    The task is not cancelled automatically. If the valet is on the way or already has the
    car, closing the record in software would drop the car from the system and break the
    chain of custody (the reasoning behind VALE_IPTAL_EDILEBILIR_DURUMLAR). The location is
    removed, the ETA stops, the panel shows a warning, and a person closes the task.
    """
    res = (await run_query(supabase.table("talepler").select(
        "id, firma_id, arac_id, gorev_tipi, durum, konum_lat").eq("token", bilgi.token))).data
    if not res:
        raise HTTPException(status_code=404, detail="Geçersiz link.")
    talep = res[0]

    kanal = "VALE_KONUM" if str(talep.get("gorev_tipi") or "").startswith("VALE_") else "YOLCU_KONUM"

    # Log first, so the withdrawal record exists before the data is erased. The result is
    # ignored on purpose (not fail-closed): stopping the erasure because the log write failed
    # would make the customer's legal right depend on a logging error. Failures go to Sentry.
    # See "Failure handling" in docs/SECURITY-PRIVACY.md.
    await riza_kaydi_yaz(
        kanal=kanal, islem="GERI_CEKME", metin_kodu=KVKK_METIN_YOLCU_RIZA,
        firma_id=talep.get("firma_id"), talep_id=talep["id"], request=request
    )

    # Shuttle requests go back to BEKLİYOR; valet tasks keep their status. The difference is
    # deliberate:
    #   - Shuttle: without a location the passenger cannot be routed. Left in KONUM ALINDI or
    #     SERVIS_HAZIR, the row would stay in the lists sofor-rotasi and canli-sira read, with
    #     no coordinates. BEKLİYOR means "waiting for a location", which shows correctly on the
    #     panel and takes the passenger off the active route.
    #   - Valet: the car may already be with the valet, and rolling the status back would drop
    #     it from the system. The valet side copes with a missing location: vale.js hides the
    #     navigation button and talep-detay skips the ETA.
    vale_gorevi = str(talep.get("gorev_tipi") or "").startswith("VALE_")
    guncelleme = {
        "konum_lat": None,
        "konum_lng": None,
        "riza_alindi": False,
        # Refusal and withdrawal share the same badge on purpose: staff do the same thing in
        # both cases (call the customer). The legal distinction is kept in the consent log.
        "riza_reddedildi": True,
        "token": f"BTT-{secrets.token_hex(8)}"
    }
    if not vale_gorevi:
        # The chosen stop also reveals where the person lives or works, so it is erased too.
        guncelleme["secilen_durak_id"] = None
        if talep.get("durum") in ("KONUM ALINDI", "SERVIS_HAZIR"):
            guncelleme["durum"] = "BEKLİYOR"

    await run_query(supabase.table("talepler").update(guncelleme).eq("id", talep["id"]))

    await manager.broadcast_firma(talep.get("firma_id"), "YENILE")
    if talep.get("arac_id"):
        await manager.broadcast_arac(talep["arac_id"], "YENILE")

    return {"mesaj": "Onayınız geri alındı ve konum bilginiz silindi. Firma sizinle iletişime geçecektir."}

class ValeDurumGuncelleIstek(BaseModel):
    arac_id: str
    talep_id: str
    yeni_durum: str
    # The valet app sends its position with every action; there is no background tracking.
    lat: Optional[float] = None
    lng: Optional[float] = None
    # Age and accuracy of that position. The app sends a cached reading rather than a fresh
    # one, and without these values a "pressed remotely" verdict could be unfair (see
    # db/gorev_etaplari.sql).
    konum_yasi_sn: Optional[int] = None
    konum_dogruluk_m: Optional[int] = None

# Valet task state machine.
VALE_GECERLI_DURUMLAR = {"VALE_YOLDA", "ARAC_ALINDI", "TAMAM_SERVIS", "TAMAM_MUSTERI"}

# The order differs by task type:
#   pickup:   the valet first drives to the customer (VALE_YOLDA), takes the car, then reaches
#             the service center.
#   delivery: the valet first takes the car out of the service center, then drives to the
#             customer (VALE_YOLDA), then hands it over.
# In both types VALE_YOLDA means "the valet is on the way to the customer", which is when the
# customer's ETA starts. A single shared order would start the delivery ETA before the car
# had even left the service center.
# The buttons in vale.js must follow this table exactly. VALE_IPTAL_EDILEBILIR_DURUMLAR has
# the same asymmetry; review both together.
VALE_GECISLER = {
    "VALE_ALIM": {
        "KONUM_ALINDI_VALE": {"VALE_YOLDA"},      # driving to the customer (ETA starts)
        "VALE_YOLDA": {"ARAC_ALINDI"},            # took the car from the customer (location erased)
        "ARAC_ALINDI": {"TAMAM_SERVIS"},          # reached the service center
    },
    "VALE_TESLIM": {
        "KONUM_ALINDI_VALE": {"ARAC_ALINDI"},     # took the car out of the service center
        "ARAC_ALINDI": {"VALE_YOLDA"},            # driving to the customer (ETA starts)
        "VALE_YOLDA": {"TAMAM_MUSTERI"},          # handed the car over (location erased)
    },
}

@app.post("/vale-durum-guncelle")
async def vale_durum_guncelle(bilgi: ValeDurumGuncelleIstek, yetkili = Depends(yetki_kontrol)):
    """Valet app: move a task to its next status.

    Besides the status it handles erasing the customer's location, the completion time,
    the distance for reports, the valet vehicle's status and position, and the punctuality
    milestones.
    """
    if yetkili["rol"] not in ["VALE", "ADMIN", "SUPERADMIN"]:
        raise HTTPException(status_code=403, detail="Sadece valeler durum güncelleyebilir.")

    if bilgi.yeni_durum not in VALE_GECERLI_DURUMLAR:
        raise HTTPException(status_code=400, detail="Geçersiz durum.")

    if yetkili["rol"] == "VALE" and yetkili.get("arac_id") != bilgi.arac_id:
        raise HTTPException(status_code=403, detail="Sadece kendi atandığınız görevi güncelleyebilirsiniz.")

    # marka is read for the milestone log (per-brand reports).
    res = await run_query(supabase.table("talepler").select("id, firma_id, sube_id, marka, durum, gorev_tipi, konum_lat, konum_lng").eq("id", bilgi.talep_id).eq("arac_id", bilgi.arac_id))
    if not res.data:
        raise HTTPException(status_code=404, detail="Görev bulunamadı.")

    talep = res.data[0]
    firma_id = talep["firma_id"]

    if yetkili["rol"] != "SUPERADMIN" and firma_id != yetkili.get("firma_id"):
        raise HTTPException(status_code=403, detail="Yetkisiz işlem.")

    # Idempotency: a replay from the app's offline queue resending the same status is not an error.
    if talep.get("durum") == bilgi.yeni_durum:
        return {"mesaj": f"Durum zaten {bilgi.yeni_durum}."}

    # Only the next step for this task type is allowed: no skipping, no reopening.
    gecisler = VALE_GECISLER.get(talep.get("gorev_tipi"), {})
    if bilgi.yeni_durum not in gecisler.get(talep.get("durum"), set()):
        raise HTTPException(status_code=409, detail=f"Geçersiz durum geçişi ({talep.get('durum')} → {bilgi.yeni_durum}).")

    # A pickup ends at the service center, a delivery at the customer.
    if bilgi.yeni_durum == "TAMAM_SERVIS" and talep.get("gorev_tipi") != "VALE_ALIM":
        raise HTTPException(status_code=400, detail="Bu görev tipi servise teslimle bitemez.")
    if bilgi.yeni_durum == "TAMAM_MUSTERI" and talep.get("gorev_tipi") != "VALE_TESLIM":
        raise HTTPException(status_code=400, detail="Bu görev tipi müşteriye teslimle bitemez.")

    # Data minimisation: the customer's location is erased at the first moment it is no
    # longer needed.
    #   pickup:   as soon as the car is taken (ARAC_ALINDI); the valet then drives to the
    #             service center and needs no address.
    #   delivery: only at hand-over (TAMAM_MUSTERI); the address is needed until then, and
    #             erasing it earlier would break the delivery navigation.
    # Name, phone and plate stay: the delivery link and reports need them. The 30-day
    # pg_cron cleanup removes them later.
    update_data = {"durum": bilgi.yeni_durum}

    # Completion time (pickup: reached the service center; delivery: handed over). Stored in
    # UTC; the frontend converts to Istanbul time in saatTR().
    if bilgi.yeni_durum in ("TAMAM_SERVIS", "TAMAM_MUSTERI"):
        update_data["tamamlanma_tarihi"] = datetime.now(timezone.utc).isoformat()

    konum_silinsin = (
        (talep.get("gorev_tipi") == "VALE_ALIM" and bilgi.yeni_durum in ("ARAC_ALINDI", "TAMAM_SERVIS"))
        or (talep.get("gorev_tipi") == "VALE_TESLIM" and bilgi.yeni_durum == "TAMAM_MUSTERI")
    )
    if konum_silinsin:
        update_data["konum_lat"] = None
        update_data["konum_lng"] = None

    # The link is not destroyed here.
    #   pickup:   the customer should still see "your car is at the service center"; the link
    #             dies when the delivery task is created (in talep_olustur).
    #   delivery: the link lives ANKET_PENCERESI_SAAT longer for the survey. Destruction is
    #             only postponed and happens in one of three ways: when the survey is sent
    #             (anket-gonder), on the first talep-detay call after the window closes, or
    #             with the row in the 30-day KVKK cleanup.
    # During the window talep-detay switches to survey mode and returns no name, phone, plate
    # or location. That is required; without it the link would keep exposing personal data
    # after delivery.

    # Distance for reports: how far the customer's car was driven by the valet (pickup: door
    # to service center; delivery: service center to door). Road distance via Mapbox, using
    # the same cached helper as shuttle.
    km_yazilacak = (
        (talep.get("gorev_tipi") == "VALE_ALIM" and bilgi.yeni_durum == "ARAC_ALINDI")
        or (talep.get("gorev_tipi") == "VALE_TESLIM" and bilgi.yeni_durum == "TAMAM_MUSTERI")
    )
    if km_yazilacak and talep.get("konum_lat") is not None:
        try:
            f_row = (await run_query(supabase.table("firmalar").select("merkez_lat, merkez_lng").eq("id", firma_id))).data
            arac_row = (await run_query(supabase.table("araclar").select("sube_id").eq("id", bilgi.arac_id))).data
            if f_row:
                m_lat, m_lng = await run_in_threadpool(referans_konum, firma_id, arac_row[0].get("sube_id") if arac_row else None, (f_row[0].get("merkez_lat"), f_row[0].get("merkez_lng")))
                if m_lat is not None:
                    yol = await yol_mesafesi_verisi_async(talep["konum_lat"], talep["konum_lng"], m_lat, m_lng)
                    update_data["mesafe_km"] = yol.get("km", 0)
        except Exception as e:
            sentry_sdk.capture_exception(e)  # a report detail; must never block the task

    # The .eq("durum") filter is a race guard: two concurrent requests cannot both apply the
    # same transition. The losing request still reaches this point and still gets a success
    # response, but durum_gercekten_degisti is False for it, and the milestone code below
    # checks that so the loser does not write a phantom milestone row.
    durum_guncellemesi = await run_query(supabase.table("talepler").update(update_data).eq("id", bilgi.talep_id).eq("durum", talep.get("durum")))
    durum_gercekten_degisti = bool(durum_guncellemesi.data)

    # Update the valet's virtual vehicle the way driver actions update a shuttle vehicle:
    # status and last known position. The status badge on the panels and the customer's ETA
    # read from here.
    vale_konum_gecerli = (
        bilgi.lat is not None and bilgi.lng is not None
        and -90.0 <= bilgi.lat <= 90.0 and -180.0 <= bilgi.lng <= 180.0
        and not (bilgi.lat == 0.0 and bilgi.lng == 0.0)
    )
    arac_update = {}
    if bilgi.yeni_durum == "VALE_YOLDA":
        arac_update["durum"] = "GÖREVDE"  # like rota-baslat
        if vale_konum_gecerli:
            arac_update["son_lat"] = bilgi.lat
            arac_update["son_lng"] = bilgi.lng
    elif bilgi.yeni_durum == "ARAC_ALINDI":
        arac_update["durum"] = "GÖREVDE"  # also repairs the badge if VALE_YOLDA was skipped
        # Never invent a position. Only the valet's own valid GPS reading is written; without
        # one the vehicle keeps its last real position and its old timestamp. Using the
        # customer's address as a stand-in would be wrong in three ways:
        #   1) the valet may press the button anywhere, and the admin map would show a made-up
        #      point with a fresh timestamp as if it were verified;
        #   2) in a delivery, ARAC_ALINDI means "took the car out of the service center", so the
        #      valet is at the service center. The next VALE_YOLDA step would compute an ETA from
        #      the customer's address to itself (0 minutes), and since 0 is falsy no promised
        #      time would be recorded and that task's punctuality would be lost;
        #   3) after a consent withdrawal the customer's location is NULL anyway.
        # A stale position on the map is better than a fresh one in the wrong place.
        if vale_konum_gecerli:
            arac_update["son_lat"] = bilgi.lat
            arac_update["son_lng"] = bilgi.lng
    elif bilgi.yeni_durum in ("TAMAM_SERVIS", "TAMAM_MUSTERI"):
        arac_update["durum"] = "MERKEZDE"  # like rota-bitir
        if vale_konum_gecerli:
            arac_update["son_lat"] = bilgi.lat
            arac_update["son_lng"] = bilgi.lng
    # Every branch above that writes a position also needs the timestamp; set it once here.
    if "son_lat" in arac_update:
        arac_update["son_hareket_zamani"] = datetime.now(timezone.utc).isoformat()

    if arac_update:
        await run_query(supabase.table("araclar").update(arac_update).eq("id", bilgi.arac_id))

    # ------------------------------------------------------------
    # Punctuality milestones (db/gorev_etaplari.sql)
    # ------------------------------------------------------------
    # Written only if this request really changed the status (see the race guard above).
    # `talep` was read at the start of the request, so konum_lat is still available here even
    # though the update above may have erased it in the database. The order matters.
    if durum_gercekten_degisti:
        talep["arac_id"] = bilgi.arac_id
        talep["_vale_kullanici_adi"] = yetkili.get("kullanici_adi")
        v_lat = bilgi.lat if vale_konum_gecerli else None
        v_lng = bilgi.lng if vale_konum_gecerli else None
        m_lat = talep.get("konum_lat")   # customer's door; used for the estimate, never stored
        m_lng = talep.get("konum_lng")
        alim = talep.get("gorev_tipi") == "VALE_ALIM"

        if alim and bilgi.yeni_durum == "VALE_YOLDA":
            await etap_ac(talep, ETAP_MUSTERIYE_GIDIS, v_lat, v_lng, m_lat, m_lng)
        elif alim and bilgi.yeni_durum == "ARAC_ALINDI":
            await etap_kapat(bilgi.talep_id, ETAP_MUSTERIYE_GIDIS, v_lat, v_lng, m_lat, m_lng,
                             bilgi.konum_yasi_sn, bilgi.konum_dogruluk_m)
            s_lat, s_lng = await run_in_threadpool(vale_merkez_koordinati, firma_id, bilgi.arac_id)
            # The return-to-service promise starts from the valet's own position when there is
            # one, and from the customer's door only as a fallback. The door can be NULL after a
            # consent withdrawal, which would leave the leg without a promise, and the valet may
            # press the button somewhere other than the door. With neither, no promise is made.
            b_lat = v_lat if v_lat is not None else m_lat
            b_lng = v_lng if v_lng is not None else m_lng
            await etap_ac(talep, ETAP_SERVISE_DONUS, b_lat, b_lng, s_lat, s_lng)
        elif alim and bilgi.yeni_durum == "TAMAM_SERVIS":
            s_lat, s_lng = await run_in_threadpool(vale_merkez_koordinati, firma_id, bilgi.arac_id)
            await etap_kapat(bilgi.talep_id, ETAP_SERVISE_DONUS, v_lat, v_lng, s_lat, s_lng,
                             bilgi.konum_yasi_sn, bilgi.konum_dogruluk_m)
        elif (not alim) and bilgi.yeni_durum == "VALE_YOLDA":
            await etap_ac(talep, ETAP_MUSTERIYE_TESLIM, v_lat, v_lng, m_lat, m_lng)
        elif (not alim) and bilgi.yeni_durum == "TAMAM_MUSTERI":
            await etap_kapat(bilgi.talep_id, ETAP_MUSTERIYE_TESLIM, v_lat, v_lng, m_lat, m_lng,
                             bilgi.konum_yasi_sn, bilgi.konum_dogruluk_m)

    await manager.broadcast_firma(firma_id, "YENILE")
    await manager.broadcast_arac(bilgi.arac_id, "YENILE")
    
    return {"mesaj": f"Durum {bilgi.yeni_durum} olarak güncellendi."}

class ServistekiAracCikarIstek(BaseModel):
    talep_id: str
    sebep: str = ""

@app.get("/serviste-bekleyen-araclar")
def serviste_bekleyen_araclar(firma_id: str, yetkili = Depends(yetki_kontrol)):
    """Panels: customer cars that a valet brought in and that are waiting at the service center.

    These are completed pickup tasks (TAMAM_SERVIS) that have no delivery task yet and were
    not closed by hand. The advisor starts the delivery from this list with one click.
    """
    if yetkili["rol"] not in ["ADMIN", "SUPERADMIN", "OPERASYON", "DANISMAN"]:
        raise HTTPException(status_code=403)
    if yetkili["rol"] != "SUPERADMIN" and yetkili.get("firma_id") != firma_id:
        raise HTTPException(status_code=403)
    if not _gecerli_uuid(firma_id):
        return []

    # tamamlanma_tarihi here is when the car arrived at the service center.
    sorgu = supabase.table("talepler").select(
        "id, musteri_ad, musteri_tel, musteri_plaka, marka, sube_id, kayit_tarihi, tamamlanma_tarihi, serviste_kapandi"
    ).eq("firma_id", firma_id).eq("gorev_tipi", "VALE_ALIM").eq("durum", "TAMAM_SERVIS")

    if yetkili["rol"] in ["DANISMAN", "OPERASYON"]:
        sorgu = sorgu.eq("marka", yetkili.get("marka", "Genel"))

    # Filter out rows closed by hand in Python: .neq(True) in SQL would also drop NULL rows.
    bekleyenler = [b for b in kapsam_sube_filtrele(yetkili, sorgu.execute().data) if not b.get("serviste_kapandi")]
    if not bekleyenler:
        return []

    # Drop cars that already have a delivery task (linked through iliskili_talep_id). A
    # cancelled delivery does not count: the car is still at the service center and must come
    # back to this list, or it would vanish from both places. The cancelled filter runs in
    # Python for the same NULL reason as above.
    ids = [b["id"] for b in bekleyenler]
    bagli = supabase.table("talepler").select("iliskili_talep_id, durum").in_("iliskili_talep_id", ids).execute().data
    teslime_cikan = {b["iliskili_talep_id"] for b in bagli
                     if b.get("iliskili_talep_id") and b.get("durum") != "IPTAL_EDILDI"}
    return [b for b in bekleyenler if b["id"] not in teslime_cikan]


@app.post("/servisteki-arac-cikar")
async def servisteki_arac_cikar(bilgi: ServistekiAracCikarIstek, request: Request, yetkili = Depends(yetki_kontrol)):
    """Panels: remove a car from the "waiting at service" list (the customer collected it).

    The status is not changed, so the valet's pickup still counts in reports.
    """
    if yetkili["rol"] not in ["ADMIN", "SUPERADMIN", "OPERASYON", "DANISMAN"]:
        raise HTTPException(status_code=403)

    res = (await run_query(supabase.table("talepler").select("id, firma_id, musteri_plaka, gorev_tipi, durum").eq("id", bilgi.talep_id))).data
    if not res:
        raise HTTPException(status_code=404, detail="Kayıt bulunamadı.")
    kayit = res[0]
    if yetkili["rol"] != "SUPERADMIN" and kayit.get("firma_id") != yetkili.get("firma_id"):
        raise HTTPException(status_code=403, detail="Bu kayda yetkiniz yok.")
    if kayit.get("gorev_tipi") != "VALE_ALIM" or kayit.get("durum") != "TAMAM_SERVIS":
        raise HTTPException(status_code=400, detail="Bu kayıt serviste bekleyen araç değil.")

    # Destroy the customer's link too: the car is no longer at the service center.
    await run_query(supabase.table("talepler").update({
        "serviste_kapandi": True,
        "token": f"BTT-{secrets.token_hex(8)}"
    }).eq("id", bilgi.talep_id))
    await audit_log_yaz(
        yapan=yetkili, eylem="SERVISTEKI_ARAC_CIKAR", hedef_tip="TALEP", hedef_id=bilgi.talep_id,
        hedef_aciklama=f"serviste bekleyen araç listeden çıkarıldı: {kayit.get('musteri_plaka')}",
        detay={"plaka": kayit.get("musteri_plaka"), "sebep": (bilgi.sebep or "")[:200]}, request=request)
    await manager.broadcast_firma(kayit["firma_id"], "YENILE")
    return {"mesaj": "Araç listeden çıkarıldı."}


class ValeGorevIptalIstek(BaseModel):
    talep_id: str
    sebep: str = ""


@app.post("/vale-gorev-iptal")
async def vale_gorev_iptal(bilgi: ValeGorevIptalIstek, request: Request, yetkili = Depends(yetki_kontrol)):
    """Panels: cancel an active valet task (opened by mistake, or the customer changed their mind).

    Without a cancel path, a wrong task would block the valet for good: it cannot be
    completed, and the one-active-task rule stops a new one. Cancelling is only allowed
    before the valet has the car; see VALE_IPTAL_EDILEBILIR_DURUMLAR.
    """
    if yetkili["rol"] not in ["ADMIN", "SUPERADMIN", "OPERASYON", "DANISMAN"]:
        raise HTTPException(status_code=403)
    if not _gecerli_uuid(bilgi.talep_id):
        raise HTTPException(status_code=400, detail="Geçersiz görev kaydı.")

    res = (await run_query(supabase.table("talepler").select(
        "id, firma_id, arac_id, marka, sube_id, musteri_ad, musteri_plaka, gorev_tipi, durum, iliskili_talep_id"
    ).eq("id", bilgi.talep_id))).data
    if not res:
        raise HTTPException(status_code=404, detail="Görev bulunamadı.")
    kayit = res[0]

    if yetkili["rol"] != "SUPERADMIN" and kayit.get("firma_id") != yetkili.get("firma_id"):
        raise HTTPException(status_code=403, detail="Bu göreve yetkiniz yok.")
    if yetkili["rol"] in ["DANISMAN", "OPERASYON"] and kayit.get("marka") != yetkili.get("marka", "Genel"):
        raise HTTPException(status_code=403, detail="Bu göreve yetkiniz yok.")
    if not kapsam_sube_filtrele(yetkili, [kayit]):
        raise HTTPException(status_code=403, detail="Bu görev sizin şubenize ait değil.")

    if not (kayit.get("gorev_tipi") or "").startswith("VALE_"):
        raise HTTPException(status_code=400, detail="Bu uç yalnız vale görevleri içindir.")
    # Idempotency: a double click or resend is not an error.
    if kayit.get("durum") == "IPTAL_EDILDI":
        return {"mesaj": "Görev zaten iptal edilmiş."}
    # gorev_tipi was checked to be VALE_* above, so this lookup always finds an entry.
    iptal_izinli_durumlar = VALE_IPTAL_EDILEBILIR_DURUMLAR.get(kayit.get("gorev_tipi"), [])
    if kayit.get("durum") not in iptal_izinli_durumlar:
        raise HTTPException(
            status_code=409,
            detail=("Araç servisten çıkarıldıktan sonra teslim görevi iptal edilemez; teslimin "
                    "tamamlanması gerekir.")
            if kayit.get("gorev_tipi") == "VALE_TESLIM"
            else "Araç alındıktan sonra görev iptal edilemez; görevin tamamlanması gerekir."
        )

    # Cancelling closes the task, so the location is erased and the link destroyed. The
    # .in_("durum", ...) filter is a race guard: if the valet took the car at that very
    # moment, the cancel does not apply.
    guncelleme = await run_query(supabase.table("talepler").update({
        "durum": "IPTAL_EDILDI",
        "tamamlanma_tarihi": datetime.now(timezone.utc).isoformat(),
        "konum_lat": None,
        "konum_lng": None,
        "token": f"BTT-{secrets.token_hex(8)}"
    }).eq("id", bilgi.talep_id).in_("durum", iptal_izinli_durumlar))

    if not guncelleme.data:
        raise HTTPException(status_code=409, detail="Görev bu sırada ilerledi; iptal edilemedi. Ekranı yenileyin.")

    # Mark any open milestone as cancelled. Otherwise its gercek_varis would stay NULL forever
    # and it would look like a leg still in progress. Reports already skip such rows; this
    # keeps the table clean.
    try:
        await run_query(supabase.table("gorev_etaplari").update({"iptal_edildi": True}).eq(
            "talep_id", bilgi.talep_id).is_("gercek_varis", "null"))
    except Exception as e:
        sentry_sdk.capture_message(f"ETAP İPTAL İŞARETLENEMEDİ: talep={bilgi.talep_id} | {str(e)[:200]}",
                                   level="warning")

    # Reset the valet's vehicle status. VALE_YOLDA set it to GÖREVDE; left as is, an idle valet
    # would show as busy on the panels and in the counters. Only reset when no other active
    # task remains (older data can have more than one).
    if kayit.get("arac_id"):
        kalan_aktif = (await run_query(supabase.table("talepler").select("id").eq(
            "arac_id", kayit["arac_id"]).in_("durum", VALE_AKTIF_DURUMLAR).limit(1))).data
        if not kalan_aktif:
            await run_query(supabase.table("araclar").update({"durum": "MERKEZDE"}).eq("id", kayit["arac_id"]))

    await audit_log_yaz(
        yapan=yetkili, eylem="VALE_GOREV_IPTAL", hedef_tip="TALEP", hedef_id=bilgi.talep_id,
        hedef_aciklama=f"vale görevi iptal edildi: {kayit.get('musteri_plaka') or kayit.get('musteri_ad')}",
        detay={
            "gorev_tipi": kayit.get("gorev_tipi"),
            "onceki_durum": kayit.get("durum"),
            "plaka": kayit.get("musteri_plaka"),
            "sebep": (bilgi.sebep or "")[:200]
        }, request=request)

    await manager.broadcast_firma(kayit["firma_id"], "YENILE")
    if kayit.get("arac_id"):
        await manager.broadcast_arac(str(kayit["arac_id"]), "YENILE")  # the valet app drops the task
    return {"mesaj": "Görev iptal edildi."}


@app.get("/vale-gorevi")
def vale_gorevi_getir(lat: float = None, lng: float = None, yetkili = Depends(yetki_kontrol)):
    """Valet app poll: the valet's current task (a single one), plus the base point when needed.

    Also stores the valet's position, which keeps the customer's ETA fresh.
    """
    if yetkili["rol"] not in ["VALE", "ADMIN", "SUPERADMIN"]:
        raise HTTPException(status_code=403)

    arac_id = yetkili.get("arac_id")
    if not arac_id:
        return {"mesaj": "Aracınız tanımlı değil.", "gorev": None}

    # Like sofor-rotasi: each poll stores the valet's last known position. Only for the VALE
    # role and only valid coordinates; (0, 0) and out-of-range values are ignored.
    if yetkili["rol"] == "VALE" and lat is not None and lng is not None:
        if -90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0 and not (lat == 0.0 and lng == 0.0):
            # son_hareket_zamani is shown on the admin map as "last seen".
            supabase.table("araclar").update({
                "son_lat": lat, "son_lng": lng, "son_hareket_zamani": datetime.now(timezone.utc).isoformat()
            }).eq("id", arac_id).execute()
        
    # Deliberately narrow field list: the customer's token is the customer's link and must
    # not reach the valet.
    VALE_GOREV_ALANLARI = "id, durum, gorev_tipi, musteri_ad, musteri_tel, musteri_plaka, konum_lat, konum_lng, arac_id, kayit_tarihi"
    # Oldest first. With the one-active-task rule there is normally a single row, but older
    # data can have two, and without an order the valet could see a different one each poll.
    devam_edenler = supabase.table("talepler").select(VALE_GOREV_ALANLARI).eq("arac_id", arac_id).in_("durum", ["KONUM_ALINDI_VALE", "VALE_YOLDA", "ARAC_ALINDI"]).order("kayit_tarihi", desc=False).execute()

    # A task whose customer has not confirmed a location yet is shown only if nothing is in progress.
    bekleyenler = supabase.table("talepler").select(VALE_GOREV_ALANLARI).eq("arac_id", arac_id).eq("durum", "BEKLIYOR_KONUM").order("kayit_tarihi", desc=False).execute()

    gorev = devam_edenler.data[0] if devam_edenler.data else (bekleyenler.data[0] if bekleyenler.data else None)
    if not gorev:
        return {"mesaj": "Görev yok", "gorev": None}

    # Base point (branch, else HQ) for the "drive back to the service center" navigation in a
    # pickup. Only fetched for pickups, to save queries on every poll.
    # It is already sent in VALE_YOLDA, one step early. When the valet presses "car taken",
    # the app still holds the VALE_YOLDA version of the task, and the navigation has to open
    # inside that click: a browser only lets a PWA open an external app during a user
    # gesture, so data arriving on the next poll would be too late.
    merkez_gerekli = (gorev.get("gorev_tipi") == "VALE_ALIM"
                      and gorev.get("durum") in ("VALE_YOLDA", "ARAC_ALINDI"))
    firma_res = supabase.table("firmalar").select("merkez_lat, merkez_lng").eq("id", yetkili.get("firma_id")).execute().data if merkez_gerekli else None
    if firma_res:
        arac_res = supabase.table("araclar").select("sube_id").eq("id", arac_id).execute().data
        fallback = (firma_res[0].get("merkez_lat"), firma_res[0].get("merkez_lng"))
        m_lat, m_lng = referans_konum(yetkili.get("firma_id"), arac_res[0].get("sube_id") if arac_res else None, fallback)
        gorev["merkez_lat"] = m_lat
        gorev["merkez_lng"] = m_lng

    mesaj = "Aktif görev var" if devam_edenler.data else "Müşteri konum onayı bekleniyor"
    return {"mesaj": mesaj, "gorev": gorev}

# ============================================================
# SATISFACTION SURVEY (customer submission)
# ============================================================
# Table definition and design notes: db/memnuniyet_anketleri.sql. No login: the customer's
# link token is the authorisation, as on the other customer endpoints.
class AnketGonderIstek(BaseModel):
    token: str
    puan_genel: int                              # required
    puan_dakiklik: Optional[int] = None           # optional; skipped means NULL, not 0, and is left out of averages
    puan_ilgi: Optional[int] = None
    puan_arac_durumu: Optional[int] = None
    puan_bilgilendirme: Optional[int] = None
    yorum: str = ""


@app.post("/anket-gonder")
@limiter.limit("5/minute")   # against token guessing and repeated submissions
def anket_gonder(bilgi: AnketGonderIstek, request: Request):
    """Customer page: submit the survey for a completed valet delivery, once, within the window."""
    def _puan_gecerli(p, zorunlu=False):
        if p is None:
            return not zorunlu
        return isinstance(p, int) and 1 <= p <= 5

    if not _puan_gecerli(bilgi.puan_genel, zorunlu=True):
        raise HTTPException(status_code=400, detail="Genel memnuniyet puanı 1-5 arasında olmalıdır.")
    for _ad, _p in (("dakiklik", bilgi.puan_dakiklik), ("ilgi", bilgi.puan_ilgi),
                    ("araç durumu", bilgi.puan_arac_durumu), ("bilgilendirme", bilgi.puan_bilgilendirme)):
        if not _puan_gecerli(_p):
            raise HTTPException(status_code=400, detail=f"Geçersiz puan: {_ad}.")

    # The comment is stored as typed and escaped when rendered (DOMPurify in the admin panel).
    # Cleaning it here could change what the customer meant. The panel must never interpret
    # it as HTML.
    yorum = (bilgi.yorum or "").strip()
    if len(yorum) > ANKET_YORUM_MAKS:
        raise HTTPException(status_code=400, detail=f"Yorum en fazla {ANKET_YORUM_MAKS} karakter olabilir.")

    res = supabase.table("talepler").select(
        "id, firma_id, sube_id, marka, arac_id, gorev_tipi, durum, tamamlanma_tarihi"
    ).eq("token", bilgi.token).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Geçersiz link.")
    talep = res.data[0]

    # Surveys exist only for completed valet deliveries.
    if talep.get("gorev_tipi") != "VALE_TESLIM" or talep.get("durum") != "TAMAM_MUSTERI":
        raise HTTPException(status_code=400, detail="Bu görev için anket açık değil.")

    # Same window rule as the survey mode in talep-detay. If the two differed, the form could
    # be shown and then the submission rejected, with no visible reason.
    if not talep.get("tamamlanma_tarihi"):
        raise HTTPException(status_code=410, detail="Anket süresi dolmuş.")
    try:
        _bitis = supabase_tarih_parse(talep["tamamlanma_tarihi"])
    except (ValueError, TypeError):
        raise HTTPException(status_code=410, detail="Anket süresi dolmuş.")
    if (datetime.now(timezone.utc) - _bitis) > timedelta(hours=ANKET_PENCERESI_SAAT):
        raise HTTPException(status_code=410, detail="Anket süresi dolmuş.")

    # The valet who did the task, for the per-valet report. Stored as the username, so it
    # survives the deletion of the request.
    vale_kadi = None
    if talep.get("arac_id"):
        try:
            _v = supabase.table("kullanicilar").select("kullanici_adi").eq(
                "arac_id", talep["arac_id"]).eq("rol", "VALE").limit(1).execute().data
            if _v:
                vale_kadi = _v[0]["kullanici_adi"]
        except Exception as e:
            sentry_sdk.capture_exception(e)   # the survey is saved even without the valet's name

    try:
        supabase.table("memnuniyet_anketleri").insert({
            "talep_id": talep["id"],
            "firma_id": talep.get("firma_id"),
            "sube_id": talep.get("sube_id"),
            "marka": talep.get("marka"),
            "arac_id": talep.get("arac_id"),
            "vale_kullanici_adi": vale_kadi,
            "gorev_tipi": talep.get("gorev_tipi"),
            "puan_genel": bilgi.puan_genel,
            "puan_dakiklik": bilgi.puan_dakiklik,
            "puan_ilgi": bilgi.puan_ilgi,
            "puan_arac_durumu": bilgi.puan_arac_durumu,
            "puan_bilgilendirme": bilgi.puan_bilgilendirme,
            "yorum": yorum or None,
            "yorum_yazildi": bool(yorum),
        }).execute()
    except Exception as e:
        # talep_id is UNIQUE in the table, so the database settles a race between two tabs.
        # 23505 is unique_violation; tell the customer it was already received.
        if "23505" in str(e) or "duplicate" in str(e).lower():
            raise HTTPException(status_code=409, detail="Değerlendirmeniz zaten alınmış. Teşekkür ederiz.")
        sentry_sdk.capture_exception(e)
        raise HTTPException(status_code=500, detail="Değerlendirme kaydedilemedi, lütfen tekrar deneyin.")

    # Destroy the link: after the survey it has no purpose left.
    try:
        supabase.table("talepler").update(
            {"token": f"BTT-{secrets.token_hex(8)}"}).eq("id", talep["id"]).execute()
    except Exception as e:
        sentry_sdk.capture_exception(e)   # the window is closed anyway because a survey row exists

    return {"mesaj": "Değerlendirmeniz için teşekkür ederiz."}


# ============================================================
# WEBSOCKET REQUESTS TO ANY OTHER PATH
# ============================================================
# The only WebSocket endpoint is /ws. A WebSocket request to any other path would otherwise
# fall through to the StaticFiles mount below, which fails with an unhandled AssertionError
# (it asserts an HTTP scope) and shows up in the error tracker. Such requests come from
# internet scanners probing for exposed Vite dev servers ("Sec-WebSocket-Protocol:
# vite-ping"). Nothing leaked, but the noise hides real errors.
#
# Placement matters on both sides: /ws must be declared before this route (routes match in
# registration order) so the real endpoint is not shadowed, and this route must come before
# the mount so the request never reaches StaticFiles. @app.websocket only matches WebSocket
# scopes, so HTTP traffic is not affected.
#
# There is no accept() on purpose: closing before the handshake completes makes the server
# answer the scanner with HTTP 403.
@app.websocket("/{gecersiz_yol:path}")
async def gecersiz_websocket(websocket: WebSocket, gecersiz_yol: str):
    await websocket.close(code=1008)  # 1008 = policy violation


# Must stay last: this catch-all mount shadows every route declared after it.
app.mount("/", StaticFiles(directory="frontend", html=True), name="static")
