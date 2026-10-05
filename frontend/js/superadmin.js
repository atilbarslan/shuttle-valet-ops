// superadmin.html: the platform operator's panel. Creates and manages customer companies
// (modules, quotas, activation), their first admins, and an admin contact list.
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
                    ADD_ATTR: ['target', 'rel', 'value', 'placeholder', 'data-firma-id', 'data-aksiyon', 'data-yeni-durum'],
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
const aktifRol = localStorage.getItem('aktif_rol');
const ekleyenKisi = localStorage.getItem('aktif_kullanici_adi');

function getAuthHeaders() {
    let hamToken = localStorage.getItem('app_token') || "";
    // Keep only the characters a JWT can contain, so a corrupted stored value cannot inject
    // anything into the header.
    let temizToken = hamToken.replace(/[^a-zA-Z0-9\-_.]/g, "");
    return {
        "Authorization": "Bearer " + temizToken,
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

// HTML-escape a value (& first, then < > and both quote characters). insertAdjacentHTML does not
// go through the DOMPurify guard above, so data inserted that way must be escaped by hand.
function pxEsc(s) {
    return (s || "").toString()
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// Username helper. The same code is in admin.js; keep the two identical.
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

// Refresh lock. After an action the panel reloads its data itself, and the server also sends a
// "YENILE" over the WebSocket for the same change, so the same lists were fetched twice within a
// second. The lock covers every caller, and a second trigger inside the 800 ms window is
// dropped rather than queued: both triggers belong to the same change, so the first fetch
// already has the new data. Superadmin actions are manual, so two different changes within
// 800 ms do not happen in practice.
let _yenilemeKilidi = false;

async function verileriCek() {
    if (_yenilemeKilidi) return;
    _yenilemeKilidi = true;
    try {
        await _verileriCekUygula();
    } finally {
        setTimeout(() => { _yenilemeKilidi = false; }, 800);
    }
}

async function _verileriCekUygula() {
    try {
        const res = await fetch(CONFIG.BASE_URL + "/firmalar", { headers: getAuthHeaders() });
        if (res.status === 401) { cikisYap(); return; }
        const firmalar = await res.json();

        const liste = document.getElementById('firmaListesi');
        const secici = document.getElementById('firmaSecici');
        liste.innerHTML = ""; secici.innerHTML = "";

        firmalar.forEach(f => {
            const tarih = f.kayit_tarihi ? new Date(f.kayit_tarihi).toLocaleDateString('tr-TR') : '-';
            const aktif = f.is_active !== false;
            const satirSinifi = aktif ? '' : 'pasif-satir';
            const durumMetni = aktif
                ? '<span class="durum-aktif">● AKTİF</span>'
                : '<span class="durum-pasif">● PASİF</span>';
            const toggleMetni = aktif ? 'Pasife Al' : 'Aktife Al';
            const toggleSinif = aktif ? 'toggle-btn toggle-pasif' : 'toggle-btn toggle-aktif';

            // Package = the enabled modules (shuttle off means a valet-only company).
            const shuttleAcik = f.shuttle_aktif !== false;
            const moduller = [];
            if (shuttleAcik) moduller.push(f.sistem_modu === 'DURAK' ? 'SERVIS PAKETİ' : 'VIP (HARİTA)');
            if (f.vale_aktif) moduller.push('VALE');
            const modMetni = moduller.join(' + ') || '—';

            // The vehicle quota only matters for shuttle; the valet quota is a separate pricing basis.
            const kotaParcalari = [];
            if (shuttleAcik) {
                kotaParcalari.push(f.subeli
                    ? `Şubeli · araç kotası ${f.toplam_kota || '?'} · ≤${f.max_sube || '?'} şube`
                    : `Tek şube · araç kotası ${f.toplam_kota || 1}`);
            } else if (f.subeli) {
                kotaParcalari.push(`Şubeli · ≤${f.max_sube || '?'} şube`);
            }
            if (f.vale_aktif) kotaParcalari.push(`vale kotası ${f.vale_kota || 0}`);
            const kotaMetni = kotaParcalari.join(' · ') || '—';

            // insertAdjacentHTML instead of innerHTML: DOMPurify sanitises a lone <tr> out of
            // context and can strip the row and cell tags. This path skips the guard, so every
            // data value below is escaped with pxEsc.
            liste.insertAdjacentHTML('beforeend', `
                <tr class="${satirSinifi}" id="firma-satir-${f.id}">
                    <td class="firma-adi-hucre">${pxEsc(f.firma_adi)}</td>
                    <td class="paket-hucre">${modMetni}<br><small>${kotaMetni}</small></td>
                    <td>${durumMetni}</td>
                    <td class="tarih-hucre">${tarih}</td>
                    <td>
                        <div class="firma-aksiyonlar">
                            <button class="${toggleSinif}" data-aksiyon="aktiflik" data-firma-id="${f.id}" data-yeni-durum="${!aktif}">${toggleMetni}</button>
                            <button class="toggle-btn" data-aksiyon="vale" data-firma-id="${f.id}" data-yeni-durum="${!f.vale_aktif}">${f.vale_aktif ? 'Vale Kapat' : 'Vale Aç'}</button>
                            <button class="toggle-btn" data-aksiyon="shuttle" data-firma-id="${f.id}" data-yeni-durum="${!shuttleAcik}">${shuttleAcik ? 'Shuttle Kapat' : 'Shuttle Aç'}</button>
                            ${!f.subeli ? `<button class="toggle-btn" data-aksiyon="subeli-yap" data-firma-id="${f.id}">Şubeli Yap</button>` : ''}
                            <button class="toggle-btn" data-aksiyon="kota" data-firma-id="${f.id}" data-subeli="${f.subeli ? 1 : 0}" data-toplam="${f.toplam_kota || 1}" data-max="${f.max_sube || 0}" data-shuttle="${shuttleAcik ? 1 : 0}" data-vale="${f.vale_aktif ? 1 : 0}" data-valekota="${f.vale_kota || 0}">Kota</button>
                            <button class="sil-btn" data-aksiyon="sil" data-firma-id="${f.id}">Sil</button>
                        </div>
                    </td>
                </tr>`);

            secici.insertAdjacentHTML('beforeend', `<option value="${f.id}">${pxEsc(f.firma_adi)}</option>`);
        });

        rehberiCek();
    } catch (e) { }
}

// Company and admin contact list.
async function rehberiCek() {
    try {
        const res = await fetch(CONFIG.BASE_URL + "/firma-admin-rehberi", { headers: getAuthHeaders() });
        if (res.status === 401) { cikisYap(); return; }
        const rehber = await res.json();
        const tb = document.getElementById('rehberListesi');
        if (!tb) return;
        tb.innerHTML = "";
        const esc = pxEsc;
        rehber.forEach(r => {
            const firma = esc(r.firma_adi);
            // Show the admin's display name with the username underneath (the actions use the
            // username). Without a display name, show the username alone.
            const gorunen = (r.gorunen_ad || '').trim();
            const yon = r.kullanici_adi
                ? (gorunen
                    ? `${esc(gorunen)}<span class="rehber-kimlik">${esc(r.kullanici_adi)}</span>`
                    : esc(r.kullanici_adi))
                : '<span class="bos-hucre">— yönetici yok</span>';
            const mail = r.email ? `<a href="mailto:${esc(r.email)}">${esc(r.email)}</a>` : '<span class="bos-hucre">—</span>';
            const tel = r.telefon ? `<a href="tel:${esc(r.telefon)}">${esc(r.telefon)}</a>` : '<span class="bos-hucre">—</span>';
            const telDigits = (r.telefon || "").replace(/\D/g, "").slice(-10);
            const islem = r.kullanici_adi
                ? `<button class="duzenle-btn" data-aksiyon="duzenle" data-kullanici-adi="${esc(r.kullanici_adi)}" data-email="${esc(r.email)}" data-telefon="${telDigits}">Düzenle</button>`
                : '<span class="bos-hucre">—</span>';
            tb.insertAdjacentHTML('beforeend', `<tr><td class="firma-adi-hucre">${firma}</td><td>${yon}</td><td>${mail}</td><td>${tel}</td><td>${islem}</td></tr>`);
        });
    } catch (e) { }
}

async function firmaOlustur() {
    try {
        const ad = document.getElementById('yeniFirmaAdi').value.trim();
        const mod = document.getElementById('sistemModu').value;
        const subeli = document.getElementById('firmaSubeli').checked;
        const toplamKota = parseInt(document.getElementById('firmaToplamKota').value, 10) || 1;
        const maxSube = subeli ? (parseInt(document.getElementById('firmaMaxSube').value, 10) || 1) : 0;

        const shuttleAktif = document.getElementById('firmaShuttleAktif').checked;
        const valeAktif = document.getElementById('firmaValeAktif').checked;
        const valeKota = valeAktif ? (parseInt(document.getElementById('firmaValeKota').value, 10) || 1) : 0;

        // KVKK: this company is the data controller named in the privacy notice its customers
        // see. Left empty, the notice would be incomplete, so both fields are required.
        const kvkkUnvan = document.getElementById('firmaKvkkUnvan').value.trim();
        const kvkkBasvuru = document.getElementById('firmaKvkkBasvuru').value.trim();
        // The address is optional: name and contact channel satisfy the notice duty (art. 10);
        // the address only helps customers who want to apply in writing.
        const kvkkAdres = document.getElementById('firmaKvkkAdres').value.trim();

        if (!ad) return alert("Firma adı boş olamaz!");
        if (subeli && maxSube < 1) return alert("Şubeli firma için azami şube sayısı en az 1 olmalı.");
        if (!shuttleAktif && !valeAktif) return alert("En az bir hizmet seçmelisiniz: Shuttle veya Vale.");
        if (valeAktif && valeKota < 1) return alert("Vale hizmeti için vale kotası en az 1 olmalı.");
        if (kvkkUnvan.length < 3) return alert("KVKK tam ünvanı zorunludur — müşteriye gösterilen aydınlatma metninde veri sorumlusu olarak bu yazar.");
        if (kvkkBasvuru.length < 3) return alert("KVKK başvuru kanalı zorunludur — müşteri m.11 haklarını buraya başvurarak kullanır.");

        const res = await fetch(CONFIG.BASE_URL + "/firma-ekle", {
            method: "POST",
            headers: getAuthHeaders(),
            body: JSON.stringify({
                firma_adi: ad,
                sistem_modu: mod,
                shuttle_aktif: shuttleAktif,
                vale_aktif: valeAktif,
                vale_kota: valeKota,
                subeli: subeli,
                max_sube: maxSube,
                toplam_kota: toplamKota,
                kvkk_unvan: kvkkUnvan,
                kvkk_basvuru_kanali: kvkkBasvuru,
                kvkk_adres: kvkkAdres
            })
        });

        if (res.status === 401) { cikisYap(); return; }

        const data = await res.json();

        if (!res.ok) {
            alert("Kurulum Hatası: " + (data.detail || JSON.stringify(data)));
            return;
        }

        document.getElementById('yeniFirmaAdi').value = "";
        document.getElementById('firmaKvkkUnvan').value = "";
        document.getElementById('firmaKvkkBasvuru').value = "";
        document.getElementById('firmaKvkkAdres').value = "";
        document.getElementById('firmaSubeli').checked = false;
        document.getElementById('firmaToplamKota').value = "1";
        document.getElementById('firmaMaxSube').value = "1";
        document.getElementById('maxSubeGrup').classList.add('gizli');
        document.getElementById('firmaShuttleAktif').checked = true;
        document.getElementById('firmaValeAktif').checked = false;
        document.getElementById('firmaValeKota').value = "1";
        document.getElementById('valeKotaGrup').classList.add('gizli');
        document.getElementById('shuttleAyarGrup').classList.remove('gizli');
        alert("Firma başarıyla kuruldu!");
        verileriCek();

    } catch (error) {
        alert("Ağ Hatası: Sunucuya ulaşılamadı. Detay: " + error.message);
    }
}

async function kullaniciOlustur() {
    const firma_id = document.getElementById('firmaSecici').value;
    const rol = document.getElementById('rolSecici').value;
    const kullanici_adi = document.getElementById('yeniKullaniciAdi').value.trim();
    const email = document.getElementById('yeniEmail').value.trim();
    const mailGonder = document.getElementById('mailGonderCheck').checked;

    if (!firma_id || !kullanici_adi) return alert("Eksik bilgi: Firma ve kullanıcı adı zorunlu!");

    if (mailGonder && !email) {
        return alert("Davet maili göndermek için e-posta adresi gerekli.\n(Veya 'otomatik mail gönder' kutusunu kapatıp linki manuel paylaşabilirsiniz.)");
    }

    const emailRegex = /^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/;
    if (email && !emailRegex.test(email)) {
        return alert("E-posta adresi geçersiz görünüyor.\n\nÖrnek: kullanici@ornekfirma.com.tr");
    }

    // Phone: 10 digits without the leading 0; +90 is added (same as the advisor and admin panels).
    const telRaw = document.getElementById('yeniTelefon').value.replace(/[^0-9]/g, "");
    if (telRaw && telRaw.length !== 10) {
        return alert("Telefon numarası başında sıfır olmadan 10 hane olmalıdır.\n\nÖrnek: 5XX XXX XX XX");
    }
    const telefon = telRaw ? "+90" + telRaw : "";

    try {
        const res = await fetch(CONFIG.BASE_URL + "/kullanici-ekle", {
            method: "POST", headers: getAuthHeaders(),
            body: JSON.stringify({
                ekleyen_kisi: ekleyenKisi, firma_id: firma_id,
                rol: rol, kullanici_adi: kullanici_adi, email: email, telefon: telefon, arac_id: "", marka: "Genel"
            })
        });
        if (res.status === 401) { cikisYap(); return; }
        if (!res.ok) {
            const hataJson = await res.json().catch(() => ({ detail: "Bilinmeyen hata" }));
            alert("Kayıt Hatası: " + (hataJson.detail || "İşlem başarısız"));
            return;
        }

        const veri = await res.json();
        const link = veri.davet_linki;

        if (mailGonder && email) {
            const mailRes = await fetch(CONFIG.BASE_URL + "/mail-davet-at", {
                method: 'POST', headers: getAuthHeaders(),
                body: JSON.stringify({ kullanici_adi: kullanici_adi })
            });
            if (mailRes.ok) {
                davetModalAc(kullanici_adi, link, `Davet maili ${email} adresine gönderildi.`, telefon);
            } else {
                const mailHata = await mailRes.json().catch(() => ({ detail: "?" }));
                davetModalAc(kullanici_adi, link, `⚠️ Mail gönderilemedi (${mailHata.detail}). Linki aşağıdan manuel iletebilirsiniz.`, telefon);
            }
        } else {
            davetModalAc(kullanici_adi, link, `Kullanıcı adı: ${kullanici_adi}`, telefon);
        }

        document.getElementById('yeniKullaniciAdi').value = "";
        document.getElementById('yeniEmail').value = "";
        document.getElementById('yeniTelefon').value = "";

    } catch (e) { alert("Sunucuya bağlanılamadı."); }
}

// Invitation link dialog: copy the link or send it on WhatsApp (same as the admin panel).
let aktifDavetLink = "";
let aktifDavetKullanici = "";
let aktifDavetTelefon = "";  // digits only (905XXXXXXXXX), for a direct wa.me chat

function davetModalAc(kullaniciAdi, link, kimlikMetni, telefon) {
    aktifDavetLink = link || "";
    aktifDavetKullanici = kullaniciAdi || "";
    aktifDavetTelefon = (telefon || "").replace(/[^0-9]/g, "");  // "+90..." -> "90..."
    document.getElementById('davetKimlik').textContent = kimlikMetni || "";
    document.getElementById('davetLinkKutu').textContent = aktifDavetLink;  // textContent, never parsed as HTML
    document.getElementById('davetOverlay').className = "overlay-acik";
    // Refresh the contact list to include the new user.
    if (typeof rehberiCek === 'function') rehberiCek();
}

function davetModalKapat() {
    document.getElementById('davetOverlay').className = "overlay-gizli";
    aktifDavetLink = "";
    aktifDavetKullanici = "";
    aktifDavetTelefon = "";
}

function davetLinkKopyala() {
    if (!aktifDavetLink) return;
    if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(aktifDavetLink).then(() => alert("Link kopyalandı!")).catch(() => alert("Kopyalanamadı, linki elle seçip kopyalayın."));
    } else {
        alert("Kopyalanamadı, linki elle seçip kopyalayın.");
    }
}

function davetWhatsapp() {
    if (!aktifDavetLink) return;
    const mesaj = `Merhaba! ${aktifDavetKullanici} kullanıcı adıyla Shuttle & Valet Ops sistemine eklendiniz. Giriş şifrenizi belirlemek için lütfen şu linke tıklayın: ${aktifDavetLink}`;
    // With a number, open that person's chat directly; otherwise let the user pick a contact.
    const taban = aktifDavetTelefon ? `https://wa.me/${aktifDavetTelefon}` : 'https://wa.me/';
    window.open(`${taban}?text=${encodeURIComponent(mesaj)}`, '_blank', 'noopener,noreferrer');
}

// Edit an admin's e-mail and phone.
let duzenlenenKullanici = null;

function duzenleAc(kullaniciAdi, email, telDigits) {
    duzenlenenKullanici = kullaniciAdi;
    document.getElementById('duzenleKimlik').textContent = kullaniciAdi;
    document.getElementById('duzenleEmail').value = email || "";
    document.getElementById('duzenleTelefon').value = telDigits || "";
    document.getElementById('duzenleOverlay').className = "overlay-acik";
}

function duzenleKapat() {
    document.getElementById('duzenleOverlay').className = "overlay-gizli";
    duzenlenenKullanici = null;
}

async function duzenleKaydet() {
    if (!duzenlenenKullanici) return;
    const email = document.getElementById('duzenleEmail').value.trim();
    const emailRegex = /^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/;
    if (email && !emailRegex.test(email)) {
        return alert("E-posta adresi geçersiz görünüyor.\n\nÖrnek: kullanici@ornekfirma.com.tr");
    }
    const telRaw = document.getElementById('duzenleTelefon').value.replace(/[^0-9]/g, "");
    if (telRaw && telRaw.length !== 10) {
        return alert("Telefon numarası başında sıfır olmadan 10 hane olmalıdır.\n\nÖrnek: 5XX XXX XX XX");
    }
    const telefon = telRaw ? "+90" + telRaw : "";
    try {
        const res = await fetch(CONFIG.BASE_URL + "/yonetici-iletisim-guncelle", {
            method: "PUT", headers: getAuthHeaders(),
            body: JSON.stringify({ kullanici_adi: duzenlenenKullanici, email: email, telefon: telefon })
        });
        if (res.status === 401) { cikisYap(); return; }
        if (!res.ok) {
            const h = await res.json().catch(() => ({ detail: "Bilinmeyen hata" }));
            return alert("Güncelleme hatası: " + (h.detail || "işlem başarısız"));
        }
        duzenleKapat();
        rehberiCek();
        alert("Yönetici bilgileri güncellendi.");
    } catch (e) { alert("Sunucuya bağlanılamadı."); }
}

// Announcement e-mail to every admin with an address.
function topluMailAc() {
    document.getElementById('topluKonu').value = "";
    document.getElementById('topluMesaj').value = "";
    document.getElementById('topluMailOverlay').className = "overlay-acik";
}
function topluMailKapat() {
    document.getElementById('topluMailOverlay').className = "overlay-gizli";
}
async function topluMailGonder() {
    const konu = document.getElementById('topluKonu').value.trim();
    const mesaj = document.getElementById('topluMesaj').value.trim();
    if (!konu || !mesaj) return alert("Konu ve mesaj zorunludur.");
    if (!confirm("E-postası tanımlı TÜM yöneticilere (ADMIN) toplu mail gönderilecek.\n\nEmin misiniz?")) return;
    const btn = document.getElementById('topluGonderBtn');
    const eski = btn.textContent;
    btn.disabled = true; btn.textContent = "Gönderiliyor…";
    try {
        const res = await fetch(CONFIG.BASE_URL + "/toplu-mail-gonder", {
            method: "POST", headers: getAuthHeaders(),
            body: JSON.stringify({ konu: konu, mesaj: mesaj })
        });
        if (res.status === 401) { cikisYap(); return; }
        const d = await res.json().catch(() => ({}));
        if (!res.ok) {
            alert("Gönderim hatası: " + (d.detail || "işlem başarısız"));
        } else {
            alert(d.mesaj || "Gönderildi.");
            topluMailKapat();
        }
    } catch (e) { alert("Sunucuya bağlanılamadı."); }
    finally { btn.disabled = false; btn.textContent = eski; }
}

async function firmaSil(firmaId) {
    if (!confirm(`Firmayı silmek istediğinize emin misiniz?\n\nBu işlem GERİ ALINAMAZ — firmaya bağlı TÜM personel, araç, güzergah, durak, talep, şube ve marka da silinir.`)) return;
    try {
        const res = await fetch(`${CONFIG.BASE_URL}/firma-sil/${firmaId}`, { method: 'DELETE', headers: getAuthHeaders() });
        if (res.status === 401) { cikisYap(); return; }
        const data = await res.json().catch(() => ({}));
        if (res.ok) { alert("Firma ve bağlı tüm kayıtlar silindi."); verileriCek(); }
        else { alert("Silinemedi: " + (data.detail || "Bilinmeyen hata")); }
    } catch (e) { alert("Sunucuya bağlanılamadı."); }
}

// Turn a company without branches into one with branches (one way). Existing records stay
// with headquarters.
async function firmaSubeliYap(firmaId) {
    const mg = prompt("Azami şube sayısı (1-100):", "2");
    if (mg === null) return;
    const maxSube = parseInt(mg, 10);
    if (isNaN(maxSube) || maxSube < 1 || maxSube > 100) return alert("Geçersiz azami şube sayısı (1-100).");
    if (!confirm(`Firma şubeli yapılacak (azami ${maxSube} şube).\nMevcut tüm kayıtlar Merkez şubede kalır; sonrasında admin panelinden şube açılabilir.\nBu dönüşüm geri alınamaz. Devam edilsin mi?`)) return;
    try {
        const res = await fetch(CONFIG.BASE_URL + "/firma-subeli-yap", {
            method: "POST", headers: getAuthHeaders(),
            body: JSON.stringify({ firma_id: firmaId, max_sube: maxSube })
        });
        if (res.status === 401) { cikisYap(); return; }
        const data = await res.json().catch(() => ({}));
        if (!res.ok) { alert("Dönüştürülemedi: " + (data.detail || "Bilinmeyen hata")); return; }
        alert(data.mesaj || "Firma şubeli yapıldı.");
        verileriCek();
    } catch (e) { alert("Sunucuya bağlanılamadı."); }
}

// Switch the shuttle module. Switching off deletes drivers, shuttle vehicles, routes, stops and
// shuttle requests for good, so it asks for confirmation.
async function shuttleDegistir(firmaId, yeniDurum) {
    let sistemModu = null;
    if (yeniDurum) {
        // Switching on asks for the operating mode instead of silently defaulting to HARITA.
        const secim = prompt(
            "Shuttle çalışma modunu seçin:\n\n" +
            "1 = VIP  (müşteri haritadan kapı konumu seçer)\n" +
            "2 = SERVİS (sabit durak / güzergah listesi)", "1");
        if (secim === null) return;
        const t = secim.trim();
        if (t !== "1" && t !== "2") return alert("Geçersiz seçim. 1 (VIP) veya 2 (SERVİS) girin.");
        sistemModu = t === "1" ? "HARITA" : "DURAK";
    } else {
        const onay = confirm(
            "⚠️ SHUTTLE HİZMETİ KAPATILACAK\n\n" +
            "Bu firmadaki TÜM şoförler, servis araçları, güzergah/duraklar ve shuttle görev kayıtları KALICI olarak silinecek.\n" +
            "Vale tarafı (vale personeli ve vale görevleri) korunur. Bu işlem GERİ ALINAMAZ.\n\nEmin misiniz?");
        if (!onay) return;
    }
    try {
        const res = await fetch(CONFIG.BASE_URL + "/firma-shuttle-guncelle", {
            method: 'POST', headers: getAuthHeaders(),
            body: JSON.stringify({ firma_id: firmaId, durum: yeniDurum, sistem_modu: sistemModu })
        });
        if (res.status === 401) { cikisYap(); return; }
        const data = await res.json();
        if (!res.ok) { alert("Shuttle durumu güncellenemedi: " + (data.detail || "")); return; }
        alert(data.mesaj || "Güncellendi.");
        verileriCek();
    } catch (e) { alert("Sunucuya bağlanılamadı."); }
}

// Switch the valet module. Switching off deletes valet staff, valet tasks and virtual valet
// vehicles for good.
async function valeDegistir(firmaId, yeniDurum) {
    if (!yeniDurum) {
        const onay = confirm(
            "⚠️ VALE HİZMETİ KAPATILACAK\n\n" +
            "Bu firmadaki TÜM vale personeli, vale görevleri (devam edenler dahil) ve sanal vale araçları KALICI olarak silinecek.\n" +
            "Devam eden görevlerin müşteri linkleri anında geçersiz olur. Bu işlem GERİ ALINAMAZ.\n\nEmin misiniz?");
        if (!onay) return;
    }
    try {
        const res = await fetch(CONFIG.BASE_URL + "/firma-vale-guncelle", {
            method: 'POST', headers: getAuthHeaders(),
            body: JSON.stringify({ firma_id: firmaId, durum: yeniDurum })
        });
        if (res.status === 401) { cikisYap(); return; }
        const data = await res.json();
        if (!res.ok) { alert("Vale durumu güncellenemedi: " + (data.detail || "")); return; }
        alert(data.mesaj || "Güncellendi.");
        verileriCek();
    } catch (e) { alert("Sunucuya bağlanılamadı."); }
}

async function aktiflikDegistir(firmaId, yeniDurum) {
    try {
        const res = await fetch(CONFIG.BASE_URL + "/firma-aktiflik-guncelle", {
            method: 'POST', headers: getAuthHeaders(),
            body: JSON.stringify({ firma_id: firmaId, durum: yeniDurum })
        });
        if (res.status === 401) { cikisYap(); return; }
        if (!res.ok) alert("Durum güncellenemedi.");
    } catch (e) { alert("Sunucuya bağlanılamadı."); }
}

// WebSocket: refresh on "YENILE" from the server.
let ws = null;

async function firmaKotaDuzenle(firmaId, subeli, mevcutToplam, mevcutMax, shuttleAcik = true, valeAcik = false, mevcutValeKota = 0) {
    const payload = { firma_id: firmaId };

    // The vehicle quota is only asked for when shuttle is on (valet-only companies have no shuttle vehicles).
    if (shuttleAcik) {
        const tg = prompt("Toplam araç kotası (shuttle fiyat tabanı):", mevcutToplam);
        if (tg === null) return;
        const toplam = parseInt(tg, 10);
        if (isNaN(toplam) || toplam < 1) return alert("Geçersiz toplam kota (en az 1).");
        payload.toplam_kota = toplam;
    }

    // Valet quota = number of valet staff (a separate pricing basis).
    if (valeAcik) {
        const vg = prompt("Vale kotası (vale personeli sayısı — fiyat tabanı):", mevcutValeKota);
        if (vg === null) return;
        const vk = parseInt(vg, 10);
        if (isNaN(vk) || vk < 1) return alert("Geçersiz vale kotası (en az 1).");
        payload.vale_kota = vk;
    }

    if (subeli) {
        const mg = prompt("Azami şube sayısı:", mevcutMax);
        if (mg === null) return;
        const maxs = parseInt(mg, 10);
        if (isNaN(maxs) || maxs < 1) return alert("Geçersiz azami şube (en az 1).");
        payload.max_sube = maxs;
    }
    try {
        const res = await fetch(CONFIG.BASE_URL + "/firma-kota-guncelle", {
            method: "POST", headers: getAuthHeaders(),
            body: JSON.stringify(payload)
        });
        const data = await res.json().catch(() => ({}));
        if (res.ok) { alert("Kota güncellendi."); verileriCek(); }
        else alert("Güncellenemedi: " + (data.detail || "Bilinmeyen hata"));
    } catch (e) { alert("Sunucuya bağlanılamadı."); }
}

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

        // Duplicate refreshes are filtered inside verileriCek().
        try {
            verileriCek();
        } catch (err) {
            console.error('verileriCek hatası:', err);
        }
    };
    ws.onclose = () => setTimeout(canliBaglantiKur, 3000);
}

