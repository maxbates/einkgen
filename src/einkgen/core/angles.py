"""Steering tokens for prompt expansion — anti-mode-collapse fuel.

The cron expands one library topic per slot via ``generate.expand_topic``
(text LLM). Without steering, the LLM converges on the 5–6 most
photogenic interpretations of any given topic — "striking world
landmark" becomes Machu Picchu / Petra / Angkor on repeat. We inject
**angle tokens** into the per-call user message to force the expansion
into a different corner of concept space each time.

Angles are organised into five axes. ``sample_angles`` picks two axes at
random and one phrase from each. The chosen phrases are passed to the
LLM as a *soft* constraint — the system prompt tells it to incorporate
them where they enhance the topic, or skip an angle that genuinely
fights the topic (e.g. "pixel art" + "macro close-up" can ignore the
scale angle).

The bag is intentionally generic across all topics. Per-topic curation
would give better hits but is more upkeep; we lean on the size of the
bag (~200 phrases across 5 axes ⇒ ~80k pairwise combinations) for
diversity instead.
"""

from __future__ import annotations

import random

# Geographic / cultural region. Bias toward specific, named places — the
# LLM does better with "Hokkaido" than "somewhere cold and northern".
REGIONS: tuple[str, ...] = (
    "Andes", "Hokkaido", "West Africa", "Patagonia", "Iceland",
    "Sahara", "Mongolia", "Scandinavia", "Mediterranean coast",
    "Pacific Northwest", "Himalayas", "Australian Outback",
    "Amazon basin", "Polynesia", "Atlantic crofts", "Pyrenees",
    "Anatolia", "Yukon", "Levantine", "Carpathians",
    "Korean peninsula", "Maghreb", "Balkans", "East African Rift",
    "Caspian shore", "Aegean", "Andalusia", "Cascadia",
    "Appalachia", "Karst plateau", "Tibetan plateau",
    "Persian highlands", "Kamchatka", "Falkland Islands",
    "Galapagos", "Lapland", "Bavarian Alps", "Cape of Good Hope",
    "Yangtze delta", "Mississippi delta", "Faroe Islands",
    "Madagascar highlands", "Atacama desert", "Gobi steppe",
    "Sicilian coast", "Newfoundland", "Hebrides", "Donegal",
    "Kyushu", "Sumatra", "Crete", "Bali interior",
    "Yucatán jungle", "Borneo rivers", "Cornish coast",
    "Sardinian interior", "Kerala backwaters", "Galician hills",
    "Greenland fjords", "Tasmanian wilderness", "Azores",
    "Svalbard", "Sicilian volcanic", "Ural foothills",
    "Java highlands", "Argentine pampas", "Vermont woods",
    "Welsh valleys", "Slovenian alpine", "Estonian forest",
)

# Era / period. Mixes historical, contemporary, and speculative so the
# LLM can pull on costume, architecture, and material cues.
ERAS: tuple[str, ...] = (
    "Bronze Age", "brutalist 1970s", "Edwardian", "Belle Époque",
    "Heian Japan", "mid-century modern", "post-industrial decay",
    "Art Deco", "Gilded Age", "Iron Age", "high Renaissance",
    "Edo period", "Victorian industrial", "1990s suburban",
    "near-future solarpunk", "Soviet 1960s", "Roman provincial",
    "Byzantine", "medieval European", "prehistoric",
    "Jazz Age", "atomic age 1950s", "Cold War East Berlin",
    "dot-com 1999", "retrofuturist 1970s", "steampunk Victorian",
    "Tang dynasty", "Mughal", "colonial Caribbean", "Hellenistic",
    "Norse Viking", "Mesoamerican classic", "Inca-era highland",
    "post-apocalyptic", "pre-Columbian", "deep-future post-human",
    "Pleistocene wild", "Holocene wild", "Weimar Berlin",
    "Edwardian seaside", "Meiji restoration", "Han dynasty",
    "early radio era", "frontier American west", "Sumerian",
    "ancient Egyptian middle kingdom", "Ottoman late",
    "Heian court", "rural French 1930s",
)

# Light and weather — atmosphere does a lot of work in a single panel.
LIGHT: tuple[str, ...] = (
    "predawn blue hour", "harsh noon sun", "golden hour rake",
    "overcast diffuse", "sodium-vapor streetlamp", "monsoon downpour",
    "dense fog", "dry thunderstorm", "snow drift", "freezing rain",
    "sun pillar", "aurora glow", "eclipse half-light",
    "neon sign wash", "candlelight", "single-bulb interior",
    "polar twilight", "dust storm", "cathedral shafts of light",
    "moonlit", "firelight", "lightning flash", "hailstorm",
    "sea fog", "smoke haze", "gas-lamp glow", "headlight beams",
    "silhouette against sky", "cloud shadow", "sun through blinds",
    "deep-shadow chiaroscuro", "white-out blizzard",
    "thin winter sun", "sunset afterglow",
    "summer-storm green sky", "phosphorescent surf",
    "iron-grey overcast", "dust-mote sunbeam through window",
    "fluorescent office strip", "torchlit cave",
)

# Scale, viewpoint, and framing. Forces the LLM off the default
# eye-level wide shot.
SCALE: tuple[str, ...] = (
    "aerial drone view", "satellite top-down", "macro close-up",
    "knee-height child's view", "fish-eye distortion",
    "telephoto compression", "low-angle hero shot", "Dutch tilt",
    "isometric projection", "behind-the-shoulder",
    "framed through doorway", "reflected in a puddle",
    "shot through fence framing", "extreme wide vista",
    "claustrophobic interior", "x-ray cutaway", "cross-section diagram",
    "exploded-view diagram", "side profile", "three-quarter view",
    "head-on symmetric composition", "worm's-eye view",
    "bird's-eye plan view", "architectural elevation",
    "split-frame diptych", "extreme rule-of-thirds",
    "single-subject negative space", "vignette through binoculars",
)

# Mood / emotional register. Steers tone without dictating subject.
MOOD: tuple[str, ...] = (
    "melancholy stillness", "joyful chaos", "eerie quiet",
    "triumphant", "contemplative", "ominous foreboding",
    "playful absurdity", "austere monastic", "festive crowded",
    "lonely abandoned", "defiant", "tender intimate",
    "surreal dreamlike", "rigorous formal",
    "raw documentary realism", "mythic legendary",
    "mundane sublime", "anxious tension", "serene balance",
    "comic deadpan", "nostalgic warm", "clinical sterile",
    "feral wild", "ceremonial sacred", "ramshackle improvised",
)

AXES: dict[str, tuple[str, ...]] = {
    "region": REGIONS,
    "era": ERAS,
    "light": LIGHT,
    "scale": SCALE,
    "mood": MOOD,
}


def sample_angles(
    *,
    n_axes: int = 2,
    rng: random.Random | None = None,
) -> list[str]:
    """Pick ``n_axes`` axes at random and one phrase from each.

    Returns a list of phrase strings (no axis labels). Order is randomised
    so the LLM doesn't always see region-before-era. Caller is free to
    join with ", " or feed as a bullet list — see ``generate.expand_topic``.

    ``n_axes`` is capped at the number of axes available; passing a
    larger value just returns one phrase per axis.
    """
    if n_axes <= 0:
        return []
    r = rng or random
    axis_names = list(AXES.keys())
    k = min(n_axes, len(axis_names))
    chosen_axes = r.sample(axis_names, k)
    return [r.choice(AXES[a]) for a in chosen_axes]
