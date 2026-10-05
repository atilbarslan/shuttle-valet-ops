// musteri.html: the customer's tracking page, opened from the link in the request (no login;
// the token in the URL is the authorisation). Flow: privacy notice and consent if required,
// then pick a location on the map (or a stop), then follow the vehicle or valet live, and for
// valet deliveries a satisfaction survey at the end.
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
                    ADD_ATTR: ['target', 'rel', 'value', 'placeholder'],
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
// Mapbox public token. Create one at https://account.mapbox.com/access-tokens/
// and restrict it to your own domain so it cannot be used from other sites.
mapboxgl.accessToken = 'YOUR_MAPBOX_PUBLIC_TOKEN';
const urlParams = new URLSearchParams(window.location.search);
const token = urlParams.get('token');
let map, marker, secilenKonum = null, aktifGorevTipi = "TOPLAMA";
// Once the customer picks a point by hand, GPS updates must not move the pin again.
let manuelKonumSecildi = false;
let secilenDurakId = null;

// ============================================================
// KVKK: privacy notice and explicit consent
// ============================================================
// When consent is required, kvkkEkran is shown before the location screen, and the normal flow
// continues once the customer agrees. kvkkOnayVerildi is sent along with the location; the
// server decides, the page only carries the answer.
let kvkkRizaGerekli = false;
let kvkkOnayVerildi = false;
let kvkkSonrakiEkran = null;   // screen to open after consent (uygulama-arayuzu)
let kvkkSonrakiIslem = null;   // work to run after consent (build the map)

/** Open the target screen, or the KVKK screen first if consent is required and defer the rest.
 *  The map is built only after consent on purpose: Mapbox measures its container when it is
 *  created, and inside a hidden container it can end up as a blank map. */
function ekranAcVeyaRizaSor(veri, hedefEkran, sonrasi) {
    kvkkSonrakiEkran = hedefEkran;
    kvkkSonrakiIslem = sonrasi || null;
    if (kvkkRizaGerekli) { kvkkEkraniGoster(veri); return; }
    ekranDegistir(hedefEkran);
    if (sonrasi) sonrasi();
}

function kvkkEkraniGoster(veri) {
    // The data controller is the customer's company. Without a stored name, the neutral default
    // text in the page stays.
    if (veri.kvkk_firma_unvan) {
        document.getElementById('kvkkUnvan').innerText = veri.kvkk_firma_unvan;
    }
    // List what is processed. Valet tasks also process the car's plate (required for them), so
    // the list must mention it there or the notice would be incomplete.
    const neIsleniyor = document.getElementById('kvkkNeIsleniyor');
    if (neIsleniyor) {
        neIsleniyor.innerHTML = '<strong>Ne işleniyor:</strong> ' + (aktifGorevTipi.startsWith('VALE_')
            ? 'haritada işaretlediğiniz konum, adınız, telefon numaranız ve aracınızın plakası.'
            : 'haritada işaretlediğiniz konum (veya seçtiğiniz durak), adınız ve telefon numaranız.');
    }

    // Link to the full customer privacy notice.
    // ADD THIS FILE: frontend/kvkk/aydinlatma-yolcu.html is NOT included in this repository.
    // Create it yourself with your own customer privacy notice (KVKK "aydınlatma metni").
    // The customer's token is passed in the URL, so that page can load the company's details
    // (data controller name and contact address) itself if you want it to.
    const tamMetin = document.getElementById('kvkkTamMetin');
    if (tamMetin) tamMetin.href = `kvkk/aydinlatma-yolcu.html?token=${encodeURIComponent(token)}`;
    ekranDegistir('kvkkEkran');
}

// Consent given: continue to the screen and work that were deferred.
function kvkkOnayAlindi() {
    kvkkOnayVerildi = true;
    ekranDegistir(kvkkSonrakiEkran || 'uygulama-arayuzu');
    if (kvkkSonrakiIslem) { kvkkSonrakiIslem(); kvkkSonrakiIslem = null; }
}

async function kvkkRedGonder() {
    // A refusal neither cancels the request nor is it final: the link stays alive and the
    // customer can change their mind.
    try {
        await fetch(`${CONFIG.BASE_URL}/riza-red`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ token: token })
        });
    } catch (e) {
        // Even if the refusal could not be recorded, do not block the customer; no personal data
        // was processed.
    }
    document.getElementById('kvkkBilgiBaslik').innerText = "Konum bilginiz alınmadı";
    document.getElementById('kvkkBilgiAciklama').innerText =
        "İşleminiz iptal edilmedi. Firma sizinle telefonla iletişime geçecektir.";
    document.getElementById('kvkkFikrimiDegistirdimBtn').style.display = "block";
    ekranDegistir('kvkkBilgiEkran');
}

async function kvkkGeriCek() {
    // Withdrawal (KVKK art. 7) is not retroactive; the server erases the location and destroys the link.
    if (!confirm("Konum onayınızı geri çekmek istediğinize emin misiniz?\n\nKonum bilginiz silinecek ve varış süresi gösterimi duracaktır. İşleminiz iptal edilmez; firma sizinle iletişime geçecektir.")) return;
    try {
        const cevap = await fetch(`${CONFIG.BASE_URL}/riza-geri-cek`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ token: token })
        });
        if (!cevap.ok) { alert("İşlem tamamlanamadı, lütfen tekrar deneyin."); return; }
    } catch (e) {
        alert("Bağlantı hatası oluştu, lütfen tekrar deneyin.");
        return;
    }
    document.getElementById('kvkkBilgiBaslik').innerText = "Onayınız geri alındı";
    document.getElementById('kvkkBilgiAciklama').innerText =
        "Konum bilginiz silindi. Firma sizinle telefonla iletişime geçecektir.";
    // The link is now destroyed, so there is no way back; hide the "I changed my mind" button.
    document.getElementById('kvkkFikrimiDegistirdimBtn').style.display = "none";
    ekranDegistir('kvkkBilgiEkran');
}

