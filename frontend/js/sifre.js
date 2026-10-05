// sifre.html: a new user sets their password and display name from the invitation link.
// Event handlers are attached here rather than inline because the CSP forbids inline scripts.

async function sifreyiKaydet() {
    const s1 = document.getElementById('s1').value;
    const s2 = document.getElementById('s2').value;
    // The display name is what the panels show; the username is an ASCII login identity and
    // is not meant for display.
    const gorunenAd = document.getElementById('gorunenAd').value.trim();
    const token = new URLSearchParams(window.location.search).get('token');

    if (gorunenAd.length < 2) return alert("Lütfen adınızı ve soyadınızı girin.");
    if (gorunenAd.length > 60) return alert("Ad en fazla 60 karakter olabilir.");

    if (s1 !== s2) return alert("Şifreler uyuşmuyor!");
    // Same password rules as the server (8+ characters, a letter and a digit), checked here
    // for immediate feedback.
    if (s1.length < 8) return alert("Şifre minimum 8 karakter olmalı!");
    // bcrypt accepts at most 72 bytes, and Turkish letters take two bytes each in UTF-8.
    if (new TextEncoder().encode(s1).length > 72) {
        return alert("Şifre çok uzun. Lütfen daha kısa bir şifre seçin (Türkçe karakterler iki kez sayılır).");
    }
    if (!/[A-Za-z]/.test(s1) || !/\d/.test(s1)) {
        return alert("Şifre en az 1 harf ve 1 rakam içermeli!");
    }

    let res;
    try {
        res = await fetch(`${CONFIG.BASE_URL}/sifre-belirle`, {
            method: 'POST',
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                token: token,
                yeni_sifre: s1,
                gorunen_ad: gorunenAd
            })
        });
    } catch (e) {
        // The request never reached the server; without this the page would just do nothing.
        alert("Sunucuya ulaşılamadı. İnternet bağlantınızı kontrol edip tekrar deneyin.");
        return;
    }
    if (res.ok) {
        alert("Şifreniz oluşturuldu! Giriş ekranına yönlendiriliyorsunuz.");
        window.location.href = "login.html";
    } else {
        // Show the server's reason (expired link, password rules and so on) when there is one.
        let detay = "Link geçersiz veya süresi dolmuş.";
        try { const j = await res.json(); if (j.detail) detay = j.detail; } catch (e) { }
        alert(detay);
    }
}

document.addEventListener('DOMContentLoaded', () => {
    const btn = document.getElementById('aktiflestirBtn');
    if (btn) btn.addEventListener('click', sifreyiKaydet);

    // Enter in any field submits the form.
    document.querySelectorAll('input').forEach(input => {
        input.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') sifreyiKaydet();
        });
    });
});