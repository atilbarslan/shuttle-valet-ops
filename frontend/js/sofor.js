// sofor.html: the shuttle driver's PWA. Shows the vehicle's ordered stop list (from
// /sofor-rotasi), starts and ends trips, marks passengers, and hands navigation off to Apple or
// Google Maps. Works offline: write actions are queued and replayed when the connection returns.
// There is no background GPS tracking; the position is sent with each request instead.
// Handlers are attached here because the CSP forbids inline scripts.

// ============================================================
// Every assignment to innerHTML on this page is passed through DOMPurify.
// ============================================================
(function () {
    const orijinalSetter = Object.getOwnPropertyDescriptor(Element.prototype, 'innerHTML').set;
    Object.defineProperty(Element.prototype, 'innerHTML', {
        set: function (html) {
            const temiz = (typeof DOMPurify !== 'undefined' && typeof html === 'string')
                ? DOMPurify.sanitize(html, {
                    ADD_ATTR: ['target', 'rel', 'value', 'placeholder', 'name', 'checked', 'data-aksiyon', 'data-token', 'data-islem', 'data-durak-id', 'data-lat', 'data-lng'],
                    ADD_TAGS: ['svg', 'path', 'use', 'circle', 'rect', 'line'],
                    ALLOW_DATA_ATTR: true,
                    ALLOWED_URI_REGEXP: /^(?:(?:https?|mailto|tel|sms|whatsapp|ftp|data):|[^a-z]|[a-z+.\-]+(?:[^a-z+.\-:]|$))/i
                })
                : html;
            orijinalSetter.call(this, temiz);
        },
        get: Object.getOwnPropertyDescriptor(Element.prototype, 'innerHTML').get
    });
})();

// ============================================================
// App
// ============================================================
const firmaId = localStorage.getItem('aktif_firma_id');
const aracId = localStorage.getItem('aktif_arac_id');
const aracPlaka = localStorage.getItem('aktif_arac_plaka');
const aktifRol = localStorage.getItem('aktif_rol');

// The stop list is kept in localStorage so it survives closing the PWA or reloading offline.
// It contains passenger names, so logout (app_ prefix) and the end of a trip clear it.
let aktifDuraklar = JSON.parse(localStorage.getItem('app_sofor_duraklar') || '[]');
let sonKonum = { lat: null, lng: null };

// Trip state and the base point survive reloads within the session.
let rotaBasladi = sessionStorage.getItem('rotaBasladi') === 'true';
let firmaMerkezLat = parseFloat(sessionStorage.getItem('firmaMerkezLat')) || null;
let firmaMerkezLng = parseFloat(sessionStorage.getItem('firmaMerkezLng')) || null;

function stateKaydet() {
    sessionStorage.setItem('rotaBasladi', rotaBasladi ? 'true' : 'false');
    if (firmaMerkezLat) sessionStorage.setItem('firmaMerkezLat', firmaMerkezLat);
    if (firmaMerkezLng) sessionStorage.setItem('firmaMerkezLng', firmaMerkezLng);
}

function stateSifirla() {
    sessionStorage.removeItem('rotaBasladi');
    sessionStorage.removeItem('firmaMerkezLat');
    sessionStorage.removeItem('firmaMerkezLng');
}

// Back online: send the queued actions and refresh in place.
let oncekiOfflineSureci = false;

window.addEventListener('online', () => {
    oncekiOfflineSureci = false;
    // Refresh in place rather than reloading the page (which would show the splash screen).
    // The WebSocket reconnects by itself through onclose.
    kuyruguErit();   // drains the queue (and calls verileriCek once it is empty)
    verileriCek();   // refresh even if the queue was already empty
});

window.addEventListener('offline', () => {
    oncekiOfflineSureci = true;
});

function getAuthHeaders() {
    const token = localStorage.getItem('app_token');
    return {
        "Authorization": `Bearer ${token}`,
        "Content-Type": "application/json"
    };
}

async function aracDurumuKontrolEt() {
    try {
        const res = await fetch(`${CONFIG.BASE_URL}/sofor-durumu`, { headers: getAuthHeaders() });
        if (res.status === 401) { oturumDolduCikisi(); return; }
        if (res.status === 402) { firmaPasifCikisi(); return; }  // company inactive: log out with that reason
        if (!res.ok) return;
        const veri = await res.json();

        // Detect every change of assignment: a new vehicle, a different vehicle, and being
        // unassigned (null). Reload in each case; when unassigned, the reload shows the
        // "no vehicle" screen instead of failing with 403 on the old vehicle.
        const norm = v => (!v || v === 'null' || v === 'undefined') ? '' : v;
        const mevcut = norm(localStorage.getItem('aktif_arac_id'));
        const yeni = norm(veri.arac_id);
        if (yeni !== mevcut) {
            if (yeni) {
                localStorage.setItem('aktif_arac_id', yeni);
                if (veri.arac_plaka) localStorage.setItem('aktif_arac_plaka', veri.arac_plaka);
            } else {
                localStorage.removeItem('aktif_arac_id');
                localStorage.removeItem('aktif_arac_plaka');
            }
            location.reload();
        }
    } catch (e) { }
}

async function cikisYap(sebep = null) {
    await appCikis();
    if (sebep) {
        window.location.href = `/login.html?sebep=${encodeURIComponent(sebep)}`;
    } else {
        window.location.href = "/login.html";
    }
}

async function oturumDolduCikisi() {
    if (window._oturumDolduTetiklendi) return;
    window._oturumDolduTetiklendi = true;
    await cikisYap('oturum_doldu');
}

