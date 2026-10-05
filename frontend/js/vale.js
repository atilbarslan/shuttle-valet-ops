// vale.html: the valet's PWA. Shows the valet's single current task and moves it through its
// statuses. Works offline: actions are queued and sent when the connection returns.

// Every assignment to innerHTML on this page goes through DOMPurify. Task cards are built from
// customer-entered text (name, plate), so this keeps markup in that text from executing.
// The extra allowances are for the icons, tel: links and map links the cards use.
(function () {
    const orijinalSetter = Object.getOwnPropertyDescriptor(Element.prototype, 'innerHTML').set;
    Object.defineProperty(Element.prototype, 'innerHTML', {
        set: function (html) {
            const temiz = (typeof DOMPurify !== 'undefined' && typeof html === 'string')
                ? DOMPurify.sanitize(html, {
                    ADD_ATTR: ['target', 'rel', 'href'],
                    ADD_TAGS: ['svg', 'path', 'a'],
                    ALLOWED_URI_REGEXP: /^(?:(?:https?|mailto|tel|geo|maps):|[^a-z]|[a-z+.\-]+(?:[^a-z+.\-:]|$))/i
                })
                : html;
            orijinalSetter.call(this, temiz);
        },
        get: Object.getOwnPropertyDescriptor(Element.prototype, 'innerHTML').get
    });
})();

const token = localStorage.getItem('app_token');
let aracId = null; // read from the JWT payload in baslat()
let guncelGorev = null;
// Every localStorage key must start with "app_": appCikis() clears keys by that
// prefix at logout, which is how customer data cached here is removed from the phone.
let cevrimdisiKuyruk = JSON.parse(localStorage.getItem('app_vale_kuyruk') || '[]');

function baslat() {
    if (!token) {
        window.location.href = "login.html";
        return;
    }
    
    try {
        const payload = JSON.parse(atob(token.split('.')[1]));
        // The badge shows the brand/department, and only when it is a specific one.
        const rozet = document.getElementById('aracGosterge');
        if (payload.marka && payload.marka !== "Genel") {
            rozet.innerText = payload.marka;
        } else {
            rozet.style.display = 'none';
        }
        aracId = payload.arac_id;
    } catch(e) {}
    
    // appCikis() revokes the token and clears every app_* key, including the cached
    // task and queue with the customer's name, phone, plate and location.
    document.getElementById('cikisBtn').addEventListener('click', async () => {
        await window.appCikis();
        window.location.href = "login.html";
    });

    // Buttons are handled by delegation on data-aksiyon attributes. Inline onclick would not
    // work: the CSP forbids inline scripts and DOMPurify strips on* attributes anyway.
    document.getElementById('icerikGövdesi').addEventListener('click', (e) => {
        const btn = e.target.closest('[data-aksiyon="durum"]');
        if (btn) { durumGuncelle(btn.dataset.talep, btn.dataset.durum); return; }
        const navBtn = e.target.closest('[data-aksiyon="navigasyon"]');
        if (navBtn) navigasyonAc(navBtn.dataset.lat, navBtn.dataset.lng);
    });

    konumuTazele();   // ask for location permission and a first fix now, not at the first action
    canliBaglantiKur();
    goreviGetir();
    
    setTimeout(() => {
        const loader = document.getElementById('px-loading');
        if (loader) {
            loader.style.opacity = '0';
            setTimeout(() => loader.style.display = 'none', 300);
        }
    }, 600);
}