// ============================================================
// Event listeners
// ============================================================
document.addEventListener('DOMContentLoaded', () => {
    // Client-side role check, only to show the right screen; the server enforces access.
    if (aktifRol !== 'SUPERADMIN') {
        document.getElementById('yetkiHata').style.display = 'flex';
        setTimeout(() => { window.location.href = "login.html"; }, 2000);
        return;
    }

    document.body.style.display = 'block';
    verileriCek();

    kullaniciAdiAlaniniBagla('yeniKullaniciAdi');

    const cikisBtn = document.getElementById('cikisBtn');
    if (cikisBtn) cikisBtn.addEventListener('click', () => cikisYap());

    const firmaOlusturBtn = document.getElementById('firmaOlusturBtn');
    if (firmaOlusturBtn) firmaOlusturBtn.addEventListener('click', firmaOlustur);

    // Show the valet quota field only when valet is ticked.
    const firmaValeAktif = document.getElementById('firmaValeAktif');
    const valeKotaGrup = document.getElementById('valeKotaGrup');
    if (firmaValeAktif && valeKotaGrup) {
        firmaValeAktif.addEventListener('change', function () {
            valeKotaGrup.classList.toggle('gizli', !this.checked);
        });
    }

    // Show the shuttle settings (mode and vehicle quota) only when shuttle is ticked; they
    // mean nothing for a valet-only company.
    const firmaShuttleAktif = document.getElementById('firmaShuttleAktif');
    const shuttleAyarGrup = document.getElementById('shuttleAyarGrup');
    if (firmaShuttleAktif && shuttleAyarGrup) {
        firmaShuttleAktif.addEventListener('change', function () {
            shuttleAyarGrup.classList.toggle('gizli', !this.checked);
        });
    }

    // Show the max-branches field only when "with branches" is ticked.
    const firmaSubeli = document.getElementById('firmaSubeli');
    const maxSubeGrup = document.getElementById('maxSubeGrup');
    if (firmaSubeli && maxSubeGrup) {
        firmaSubeli.addEventListener('change', function () {
            maxSubeGrup.classList.toggle('gizli', !this.checked);
        });
    }

    const kullaniciOlusturBtn = document.getElementById('kullaniciOlusturBtn');
    if (kullaniciOlusturBtn) kullaniciOlusturBtn.addEventListener('click', kullaniciOlustur);

    // Digits only in phone fields.
    const yeniTelefon = document.getElementById('yeniTelefon');
    if (yeniTelefon) {
        yeniTelefon.addEventListener('input', function () { this.value = this.value.replace(/[^0-9]/g, ''); });
    }

    // The company table is rebuilt on every refresh, so its buttons are handled by delegation.
    const firmaListesi = document.getElementById('firmaListesi');
    if (firmaListesi) {
        firmaListesi.addEventListener('click', (e) => {
            const btn = e.target.closest('button[data-aksiyon]');
            if (!btn) return;

            const aksiyon = btn.dataset.aksiyon;
            const firmaId = btn.dataset.firmaId;

            if (aksiyon === 'aktiflik') {
                const yeniDurum = btn.dataset.yeniDurum === 'true';
                aktiflikDegistir(firmaId, yeniDurum);
            } else if (aksiyon === 'vale') {
                valeDegistir(firmaId, btn.dataset.yeniDurum === 'true');
            } else if (aksiyon === 'shuttle') {
                shuttleDegistir(firmaId, btn.dataset.yeniDurum === 'true');
            } else if (aksiyon === 'subeli-yap') {
                firmaSubeliYap(firmaId);
            } else if (aksiyon === 'sil') {
                firmaSil(firmaId);
            } else if (aksiyon === 'kota') {
                firmaKotaDuzenle(firmaId, btn.dataset.subeli === '1', parseInt(btn.dataset.toplam, 10) || 1, parseInt(btn.dataset.max, 10) || 0, btn.dataset.shuttle === '1', btn.dataset.vale === '1', parseInt(btn.dataset.valekota, 10) || 0);
            }
        });
    }

    // "Edit" buttons in the contact list (delegation).
    const rehberListesi = document.getElementById('rehberListesi');
    if (rehberListesi) {
        rehberListesi.addEventListener('click', (e) => {
            const btn = e.target.closest('button[data-aksiyon="duzenle"]');
            if (!btn) return;
            duzenleAc(btn.dataset.kullaniciAdi, btn.dataset.email, btn.dataset.telefon);
        });
    }

    // Edit dialog.
    const duzenleTelefon = document.getElementById('duzenleTelefon');
    if (duzenleTelefon) {
        duzenleTelefon.addEventListener('input', function () { this.value = this.value.replace(/[^0-9]/g, ''); });
    }
    const duzenleKaydetBtn = document.getElementById('duzenleKaydetBtn');
    if (duzenleKaydetBtn) duzenleKaydetBtn.addEventListener('click', duzenleKaydet);
    const duzenleVazgecBtn = document.getElementById('duzenleVazgecBtn');
    if (duzenleVazgecBtn) duzenleVazgecBtn.addEventListener('click', duzenleKapat);
    const duzenleOverlay = document.getElementById('duzenleOverlay');
    if (duzenleOverlay) duzenleOverlay.addEventListener('click', (e) => { if (e.target === duzenleOverlay) duzenleKapat(); });

    // Announcement e-mail dialog.
    const topluMailBtn = document.getElementById('topluMailBtn');
    if (topluMailBtn) topluMailBtn.addEventListener('click', topluMailAc);
    const topluGonderBtn = document.getElementById('topluGonderBtn');
    if (topluGonderBtn) topluGonderBtn.addEventListener('click', topluMailGonder);
    const topluVazgecBtn = document.getElementById('topluVazgecBtn');
    if (topluVazgecBtn) topluVazgecBtn.addEventListener('click', topluMailKapat);
    const topluMailOverlay = document.getElementById('topluMailOverlay');
    if (topluMailOverlay) topluMailOverlay.addEventListener('click', (e) => { if (e.target === topluMailOverlay) topluMailKapat(); });

    // Invitation link dialog.
    const davetKopyaBtn = document.getElementById('davetKopyaBtn');
    if (davetKopyaBtn) davetKopyaBtn.addEventListener('click', davetLinkKopyala);
    const davetWpBtn = document.getElementById('davetWpBtn');
    if (davetWpBtn) davetWpBtn.addEventListener('click', davetWhatsapp);
    const davetKapatBtn = document.getElementById('davetKapatBtn');
    if (davetKapatBtn) davetKapatBtn.addEventListener('click', davetModalKapat);
    const davetOverlay = document.getElementById('davetOverlay');
    if (davetOverlay) davetOverlay.addEventListener('click', (e) => { if (e.target === davetOverlay) davetModalKapat(); });

    canliBaglantiKur();
});