// Company inactive or deleted (402): log out and let the login page explain. Runs once even if
// several requests fail at the same time.
async function firmaPasifCikisi() {
    if (window._oturumDolduTetiklendi) return;
    window._oturumDolduTetiklendi = true;
    await cikisYap('firma_pasif');
}

// Warn the driver when the token is about to expire (30 minutes left), so it does not happen
// in the middle of a trip.
let _tokenUyariGosterildi = false;

function tokenSureKontrolu() {
    if (_tokenUyariGosterildi) return;

    const token = localStorage.getItem('app_token');
    if (!token) return;

    try {
        const payload = JSON.parse(atob(token.split('.')[1]));
        const expEpoch = payload.exp;
        if (!expEpoch) return;

        const simdiEpoch = Math.floor(Date.now() / 1000);
        const kalanSaniye = expEpoch - simdiEpoch;
        const kalanDakika = Math.floor(kalanSaniye / 60);

        if (kalanSaniye > 0 && kalanSaniye <= 1800) {
            _tokenUyariGosterildi = true;

            const onayDurum = confirm(
                `⏰ Oturumunuzun dolmasına yaklaşık ${kalanDakika} dakika kaldı.\n\n` +
                `Sürüş ortasında oturum süresi dolmaması için uygun bir zamanda tekrar giriş yapmanız önerilir.\n\n` +
                `Şimdi tekrar giriş yapmak ister misiniz?`
            );

            if (onayDurum) {
                cikisYap();
            }
        }
    } catch (err) {
        console.warn('Token decode hatası:', err);
    }
}

async function firmaKonumunuCek() {
    try {
        const res = await fetch(`${CONFIG.BASE_URL}/firma-detay/${firmaId}`, { headers: getAuthHeaders() });
        if (res.status === 401) { oturumDolduCikisi(); return; }
        const detay = await res.json();
        firmaMerkezLat = detay.merkez_lat || null;
        firmaMerkezLng = detay.merkez_lng || null;
        stateKaydet();
    } catch (e) { }
}

async function verileriCek() {
    navigator.geolocation.getCurrentPosition(
        async (pos) => {
            sonKonum.lat = pos.coords.latitude;
            sonKonum.lng = pos.coords.longitude;

            if (!navigator.onLine) return;

            try {
                const durumRes = await fetch(`${CONFIG.BASE_URL}/sofor-durumu`, { headers: getAuthHeaders() });

                // Only 401 means the session ended. sofor-durumu does not return 403.
                if (durumRes.status === 401) { oturumDolduCikisi(); return; }
                if (durumRes.status === 402) { firmaPasifCikisi(); return; }  // company inactive or deleted
                if (!durumRes.ok) return;

                const durumVeri = await durumRes.json();

                // Base point to return to: the vehicle's branch, else company headquarters.
                if (durumVeri.merkez_lat != null && durumVeri.merkez_lng != null) {
                    firmaMerkezLat = durumVeri.merkez_lat;
                    firmaMerkezLng = durumVeri.merkez_lng;
                    stateKaydet();
                }

                if (durumVeri.arac_plaka) {
                    localStorage.setItem('aktif_arac_plaka', durumVeri.arac_plaka);
                    document.getElementById('aracGosterge').innerText = durumVeri.arac_plaka;
                }

                if (durumVeri.rota_aktif && !rotaBasladi) {
                    rotaBasladi = true;
                    stateKaydet();
                }
            } catch (e) {
                console.error("Vehicle status check failed:", e);
            }

            try {
                const res = await fetch(`${CONFIG.BASE_URL}/sofor-rotasi?arac_id=${aracId}&lat=${sonKonum.lat}&lng=${sonKonum.lng}`, { headers: getAuthHeaders() });
                if (res.status === 401) { oturumDolduCikisi(); return; }
                // 403 here means "this vehicle is no longer yours" (unassigned or reassigned), not an
                // ended session. Resync, which reloads into the right state.
                if (res.status === 403) { aracDurumuKontrolEt(); return; }
                aktifDuraklar = await res.json();
                ekraniGuncelle();
            } catch (e) {
                console.error("Route fetch failed:", e);
            }
        },
        (hata) => {
            document.getElementById('listeGövdesi').innerHTML = `
                        <div class="konum-hata-karti konum-hata-uyari">
                            <h3 class="konum-hata-baslik-uyari">⏳ Konum Uyandırılıyor...</h3>
                            <p>Navigasyondan dönüldüğü için GPS sinyali tazeleniyor. Lütfen bekleyin...</p>
                            <button id="manuelDeneBtn" class="yeniden-dene-btn manuel-dene-gizli">Tekrar Dene</button>
                        </div>`;

            setTimeout(() => {
                if (document.visibilityState === 'visible') {
                    yenidenDene();
                } else {
                    const btn = document.getElementById('manuelDeneBtn');
                    if (btn) btn.style.display = 'inline-block';
                }
            }, 3000);
        },
        { enableHighAccuracy: true, timeout: 15000 }
    );
}

