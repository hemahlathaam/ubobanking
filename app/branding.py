"""Brand registry for RUBO. Single source of truth for branding."""

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
        "partner_label": "",
        "partner_slug": "",
    },
}

DEFAULT_ORG = "rubo"
PARTNERSHIP_LINE = "Republic of United Boys Organization"


def get_org(slug):
    if not slug:
        return ORGS[DEFAULT_ORG]
    return ORGS.get(slug.lower(), ORGS[DEFAULT_ORG])


def all_orgs():
    return list(ORGS.values())