// Arrival as a clock time ("17:10") rather than a duration ("63 min"): customers plan around
// a time, and a time does not go stale while the page stays open. Computed as now + minutes, so
// it shifts on every refresh; the valet screen uses saatBicimle with a server-side time instead.
function varisSaatiHesapla(eklenecekDakika) {
    if (!(eklenecekDakika >= 1)) return "Çok Yakında";
    const varisZamani = new Date();
    varisZamani.setMinutes(varisZamani.getMinutes() + eklenecekDakika);
    const saat = varisZamani.getHours().toString().padStart(2, '0');
    const dk = varisZamani.getMinutes().toString().padStart(2, '0');
    return `${saat}:${dk}`;
}

// Format a fixed arrival time from the server (ISO) in Istanbul time. Unlike
// varisSaatiHesapla, which computes now + minutes, this formats a moment that does not move.
function saatBicimle(iso) {
    if (!iso) return null;
    const d = new Date(iso);
    if (isNaN(d.getTime())) return null;
    return d.toLocaleTimeString('tr-TR', { timeZone: 'Europe/Istanbul', hour: '2-digit', minute: '2-digit' });
}

// Show the "withdraw my consent" link only while there is something to withdraw: consent was
// asked for and a location is still stored. The server erases the location as soon as it is no
// longer needed (pickup: when the car is taken; delivery: at hand-over), after which offering to
// delete it would be misleading.
function geriCekmeLinkiniGuncelle(veri) {
    const rizaUsulu = veri && veri.kvkk_riza_gerekli === true;
    const silinecekKonumVar = veri && veri.konum_lat != null;
    const goster = rizaUsulu && silinecekKonumVar;
    document.querySelectorAll('.kvkk-geri-cek-btn').forEach(b => {
        b.style.display = goster ? "" : "none";
    });
}

async function baslat() {
    if (!token) {
        document.getElementById('hataBaslik').innerText = "Link Eksik";
        document.getElementById('hataAciklama').innerText = "Bu sayfaya direkt erişilemiyor.";
        ekranDegistir('ekran-hata'); return;
    }

    try {
        const cevap = await fetch(`${CONFIG.BASE_URL}/talep-detay/${token}`);
        if (!cevap.ok) {
            document.getElementById('hataBaslik').innerText = cevap.status === 404 ? "Link Bulunamadı" : "Bağlantı Hatası";
            ekranDegistir('ekran-hata'); return;
        }

        const veri = await cevap.json();
        aktifGorevTipi = veri.gorev_tipi || "TOPLAMA";

        // Ask for consent only if the company requires it and it was not already given (for
        // example, the customer reopened the link to correct the pin). If the field is missing
        // the page does not ask; the server owns the decision and rejects a location without
        // consent with 400 anyway.
        kvkkRizaGerekli = (veri.kvkk_riza_gerekli === true) && (veri.riza_alindi !== true);
        kvkkOnayVerildi = (veri.riza_alindi === true);

        geriCekmeLinkiniGuncelle(veri);

        if (veri.durum === "SERVIS_HAZIR") { ekranDegistir('ekran-dagitim-basari'); beklemeKutusunuYesilYap(); return; }
        if (veri.durum === "YOLCU INDI" || veri.durum === "TAMAM_ALINDI") { ekranDegistir('ekran-toplama-takip'); yolculukBittiEkrani(); return; }
        if (veri.durum === "TAMAM_SERVIS" || veri.durum === "TAMAM_MUSTERI") { ekranDegistir('ekran-vale-takip'); valeBasariEkrani(veri.durum, veri); return; }
        if (veri.durum !== "BEKLİYOR" && veri.durum !== "BEKLIYOR_KONUM") { 
            if (aktifGorevTipi.startsWith("VALE_")) {
                ekranDegistir('ekran-vale-takip');
                valeTakipGuncelle(veri);
            } else {
                ekranDegistir(aktifGorevTipi === 'DAGITIM' ? 'ekran-dagitim-basari' : 'ekran-toplama-takip');
            }
            return; 
        }

        document.getElementById('karsilama').innerText = `Merhaba, ${veri.musteri_ad}`;
        // Valet tasks: hide the yellow plate box, which is meant for the shuttle vehicle's plate.
        // For a valet, arac_plaka is the virtual vehicle's plate, i.e. the valet's username, which
        // must not be shown to customers. Show the valet's display name instead.
        const valeGorevi = aktifGorevTipi.startsWith("VALE_");
        const plakaKutusu = document.getElementById('aracPlaka');
        const valeSatiri = document.getElementById('valePersonel');
        if (valeGorevi) {
            if (plakaKutusu) plakaKutusu.style.display = "none";
            // No display name: hide the line rather than show an empty label.
            if (valeSatiri) {
                if (veri.vale_adi) {
                    valeSatiri.innerText = `Vale hizmetini sağlayacak personel: ${veri.vale_adi}`;
                    valeSatiri.style.display = "block";
                } else {
                    valeSatiri.style.display = "none";
                }
            }
        } else if (veri.arac_id && plakaKutusu) {
            plakaKutusu.innerText = veri.arac_plaka || veri.arac_id;
            plakaKutusu.style.display = "inline-block";
        }

        // Valet tasks need a door location, so they always use the map, even in a DURAK company.
        const sistemModu = aktifGorevTipi.startsWith('VALE_') ? "HARITA" : (veri.sistem_modu || "HARITA");

        if (sistemModu === "DURAK") {
            document.getElementById('gorevEtiketi').innerText = "SABİT DURAK / SERVİS SİSTEMİ";
            document.getElementById('gorevEtiketi').style.background = "#e5e5ea";
            document.getElementById('gorevAciklamasi').innerText = "Aracınızın güzergahındaki duraklar aşağıda listelenmiştir.";

            document.getElementById('map-area').style.display = 'none';
            document.getElementById('durak-area').style.display = 'flex';
            document.getElementById('bilgi-alani').innerHTML = `Listeden durak seçiniz`;

            // (Valet tasks never reach this branch; see sistemModu above.)
            const durakSecimMetni = document.getElementById('durakSecimMetni');
            if (aktifGorevTipi === 'DAGITIM') {
                durakSecimMetni.innerText = "Lütfen gitmek (inmek) istediğiniz durağı seçin. Servis aracı sizi bu durağa bırakacaktır.";
            } else {
                durakSecimMetni.innerText = "Lütfen size en yakın durağı seçin. Servis aracı belirtilen saatte bu durakta olacaktır.";
            }

            const durakRes = await fetch(`${CONFIG.BASE_URL}/firma-duraklari?firma_id=${veri.firma_id}&talep_token=${token}`);
            const duraklar = await durakRes.json();

            const aracinDuraklari = duraklar.filter(d => d.guzergah_id === veri.guzergah_id);
            aracinDuraklari.sort((a, b) => a.sira_no - b.sira_no);
            const secici = document.getElementById('durakSecici');

            if (aracinDuraklari.length === 0) {
                secici.innerHTML = '<option value="">Bu araca atanmış bir hat/durak bulunamadı.</option>';
            } else {
                secici.innerHTML = '<option value="">-- Lütfen Durak Seçiniz --</option>' +
                    aracinDuraklari.map(d => `<option value="${d.id}|${d.konum_lat},${d.konum_lng}">${d.durak_adi}</option>`).join('');
            }

            ekranAcVeyaRizaSor(veri, 'uygulama-arayuzu');
        } else {
            if (aktifGorevTipi.startsWith('VALE_')) {
                const valeAlimMi = aktifGorevTipi === 'VALE_ALIM';
                document.getElementById('gorevEtiketi').innerText = valeAlimMi ? "VALE: ARAÇ ALIM" : "VALE: ARAÇ TESLİM";
                document.getElementById('gorevEtiketi').style.background = "#e5f0ff";
                document.getElementById('gorevEtiketi').style.color = "var(--blue)";
                // Mention the plate so the customer knows which car this is about.
                const plakaOneki = veri.musteri_plaka ? `${veri.musteri_plaka} plakalı aracınızın` : "Aracınızın";
                document.getElementById('gorevAciklamasi').innerText = plakaOneki + (valeAlimMi
                    ? " alınacağı tam konumu haritadan işaretleyin."
                    : " teslim edileceği konumu haritadan işaretleyin.");
            } else if (aktifGorevTipi === 'DAGITIM') {
                document.getElementById('gorevEtiketi').innerText = "MERKEZDEN EVE DAĞITIM";
                document.getElementById('gorevEtiketi').style.background = "#e5e5ea";
                document.getElementById('gorevAciklamasi').innerText = "Lütfen merkeze dönüşte bırakılacağınız ev adresini haritadan işaretleyin.";
            } else {
                document.getElementById('gorevEtiketi').innerText = "EVDEN MERKEZE TOPLAMA";
                document.getElementById('gorevEtiketi').style.background = "#e5f0ff";
                document.getElementById('gorevEtiketi').style.color = "var(--blue)";
                document.getElementById('gorevAciklamasi').innerText = "Lütfen alınacağınız tam konumu haritadan işaretleyin.";
            }

            let merkezLat = veri.merkez_lat || null;
            let merkezLng = veri.merkez_lng || null;

            // Show the container first (ekranDegistir sets display:flex), then build the map. Mapbox
            // reads the container size synchronously, which forces a layout, so it gets the right
            // size without waiting for a frame. (Blank maps were caused by the referrer policy in
            // musteri.html, not by timing.)
            ekranAcVeyaRizaSor(veri, 'uygulama-arayuzu', () => haritayiKur(merkezLat, merkezLng));
        }

    } catch (err) {
        document.getElementById('hataBaslik').innerText = "Bağlantı Kurulamadı";
        ekranDegistir('ekran-hata');
    }
}