async function hizliYenile() {
    if (!navigator.onLine) return;
    try {
        // No position yet (null): do a full refresh, which waits for GPS. Calling sofor-rotasi
        // with lat=null would get a 422.
        if (!sonKonum.lat || !sonKonum.lng) {
            verileriCek();
            return;
        }

        const res = await fetch(`${CONFIG.BASE_URL}/sofor-rotasi?arac_id=${aracId}&lat=${sonKonum.lat}&lng=${sonKonum.lng}`, {
            headers: getAuthHeaders()
        });

        if (res.status === 401) {
            if (typeof oturumDolduCikisi === "function") oturumDolduCikisi();
            return;
        }
        // 403: the assignment changed, not the session. Resync.
        if (res.status === 403) {
            if (typeof aracDurumuKontrolEt === "function") aracDurumuKontrolEt();
            return;
        }

        if (res.ok) {
            const data = await res.json();
            aktifDuraklar = data || [];
            ekraniGuncelle();
        }
    } catch (error) {
        console.error("Hızlı yenileme sırasında hata oluştu:", error);
    }
}

function yenidenDene() {
    document.getElementById('listeGövdesi').innerHTML = '<p class="bos-liste-mesaj">Konum alınıyor...</p>';
    verileriCek();
}

let durakBasiYolcular = {};

function ekraniGuncelle() {
    // Persist the current list so an offline reload still shows it.
    try { localStorage.setItem('app_sofor_duraklar', JSON.stringify(aktifDuraklar)); } catch (e) {}

    const liste = document.getElementById('listeGövdesi');
    const bitirBtn = document.getElementById('bitirBtn');
    const hazirBtn = document.getElementById('hazirBtn');

    if (aktifDuraklar.length === 0) {
        liste.innerHTML = '<p class="bos-liste-mesaj">Şu an bekleyen durak yok.</p>';
        bitirBtn.style.display = "block";
        hazirBtn.style.display = "none";
        return;
    }

    bitirBtn.style.display = "none";
    const dagitimVarMi = aktifDuraklar.some(d => d.gorev_tipi === 'DAGITIM');
    hazirBtn.style.display = dagitimVarMi ? "block" : "none";

    const sistemModu = aktifDuraklar[0].sistem_modu || "HARITA";

    if (sistemModu === "DURAK") {
        // DURAK mode: one card per stop, with all its passengers.
        // On a LINE route the same physical stop is visited twice, for drop-offs on the way out
        // and pickups on the way back, so cards are grouped by stop and task type. Otherwise a
        // pickup passenger would be collected on the way out. YARIM_AY/NULL: one card per stop.
        const lineMode = aktifDuraklar[0].guzergah_tip === 'LINE';
        const durakGruplari = {};
        aktifDuraklar.forEach(yolcu => {
            const dId = yolcu.secilen_durak_id || "genel";
            const grupKey = lineMode ? (dId + '|' + (yolcu.gorev_tipi || '')) : dId;
            if (!durakGruplari[grupKey]) {
                durakGruplari[grupKey] = {
                    ad: yolcu.durak_adi || "Tanımlanmamış Durak",
                    lat: yolcu.konum_lat,
                    lng: yolcu.konum_lng,
                    durakId: dId,
                    gorev: lineMode ? (yolcu.gorev_tipi || '') : '',
                    yolcular: []
                };
            }
            durakGruplari[grupKey].yolcular.push(yolcu);
        });

        liste.innerHTML = Object.entries(durakGruplari).map(([dId, veri]) => `
                    <div class="durak-karti durak-karti-mor" id="kart-${dId}">
                        <div class="durak-ust">
                            <div class="durak-ust-sol">
                                <h3 class="durak-baslik-siyah">${veri.ad}${veri.gorev === 'DAGITIM' ? ' · Bırakış' : (veri.gorev === 'TOPLAMA' ? ' · Alış' : '')}</h3>
                                <div class="mesafe-bilgi mesafe-bilgi-mavi">Bekleyen: ${veri.yolcular.length} Yolcu</div>
                            </div>
                            <button class="btn btn-gri btn-yol-tarifi" data-aksiyon="tek-navigasyon" data-lat="${veri.lat}" data-lng="${veri.lng}">Yol Tarifi</button>
                        </div>

                        <div class="yolcu-listesi-ic">
                            ${veri.yolcular.map(y => {
            let isDagitim = (y.gorev_tipi === 'DAGITIM');
            let gorevRozeti = isDagitim
                ? '<span class="gorev-rozet gorev-rozet-dagitim">DAĞITIM</span>'
                : '<span class="gorev-rozet gorev-rozet-toplama">TOPLAMA</span>';
            let successVal = isDagitim ? 'indi' : 'alindi';

            return `
                                <div class="durak-yolcu-satir">
                                    <div>
                                        <span class="durak-yolcu-ad">${y.musteri_ad}</span>
                                        <div class="durak-yolcu-rozet">${gorevRozeti}</div>
                                    </div>
                                    <div class="radio-grup">
                                        <input type="radio" name="stat-${y.token}" id="al-${y.token}" data-aksiyon="toplu-secim-kaydet" data-token="${y.token}" data-islem="${successVal}" checked ${!rotaBasladi ? 'disabled' : ''}>
                                        <input type="radio" name="stat-${y.token}" id="gel-${y.token}" data-aksiyon="toplu-secim-kaydet" data-token="${y.token}" data-islem="gelmedi" ${!rotaBasladi ? 'disabled' : ''}>
                                    </div>
                                </div>`;
        }).join('')}
                            <div class="radio-legend">
                                <span>BAŞARILI (İndi/Bindi)</span> <span>GELMEDİ / YOK</span>
                            </div>
                        </div>

                        <button class="akilli-rota-btn durak-bitir-btn" data-aksiyon="durak-tamamla" data-durak-id="${veri.durakId}" data-gorev="${veri.gorev || ''}" data-lat="${veri.lat}" data-lng="${veri.lng}" ${!rotaBasladi ? 'disabled' : ''}>
                            ${rotaBasladi ? 'Durağı Bitir ve Devam Et' : 'Önce Akıllı Rotayı Başlat'}
                        </button>
                    </div>
                `).join('');

    } else {
        // HARITA mode: one card per passenger.
        liste.innerHTML = aktifDuraklar.map(d => {
            const markaBilgisi = d.marka && d.marka !== 'Genel' ? `<span class="marka-rozet">${d.marka}</span>` : '';
            let isDagitim = (d.gorev_tipi === 'DAGITIM');
            let gorevRozeti = isDagitim
                ? '<span class="gorev-rozet gorev-rozet-dagitim">DAĞITIM</span>'
                : '<span class="gorev-rozet gorev-rozet-toplama">TOPLAMA</span>';

            let basariliButonMetni = isDagitim ? 'İNDİ' : 'ARAÇTA';
            let basariliDurum = isDagitim ? 'indi' : 'alindi';

            // Buttons stay disabled until the trip is started (the server refuses with 409 anyway).
            let akilliButonlar = `
                        <button class="btn ${isDagitim ? 'btn-mor' : 'btn-yesil'}" data-aksiyon="islem-yap" data-token="${d.token}" data-islem="${basariliDurum}" ${!rotaBasladi ? 'disabled' : ''}>${basariliButonMetni}</button>
                        <button class="btn btn-kirmizi" data-aksiyon="islem-yap" data-token="${d.token}" data-islem="gelmedi" ${!rotaBasladi ? 'disabled' : ''}>Gelmedi</button>`;

            return `
                    <div class="durak-karti">
                        <div class="durak-ust">
                            <div>
                                <h3>${d.musteri_ad} ${markaBilgisi}</h3>
                                <div class="mesafe-bilgi">${d.mesafe_km} km | ${gorevRozeti}</div>
                            </div>
                            <div class="sure-etiket">~${d.tahmini_dakika} dk</div>
                        </div>
                        <div class="buton-grubu">
                            <button class="btn btn-gri" data-aksiyon="tek-navigasyon" data-lat="${d.konum_lat}" data-lng="${d.konum_lng}">Harita</button>
                            ${akilliButonlar}
                        </div>
                    </div>`;
        }).join('');
    }
}

