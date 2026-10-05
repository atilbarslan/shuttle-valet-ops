// operasyon.html: the operations panel. Live view of every vehicle and valet with their open
// requests, completed work, departure times, passenger transfers and task cancellation.
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
                    ADD_ATTR: ['target', 'rel', 'value', 'placeholder', 'disabled', 'data-aksiyon', 'data-arac-id', 'data-token', 'data-yolcu-ad', 'data-sablon-id', 'data-firma-id', 'data-orjinal-saat', 'data-talep-id', 'data-tanim'],
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

function getAuthHeaders() {
    return {
        "Authorization": `Bearer ${localStorage.getItem('app_token')}`,
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

let tumAraclar = [];

// A valet's virtual vehicle stores the valet's username in its "plaka" field. Older records
// have a "Vale " prefix, which is stripped so the panel does not repeat the word.
function valeAdi(plaka) {
    return (plaka || '').replace(/^Vale\s+/i, '') || 'Vale';
}

// Name to show for a valet (same logic as danisman.js): the display name from the vehicle list
// (firma-araclari -> sofor_adi). A retired valet has no user row, so fall back to the virtual
// vehicle's plate, which is the valet's username.
function valeGorunenAd(aracId, yedekPlaka) {
    const arac = (tumAraclar || []).find(a => a.id === aracId);
    return valeAdi((arac && arac.sofor_adi) || yedekPlaka);
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

// Must match VALE_IPTAL_EDILEBILIR_DURUMLAR in main.py exactly (danisman.js has the same copy).
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

// Date and time for display, always in Istanbul time (the server stores UTC). Copied in
// danisman.js, admin.js and vale.js; keep the copies identical.
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
// "No consent" badge. Identical to the copy in danisman.js; keep them in sync. If the customer
// refused or withdrew consent there is no point waiting for a location, so staff should call.
function rizaRozeti(y) {
    if (!y || y.riza_reddedildi !== true) return '';
    return '<span class="riza-rozet" title="Müşteri konum onayı vermedi veya onayını geri çekti. Konum alınamaz — telefonla iletişime geçin.">ONAY YOK</span>';
}

function valeDurumu(durum) {
    return durum === 'ARAC_ALINDI' || Object.prototype.hasOwnProperty.call(VALE_DURUM_ETIKET, durum);
}
function valeDurumEtiketi(durum, gorevTipi) {
    if (durum === 'ARAC_ALINDI') return gorevTipi === 'VALE_TESLIM' ? 'Servisten Çıktı' : 'Araç Alındı';
    return VALE_DURUM_ETIKET[durum] || (durum || '').replace(/_/g, ' ');
}

// ============================================================
// Task milestone timeline (promised vs actual times)
// ============================================================
// Identical to the copy in danisman.js; change both together. The data comes from the
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
// the advisor panel creates tasks and does not evaluate staff.
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
    const basariliMi = ['TAMAM_ALINDI', 'TAMAM_SERVIS', 'TAMAM_MUSTERI'].includes(y.durum);
    const iptalMi = y.durum === 'IPTAL_EDILDI';
    const valeMi = y.gorev_tipi && y.gorev_tipi.startsWith('VALE_');
    const gorevMetni = y.gorev_tipi === 'VALE_ALIM' ? 'Araç Alım'
        : y.gorev_tipi === 'VALE_TESLIM' ? 'Araç Teslim' : (y.gorev_tipi || 'Görev');
    const detay = (valeMi && y.musteri_plaka)
        ? `Müşteri Plaka: <b style="color:var(--orange)">${y.musteri_plaka}</b>`
        : `Araç: <b class="tamamlanan-arac">${y.arac_plaka || 'Bilinmiyor'}</b>`;
    return `
                        <div class="tamamlanan-satir">
                            <div class="tamamlanan-bilgi-kutu">
                                <div class="tamamlanan-ad">${y.musteri_ad}</div>
                                <div class="yolcu-alt">
                                    <span>${gorevMetni}</span>
                                    <span class="tamamlanan-arac-bilgi">• ${detay}</span>
                                </div>
                                <div class="tamamlanan-zaman">${saatTR(y.kayit_tarihi)} → ${saatTR(y.tamamlanma_tarihi)}</div>
                                ${etapCizelgesi(y, true)}
                            </div>
                            <div class="tamamlanan-rozet ${iptalMi ? 'tamamlanan-iptal' : (basariliMi ? 'tamamlanan-basarili' : 'tamamlanan-basarisiz')}">
                                ${iptalMi ? 'İPTAL' : (basariliMi ? 'BAŞARILI' : 'GELMEDİ')}
                            </div>
                        </div>`;
}

async function sahaVerileriniCek() {
    try {
        const [araclarRes, taleplerRes] = await Promise.all([
            fetch(`${CONFIG.BASE_URL}/firma-araclari?firma_id=${firmaId}&marka=${aktifMarka}`, { headers: getAuthHeaders() }),
            fetch(`${CONFIG.BASE_URL}/firma-talepleri?firma_id=${firmaId}&marka=${aktifMarka}`, { headers: getAuthHeaders() })
        ]);
        if (araclarRes.status === 401) { await appCikis(); window.location.href = "login.html"; return; }
        // 402: company inactive or deleted. The body is not a list, so without this the panel
        // would just stay empty.
        if (araclarRes.status === 402) { await appCikis(); window.location.href = "login.html?sebep=firma_pasif"; return; }

        tumAraclar = await araclarRes.json();
        const talepler = await taleplerRes.json();

        let gorevdeSayisi = 0, beklemedeSayisi = 0, toplamYolcu = 0;

        const grid = document.getElementById('aracGrid');
        const valeGrid = document.getElementById('valeGrid');
        if (grid) grid.innerHTML = '';
        if (valeGrid) valeGrid.innerHTML = '';

        let htmlArac = '';
        let htmlVale = '';

        tumAraclar.forEach(arac => {
            // Open requests only. Cancelled valet tasks appear under "completed" with a cancelled badge.
            const yolcular = talepler.filter(t => t.arac_id === arac.id && t.durum !== 'MERKEZE DONUS'
                && (!t.durum || !t.durum.startsWith('TAMAM')) && t.durum !== 'IPTAL_EDILDI');

            let durumMetni = arac.durum || "MERKEZDE";
            let durumSinif = '';
            let badgeSinif = '';
            let durumYazi = '';

            if (durumMetni === "MERKEZE DÖNÜYOR") {
                durumSinif = 'donuyor'; badgeSinif = 'badge-donuyor'; durumYazi = 'Dönüyor';
            } else if (durumMetni === "MERKEZDE") {
                durumSinif = 'merkezde'; badgeSinif = 'badge-merkezde'; durumYazi = 'Merkezde';
            } else if (durumMetni === "ARAÇ DIŞARIDA") {
                durumSinif = 'disarida'; badgeSinif = 'badge-disarida'; durumYazi = 'Dışarıda';
            } else {
                durumMetni = "GÖREVDE"; durumSinif = 'gorevde'; badgeSinif = 'badge-gorevde'; durumYazi = 'Görevde';
            }

            // rota_aktif is shuttle only (set by rota-baslat/rota-bitir); valets never set it and
            // only change durum. So the "on duty" counter also looks at durum, otherwise busy
            // valets would be counted as idle. For shuttle the result is the same, since
            // rota-baslat sets both.
            if (arac.rota_aktif || durumMetni === "GÖREVDE" || durumMetni === "MERKEZE DÖNÜYOR") gorevdeSayisi++;
            else beklemedeSayisi++;

            toplamYolcu += yolcular.length;

            let yolcuHtml = yolcular.map(y => {
                let durum = y.durum;
                let durumSinifYolcu = 'd-bekliyor';
                if (durum === 'YOLCU ALINDI') { durum = 'Araçta'; durumSinifYolcu = 'd-alindi'; }
                else if (durum === 'SERVIS_HAZIR') { durum = 'Hazır'; durumSinifYolcu = 'd-hazir'; }
                else if (durum === 'KONUM ALINDI') { durum = 'Konum Var'; }
                else if (durum === 'BEKLİYOR') { durum = 'Bekliyor'; }
                else if (valeDurumu(y.durum)) {
                    durum = valeDurumEtiketi(y.durum, y.gorev_tipi);
                    if (y.durum === 'VALE_YOLDA' || y.durum === 'ARAC_ALINDI') durumSinifYolcu = 'd-alindi';
                }

                // Escape double quotes for use inside a data attribute.
                const yolcuAdiSafe = (y.musteri_ad || '').replace(/"/g, '&quot;');

                const devretBtn = ['BEKLİYOR', 'KONUM ALINDI', 'SERVIS_HAZIR'].includes(y.durum)
                    ? `<button class="devret-btn" data-aksiyon="modal-ac" data-token="${y.token}" data-yolcu-ad="${yolcuAdiSafe}" data-arac-id="${arac.id}">Devret</button>`
                    : '';

                const durakBilgisi = y.durak_adi ? `<span class="durak-rozet">${y.durak_adi}</span>` : '';

                // Valet task cancel button, only before the car has been taken (the server checks too).
                const iptalBtn = valeIptalEdilebilirMi(y)
                    ? `<button class="gorev-iptal-btn" data-aksiyon="vale-gorev-iptal" data-talep-id="${y.id}" data-tanim="${(y.musteri_plaka || y.musteri_ad || '').replace(/"/g, '&quot;')}">İptal</button>`
                    : '';

                return `
                            <div class="yolcu-satir">
                                <div>
                                    <div class="yolcu-bilgi">${y.musteri_ad} ${durakBilgisi}${y.devreden ? '<span class="devreden-rozet">DEVREDEN</span>' : ''}${rizaRozeti(y)}</div>
                                    <div class="yolcu-alt">
                                        <span>${y.gorev_tipi === 'VALE_ALIM' ? 'Araç Alım' : y.gorev_tipi === 'VALE_TESLIM' ? 'Araç Teslim' : (y.gorev_tipi || 'Görev')}</span>
                                        ${y.musteri_plaka ? `<span class="plaka-rozet">${y.musteri_plaka}</span>` : ''}
                                        ${y.musteri_tel ? `<a href="tel:${y.musteri_tel}" class="yolcu-tel-link">${y.musteri_tel}</a>` : ''}
                                        <span class="kayit-zaman">Açılış: ${saatTR(y.kayit_tarihi)}</span>
                                    </div>
                                    ${etapCizelgesi(y, true)}
                                </div>
                                <div class="yolcu-sag-grup">
                                    <span class="yolcu-durum ${durumSinifYolcu}">${durum}</span>
                                    ${devretBtn}
                                    ${iptalBtn}
                                </div>
                            </div>`;
            }).join('');

            if (!yolcular.length) {
                yolcuHtml = `<div class="bos-mesaj">${arac.tip === 'VALE' ? 'Aktif vale görevi yok' : 'Aktif yolcu yok'}</div>`;
            }

            // Emergency "end trip" button, only while a trip is running.
            const acilBtn = arac.rota_aktif
                ? `<div class="kart-footer"><button class="acil-btn" data-aksiyon="acil-rota-bitir" data-arac-id="${arac.id}">Rotayı Sonlandır</button></div>`
                : '';

            const hatBilgisi = arac.guzergah_adi ? `<div class="hat-bilgisi">Hat: ${arac.guzergah_adi}</div>` : '';

            let saatKontrolleri = '';
            if (arac.tip !== 'VALE') {
                saatKontrolleri = `
                                        <div class="saat-input-kutu">
                                            <input type="time" id="saat-${arac.id}" value="${(arac.hareket_saati || '').substring(0, 5)}" data-orjinal-saat="${(arac.hareket_saati || '').substring(0, 5)}" class="saat-input">
                                            <button class="saat-kaydet-btn" data-aksiyon="saat-kaydet" data-arac-id="${arac.id}">Kaydet</button>
                                        </div>

                                        <button class="sablon-yonet-btn" data-aksiyon="sablon-yonet" data-arac-id="${arac.id}" title="Saat Şablonlarını Yönet"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"></circle><line x1="12" y1="12" x2="12" y2="8"></line><line x1="12" y1="12" x2="15" y2="13"></line></svg></button>`;
            }

            let kartHtml = `
                        <div class="arac-kart ${durumSinif}">
                            <div class="arac-kart-header arac-kart-header-flex">
                                <div class="arac-kart-baslik">
                                    <h3 class="arac-plaka-baslik">${arac.tip === 'VALE' ? valeAdi(arac.sofor_adi || arac.plaka) : arac.plaka}</h3>
                                    <div class="sofor-bilgi">
                                        ${arac.tip === 'VALE'
                                            /* valet card: the name is already the title, so only show the phone */
                                            ? (arac.sofor_tel ? `<a href="tel:${arac.sofor_tel}" class="yolcu-tel-link">${arac.sofor_tel}</a>` : (arac.sofor_adi ? '' : 'Atanmamış'))
                                            : `${arac.sofor_adi ? arac.sofor_adi : 'Şoför Atanmamış'}${arac.sofor_tel ? ` · <a href="tel:${arac.sofor_tel}" class="yolcu-tel-link">${arac.sofor_tel}</a>` : ''}`}
                                    </div>
                                    ${hatBilgisi}
                                </div>
                                <div class="arac-kart-sag">
                                    <span class="durum-badge ${badgeSinif}">${durumYazi}</span>
                                    <div class="saat-aksiyon-grup">
                                        ${saatKontrolleri}
                                    </div>
                                </div>
                            </div>
                            <div class="yolcu-liste">${yolcuHtml}</div>
                            ${acilBtn}
                        </div>`;

            if (arac.tip === 'VALE') {
                htmlVale += kartHtml;
            } else {
                htmlArac += kartHtml;
            }
        });

        if (grid) grid.innerHTML = htmlArac || '<div class="araclar-yukleniyor" style="grid-column: 1/-1;">Aktif araç bulunmuyor.</div>';
        if (valeGrid) valeGrid.innerHTML = htmlVale || '<div class="araclar-yukleniyor" style="grid-column: 1/-1;">Aktif vale bulunmuyor.</div>';

        // Summary counters. sayiYaz writes a number and removes the pulsing "loading" style the
        // counters start with (a plain "-" used to read as "nothing here").
        const sayiYaz = (id, deger) => {
            const el = document.getElementById(id);
            if (!el) return;
            el.innerText = deger;
            el.classList.remove('sayi-yukleniyor');
        };
        sayiYaz('ozGorevde', gorevdeSayisi);

        // Completed list. Cancelled valet tasks are listed too, with a badge, so no record
        // disappears from every screen.
        const tamamlananlar = talepler.filter(t => t.durum && (t.durum.startsWith('TAMAM') || t.durum === 'IPTAL_EDILDI'));
        const tamamlananListesi = document.getElementById('tamamlananListesi');

        if (tamamlananlar.length === 0) {
            tamamlananListesi.innerHTML = "<div class='bos-mesaj'>Henüz tamamlanan görev yok.</div>";
        } else {
            // Valet tasks are grouped under the valet's name, showing at a glance how many each
            // valet completed. Shuttle requests stay a flat list.
            const valeler = tamamlananlar.filter(y => y.arac_tip === 'VALE');
            const digerler = tamamlananlar.filter(y => y.arac_tip !== 'VALE');

            const gruplar = {};
            valeler.forEach(y => {
                const ad = valeGorunenAd(y.arac_id, y.arac_plaka);
                (gruplar[ad] = gruplar[ad] || []).push(y);
            });

            const valeHtml = Object.keys(gruplar).sort().map(ad => `
                        <div class="vale-tamamlanan-grup">
                            <div class="vale-grup-basligi">
                                <span class="vale-grup-ad">${ad}</span>
                                <span class="vale-grup-sayi">${gruplar[ad].length} görev tamamladı</span>
                            </div>
                            ${gruplar[ad].map(tamamlananSatir).join('')}
                        </div>`).join('');

            tamamlananListesi.innerHTML = valeHtml + digerler.map(tamamlananSatir).join('');
        }

        sayiYaz('ozBeklemede', beklemedeSayisi);
        sayiYaz('ozYolcu', toplamYolcu);

        // The company name and module flags come from the single firma-detay call at start-up.

    } catch (e) {
        console.error(e);
        // On error, do not leave the counters pulsing as if still loading; show "-" instead.
        ['ozGorevde', 'ozBeklemede', 'ozYolcu'].forEach(id => {
            const el = document.getElementById(id);
            if (el) { el.innerText = '-'; el.classList.remove('sayi-yukleniyor'); }
        });
    }
}

// Transfer dialog: move a passenger who has not boarded yet to another vehicle.
function modalAc(token, yolcuAd, mevcutAracId) {
    document.getElementById('devretToken').value = token;
    document.getElementById('devretYolcuAdi').innerText = yolcuAd;
    const digerAraclar = tumAraclar.filter(a => a.id !== mevcutAracId);
    document.getElementById('yeniAracSec').innerHTML = digerAraclar.map(a =>
        `<option value="${a.id}">${a.plaka}${a.sofor_adi ? ' — ' + a.sofor_adi : ''}</option>`
    ).join('');
    document.getElementById('devretModal').style.display = 'flex';
}
function modalKapat() { document.getElementById('devretModal').style.display = 'none'; }

async function aktarimiOnayla() {
    const token = document.getElementById('devretToken').value;
    const yeniArac = document.getElementById('yeniAracSec').value;
    if (!yeniArac) return alert("Lütfen bir araç seçin.");
    try {
        const res = await fetch(`${CONFIG.BASE_URL}/yolcu-devret?token=${token}&yeni_arac_id=${yeniArac}`, {
            method: 'POST', headers: getAuthHeaders()
        });
        if (res.ok) {
            modalKapat();
        } else {
            const err = await res.json(); alert("Hata: " + err.detail);
        }
    } catch (e) { alert("Sunucuya ulaşılamadı."); }
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
    if (!talepId) return;
    if (!confirm(`${tanim || 'Bu görev'} iptal edilecek.\n\nMüşteriye gönderilen takip linki geçersiz olur ve vale yeni görev alabilir hale gelir.\n\nOnaylıyor musunuz?`)) return;
    const sebep = prompt("İptal sebebi (isteğe bağlı):", "") || "";
    const geriAl = satiriIptalEdiliyorIsaretle(talepId, '.yolcu-satir');
    try {
        const res = await fetch(`${CONFIG.BASE_URL}/vale-gorev-iptal`, {
            method: 'POST', headers: getAuthHeaders(),
            body: JSON.stringify({ talep_id: talepId, sebep: sebep })
        });
        if (res.status === 401) { cikisYap(); return; }
        const data = await res.json().catch(() => ({}));
        if (!res.ok) { geriAl(); alert("İptal edilemedi: " + (data.detail || "")); return; }
        sahaVerileriniCek();
    } catch (e) { geriAl(); alert("Sunucuya bağlanılamadı."); }
}

// Emergency end of a shuttle trip from the panel.
async function acilRotaBitir(aracId) {
    if (!confirm("Bu aracın rotasını sonlandırmak istediğinize emin misiniz?")) return;
    try {
        const res = await fetch(`${CONFIG.BASE_URL}/rota-bitir?arac_id=${aracId}`, {
            method: 'POST', headers: getAuthHeaders()
        });
        if (!res.ok) alert("Hata oluştu.");
    } catch (e) { alert("Bağlantı hatası."); }
}

// WebSocket: refresh on "YENILE", at most once per second.
let ws = null, guncellemeKilit = false;
function canliBaglantiKur() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

    const wsUrl = CONFIG.BASE_URL.replace(/^http/, 'ws') + '/ws';
    ws = new WebSocket(wsUrl);

    ws.onopen = function () {
        let wsToken = localStorage.getItem('app_token');
        if (!wsToken) {
            wsToken = new URLSearchParams(window.location.search).get('token');
        }
        if (wsToken) ws.send(JSON.stringify({ token: wsToken }));
    };

    ws.onmessage = function (event) {
        if (event.data !== "YENILE") return;
        if (guncellemeKilit) return;
        guncellemeKilit = true;
        try {
            sahaVerileriniCek();
        } catch (err) {
            console.error('sahaVerileriniCek hatası:', err);
        } finally {
            setTimeout(() => { guncellemeKilit = false; }, 1000);
        }
    };
    ws.onclose = () => setTimeout(canliBaglantiKur, 3000);
}

document.addEventListener('visibilitychange', () => {
    if (!document.hidden) sahaVerileriniCek();
});

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

async function saatSablonYonetimi(aracId) {
    const token = localStorage.getItem('app_token');
    if (!token) return alert("Oturum süreniz dolmuş.");

    // Company id from the token payload.
    let modalFirmaId = null;
    try { modalFirmaId = JSON.parse(atob(token.split('.')[1])).firma_id; } catch (e) { }

    const res = await fetch(`${CONFIG.BASE_URL}/admin/arac-saat-sablonlari/${aracId}`, {
        headers: { 'Authorization': `Bearer ${token}` }
    });
    const sablonlar = await res.json();

    // Build the dialog (CSS classes, no inline styles).
    const modal = document.createElement('div');
    modal.id = 'sablonModal';
    modal.className = 'sablon-modal-overlay';

    let listeHTML = sablonlar.map(s => `
                <div class="sablon-satir">
                    <span class="sablon-saat">${s.saat}</span>
                    <button class="sablon-sil-btn" data-aksiyon="sablon-sil" data-sablon-id="${s.id}" data-arac-id="${aracId}">Sil</button>
                </div>
            `).join('');

    if (sablonlar.length === 0) listeHTML = '<div class="sablon-bos-mesaj">Henüz kayıtlı saat şablonu yok.</div>';

    modal.innerHTML = `
                <div class="sablon-modal-kutu">
                    <div class="sablon-modal-header">
                        <h3 class="sablon-modal-baslik">Saat Şablonları</h3>
                        <button class="sablon-modal-kapat-btn" data-aksiyon="sablon-modal-kapat">&times;</button>
                    </div>
                    <div class="sablon-modal-liste">
                        ${listeHTML}
                    </div>
                    <div class="sablon-modal-footer">
                        <input type="time" id="yeniSablonSaati" class="sablon-saat-input">
                        <button class="sablon-ekle-btn" data-aksiyon="sablon-ekle" data-arac-id="${aracId}" data-firma-id="${modalFirmaId}">Ekle</button>
                    </div>
                </div>
            `;
    document.body.appendChild(modal);
}

async function sablonEkle(aracId) {
    const saat = document.getElementById('yeniSablonSaati').value;
    if (!saat) return alert("Lütfen bir saat seçin.");

    const token = localStorage.getItem('app_token');
    // No firma_id in the body: the server takes it from the token.
    await fetch(`${CONFIG.BASE_URL}/admin/arac-saat-sablonlari`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
        body: JSON.stringify({ arac_id: aracId, saat: saat })
    });
    document.getElementById('sablonModal').remove();
    saatSablonYonetimi(aracId);
}

async function sablonSil(sablonId, aracId, btn) {
    btn.innerText = '...';
    const token = localStorage.getItem('app_token');
    await fetch(`${CONFIG.BASE_URL}/admin/arac-saat-sablonlari/${sablonId}`, {
        method: 'DELETE',
        headers: { 'Authorization': `Bearer ${token}` }
    });
    document.getElementById('sablonModal').remove();
    saatSablonYonetimi(aracId);
}

// ============================================================
// Event listeners
// ============================================================
document.addEventListener('DOMContentLoaded', () => {
    // Client-side check, only to show the right screen; the server enforces access. A valid
    // company id is required: SUPERADMIN has none (stored as the string "null"), so it cannot
    // use this page.
    if (!firmaId || firmaId === 'null' || firmaId === 'undefined' || !['OPERASYON', 'ADMIN'].includes(aktifRol)) {
        alert("Bu sayfaya erişim yetkiniz yok.");
        window.location.href = "login.html";
        return;
    }

    // Show or hide the shuttle and valet sections from the flags stored at login first, so
    // sections do not flash on screen, then confirm with firma-detay.
    const modulUIUygula = (shuttleAktif, valeAktif) => {
        const goster = (id, acik) => { const el = document.getElementById(id); if (el) el.style.display = acik ? '' : 'none'; };
        goster('servisAracBaslik', shuttleAktif); goster('aracGrid', shuttleAktif);
        goster('valeBaslik', valeAktif); goster('valeGrid', valeAktif);
        // The counter counts open requests: valet tasks in a valet-only company, passengers in a
        // shuttle-only one, both in a mixed one. Label it accordingly.
        const etiket = document.getElementById('ozYolcuEtiket');
        if (etiket) etiket.innerText = !shuttleAktif ? 'Aktif Vale Görevi'
            : (valeAktif ? 'Aktif Görev' : 'Aktif Yolcu');
    };
    modulUIUygula(localStorage.getItem('aktif_shuttle_aktif') !== '0',
                  localStorage.getItem('aktif_vale_aktif') === '1');

    // One firma-detay call provides both the title and the module flags.
    fetch(`${CONFIG.BASE_URL}/firma-detay/${firmaId}`, { headers: getAuthHeaders() })
        .then(r => r.json()).then(d => {
            const baslik = document.getElementById('firmaAdiBaslik');
            if (baslik && d.firma_adi) baslik.innerText = d.firma_adi + ' — Operasyon';
            modulUIUygula(d.shuttle_aktif !== false, !!d.vale_aktif);
        }).catch(() => { });

    const cikisBtn = document.getElementById('cikisBtn');
    if (cikisBtn) cikisBtn.addEventListener('click', () => cikisYap());

    const onaylaBtn = document.getElementById('aktarimOnaylaBtn');
    if (onaylaBtn) onaylaBtn.addEventListener('click', aktarimiOnayla);

    const modalIptalBtn = document.getElementById('modalIptalBtn');
    if (modalIptalBtn) modalIptalBtn.addEventListener('click', modalKapat);

    // Vehicle and valet cards are redrawn on every refresh, so their buttons are handled by
    // delegation. Valet cards live in #valeGrid, a sibling of #aracGrid rather than a child, so
    // the same handler has to be attached to both containers.
    const kartTiklamasi = (e) => {
        const btn = e.target.closest('button[data-aksiyon]');
        if (!btn) return;

        const aksiyon = btn.dataset.aksiyon;
        const aracId = btn.dataset.aracId;

        if (aksiyon === 'modal-ac') {
            modalAc(btn.dataset.token, btn.dataset.yolcuAd, aracId);
        } else if (aksiyon === 'acil-rota-bitir') {
            acilRotaBitir(aracId);
        } else if (aksiyon === 'saat-kaydet') {
            saatKaydet(aracId);
        } else if (aksiyon === 'sablon-yonet') {
            saatSablonYonetimi(aracId);
        } else if (aksiyon === 'vale-gorev-iptal') {
            valeGorevIptal(btn.dataset.talepId, btn.dataset.tanim || '');
        }
    };
    ['aracGrid', 'valeGrid'].forEach(id => {
        const kap = document.getElementById(id);
        if (kap) kap.addEventListener('click', kartTiklamasi);
    });

    // The template dialog is appended to body, so its buttons are handled on document.
    document.addEventListener('click', (e) => {
        const btn = e.target.closest('button[data-aksiyon]');
        if (!btn) return;

        const aksiyon = btn.dataset.aksiyon;

        if (aksiyon === 'sablon-modal-kapat') {
            const modal = document.getElementById('sablonModal');
            if (modal) modal.remove();
        } else if (aksiyon === 'sablon-ekle') {
            sablonEkle(btn.dataset.aracId);
        } else if (aksiyon === 'sablon-sil') {
            sablonSil(btn.dataset.sablonId, btn.dataset.aracId, btn);
        }
    });

    sahaVerileriniCek();
    canliBaglantiKur();
});