function durakSecildi(selectElement) {
    if (!selectElement.value) {
        secilenKonum = null;
        secilenDurakId = null;
        document.getElementById('onayBtn').disabled = true;
        document.getElementById('bilgi-alani').innerHTML = `Lütfen listeden seçim yapın`;
        return;
    }

    const parcalar = selectElement.value.split('|');
    secilenDurakId = parcalar[0];
    const coords = parcalar[1].split(',');
    secilenKonum = { lat: parseFloat(coords[0]), lng: parseFloat(coords[1]) };

    document.getElementById('onayBtn').disabled = false;

    // Link to the stop on Google Maps so the customer can check where it is.
    const haritaLink = `https://www.google.com/maps/search/?api=1&query=${secilenKonum.lat},${secilenKonum.lng}`;

    document.getElementById('bilgi-alani').innerHTML = `
                <span class="yesil-onay">✓ Durak seçimi alındı.</span><br>
                <a href="${haritaLink}" target="_blank" rel="noopener noreferrer" class="harita-link">
                    Seçtiğim Durağı Haritada Gör
                </a>`;
}

function haritayiKur(merkezLat, merkezLng) {
    const baslangicLat = merkezLat || 38.4189; // default view (İzmir) when the company has no base location
    const baslangicLng = merkezLng || 27.1287;
    const baslangicZoom = merkezLat ? 14 : 7;

    map = new mapboxgl.Map({
        container: 'map',
        style: 'mapbox://styles/mapbox/streets-v12',
        center: [baslangicLng, baslangicLat], // Mapbox expects [longitude, latitude]
        zoom: baslangicZoom
    });

    // "Locate me" button.
    const geolocate = new mapboxgl.GeolocateControl({
        positionOptions: { enableHighAccuracy: true },
        trackUserLocation: true,
        showUserHeading: true
    });
    map.addControl(geolocate);

    // Locate the customer automatically once the map has loaded.
    map.on('load', () => {
        map.resize();  // re-measure the container at load time, against a blank map
        geolocate.trigger();
    });

    // Drop the pin on the GPS position.
    geolocate.on('geolocate', (e) => {
        if (manuelKonumSecildi) return;  // a hand-picked point wins over GPS
        konumGuncelle([e.coords.longitude, e.coords.latitude]);
    });

    // Pressing the locate button again means "use my GPS position" again.
    geolocate.on('trackuserlocationstart', () => {
        manuelKonumSecildi = false;
    });

    map.on('click', (e) => {
        manuelKonumSecildi = true;  // GPS must not overwrite it from now on
        konumGuncelle([e.lngLat.lng, e.lngLat.lat]);
    });
}

