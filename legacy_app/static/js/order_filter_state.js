// Per-tab UI preferences only. Never persist selections, tokens or credentials.
(function () {
    const key = 'rainbow.order-filters.v1';
    const ids = ['orderWarehouseFilter','orderCategoryFilter','orderSearch',
        'orderViewFilter','orderPayFilter','orderStartDate','orderEndDate',
        'orderStartDateTime','orderEndDateTime','ordersPageSize'];
    let signedOut = false;
    function clear() { try { sessionStorage.removeItem(key); } catch (_) {} }
    function save() {
        if (signedOut) return;
        const values = {};
        ids.forEach(id => { const e = document.getElementById(id); if (e) values[id] = e.value; });
        try { sessionStorage.setItem(key, JSON.stringify({version:1, at:Date.now(), values:values})); } catch (_) {}
    }
    function restore() {
        let state;
        try { state = JSON.parse(sessionStorage.getItem(key)); } catch (_) { clear(); return; }
        if (!state) return;
        if (state.version !== 1 || !state.values || !Number.isFinite(state.at) ||
                Date.now()-state.at > 12*3600000 || state.at > Date.now()+60000) { clear(); return; }
        ids.forEach(id => {
            const e = document.getElementById(id), value = state.values[id];
            if (!e || typeof value !== 'string' || value.length > 500) return;
            if (e.tagName === 'SELECT' && !Array.from(e.options).some(o => o.value === value)) return;
            e.value = value;
        });
    }
    window.RainbowOrderFilterState = {save:save, restore:restore, clear:clear};
    if (location.pathname === '/login') { signedOut = true; clear(); }
    document.addEventListener('click', function (event) {
        const link = event.target.closest('a[href]');
        if (link && new URL(link.href, location.href).pathname === '/logout') { signedOut = true; clear(); }
    }, true);
})();
