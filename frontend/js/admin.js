// admin.html: the company admin's panel. Base location and stop maps (Mapbox), staff, vehicles,
// routes and stops, brands/departments, branches, and the reports (vehicle, punctuality,
// satisfaction). Handlers are attached here because the CSP forbids inline scripts.

// ============================================================
// Every assignment to innerHTML on this page is passed through DOMPurify.
// ============================================================
(function () {
    const orijinalSetter = Object.getOwnPropertyDescriptor(Element.prototype, 'innerHTML').set;
    Object.defineProperty(Element.prototype, 'innerHTML', {
        set: function (html) {
            const temiz = (typeof DOMPurify !== 'undefined' && typeof html === 'string')
                ? DOMPurify.sanitize(html, {
                    ADD_ATTR: ['target', 'rel', 'value', 'placeholder', 'title', 'data-aksiyon', 'data-arac-id', 'data-kadi', 'data-ad', 'data-guzergah-id', 'data-durak-id', 'data-grup-id', 'data-sayfa-id', 'data-sube-id', 'data-kota', 'data-marka-id', 'data-marka-ad'],
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
// appModal: a promise-based replacement for prompt() with validation.
// Resolves to the entered text, or null when cancelled.
// ============================================================
window.appModal = function (ayarlar) {
    return new Promise((resolve) => {
        const overlay = document.getElementById('app-modal-overlay');
        const baslikEl = document.getElementById('pxm-baslik');
        const aciklamaEl = document.getElementById('pxm-aciklama');
        const inputEl = document.getElementById('pxm-input');
        const textareaEl = document.getElementById('pxm-textarea');
        const hataEl = document.getElementById('pxm-hata');
        const onayBtn = document.getElementById('pxm-onay');
        const iptalBtn = document.getElementById('pxm-iptal');

        baslikEl.textContent = ayarlar.baslik || 'Bilgi';
        aciklamaEl.textContent = ayarlar.aciklama || '';
        aciklamaEl.style.display = ayarlar.aciklama ? 'block' : 'none';
        hataEl.textContent = '';
        hataEl.classList.remove('gosterilsin');
        onayBtn.textContent = ayarlar.onayButon || 'Tamam';
        iptalBtn.textContent = ayarlar.iptalButon || 'Vazgeç';

        const tip = ayarlar.inputTipi || 'text';
        const isTextarea = tip === 'textarea';
        inputEl.style.display = isTextarea ? 'none' : 'block';
        textareaEl.style.display = isTextarea ? 'block' : 'none';

        const aktifEl = isTextarea ? textareaEl : inputEl;
        if (!isTextarea) {
            inputEl.type = (tip === 'password') ? 'password' : (tip === 'number' ? 'number' : 'text');
        }
        aktifEl.value = ayarlar.varsayilan || '';
        aktifEl.placeholder = ayarlar.placeholder || '';

        overlay.classList.add('acik');
        setTimeout(() => aktifEl.focus(), 100);

        function kapat(sonuc) {
            overlay.classList.remove('acik');
            onayBtn.onclick = null;
            iptalBtn.onclick = null;
            overlay.onclick = null;
            document.removeEventListener('keydown', klavyeDinleyici);
            resolve(sonuc);
        }

        function onaya_tikla() {
            const deger = aktifEl.value.trim();
            if (ayarlar.validate) {
                const hata = ayarlar.validate(deger);
                if (hata) {
                    hataEl.textContent = hata;
                    hataEl.classList.add('gosterilsin');
                    aktifEl.focus();
                    return;
                }
            }
            kapat(deger);
        }

        function klavyeDinleyici(e) {
            if (e.key === 'Escape') kapat(null);
            if (e.key === 'Enter' && !isTextarea) onaya_tikla();
        }

        onayBtn.onclick = onaya_tikla;
        iptalBtn.onclick = () => kapat(null);
        overlay.onclick = (e) => { if (e.target === overlay) kapat(null); };
        document.addEventListener('keydown', klavyeDinleyici);
    });
};

// ============================================================
// Panel
// ============================================================
// Mapbox public token. Create one at https://account.mapbox.com/access-tokens/
// and restrict it to your own domain so it cannot be used from other sites.
mapboxgl.accessToken = 'YOUR_MAPBOX_PUBLIC_TOKEN';

const firmaId = localStorage.getItem('aktif_firma_id');
const aktifRol = localStorage.getItem('aktif_rol');
const ekleyenKisi = localStorage.getItem('aktif_kullanici_adi');
const aktifSubeId = localStorage.getItem('aktif_sube_id') || '';  // empty = headquarters admin, or a company without branches
let firmaSubeli = false;  // set from the sube-listele response
let firmaToplamKota = null;  // company vehicle quota (shown when there are no branches)
let subeAktifKota = null;    // a branch admin's own branch quota
let sonAracSayisi = 0;       // vehicle count from the last fetch (for the quota display)

function getAuthHeaders() {
    const token = localStorage.getItem('app_token');
    return {
        "Authorization": `Bearer ${token}`,
        "Content-Type": "application/json"
    };
}

let merkezMap, durakMap;
let merkezMarker = null;
let yolcuVeAracMarkers = [];
let durakMarkers = [];
let firmaMerkezLat = null, firmaMerkezLng = null;
let firmaSistemModu = "HARITA";
let firmaShuttleAktif = true;  // false for a valet-only company: shuttle UI is hidden

// A valet's virtual vehicle stores the valet's username in its "plaka" field. Older records
// have a "Vale " prefix, which is stripped (same as danisman.js and operasyon.js).
// Date and time for display, always in Istanbul time (the server stores UTC). Copied in
// danisman.js, operasyon.js and vale.js; keep the copies identical.
function saatTR(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '—';
    return d.toLocaleString('tr-TR', {
        timeZone: 'Europe/Istanbul',
        day: '2-digit', month: '2-digit', year: 'numeric',
        hour: '2-digit', minute: '2-digit'
    }).replace(',', '');
}

// Time only, for the map popups (the date is today anyway).
function yalnizSaatTR(iso) {
    if (!iso) return null;
    const d = new Date(iso);
    if (isNaN(d.getTime())) return null;
    return d.toLocaleTimeString('tr-TR', { timeZone: 'Europe/Istanbul', hour: '2-digit', minute: '2-digit' });
}

function valeAdi(plaka) {
    return (plaka || '').replace(/^Vale\s+/i, '') || 'Vale';
}

// Username helper. The same code is in superadmin.js; keep the two identical.
// Usernames are ASCII only on the server (_KULLANICI_ADI_REGEX) and must stay that way: the
// username is the de facto primary key (queries, token revocation, Redis keys) and uniqueness
// is checked in lowercase, where Turkish case folding would create silent collisions. Rather
// than rejecting input, convert it while the user types: Turkish letters become their ASCII
// counterparts and spaces become underscores.
const TR_ASCII_HARITA = { 'ı':'i','İ':'I','ğ':'g','Ğ':'G','ü':'u','Ü':'U','ş':'s','Ş':'S','ö':'o','Ö':'O','ç':'c','Ç':'C' };
function kullaniciAdiTemizle(deger) {
    return (deger || '')
        .replace(/[ıİğĞüÜşŞöÖçÇ]/g, (h) => TR_ASCII_HARITA[h] || h)
        .replace(/\s+/g, '_')                 // spaces become underscores, so "Name Surname" works
        .replace(/[^a-zA-Z0-9_.\-]/g, '');    // drop anything else that is not allowed
}
// Clean a field as the user types, keeping the cursor in place (so editing in the middle
// does not jump to the end).
function kullaniciAdiAlaniniBagla(elemanId) {
    const alan = document.getElementById(elemanId);
    if (!alan) return;
    alan.addEventListener('input', () => {
        const eski = alan.value;
        const yeni = kullaniciAdiTemizle(eski);
        if (yeni === eski) return;
        const poz = Math.max(0, (alan.selectionStart || 0) - (eski.length - yeni.length));
        alan.value = yeni;
        alan.setSelectionRange(poz, poz);
    });
}

let guzergahlar = [];
let duraklar = [];
let seciliGuzergahId = null;
let aktifDavetBilgileri = {};
let geciciMarker = null;
let geciciKonum = null;

function sayfaDegistir(hedefId, element) {
    document.querySelectorAll('.sayfa-gorunumu').forEach(s => s.classList.remove('active'));
    document.getElementById(hedefId).classList.add('active');
    document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
    element.classList.add('active');

    if (hedefId === 'ekip-yonetimi') {
        personelleriGetir(); markalariGetir(); subeleriGetir(); filoyuGetir();
    }
    if (hedefId === 'raporlar') raporlariGetir();
    if (hedefId === 'memnuniyet') memnuniyetGetir();
}

async function cikisYap(sebep = null) {
    await appCikis();
    if (sebep) {
        window.location.href = `/login.html?sebep=${encodeURIComponent(sebep)}`;
    } else {
        window.location.href = "/login.html";
    }
}

// Edit a person's display name. The username (identity) does not change, so sessions, vehicle
// assignment and password are unaffected; only the name shown on screens changes.
async function gorunenAdDuzenle(kullaniciAdi, mevcutAd) {
    const yeniAd = await appModal({
        baslik: `${mevcutAd || kullaniciAdi} — görünen ad`,
        aciklama: "Panellerde bu isim görünür (örn. Ayşe Yılmaz). Giriş kimliği değişmez.",
        inputTipi: "text",
        placeholder: "Adı Soyadı",
        varsayilan: mevcutAd || "",
        onayButon: "Kaydet",
        validate: (deger) => {
            const d = (deger || '').trim();
            if (d.length < 2) return "Ad en az 2 karakter olmalı.";
            if (d.length > 60) return "Ad en fazla 60 karakter olabilir.";
            return null;
        }
    });
    if (!yeniAd || !yeniAd.trim()) return;

    try {
        const res = await fetch(`${CONFIG.BASE_URL}/gorunen-ad-guncelle`, {
            method: "PUT",
            headers: getAuthHeaders(),
            body: JSON.stringify({ kullanici_adi: kullaniciAdi, gorunen_ad: yeniAd.trim() })
        });
        if (res.status === 401) { cikisYap(); return; }
        const data = await res.json().catch(() => ({}));
        if (!res.ok) { alert("Güncellenemedi: " + (data.detail || "")); return; }
        personelleriGetir();   // refresh the staff list
        filoyuGetir();         // and the vehicle cards, which show the name too
    } catch (e) { alert("Sunucuya bağlanılamadı."); }
}

async function sifreSifirlaPrompt(kullaniciAdi) {
    const yeniSifre = await appModal({
        baslik: `${kullaniciAdi} için yeni şifre`,
        aciklama: "En az 8 karakter, harf ve rakam içermeli.",
        inputTipi: "password",
        placeholder: "Yeni şifre",
        onayButon: "Belirle",
        validate: (deger) => {
            if (!deger || deger.length < 8) return "Şifre en az 8 karakter olmalı.";
            // bcrypt accepts at most 72 bytes, and Turkish letters take two bytes each in UTF-8.
            if (new TextEncoder().encode(deger).length > 72) return "Şifre çok uzun (Türkçe karakterler iki kez sayılır).";
            if (!/[a-zA-Z]/.test(deger)) return "Şifre en az 1 harf içermeli.";
            if (!/[0-9]/.test(deger)) return "Şifre en az 1 rakam içermeli.";
            return null;
        }
    });
    if (!yeniSifre || yeniSifre.trim() === "") return;

    const sifre = yeniSifre.trim();

    if (sifre.length < 8) {
        alert("Şifre minimum 8 karakter olmalı!");
        return;
    }
    if (!/[A-Za-z]/.test(sifre) || !/\d/.test(sifre)) {
        alert("Şifre en az 1 harf ve 1 rakam içermeli!");
        return;
    }

    try {
        const res = await fetch(`${CONFIG.BASE_URL}/personel-sifre-sifirla`, {
            method: "PUT",
            headers: getAuthHeaders(),
            body: JSON.stringify({ kullanici_adi: kullaniciAdi, yeni_sifre: sifre })
        });
        if (res.ok) {
            alert(`${kullaniciAdi} şifresi başarıyla güncellendi!`);
        } else if (res.status === 401) {
            cikisYap();
        } else {
            const hata = await res.json().catch(() => ({ detail: "Bilinmeyen hata" }));
            alert("Hata: " + (hata.detail || "İşlem başarısız"));
        }
    } catch (e) {
        alert("Bağlantı hatası: Sunucuya ulaşılamadı.");
    }
}

function rolDegisti() {
    const rol = document.getElementById('p_rol').value;
    // Phone is required for field roles (driver and valet): the operations panel calls them from
    // their card.
    document.getElementById('p_tel_container').style.display = (rol === 'SOFOR' || rol === 'VALE') ? 'block' : 'none';
    // The branch selector is shown only to a headquarters admin creating an ADMIN (that is, a
    // branch manager). Otherwise it is hidden and the server picks the branch: HQ for an HQ admin,
    // their own branch for a branch admin.
    const pSubeEl = document.getElementById('pSube');
    if (pSubeEl) pSubeEl.classList.toggle('gizli', !(firmaSubeli && !aktifSubeId && rol === 'ADMIN'));
}

/**
 * Show or hide the shuttle and valet parts of the UI according to the module flags.
 * Called twice: first at start-up from the flags stored at login, so a valet-only company
 * never sees shuttle sections flash on screen; then with the values from firma-detay, which
 * correct a stale cache. <option> elements are only removed when kesinlesti is true, because
 * removing them cannot be undone and a stale cache would otherwise do lasting damage.
 */
function modulUIUygula(shuttleAktif, valeAktif, kesinlesti = false) {
    const goster = (id, acik) => { const el = document.getElementById(id); if (el) el.style.display = acik ? '' : 'none'; };

    ['aracEkleKart', 'filoDurumKart', 'aracRaporTablosu'].forEach(id => goster(id, shuttleAktif));
    if (!shuttleAktif) goster('durakYonetimAlani', false);  // when shuttle is on, the DURAK mode logic controls this
    goster('valeRaporBaslik', valeAktif && shuttleAktif);   // without shuttle the page title already says valet
    goster('valeRaporTablosu', valeAktif);
    // Punctuality exists only for the valet module: milestones (gorev_etaplari) are recorded for
    // valet tasks only, so a shuttle-only company never sees this block.
    ['dakiklikBaslik', 'dakiklikAciklama', 'dakiklikTablosu'].forEach(id => goster(id, valeAktif));

    const raporBaslik = document.querySelector('#raporlar .header-area h1');
    if (raporBaslik) raporBaslik.innerText = shuttleAktif ? 'Araç Bazlı Performans Raporları' : 'Vale Bazlı Performans Raporları';

    if (kesinlesti) {
        if (!valeAktif) { const o = document.querySelector('#p_rol option[value="VALE"]'); if (o) o.remove(); }
        if (!shuttleAktif) { const o = document.querySelector('#p_rol option[value="SOFOR"]'); if (o) o.remove(); }
    }
}

async function sistemiBaslat() {
    try {
        const detayRes = await fetch(`${CONFIG.BASE_URL}/firma-detay/${firmaId}`, { headers: getAuthHeaders() });
        if (detayRes.status === 401) { cikisYap(); return; }
        // 402: company inactive or deleted. Log out with that reason, or the panel would stay empty.
        if (detayRes.status === 402) { cikisYap('firma_pasif'); return; }
        const detay = await detayRes.json();
        document.getElementById('firmaAdiSidebar').innerText = detay.firma_adi;
        firmaSistemModu = detay.sistem_modu || "HARITA";

        // Apply the module flags again; the server's answer is authoritative (the cached values were
        // applied at start-up).
        firmaShuttleAktif = detay.shuttle_aktif !== false;
        modulUIUygula(firmaShuttleAktif, !!detay.vale_aktif, true);

        if (detay.merkez_lat) {
            firmaMerkezLat = detay.merkez_lat;
            firmaMerkezLng = detay.merkez_lng;
        } else {
            document.getElementById('konumUyari').style.display = 'block';
            document.getElementById('konumUyari').innerText = 'Firma merkez konumu henüz ayarlanmamış. Lütfen ayarlayın.';
        }

        // A branch admin manages the branch's location instead of the company headquarters.
        if (aktifSubeId) {
            firmaMerkezLat = null; firmaMerkezLng = null;
            try {
                const sres = await fetch(`${CONFIG.BASE_URL}/sube-listele/${firmaId}`, { headers: getAuthHeaders() });
                if (sres.ok) {
                    const sv = await sres.json();
                    const benim = (sv.subeler || []).find(s => s.id === aktifSubeId);
                    if (benim && benim.konum_lat != null) { firmaMerkezLat = benim.konum_lat; firmaMerkezLng = benim.konum_lng; }
                    // Branch admin: show company and branch in the sidebar and the header badge.
                    if (benim && benim.sube_adi) {
                        const sb = document.getElementById('firmaAdiSidebar');
                        if (sb) sb.textContent = `${detay.firma_adi} · ${benim.sube_adi}`;
                        const rozet = document.getElementById('firmaSubeRozet');
                        if (rozet) { rozet.textContent = `🏢 ${detay.firma_adi} · ${benim.sube_adi}`; rozet.style.display = 'inline-block'; }
                    }
                }
            } catch (e) { }
            const bEl = document.getElementById('konumBaslik');
            if (bEl) bEl.textContent = 'ŞUBE KONUMU';
            const aEl = document.getElementById('konumAciklama');
            if (aEl) aEl.textContent = 'Haritaya tıklayarak şubenizin konumunu işaretleyin. Bu konum, şubenizin araçlarının rotayı tamamlayıp geri döneceği noktadır.';
            const uEl = document.getElementById('konumUyari');
            if (uEl) {
                if (firmaMerkezLat == null) { uEl.style.display = 'block'; uEl.innerText = 'Şube konumu henüz ayarlanmamış. Lütfen ayarlayın.'; }
                else { uEl.style.display = 'none'; uEl.innerText = ''; }
            }
        }
    } catch (e) { }

    const lat = firmaMerkezLat || 38.4189, lng = firmaMerkezLng || 27.1287;

    // Base location map.
    merkezMap = new mapboxgl.Map({
        container: 'merkez-map',
        style: 'mapbox://styles/mapbox/streets-v12',
        center: [lng, lat],
        zoom: firmaMerkezLat ? 14 : 7
    });
    merkezMap.on('load', () => merkezMap.resize());  // render at the container's real size (avoids distortion on wide screens)

    if (firmaMerkezLat) {
        const el = document.createElement('div');
        // Marker styled by class, not inline styles.
        el.className = 'mapbox-merkez-marker';
        el.textContent = '🏢';
        merkezMarker = new mapboxgl.Marker(el)
            .setLngLat([firmaMerkezLng, firmaMerkezLat])
            .setPopup(new mapboxgl.Popup({ offset: 25 }).setHTML("<b>Firma Merkezi</b>"))
            .addTo(merkezMap);
    }
    konumButonGuncelle();  // on load: compact "change" button if a base location exists

    // Stop map, only in DURAK mode and with the shuttle module (valet-only companies have no routes).
    if (firmaSistemModu === "DURAK" && firmaShuttleAktif) {
        document.getElementById('durakYonetimAlani').style.display = 'block';

        durakMap = new mapboxgl.Map({
            container: 'durak-map',
            style: 'mapbox://styles/mapbox/streets-v12',
            center: [lng, lat],
            zoom: firmaMerkezLat ? 14 : 7
        });
        durakMap.on('load', () => durakMap.resize());  // render at the container's real size

        setTimeout(() => {
            durakMap.on('click', async function (e) {
                if (!seciliGuzergahId) { alert("Lütfen önce bir Güzergah seçin!"); return; }
                const durakAdi = await appModal({
                    baslik: "Yeni Durak",
                    aciklama: "Durak ismini girin.",
                    inputTipi: "text",
                    placeholder: "Örn: Bornova Meydan",
                    validate: (deger) => {
                        if (!deger) return "Durak ismi boş olamaz.";
                        if (deger.length > 80) return "En fazla 80 karakter olabilir.";
                        return null;
                    }
                });
                if (!durakAdi) return;

                const mevcutDuraklar = duraklar.filter(d => d.guzergah_id === seciliGuzergahId);
                const varsayilanSira = mevcutDuraklar.length > 0 ? Math.max(...mevcutDuraklar.map(d => d.sira_no)) + 1 : 1;

                const girilenSiraStr = await appModal({
                    baslik: "Durak Sırası",
                    aciklama: `Bu durak güzergahta kaçıncı sırada olsun?`,
                    inputTipi: "number",
                    varsayilan: String(varsayilanSira),
                    placeholder: "1, 2, 3...",
                    validate: (deger) => {
                        const sayi = parseInt(deger);
                        if (isNaN(sayi) || sayi < 1) return "Geçerli bir sıra numarası girin (1 veya üzeri).";
                        return null;
                    }
                });
                if (!girilenSiraStr) return;
                const yeniSiraNo = parseInt(girilenSiraStr) || varsayilanSira;

                try {
                    const res = await fetch(`${CONFIG.BASE_URL}/durak-ekle`, {
                        method: 'POST', headers: getAuthHeaders(),
                        body: JSON.stringify({
                            guzergah_id: seciliGuzergahId, firma_id: firmaId,
                            durak_adi: durakAdi.trim(),
                            konum_lat: e.lngLat.lat,
                            konum_lng: e.lngLat.lng,
                            sira_no: yeniSiraNo
                        })
                    });
                    if (res.ok) duraklariGetir();
                } catch (err) { alert("Hata!"); }
            });
        }, 1500);
        guzergahlariGetir();
    }

    markalariGetir(); subeleriGetir(); personelleriGetir(); filoyuGetir(); rolDegisti();
}

// Compact "change" button when a base location is set, full-size "choose" button otherwise.
function konumButonGuncelle() {
    const btn = document.getElementById('firmaKonumAyarlaBtn');
    if (!btn) return;
    const merkezVar = (firmaMerkezLat != null && firmaMerkezLng != null);
    if (merkezVar) {
        btn.textContent = '📍 Merkezi Değiştir';
        btn.classList.add('konum-btn-kompakt');
    } else {
        btn.textContent = 'Merkezi Haritadan Seç';
        btn.classList.remove('konum-btn-kompakt');
    }
}

function firmaKonumunuAyarla() {
    document.getElementById('konumUyari').style.display = 'block';
    document.getElementById('konumUyari').innerText = "Haritaya tıklayarak merkezi işaretleyin.";
    merkezMap.on('click', konumSec);
    document.getElementById('merkez-map').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function konumSec(e) {
    geciciKonum = e.lngLat;
    if (geciciMarker) geciciMarker.remove();

    const el = document.createElement('div');
    el.className = 'mapbox-gecici-marker';
    el.textContent = '📍';

    geciciMarker = new mapboxgl.Marker(el)
        .setLngLat(geciciKonum).addTo(merkezMap)
        .setPopup(new mapboxgl.Popup({ offset: 25 }).setHTML("<b>Burası merkez mi?</b>"));

    geciciMarker.togglePopup();
    document.getElementById('konumOnayPanel').style.display = 'flex';
}

function konumuIptalEt() {
    if (geciciMarker) geciciMarker.remove();
    geciciMarker = null; geciciKonum = null;
    document.getElementById('konumOnayPanel').style.display = 'none';
    merkezMap.off('click', konumSec);
}

async function konumuOnayla() {
    if (!geciciKonum) return;
    try {
        const res = await fetch(`${CONFIG.BASE_URL}/firma-konum-kaydet?firma_id=${firmaId}&lat=${geciciKonum.lat}&lng=${geciciKonum.lng}`, {
            method: 'POST', headers: getAuthHeaders()
        });
        if (res.status === 401) { cikisYap(); return; }
        if (res.ok) {
            firmaMerkezLat = geciciKonum.lat; firmaMerkezLng = geciciKonum.lng;
            if (geciciMarker) geciciMarker.remove();
            if (merkezMarker) merkezMarker.remove();

            const el = document.createElement('div');
            el.className = 'mapbox-merkez-marker';
            el.textContent = '🏢';
            merkezMarker = new mapboxgl.Marker(el)
                .setLngLat([firmaMerkezLng, firmaMerkezLat])
                .setPopup(new mapboxgl.Popup({ offset: 25 }).setHTML("<b>Firma Merkezi</b>"))
                .addTo(merkezMap);

            merkezMap.off('click', konumSec);
            document.getElementById('konumUyari').style.display = 'none';
            document.getElementById('konumOnayPanel').style.display = 'none';
            geciciMarker = null; geciciKonum = null;
            konumButonGuncelle();  // after saving, the button turns into the compact "change" button
            alert("Merkez konumu kaydedildi!");
        }
    } catch (e) { alert("Konum kaydedilemedi."); }
}

async function dashboardVerileriniGuncelle() {
    try {
        const [yolcularRes, araclarRes] = await Promise.all([
            fetch(`${CONFIG.BASE_URL}/firma-talepleri?firma_id=${firmaId}`, { headers: getAuthHeaders() }),
            fetch(`${CONFIG.BASE_URL}/firma-araclari?firma_id=${firmaId}`, { headers: getAuthHeaders() })
        ]);

        if (yolcularRes.status === 401 || araclarRes.status === 401) { cikisYap(); return; }

        const yolcular = await yolcularRes.json();
        const araclar = await araclarRes.json();

        yolcuVeAracMarkers.forEach(m => m.remove());
        yolcuVeAracMarkers = [];

        let anlik = { bekliyor: 0, alindi: 0, gelmedi: 0 };

        yolcular.forEach(y => {
            if (y.konum_lat) {
                if (y.durum === 'KONUM ALINDI' || y.durum === 'SERVIS_HAZIR') {
                    anlik.bekliyor++; markerEkle(y, '#007aff');
                } else if (y.durum === 'YOLCU ALINDI' || y.durum === 'YOLCU INDI') {
                    anlik.alindi++; markerEkle(y, '#34c759');
                } else if (y.durum === 'YOLCU GELMEDİ') {
                    anlik.gelmedi++; markerEkle(y, '#ff9500');
                }
            }
        });

        araclar.forEach(a => {
            if (a.son_lat) {
                // Valets get a different marker from shuttle vehicles, so the two can be told apart on the map.
                const valeMi = a.tip === 'VALE';
                const el = document.createElement('div');
                el.className = valeMi ? 'vale-marker' : 'bus-marker';
                if (!valeMi) el.textContent = '🚌';

                const baslik = valeMi ? valeAdi(a.plaka) : (a.plaka || a.id);
                const altBilgi = a.sofor_adi ? a.sofor_adi : (valeMi ? 'Atanmamış' : 'Şoför atanmamış');
                // The marker is the last known position, not a live one: the PWAs cannot report their
                // position from the background (for example while navigation runs), so the time it was
                // recorded is shown with it. An absolute time is used rather than "42 min ago", because the
                // map is not redrawn continuously and a relative time would silently go wrong.
                const konumZamani = yalnizSaatTR(a.son_hareket_zamani);

                const marker = new mapboxgl.Marker(el)
                    .setLngLat([a.son_lng, a.son_lat])
                    .setPopup(new mapboxgl.Popup({ offset: 15 }).setHTML(`
                                <b>${baslik}</b>${valeMi ? ' <span class="marker-vale-rozet">VALE</span>' : ''}<br>
                                ${valeMi && a.sofor_adi ? '' : altBilgi}
                                ${a.sofor_tel ? `<br><a href="tel:${a.sofor_tel}">${a.sofor_tel}</a>` : ''}
                                ${konumZamani ? `<br><span class="marker-son-gorulme">Son bilinen konum: ${konumZamani}</span>` : ''}
                            `))
                    .addTo(merkezMap);
                yolcuVeAracMarkers.push(marker);
            }
        });

        // These counters may not exist in the HTML; check before writing.
        const bEl = document.getElementById('c-bekliyor');
        const aEl = document.getElementById('c-alindi');
        const gEl = document.getElementById('c-gelmedi');
        if (bEl) bEl.innerText = anlik.bekliyor;
        if (aEl) aEl.innerText = anlik.alindi;
        if (gEl) gEl.innerText = anlik.gelmedi;
    } catch (err) { }
}

function markerEkle(yolcu, color) {
    let plakaMetni = yolcu.arac_id || "Araç Yok";
    if (typeof araclar !== 'undefined') {
        let atanmisArac = araclar.find(a => a.id === yolcu.arac_id);
        if (atanmisArac) plakaMetni = atanmisArac.plaka;
    } else if (yolcu.arac_plaka) plakaMetni = yolcu.arac_plaka;

    let markaMetni = (yolcu.marka && yolcu.marka !== "Genel")
        ? ` <span class="marker-marka-rozet">${yolcu.marka}</span>` : "";

    const htmlIcerik = `<b>${yolcu.musteri_ad}</b>${markaMetni}<br><span class="marker-arac-bilgi">Araç: <b>${plakaMetni}</b></span>`;

    // Round marker; the colour is set per vehicle below.
    const el = document.createElement('div');
    el.className = 'yolcu-marker';
    el.style.backgroundColor = color;  // setting a style property from script is allowed by the CSP

    const marker = new mapboxgl.Marker(el)
        .setLngLat([yolcu.konum_lng, yolcu.konum_lat])
        .setPopup(new mapboxgl.Popup({ offset: 10 }).setHTML(htmlIcerik))
        .addTo(merkezMap);
    yolcuVeAracMarkers.push(marker);
}

// --- Reports, staff and vehicles ---
async function raporlariGetir(periyot = 'gunluk', btnElement = null) {
    if (btnElement) {
        document.querySelectorAll('.filtre-btn').forEach(b => b.classList.remove('active'));
        btnElement.classList.add('active');
    }
    const tablo = document.getElementById('aracRaporTablosu');
    const valeTablo = document.getElementById('valeRaporTablosu');
    const dakTablo = document.getElementById('dakiklikTablosu');
    const icerik = document.getElementById('raporIcerik');
    const yukleniyorSatiri = document.getElementById('raporYukleniyor');

    // The report screen loads as a whole. While loading, the whole content block is hidden and a
    // single "loading" line is shown; when the data arrives, all tables appear at once. Updating
    // the tables one by one would briefly show data from two different periods side by side.
    // Visibility is controlled only on the wrapper. The elements inside are not styled here,
    // because their display value belongs to the module flags (see modulUIUygula).
    const gorunum = (hazir) => {
        if (yukleniyorSatiri) yukleniyorSatiri.style.display = hazir ? 'none' : '';
        if (icerik) icerik.style.display = hazir ? '' : 'none';
    };
    gorunum(false);

    // Start the punctuality request now so it runs in parallel with arac-rapor.
    const dakSozu = dakiklikHtmlGetir(periyot);

    let aracHtml, valeHtml;
    try {
        const res = await fetch(`${CONFIG.BASE_URL}/arac-rapor?firma_id=${firmaId}&periyot=${periyot}`, { headers: getAuthHeaders() });
        if (res.status === 401) { gorunum(true); cikisYap(); return; }
        const tumAraclar = await res.json();
        
        const araclar = tumAraclar.filter(a => a.tip !== 'VALE');
        const valeler = tumAraclar.filter(a => a.tip === 'VALE');


        const renderTable = (veriListesi, valemi) => {
            if (!veriListesi.length) return '<p class="rapor-yukleniyor">Veri yok.</p>';

            // If nothing at all happened in the period, show a short message instead of a table:
            // /arac-rapor returns every staff member whether they worked or not, and a table full of
            // zeros says nothing. Zero rows are kept when there was some activity, because "this person
            // had 0 tasks today" is useful then. The check is per period, not per row.
            const hareketVar = veriListesi.some(a => valemi
                ? ((a.toplam_gorev || 0) + (a.teslim_edilen || 0) + (a.iptal_edilen || 0)
                   + (a.toplam_km || 0) + (a.vale_yolu_km || 0)) > 0
                : ((a.basarili || 0) + (a.gelmeyen || 0) + (a.toplama_sayisi || 0)
                   + (a.dagitim_sayisi || 0) + (a.toplam_km || 0)) > 0);
            if (!hareketVar) {
                return `<p class="rapor-yukleniyor">Bu dönemde ${valemi ? 'vale görevi' : 'servis görevi'} yok.</p>`;
            }

            if (valemi) {
                const topGorev = veriListesi.reduce((s, a) => s + (a.toplam_gorev || 0), 0);
                const topTeslim = veriListesi.reduce((s, a) => s + (a.teslim_edilen || 0), 0);
                const topKm = veriListesi.reduce((s, a) => s + a.toplam_km, 0).toFixed(1);

                return `
                    <div class="rapor-ozet-grid">
                        <div class="kart green-border rapor-ozet-kart">
                            <h4>TOPLAM GÖREV</h4>
                            <span class="rapor-ozet-deger">${topGorev}</span>
                        </div>
                        <div class="kart red-border rapor-ozet-kart" style="border-color: var(--blue);">
                            <h4 style="color: var(--blue);">TESLİM EDİLEN ARAÇ</h4>
                            <span class="rapor-ozet-deger">${topTeslim}</span>
                        </div>
                        <div class="kart orange-border rapor-ozet-kart">
                            <h4>TOPLAM YOL</h4>
                            <span class="rapor-ozet-deger">${topKm} <small class="rapor-km-birim">KM</small></span>
                        </div>
                    </div>
                    <table class="rapor-tablo">
                        <thead>
                            <tr class="rapor-tablo-header">
                                <th class="rapor-th rapor-th-left">Personel</th>
                                <th class="rapor-th rapor-th-center">Toplam Görev</th>
                                <th class="rapor-th rapor-th-center">Teslim Edilen</th>
                                <th class="rapor-th rapor-th-center">İptal</th>
                                <th class="rapor-th rapor-th-center">Toplam KM</th>
                                <th class="rapor-th rapor-th-center">Vale Yolu</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${veriListesi.map(a => `
                                <tr class="rapor-tablo-satir">
                                    <td class="rapor-td rapor-td-sofor">${valeAdi(a.sofor_adi)}</td>
                                    <td class="rapor-td rapor-td-center">${a.toplam_gorev}</td>
                                    <td class="rapor-td rapor-td-center rapor-basarili" style="color: var(--blue);">${a.teslim_edilen}</td>
                                    <td class="rapor-td rapor-td-center" style="color:#94a3b8;">${a.iptal_edilen || 0}</td>
                                    <td class="rapor-td rapor-td-center rapor-km">${a.toplam_km} <small class="rapor-km-birim-alt">KM</small></td>
                                    <td class="rapor-td rapor-td-center rapor-km">${a.vale_yolu_km} <small class="rapor-km-birim-alt">KM</small></td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                    <p class="dakiklik-notlar">
                        <span class="dakiklik-terim">Toplam KM</span> = müşterinin aracının vale ile kat ettiği yol (kapı ile servis arası).
                        Üstteki TOPLAM YOL kartı bu sütunu toplar.<br>
                        <span class="dakiklik-terim">Vale Yolu</span> = valenin kendi konumlarından ölçülen operasyon yolu: yola çıktığı noktadan müşterinin kapısına, oradan servise.
                        İptal edilen görevler ve tamamlanmamış adımlar sayılmaz. İki nokta arası yol mesafesidir, sapmalar ve ara duraklar dahil değildir.
                    </p>`;
            } else {
                const topBasarili = veriListesi.reduce((s, a) => s + a.basarili, 0);
                const topGelmeyen = veriListesi.reduce((s, a) => s + a.gelmeyen, 0);
                const topKm = veriListesi.reduce((s, a) => s + a.toplam_km, 0).toFixed(1);

                return `
                    <div class="rapor-ozet-grid">
                        <div class="kart green-border rapor-ozet-kart">
                            <h4>TOPLAM BAŞARILI</h4>
                            <span class="rapor-ozet-deger">${topBasarili}</span>
                        </div>
                        <div class="kart red-border rapor-ozet-kart">
                            <h4>TOPLAM GELMEYEN</h4>
                            <span class="rapor-ozet-deger">${topGelmeyen}</span>
                        </div>
                        <div class="kart orange-border rapor-ozet-kart">
                            <h4>TOPLAM YOL</h4>
                            <span class="rapor-ozet-deger">${topKm} <small class="rapor-km-birim">KM</small></span>
                        </div>
                    </div>
                    <table class="rapor-tablo">
                        <thead>
                            <tr class="rapor-tablo-header">
                                <th class="rapor-th rapor-th-left">Araç</th>
                                <th class="rapor-th rapor-th-left">Şoför</th>
                                <th class="rapor-th rapor-th-center">Başarılı</th>
                                <th class="rapor-th rapor-th-center">Gelmeyen</th>
                                <th class="rapor-th rapor-th-center">Toplama</th>
                                <th class="rapor-th rapor-th-center">Dağıtım</th>
                                <th class="rapor-th rapor-th-center">Toplam KM</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${veriListesi.map(a => `
                                <tr class="rapor-tablo-satir">
                                    <td class="rapor-td rapor-td-arac">${a.plaka}</td>
                                    <td class="rapor-td rapor-td-sofor">${a.sofor_adi}</td>
                                    <td class="rapor-td rapor-td-center rapor-basarili">${a.basarili}</td>
                                    <td class="rapor-td rapor-td-center rapor-gelmeyen">${a.gelmeyen}</td>
                                    <td class="rapor-td rapor-td-center">${a.toplama_sayisi}</td>
                                    <td class="rapor-td rapor-td-center">${a.dagitim_sayisi}</td>
                                    <td class="rapor-td rapor-td-center rapor-km">${a.toplam_km} <small class="rapor-km-birim-alt">KM</small></td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>`;
            }
        };

        if (!tumAraclar.length) {
            aracHtml = '<p class="rapor-yukleniyor">Henüz araç verisi yok.</p>';
            valeHtml = '<p class="rapor-yukleniyor">Henüz vale verisi yok.</p>';
        } else {
            aracHtml = renderTable(araclar, false);
            valeHtml = renderTable(valeler, true);
        }
    } catch (err) { 
        aracHtml = '<p class="rapor-yukleniyor">Rapor yüklenemedi.</p>';
        valeHtml = '<p class="rapor-yukleniyor">Rapor yüklenemedi.</p>';
    }

    // Punctuality comes from its own endpoint (ADMIN and SUPERADMIN only) and never rejects: on
    // failure it returns its own error text, and the rest of the report is still drawn.
    const dakHtml = await dakSozu;

    // Fill all three tables while the block is still hidden, then show it. The other order would
    // show old or empty content for a frame.
    tablo.innerHTML = aracHtml;
    if (valeTablo) valeTablo.innerHTML = valeHtml;
    if (dakTablo) dakTablo.innerHTML = dakHtml;
    gorunum(true);
}

// ============================================================
// Punctuality report: promised vs actual time
// ============================================================
// The server makes every judgement (on-time threshold, "needs attention" flag, data-quality
// counts) in /dakiklik-raporu. Do not redefine the threshold here, or the two files would drift.
function dakiklikSapmaHtml(dk, esikDk) {
    if (dk === null || dk === undefined) return '<span class="rapor-td-sofor">—</span>';
    const isaret = dk > 0 ? '+' : '';
    // Early is not shown in green. With a symmetric band an early arrival is also a deviation (the
    // estimate was off, or the button was pressed before the work was done); amber means "look".
    const sinif = dk > esikDk ? 'dakiklik-gec' : (dk < -esikDk ? 'dakiklik-erken' : 'dakiklik-zamaninda');
    return `<span class="${sinif}">${isaret}${dk} dk</span>`;
}

// Returns HTML rather than writing to the DOM, so all tables can be painted at once.
// Never rejects: on error it returns an error text, so raporlariGetir is not left waiting and
// the other two tables are still drawn.
async function dakiklikHtmlGetir(periyot) {
    try {
        const res = await fetch(`${CONFIG.BASE_URL}/dakiklik-raporu?firma_id=${firmaId}&periyot=${periyot}`, { headers: getAuthHeaders() });
        if (res.status === 401) { cikisYap(); return '<p class="rapor-yukleniyor">Oturum sonlandı.</p>'; }
        if (!res.ok) { return '<p class="rapor-yukleniyor">Dakiklik raporu yüklenemedi.</p>'; }
        const veri = await res.json();
        const valeler = (veri && veri.valeler) || [];
        const esik = (veri && veri.esikler) || { zamaninda_dk: 10, mesafe_m: 300, asgari_ornek: 5 };

        if (!valeler.length) {
            // "No data" and "no measurable tasks yet" are different; saying which one keeps a manager from
            // taking an empty table for a fault.
            return '<p class="rapor-yukleniyor">Bu dönemde ölçülebilir görev yok. Dakiklik, yalnız vale yola çıkarken varış saati hesaplanabilmiş ve tamamlanmış görevlerde ölçülür.</p>';
        }

        const oz = veri.ozet || {};
        return `
            <div class="rapor-ozet-grid">
                <div class="kart green-border rapor-ozet-kart">
                    <h4>ZAMANINDA TAMAMLAMA</h4>
                    <span class="rapor-ozet-deger">%${oz.zamaninda_yuzde ?? '—'}</span>
                </div>
                <div class="kart orange-border rapor-ozet-kart">
                    <h4>ORTALAMA SAPMA</h4>
                    <span class="rapor-ozet-deger">${dakiklikSapmaHtml(oz.ort_sapma_dk, esik.zamaninda_dk)}</span>
                </div>
                <div class="kart red-border rapor-ozet-kart" style="border-color: var(--blue);">
                    <h4 style="color: var(--blue);">ÖLÇÜLEN ETAP</h4>
                    <span class="rapor-ozet-deger">${oz.etap_sayisi ?? 0}</span>
                </div>
            </div>
            <table class="rapor-tablo">
                <thead>
                    <tr class="rapor-tablo-header">
                        <th class="rapor-th rapor-th-left">Personel</th>
                        <th class="rapor-th rapor-th-center">Ölçülen Etap</th>
                        <th class="rapor-th rapor-th-center">Ortalama Sapma</th>
                        <th class="rapor-th rapor-th-center">En Kötü</th>
                        <th class="rapor-th rapor-th-center">Zamanında</th>
                        <th class="rapor-th rapor-th-center">Erken</th>
                        <th class="rapor-th rapor-th-center">Geç</th>
                        <th class="rapor-th rapor-th-center">Konum Yok</th>
                        <th class="rapor-th rapor-th-center">Uzaktan İşaretleme</th>
                        <th class="rapor-th rapor-th-center">Müşteri Puanı</th>
                    </tr>
                </thead>
                <tbody>
                    ${valeler.map(v => `
                        <tr class="rapor-tablo-satir ${v.dikkat ? 'dakiklik-satir-dikkat' : ''}">
                            <td class="rapor-td rapor-td-sofor">${v.ad}${v.dikkat ? '<span class="dakiklik-rozet">sürekli geç</span>' : ''}${!v.yeterli_veri ? `<span class="dakiklik-yetersiz">az veri</span>` : ''}</td>
                            <td class="rapor-td rapor-td-center">${v.etap_sayisi}</td>
                            <td class="rapor-td rapor-td-center">${dakiklikSapmaHtml(v.ort_sapma_dk, esik.zamaninda_dk)}</td>
                            <td class="rapor-td rapor-td-center">${dakiklikSapmaHtml(v.en_kotu_sapma_dk, esik.zamaninda_dk)}</td>
                            <td class="rapor-td rapor-td-center rapor-basarili">%${v.zamaninda_yuzde}</td>
                            <td class="rapor-td rapor-td-center ${v.erken ? 'dakiklik-erken' : ''}">${v.erken ?? 0}</td>
                            <td class="rapor-td rapor-td-center ${v.gec ? 'dakiklik-gec' : ''}">${v.gec}</td>
                            <td class="rapor-td rapor-td-center ${v.konum_bildirilmeyen ? 'dakiklik-uyari' : ''}">${v.konum_bildirilmeyen}</td>
                            <td class="rapor-td rapor-td-center ${v.uzaktan_basilan ? 'dakiklik-uyari' : ''}">${v.uzaktan_basilan}</td>
                            <td class="rapor-td rapor-td-center">${memPuanHtml(v.musteri_puani, v.anket_sayisi)}</td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>
            <p class="dakiklik-notlar">
                <span class="dakiklik-terim">Zamanında</span> = sapma en fazla ${esik.zamaninda_dk} dakika, erken ya da geç fark etmez.
                Beklenenden çok erken tamamlanan görev de sapmadır: ya tahmin tutmamıştır ya da iş bitmeden işaretlenmiştir.<br>
                <span class="dakiklik-terim">Sürekli geç</span> uyarısı en az ${esik.asgari_ornek} ölçülen etap olmadan verilmez; tek kötü gün örüntü değildir.<br>
                <span class="dakiklik-terim">Konum Yok</span> = vale işaretleme anında konum bildirmedi, cihazda izin kapalı olabilir.<br>
                <span class="dakiklik-terim">Uzaktan İşaretleme</span> = hedeften ${esik.mesafe_m} metreden uzakta işaretlenmiş, okuması taze ve hassas kayıt sayısı.<br>
                Son iki sütun sapmayı değil, sapmaya duyulan güveni ölçer.<br>
                <span class="dakiklik-terim">Müşteri Puanı</span> = teslim sonrası ankette verilen genel memnuniyet ortalaması (5 üzerinden).
                Anket gelmemişse boş görünür; sıfır diye gösterilmez, çünkü puan aralığı 1 ile 5 arasındadır.
                Bu sütun ile ortalama sapmanın birlikte okunması gerekir: sapma temiz ama puan düşükse sorun dakiklik değildir.
            </p>`;
    } catch (e) {
        return '<p class="rapor-yukleniyor">Dakiklik raporu yüklenemedi.</p>';
    }
}

// ============================================================
// Satisfaction survey report
// ============================================================
// Data and question labels come from /memnuniyet-raporu (ANKET_SORU_ETIKETLERI in main.py);
// they are not redefined here. A rating is never 0 (the range is 1-5), so no data shows a dash.
function memPuanHtml(puan, adet) {
    if (puan === null || puan === undefined) return '<span class="mem-yok">—</span>';
    const metin = String(puan).replace('.', ',');
    return `<span class="mem-puan">${metin}</span><span class="mem-puan-birim"> / 5</span>`
        + (adet ? `<div class="mem-puan-birim">${adet} anket</div>` : '');
}

// Show the customer's comment exactly as typed, never as HTML. The innerHTML guard
// (DOMPurify) already blocks scripts but allows harmless tags like <b>, which would format the
// comment. A comment is data, so escape it to entities here.
function metniKacir(metin) {
    return String(metin == null ? '' : metin)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

async function memnuniyetGetir(periyot = 'aylik', btnElement = null) {
    if (btnElement) {
        // Only this tab's buttons, so the filters on the reports tab are not affected.
        document.querySelectorAll('.filtre-btn[data-aksiyon="memnuniyet-getir"]')
            .forEach(b => b.classList.remove('active'));
        btnElement.classList.add('active');
    }
    const icerik = document.getElementById('memnuniyetIcerik');
    const yukleniyor = document.getElementById('memnuniyetYukleniyor');
    if (!icerik) return;
    // Same pattern as the reports tab: hide the content while loading, show one indicator, then
    // draw everything at once.
    if (yukleniyor) yukleniyor.style.display = '';
    icerik.style.display = 'none';

    let html;
    try {
        const res = await fetch(`${CONFIG.BASE_URL}/memnuniyet-raporu?firma_id=${firmaId}&periyot=${periyot}`, { headers: getAuthHeaders() });
        if (res.status === 401) { cikisYap(); return; }
        if (!res.ok) throw new Error('yuklenemedi');
        const veri = await res.json();
        const valeler = (veri && veri.valeler) || [];
        const sorular = (veri && veri.sorular) || {};
        const oz = (veri && veri.ozet) || null;

        if (!valeler.length) {
            html = '<p class="rapor-yukleniyor">Bu dönemde değerlendirme yok. Anket, teslim görevi tamamlandıktan sonra müşterinin bağlantısında açılır ve 24 saat içinde doldurulabilir.</p>';
        } else {
            const alanlar = Object.keys(sorular);
            html = `
                <div class="rapor-ozet-grid">
                    <div class="kart green-border rapor-ozet-kart">
                        <h4>GENEL MEMNUNİYET</h4>
                        <span class="rapor-ozet-deger">${memPuanHtml(oz && oz.puan_genel, 0)}</span>
                    </div>
                    <div class="kart red-border rapor-ozet-kart" style="border-color: var(--blue);">
                        <h4 style="color: var(--blue);">DEĞERLENDİRME</h4>
                        <span class="rapor-ozet-deger">${(oz && oz.anket_sayisi) || 0}</span>
                    </div>
                    <div class="kart orange-border rapor-ozet-kart">
                        <h4>YAZILI YORUM</h4>
                        <span class="rapor-ozet-deger">${(oz && oz.yorum_sayisi) || 0}</span>
                    </div>
                </div>

                <h2 class="mem-bolum-baslik">Personel Bazlı Puanlar</h2>
                <table class="rapor-tablo">
                    <thead>
                        <tr class="rapor-tablo-header">
                            <th class="rapor-th rapor-th-left">Personel</th>
                            <th class="rapor-th rapor-th-center">Değerlendirme</th>
                            ${alanlar.map(a => `<th class="rapor-th rapor-th-center">${sorular[a]}</th>`).join('')}
                        </tr>
                    </thead>
                    <tbody>
                        ${valeler.map(v => `
                            <tr class="rapor-tablo-satir">
                                <td class="rapor-td rapor-td-sofor">${v.ad}</td>
                                <td class="rapor-td rapor-td-center">${v.anket_sayisi}</td>
                                ${alanlar.map(a => `<td class="rapor-td rapor-td-center">${memPuanHtml(v[a], 0)}</td>`).join('')}
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
                <p class="dakiklik-notlar">
                    Atlanan sorular ortalamaya girmez; hiç cevaplanmamış bir soru boş görünür.
                    Puan aralığı 1 ile 5 arasındadır, bu yüzden sıfır gösterilmez.
                </p>`;

            const yorumlar = (veri && veri.yorumlar) || [];
            html += `<h2 class="mem-bolum-baslik">Yazılı Geri Bildirimler</h2>`;
            if (!yorumlar.length) {
                html += '<p class="rapor-yukleniyor">Bu dönemde yazılı yorum yok.</p>';
            } else {
                html += yorumlar.map(y => `
                    <div class="mem-yorum">
                        <div class="mem-yorum-ust">
                            <span class="mem-yorum-puan">${y.puan_genel} / 5</span>
                            <span>${y.ad}</span>
                            <span>${saatTR(y.kayit_tarihi)}</span>
                            ${y.marka ? `<span class="mem-rozet">${metniKacir(y.marka)}</span>` : ''}
                        </div>
                        <div class="mem-yorum-metin">${metniKacir(y.yorum)}</div>
                    </div>`).join('');
                html += `<p class="dakiklik-notlar">Yazılı yorumlar 90 gün sonra silinir; puanlar kalır.</p>`;
            }
        }
    } catch (e) {
        html = '<p class="rapor-yukleniyor">Memnuniyet raporu yüklenemedi.</p>';
    }

    icerik.innerHTML = html;
    if (yukleniyor) yukleniyor.style.display = 'none';
    icerik.style.display = '';
}

async function markalariGetir() {
    try {
        const res = await fetch(`${CONFIG.BASE_URL}/firma-markalari?firma_id=${firmaId}`, { headers: getAuthHeaders() });
        const markalar = await res.json();
        const options = '<option value="Genel">Genel / Merkez</option>' + markalar.map(m => `<option value="${m.marka_adi}">${m.marka_adi}</option>`).join('');
        document.getElementById('pMarka').innerHTML = options; document.getElementById('v_marka').innerHTML = options;
        // Existing brands/departments with a delete button.
        const liste = document.getElementById('markaListesi');
        if (liste) {
            if (!Array.isArray(markalar) || markalar.length === 0) {
                liste.innerHTML = '<p class="marka-bos">Henüz bölüm eklenmemiş.</p>';
            } else {
                liste.innerHTML = markalar.map(m => {
                    const ad = (m.marka_adi || '').replace(/</g, "&lt;").replace(/>/g, "&gt;");
                    return `<div class="marka-satir"><span>${ad}</span><button class="sil-btn sil-kucuk" data-aksiyon="marka-sil" data-marka-id="${m.id}" data-marka-ad="${ad}">Sil</button></div>`;
                }).join('');
            }
        }
    } catch (e) { }
}

async function markaSil(id, ad) {
    if (!confirm(`"${ad}" bölümünü silmek istediğinize emin misiniz?\n\nBu bölüme bağlı kullanıcı/araç varsa silme engellenir; önce onları başka bölüme taşımalısınız.`)) return;
    try {
        const res = await fetch(`${CONFIG.BASE_URL}/marka-sil/${id}`, { method: 'DELETE', headers: getAuthHeaders() });
        if (res.ok) {
            alert("Bölüm silindi.");
            markalariGetir();
        } else {
            const hata = await res.json().catch(() => ({ detail: "Bilinmeyen hata" }));
            alert("Silinemedi: " + (hata.detail || "Bilinmeyen hata"));
        }
    } catch (e) {
        alert("Sunucuya bağlanılamadı.");
    }
}

async function markaEkle() {
    const ad = document.getElementById('yeniMarkaAdi').value.trim();
    if (!ad) return alert("Bölüm adı boş olamaz!");

    if (ad.length > 50) return alert("Bölüm adı en fazla 50 karakter olabilir.");
    if (!/^[a-zA-Z0-9ğüşıöçĞÜŞİÖÇ\s\-_.,&/]+$/.test(ad)) {
        return alert("Bölüm adı sadece harf, rakam ve temel noktalama içerebilir.\n\nKabul edilmeyen karakterler: < > ' \" ; ( ) [ ] gibi.");
    }

    try {
        const res = await fetch(CONFIG.BASE_URL + "/marka-ekle", {
            method: "POST",
            headers: getAuthHeaders(),
            // No sube_id: the server derives the branch from the caller's scope (HQ admin: HQ, branch
            // admin: their own branch).
            body: JSON.stringify({ firma_id: firmaId, marka_adi: ad, sube_id: null })
        });
        if (res.ok) {
            alert("Bölüm eklendi!");
            document.getElementById('yeniMarkaAdi').value = "";
            markalariGetir();
        } else {
            const hata = await res.json().catch(() => ({ detail: "Bilinmeyen hata" }));
            alert("Eklenemedi: " + (hata.detail || "Bilinmeyen hata"));
        }
    } catch (e) {
        alert("Sunucuya bağlanılamadı.");
    }
}

// Read-only vehicle quota display (for a company without branches, and for a branch admin;
// for an HQ admin the branch panel shows it).
function kotaBilgiGuncelle() {
    const el = document.getElementById('kotaBilgi');
    if (!el) return;
    if (firmaSubeli && !aktifSubeId) { el.classList.add('gizli'); return; }  // HQ: the branch panel already shows the quotas
    const kota = aktifSubeId ? subeAktifKota : firmaToplamKota;
    if (kota === null || kota === undefined) { el.classList.add('gizli'); return; }
    el.classList.remove('gizli');
    el.textContent = `Araç kotası: ${sonAracSayisi}/${kota} (+1 yedek)`;
}

// === Branch management (HQ admin) ===
// Fill the branch selectors. They are shown only in a company with branches and only to an HQ
// admin; a branch admin always writes to their own branch.
function subeSecicileriDoldur(subeler) {
    // Only the staff form keeps a branch selector (HQ creates branch managers). Vehicles and
    // brands created by HQ always go to HQ, and the server derives that from the caller's scope.
    const pSubeEl = document.getElementById('pSube');
    if (pSubeEl) {
        pSubeEl.innerHTML = '<option value="">— Şube seçin —</option>' +
            (subeler || []).map(s => `<option value="${s.id}">${(s.sube_adi || '').replace(/</g, '&lt;').replace(/>/g, '&gt;')}</option>`).join('');
    }
    // In a company with branches, offer ADMIN in the role list so HQ can create branch managers.
    const rolEl = document.getElementById('p_rol');
    if (rolEl) {
        const varMi = Array.from(rolEl.options).some(o => o.value === 'ADMIN');
        if (firmaSubeli && !varMi) {
            const opt = document.createElement('option');
            opt.value = 'ADMIN';
            opt.textContent = 'Yönetici (Şube)';
            rolEl.appendChild(opt);
        } else if (!firmaSubeli && varMi) {
            Array.from(rolEl.options).forEach(o => { if (o.value === 'ADMIN') o.remove(); });
        }
    }
    rolDegisti();  // update the branch selector's visibility for the current role
}

async function subeleriGetir() {
    const panel = document.getElementById('subePanel');
    const panelGizle = () => { if (panel) panel.classList.add('gizli'); };
    try {
        const res = await fetch(`${CONFIG.BASE_URL}/sube-listele/${firmaId}`, { headers: getAuthHeaders() });
        if (!res.ok) { firmaSubeli = false; subeSecicileriDoldur([]); panelGizle(); return; }
        const v = await res.json();
        firmaSubeli = !!v.subeli;
        // Values for the quota display (company quota, and a branch admin's own branch quota).
        firmaToplamKota = (v.toplam_kota != null) ? v.toplam_kota : null;
        subeAktifKota = null;
        if (aktifSubeId && Array.isArray(v.subeler)) {
            const benimSube = v.subeler.find(s => s.id === aktifSubeId);
            subeAktifKota = benimSube ? (benimSube.aktif_kota || 0) : null;
        }
        kotaBilgiGuncelle();
        if (!firmaSubeli) { subeSecicileriDoldur([]); panelGizle(); return; }   // no branches: hide the selectors
        subeSecicileriDoldur(v.subeler || []);
        // Branch management is for the HQ admin only (no sube_id); branch admins cannot manage branches.
        if (aktifSubeId || !panel) { panelGizle(); return; }
        panel.classList.remove('gizli');
        document.getElementById('subeKotaOzet').innerHTML =
            `Toplam araç kotası: <b>${v.toplam_kota}</b> &nbsp;·&nbsp; Dağıtılmış: <b>${v.dagitilmis}</b> &nbsp;·&nbsp; Kalan: <b>${v.kalan}</b> &nbsp;·&nbsp; Şube: <b>${v.sube_sayisi}/${v.max_sube}</b>`;
        const liste = document.getElementById('subeListesi');
        if (!v.subeler.length) {
            liste.innerHTML = '<p>Henüz şube yok — yukarıdan ekleyin.</p>';
        } else {
            liste.innerHTML = v.subeler.map(s => {
                const ad = (s.sube_adi || '').replace(/</g, "&lt;").replace(/>/g, "&gt;");
                const pasif = s.aktif === false ? ' · <i>pasif</i>' : '';
                return `<div class="sube-satir">
                    <span><b>${ad}</b> — aktif kota: <b>${s.aktif_kota || 0}</b>${pasif}</span>
                    <span>
                        <button class="ana-btn sube-btn-kucuk" data-aksiyon="sube-kota" data-sube-id="${s.id}" data-kota="${s.aktif_kota || 0}">Kota</button>
                        <button class="sil-btn sil-kucuk" data-aksiyon="sube-sil" data-sube-id="${s.id}">Sil</button>
                    </span>
                </div>`;
            }).join('');
        }
    } catch (e) { }
}

async function subeEkle() {
    const ad = document.getElementById('yeniSubeAdi').value.trim();
    const kota = parseInt(document.getElementById('yeniSubeKota').value, 10) || 0;
    if (!ad) return alert("Şube adı boş olamaz!");
    if (ad.length > 60) return alert("Şube adı en fazla 60 karakter olabilir.");
    if (!/^[a-zA-Z0-9ğüşıöçĞÜŞİÖÇ\s\-_.,&/]+$/.test(ad)) return alert("Şube adı sadece harf, rakam ve temel noktalama içerebilir.");
    try {
        const res = await fetch(CONFIG.BASE_URL + "/sube-ekle", {
            method: "POST", headers: getAuthHeaders(),
            body: JSON.stringify({ firma_id: firmaId, sube_adi: ad, aktif_kota: kota })
        });
        const data = await res.json().catch(() => ({}));
        if (res.ok) {
            document.getElementById('yeniSubeAdi').value = "";
            document.getElementById('yeniSubeKota').value = "0";
            subeleriGetir();
        } else {
            alert("Eklenemedi: " + (data.detail || "Bilinmeyen hata"));
        }
    } catch (e) { alert("Sunucuya bağlanılamadı."); }
}

async function subeKotaGuncelle(subeId, mevcutKota) {
    const girdi = prompt("Bu şubenin aktif araç kotası:", mevcutKota);
    if (girdi === null) return;
    const yeni = parseInt(girdi, 10);
    if (isNaN(yeni) || yeni < 0) return alert("Geçersiz kota.");
    try {
        const res = await fetch(CONFIG.BASE_URL + "/sube-guncelle", {
            method: "PUT", headers: getAuthHeaders(),
            body: JSON.stringify({ sube_id: subeId, aktif_kota: yeni })
        });
        const data = await res.json().catch(() => ({}));
        if (res.ok) subeleriGetir();
        else alert("Güncellenemedi: " + (data.detail || "Bilinmeyen hata"));
    } catch (e) { alert("Sunucuya bağlanılamadı."); }
}

async function subeSil(subeId) {
    if (!confirm("Bu şube silinsin mi? (Bağlı kullanıcı/araç/güzergah/talep varsa engellenir.)")) return;
    try {
        const res = await fetch(`${CONFIG.BASE_URL}/sube-sil/${subeId}`, { method: "DELETE", headers: getAuthHeaders() });
        const data = await res.json().catch(() => ({}));
        if (res.ok) subeleriGetir();
        else alert("Silinemedi: " + (data.detail || "Bilinmeyen hata"));
    } catch (e) { alert("Sunucuya bağlanılamadı."); }
}

async function personelEkle() {
    const kadi = document.getElementById('p_kadi').value.trim();
    const emailKutusu = document.getElementById('p_email');
    const email = emailKutusu ? emailKutusu.value.trim() : "";
    const rol = document.getElementById('p_rol').value;
    const marka = document.getElementById('pMarka').value;
    const pSubeEl = document.getElementById('pSube');
    const subeId = (pSubeEl && !pSubeEl.classList.contains('gizli')) ? pSubeEl.value : '';  // hidden for branch admins; the server uses their own branch
    // New ADMIN: in a company with branches an HQ admin creates a branch manager (branch required);
    // otherwise it is a brand manager (brand required).
    if (rol === 'ADMIN') {
        if (firmaSubeli && !aktifSubeId) {
            if (!subeId) return alert("Şube yöneticisi için bir şube seçmelisiniz.");
        } else {
            if (!marka || marka === 'Genel') return alert("Marka yöneticisi için bir bölüm/marka seçmelisiniz.");
        }
    }
    let telRaw = document.getElementById('p_tel').value.trim();
    let tel = "";
    // For a direct WhatsApp chat, turn a 10-digit number into 905XXXXXXXXX, whatever the role.
    // This is only for the WhatsApp link: the phone sent to the server is set for field roles only
    // (below).
    const telDigits = telRaw.replace(/\D/g, '');
    const waTel = telDigits.length === 10 ? "90" + telDigits : "";
    if (!kadi) return alert("Kullanıcı adı boş olamaz!");
    // Field roles (driver, valet) must have a phone so operations can call them.
    if (rol === 'SOFOR' || rol === 'VALE') {
        if (telRaw.length !== 10) { alert("Telefon numarası 10 hane olmalıdır!"); return; }
        tel = "+90" + telRaw;
    }

    if (email) {
        const emailRegex = /^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/;
        if (!emailRegex.test(email)) {
            return alert("E-posta adresi geçersiz görünüyor.\n\nÖrnek: kullanici@ornekfirma.com.tr");
        }
    }

    const btn = document.getElementById('personelEkleBtn');
    btn.innerText = "Kaydediliyor..."; btn.disabled = true;

    try {
        const res = await fetch(CONFIG.BASE_URL + "/kullanici-ekle", {
            method: "POST", headers: getAuthHeaders(),
            body: JSON.stringify({ ekleyen_kisi: ekleyenKisi || "admin", kullanici_adi: kadi, email: email, telefon: tel, rol: rol, firma_id: firmaId, marka: marka, sube_id: subeId || null })
        });
        if (res.ok) {
            const veri = await res.json();
            document.getElementById('p_kadi').value = ""; if (emailKutusu) emailKutusu.value = ""; document.getElementById('p_tel').value = "";
            davetPenceresiAc(kadi, veri.davet_linki, email, marka, waTel); personelleriGetir();
        } else { const hataJson = await res.json(); alert("Kayıt Hatası: " + hataJson.detail); }
    } catch (error) { } finally { btn.innerText = "Personeli Kaydet"; btn.disabled = false; }
}

function davetPenceresiAc(isim, link, email, marka, waTel) {
    aktifDavetBilgileri = { isim, link, email, marka };
    document.getElementById('modalLink').innerText = link;
    document.getElementById('davetModal').style.display = 'flex';
    const wpMesaj = `Merhaba! ${isim} kullanıcı adıyla Shuttle & Valet Ops sistemine eklendiniz. Sisteme giriş şifrenizi belirlemek için lütfen şu linke tıklayın: ${link}`;
    // With a number, open that person's chat directly; otherwise let the user pick a contact.
    const wpTaban = waTel ? `https://wa.me/${waTel}` : 'https://wa.me/';
    document.getElementById('btnWp').onclick = () => window.open(`${wpTaban}?text=${encodeURIComponent(wpMesaj)}`, '_blank', 'noopener,noreferrer');
    const mailBtn = document.getElementById('btnMail');
    if (email && email.includes('@')) {
        mailBtn.style.display = 'block';
        mailBtn.onclick = () => mailGonderModal(isim, link, email, marka);
    } else {
        mailBtn.style.display = 'none';
    }
}

function linkKopyalaModal() {
    navigator.clipboard.writeText(aktifDavetBilgileri.link).then(() => alert("Link kopyalandı!"));
}

function davetModalKapat() {
    document.getElementById('davetModal').style.display = 'none';
}

async function mailGonderModal(kadi, link, email, marka) {
    const btn = document.getElementById('btnMail'); btn.innerText = "Gönderiliyor..."; btn.disabled = true;
    try {
        const res = await fetch(CONFIG.BASE_URL + "/mail-davet-at", { method: 'POST', headers: getAuthHeaders(), body: JSON.stringify({ kullanici_adi: kadi }) });
        if (res.ok) {
            alert("Davet maili gönderildi!");
        } else {
            const hata = await res.json().catch(() => ({ detail: "Bilinmeyen hata" }));
            alert("Mail gönderilemedi: " + (hata.detail || "Sebep belirsiz"));
        }
    } catch (e) { } btn.innerText = "E-posta ile Gönder"; btn.disabled = false;
}

async function aracEkle() {
    const plaka = document.getElementById('v_plaka').value.trim(); const marka = document.getElementById('v_marka').value;
    if (!plaka) return alert("Plaka girmelisiniz!");
    try {
        // No sube_id: the server derives the branch from the caller's scope.
        const res = await fetch(`${CONFIG.BASE_URL}/arac-ekle?plaka=${plaka}&firma_id=${firmaId}&marka=${marka}`, { method: 'POST', headers: getAuthHeaders() });
        if (res.ok) { alert("Araç eklendi!"); document.getElementById('v_plaka').value = ""; filoyuGetir(); } else { const err = await res.json(); alert(err.detail); }
    } catch (e) { }
}

async function filoyuGetir() {
    try {
        const res = await fetch(`${CONFIG.BASE_URL}/firma-araclari?firma_id=${firmaId}`, { headers: getAuthHeaders() });
        const araclar = await res.json();
        // Hide valets' virtual vehicles; valets are counted against their own quota.
        const gercekAraclar = Array.isArray(araclar) ? araclar.filter(a => a.tip !== 'VALE') : [];
        // The quota display counts shuttle vehicles only, matching kapsam_kota_bilgisi on the server.
        sonAracSayisi = gercekAraclar.length;
        kotaBilgiGuncelle();
        const liste = document.getElementById('aracListesi');
        liste.innerHTML = gercekAraclar.map(a => `
                    <div class="arac-satir">
                        <div class="arac-satir-ust">
                            <div><b class="arac-plaka-buyuk">${a.plaka}</b><br><span class="arac-sofor-info">${a.sofor_adi ? `Şoför: ${a.sofor_adi}` : 'Boşta'}</span></div>
                            <button class="arac-sil-btn" data-aksiyon="arac-sil" data-arac-id="${a.id}">Sil</button>
                        </div>
                        <div class="arac-guzergah-kutu ${firmaSistemModu === 'DURAK' ? 'gosterilsin-flex' : ''}">
                            <span class="arac-guzergah-label">Atalı Hat:</span>
                            <select class="arac-guzergah-select" data-aksiyon="arac-guzergah-guncelle" data-arac-id="${a.id}">
                                <option value="">-- Serbest Araç (Hat Yok) --</option>
                                ${guzergahlar.map(g => `<option value="${g.id}" ${a.guzergah_id === g.id ? 'selected' : ''}>${g.guzergah_adi}</option>`).join('')}
                            </select>
                        </div>
                    </div>`).join('');
        window.tumAraclar = araclar;
    } catch (e) { }
}

async function aracSil(id) {
    if (!confirm("Emin misiniz?")) return;
    const res = await fetch(`${CONFIG.BASE_URL}/arac-sil/${id}`, { method: 'DELETE', headers: getAuthHeaders() });
    if (res.ok) {
        filoyuGetir();
    } else {
        const err = await res.json().catch(() => ({}));
        alert(err.detail || "Araç silinemedi.");
    }
}

async function personelleriGetir() {
    try {
        const res = await fetch(`${CONFIG.BASE_URL}/firma-personelleri/${firmaId}`, { method: 'GET', headers: getAuthHeaders() });
        const personeller = await res.json();
        const liste = document.getElementById('personelListesi');
        if (personeller.length === 0) { liste.innerHTML = '<p class="bos-personel-mesaj">Henüz personel yok.</p>'; return; }

        const grupluPersoneller = {};
        personeller.forEach(p => { const marka = p.marka || 'Genel / Merkez'; if (!grupluPersoneller[marka]) grupluPersoneller[marka] = []; grupluPersoneller[marka].push(p); });

        let html = '';
        for (const [markaAdi, kisiler] of Object.entries(grupluPersoneller)) {
            const divId = 'grup-' + markaAdi.replace(/[^a-z0-9]/gi, '-').toLowerCase();
            const aktifSayisi = kisiler.filter(k => !k.davet_token).length;
            html += `
                    <div class="akordeon-grup">
                        <div class="akordeon-baslik" data-aksiyon="akordeon-toggle" data-grup-id="${divId}">
                            <div><span class="marka-rozet">${markaAdi}</span><span class="kisi-sayisi">(${aktifSayisi} / ${kisiler.length})</span></div><span id="ikon-${divId}">▼</span>
                        </div>
                        <div class="akordeon-icerik" id="${divId}">
                            ${kisiler.map(p => `
                                <div class="personel-satir">
                                    <div>
                                        <div class="personel-rol-row">
                                            <span class="personel-rol ${p.rol === 'SOFOR' ? 'rol-sofor' : 'rol-diger'}">${p.rol}</span>
                                            ${(p.arac_id && p.rol !== 'VALE') ? `<span class="plaka-etiket">${p.arac_plaka || p.arac_id}</span>` : ''}
                                        </div>
                                        <b class="personel-kadi ${(p.gorunen_ad || '').trim() ? 'kimlik-acilir' : ''}" ${(p.gorunen_ad || '').trim() ? 'data-aksiyon="kimlik-toggle" title="Giriş kullanıcı adını göster/gizle"' : ''}>${(p.gorunen_ad || '').trim() || p.kullanici_adi}</b>
                                        ${(p.gorunen_ad || '').trim() ? `<span class="personel-kimlik">${p.kullanici_adi}</span>` : ''}
                                        ${p.davet_token ? '<span class="durum-bekliyor-rozet">Bekliyor</span>' : '<span class="durum-aktif-rozet">Aktif</span>'}
                                    </div>
                                    <div class="personel-aksiyonlar">
                                        ${p.rol === 'SOFOR' ? (p.arac_id ? `<button class="kes-btn" data-aksiyon="baglanti-kes" data-kadi="${p.kullanici_adi}">Kes</button>` : `<button class="ata-btn" data-aksiyon="arac-ata" data-kadi="${p.kullanici_adi}">Ata</button>`) : ''}
                                        <button data-aksiyon="ad-duzenle" data-kadi="${p.kullanici_adi}" data-ad="${((p.gorunen_ad || '').trim()).replace(/&/g, '&amp;').replace(/"/g, '&quot;')}" class="ata-btn sifre-kucuk">Ad</button><button data-aksiyon="sifre-sifirla" data-kadi="${p.kullanici_adi}" class="sifre-sifirla-btn sifre-kucuk">Şifre</button><button data-aksiyon="personel-sil" data-kadi="${p.kullanici_adi}" class="sil-btn sil-kucuk">SİL</button>
                                    </div>
                                </div>`).join('')}
                        </div>
                    </div>`;
        }
        liste.innerHTML = html;
    } catch (error) { }
}

async function aracAtaModalAc(kadi) {
    // tumAraclar may not be loaded yet (filoyuGetir has not run or failed). Tell "not loaded"
    // apart from "no free vehicles" instead of failing on undefined.
    if (!Array.isArray(window.tumAraclar)) {
        alert("Araç listesi henüz yüklenmedi. Lütfen birkaç saniye sonra tekrar deneyin.");
        return;
    }
    // Never list a valet's virtual vehicle; it is an implementation detail. Normally its valet
    // user is assigned so sofor_adi is set and it is filtered out anyway; this extra check also
    // covers an orphaned record.
    const bosAraclar = window.tumAraclar.filter(a => !a.sofor_adi && (a.tip || 'SERVIS') !== 'VALE');
    if (bosAraclar.length === 0) { alert("Boşta araç bulunmuyor!"); return; }
    const secenekler = bosAraclar.map(a => a.plaka).join("\n- ");
    const secilenPlaka = await appModal({
        baslik: `${kadi} için araç ata`,
        aciklama: `Boştaki araçlar:\n${secenekler.split('\n- ').map(p => '  • ' + p.trim()).join('\n')}`,
        inputTipi: "text",
        placeholder: "Plaka (örn: 34ABC123)",
        onayButon: "Ata",
        validate: (deger) => {
            if (!deger) return "Plaka boş olamaz.";
            return null;
        }
    });
    if (secilenPlaka) {
        const plakaBuyuk = secilenPlaka.trim().toUpperCase();
        const araçVarMi = bosAraclar.find(a => a.plaka === plakaBuyuk);
        if (araçVarMi) {
            try {
                const res = await fetch(`${CONFIG.BASE_URL}/sofor-arac-ata`, { method: "PUT", headers: getAuthHeaders(), body: JSON.stringify({ kullanici_adi: kadi, yeni_arac_id: araçVarMi.id }) });
                if (res.ok) { alert(`Atandı.`); personelleriGetir(); filoyuGetir(); }
            } catch (e) { }
        } else { alert("Hatalı plaka!"); }
    }
}

async function baglantiyiKes(kadi) {
    if (!confirm(`Ayırmak istediğinize emin misiniz?`)) return;
    try {
        const res = await fetch(`${CONFIG.BASE_URL}/sofor-baglantisini-kes?kullanici_adi=${kadi}`, { method: 'POST', headers: getAuthHeaders() });
        if (res.ok) { personelleriGetir(); filoyuGetir(); }
    } catch (e) { }
}

function akordeonGosterGizle(id) {
    const icerik = document.getElementById(id);
    const ikon = document.getElementById('ikon-' + id);
    if (!icerik || !ikon) return;
    if (icerik.classList.contains('akordeon-acik')) {
        icerik.classList.remove('akordeon-acik');
        ikon.innerText = '▼';
    } else {
        icerik.classList.add('akordeon-acik');
        ikon.innerText = '▲';
    }
}

async function personelSil(kadi) {
    if (!confirm(`${kadi} kullanıcısını silmek istediğinize emin misiniz?`)) return;
    const res = await fetch(`${CONFIG.BASE_URL}/personel-sil/${kadi}`, { method: 'DELETE', headers: getAuthHeaders() });
    if (!res.ok) {
        const hata = await res.json().catch(() => ({ detail: "Bilinmeyen hata" }));
        alert("Silinemedi: " + (hata.detail || "Yetki hatası"));
        return;
    }
    personelleriGetir();
}

// --- Routes and stops ---
async function guzergahlariGetir() {
    try {
        const res = await fetch(`${CONFIG.BASE_URL}/firma-guzergahlari?firma_id=${firmaId}`, { headers: getAuthHeaders() });
        if (res.ok) { guzergahlar = await res.json(); guzergahListesiniCiz(); filoyuGetir(); }
    } catch (e) { }
}

function guzergahListesiniCiz() {
    const liste = document.getElementById('guzergahListesi');
    if (guzergahlar.length === 0) { liste.innerHTML = '<div class="bos-guzergah-mesaj">Henüz güzergah eklenmemiş.</div>'; return; }
    liste.innerHTML = guzergahlar.map(g => {
        // Route shape badge: LINE, YARIM_AY ("half moon") or not computed yet.
        const rozet = g.tip === 'LINE' ? 'LINE' : (g.tip === 'YARIM_AY' ? 'YARIM AY' : 'Tip yok');
        const btnMetni = g.tip ? 'Yeniden' : 'Bitti';
        return `
                <div class="guzergah-satir ${seciliGuzergahId === g.id ? 'guzergah-secili' : ''}" data-aksiyon="guzergah-sec" data-guzergah-id="${g.id}" data-guzergah-ad="${g.guzergah_adi}">
                    <b class="guzergah-ad ${seciliGuzergahId === g.id ? 'guzergah-ad-secili' : ''}">${g.guzergah_adi}</b>
                    <div class="guzergah-satir-sag">
                        <span class="guzergah-tip-rozet">${rozet}</span>
                        <button class="guzergah-tip-btn" data-aksiyon="guzergah-tip-hesapla" data-guzergah-id="${g.id}">${btnMetni}</button>
                        <button class="guzergah-sil-btn" data-aksiyon="guzergah-sil" data-guzergah-id="${g.id}">Sil</button>
                    </div>
                </div>`;
    }).join('');
}

async function guzergahEkle() {
    const ad = document.getElementById('yeniGuzergahAdi').value.trim(); if (!ad) return alert("Lütfen bir güzergah adı yazın.");
    try {
        // No sube_id: the server uses the creator's scope (HQ admin: NULL, branch admin: their branch).
        const res = await fetch(`${CONFIG.BASE_URL}/guzergah-ekle`, { method: 'POST', headers: getAuthHeaders(), body: JSON.stringify({ firma_id: firmaId, guzergah_adi: ad }) });
        if (res.ok) { document.getElementById('yeniGuzergahAdi').value = ''; guzergahlariGetir(); }
    } catch (e) { }
}

async function guzergahSil(id, event) {
    if (event) event.stopPropagation();
    if (!confirm("DİKKAT: İçindeki tüm duraklar da silinecektir! Emin misiniz?")) return;
    try {
        const res = await fetch(`${CONFIG.BASE_URL}/guzergah-sil/${id}`, { method: 'DELETE', headers: getAuthHeaders() });
        if (res.ok) {
            if (seciliGuzergahId === id) {
                seciliGuzergahId = null;
                durakMarkers.forEach(m => m.remove());
                durakMarkers = [];
                document.getElementById('aktifGuzergahBilgi').style.display = 'none';
            }
            guzergahlariGetir();
        }
    } catch (e) { }
}

function guzergahSec(id, ad) {
    seciliGuzergahId = id; guzergahListesiniCiz();
    const bilgi = document.getElementById('aktifGuzergahBilgi');
    bilgi.style.display = 'block';
    bilgi.innerText = `Haritaya Tıklayarak "${ad}" İçin Durak Ekleyin`;
    duraklariGetir();
}

async function guzergahTipiHesapla(id) {
    try {
        const res = await fetch(`${CONFIG.BASE_URL}/guzergah-tipi-hesapla/${id}`, { method: 'POST', headers: getAuthHeaders() });
        if (res.ok) {
            const veri = await res.json();
            const tipMetni = veri.tip === 'LINE' ? 'LINE (doğrusal)' : 'YARIM AY (dairesel)';
            alert(`Güzergah tipi: ${tipMetni}\nDurak sayısı: ${veri.durak_sayisi}`);
            guzergahlariGetir();
        } else {
            const err = await res.json().catch(() => ({}));
            alert("Hesaplanamadı: " + (err.detail || "Bilinmeyen hata"));
        }
    } catch (e) { alert("Hata!"); }
}

async function duraklariGetir() {
    if (!seciliGuzergahId) return;
    try {
        const res = await fetch(`${CONFIG.BASE_URL}/firma-duraklari?firma_id=${firmaId}`, { headers: getAuthHeaders() });
        if (res.ok) { duraklar = await res.json(); haritayaDuraklariCiz(); }
    } catch (e) { }
}

function haritayaDuraklariCiz() {
    durakMarkers.forEach(m => m.remove());
    durakMarkers = [];

    const aktifDuraklar = duraklar.filter(d => d.guzergah_id === seciliGuzergahId).sort((a, b) => a.sira_no - b.sira_no);

    aktifDuraklar.forEach((d) => {
        // Popup content; the delete button uses data-aksiyon, handled on document.body below.
        const popupIcerik = `<div class="durak-popup"><b class="durak-popup-baslik">${d.sira_no}. ${d.durak_adi}</b><button class="durak-sil-popup-btn" data-aksiyon="durak-sil" data-durak-id="${d.id}">Durağı Sil</button></div>`;

        const el = document.createElement('div');
        // Numbered stop marker.
        el.innerHTML = `<div class="durak-marker-numara">${d.sira_no}</div>`;

        const marker = new mapboxgl.Marker(el)
            .setLngLat([d.konum_lng, d.konum_lat])
            .setPopup(new mapboxgl.Popup({ offset: 15 }).setHTML(popupIcerik))
            .addTo(durakMap);
        durakMarkers.push(marker);
    });
}

async function durakSil(durakId) {
    if (!confirm("Durağı silmek istediğinize emin misiniz?")) return;
    try {
        const res = await fetch(`${CONFIG.BASE_URL}/durak-sil/${durakId}`, { method: 'DELETE', headers: getAuthHeaders() });
        if (res.ok) { duraklariGetir(); }
    } catch (e) { }
}

async function aracGuzergahGuncelle(aracId, guzergahId) {
    try {
        const res = await fetch(`${CONFIG.BASE_URL}/arac-guzergah-ata`, {
            method: 'PUT',
            headers: getAuthHeaders(),
            body: JSON.stringify({ arac_id: aracId, guzergah_id: guzergahId || null })
        });
        if (!res.ok) {
            const hata = await res.json();
            alert(hata.detail || "Güzergah atama başarısız.");
            filoyuGetir();
        }
    } catch (e) {
        alert("Bağlantı hatası, tekrar deneyin.");
    }
}

// WebSocket: refresh on "YENILE".
let ws = null;
let guncellemeKilit = false;

function canliBaglantiKur() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
    const wsUrl = CONFIG.BASE_URL.replace(/^http/, 'ws') + '/ws';
    ws = new WebSocket(wsUrl);
    ws.onopen = function () {
        let wsToken = localStorage.getItem('app_token');
        if (wsToken) { ws.send(JSON.stringify({ token: wsToken })); }
    };
    ws.onmessage = function (event) {
        if (event.data !== "YENILE") return;
        if (guncellemeKilit) return;
        guncellemeKilit = true;
        try {
            if (typeof dashboardVerileriniGuncelle === "function") dashboardVerileriniGuncelle();
            if (typeof personelleriGetir === "function") personelleriGetir();
        } catch (err) {
            console.error('Güncelleme hatası:', err);
        } finally {
            setTimeout(() => { guncellemeKilit = false; }, 2000);
        }
    };
    ws.onclose = () => setTimeout(canliBaglantiKur, 3000);
}

document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === 'visible') {
        canliBaglantiKur();
        if (typeof dashboardVerileriniGuncelle === "function") dashboardVerileriniGuncelle();
    }
});

// ============================================================
// Event listeners
// ============================================================
document.addEventListener('DOMContentLoaded', () => {
    // Hide the phone field until a field role is chosen.
    const pTelContainer = document.getElementById('p_tel_container');
    if (pTelContainer) pTelContainer.style.display = 'none';

    kullaniciAdiAlaniniBagla('p_kadi');  // converts Turkish letters to ASCII while typing

    // Client-side role check, only to show the right screen; the server enforces access.
    if (!firmaId || aktifRol !== 'ADMIN') {
        document.getElementById('yetkiHata').style.display = 'flex';
        setTimeout(() => { window.location.href = "login.html"; }, 2000);
        return;
    }

    // Apply the module flags from the login cache now, without waiting for the server, so a
    // valet-only company never sees shuttle sections flash. firma-detay applies the final values.
    firmaShuttleAktif = localStorage.getItem('aktif_shuttle_aktif') !== '0';
    modulUIUygula(firmaShuttleAktif, localStorage.getItem('aktif_vale_aktif') === '1');

    document.getElementById('appContainer').style.display = 'flex';
    sistemiBaslat();

    // Resize the Mapbox maps when the window size changes (maximise, wide screens); otherwise the
    // canvas keeps its old size and shows grey areas. Debounced.
    let _haritaResizeTimer = null;
    window.addEventListener('resize', () => {
        clearTimeout(_haritaResizeTimer);
        _haritaResizeTimer = setTimeout(() => {
            try { if (merkezMap) merkezMap.resize(); } catch (e) { }
            try { if (durakMap) durakMap.resize(); } catch (e) { }
        }, 200);
    });

    // --- Sidebar navigation ---
    document.querySelectorAll('.nav-item[data-aksiyon="sayfa-degistir"]').forEach(item => {
        item.addEventListener('click', function () {
            sayfaDegistir(this.dataset.sayfaId, this);
        });
    });

    const operasyonAcBtn = document.getElementById('operasyonAcBtn');
    if (operasyonAcBtn) {
        operasyonAcBtn.addEventListener('click', () => window.open('operasyon.html', '_blank', 'noopener'));
    }

    const cikisBtn = document.getElementById('cikisBtn');
    if (cikisBtn) cikisBtn.addEventListener('click', () => cikisYap());

    // --- Form inputs ---
    const pRolEl = document.getElementById('p_rol');
    if (pRolEl) pRolEl.addEventListener('change', rolDegisti);

    const pTelEl = document.getElementById('p_tel');
    if (pTelEl) {
        pTelEl.addEventListener('input', function () {
            this.value = this.value.replace(/[^0-9]/g, '');
        });
    }

    // --- Static buttons ---
    const markaEkleBtn = document.getElementById('markaEkleBtn');
    if (markaEkleBtn) markaEkleBtn.addEventListener('click', markaEkle);

    const subeEkleBtn = document.getElementById('subeEkleBtn');
    if (subeEkleBtn) subeEkleBtn.addEventListener('click', subeEkle);

    const personelEkleBtn = document.getElementById('personelEkleBtn');
    if (personelEkleBtn) personelEkleBtn.addEventListener('click', personelEkle);

    const aracEkleBtn = document.getElementById('aracEkleBtn');
    if (aracEkleBtn) aracEkleBtn.addEventListener('click', aracEkle);

    const firmaKonumAyarlaBtn = document.getElementById('firmaKonumAyarlaBtn');
    if (firmaKonumAyarlaBtn) firmaKonumAyarlaBtn.addEventListener('click', firmaKonumunuAyarla);

    const konumOnaylaBtn = document.getElementById('konumOnaylaBtn');
    if (konumOnaylaBtn) konumOnaylaBtn.addEventListener('click', konumuOnayla);

    const konumIptalBtn = document.getElementById('konumIptalBtn');
    if (konumIptalBtn) konumIptalBtn.addEventListener('click', konumuIptalEt);

    const guzergahEkleBtn = document.getElementById('guzergahEkleBtn');
    if (guzergahEkleBtn) guzergahEkleBtn.addEventListener('click', guzergahEkle);

    // --- Report period buttons ---
    document.querySelectorAll('.filtre-btn[data-aksiyon="rapor-getir"]').forEach(btn => {
        btn.addEventListener('click', function () {
            raporlariGetir(this.dataset.periyot, this);
        });
    });

    // --- Satisfaction period buttons (separate tab, separate endpoint) ---
    document.querySelectorAll('.filtre-btn[data-aksiyon="memnuniyet-getir"]').forEach(btn => {
        btn.addEventListener('click', function () {
            memnuniyetGetir(this.dataset.periyot, this);
        });
    });

    // --- Dialog buttons ---
    const linkKopyalaBtn = document.getElementById('linkKopyalaBtn');
    if (linkKopyalaBtn) linkKopyalaBtn.addEventListener('click', linkKopyalaModal);

    const davetModalKapatBtn = document.getElementById('davetModalKapatBtn');
    if (davetModalKapatBtn) davetModalKapatBtn.addEventListener('click', davetModalKapat);

    // --- Delegated handlers for lists that are redrawn: staff ---
    const personelListesi = document.getElementById('personelListesi');
    if (personelListesi) {
        personelListesi.addEventListener('click', (e) => {
            const el = e.target.closest('[data-aksiyon]');
            if (!el) return;

            const aksiyon = el.dataset.aksiyon;
            const kadi = el.dataset.kadi;
            const grupId = el.dataset.grupId;

            if (aksiyon === 'akordeon-toggle' && grupId) {
                akordeonGosterGizle(grupId);
            } else if (aksiyon === 'sifre-sifirla' && kadi) {
                sifreSifirlaPrompt(kadi);
            } else if (aksiyon === 'personel-sil' && kadi) {
                personelSil(kadi);
            } else if (aksiyon === 'baglanti-kes' && kadi) {
                baglantiyiKes(kadi);
            } else if (aksiyon === 'arac-ata' && kadi) {
                aracAtaModalAc(kadi);
            } else if (aksiyon === 'ad-duzenle' && kadi) {
                gorunenAdDuzenle(kadi, el.dataset.ad || '');
            } else if (aksiyon === 'kimlik-toggle') {
                // The login username is hidden by default so it does not show up in screenshots; a click on
                // the name reveals it when the admin needs it.
                const kimlik = el.parentElement && el.parentElement.querySelector('.personel-kimlik');
                if (kimlik) kimlik.classList.toggle('gorunur');
            }
        });
    }

    // --- Branch list ---
    const subeListesi = document.getElementById('subeListesi');
    if (subeListesi) {
        subeListesi.addEventListener('click', (e) => {
            const el = e.target.closest('[data-aksiyon]');
            if (!el) return;
            const aksiyon = el.dataset.aksiyon;
            const subeId = el.dataset.subeId;
            if (aksiyon === 'sube-sil' && subeId) subeSil(subeId);
            else if (aksiyon === 'sube-kota' && subeId) subeKotaGuncelle(subeId, el.dataset.kota || 0);
        });
    }

    // --- Brand/department list ---
    const markaListesi = document.getElementById('markaListesi');
    if (markaListesi) {
        markaListesi.addEventListener('click', (e) => {
            const el = e.target.closest('[data-aksiyon="marka-sil"]');
            if (el && el.dataset.markaId) markaSil(el.dataset.markaId, el.dataset.markaAd || '');
        });
    }

    // --- Vehicle list ---
    const aracListesi = document.getElementById('aracListesi');
    if (aracListesi) {
        aracListesi.addEventListener('click', (e) => {
            const el = e.target.closest('[data-aksiyon]');
            if (!el) return;

            const aksiyon = el.dataset.aksiyon;
            const aracIdData = el.dataset.aracId;

            if (aksiyon === 'arac-sil' && aracIdData) {
                aracSil(aracIdData);
            }
        });

        aracListesi.addEventListener('change', (e) => {
            const el = e.target.closest('[data-aksiyon="arac-guzergah-guncelle"]');
            if (!el) return;
            aracGuzergahGuncelle(el.dataset.aracId, el.value);
        });
    }

    // --- Route list ---
    const guzergahListesi = document.getElementById('guzergahListesi');
    if (guzergahListesi) {
        guzergahListesi.addEventListener('click', (e) => {
            const el = e.target.closest('[data-aksiyon]');
            if (!el) return;

            const aksiyon = el.dataset.aksiyon;
            const guzergahId = el.dataset.guzergahId;

            if (aksiyon === 'guzergah-sil' && guzergahId) {
                e.stopPropagation();
                guzergahSil(guzergahId, e);
            } else if (aksiyon === 'guzergah-tip-hesapla' && guzergahId) {
                e.stopPropagation();
                guzergahTipiHesapla(guzergahId);
            } else if (aksiyon === 'guzergah-sec' && guzergahId) {
                guzergahSec(guzergahId, el.dataset.guzergahAd);
            }
        });
    }

    // --- Mapbox popups ---
    // Mapbox adds popups under document.body, so the stop delete button is handled there.
    document.body.addEventListener('click', (e) => {
        const el = e.target.closest('[data-aksiyon="durak-sil"]');
        if (el && el.dataset.durakId) {
            durakSil(el.dataset.durakId);
        }
    });

    canliBaglantiKur();
});