function konumGuncelle(lngLatArray) {
    secilenKonum = { lat: lngLatArray[1], lng: lngLatArray[0] };
    document.getElementById('onayBtn').disabled = false;
    // Tell the customer whether the pin came from GPS or was placed by hand.
    const mesaj = manuelKonumSecildi
        ? '<span class="yesil-mini">✓ Manuel olarak işaretlendi</span><br><small class="gri-mini">GPS butonuyla mevcut konumunuza dönebilirsiniz</small>'
        : '<span class="yesil-mini">✓ Konum işaretlendi</span>';
    document.getElementById('bilgi-alani').innerHTML = mesaj;

    if (marker) {
        marker.setLngLat(lngLatArray);
    } else {
        const el = document.createElement('div');
        el.className = 'custom-pin';
        el.innerHTML = '📍';

        marker = new mapboxgl.Marker({ element: el, draggable: true })
            .setLngLat(lngLatArray)
            .addTo(map);

        marker.on('dragend', () => {
            manuelKonumSecildi = true;  // dragging the pin counts as a manual choice
            const currentLngLat = marker.getLngLat();
            konumGuncelle([currentLngLat.lng, currentLngLat.lat]);
        });
    }
}

async function konumuGonder() {
    if (!secilenKonum || !token) return;
    const btn = document.getElementById('onayBtn');
    btn.innerText = "Gönderiliyor..."; btn.disabled = true;
    try {
        const gidenVeri = {
            token: token,
            lat: secilenKonum.lat,
            lng: secilenKonum.lng,
            // The page only carries the answer; the server decides (400 if consent is required and missing).
            riza_onay: kvkkOnayVerildi
        };

        // Include the stop only when one was chosen. In HARITA mode the field is left out
        // entirely instead of being sent as null.
        if (secilenDurakId) {
            gidenVeri.secilen_durak_id = secilenDurakId;
        }

        const endpoint = aktifGorevTipi.startsWith("VALE_") ? "/vale-konum-onay" : "/konum-dogrula";
        const cevap = await fetch(CONFIG.BASE_URL + endpoint, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(gidenVeri)
        });

        if (cevap.ok) {
            if (aktifGorevTipi.startsWith("VALE_")) {
                ekranDegistir('ekran-vale-takip');
                valeTakipGuncelle({durum: 'KONUM_ALINDI_VALE'});
            } else {
                ekranDegistir(aktifGorevTipi === 'DAGITIM' ? 'ekran-dagitim-basari' : 'ekran-toplama-takip');
            }
        } else if (cevap.status === 400 || cevap.status === 503) {
            // Consent missing (400) or the consent record could not be saved (503): show the
            // server's explanation, which is more useful than a status code.
            let detay = "İşlem tamamlanamadı.";
            try { const j = await cevap.json(); if (j.detail) detay = j.detail; } catch (e) { }
            alert(detay);
            btn.innerText = "Konumu Onayla";
            btn.disabled = false;
        } else {
            // Any other refusal: re-enable the button.
            alert("İşlem onaylanamadı. (Hata kodu: " + cevap.status + ")");
            btn.innerText = "Konumu Onayla";
            btn.disabled = false;
        }
    } catch (e) {
        alert("Bağlantı hatası oluştu, tekrar deneyin.");
        btn.innerText = "Konumu Onayla";
        btn.disabled = false;
    }
}

function ekranDegistir(aktifId) {
    // Do not call durumuCanliKontrolEt again if the target screen is already open. Otherwise
    // the no-show branch recurses (durumuCanliKontrolEt -> ekranDegistir('ekran-toplama-takip')
    // -> durumuCanliKontrolEt -> ...) and hits talep-detay dozens of times a second until the
    // rate limit stops it. WebSocket, visibility and online triggers call durumuCanliKontrolEt
    // directly, so they are not affected.
    const hedefEl = document.getElementById(aktifId);
    const zatenAcik = !!(hedefEl && hedefEl.style.display === 'flex');
    // Every screen must be in this list; a screen missing here is never shown or hidden.
    ['yukleniyor', 'ekran-hata', 'uygulama-arayuzu', 'ekran-dagitim-basari', 'ekran-toplama-takip', 'ekran-vale-takip',
     'kvkkEkran', 'kvkkOnaysizEkran', 'kvkkBilgiEkran'].forEach(id => {
        const el = document.getElementById(id);
        if (!el) return;
        if (id === aktifId) el.style.display = 'flex'; else el.style.display = 'none';
    });
    if (aktifId === 'uygulama-arayuzu' && map) setTimeout(() => map.resize(), 300); // re-measure after the screen becomes visible
    if ((aktifId === 'ekran-dagitim-basari' || aktifId === 'ekran-toplama-takip') && !zatenAcik) durumuCanliKontrolEt();
}

let sonBilinenDurum = "";