function topluSecimKaydet(token, islem) {
    durakBasiYolcular[token] = islem;
}

async function durakTamamla(dId, lat, lng, gorev) {
    // On LINE routes the same stop has separate drop-off and pickup cards; complete only this one.
    const eslesir = (y) => (y.secilen_durak_id || "genel") === dId && (!gorev || y.gorev_tipi === gorev);
    const duraktakiYolcular = aktifDuraklar.filter(eslesir);
    const gonderilecekler = duraktakiYolcular.map(y => ({
        token: y.token,
        islem: durakBasiYolcular[y.token] || (y.gorev_tipi === 'DAGITIM' ? 'indi' : 'alindi')
    }));

    if (!confirm(`${duraktakiYolcular.length} yolcu için işlemler onaylanıyor. Sıradaki durağa geçilsin mi?`)) return;

    const sonuc = await guvenliGonder('POST', `${CONFIG.BASE_URL}/durak-islem-tamamla`, {
        arac_id: aracId, durak_lat: lat, durak_lng: lng, yolcular: gonderilecekler
    });
    if (sonuc.durum === 'yetkisiz') return;
    if (sonuc.durum === 'hata') { alert("İşlem başarısız, tekrar deneyin."); return; }

    // 'ok' or 'kuyrukta' (queued): optimistically remove the stop, which also works offline.
    const yeniList = aktifDuraklar.filter(y => !eslesir(y));
    aktifDuraklar = yeniList;
    ekraniGuncelle();

    if (yeniList.length > 0) {
        navigasyonAc(yeniList[0].konum_lat, yeniList[0].konum_lng);
        if (navigator.onLine) verileriCek();
    } else {
        const merkezeMi = confirm("Başka durak kalmadı!\n\nMerkeze dönüş navigasyonunu başlatmak ister misiniz?\n(İptal derseniz rota olduğunuz yerde biter.)");
        if (merkezeMi) {
            navigasyonAc(firmaMerkezLat, firmaMerkezLng);
            document.getElementById('btnMerkezdeyim').style.display = 'block';
        }
        const bitis = await guvenliGonder('POST', `${CONFIG.BASE_URL}/rota-bitir?arac_id=${aracId}&merkeze_donus=${merkezeMi}`);
        if (bitis.durum === 'yetkisiz') return;
        // Do not show the trip as ended if the server refused. The list is empty now, so the
        // "Turu Tamamla" button is visible and the driver can retry from there.
        if (bitis.durum === 'hata') { alert('Rota sonlandırılamadı. Lütfen "Turu Tamamla" ile tekrar deneyin.'); return; }
        rotaBasladi = false;
        aktifDuraklar = [];
        ekraniGuncelle();
        if (typeof konumTakibiniDurdur === 'function') konumTakibiniDurdur();
        stateSifirla();
        if (navigator.onLine) verileriCek();
    }
}

async function aracMerkezeVardi() {
    const sonuc = await guvenliGonder('POST', `${CONFIG.BASE_URL}/arac-durum-guncelle?arac_id=${aracId}&durum=MERKEZDE`);
    if (sonuc.durum === 'yetkisiz') return;
    // The button stays visible on an error so the driver can try again.
    if (sonuc.durum === 'kuyrukta') {
        document.getElementById('btnMerkezdeyim').style.display = 'none';
        offlineToast("İnternet yok. 'Merkezdeyim' bağlantı gelince gönderilecek.");
    } else if (sonuc.durum === 'ok') {
        document.getElementById('btnMerkezdeyim').style.display = 'none';
        alert("Araç durumu 'Merkezde' olarak güncellendi!");
    } else {
        alert("İşlem başarısız, tekrar deneyin.");
    }
}

