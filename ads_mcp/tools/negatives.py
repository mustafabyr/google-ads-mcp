# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Turkish helpers for negative keyword lists. Pure functions; no API.

Negative keywords never match close variants, so a Turkish negative has to
be listed in every form a searcher might type: "kurs" blocks "kurs" but
not "kursta", "kurslar" or "kursunda", and none of them block the same
word typed without Turkish letters ("kursu" vs "kursu" is fine, but
"ücretsiz" and "ucretsiz" are two different negatives to Google).

``expand_turkish`` writes those forms out. It follows vowel harmony and
consonant hardening, which covers regular nouns; it does not know about
stems that change (burun -> burnu) or loanwords with a soft final
consonant (kitap -> kitabı), so read the list before adding it. It adds
nothing to any list -- that is add_to_negative_keyword_list's job.
"""

from typing import Any, Dict, List

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

negatives_mcp = FastMCP("negatives")

_BACK = set("aıou")
_FRONT = set("eiöü")
_VOWELS = _BACK | _FRONT
# "fıstıkçı şahap": after these the suffix consonant hardens, d -> t.
_VOICELESS = set("fstkçşhp")

_ASCII = str.maketrans(
    {
        "ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u",
        "Ç": "C", "Ğ": "G", "İ": "I", "Ö": "O", "Ş": "S", "Ü": "U",
    }
)


def _last_vowel(word: str) -> str:
    for ch in reversed(word):
        if ch in _VOWELS:
            return ch
    return "a"


def _two_way(word: str) -> str:
    """a or e, for -da/-de, -lar/-ler."""
    return "a" if _last_vowel(word) in _BACK else "e"


def _four_way(word: str) -> str:
    """ı, i, u or ü, for the possessive buffer vowel."""
    vowel = _last_vowel(word)
    if vowel in "aı":
        return "ı"
    if vowel in "ei":
        return "i"
    if vowel in "ou":
        return "u"
    return "ü"


def _suffixed_forms(word: str) -> Dict[str, str]:
    """The inflected forms of one word, keyed by what they are."""
    word = word.strip()
    if not word:
        return {}
    a_e = _two_way(word)
    consonant = "t" if word[-1] in _VOICELESS else "d"
    locative = f"{word}{consonant}{a_e}"
    if word[-1] in _VOWELS:
        possessive = f"{word}s{_four_way(word)}"
    else:
        possessive = f"{word}{_four_way(word)}"
    return {
        "locative": locative,  # kursta, evde
        "locative_relative": f"{locative}ki",  # kurstaki, evdeki
        "possessive_locative": f"{possessive}nd{a_e}",  # kursunda, evinde
        "plural": f"{word}l{a_e}r",  # kurslar, evler
    }


def ascii_form(text: str) -> str:
    """The same text typed without Turkish letters."""
    return text.translate(_ASCII)


def expand(term: str) -> Dict[str, Any]:
    """Pure function behind the tool; importable without the server."""
    cleaned = " ".join(str(term or "").split()).lower()
    if not cleaned:
        raise ValueError("Give a term.")
    words = cleaned.split(" ")
    head, last = words[:-1], words[-1]
    forms = _suffixed_forms(last)
    suffixed = [" ".join(head + [form]) for form in forms.values()]

    everything: List[str] = []
    for candidate in [cleaned] + suffixed:
        for variant in (candidate, ascii_form(candidate)):
            if variant not in everything:
                everything.append(variant)

    return {
        "term": cleaned,
        "suffixed": suffixed,
        "forms": {name: " ".join(head + [form]) for name, form in forms.items()},
        "ascii": sorted(
            {ascii_form(v) for v in [cleaned] + suffixed}
            - set([cleaned] + suffixed)
        ),
        "all": everything,
        "count": len(everything),
        "note": (
            "Suffixes follow vowel harmony and consonant hardening; stems "
            "that change (burun -> burnu) and softening loanwords (kitap -> "
            "kitabı) are not handled. Read the list, then add what applies "
            "with add_to_negative_keyword_list. Nothing was added."
        ),
    }


@negatives_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def expand_turkish(term: str) -> Dict[str, Any]:
    """Writes out the Turkish forms a negative keyword needs to cover.

    For a term it returns the locative (-de/-da/-te/-ta), the locative
    relative (-deki/-daki), the possessive locative (-inde/-ında, -sında
    after a vowel), the plural (-ler/-lar), and every one of those typed
    without Turkish letters (ü -> u, ş -> s, ı -> i ...). Negatives never
    match close variants, so each form is a separate negative to Google.

    On a multi-word term only the last word is inflected. It adds nothing
    anywhere: pick the forms that apply and pass them to
    add_to_negative_keyword_list.

    Args:
        term: The word or phrase, e.g. "ücretsiz kurs".

    Returns:
        `all`, the full list of forms; `forms`, each by name; `ascii`, the
        unaccented spellings; and a note on what the rules do not cover.
    """
    return expand(term)