function getAuthHeaders() {
    return { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' };
}

// Open turn-by-turn navigation in Apple Maps on iOS and Google Maps elsewhere (same as sofor.js).
// Must be called synchronously inside the user's click; see durumGuncelle.
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

/** Show a short message, reusing the offline toast element. */
function toastGoster(mesaj, sure = 5000) {
    const toast = document.getElementById('offlineToast');
    if (!toast) return;
    toast.innerText = mesaj;
    toast.style.display = 'block';
    setTimeout(() => { toast.style.display = 'none'; }, sure);
}

// Statuses in which the customer's address is still needed, per task type. The server erases
// the address as soon as it is no longer needed (data minimisation):
//   pickup:   until the car is taken; at ARAC_ALINDI the valet heads to the service center.
//   delivery: until the hand-over (erased at TAMAM_MUSTERI).
const KONUM_GEREKEN_DURUMLAR = {
    'VALE_ALIM': ['KONUM_ALINDI_VALE', 'VALE_YOLDA'],
    'VALE_TESLIM': ['KONUM_ALINDI_VALE', 'ARAC_ALINDI', 'VALE_YOLDA']
};

/** True if the address is missing while it is still needed (the customer may have withdrawn
 *  consent). A missing location alone is not enough, because the normal flow also erases it
 *  (right after a pickup), and a warning there would be a false alarm. */
function konumImhaEdilmisMi(gorev) {
    if (!gorev || gorev.konum_lat != null) return false;
    return (KONUM_GEREKEN_DURUMLAR[gorev.gorev_tipi] || []).includes(gorev.durum);
}

/**
 * Where navigation should open for this status change, or null.
 *  pickup:   VALE_YOLDA -> the customer; ARAC_ALINDI -> back to the service center
 *  delivery: ARAC_ALINDI -> none (the valet is still at the service center);
 *            VALE_YOLDA -> the customer
 */
function navigasyonHedefi(yeniDurum) {
    if (!guncelGorev) return null;
    const alim = guncelGorev.gorev_tipi === 'VALE_ALIM';
    if (yeniDurum === 'VALE_YOLDA' && guncelGorev.konum_lat != null) {
        return { lat: guncelGorev.konum_lat, lng: guncelGorev.konum_lng };   // to the customer, both types
    }
    if (yeniDurum === 'ARAC_ALINDI' && alim && guncelGorev.merkez_lat != null) {
        return { lat: guncelGorev.merkez_lat, lng: guncelGorev.merkez_lng }; // back to the service center
    }
    return null;
}

// The position is refreshed in the background and nothing ever waits for it (same as
// sofor.js). Awaiting GPS before an action would delay the screen and, worse, would open
// navigation after an await, outside the user's click, so the map would open inside the PWA.
let sonKonum = null;

// Cold start. sonKonum is null when the app opens, and the first GPS fix arrives 1-8 seconds
// later, so the first request goes out without a position. If the valet then taps navigation,
// the PWA goes to the background and the 30-second poll stops (it only runs while visible),
// so no position is written for that whole leg and the customer keeps seeing a stale arrival
// time. That first position is the most valuable one. This flag records that a request went
// out without a position; when the first fix arrives the request is repeated once. Nothing
// waits for the fix; it only costs one extra request.
let konumsuzIstekYapildi = false;

function konumuTazele() {
    if (!navigator.geolocation) return;
    navigator.geolocation.getCurrentPosition(
        (pos) => {
            // Keep the reading's age and accuracy too. A button press sends this cached reading
            // rather than a fresh one (the click must not wait), and the punctuality report needs
            // to know how reliable it was before flagging a press as "made remotely".
            sonKonum = {
                lat: pos.coords.latitude,
                lng: pos.coords.longitude,
                zaman: pos.timestamp,              // when the reading was taken (ms)
                dogruluk: pos.coords.accuracy      // accuracy radius in metres
            };
            // First fix after a request without a position: repeat it once (see above). This
            // cannot loop, because the repeated request carries a position.
            if (konumsuzIstekYapildi && navigator.onLine) {
                konumsuzIstekYapildi = false;
                goreviGetir();
            }
        },
        () => { },  // no permission or error: sonKonum stays null and the app works without it
        // Fresh, high-accuracy readings, since they are used for auditing. The poll runs at most
        // every 30 seconds and only while the app is visible, so the battery cost is limited.
        { enableHighAccuracy: true, timeout: 8000, maximumAge: 15000 }
    );
}

async function goreviGetir() {
    if (!navigator.onLine) {
        arayuzuCiz(guncelGorev); // offline: show the last known task
        return;
    }

    try {
        // Send the position along (if there is one yet) so the customer's ETA stays fresh.
        konumuTazele();
        konumsuzIstekYapildi = !sonKonum;   // repeated when the first fix arrives
        const gorevUrl = sonKonum
            ? `${CONFIG.BASE_URL}/vale-gorevi?lat=${sonKonum.lat}&lng=${sonKonum.lng}`
            : `${CONFIG.BASE_URL}/vale-gorevi`;
        const res = await fetch(gorevUrl, { headers: getAuthHeaders() });
        if (res.status === 401 || res.status === 403) {
            localStorage.removeItem('app_token');
            window.location.href = "login.html";
            return;
        }
        // 402: the company is inactive or deleted. The response has no task field, so without
        // this branch the screen would just say "no active task". Log out and let the login
        // page explain.
        if (res.status === 402) {
            localStorage.removeItem('app_token');
            localStorage.removeItem('app_vale_gorev');  // drop cached customer data; do not redraw it
            window.location.href = "login.html?sebep=firma_pasif";
            return;
        }
        const data = await res.json();
        guncelGorev = data.gorev;
        localStorage.setItem('app_vale_gorev', JSON.stringify(guncelGorev));
        arayuzuCiz(guncelGorev);
    } catch (e) {
        guncelGorev = JSON.parse(localStorage.getItem('app_vale_gorev'));
        arayuzuCiz(guncelGorev);
    }
}

// Date and time for display. The server stores UTC and the UI always shows Istanbul time, so a
// phone set to another time zone still shows the right times. The same function is copied in
// danisman.js, operasyon.js and admin.js; keep the copies identical.
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

function arayuzuCiz(gorev) {
    const container = document.getElementById('icerikGövdesi');
    
    if (!gorev) {
        container.innerHTML = `
            <div class="bos-mesaj">
                <svg class="bos-ikon" width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>
                <br>Aktif bir göreviniz bulunmuyor.<br><small style="color:#666">Yeni bir görev atandığında ekranınız otomatik güncellenecektir.</small>
            </div>
        `;
        return;
    }
    
    // BEKLIYOR_KONUM: the customer has not picked a location yet, so no actions are possible.
    if (gorev.durum === 'BEKLIYOR_KONUM') {
        const plakaKutusu = gorev.musteri_plaka ? `<div style="background:#eab308; color:#000; padding:10px; border-radius:8px; text-align:center; font-size:24px; font-weight:900; letter-spacing:2px; margin-bottom:15px; border:2px solid #000; box-shadow:0 4px 10px rgba(234,179,8,0.3);">${gorev.musteri_plaka}</div>` : '';
        container.innerHTML = `
            <div class="gorev-karti bekliyor">
                <div class="gorev-ust">
                    <span class="gorev-tip ${gorev.gorev_tipi === 'VALE_TESLIM' ? 'teslim' : ''}">${gorev.gorev_tipi === 'VALE_ALIM' ? 'ARAÇ ALIM' : 'ARAÇ TESLİM'}</span>
                    <span style="font-size:12px; color:var(--orange);">Konum Bekleniyor</span>
                </div>
                ${plakaKutusu}
                <div class="musteri-isim">${gorev.musteri_ad}</div>
                <a href="tel:${gorev.musteri_tel}" class="musteri-tel">📞 ${gorev.musteri_tel}</a>
                <div class="gorev-zaman">Görev açılışı: ${saatTR(gorev.kayit_tarihi)}</div>
                <p style="font-size:14px; color:var(--text-2); line-height:1.5;">Müşterinin kendisine gönderilen link üzerinden adresini seçmesi bekleniyor. Konum işaretlendiğinde butonlar aktifleşecektir.</p>
            </div>
        `;
        return;
    }
    
    // Action buttons (data-* attributes, handled by the delegated listener in baslat).
    // The order depends on the task type and must match VALE_GECISLER on the server exactly:
    //   pickup:   drive to the customer -> take the car -> reach the service center
    //   delivery: take the car out of the service center -> drive to the customer -> hand over
    const alim = gorev.gorev_tipi === 'VALE_ALIM';
    const ok = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/><path d="m12 5 7 7-7 7"/></svg>';
    const tik = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>';
    const bayrak = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="18" x="3" y="4" rx="2" ry="2"/><line x1="16" x2="16" y1="2" y2="6"/><line x1="8" x2="8" y1="2" y2="6"/><line x1="3" x2="21" y1="10" y2="10"/><path d="m9 16 2 2 4-4"/></svg>';
    const pusula = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="3 11 22 2 13 21 11 13 3 11"/></svg>';
    const btn = (sinif, aksiyon, veri, ikon, metin, ek = '') =>
        `<button class="btn ${sinif}" data-aksiyon="${aksiyon}" ${veri} style="${ek}">${ikon} ${metin}</button>`;
    const navBtn = (lat, lng, metin) =>
        (lat == null ? '' : btn('btn-mavi', 'navigasyon', `data-lat="${lat}" data-lng="${lng}"`, pusula, metin, 'background:#1e40af; margin-bottom:12px;'));
    const durumBtn = (sinif, durum, ikon, metin) =>
        btn(sinif, 'durum', `data-talep="${gorev.id}" data-durum="${durum}"`, ikon, metin);

    let aksiyonButonlari = "";
    if (gorev.durum === 'KONUM_ALINDI_VALE') {
        aksiyonButonlari = alim
            ? durumBtn('btn-mavi', 'VALE_YOLDA', ok, 'Araç Alıma Git')
            // delivery: the valet is still at the service center and takes the car out first
            : durumBtn('btn-yesil', 'ARAC_ALINDI', tik, 'Aracı Servisten Çıkardım');
    } else if (gorev.durum === 'VALE_YOLDA') {
        // Both types: the valet is driving to the customer (the customer sees an ETA).
        aksiyonButonlari = navBtn(gorev.konum_lat, gorev.konum_lng, 'Navigasyonu Aç') + (alim
            ? durumBtn('btn-yesil', 'ARAC_ALINDI', tik, 'Aracı Müşteriden Aldım')
            : durumBtn('btn-sari', 'TAMAM_MUSTERI', bayrak, 'Müşteriye Teslim Ettim (Görevi Bitir)'));
    } else if (gorev.durum === 'ARAC_ALINDI') {
        aksiyonButonlari = alim
            // pickup: car taken, drive back to the service center
            ? '<div style="text-align:center; margin-bottom:15px; color:var(--green); font-weight:bold;">Araç teslim alındı</div>'
              + navBtn(gorev.merkez_lat, gorev.merkez_lng, 'Servise Dönüş Navigasyonu')
              + durumBtn('btn-sari', 'TAMAM_SERVIS', bayrak, 'Servise Ulaştım (Görevi Bitir)')
            // delivery: car is out, drive to the customer (the ETA starts here)
            : '<div style="text-align:center; margin-bottom:15px; color:var(--green); font-weight:bold;">Araç servisten çıkarıldı</div>'
              + durumBtn('btn-mavi', 'VALE_YOLDA', ok, 'Müşteriye Teslime Git');
    }
    
    // If the address was erased while still needed, tell the valet what happened and what to do.
    const rizaUyarisi = konumImhaEdilmisMi(gorev)
        ? `<div style="margin-top:15px; background:rgba(239,68,68,0.12); border:1px solid rgba(239,68,68,0.35); border-radius:10px; padding:14px;">
               <div style="color:#f87171; font-weight:800; font-size:14px; margin-bottom:6px;">ADRES BİLGİSİ SİLİNDİ</div>
               <div style="color:var(--text-2); font-size:13px; line-height:1.5;">Müşteri konum onayını geri çekti; adresi sistemden kaldırıldı ve navigasyon açılamaz. Göreve devam edecekseniz adresi <b>telefonla</b> teyit edin.</div>
           </div>`
        : '';
    const plakaKutusu = gorev.musteri_plaka ? `<div style="background:#eab308; color:#000; padding:10px; border-radius:8px; text-align:center; font-size:24px; font-weight:900; letter-spacing:2px; margin-bottom:15px; border:2px solid #000; box-shadow:0 4px 10px rgba(234,179,8,0.3);">${gorev.musteri_plaka}</div>` : '';
    container.innerHTML = `
        <div class="gorev-karti">
            <div class="gorev-ust">
                <span class="gorev-tip ${gorev.gorev_tipi === 'VALE_TESLIM' ? 'teslim' : ''}">${gorev.gorev_tipi === 'VALE_ALIM' ? 'ARAÇ ALIM' : 'ARAÇ TESLİM'}</span>
                <span style="font-size:12px; color:var(--blue);">${gorev.durum.replace(/_/g, ' ')}</span>
            </div>
            ${plakaKutusu}
            <div class="musteri-isim">${gorev.musteri_ad}</div>
            <a href="tel:${gorev.musteri_tel}" class="musteri-tel">📞 ${gorev.musteri_tel}</a>
            <div class="gorev-zaman">Görev açılışı: ${saatTR(gorev.kayit_tarihi)}</div>
            ${rizaUyarisi}
            <div style="margin-top:20px;">
                ${aksiyonButonlari}
            </div>
        </div>
    `;
}

// Move the task to its next status. Works offline: the request is queued if needed.
function durumGuncelle(talepId, yeniDurum) {
    if (!confirm("Emin misiniz?")) return;

    // Navigation must open first and synchronously. A browser only hands off to an external
    // app (Maps) while the user's click is being handled; after any await that context is
    // gone and the link opens inside the PWA, without the destination. Never move this below
    // an await.
    const hedef = navigasyonHedefi(yeniDurum);
    if (hedef) navigasyonAc(hedef.lat, hedef.lng);
    // If this step should open navigation but there is no destination, say so. Do not try to
    // guess why (withdrawn consent, a stale screen, bad data): checking for a specific cause
    // misses cases where the cached task is empty or outdated. What matters is that the valet
    // learns there is no address to drive to.
    const navBekleniyordu = (yeniDurum === 'VALE_YOLDA')
        || (yeniDurum === 'ARAC_ALINDI' && guncelGorev && guncelGorev.gorev_tipi === 'VALE_ALIM');
    const navHedefiYok = navBekleniyordu && !hedef;

    const veri = { arac_id: aracId, talep_id: talepId, yeni_durum: yeniDurum };
    if (sonKonum) {
        veri.lat = sonKonum.lat; veri.lng = sonKonum.lng;   // whatever reading we already have
        // Age and accuracy of the reading; the panels do not judge distance without them.
        if (sonKonum.zaman) veri.konum_yasi_sn = Math.max(0, Math.round((Date.now() - sonKonum.zaman) / 1000));
        if (sonKonum.dogruluk != null) veri.konum_dogruluk_m = Math.round(sonKonum.dogruluk);
    }
    konumuTazele();  // refresh in the background for the next action

    // Update the screen right away, before the server answers.
    if (guncelGorev && guncelGorev.id === talepId) {
        guncelGorev.durum = yeniDurum;
        arayuzuCiz(guncelGorev);
    }

    // Not awaited: guvenliGonder queues the request if it cannot be delivered.
    guvenliGonder(`${CONFIG.BASE_URL}/vale-durum-guncelle`, veri);

    // Order matters: when online, guvenliGonder hides the toast synchronously, before its first
    // await. Showing the message before that call would hide it at once; showing it after the
    // call returns is deterministic. Offline, the toast already carries the offline message,
    // so leave it alone.
    if (navHedefiYok && navigator.onLine) {
        toastGoster('Adres bilgisi yok — navigasyon açılamıyor. Müşteri konum onayını geri çekmiş olabilir; adresi telefonla teyit edin.', 8000);
    }

    if (yeniDurum === 'TAMAM_SERVIS' || yeniDurum === 'TAMAM_MUSTERI') {
        guncelGorev = null;
        localStorage.removeItem('app_vale_gorev'); // the customer's data should not stay on the phone
        arayuzuCiz(null);
    }
}

// POST with an offline queue (adapted from sofor.js).
// Queue rule, shared with sofor.js: keep only requests that could succeed later. Here that means
// offline, network errors and 5xx. A 401 sends the user to the login page instead. When the
// queue is drained later, a 401 keeps the entry (it will work after logging in again). Any
// other 4xx is the server's final word (task cancelled, status already moved on) and is dropped.
async function guvenliGonder(url, payload) {
    const toast = document.getElementById('offlineToast');
    if (navigator.onLine) {
        try {
            toast.style.display = 'none';
            const res = await fetch(url, {
                method: "POST", headers: getAuthHeaders(), body: JSON.stringify(payload)
            });
            if(res.ok) return true;
            // A 4xx is a final refusal and is not queued; retrying would get the same answer on
            // every reconnect forever. Example: the valet acts while an advisor cancels the task,
            // and the server answers 409. Show the reason and reload the real state instead.
            // (5xx and network errors are temporary and fall through to the queue below.)
            // 401 means the session ended: goreviGetir() sends the user to the login page.
            if (res.status === 401) { goreviGetir(); return false; }
            if (res.status >= 400 && res.status < 500) {
                const hata = await res.json().catch(() => ({}));
                toast.innerText = hata.detail || "İşlem sunucu tarafından kabul edilmedi.";
                toast.style.display = 'block';
                setTimeout(() => { toast.style.display = 'none'; }, 4000);
                goreviGetir();  // reload the real state (a cancelled task disappears)
                return false;
            }
        } catch (e) {}
    }
    
    // Offline, network error or 5xx: queue it.
    cevrimdisiKuyruk.push({ url, payload, zaman: Date.now() });
    localStorage.setItem('app_vale_kuyruk', JSON.stringify(cevrimdisiKuyruk));
    toast.innerText = "İnternet yok. İşleminiz kuyruğa eklendi, bağlantı gelince gönderilecek.";
    toast.style.display = 'block';
    setTimeout(() => { toast.style.display = 'none'; }, 3000);
    return false;
}

// Back online: send the queued requests in order.
window.addEventListener('online', async () => {
    document.getElementById('offlineToast').style.display = 'none';
    if (cevrimdisiKuyruk.length > 0) {
        let yeniKuyruk = [];
        for (const islem of cevrimdisiKuyruk) {
            try {
                const res = await fetch(islem.url, {
                    method: "POST", headers: getAuthHeaders(), body: JSON.stringify(islem.payload)
                });
                // Keep only what may succeed later (401, 5xx); see the rule above guvenliGonder.
                if (!res.ok && (res.status === 401 || res.status >= 500)) yeniKuyruk.push(islem);
            } catch(e) {
                yeniKuyruk.push(islem);
            }
        }
        cevrimdisiKuyruk = yeniKuyruk;
        localStorage.setItem('app_vale_kuyruk', JSON.stringify(cevrimdisiKuyruk));
        goreviGetir(); // resync with the server
    } else {
        goreviGetir();
    }
});

window.addEventListener('offline', () => {
    const toast = document.getElementById('offlineToast');
    toast.innerText = "Bağlantı koptu. Çevrimdışı moda geçildi.";
    toast.style.display = 'block';
    setTimeout(() => { toast.style.display = 'none'; }, 3000);
});

// WebSocket: the server sends "YENILE" (refresh) whenever this valet's data changes.
let ws = null;
let reconnectTimeout = null;

function canliBaglantiKur() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
    if (reconnectTimeout) { clearTimeout(reconnectTimeout); reconnectTimeout = null; }
    
    const wsUrl = CONFIG.BASE_URL.replace(/^http/, 'ws') + '/ws';
    ws = new WebSocket(wsUrl);
    
    ws.onopen = () => {
        if (token) ws.send(JSON.stringify({ token: token }));
    };
    
    ws.onmessage = (event) => {
        if (event.data === "YENILE") {
            goreviGetir();
        }
    };
    
    ws.onclose = () => {
        reconnectTimeout = setTimeout(canliBaglantiKur, 3000);
    };
}