// Open turn-by-turn navigation: Apple Maps on iOS, Google Maps elsewhere. A temporary link is
// clicked so the OS hands off to the maps app. Must be called synchronously within the user's
// click, never after an await, or the map opens inside the PWA.
function navigasyonAc(lat, lng) {
    const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) && !window.MSStream;
    const url = isIOS
        ? `http://maps.apple.com/?daddr=${lat},${lng}`
        : `https://www.google.com/maps/dir/?api=1&destination=${lat},${lng}`;

    const gizliLink = document.createElement('a');
    gizliLink.href = url;
    document.body.appendChild(gizliLink);
    gizliLink.click();
    document.body.removeChild(gizliLink);
}

function tekNavigasyon(lat, lng) { navigasyonAc(lat, lng); }

function tumRotayiAc() {
    // Starting a trip needs the server, so it is not possible offline.
    if (!navigator.onLine) { alert("Rota başlatmak için internet bağlantısı gerekli."); return; }
    if (aktifDuraklar.length === 0) return alert("Aktif durak yok.");
    if (!sonKonum.lat) { alert("Konumunuz henüz alınamadı, bekleyin."); return; }

    rotaBasladi = true;
    stateKaydet();
    ekraniGuncelle();  // enable the passenger buttons now, without waiting for a refresh

    const ilkDurak = aktifDuraklar[0];
    navigasyonAc(ilkDurak.konum_lat, ilkDurak.konum_lng);

    // Navigation has to open inside the click, so the request goes out afterwards and its result
    // is checked here. If the server refuses, undo the optimistic start. Starting a trip is never
    // queued. If the request did reach the server despite a network error, the next refresh sees
    // the open trip on the server and marks it started again.
    const baslatmayiGeriAl = (mesaj) => {
        rotaBasladi = false;
        stateKaydet();
        ekraniGuncelle();
        alert(mesaj);
    };
    fetch(`${CONFIG.BASE_URL}/rota-baslat?arac_id=${aracId}&lat=${sonKonum.lat}&lng=${sonKonum.lng}`, { headers: getAuthHeaders() })
        .then(async (res) => {
            if (res.ok) return;
            if (res.status === 401) { oturumDolduCikisi(); return; }
            const hata = await res.json().catch(() => ({}));
            baslatmayiGeriAl(typeof hata.detail === 'string'
                ? `Rota başlatılamadı.\n${hata.detail}`
                : "Rota başlatılamadı. Lütfen tekrar deneyin.");
        })
        .catch(() => baslatmayiGeriAl("Rota başlatılamadı. İnternet bağlantınızı kontrol edip tekrar deneyin."));
}

async function islemYap(token, tip) {
    if (tip === 'gelmedi' && !confirm("Yolcunun gelmediğini onaylıyor musunuz?")) return;

    let kalanDuraklar = aktifDuraklar.filter(d => d.token !== token);
    // Set when this was the last passenger and the trip has to end after it.
    let rotaBitisUrl = null;
    let merkezeMi = false;

    if (rotaBasladi) {
        let hedefLat = null;
        let hedefLng = null;

        if (kalanDuraklar.length > 0) {
            hedefLat = kalanDuraklar[0].konum_lat;
            hedefLng = kalanDuraklar[0].konum_lng;
            navigasyonAc(hedefLat, hedefLng);
        } else if (firmaMerkezLat && firmaMerkezLng) {
            merkezeMi = confirm("Tüm duraklar tamamlandı!\n\nMerkeze dönmek ister misiniz?\n(Hayır/İptal'e basarsanız rota bulunduğunuz yerde biter.)");

            if (merkezeMi) {
                hedefLat = firmaMerkezLat;
                hedefLng = firmaMerkezLng;
                document.getElementById('btnMerkezdeyim').style.display = 'block';
                navigasyonAc(hedefLat, hedefLng);
            }
            rotaBitisUrl = `${CONFIG.BASE_URL}/rota-bitir?arac_id=${aracId}&merkeze_donus=${merkezeMi}`;
        }

        stateKaydet();
    }

    aktifDuraklar = kalanDuraklar;
    ekraniGuncelle();

    let endpoint = (tip === 'alindi') ? 'yolcu-alindi' : (tip === 'indi' ? 'yolcu-indi' : 'yolcu-gelmedi');

    if (!rotaBitisUrl) {
        // The screen was updated optimistically above; guvenliGonder sends or queues (not awaited).
        guvenliGonder('POST', `${CONFIG.BASE_URL}/${endpoint}?token=${token}`);
        return;
    }

    // Last passenger: send the passenger's own action first and wait for it, then end the trip.
    // In the other order the trip would close first and the passenger's action would be refused
    // with 409 ("trip not started"); the same order also holds when both are queued offline.
    // Navigation was already opened above, before any await, as the PWA requires.
    const yolcuSonucu = await guvenliGonder('POST', `${CONFIG.BASE_URL}/${endpoint}?token=${token}`);
    if (yolcuSonucu.durum === 'yetkisiz') return;
    let bitis;
    if (yolcuSonucu.durum === 'kuyrukta') {
        // The passenger's action is waiting in the queue (offline or a temporary server error).
        // Sent now, the end of the trip would reach the server first and the passenger's action
        // would be refused when it is replayed, so it goes into the queue behind it.
        kuyrugaEkle('POST', rotaBitisUrl);
        bitis = { durum: 'kuyrukta' };
    } else {
        bitis = await guvenliGonder('POST', rotaBitisUrl);
    }
    if (bitis.durum === 'yetkisiz') return;
    // Do not show the trip as ended if the server refused; "Turu Tamamla" is visible for a retry.
    if (bitis.durum === 'hata') { alert('Rota sonlandırılamadı. Lütfen "Turu Tamamla" ile tekrar deneyin.'); return; }
    if (!merkezeMi) alert("Rota bulunduğunuz konumda başarıyla sonlandırıldı.");
    rotaBasladi = false;
    stateSifirla();
    stateKaydet();
}