async function durumuCanliKontrolEt() {
    // Never switch screens while a KVKK screen is open. WebSocket and visibility events call
    // this function directly, and without the guard the page could jump to a tracking screen
    // while the customer is still reading the consent text.
    const kvkkAcik = ['kvkkEkran', 'kvkkOnaysizEkran', 'kvkkBilgiEkran'].some(id => {
        const el = document.getElementById(id);
        return el && el.style.display === 'flex';
    });
    if (kvkkAcik) return;
    // After the survey is sent the token is destroyed, so every poll would get 404 and replace
    // the thank-you screen with a "link ended" error. Leave the screen as it is.
    if (anketGonderildi) return;

    try {
        const zamanDamgasi = new Date().getTime();
        const cevap = await fetch(`${CONFIG.BASE_URL}/talep-detay/${token}?t=${zamanDamgasi}`);
        // The token may have been destroyed (valet delivery started, task closed) or expired.
        // Say so instead of silently freezing on the last state.
        if (!cevap.ok) {
            document.getElementById('hataBaslik').innerText =
                cevap.status === 410 ? "Bağlantı Süresi Doldu" : "Bağlantı Sonlandı";
            document.getElementById('hataAciklama').innerText =
                "Bu takip bağlantısı artık geçerli değil. Yeni bir işlem başlatıldıysa size yeni bir bağlantı iletilir.";
            ekranDegistir('ekran-hata');
            return;
        }
        const veri = await cevap.json();

        if (sonBilinenDurum === 'YOLCU ALINDI' && veri.durum === 'KONUM ALINDI') return;
        sonBilinenDurum = veri.durum;

        // Hide the withdraw link once the location has been erased.
        geriCekmeLinkiniGuncelle(veri);

        if (aktifGorevTipi.startsWith('VALE_')) { valeTakipGuncelle(veri); return; }
        if (veri.durum === 'SERVIS_HAZIR' && aktifGorevTipi === 'DAGITIM') { beklemeKutusunuYesilYap(); return; }
        // A closed no-show (TAMAM_GELMEDI) shows "you did not board", not "trip finished". In
        // practice the token is destroyed by then and the page gets 404; this is defensive.
        if (veri.durum === 'YOLCU INDI' || veri.durum === 'TAMAM_ALINDI') { yolculukBittiEkrani(); return; }
        if (veri.durum === 'YOLCU GELMEDİ' || veri.durum === 'TAMAM_GELMEDI') {
            ekranDegistir('ekran-toplama-takip');
            const siraKutusu = document.getElementById('siraKutusu');
            if (siraKutusu) { siraKutusu.style.background = '#ffebee'; siraKutusu.style.borderColor = '#ffcdd2'; }
            const sureMetni = document.getElementById('canliSure');
            if (sureMetni) { sureMetni.innerText = "Araca Binmediniz"; sureMetni.style.fontSize = "22px"; sureMetni.style.color = '#c62828'; }
            const altBilgi = document.getElementById('siraAltBilgi');
            if (altBilgi) altBilgi.innerHTML = "Servis aracına biniş yapmadığınız tespit edildi.";
            return;
        }

        if (veri.durum === 'YOLCU ALINDI') {
            const siraKutusu = document.getElementById('siraKutusu');
            if (siraKutusu) { siraKutusu.style.background = '#fff'; siraKutusu.style.borderColor = '#ddd'; }

            const canliSure = document.getElementById('canliSure');
            if (canliSure) { canliSure.innerText = "Araçtasınız"; canliSure.style.fontSize = "26px"; canliSure.style.color = "#000"; }

            const altBilgi = document.getElementById('siraAltBilgi');
            if (altBilgi) { altBilgi.innerHTML = "İyi yolculuklar dileriz!"; altBilgi.style.color = "#34c759"; }
            return;
        }

        const rotaAktif = veri.rota_aktif === true;

        // The vehicle has not started its trip yet.
        if (!rotaAktif && veri.durum === 'KONUM ALINDI') {
            // A drop-off passenger waits at base, so show the departure time on the drop-off
            // screen itself (#canliSure belongs to the pickup screen, which is hidden here).
            if (aktifGorevTipi.startsWith('VALE_')) {
                const valeAlimMi = aktifGorevTipi === 'VALE_ALIM';
                document.getElementById('gorevEtiketi').innerText = valeAlimMi ? "VALE: ARAÇ ALIM" : "VALE: ARAÇ TESLİM";
                document.getElementById('gorevEtiketi').style.background = "#e5f0ff";
                document.getElementById('gorevEtiketi').style.color = "var(--blue)";
                // Mention the plate so the customer knows which car this is about.
                const plakaOneki = veri.musteri_plaka ? `${veri.musteri_plaka} plakalı aracınızın` : "Aracınızın";
                document.getElementById('gorevAciklamasi').innerText = plakaOneki + (valeAlimMi
                    ? " alınacağı tam konumu haritadan işaretleyin."
                    : " teslim edileceği konumu haritadan işaretleyin.");
            } else if (aktifGorevTipi === 'DAGITIM') {
                const saatKutu = document.getElementById('dagitimSaatKutu');
                const saatDeger = document.getElementById('dagitimSaatDeger');
                const beklemeKutusu = document.getElementById('beklemeKutusu');
                if (veri.hareket_saati) {
                    if (saatDeger) saatDeger.innerText = veri.hareket_saati;
                    if (saatKutu) saatKutu.style.display = 'block';
                    if (beklemeKutusu) beklemeKutusu.innerHTML = "Servis aracınız belirtilen saatte kalkış yapacaktır. Lütfen hareket saatinden önce bekleme alanında hazır olunuz.";
                } else {
                    if (saatKutu) saatKutu.style.display = 'none';
                }
                return;
            }

            const canliSure = document.getElementById('canliSure');
            const altBilgi = document.getElementById('siraAltBilgi');
            const siraBaslik = document.getElementById('siraBaslik');
            // No trip yet, so an "estimated arrival" heading would mean nothing.
            if (siraBaslik) siraBaslik.style.display = 'none';

            if (veri.hareket_saati) {
                // A departure time is set: show it.
                if (canliSure) {
                    canliSure.innerHTML = `
                                <div class="hareket-saati-baslik">Hareket Saati</div>
                                <div class="hareket-saati-deger">${veri.hareket_saati}</div>`;
                }
                if (altBilgi) altBilgi.innerHTML = "Araç belirlenen saatte kalkış yapacaktır.<br>Tahmini geliş süresi araç hareket ettikten sonra güncellenecektir.";
            } else {
                // No departure time yet.
                if (canliSure) {
                    canliSure.innerText = "Hazırlanıyor...";
                    canliSure.style.fontSize = "20px";
                }
                if (altBilgi) altBilgi.innerHTML = "Araç hazırlık aşamasında.";
            }
            return;
        }

        if (aktifGorevTipi === 'TOPLAMA' && veri.durum === 'KONUM ALINDI') {
            const siraCevap = await fetch(`${CONFIG.BASE_URL}/canli-sira/${token}?t=${zamanDamgasi}`);
            const siraVeri = await siraCevap.json();

            const siraKutusu = document.getElementById('siraKutusu');
            const sureMetni = document.getElementById('canliSure');
            const altBilgi = document.getElementById('siraAltBilgi');
            const siraBaslik = document.getElementById('siraBaslik');
            // The trip has started: show the ETA heading again.
            if (siraBaslik) { siraBaslik.style.display = 'block'; siraBaslik.innerText = 'Tahmini Varış Saati'; }

            if (siraVeri.sira === 1) {
                siraKutusu.style.background = '#d4edda';
                siraKutusu.style.borderColor = '#c3e6cb';
                sureMetni.style.color = '#155724';
                altBilgi.style.color = '#155724';
                if (siraVeri.tahmini_dakika !== undefined) {
                    sureMetni.innerHTML = `<span class="canli-sure-buyuk">${varisSaatiHesapla(siraVeri.tahmini_dakika)}</span>`;
                    if (siraVeri.sistem_modu === "DURAK") {
                        altBilgi.innerHTML = "SIRADAKİ DURAK SİZİN — Lütfen durakta bekleyiniz";
                    } else {
                        altBilgi.innerHTML = "SIRADAKİ SİZSİNİZ — Hazır olunuz";
                    }
                } else {
                    sureMetni.innerHTML = siraVeri.sistem_modu === "DURAK" ? "SIRADAKİ DURAK!" : "SIRADAKİ SİZSİNİZ!";
                    altBilgi.innerHTML = "";
                }
            } else if (siraVeri.sira > 1) {
                siraKutusu.style.background = '#f0f8ff';
                siraKutusu.style.borderColor = 'var(--blue)';
                sureMetni.style.color = 'var(--blue)';
                altBilgi.style.color = '#666';
                if (siraVeri.tahmini_dakika !== undefined) {
                    sureMetni.innerHTML = `<span class="canli-sure-cok-buyuk">${varisSaatiHesapla(siraVeri.tahmini_dakika)}</span>`;
                    if (siraVeri.sistem_modu === "DURAK") {
                        // Show how many stops are ahead of the customer, not the route's total.
                        altBilgi.innerHTML = `${siraVeri.sira}. Sıradasınız (Önünüzde ${siraVeri.sira - 1} Durak Var)`;
                    } else {
                        altBilgi.innerHTML = `${siraVeri.sira}. Sıradasınız (Önünüzde ${siraVeri.sira - 1} Kişi Var)`;
                    }
                } else {
                    sureMetni.innerHTML = `${siraVeri.sira}. Sıradasınız`;
                    altBilgi.innerHTML = "";
                }
            } else {
                sureMetni.style.fontSize = "20px";
                sureMetni.innerHTML = "Araç Bekleniyor";
                altBilgi.innerHTML = "";
            }
        }
    } catch (e) { }
}

