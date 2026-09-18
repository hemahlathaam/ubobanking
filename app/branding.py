"""Brand registry for RUBO and COB. Single source of truth for org theming."""

ORGS = {
    "rubo": {
        "slug": "rubo",
        "name": "RUBO",
        "full_name": "Republic of United Boys Organization",
        "tagline": "Smart Banking for the Modern Era",
        "short_tagline": "Republic of United Boys Organization",
        "accent": "#315efb",
        "accent_dark": "#2249ce",
        "accent_soft": "#eef2ff",
        "ring": "#e0b13a",
        "ring_soft": "#fff6db",
        "logo_emoji": "⚡",
        "partner_label": "In collaboration with COB",
        "partner_slug": "cob",
    },
    "cob": {
        "slug": "cob",
        "name": "COB",
        "full_name": "Civilisation of Boys",
        "tagline": "Civilisation of Boys",
        "short_tagline": "Civilisation of Boys",
        "accent": "#7a1f1f",
        "accent_dark": "#5c1515",
        "accent_soft": "#fbeaea",
        "ring": "#d4af37",
        "ring_soft": "#fff6db",
        "logo_emoji": "🛡️",
        "partner_label": "In collaboration with RUBO",
        "partner_slug": "rubo",
    },
}

DEFAULT_ORG = "rubo"
PARTNERSHIP_LINE = (
    "RUBO × COB — Republic of United Boys Organization & Civilisation of Boys"
)


def get_org(slug):
    if not slug:
        return ORGS[DEFAULT_ORG]
    return ORGS.get(slug.lower(), ORGS[DEFAULT_ORG])


def all_orgs():
    return list(ORGS.values())
