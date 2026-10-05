// danisman.html: the advisor's panel. Creates shuttle passengers and valet tasks, produces the
// customer's tracking link, lists open and completed work, and starts deliveries for cars
// waiting at the service center. Handlers are attached here because the CSP forbids inline scripts.

// ============================================================
// Every assignment to innerHTML on this page is passed through DOMPurify.
// ============================================================
(function () {
    const orijinalSetter = Object.getOwnPropertyDescriptor(Element.prototype, 'innerHTML').set;
    Object.defineProperty(Element.prototype, 'innerHTML', {
        set: function (html) {
            const temiz = (typeof DOMPurify !== 'undefined' && typeof html === 'string')
                ? DOMPurify.sanitize(html, {
                    // 'disabled' is needed to render busy valets as unselectable options.
                    ADD_ATTR: ['target', 'rel', 'value', 'placeholder', 'disabled', 'data-arac-id', 'data-orjinal-saat', 'data-aksiyon'],
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
// Panel
// ============================================================
const firmaId = localStorage.getItem('aktif_firma_id');
const aktifRol = localStorage.getItem('aktif_rol');
const aktifMarka = localStorage.getItem('aktif_marka') || "Genel";
let aktifYolcuLinki = "";
let tumAraclarData = [];
let firmaValeAktif = false;
// Valets with a task in progress; they are disabled in the dropdown (one active task per valet).
let mesgulValeIdleri = new Set();

// A valet's virtual vehicle stores the valet's username in its "plaka" field. Older records
// have a "Vale " prefix, which is stripped so the panel does not repeat the word.
function valeAdi(plaka) {
    return (plaka || '').replace(/^Vale\s+/i, '') || 'Vale';
}

// Name to show for a valet (same logic as operasyon.js): the display name from the vehicle list
// (firma-araclari -> sofor_adi). A retired valet has no user row, so fall back to the virtual
// vehicle's plate, which is the valet's username.
function valeGorunenAd(aracId, yedekPlaka) {
    const arac = (tumAraclarData || []).find(a => a.id === aracId);
    return valeAdi((arac && arac.sofor_adi) || yedekPlaka);
}

// Date and time for display, always in Istanbul time (the server stores UTC), so a device in
// another time zone shows the same times. Returns a dash for empty or invalid input. Copied in
// operasyon.js, admin.js and vale.js; keep the copies identical.
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

// "No consent" badge. Identical to the copy in operasyon.js; keep them in sync.
// If the customer refused or withdrew consent there is no point waiting for a location, so
// staff should call. Without the badge, "never opened the link" and "refused" look the same
// (no location in both cases) although they call for different actions. Refusal and
// withdrawal share one flag (riza_reddedildi) because the action is the same; the legal
// distinction is kept in the consent log.
function rizaRozeti(y) {
    if (!y || y.riza_reddedildi !== true) return '';
    return '<span class="riza-rozet" title="Müşteri konum onayı vermedi veya onayını geri çekti. Konum alınamaz — telefonla iletişime geçin.">ONAY YOK</span>';
}

// Readable labels for valet statuses, so staff never see raw values like KONUM_ALINDI_VALE.
// ARAC_ALINDI means different things per task type (see valeDurumEtiketi):
//    pickup:   the valet took the car from the customer
//    delivery: the valet took the car out of the service center
const VALE_DURUM_ETIKET = {
    'BEKLIYOR_KONUM': 'Konum Bekleniyor',
    'KONUM_ALINDI_VALE': 'Konum Alındı',
    'VALE_YOLDA': 'Vale Yolda',
    'TAMAM_SERVIS': 'Serviste',
    'TAMAM_MUSTERI': 'Teslim Edildi',
    'IPTAL_EDILDI': 'İptal Edildi'
};
function valeDurumEtiketi(durum, gorevTipi) {
    if (durum === 'ARAC_ALINDI') return gorevTipi === 'VALE_TESLIM' ? 'Servisten Çıktı' : 'Araç Alındı';
    return VALE_DURUM_ETIKET[durum] || (durum || '').replace(/_/g, ' ');
}

// Must match VALE_IPTAL_EDILEBILIR_DURUMLAR in main.py exactly (operasyon.js has the same copy).
// It depends on the task type, so it cannot be a flat list:
//   pickup:   KONUM_ALINDI_VALE -> VALE_YOLDA -> ARAC_ALINDI
//             in VALE_YOLDA the car has not been taken yet: cancellable
//   delivery: KONUM_ALINDI_VALE -> ARAC_ALINDI -> VALE_YOLDA
//             in VALE_YOLDA the valet is driving the customer's car: not cancellable
// This copy only shows or hides the button; the server makes the real decision (409).
const VALE_IPTAL_EDILEBILIR = {
    'VALE_ALIM': ['BEKLIYOR_KONUM', 'KONUM_ALINDI_VALE', 'VALE_YOLDA'],
    'VALE_TESLIM': ['BEKLIYOR_KONUM', 'KONUM_ALINDI_VALE']
};
function valeIptalEdilebilirMi(y) {
    return !!y && (VALE_IPTAL_EDILEBILIR[y.gorev_tipi] || []).includes(y.durum);
}

// ============================================================
// Task milestone timeline (promised vs actual times)
// ============================================================
// Identical to the copy in operasyon.js; change both together. The data comes from the
// `etaplar` array in the firma-talepleri response.
//
// The promise shown is the one frozen when the leg started (hedef_varis), not the live
// estimate the customer sees. The live estimate moves with the valet and is never stored;
// punctuality cannot be measured against a moving target.
const ETAP_ETIKET = {
    'MUSTERIYE_GIDIS': 'Müşteriye gidiş',
    'SERVISE_DONUS': 'Servise dönüş',
    'MUSTERIYE_TESLIM': 'Müşteriye teslim',
    'SERVIS_HAZIRLIK': 'Servis hazırlığı'
};

// Time only (the row already shows the date).
function etapSaat(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '—';
    return d.toLocaleTimeString('tr-TR', { timeZone: 'Europe/Istanbul', hour: '2-digit', minute: '2-digit' });
}

// Deviation badge: + is late, - is early. Within 10 minutes either way counts as on time
// (same threshold as DAKIKLIK_ZAMANINDA_ESIK_DK on the server).
function etapSapma(sapma) {
    if (sapma === null || sapma === undefined) return '';
    const gec = sapma > 10;
    const erken = sapma < -10;
    const sinif = gec ? 'etap-gec' : (erken ? 'etap-erken' : 'etap-zamaninda');
    const isaret = sapma > 0 ? '+' : '';
    return `<span class="etap-sapma ${sinif}">${isaret}${sapma} dk</span>`;
}

// Where the button was pressed, relative to the target. Shown on the operations panel only;
// the advisor panel creates tasks and does not evaluate staff. (On this page etapCizelgesi is
// called without mesafeGoster, so this badge is never shown here.)
// Why 300 m: the valet app sends a cached GPS reading (it cannot wait for a fresh one inside
// the click), and for a moving valet a slightly old reading is already a few hundred metres
// off. A lower threshold would flag valets standing at the door. One record means little;
// patterns show up in the punctuality report.
// The badge wording reports a measurement and must not sound like an accusation; judging is
// the manager's job.
// The thresholds below must match DAKIKLIK_* in main.py.
const ETAP_MESAFE_ESIGI_M = 300;

function etapMesafeBicim(m) {
    return m >= 1000 ? (m / 1000).toFixed(1).replace('.', ',') + ' km' : m + ' m';
}

// A reading is trusted only within these limits; outside them no "pressed remotely" verdict
// is shown.
const ETAP_KONUM_TAZE_SN = 60;      // readings older than this are not judged
const ETAP_KONUM_DOGRULUK_M = 200;  // nor readings less accurate than this

function etapKonumUyarisi(e) {
    // 1) No position reported at all (location permission may be off): worth following up.
    if (e.konum_bildirildi === false) {
        return '<span class="etap-konum-uyari" title="Vale bu adımda konum bildirmedi — cihazda konum izni kapalı olabilir. Yönetimin takip etmesi gereken durum.">konum bildirilmedi</span>';
    }
    // 2) The order matters: an unreliable reading must not be turned into a distance verdict.
    // The app sends its cached reading on a press, and while navigation runs the app is in the
    // background, so that reading can be minutes old and kilometres away from a valet who is
    // actually at the door. In that case describe the data, not the valet.
    const yas = e.basma_konum_yasi_sn;
    if (typeof yas === 'number' && yas > ETAP_KONUM_TAZE_SN) {
        const dk = Math.round(yas / 60);
        return `<span class="etap-konum-uyari" title="İşaretleme anında cihazın konumu güncel değildi; mesafe ölçümü bu kayıt için yorumlanamaz.">konum eski (${dk >= 1 ? dk + ' dk' : yas + ' sn'} önce alınmış)</span>`;
    }
    const dogruluk = e.basma_dogruluk_m;
    if (typeof dogruluk === 'number' && dogruluk > ETAP_KONUM_DOGRULUK_M) {
        return `<span class="etap-konum-uyari" title="Cihazın konum hassasiyeti düşüktü; mesafe ölçümü bu kayıt için yorumlanamaz.">konum belirsiz (±${etapMesafeBicim(dogruluk)})</span>`;
    }
    // 3) Fresh and accurate reading: the distance is a real finding.
    if (typeof e.basma_mesafe_m === 'number' && e.basma_mesafe_m > ETAP_MESAFE_ESIGI_M) {
        return `<span class="etap-konum-uyari" title="İşaretleme, hedef noktadan bu kadar uzakta yapıldı. Okuma taze ve hassastı. Tek kayıt değil, ÖRÜNTÜ anlamlıdır.">hedeften ${etapMesafeBicim(e.basma_mesafe_m)} uzakta işaretlendi</span>`;
    }
    return '';
}

// mesafeGoster is true only on the operations panel (see above).
function etapCizelgesi(y, mesafeGoster) {
    const etaplar = (y && y.etaplar) || [];
    if (!etaplar.length) return '';
    const satirlar = etaplar.map(e => {
        const ad = ETAP_ETIKET[e.etap] || e.etap;
        if (e.iptal_edildi) {
            return `<div class="etap-satir"><b>${ad}</b> <span class="etap-iptal">iptal edildi</span></div>`;
        }
        // Legs without a promise show times only, no deviation.
        const sozVar = !!e.hedef_varis;
        if (!e.gercek_varis) {
            return `<div class="etap-satir"><b>${ad}</b> · çıkış ${etapSaat(e.baslangic)}`
                + (sozVar ? ` · söz <b>${etapSaat(e.hedef_varis)}</b>` : '')
                + ` · <span class="etap-suruyor">sürüyor</span></div>`;
        }
        return `<div class="etap-satir"><b>${ad}</b> · çıkış ${etapSaat(e.baslangic)}`
            + (sozVar ? ` · söz <b>${etapSaat(e.hedef_varis)}</b>` : '')
            + ` · varış <b>${etapSaat(e.gercek_varis)}</b> ${etapSapma(e.sapma_dk)}`
            + (mesafeGoster ? ` ${etapKonumUyarisi(e)}` : '')
            + `</div>`;
    }).join('');
    return `<div class="etap-cizelge">${satirlar}</div>`;
}

// One row in the "completed" list, used both inside valet groups and for shuttle requests.
function tamamlananSatir(y) {
    // Valet completions (TAMAM_SERVIS, TAMAM_MUSTERI) count as successful; only TAMAM_GELMEDI (no-show) does not.
    const basariliMi = ['TAMAM_ALINDI', 'TAMAM_SERVIS', 'TAMAM_MUSTERI'].includes(y.durum);
    const iptalMi = y.durum === 'IPTAL_EDILDI';
    const valeMi = y.gorev_tipi && y.gorev_tipi.startsWith('VALE_');
    const gorevMetni = y.gorev_tipi === 'VALE_ALIM' ? 'Araç Alım'
        : y.gorev_tipi === 'VALE_TESLIM' ? 'Araç Teslim' : (y.gorev_tipi || 'Görev');
    const detay = valeMi
        ? `Plaka: <b class="tamamlanan-arac">${y.musteri_plaka || 'Belirsiz'}</b>`
        : `Araç: <b class="tamamlanan-arac">${y.arac_plaka || 'Belirsiz'}</b>`;
    // Opened -> finished. Older rows without tamamlanma_tarihi show a dash for the end.
    const zaman = `<span class="tamamlanan-zaman">${saatTR(y.kayit_tarihi)} → ${saatTR(y.tamamlanma_tarihi)}</span>`;
    const rozetSinif = iptalMi ? 'tamamlanan-iptal' : (basariliMi ? 'tamamlanan-basarili' : 'tamamlanan-basarisiz');
    const rozetMetin = iptalMi ? 'İPTAL' : (basariliMi ? 'BAŞARILI' : 'GELMEDİ');
    return `
        <div class="tamamlanan-satir">
            <div class="tamamlanan-bilgi">
                <b class="tamamlanan-ad">${y.musteri_ad}</b>
                <span class="tamamlanan-detay">${gorevMetni} • ${detay}</span>
                ${zaman}
                ${etapCizelgesi(y)}
            </div>
            <div class="tamamlanan-rozet ${rozetSinif}">
                ${rozetMetin}
            </div>
        </div>`;
}
let servistekiAraclar = [];   // customer cars a valet brought in, waiting at the service center
let secilenServistekiId = null; // source pickup task when a delivery was started with "Teslim Et"

function getAuthHeaders() {
    const token = localStorage.getItem('app_token');
    return {
        "Authorization": `Bearer ${token}`,
        "Content-Type": "application/json"
    };
}

async function cikisYap(sebep = null) {
    await appCikis();
    if (sebep) {
        window.location.href = `/login.html?sebep=${encodeURIComponent(sebep)}`;
    } else {
        window.location.href = "/login.html";
    }
}

async function sistemiBaslat() {
    document.getElementById('danismanMarka').innerText = "Bölüm: " + aktifMarka;
    // Hide the valet sections using the flag stored at login, so they do not flash on screen
    // before firma-detay answers.
    if (localStorage.getItem('aktif_vale_aktif') === '0') {
        ['valeKart', 'servistekiKart'].forEach(id => { const el = document.getElementById(id); if (el) el.style.display = 'none'; });
    }
    try {
        const res = await fetch(`${CONFIG.BASE_URL}/firma-detay/${firmaId}`, { headers: getAuthHeaders() });
        if (res.status === 401) { cikisYap(); return; }
        const detay = await res.json();
        document.getElementById('firmaAdiBaslik').innerText = detay.firma_adi + " Operasyon";
        firmaValeAktif = !!detay.vale_aktif;
        // Without the valet module: remove valet task types and hide the valet sections.
        if (!detay.vale_aktif) {
            document.querySelectorAll('#mGorev option[value^="VALE_"]').forEach(o => o.remove());
            const vk = document.getElementById('valeKart'); if (vk) vk.style.display = 'none';
            const sk = document.getElementById('servistekiKart'); if (sk) sk.style.display = 'none';
        }
        // Without the shuttle module (valet-only company): remove shuttle task types, hide the
        // passenger section, and reword and renumber the section titles so the numbering has no gap.
        if (detay.shuttle_aktif === false) {
            document.querySelectorAll('#mGorev option[value="TOPLAMA"], #mGorev option[value="DAGITIM"]').forEach(o => o.remove());
            const yolcularKart = document.getElementById('yolcularKart');
            if (yolcularKart) yolcularKart.style.display = 'none';
            const yaz = (id, metin) => { const el = document.getElementById(id); if (el) el.innerText = metin; };
            yaz('yeniKayitBaslik', '1. Yeni Vale Görevi');
            yaz('valeBaslik', '2. Vale Görevleri (Canlı)');
            yaz('tamamlananBaslik', '3. Tamamlanan Görevler (Bugün)');
            yaz('servistekiBaslik', '4. Serviste Bekleyen Araçlar');
            yaz('aracSeciciEtiket', 'Atanacak Vale');
            // In a valet-only company the person is a customer leaving a car, not a passenger.
            yaz('headerAltyazi', 'Danışman / Müşteri Kabul Ekranı');
        }
    } catch (e) { }

    // The vehicle dropdown is filled inside listeyiGuncelle from the same fetch, so it also
    // updates on every "YENILE".
    listeyiGuncelle();
}

// Fill the vehicle/valet dropdown (mArac). Keeps the current choice, so a refresh arriving
// while the advisor is filling in the form does not reset it.
function aracSecicileriniDoldur(araclar) {
    const select = document.getElementById('mArac');
    const gorevSelect = document.getElementById('mGorev');
    if (!select || !gorevSelect) return;
    const oncekiSecim = select.value;
    const seciliGorev = gorevSelect.value;
    
    // Until a task type is chosen it is unknown whether to list vehicles or valets, so ask for
    // the task type first. (Defaulting to vehicles would tell a valet-only company that no
    // vehicle exists.)
    if (!seciliGorev) {
        select.innerHTML = "<option value=''>-- Önce görev tipi seçiniz --</option>";
        return;
    }

    const valeGorevi = seciliGorev.startsWith("VALE");
    const gosterilecekAraclar = valeGorevi
        ? araclar.filter(a => a.tip === "VALE")
        : araclar.filter(a => !a.tip || a.tip === "SERVIS");

    if (!Array.isArray(gosterilecekAraclar) || gosterilecekAraclar.length === 0) {
        select.innerHTML = `<option value=''>${valeGorevi ? 'Henüz personel eklenmemiş!' : 'Uygun servis aracı bulunamadı!'}</option>`;
        return;
    }
    // A valet with a task in progress cannot be chosen (one active task per valet).
    select.innerHTML = `<option value="">${valeGorevi ? '-- Seçiniz --' : '-- Araç Seçiniz --'}</option>` +
        gosterilecekAraclar.map(a => {
            const mesgul = valeGorevi && mesgulValeIdleri.has(a.id);
            // Valets are listed by display name, not by the username stored in the virtual
            // vehicle's plate field. Without a display name, fall back to the username. Only the
            // label differs: the option value is still the vehicle id.
            const etiket = valeGorevi ? valeAdi(a.sofor_adi || a.plaka || a.id) : (a.plaka || a.id);
            return `<option value="${a.id}"${mesgul ? ' disabled' : ''}>${etiket}${mesgul ? ' (görevde)' : ''}</option>`;
        }).join('');
    // Restore the previous choice, unless that valet has become busy in the meantime.
    if (oncekiSecim && !(valeGorevi && mesgulValeIdleri.has(oncekiSecim))
        && gosterilecekAraclar.some(a => a.id === oncekiSecim)) select.value = oncekiSecim;
}

async function yolcuOlustur() {
    const ad = document.getElementById('mAd').value.trim();
    let telRaw = document.getElementById('mTel').value.trim();
    const gorev = document.getElementById('mGorev').value;
    const plaka = document.getElementById('mPlaka') ? document.getElementById('mPlaka').value.trim() : "";
    const arac = document.getElementById('mArac').value;
    const fId = localStorage.getItem('aktif_firma_id');
    const btn = document.getElementById('olusturBtn');
    const tel = telRaw ? "+90" + telRaw : "";

    if (!ad) { alert("Lütfen müşteri adını girin!"); return; }
    if (!gorev) { alert("Lütfen görev tipini (Toplama/Dağıtım) seçin!"); return; }
    if (gorev.startsWith('VALE_') && !plaka) { alert("Vale görevlerinde müşteri araç plakası girmek zorunludur!"); return; }
    if (!arac) { alert("Lütfen bir servis aracı seçin!"); return; }
    if (!fId) { alert("Sistem hatası: Firma kimliği bulunamadı."); return; }
    if (telRaw && telRaw.length !== 10) { alert("Telefon numarası başında sıfır olmadan 10 hane olmalıdır!"); return; }

    btn.innerText = "İşleniyor..."; btn.disabled = true;

    try {
        const cevap = await fetch(CONFIG.BASE_URL + "/yeni-talep", {
            method: "POST", headers: getAuthHeaders(),
            body: JSON.stringify({
                musteri_ad: ad, musteri_tel: tel, arac_id: arac,
                gorev_tipi: gorev, firma_id: fId, marka: aktifMarka,
                musteri_plaka: plaka,
                // When started from "Teslim Et", link to the pickup task; that removes the car
                // from the "waiting at service" list.
                iliskili_talep_id: (gorev === 'VALE_TESLIM' ? secilenServistekiId : null)
            })
        });

        if (cevap.status === 401) { cikisYap(); return; }

        if (cevap.ok) {
            const veri = await cevap.json();
            aktifYolcuLinki = veri.link;

            document.getElementById('sonucKutusu').style.display = 'block';
            document.getElementById('uretilenLink').innerText = veri.link;

            const wpMesaj = `Merhaba ${ad}, servis aracınızı takip etmek ve konumunuzu işaretlemek için lütfen linke tıklayın:\n\n${veri.link}`;
            const sifreliMesaj = encodeURIComponent(wpMesaj);
            const telefondaMi = /iPhone|iPad|iPod|Android/i.test(navigator.userAgent);

            const waNumara = tel.replace(/\D/g, ''); // digits only (905XXXXXXXXX), if a number was entered
            const wpTaban = telefondaMi ? 'whatsapp://send' : 'https://web.whatsapp.com/send';
            document.getElementById('wpLink').href = waNumara
                ? `${wpTaban}?phone=${waNumara}&text=${sifreliMesaj}`
                : `${wpTaban}?text=${sifreliMesaj}`;

            document.getElementById('mAd').value = "";
            document.getElementById('mTel').value = "";
            const plakaAlani = document.getElementById('mPlaka');
            if (plakaAlani) plakaAlani.value = "";
            secilenServistekiId = null;   // the link is used; the next request starts fresh
            servistekiAraclariGetir();    // the car now out for delivery leaves the list
        } else {
            const hataVerisi = await cevap.json();
            alert("Kayıt Hatası: " + JSON.stringify(hataVerisi.detail));
        }
    } catch (e) {
        alert("Bağlantı Hatası.");
    } finally {
        btn.innerText = "Sisteme Ekle ve Link Üret"; btn.disabled = false;
    }
}

function linkiKopyala() {
    let kopyalanacakMetin = aktifYolcuLinki || document.getElementById('uretilenLink').innerText;
    if (!kopyalanacakMetin || kopyalanacakMetin === "http://..." || kopyalanacakMetin === "invalid link") {
        alert("Kopyalanacak geçerli bir link bulunamadı."); return;
    }
    if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(kopyalanacakMetin).then(() => alert("Link kopyalandı.")).catch(() => eskiUsulKopyala(kopyalanacakMetin));
    } else {
        eskiUsulKopyala(kopyalanacakMetin);
    }
}

function eskiUsulKopyala(metin) {
    let textArea = document.createElement("textarea");
    textArea.value = metin;
    textArea.className = "kopyalama-textarea";
    document.body.appendChild(textArea);
    textArea.focus(); textArea.select();
    try { document.execCommand('copy'); alert("Link kopyalandı."); } catch (err) { alert("Desteklenmiyor."); }
    document.body.removeChild(textArea);
}

async function listeyiGuncelle() {
    try {
        const token = localStorage.getItem('app_token');
        if (!token) return;

        // Requests, vehicles, routes and departure-time templates in parallel.
        const [yolcularRes, araclarRes, guzergahlarRes, sablonRes] = await Promise.all([
            fetch(`${CONFIG.BASE_URL}/firma-talepleri?firma_id=${firmaId}&marka=${aktifMarka}`, { headers: getAuthHeaders() }),
            fetch(`${CONFIG.BASE_URL}/firma-araclari?firma_id=${firmaId}&marka=${aktifMarka}`, { headers: getAuthHeaders() }),
            fetch(`${CONFIG.BASE_URL}/firma-guzergahlari?firma_id=${firmaId}`, { headers: getAuthHeaders() }),
            fetch(`${CONFIG.BASE_URL}/admin/tum-saat-sablonlari`, { headers: getAuthHeaders() })
        ]);

        if (yolcularRes.status === 401) { cikisYap(); return; }
        // 402: company inactive or deleted. The body is {detail: ...}, not a list, and the
        // .filter() calls below would fail and leave the panel empty.
        if (yolcularRes.status === 402) { cikisYap('firma_pasif'); return; }

        const yolcular = await yolcularRes.json();
        const araclar = await araclarRes.json();
        tumAraclarData = araclar;

        // Busy valets cannot be chosen. The server refuses with 409 anyway; this saves filling in
        // the form for nothing. Tasks carried over from earlier days count too (firma-talepleri
        // returns them).
        mesgulValeIdleri = new Set(
            yolcular.filter(y => (y.gorev_tipi || '').startsWith('VALE_')
                && !(y.durum || '').startsWith('TAMAM') && y.durum !== 'IPTAL_EDILDI')
                .map(y => y.arac_id)
        );

        // Refresh the vehicle dropdown from the same data (a vehicle added by an admin shows up at once).
        aracSecicileriniDoldur(araclar);

        // Routes and templates are optional; fall back to empty lists.
        let guzergahlar = guzergahlarRes.ok ? await guzergahlarRes.json() : [];
        let tumSablonlar = sablonRes && sablonRes.ok ? await sablonRes.json() : [];

        const liste = document.getElementById('yolcuListesi');
        const valeListe = document.getElementById('valeListesi');

        // Shuttle vehicles and valets are in separate sections (valets have no departure time or route).
        const servisAraclar = araclar.filter(a => a.tip !== 'VALE');
        const valeAraclar = araclar.filter(a => a.tip === 'VALE');

        let html = '';

        servisAraclar.forEach(arac => {
            // Open requests of this vehicle.
            const aracinYolculari = yolcular.filter(y => y.arac_id === arac.id && y.durum !== 'MERKEZE DONUS' && !y.durum.startsWith('TAMAM'));

            // Departure time dropdown from this vehicle's templates.
            const aracaOzelSablonlar = tumSablonlar.filter(s => s.arac_id === arac.id);
            let opsiyonlar = `<option value="">Saat Seç</option>`;
            aracaOzelSablonlar.forEach(s => {
                const mevcutSaat = (arac.hareket_saati || '').substring(0, 5);
                const sablonSaat = (s.saat || '').substring(0, 5);
                const secili = mevcutSaat === sablonSaat ? 'selected' : '';
                opsiyonlar += `<option value="${s.saat}" ${secili}>${s.saat}</option>`;
            });

            // Status badge class (colours are in the CSS).
            let durumMetni = arac.durum || "MERKEZDE";
            let durumSinif = "durum-default";
            if (durumMetni === "MERKEZE DÖNÜYOR") { durumSinif = "durum-donus"; }
            else if (durumMetni === "MERKEZDE") { durumSinif = "durum-merkezde"; }
            else if (durumMetni === "ARAÇ DIŞARIDA") { durumSinif = "durum-disarda"; }
            else { durumMetni = "GÖREVDE"; durumSinif = "durum-gorevde"; }

            // Route badge.
            let guzergahMetni = "";
            if (arac.guzergah_id) {
                const hat = guzergahlar.find(g => g.id === arac.guzergah_id);
                if (hat) {
                    guzergahMetni = `<span class="guzergah-rozet">${hat.guzergah_adi}</span>`;
                }
            }

            html += `
            <div class="arac-kutusu">
                <div class="arac-header">
                    <div class="arac-baslik">
                        <b class="arac-plaka">${arac.plaka || arac.id}</b>
                        ${guzergahMetni}
                    </div>
                    <div class="arac-aksiyonlar">
                        <div class="saat-secici-kutu">
                            <select id="saat-${arac.id}" class="saat-select" data-orjinal-saat="${(arac.hareket_saati || '').substring(0, 5)}">
                                ${opsiyonlar}
                            </select>
                            <button class="saat-kaydet-btn" data-aksiyon="saat-kaydet" data-arac-id="${arac.id}">Kaydet</button>
                        </div>
                        <span class="arac-durum-rozet ${durumSinif}">${durumMetni}</span>
                    </div>
                </div>
                <div class="arac-yolcu-listesi">
                    ${aracinYolculari.length === 0 ? '<div class="bos-arac-mesaj">Araçta aktif talep yok.</div>' : ''}
                    ${aracinYolculari.map(y => {
                let yolcuDurumSinif = 'durum-bekliyor';
                if (y.durum === 'KONUM ALINDI') { yolcuDurumSinif = 'durum-konum'; }
                else if (y.durum === 'SERVIS_HAZIR') { yolcuDurumSinif = 'durum-hazir'; }
                else if (y.durum === 'YOLCU ALINDI') { yolcuDurumSinif = 'durum-alindi'; }
                else if (y.durum === 'YOLCU GELMEDİ') { yolcuDurumSinif = 'durum-gelmedi'; }

                return `
                        <div class="yolcu-kayit-satir">
                            <div>
                                <b class="yolcu-ad">${y.musteri_ad}</b>
                                <span class="yolcu-gorev">(${y.gorev_tipi})</span>
                                ${rizaRozeti(y)}
                                <span class="kayit-zaman">Açılış: ${saatTR(y.kayit_tarihi)}</span>
                                ${y.musteri_tel ? `<br><a href="tel:${y.musteri_tel}" class="yolcu-tel">${y.musteri_tel}</a>` : ''}
                            </div>
                            <div class="yolcu-durum-rozet ${yolcuDurumSinif}">${y.durum}</div>
                        </div>`
            }).join('')}
                </div>
            </div>`;
        });

        liste.innerHTML = html || "<div class='bos-liste'>Firmaya ait servis aracı bulunamadı.</div>";

        // Cars waiting at the service center (brought in by a valet, not yet delivered).
        if (firmaValeAktif) servistekiAraclariGetir();

        // Valet section (no departure time or route; the customer's plate is the key detail).
        if (valeListe) {
            let valeHtml = '';
            valeAraclar.forEach(arac => {
                // Open tasks only: completed and cancelled ones are not in the live list.
                const valeGorevleri = yolcular.filter(y => y.arac_id === arac.id
                    && !y.durum.startsWith('TAMAM') && y.durum !== 'IPTAL_EDILDI');

                let durumMetni = arac.durum || "MERKEZDE";
                let durumSinif = durumMetni === "MERKEZDE" ? "durum-merkezde" : "durum-gorevde";
                if (durumMetni !== "MERKEZDE") durumMetni = "GÖREVDE";

                valeHtml += `
            <div class="arac-kutusu">
                <div class="arac-header">
                    <div class="arac-baslik">
                        <b class="arac-plaka vale-ad">${valeAdi(arac.sofor_adi || arac.plaka)}</b>
                    </div>
                    <div class="arac-aksiyonlar">
                        <span class="arac-durum-rozet ${durumSinif}">${durumMetni}</span>
                    </div>
                </div>
                <div class="arac-yolcu-listesi">
                    ${valeGorevleri.length === 0 ? '<div class="bos-arac-mesaj">Aktif vale görevi yok.</div>' : ''}
                    ${valeGorevleri.map(y => `
                        <div class="yolcu-kayit-satir">
                            <div>
                                <b class="yolcu-ad">${y.musteri_ad}</b>
                                <span class="yolcu-gorev">(${y.gorev_tipi === 'VALE_ALIM' ? 'Araç Alım' : 'Araç Teslim'})</span>
                                ${y.musteri_plaka ? `<span class="guzergah-rozet">${y.musteri_plaka}</span>` : ''}
                                ${y.devreden ? '<span class="devreden-rozet">DEVREDEN</span>' : ''}
                                ${rizaRozeti(y)}
                                <span class="kayit-zaman">Açılış: ${saatTR(y.kayit_tarihi)}</span>
                                ${y.musteri_tel ? `<br><a href="tel:${y.musteri_tel}" class="yolcu-tel">${y.musteri_tel}</a>` : ''}
                                ${etapCizelgesi(y)}
                            </div>
                            <div class="yolcu-satir-sag">
                                <div class="yolcu-durum-rozet durum-bekliyor">${valeDurumEtiketi(y.durum, y.gorev_tipi)}</div>
                                ${valeIptalEdilebilirMi(y)
                                    ? `<button class="gorev-iptal-btn" data-aksiyon="vale-gorev-iptal" data-talep-id="${y.id}" data-tanim="${(y.musteri_plaka || y.musteri_ad || '').replace(/"/g, '&quot;')}">İptal</button>`
                                    : ''}
                            </div>
                        </div>`).join('')}
                </div>
            </div>`;
            });
            valeListe.innerHTML = valeHtml || "<div class='bos-liste'>Firmada kayıtlı vale yok.</div>";
        }

        // Completed list. Cancelled valet tasks are listed too, with a badge; otherwise a
        // cancelled task would vanish from every screen.
        const tamamlananlar = yolcular.filter(y => y.durum.startsWith('TAMAM') || y.durum === 'IPTAL_EDILDI');
        const tamamlananListesi = document.getElementById('tamamlananListesi');

        if (tamamlananListesi) {
            if (tamamlananlar.length === 0) {
                tamamlananListesi.innerHTML = "<div class='bos-liste'>Henüz tamamlanan görev yok.</div>";
            } else {
                // Valet tasks are grouped under the valet's name, showing how many each completed.
                // Shuttle requests stay a flat list.
                const valeTamamlananlar = tamamlananlar.filter(y => y.arac_tip === 'VALE');
                const digerTamamlananlar = tamamlananlar.filter(y => y.arac_tip !== 'VALE');

                const gruplar = {};
                valeTamamlananlar.forEach(y => {
                    const ad = valeGorunenAd(y.arac_id, y.arac_plaka);
                    (gruplar[ad] = gruplar[ad] || []).push(y);
                });

                const valeHtml = Object.keys(gruplar).sort().map(ad => `
                    <div class="arac-kutusu" style="margin-bottom:12px;">
                        <div class="arac-header">
                            <div class="arac-baslik"><b class="arac-plaka vale-ad">${ad}</b></div>
                            <div class="arac-aksiyonlar">
                                <span class="arac-durum-rozet durum-merkezde">${gruplar[ad].length} GÖREV</span>
                            </div>
                        </div>
                        <div class="arac-yolcu-listesi">
                            ${gruplar[ad].map(y => tamamlananSatir(y)).join('')}
                        </div>
                    </div>`).join('');

                tamamlananListesi.innerHTML = valeHtml + digerTamamlananlar.map(y => tamamlananSatir(y)).join('');
            }
        }

    } catch (e) {
        console.error("Liste hatası:", e);
        document.getElementById('yolcuListesi').innerHTML = "<div class='bos-liste'>Liste yüklenemedi.</div>";
    }
}

// Cars a valet brought in that have no delivery task yet.
async function servistekiAraclariGetir() {
    const kutu = document.getElementById('servistekiListesi');
    if (!kutu) return;
    try {
        const res = await fetch(`${CONFIG.BASE_URL}/serviste-bekleyen-araclar?firma_id=${firmaId}`, { headers: getAuthHeaders() });
        if (res.status === 401) { cikisYap(); return; }
        if (!res.ok) { kutu.innerHTML = "<div class='bos-liste'>Liste yüklenemedi.</div>"; return; }
        servistekiAraclar = await res.json();

        if (!servistekiAraclar.length) {
            kutu.innerHTML = "<div class='bos-liste'>Serviste bekleyen araç yok.</div>";
            return;
        }

        kutu.innerHTML = servistekiAraclar.map(a => {
            // Arrival at the service center: when the pickup was completed, else when it was opened.
            // Always formatted with saatTR(), never with the device's own time zone.
            const giris = saatTR(a.tamamlanma_tarihi || a.kayit_tarihi);
            return `
            <div class="yolcu-kayit-satir">
                <div>
                    <b class="yolcu-ad">${a.musteri_ad}</b>
                    ${a.musteri_plaka ? `<span class="guzergah-rozet">${a.musteri_plaka}</span>` : ''}
                    ${a.musteri_tel ? `<br><a href="tel:${a.musteri_tel}" class="yolcu-tel">${a.musteri_tel}</a>` : ''}
                    <br><small style="color:#8a8f98">Servise giriş: ${giris}</small>
                </div>
                <div style="display:flex; gap:8px; align-items:center;">
                    <button class="ana-btn" style="padding:8px 14px; font-size:13px;" data-aksiyon="teslim-baslat" data-id="${a.id}">Teslim Et</button>
                    <button class="sil-btn" style="padding:8px 12px; font-size:12px;" data-aksiyon="servisteki-cikar" data-id="${a.id}">Çıkar</button>
                </div>
            </div>`;
        }).join('');
    } catch (e) {
        kutu.innerHTML = "<div class='bos-liste'>Liste yüklenemedi.</div>";
    }
}

// "Teslim Et" (deliver): pre-fill the form with the waiting car's details for a delivery task.
function teslimBaslat(talepId) {
    const arac = servistekiAraclar.find(a => a.id === talepId);
    if (!arac) return;
    secilenServistekiId = talepId;

    document.getElementById('mAd').value = arac.musteri_ad || "";
    document.getElementById('mTel').value = (arac.musteri_tel || "").replace('+90', '');
    document.getElementById('mPlaka').value = arac.musteri_plaka || "";
    const gorev = document.getElementById('mGorev');
    gorev.value = 'VALE_TESLIM';
    gorev.dispatchEvent(new Event('change'));  // switch the dropdown to valets and show the plate field

    document.getElementById('mAd').scrollIntoView({ behavior: 'smooth', block: 'center' });
    alert(`${arac.musteri_plaka} teslim için hazırlandı. Vale seçip "Sisteme Ekle ve Link Üret" deyin.`);
}

// Remove a car from the list when the customer collected it themselves (the pickup stays in reports).
async function servistekiCikar(talepId) {
    const arac = servistekiAraclar.find(a => a.id === talepId);
    if (!confirm(`${arac ? arac.musteri_plaka : 'Bu araç'} listeden çıkarılacak.\n(Müşteri aracını kendisi aldıysa kullanın — vale kaydı raporda kalır.)\n\nOnaylıyor musunuz?`)) return;
    try {
        const res = await fetch(`${CONFIG.BASE_URL}/servisteki-arac-cikar`, {
            method: 'POST', headers: getAuthHeaders(),
            body: JSON.stringify({ talep_id: talepId, sebep: "Müşteri aracını kendisi aldı" })
        });
        if (res.status === 401) { cikisYap(); return; }
        const data = await res.json().catch(() => ({}));
        if (!res.ok) { alert("Çıkarılamadı: " + (data.detail || "")); return; }
        servistekiAraclariGetir();
    } catch (e) { alert("Sunucuya bağlanılamadı."); }
}

// Cancel an active valet task (opened by mistake, or the customer changed their mind). Only
// before the car is taken: the button is only drawn then, and the server refuses with 409.

// Optimistic feedback. A cancel plus the following refresh takes 2-3 seconds of consecutive
// database round trips. Instead of leaving the row unchanged meanwhile (and inviting a second
// click), mark it "cancelling" at once. The server still decides; after the refresh the row
// has moved to the completed list. Uses textContent and style, not innerHTML.
// Returns a function that restores the row if the server refuses.
function satiriIptalEdiliyorIsaretle(talepId, satirSinifi) {
    const btn = document.querySelector(
        `button[data-aksiyon="vale-gorev-iptal"][data-talep-id="${talepId}"]`);
    const satir = btn ? btn.closest(satirSinifi) : null;
    // The list may have been redrawn in the meantime and the button gone; then do nothing.
    if (!btn || !satir) return () => { };

    const eskiMetin = btn.textContent;
    const eskiSaydamlik = satir.style.opacity;
    btn.disabled = true;
    btn.textContent = 'İptal ediliyor...';
    satir.style.opacity = '0.45';

    // Restore on refusal (409: car already taken, 404: no such task), or the row would stay
    // faded as if cancelled.
    return () => {
        btn.disabled = false;
        btn.textContent = eskiMetin;
        satir.style.opacity = eskiSaydamlik;
    };
}

async function valeGorevIptal(talepId, tanim) {
    if (!confirm(`${tanim || 'Bu görev'} iptal edilecek.\n\nMüşteriye gönderilen takip linki geçersiz olur ve vale yeni görev alabilir hale gelir.\n\nOnaylıyor musunuz?`)) return;
    const sebep = prompt("İptal sebebi (isteğe bağlı):", "") || "";
    const geriAl = satiriIptalEdiliyorIsaretle(talepId, '.yolcu-kayit-satir');
    try {
        const res = await fetch(`${CONFIG.BASE_URL}/vale-gorev-iptal`, {
            method: 'POST', headers: getAuthHeaders(),
            body: JSON.stringify({ talep_id: talepId, sebep: sebep })
        });
        if (res.status === 401) { cikisYap(); return; }
        const data = await res.json().catch(() => ({}));
        if (!res.ok) { geriAl(); alert("İptal edilemedi: " + (data.detail || "")); return; }
        listeyiGuncelle();
    } catch (e) { geriAl(); alert("Sunucuya bağlanılamadı."); }
}

// WebSocket: refresh on "YENILE", at most once per second.
let ws = null;
let guncellemeKilit = false;

function canliBaglantiKur() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

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
            listeyiGuncelle();
        } catch (err) {
            console.error('listeyiGuncelle hatası:', err);
        } finally {
            setTimeout(() => { guncellemeKilit = false; }, 1000);
        }
    };
    ws.onclose = () => setTimeout(canliBaglantiKur, 3000);
}

// Last saved departure time per vehicle, to skip saving an unchanged value.
const sonKaydedilenSaat = {};

async function saatKaydet(aracId) {
    const inputEl = document.getElementById(`saat-${aracId}`);
    const saat = inputEl.value;

    // Do not send a request if the time did not change.
    const ilkDeger = inputEl.getAttribute('data-orjinal-saat') ?? '';
    const sonSaat = sonKaydedilenSaat[aracId] ?? ilkDeger;

    if (saat === sonSaat) {
        alert(`Hareket saati zaten ${saat || '(boş)'} olarak ayarlı. Değişiklik yok.`);
        return;
    }

    try {
        const token = localStorage.getItem('app_token');
        if (!token) {
            alert("Oturum süreniz dolmuş, lütfen tekrar giriş yapın.");
            return;
        }

        const res = await fetch(`${CONFIG.BASE_URL}/admin/arac-saat-guncelle`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${token}`
            },
            body: JSON.stringify({ arac_id: aracId, hareket_saati: saat })
        });

        if (res.ok) {
            const sonuc = await res.json().catch(() => ({}));
            if (sonuc.status === 'noop') {
                alert("Saat zaten ayarlı, güncelleme yapılmadı.");
            } else {
                alert("Hareket saati başarıyla kaydedildi ve müşterilere iletildi.");
            }
            sonKaydedilenSaat[aracId] = saat;
            inputEl.setAttribute('data-orjinal-saat', saat);
        } else {
            const hata = await res.json().catch(() => ({ detail: "Yetki hatası" }));
            alert("Saat kaydedilemedi: " + (hata.detail || "Bilinmeyen hata"));
        }
    } catch (e) {
        alert("Bağlantı hatası: Saat güncellenemedi!");
    }
}

// ============================================================
// Event listeners
// ============================================================
document.addEventListener('DOMContentLoaded', () => {
    // Client-side role check, only to show the right screen; the server enforces access.
    if (!firmaId || aktifRol !== 'DANISMAN') {
        document.getElementById('yetkiHata').style.display = 'flex';
        setTimeout(() => { window.location.href = "login.html"; }, 2000);
        return;
    }

    document.getElementById('anaUygulama').style.display = 'block';
    sistemiBaslat();

    const cikisBtn = document.getElementById('cikisBtn');
    if (cikisBtn) cikisBtn.addEventListener('click', () => cikisYap());


    // Changing the task type switches the dropdown between vehicles and valets and shows the
    // plate field for valet tasks.
    const mGorev = document.getElementById('mGorev');
    if (mGorev) {
        mGorev.addEventListener('change', () => {
            aracSecicileriniDoldur(tumAraclarData);
            const plakaGrup = document.getElementById('plakaGrup');
            if (plakaGrup) {
                if (mGorev.value.startsWith('VALE_')) {
                    plakaGrup.style.display = 'block';
                } else {
                    plakaGrup.style.display = 'none';
                }
            }
        });
    }

    const olusturBtn = document.getElementById('olusturBtn');
    if (olusturBtn) olusturBtn.addEventListener('click', yolcuOlustur);

    const kopyalaBtn = document.getElementById('kopyalaBtn');
    if (kopyalaBtn) kopyalaBtn.addEventListener('click', linkiKopyala);

    // Digits only in the phone field.
    const mTel = document.getElementById('mTel');
    if (mTel) {
        mTel.addEventListener('input', function () {
            this.value = this.value.replace(/[^0-9]/g, '');
        });
    }

    // The lists below are redrawn on every refresh, so their buttons are handled by delegation.
    // Departure time "save" buttons in the vehicle list:
    const yolcuListesi = document.getElementById('yolcuListesi');
    if (yolcuListesi) {
        yolcuListesi.addEventListener('click', (e) => {
            const btn = e.target.closest('button[data-aksiyon="saat-kaydet"]');
            if (!btn) return;
            const aracId = btn.dataset.aracId;
            if (aracId) saatKaydet(aracId);
        });
    }

    // Valet task "cancel" buttons:
    const valeListesiEl = document.getElementById('valeListesi');
    if (valeListesiEl) {
        valeListesiEl.addEventListener('click', (e) => {
            const btn = e.target.closest('button[data-aksiyon="vale-gorev-iptal"]');
            if (!btn || !btn.dataset.talepId) return;
            valeGorevIptal(btn.dataset.talepId, btn.dataset.tanim || '');
        });
    }

    // "Deliver" and "remove" buttons in the waiting-at-service list:
    const servistekiListesi = document.getElementById('servistekiListesi');
    if (servistekiListesi) {
        servistekiListesi.addEventListener('click', (e) => {
            const btn = e.target.closest('button[data-aksiyon]');
            if (!btn || !btn.dataset.id) return;
            if (btn.dataset.aksiyon === 'teslim-baslat') teslimBaslat(btn.dataset.id);
            else if (btn.dataset.aksiyon === 'servisteki-cikar') servistekiCikar(btn.dataset.id);
        });
    }

    canliBaglantiKur();
});

document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === 'visible') {
        canliBaglantiKur();
        if (typeof listeyiGuncelle === "function") listeyiGuncelle();
    }
});