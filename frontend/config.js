// Shared front-end configuration, loaded by every page.

const CONFIG = {
    // The API is served from the same origin as the pages, so whatever address the site was
    // opened from (domain, IP or localhost) is also the API address.
    BASE_URL: window.location.origin
};

// Log out: revoke the token on the server, then clear everything this app stored locally.
window.appCikis = async function() {
    const token = localStorage.getItem('app_token');
    
    if (token) {
        try {
            await fetch(`${CONFIG.BASE_URL}/logout`, {
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${token}`,
                    'Content-Type': 'application/json'
                }
            });
        } catch (err) {
            // Server unreachable (offline?): still clear local data below.
            console.warn('Logout isteği başarısız (offline?):', err);
        }
    }
    
    // Remove only this app's keys: those with these prefixes plus a few exact names.
    // Clearing local data also removes cached personal data such as a driver's passenger list.
    const app_prefixleri = ['app_', 'aktif_'];
    const app_anahtarlari = ['yetkili'];
    
    const silinecek_anahtarlar = [];
    for (let i = 0; i < localStorage.length; i++) {
        const anahtar = localStorage.key(i);
        if (!anahtar) continue;
        
        const prefix_eslesti = app_prefixleri.some(p => anahtar.startsWith(p));
        const tam_eslesme = app_anahtarlari.includes(anahtar);
        
        if (prefix_eslesti || tam_eslesme) {
            silinecek_anahtarlar.push(anahtar);
        }
    }
    silinecek_anahtarlar.forEach(a => localStorage.removeItem(a));
    
    sessionStorage.clear();
};