document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === 'visible') {
        if (!ws || ws.readyState !== WebSocket.OPEN) canliBaglantiKur();
        if (navigator.onLine) goreviGetir();
    }
});

// Poll every 30 seconds as a safety net. WebSocket messages are not stored, so a "YENILE"
// sent while the connection was down is lost, and without polling the screen could stay stale
// indefinitely (for example, still showing a cancelled task). sofor.js does the same. No
// requests while the screen is hidden or offline.
setInterval(() => {
    if (document.visibilityState === 'visible' && navigator.onLine) goreviGetir();
}, 30000);

document.addEventListener('DOMContentLoaded', baslat);

// ============================================================
// SERVICE WORKER (PWA), the same pattern as sofor.js. The valet app must register it itself:
// valets never open sofor.html, and without registration there would be no offline start,
// no version display and no automatic updates.
// ============================================================
if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
        // When a new service worker takes control, reload once to run the new code. The task and
        // the offline queue are in localStorage, so the reload loses nothing.
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
                const el = document.getElementById('valeSurum');
                if (el && e.data && e.data.version) {
                    el.textContent = String(e.data.version).replace('app-', '');
                }
            };
            reg.active.postMessage({ type: 'GET_VERSION' }, [kanal.port2]);
        }).catch(() => { });

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