const KUYRUK_KEY = 'app_offline_kuyruk';

// Short notice at the bottom of the screen (styled by class, no inline style).
function offlineToast(mesaj) {
    const info = document.createElement('div');
    info.className = 'offline-info-toast';
    info.textContent = mesaj;
    document.body.appendChild(info);
    setTimeout(() => info.remove(), 4000);
}

// Offline queue entry: {method, url, body, zaman}.
function kuyrugaEkle(method, url, body) {
    let kuyruk = JSON.parse(localStorage.getItem(KUYRUK_KEY) || '[]');
    kuyruk.push({ method: method, url: url, body: body || null, zaman: new Date().getTime() });
    localStorage.setItem(KUYRUK_KEY, JSON.stringify(kuyruk));
    offlineToast("İnternet yok. İşlem kaydedildi, bağlantı gelince aktarılacak.");
}

// Endpoints whose 5xx answer is queued for a later retry, as vale.js does. They are safe to
// replay at any later time: the server skips a passenger or stop that already has the target
// status. rota-bitir and arac-durum-guncelle are left out on purpose: a retry that arrives
// after the driver has started a new trip would end that new trip.
const KUYRUGA_ALINABILIR_UCLAR = ['/yolcu-alindi', '/yolcu-indi', '/yolcu-gelmedi', '/durak-islem-tamamla'];

// Send a write request, or queue it when offline or on a network error. Callers update the
// screen optimistically before calling. Returns {durum: 'ok' | 'kuyrukta' (queued) | 'yetkisiz'
// (unauthorised) | 'hata' (error), res?, status?}. While online, a 5xx from one of
// KUYRUGA_ALINABILIR_UCLAR is queued; any other non-2xx answer is returned as 'hata'.
async function guvenliGonder(method, url, body) {
    if (!navigator.onLine) {
        kuyrugaEkle(method, url, body);
        return { durum: 'kuyrukta' };
    }
    try {
        const opt = { method: method, headers: getAuthHeaders() };
        if (body) opt.body = JSON.stringify(body);
        const res = await fetch(url, opt);
        if (res.status === 401) { oturumDolduCikisi(); return { durum: 'yetkisiz' }; }
        if (res.status >= 500 && KUYRUGA_ALINABILIR_UCLAR.includes(new URL(url).pathname)) {
            // Temporary server error: keep the action for a retry instead of losing it.
            kuyrugaEkle(method, url, body);
            return { durum: 'kuyrukta' };
        }
        if (!res.ok) return { durum: 'hata', status: res.status };
        return { durum: 'ok', res: res };
    } catch (e) {
        // Network error: queue it. Delivery is at least once, which is safe because the
        // passenger, stop and end-of-trip endpoints are idempotent.
        kuyrugaEkle(method, url, body);
        return { durum: 'kuyrukta' };
    }
}

async function kuyruguErit() {
    let kuyruk = JSON.parse(localStorage.getItem(KUYRUK_KEY) || '[]');
    if (kuyruk.length === 0) return;
    let kalanKuyruk = [];
    for (let islem of kuyruk) {
        // Entries in an older {endpoint, token} format may still be waiting on some phones.
        let url = islem.url || (islem.endpoint ? `${CONFIG.BASE_URL}/${islem.endpoint}?token=${islem.token}` : null);
        let method = islem.method || 'POST';
        if (!url) continue;
        // An "end trip" must not overtake an earlier action that is still waiting: it would close
        // the trip and that action would then be refused. Hold it until the queue ahead is clear.
        if (kalanKuyruk.length > 0 && new URL(url).pathname === '/rota-bitir') {
            kalanKuyruk.push(islem);
            continue;
        }
        try {
            const opt = { method: method, headers: getAuthHeaders() };
            if (islem.body) opt.body = JSON.stringify(islem.body);
            const res = await fetch(url, opt);
            // Keep only entries that could succeed later (same rule as vale.js):
            //   - 401: token problem; the same action works after the driver logs in again.
            //   - 5xx: temporary server error; retry.
            // Any other 4xx (409 "trip not started", 404, 403...) is a final refusal and is
            // dropped, since a retry would get the same answer. Treating everything except 401
            // as delivered would silently lose actions on a temporary 500.
            if (res.status === 401 || res.status >= 500) kalanKuyruk.push(islem);
        } catch (e) {
            kalanKuyruk.push(islem); // network error: keep it
        }
    }
    localStorage.setItem(KUYRUK_KEY, JSON.stringify(kalanKuyruk));
    if (kalanKuyruk.length === 0 && navigator.onLine) verileriCek();
}

