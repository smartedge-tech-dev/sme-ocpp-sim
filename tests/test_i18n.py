"""i18n guards for the web UI (sme-sim-common checklist §3)."""

from pathlib import Path

from sme_sim_common.testing import check_i18n_keys, iter_i18n_keys

STATIC = Path(__file__).resolve().parents[1] / "sme_ocpp_sim" / "static"


def test_no_reserved_i18n_keys():
    # shell.* / vendor.* / common.* belong to sme-sim-common and must not be redefined by the product.
    assert check_i18n_keys(STATIC) == []


def test_product_dictionaries_complete():
    # en.js is the reference dictionary; ja / zh must cover exactly the same keys.
    keys = {}
    for lang in ("en", "ja", "zh"):
        keys[lang] = {k.key for k in iter_i18n_keys(STATIC / "i18n" / f"{lang}.js") if k.lang == lang}
    assert keys["en"], "en.js defines no strings"
    for lang in ("ja", "zh"):
        assert keys[lang] - keys["en"] == set(), f"{lang}.js has keys missing from en.js"
        assert keys["en"] - keys[lang] == set(), f"{lang}.js lacks keys defined in en.js"
