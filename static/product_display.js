/**
 * Sneakerbot — affichage produit (modèle + ref + image), synchronisé sur #modelSelect.
 * Données: /static/sneakers_db.json
 *
 * Matching strict marque+modèle : si la marque DB ne correspond pas à la marque
 * demandée, l'image est rejetée et on affiche le fallback neutre.
 */
(function () {
  const DATA_URL = '/static/sneakers_db.json';
  const NO_IMAGE_SRC = '/static/no-image.png';

  function loadSneakersDb() {
    if (window.sneakersDB && typeof window.sneakersDB === 'object') return Promise.resolve(window.sneakersDB);
    return fetch(DATA_URL, { cache: 'no-store' })
      .then(function (res) {
        if (!res.ok) throw new Error('sneakers_db load failed: ' + res.status);
        return res.json();
      })
      .then(function (data) {
        window.sneakersDB = data;
        return data;
      })
      .catch(function (e) {
        console.warn('[sneakers_db]', e && e.message ? e.message : e);
        window.sneakersDB = window.sneakersDB || {};
        return window.sneakersDB;
      });
  }

  function clearImageFallbackTimer(img) {
    if (img && img.__sbImgTimer) {
      clearTimeout(img.__sbImgTimer);
      img.__sbImgTimer = null;
    }
  }

  function hideProductPanel() {
    var container = document.getElementById('product-info');
    var img = document.getElementById('product-image');
    var nameEl = document.getElementById('product-name');
    var refEl = document.getElementById('product-ref');
    if (container) container.style.display = 'none';
    if (nameEl) {
      nameEl.textContent = '';
      nameEl.style.opacity = '';
    }
    if (refEl) refEl.textContent = '';
    if (img) {
      clearImageFallbackTimer(img);
      img.onload = null;
      img.removeAttribute('src');
      img.alt = '';
      img.style.display = '';
      img.style.background = '';
      img.style.minHeight = '';
      img.onerror = null;
    }
  }

  /**
   * Affiche le produit correspondant à (model, brand).
   * @param {string} model - Nom exact du modèle (clé dans sneakers_db.json)
   * @param {string|null} brand - Marque attendue (validation stricte cross-brand)
   */
  function displayProduct(model, brand) {
    var key = String(model == null ? '' : model).trim();
    if (!key) {
      hideProductPanel();
      return;
    }
    if (!window.sneakersDB || typeof window.sneakersDB !== 'object') {
      loadSneakersDb().then(function () {
        displayProduct(key, brand);
      });
      return;
    }

    var img = document.getElementById('product-image');
    var nameEl = document.getElementById('product-name');
    var refEl = document.getElementById('product-ref');
    var container = document.getElementById('product-info');
    if (!img || !nameEl || !refEl) return;

    var db = window.sneakersDB;

    // Lookup insensible à la casse
    var product = db[key];
    if (!product) {
      var keyLower = key.toLowerCase();
      for (var dbKey in db) {
        if (dbKey.toLowerCase() === keyLower) {
          product = db[dbKey];
          break;
        }
      }
    }

    if (!product) {
      console.warn('[sneakers_db] MODEL NOT FOUND:', key);
      hideProductPanel();
      return;
    }

    // Validation stricte de la marque — empêche toute confusion cross-brand
    var brandNorm = String(brand || '').trim().toLowerCase();
    var dbBrand = String(product.brand || '').trim().toLowerCase();
    if (brandNorm && dbBrand && brandNorm !== dbBrand) {
      console.warn('[sneakers_db] BRAND MISMATCH — demandé:', brand, '/ DB:', product.brand, '/ modèle:', key);
      hideProductPanel();
      return;
    }

    nameEl.textContent = key;
    refEl.textContent = 'Ref: ' + (product.ref || '');
    img.alt = key;

    clearImageFallbackTimer(img);
    img.onload = null;
    img.onerror = null;

    img.style.background = '#111';
    img.style.minHeight = '120px';
    img.style.display = 'block';

    function applyImageFallback(reason) {
      clearImageFallbackTimer(img);
      img.onerror = null;
      if (reason) console.warn('[sneakers_db] image fallback:', reason);
      img.style.background = '';
      img.style.minHeight = '';
      img.src = NO_IMAGE_SRC;
      img.style.display = 'block';
      nameEl.style.opacity = '1';
    }

    img.onerror = function () {
      console.warn('[sneakers_db] Image failed, fallback triggered');
      clearImageFallbackTimer(this);
      this.onerror = null;
      this.style.background = '';
      this.style.minHeight = '';
      this.src = NO_IMAGE_SRC;
      nameEl.style.opacity = '1';
    };

    img.onload = function () {
      clearImageFallbackTimer(img);
      if (String(img.src || '').indexOf(NO_IMAGE_SRC) === -1) {
        img.style.background = '';
        img.style.minHeight = '';
      }
      nameEl.style.opacity = '1';
    };

    // Rejette les placeholders explicites côté client
    var url = product.image && String(product.image).trim();
    var isPlaceholder = !url || url.indexOf('no-image') !== -1;

    if (url && !isPlaceholder) {
      img.src = url;
      img.__sbImgTimer = setTimeout(function () {
        img.__sbImgTimer = null;
        if (!img.complete || img.naturalWidth === 0) {
          applyImageFallback('Image timeout');
        }
      }, 3000);
    } else {
      applyImageFallback('Pas d\'image valide pour ce modèle');
    }
    if (container) container.style.display = 'flex';
  }

  window.displayProduct = displayProduct;
  window.loadSneakersDb = loadSneakersDb;

  function _getBrandSelectValue() {
    var brandSelect = document.querySelector('#brandSelect');
    return brandSelect ? String(brandSelect.value || '').trim() : null;
  }

  function bindModelSelect() {
    var modelSelect = document.querySelector('#modelSelect');
    if (!modelSelect) return;
    if (!window.__sneakerbotModelSelectBound) {
      window.__sneakerbotModelSelectBound = true;
      modelSelect.addEventListener('change', function () {
        var brand = _getBrandSelectValue();
        console.log('[sneakers_db] Selected model:', modelSelect.value, '/ brand:', brand);
        displayProduct(modelSelect.value, brand);
      });
    }
    if (modelSelect.value) {
      displayProduct(modelSelect.value, _getBrandSelectValue());
    }
  }

  function bindTableRows() {
    var tbody = document.getElementById('tableBody');
    if (!tbody) return;
    tbody.addEventListener(
      'click',
      function (e) {
        var tr = e.target.closest('tr[data-model-id]');
        if (!tr) return;
        var modelId = tr.dataset.modelId || '';
        // Format attendu: "Brand|Model"
        var parts = modelId.split('|');
        var brandName = parts.length > 1 ? parts[0].trim() : null;
        var modelName = parts.length > 1 ? parts.slice(1).join('|').trim() : parts[0].trim();
        if (modelName && typeof displayProduct === 'function') {
          displayProduct(modelName, brandName);
        }
      },
      true
    );
  }

  function initAll() {
    loadSneakersDb().finally(function () {
      bindModelSelect();
      bindTableRows();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initAll);
  } else {
    initAll();
  }
})();

document.addEventListener('DOMContentLoaded', function () {
  var select = document.querySelector('#modelSelect');
  var brandSelect = document.querySelector('#brandSelect');
  var brand = brandSelect ? String(brandSelect.value || '').trim() : null;
  if (select && select.value && typeof window.displayProduct === 'function') {
    window.displayProduct(select.value, brand);
  }
});