async function yolculariCagir() {
    // "Shuttle ready" is a live notification to passengers, so it makes no sense offline.
    if (!navigator.onLine) { alert("'Servis Hazır' bildirimi için internet bağlantısı gerekli."); return; }
    if (!confirm("Tüm dağıtım yolcularına 'Servis Hazır' mesajı gönderilecek?")) return;
    try {
        const res = await fetch(`${CONFIG.BASE_URL}/servis-hazir?arac_id=${aracId}`, { method: 'POST', headers: getAuthHeaders() });
        if (res.status === 401) { oturumDolduCikisi(); return; }
        const sonuc = await res.json();
        alert(sonuc.mesaj);
        verileriCek();
    } catch (e) { alert("Bağlantı hatası!"); }
}

async function turuTamamla() {
    if (!rotaBasladi) return alert("Şu an aktif bir rotanız bulunmuyor.");

    const eminMi = confirm("DİKKAT: Henüz tamamlanmamış duraklar olabilir!\n\nRotayı zorla (manuel olarak) bitirmek istediğinize emin misiniz?");

    if (eminMi) {
        const merkezeMi = confirm("Merkeze dönüş yapıyor musunuz?\n\n(Tamam: Dönüş kilometresini yazar, navigasyon açar.\nİptal: Olduğunuz yerde rotayı kapatır.)");

        let url = `${CONFIG.BASE_URL}/rota-bitir?arac_id=${aracId}`;

        if (merkezeMi) {
            url += `&merkeze_donus=true`;
            document.getElementById('btnMerkezdeyim').style.display = 'block';
            navigasyonAc(firmaMerkezLat, firmaMerkezLng);
            alert("Merkeze dönüş başlatıldı. Tur tamamlanıyor...");
        } else {
            url += `&merkeze_donus=false`;
            alert("Rota bulunduğunuz konumda manuel olarak sonlandırıldı.");
        }

        const sonuc = await guvenliGonder('POST', url);
        if (sonuc.durum === 'yetkisiz') return;
        if (sonuc.durum === 'hata') { alert("İşlem başarısız, tekrar deneyin."); return; }
        // 'ok' or queued: end the trip optimistically (works offline too).
        rotaBasladi = false;
        aktifDuraklar = [];
        ekraniGuncelle();
        stateSifirla();
        if (navigator.onLine) verileriCek();
    }
}

// WebSocket: refresh on "YENILE", at most once per second.
let ws = null;
let guncellemeKilit = false;
let reconnectTimeout = null;

function canliBaglantiKur() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

    if (reconnectTimeout) {
        clearTimeout(reconnectTimeout);
        reconnectTimeout = null;
    }

    const wsUrl = CONFIG.BASE_URL.replace(/^http/, 'ws') + '/ws';
    ws = new WebSocket(wsUrl);

    ws.onopen = function () {
        let wsToken = localStorage.getItem('app_token');
        if (!wsToken) {
            wsToken = new URLSearchParams(window.location.search).get('token');
        }

        if (wsToken) {
            ws.send(JSON.stringify({ token: wsToken }));
        }
    };

    ws.onmessage = function (event) {
        if (event.data !== "YENILE") return;
        if (guncellemeKilit) return;
        guncellemeKilit = true;
        try {
            // On the "no vehicle" screen, aracId is a constant read at load time and stays null,
            // so a normal refresh cannot pick up a new assignment. Check the assignment instead,
            // which reloads the page when a vehicle has been assigned.
            if (!aracId || aracId === 'null' || aracId === 'undefined') {
                if (typeof aracDurumuKontrolEt === "function") aracDurumuKontrolEt();  // reloads on assignment
            } else if (typeof hizliYenile === "function") {
                hizliYenile();  // has a vehicle: normal list refresh
            }
        } catch (err) {
            console.error('WS güncelleme hatası:', err);
        } finally {
            setTimeout(() => { guncellemeKilit = false; }, 1000);
        }
    };

    ws.onclose = () => {
        reconnectTimeout = setTimeout(canliBaglantiKur, 3000);
    };
}

document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === 'visible') {

        if (!ws || ws.readyState !== WebSocket.OPEN) {
            canliBaglantiKur();
        }

        // iOS pauses timers in the background, so an assignment may have been missed. Same
        // logic as the WebSocket handler above.
        if (!aracId || aracId === 'null' || aracId === 'undefined') {
            if (typeof aracDurumuKontrolEt === "function") aracDurumuKontrolEt();
        } else if (typeof hizliYenile === "function") {
            hizliYenile();
        }
    }
});

