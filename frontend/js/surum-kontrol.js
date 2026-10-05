// "New version available, please reload" banner for the staff panels.
//
// The driver and valet PWAs update themselves through their service worker (a new
// CACHE_VERSION in sw.js triggers controllerchange and a reload). The panels have no service
// worker, so a tab that stays open keeps running old JavaScript after a deploy. This matters
// for daytime hotfixes, which would otherwise never reach those tabs.
//
// The version is read from CACHE_VERSION in /sw.js, the single source; keeping a second
// version field would drift. The server sends /sw.js with no-cache headers, so every request
// gets the real file and no extra endpoint is needed.
//
// There is no automatic reload, on purpose: an advisor may be in the middle of filling a form,
// and reloading would lose the input. The banner does not block the page; the user reloads
// when ready.
//
// CSP: no inline script or style. Styles are set through CSSOM (el.style.*), never through a
// <style> element or a style attribute.

(function () {
    const KONTROL_ARALIGI_MS = 5 * 60 * 1000;   // poll every 5 minutes
    const EN_ERKEN_TEKRAR_MS = 60 * 1000;      // never check more often than once a minute
    let yuklenenSurum = null;   // version when this tab was loaded (the baseline)
    let seritGosterildi = false;
    let sonKontrol = 0;

    async function surumuOku() {
        try {
            // no-store, so no cache layer (browser or proxy) can return a stale version
            const cevap = await fetch('/sw.js', { cache: 'no-store' });
            if (!cevap.ok) return null;
            const metin = await cevap.text();
            const eslesme = metin.match(/CACHE_VERSION\s*=\s*['"]([^'"]+)['"]/);
            return eslesme ? eslesme[1] : null;
        } catch (e) {
            return null;   // offline or transient error: try again at the next check
        }
    }

    function seridiGoster(yeniSurum) {
        if (seritGosterildi) return;
        seritGosterildi = true;

        const serit = document.createElement('div');
        serit.setAttribute('role', 'status');
        Object.assign(serit.style, {
            position: 'fixed', top: '0', left: '0', right: '0', zIndex: '99999',
            background: '#DFFF00', color: '#0a0a0a',
            padding: '10px 16px', fontSize: '14px', fontWeight: '700',
            fontFamily: 'inherit', display: 'flex', alignItems: 'center',
            justifyContent: 'center', gap: '14px', flexWrap: 'wrap',
            boxShadow: '0 2px 14px rgba(0,0,0,0.45)'
        });

        const metin = document.createElement('span');
        metin.textContent = 'Yeni sürüm yayınlandı (' + String(yeniSurum).replace('app-', '') + '). Güncel çalışmak için sayfayı yenileyin.';

        const btn = document.createElement('button');
        btn.type = 'button';
        btn.textContent = 'Şimdi Yenile';
        Object.assign(btn.style, {
            background: '#0a0a0a', color: '#DFFF00', border: 'none',
            borderRadius: '8px', padding: '7px 16px', fontSize: '13px',
            fontWeight: '700', cursor: 'pointer', fontFamily: 'inherit'
        });
        // A plain reload is enough (reload(true) is obsolete and the argument is ignored): code
        // files are revalidated with their ETag, so the new files are fetched.
        btn.addEventListener('click', () => window.location.reload());

        serit.appendChild(metin);
        serit.appendChild(btn);
        document.body.appendChild(serit);

        // The banner is position: fixed and covers the top of the page. The panels use different
        // body paddings, so measure the current one and add the banner's height to it; that
        // keeps the header and logout button from hiding under the banner.
        const mevcut = parseFloat(window.getComputedStyle(document.body).paddingTop) || 0;
        document.body.style.paddingTop = (mevcut + serit.offsetHeight) + 'px';
    }

    async function kontrolEt() {
        // Debounce. visibilitychange fires as often as the user switches tabs, which could mean
        // dozens of requests a minute; the timer alone is already every 5 minutes.
        // No debounce before the baseline exists: if the page opened offline the first read
        // failed, and taking the baseline late could mistake a deploy made in between for the
        // version this tab started with, so the banner would never appear.
        const simdi = Date.now();
        if (yuklenenSurum && simdi - sonKontrol < EN_ERKEN_TEKRAR_MS) return;
        sonKontrol = simdi;

        const surum = await surumuOku();
        if (!surum) return;
        if (!yuklenenSurum) { yuklenenSurum = surum; return; }   // first successful read is the baseline
        if (surum !== yuklenenSurum) seridiGoster(surum);
    }

    // Take the baseline now, then check periodically and whenever the tab becomes visible
    // again (a panel can sit in the background all day and be used in the afternoon).
    kontrolEt();
    const zamanlayici = setInterval(() => {
        // Once the banner is shown there is nothing more to detect; stop polling.
        if (seritGosterildi) { clearInterval(zamanlayici); return; }
        kontrolEt();
    }, KONTROL_ARALIGI_MS);
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible' && !seritGosterildi) kontrolEt();
    });
})();
