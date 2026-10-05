// login.html: staff login. Handlers are attached here because the CSP forbids inline scripts.

// Pages that log the user out redirect here with ?sebep=<reason>; tell the user why.
// firma_pasif comes from a 402 (company deactivated). Without this message the valet app
// would just say "no active task" and the panels would stay empty.
const _sebep = new URLSearchParams(window.location.search).get('sebep');
if (_sebep === 'oturum_doldu') {
    const el = document.getElementById('oturum-mesaji');
    if (el) el.style.display = 'block';
} else if (_sebep === 'firma_pasif') {
    const hataEl = document.getElementById('hataMesaji');
    if (hataEl) {
        hataEl.innerText = "Firmanızın hesabı şu anda pasif durumda. Lütfen Shuttle & Valet Ops ekibi ile iletişime geçiniz.";
        hataEl.style.display = 'block';
    }
}

async function girisYap() {
    const kAdi = document.getElementById('kullaniciAdi').value.trim();
    const sifre = document.getElementById('sifre').value.trim();
    const btn = document.getElementById('girisBtn');
    const hata = document.getElementById('hataMesaji');

    if (!kAdi || !sifre) {
        hata.innerText = "Lütfen kullanıcı adı ve şifreyi boş bırakmayınız.";
        hata.style.display = 'block';
        return;
    }

    btn.innerText = "Doğrulanıyor...";
    btn.disabled = true;
    hata.style.display = 'none';

    try {
        const cevap = await fetch(CONFIG.BASE_URL + "/giris-yap", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ kullanici_adi: kAdi, sifre: sifre })
        });

        if (cevap.ok) {
            const sonuc = await cevap.json();
            btn.innerText = "Yönlendiriliyor...";

            localStorage.setItem('app_token', sonuc.token);
            localStorage.setItem('aktif_rol', sonuc.rol);
            localStorage.setItem('aktif_firma_id', sonuc.firma_id);
            localStorage.setItem('aktif_kullanici_adi', sonuc.kullanici_adi);
            localStorage.setItem('aktif_marka', sonuc.marka);
            localStorage.setItem('aktif_sube_id', sonuc.sube_id || '');  // empty = headquarters or no branches
            // Module flags, stored so the panels can draw the right sections before any request.
            localStorage.setItem('aktif_shuttle_aktif', sonuc.shuttle_aktif === false ? '0' : '1');
            localStorage.setItem('aktif_vale_aktif', sonuc.vale_aktif ? '1' : '0');

            // Admin of an inactive company: the server lets them log in, but every protected
            // endpoint answers 402, so do not open a session; show a notice instead.
            if (sonuc.firma_pasif) {
                await appCikis();  // discard the token just received; it cannot be used
                hata.innerText = `"${sonuc.firma_adi || 'Firmanız'}" şu an pasif durumdadır. Lütfen Shuttle & Valet Ops ekibi ile iletişime geçiniz.`;
                hata.style.display = 'block';
                btn.innerText = "Sisteme Gir";
                btn.disabled = false;
                return;
            }

            if (sonuc.rol === "SOFOR") {
                localStorage.setItem("aktif_arac_id", sonuc.arac_id);
                window.location.href = "sofor.html";
            }
            else if (sonuc.rol === "ADMIN") window.location.href = "admin.html";
            else if (sonuc.rol === "SUPERADMIN") window.location.href = "superadmin.html";
            else if (sonuc.rol === "DANISMAN") window.location.href = "danisman.html";
            else if (sonuc.rol === "OPERASYON") window.location.href = "operasyon.html";
            else if (sonuc.rol === "VALE") window.location.href = "vale.html";
            else {
                appCikis();
                hata.innerText = "Hesabınızın yetki rolü geçersiz. Yöneticinize başvurun.";
                hata.style.display = 'block';
                btn.innerText = "Sisteme Gir";
                btn.disabled = false;
            }

        } else {
            // Other roles of an inactive company get 403 with "PASIF_FIRMA:<company name>".
            if (cevap.status === 403) {
                const hataJson = await cevap.json();
                if (hataJson.detail && hataJson.detail.startsWith("PASIF_FIRMA:")) {
                    const firmaAdi = hataJson.detail.split(":")[1];
                    hata.innerText = `"${firmaAdi}" firması şu an pasif durumda. Lütfen yöneticinizle iletişime geçin.`;
                    hata.style.display = 'block';
                    btn.innerText = "Sisteme Gir";
                    btn.disabled = false;
                    return;
                }
            }
            throw new Error("Hatalı giriş");
        }
    } catch (error) {
        hata.innerText = "Hatalı kullanıcı adı veya şifre girdiniz.";
        hata.style.display = 'block';
        btn.innerText = "Sisteme Gir";
        btn.disabled = false;
    }
}

document.addEventListener('DOMContentLoaded', () => {
    const girisBtn = document.getElementById('girisBtn');
    if (girisBtn) girisBtn.addEventListener('click', girisYap);

    // Enter in the password field logs in.
    const sifreInput = document.getElementById('sifre');
    if (sifreInput) {
        sifreInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') girisYap();
        });
    }
});