function beklemeKutusunuYesilYap() {
    const kutu = document.getElementById('beklemeKutusu');
    kutu.style.background = '#d4edda'; kutu.style.color = '#155724'; kutu.style.borderColor = '#c3e6cb';
    kutu.innerHTML = '<b>SERVİSİNİZ HAZIR!</b><br><br>Aracınız biniş için perona yanaşmıştır. Lütfen aracınıza geçiniz.';
}

function yolculukBittiEkrani() {
    const siraKutusu = document.getElementById('siraKutusu');
    if (siraKutusu) { siraKutusu.style.background = '#d4edda'; siraKutusu.style.borderColor = '#c3e6cb'; }
    const sureMetni = document.getElementById('canliSure');
    if (sureMetni) { sureMetni.innerText = "Yolculuk Bitti"; sureMetni.style.fontSize = "24px"; sureMetni.style.color = '#155724'; }
    const altBilgi = document.getElementById('siraAltBilgi');
    if (altBilgi) altBilgi.innerHTML = "";
}

// WebSocket: refresh on "YENILE", at most once every 2 seconds. The customer's request token
// (from the URL) authenticates the connection.
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
        if (event.data === "YENILE" && !guncellemeKilit) {
            guncellemeKilit = true;
            if (typeof durumuCanliKontrolEt === "function") durumuCanliKontrolEt();
            setTimeout(() => { guncellemeKilit = false; }, 2000);
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
            if (typeof durumuCanliKontrolEt === "function") durumuCanliKontrolEt();
        }
    }
});

// Back online: reconnect and fetch the state that may have changed meanwhile.
let _onceOfflineOlduMu = false;

window.addEventListener('offline', () => {
    _onceOfflineOlduMu = true;
});

window.addEventListener('online', () => {
    if (_onceOfflineOlduMu) {
        _onceOfflineOlduMu = false;
        if (!ws || ws.readyState !== WebSocket.OPEN) {
            canliBaglantiKur();
        }
        if (typeof durumuCanliKontrolEt === "function") {
            setTimeout(durumuCanliKontrolEt, 500);  // let the WebSocket connect first
        }
    }
});

canliBaglantiKur();

