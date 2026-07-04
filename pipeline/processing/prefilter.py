"""Free deterministic pre-filter (runs before any LLM call).

An article must mention a road-crash OR infra-defect term — in any of the 13
configured languages — anywhere in its clean text, else it is marked irrelevant
without spending a token. Google-News-sourced items usually pass (queries are
already keyword-targeted); this mainly kills GDELT wide-net noise and listing junk.
"""
from functools import lru_cache

from pipeline import configload


@lru_cache(maxsize=1)
def _terms() -> list[str]:
    kw = configload.keywords()
    out: set[str] = set()
    for lang_terms in kw.values():
        for cat in ("crash", "infra_defect", "crash_type"):
            for t in lang_terms.get(cat, []):
                t = t.lower()
                out.add(t)
                # Indic languages inflect suffixes (ta: விபத்து -> விபத்தில்);
                # a last-char-stripped stem catches the common case-endings.
                if len(t) >= 4:
                    out.add(t[:-1])
    return sorted(out)


def passes(text: str) -> bool:
    hay = (text or "").lower()
    return any(t in hay for t in _terms())