// ============================================================
// Start-up and event listeners
// ============================================================
document.addEventListener('DOMContentLoaded', () => {
    // Fade out the loading overlay.
    setTimeout(() => {
        const el = document.getElementById('px-loading');
        if (el) {
            el.style.opacity = '0';
            setTimeout(() => el.remove(), 300);
        }
    }, 800);

    // Client-side role check (a valet is redirected to the valet app); the server enforces access.
    if (!firmaId || aktifRol !== 'SOFOR') {
        if (aktifRol === 'VALE') {
            window.location.href = "vale.html";
            return;
        }
        document.getElementById('yetkiHata').style.display = 'flex';
        setTimeout(() => { window.location.href = "login.html"; }, 2000);
        return;
    } else if (!aracId || aracId === 'null' || aracId === 'undefined') {
        document.getElementById('soforUygulama').style.display = 'block';
        document.getElementById('aracGosterge').innerText = 'Araç Yok';
        document.getElementById('listeGövdesi').innerHTML = `
                <div class="arac-yok-kutu">
                    <div class="arac-yok-ikon"><svg width="56" height="56" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="1" y="6" width="15" height="10" rx="2"></rect><path d="M16 9h4l3 3v4h-7"></path><circle cx="6" cy="18" r="2"></circle><circle cx="18" cy="18" r="2"></circle></svg></div>
                    <h2 class="arac-yok-baslik">Henüz Araç Atanmadı</h2>
                    <p class="arac-yok-mesaj">
                        Yöneticiniz size bir araç atayana kadar bu ekran aktif olmayacaktır.
                        <br><br>
                        Araç atandığında sayfa otomatik güncellenecektir.
                    </p>
                </div>`;
        const akilliRotaBtn = document.querySelector('.akilli-rota-btn');
        if (akilliRotaBtn) akilliRotaBtn.style.display = 'none';
        // Without a vehicle the action buttons mean nothing; hide them.
        const altAksiyonlar = document.querySelector('.alt-aksiyonlar');
        if (altAksiyonlar) altAksiyonlar.style.display = 'none';
        const hazirBtnYok = document.getElementById('hazirBtn');
        if (hazirBtnYok) hazirBtnYok.style.display = 'none';
        setInterval(aracDurumuKontrolEt, 30000);
    } else {
        document.getElementById('soforUygulama').style.display = 'block';
        document.getElementById('aracGosterge').innerText = aracPlaka || "Araç Yükleniyor...";
        // Show the stored list right away, before (or without) the network.
        if (aktifDuraklar.length > 0) ekraniGuncelle();
        firmaKonumunuCek();
        verileriCek();
        kuyruguErit();
    }

    // Check the token's remaining lifetime every 5 minutes.
    setInterval(tokenSureKontrolu, 5 * 60 * 1000);
    tokenSureKontrolu();

    const cikisBtn = document.getElementById('cikisBtn');
    if (cikisBtn) cikisBtn.addEventListener('click', () => cikisYap());

    const hazirBtn = document.getElementById('hazirBtn');
    if (hazirBtn) hazirBtn.addEventListener('click', yolculariCagir);

    // "Start route" button at the top.
    const akilliRotaBtn = document.querySelector('.akilli-rota-btn');
    if (akilliRotaBtn) akilliRotaBtn.addEventListener('click', tumRotayiAc);

    const bitirBtn = document.getElementById('bitirBtn');
    if (bitirBtn) bitirBtn.addEventListener('click', turuTamamla);

    const btnMerkezdeyim = document.getElementById('btnMerkezdeyim');
    if (btnMerkezdeyim) btnMerkezdeyim.addEventListener('click', aracMerkezeVardi);

    // Stop cards are redrawn constantly, so their controls are handled by delegation.
    const listeGovdesi = document.getElementById('listeGövdesi');
    if (listeGovdesi) {
        listeGovdesi.addEventListener('click', (e) => {
            const btn = e.target.closest('[data-aksiyon]');
            if (!btn) return;

            const aksiyon = btn.dataset.aksiyon;

            if (aksiyon === 'tek-navigasyon') {
                tekNavigasyon(parseFloat(btn.dataset.lat), parseFloat(btn.dataset.lng));
            } else if (aksiyon === 'islem-yap') {
                islemYap(btn.dataset.token, btn.dataset.islem);
            } else if (aksiyon === 'durak-tamamla') {
                durakTamamla(btn.dataset.durakId, parseFloat(btn.dataset.lat), parseFloat(btn.dataset.lng), btn.dataset.gorev || '');
            } else if (aksiyon === 'manuel-dene') {
                yenidenDene();
            }
        });

        // Radio buttons (per-passenger choice on a stop card).
        listeGovdesi.addEventListener('change', (e) => {
            const input = e.target.closest('[data-aksiyon="toplu-secim-kaydet"]');
            if (!input) return;
            topluSecimKaydet(input.dataset.token, input.dataset.islem);
        });
    }

    // "Try again" on the location error card, which verileriCek inserts dynamically.
    document.body.addEventListener('click', (e) => {
        if (e.target && e.target.id === 'manuelDeneBtn') {
            yenidenDene();
        }
    });

    canliBaglantiKur();

    // Service worker registration (PWA).
    if ('serviceWorker' in navigator) {
        window.addEventListener('load', () => {
            // When a new service worker takes control, reload once to run the new code. Only if a
            // worker was already in control, to avoid a pointless reload on first install. The
            // stop list and the offline queue are in localStorage, so the reload loses nothing.
            if (navigator.serviceWorker.controller) {
                let yenilendi = false;
                navigator.serviceWorker.addEventListener('controllerchange', () => {
                    if (yenilendi) return;
                    yenilendi = true;
                    window.location.reload();
                });
            }

            // Show the active version (CACHE_VERSION in sw.js). serviceWorker.ready guarantees an
            // active worker, unlike reading controller, which can still be null.
            navigator.serviceWorker.ready.then(reg => {
                if (!reg.active) return;
                const kanal = new MessageChannel();
                kanal.port1.onmessage = (e) => {
                    const el = document.getElementById('soforSurum');
                    if (el && e.data && e.data.version) {
                        el.textContent = String(e.data.version).replace('app-', '');
                    }
                };
                reg.active.postMessage({ type: 'GET_VERSION' }, [kanal.port2]);
            }).catch(() => {});

            navigator.serviceWorker.register('sw.js')
                .then(reg => {
                    reg.update(); // check for updates at start; iOS PWAs can be slow to do it themselves
                    // On iOS, 'load' does not fire again when the PWA returns from the background,
                    // so also check whenever it becomes visible.
                    document.addEventListener('visibilitychange', () => {
                        if (document.visibilityState === 'visible') reg.update();
                    });
                })
                .catch(err => console.error('Service Worker registration failed:', err));
        });
    }
});