// ============================================================
// Event listeners
// ============================================================
document.addEventListener('DOMContentLoaded', () => {
    baslat();

    const durakSecici = document.getElementById('durakSecici');
    if (durakSecici) {
        durakSecici.addEventListener('change', function () {
            durakSecildi(this);
        });
    }

    const onayBtn = document.getElementById('onayBtn');
    if (onayBtn) onayBtn.addEventListener('click', konumuGonder);

    // KVKK screens.
    // The primary button stays disabled until the box is ticked, so consent is never given by accident.
    const onayKutusu = document.getElementById('kvkkOnayKutusu');
    const devamBtn = document.getElementById('kvkkDevamBtn');
    if (onayKutusu && devamBtn) {
        onayKutusu.addEventListener('change', () => { devamBtn.disabled = !onayKutusu.checked; });
    }
    // Tapping anywhere on the row toggles the box (the box itself is a small target on phones).
    // Clicks on the input or its <label for> are skipped: the label already toggles the input,
    // and handling it here too would toggle twice and look as if nothing happened.
    const onaySatir = document.getElementById('kvkkOnaySatir');
    if (onaySatir && onayKutusu) {
        onaySatir.addEventListener('click', (e) => {
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'LABEL' || e.target.closest('label')) return;
            onayKutusu.checked = !onayKutusu.checked;
            onayKutusu.dispatchEvent(new Event('change'));
        });
    }
    if (devamBtn) devamBtn.addEventListener('click', kvkkOnayAlindi);

    // "Continue without consent" first goes to an intermediate screen with a one-tap way back.
    const onaysizBtn = document.getElementById('kvkkOnaysizBtn');
    if (onaysizBtn) onaysizBtn.addEventListener('click', () => ekranDegistir('kvkkOnaysizEkran'));

    const geriDonBtn = document.getElementById('kvkkGeriDonBtn');
    if (geriDonBtn) geriDonBtn.addEventListener('click', () => ekranDegistir('kvkkEkran'));

    const onaysizDevamBtn = document.getElementById('kvkkOnaysizDevamBtn');
    if (onaysizDevamBtn) onaysizDevamBtn.addEventListener('click', kvkkRedGonder);

    // "I changed my mind": a refusal is not final and the link is still alive, so go back to the KVKK screen.
    const fikirBtn = document.getElementById('kvkkFikrimiDegistirdimBtn');
    if (fikirBtn) {
        fikirBtn.addEventListener('click', () => {
            if (onayKutusu) onayKutusu.checked = false;
            if (devamBtn) devamBtn.disabled = true;
            ekranDegistir('kvkkEkran');
        });
    }

    // "Withdraw my consent" links on the tracking screens (same class on all three).
    document.querySelectorAll('.kvkk-geri-cek-btn').forEach(b => b.addEventListener('click', kvkkGeriCek));
});
function valeTakipGuncelle(veri) {
    const baslik = document.getElementById('valeDurumBaslik');
    const aciklama = document.getElementById('valeDurumAciklama');
    const etaKutu = document.getElementById('valeEtaKutu');
    const etaSure = document.getElementById('valeEtaSure');
    const plakaKutu = document.getElementById('valePlakaKutu');
    
    if (plakaKutu && veri && veri.musteri_plaka) {
        plakaKutu.style.display = "block";
        plakaKutu.innerText = veri.musteri_plaka;
    }
    
    // The order depends on the task type and follows VALE_GECISLER on the server:
    //   pickup:   location set -> VALE_YOLDA (ETA) -> car taken -> at the service center
    //   delivery: location set -> car out of the service center -> VALE_YOLDA (ETA) -> handed over
    // The ETA is shown only in VALE_YOLDA, which in both types means "the valet is on the way to you".
    const alim = aktifGorevTipi === 'VALE_ALIM';
    if (veri.durum === 'KONUM_ALINDI_VALE') {
        baslik.innerText = alim ? "Vale Bekleniyor" : "Teslimat Hazırlanıyor";
        aciklama.innerText = alim
            ? "Konumunuz alındı. Vale ataması tamamlanıp yola çıkıldığında burada tahmini varış süresini göreceksiniz."
            : "Konumunuz alındı. Aracınız teslimat için hazırlanıyor.";
        if(etaKutu) etaKutu.style.display = "none";
    } else if (veri.durum === 'VALE_YOLDA') {
        baslik.innerText = "Vale Yolda";
        aciklama.innerText = alim
            ? "Vale aracınızı teslim almak için yola çıkmıştır."
            : "Aracınız yola çıktı, vale size doğru geliyor.";
        if(etaKutu) etaKutu.style.display = "block";
        // Show a clock time, never "X minutes left". This page has no periodic refresh (no
        // setInterval; it only refreshes on WebSocket "YENILE" and when the tab becomes visible),
        // so a countdown would silently go wrong while the page stays open. A time stays correct.
        // Do not add a "minutes left" text here; there is nothing to keep it counting down.
        // The server sends the finished time (vale_varis_saati), anchored to when the valet's
        // position was written, so it does not shift on refresh but does update when the valet
        // moves. Without a time (no position yet), show a neutral text.
        const varisSaati = saatBicimle(veri && veri.vale_varis_saati);
        if (etaSure) etaSure.innerText = varisSaati || "Yaklaşıyor...";
    } else if (veri.durum === 'ARAC_ALINDI') {
        baslik.innerText = alim ? "Araç Alındı" : "Araç Servisten Çıktı";
        aciklama.innerText = alim
            ? "Aracınız vale tarafından teslim alınmıştır ve servise götürülmektedir."
            : "Aracınız servisten çıkış yapmıştır. Vale yola çıktığında tahmini varış süresi burada görünecek.";
        if(etaKutu) etaKutu.style.display = "none";
    } else if (veri.durum === 'TAMAM_SERVIS' || veri.durum === 'TAMAM_MUSTERI') {
        valeBasariEkrani(veri.durum, veri);
    }
}

function valeBasariEkrani(durum, veri) {
    const baslik = document.getElementById('valeDurumBaslik');
    const aciklama = document.getElementById('valeDurumAciklama');
    const etaKutu = document.getElementById('valeEtaKutu');
    if(etaKutu) etaKutu.style.display = "none";
    
    if (durum === 'TAMAM_SERVIS') {
        baslik.innerText = "Araç Serviste";
        aciklama.innerText = "Aracınız güvenle servise ulaşmıştır.";
    } else {
        baslik.innerText = "Teslimat Tamamlandı";
        aciklama.innerText = "Aracınız tarafınıza başarıyla teslim edilmiştir.";
    }

    // Draw the survey if the server says the window is open. The server checks the time
    // window, the task type and whether a survey was already sent; the page only reads
    // anket_gosterilsin.
    if (veri && veri.anket_gosterilsin) anketiCiz(veri);
}

// ============================================================
// Satisfaction survey (valet deliveries)
// ============================================================
// The questions are defined once here and the rows are generated from this list. The field
// names and their order must match ANKET_SORU_ETIKETLERI in main.py and the body of /anket-gonder.
const ANKET_SORULARI = [
    { alan: 'puan_genel',         metin: 'Genel memnuniyetiniz',                zorunlu: true },
    { alan: 'puan_dakiklik',      metin: 'Söz verilen saate uyuldu mu' },
    { alan: 'puan_ilgi',          metin: 'Personelin ilgisi ve nezaketi' },
    { alan: 'puan_arac_durumu',   metin: 'Aracınızın teslim edildiği durum' },
    { alan: 'puan_bilgilendirme', metin: 'Süreç boyunca bilgilendirme' }
];
const YILDIZ_SVG = '<svg width="26" height="26" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
    + '<path d="M12 2.6l2.9 5.9 6.5.95-4.7 4.6 1.1 6.45L12 17.45 6.2 20.5l1.1-6.45-4.7-4.6 6.5-.95z"/></svg>';

let anketPuanlari = {};
let anketCizildi = false;
let anketGonderildi = false;   // stops live polling after submission (see durumuCanliKontrolEt)

function anketiCiz(veri) {
    // Draw only once. valeTakipGuncelle runs again on every WebSocket "YENILE", and redrawing
    // would wipe the customer's stars and comment while they are filling them in.
    if (anketCizildi || anketGonderildi) return;
    anketCizildi = true;

    const kutu = document.getElementById('anketKutu');
    const alan = document.getElementById('anketSorular');
    if (!kutu || !alan) return;

    // Hide the plate box during the survey (the server does not send the plate in survey mode anyway).
    const plakaKutu = document.getElementById('valePlakaKutu');
    if (plakaKutu) plakaKutu.style.display = "none";

    alan.innerHTML = ANKET_SORULARI.map((soru, si) => {
        const yildizlar = [1,2,3,4,5].map(p =>
            `<button type="button" class="anket-yildiz" data-alan="${soru.alan}" data-puan="${p}"
                     aria-label="${p} yıldız">${YILDIZ_SVG}</button>`).join('');
        return `<div class="anket-satir">
                    <div class="anket-soru">${si + 1}. ${soru.metin}${soru.zorunlu ? '' : ' (isteğe bağlı)'}</div>
                    <div class="anket-yildizlar" role="group">${yildizlar}</div>
                </div>`;
    }).join('');

    // One delegated listener for all stars.
    alan.addEventListener('click', (e) => {
        const btn = e.target.closest('.anket-yildiz');
        if (!btn) return;
        const secilenAlan = btn.dataset.alan;
        const puan = parseInt(btn.dataset.puan, 10);
        // Tapping the same star again clears the answer, so an optional question tapped by
        // mistake can be left empty again.
        anketPuanlari[secilenAlan] = (anketPuanlari[secilenAlan] === puan) ? undefined : puan;
        anketYildizlariBoya();
        anketButonuGuncelle();
    });

    const yorum = document.getElementById('anketYorum');
    const sayac = document.getElementById('anketSayac');
    const limit = document.getElementById('anketLimit');
    const sunucuLimiti = (veri && veri.anket_yorum_limiti) || 500;
    if (yorum) {
        yorum.setAttribute('maxlength', String(sunucuLimiti));
        yorum.addEventListener('input', () => { if (sayac) sayac.innerText = String(yorum.value.length); });
    }
    if (limit) limit.innerText = String(sunucuLimiti);

    const btn = document.getElementById('anketGonderBtn');
    if (btn) btn.addEventListener('click', anketGonder);
    // The button state follows the data (anketPuanlari.puan_genel), not the disabled attribute
    // in the HTML.
    anketButonuGuncelle();

    kutu.style.display = "block";
}

function anketYildizlariBoya() {
    document.querySelectorAll('.anket-yildiz').forEach(b => {
        const secili = anketPuanlari[b.dataset.alan];
        // Fill every star up to the chosen rating.
        b.classList.toggle('dolu', !!secili && parseInt(b.dataset.puan, 10) <= secili);
    });
}

function anketButonuGuncelle() {
    const btn = document.getElementById('anketGonderBtn');
    if (btn) btn.disabled = !anketPuanlari.puan_genel;   // only the first question is required
}

async function anketGonder() {
    const btn = document.getElementById('anketGonderBtn');
    const hata = document.getElementById('anketHata');
    const yorum = document.getElementById('anketYorum');
    if (!anketPuanlari.puan_genel) return;

    if (btn) { btn.disabled = true; btn.innerText = "Gönderiliyor..."; }
    if (hata) hata.style.display = "none";

    const govde = { token: token, yorum: yorum ? yorum.value.trim() : "" };
    ANKET_SORULARI.forEach(s => { if (anketPuanlari[s.alan]) govde[s.alan] = anketPuanlari[s.alan]; });

    try {
        const cevap = await fetch(`${CONFIG.BASE_URL}/anket-gonder`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(govde)
        });
        // 409 means it was already submitted (UNIQUE in the database). That is not an error for
        // the customer: show the thank-you screen.
        if (cevap.ok || cevap.status === 409) { anketTesekkurEkrani(); return; }

        let mesaj = "Değerlendirmeniz gönderilemedi. Lütfen tekrar deneyin.";
        if (cevap.status === 410) mesaj = "Değerlendirme süresi dolmuş.";
        else if (cevap.status === 429) mesaj = "Çok fazla deneme yapıldı. Lütfen biraz bekleyin.";
        else {
            try { const j = await cevap.json(); if (j && j.detail) mesaj = j.detail; } catch (e) { }
        }
        if (hata) { hata.innerText = mesaj; hata.style.display = "block"; }
        if (btn) { btn.disabled = false; btn.innerText = "Değerlendirmeyi Gönder"; }
    } catch (e) {
        if (hata) { hata.innerText = "Bağlantı kurulamadı. Lütfen tekrar deneyin."; hata.style.display = "block"; }
        if (btn) { btn.disabled = false; btn.innerText = "Değerlendirmeyi Gönder"; }
    }
}

function anketTesekkurEkrani() {
    anketGonderildi = true;
    const kutu = document.getElementById('anketKutu');
    const tesekkur = document.getElementById('anketTesekkur');
    if (kutu) kutu.style.display = "none";
    if (tesekkur) tesekkur.style.display = "block";
    // The server destroys the token after a submission, so later polls would get 404 and turn
    // this screen into an error. anketGonderildi stops that (checked at the start of
    // durumuCanliKontrolEt).
}
